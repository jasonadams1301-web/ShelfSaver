import subprocess

import pytest

from shelfsaver.probe import ProbeError, parse_probe, probe
from tests.helpers import make_unreadable_subtitle_video, make_video


def test_probe_reports_no_codec_for_an_unreadable_subtitle(tmp_path):
    p = make_unreadable_subtitle_video(tmp_path / "u.mkv", size="320x240", seconds=1)
    subtitles = [s for s in probe(p, "ffprobe").streams if s.kind == "subtitle"]
    assert [s.codec for s in subtitles] == [None]


def test_probe_reads_languages_and_codec(tmp_path):
    p = make_video(tmp_path / "a.mkv", audio=[("eng", "Main"), ("spa", "Spanish")])
    info = probe(p, "ffprobe")
    assert info.video_codec == "h264" and info.width == 1920
    assert [s.language for s in info.streams if s.kind == "audio"] == ["eng", "spa"]


def test_probe_rejects_garbage(tmp_path):
    (tmp_path / "b.mkv").write_bytes(b"nope")
    with pytest.raises(ProbeError):
        probe(tmp_path / "b.mkv", "ffprobe")


def test_probe_reports_basics_and_subtitles(tmp_path):
    p = make_video(tmp_path / "c.mkv", size="1280x720", subs=[("eng", "Full")])
    info = probe(p, "ffprobe")
    assert info.ext == ".mkv" and info.size == p.stat().st_size
    assert info.height == 720 and info.duration == pytest.approx(3, abs=0.5)
    assert info.bit_depth == 8 and info.video_bitrate > 0
    assert [s.language for s in info.streams if s.kind == "subtitle"] == ["eng"]
    assert not info.dolby_vision


def test_probe_marks_audio_default_and_title(tmp_path):
    p = make_video(tmp_path / "d.mkv", audio=[("eng", "Main"), ("fre", "French Dub")])
    audio = [s for s in probe(p, "ffprobe").streams if s.kind == "audio"]
    assert [s.title for s in audio] == ["Main", "French Dub"]
    assert audio[0].is_default


def test_probe_detects_interlaced(tmp_path):
    p = make_video(tmp_path / "e.mkv", interlaced=True)
    assert probe(p, "ffprobe").field_order in ("tt", "bb", "tb", "bt")


def test_probe_detects_hdr_ten_bit(tmp_path):
    p = make_video(tmp_path / "f.mkv", vcodec="libx265", hdr=True)
    info = probe(p, "ffprobe")
    assert info.bit_depth == 10 and info.color_transfer == "smpte2084"
    assert info.color_primaries == "bt2020" and info.video_codec == "hevc"


@pytest.mark.parametrize("name", ["g.mkv", "g.mp4"])
def test_probe_flags_cover_art(tmp_path, name):
    p = make_video(tmp_path / name, cover_art=True)
    info = probe(p, "ffprobe")
    assert any(s.is_cover_art for s in info.streams)
    assert info.width == 1920  # main video, not the cover image


def test_probe_flags_cover_art_in_reencoded_mkv_output(tmp_path):
    # Matroska output reports cover art as an mjpeg video with a mimetype tag, no attached_pic.
    src = make_video(tmp_path / "s.mp4", seconds=2, size="640x360", cover_art=True)
    out = tmp_path / "o.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-map", "0", "-c", "copy", str(out)],
                   check=True)
    info = probe(out, "ffprobe")
    covers = [s for s in info.streams if s.is_cover_art]
    assert len(covers) == 1 and covers[0].codec == "mjpeg"
    assert info.video_codec == "h264" and info.width == 640


def test_cover_art_listed_first_is_never_the_main_video():
    cover = {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "width": 64, "height": 64,
             "tags": {"MIMETYPE": "image/jpeg"}}
    video = {"index": 1, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
             "pix_fmt": "yuv420p", "avg_frame_rate": "25/1"}
    info = parse_probe({"format": {"duration": "10.0"}, "streams": [cover, video]}, "x.mkv", 1000)
    assert info.video_codec == "hevc" and info.width == 1920
    assert [s.is_cover_art for s in info.streams] == [True, False]


@pytest.mark.parametrize("extra", [{}, {"avg_frame_rate": "0/0", "r_frame_rate": "0/0"}])
def test_still_image_codec_without_frame_rate_is_cover_art(extra):
    still = {"index": 0, "codec_type": "video", "codec_name": "png", "width": 64, "height": 64, **extra}
    video = {"index": 1, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
             "avg_frame_rate": "25/1", "r_frame_rate": "25/1", "duration": "10.0"}
    info = parse_probe({"format": {"duration": "10.0"}, "streams": [still, video]}, "x.mkv", 1000)
    assert info.streams[0].is_cover_art and not info.streams[1].is_cover_art
    assert info.width == 1280


def _raw(**video):
    stream = {"index": 0, "codec_type": "video", "codec_name": "hevc",
              "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le"}
    stream.update(video)
    return {"format": {"duration": "10.0"}, "streams": [stream]}


def test_parse_detects_dolby_vision():
    data = _raw(side_data_list=[{"side_data_type": "DOVI configuration record"}])
    assert parse_probe(data, "x.mkv", 1000).dolby_vision is True
    assert parse_probe(_raw(), "x.mkv", 1000).dolby_vision is False


def test_bitrate_prefers_stream_then_bps_tag_then_estimate():
    assert parse_probe(_raw(bit_rate="5000000", tags={"BPS": "9"}), "x.mkv", 1000).video_bitrate == 5_000_000
    assert parse_probe(_raw(tags={"BPS": "4000000"}), "x.mkv", 1000).video_bitrate == 4_000_000
    data = _raw()
    data["streams"].append({"index": 1, "codec_type": "audio", "codec_name": "aac",
                            "bit_rate": "100000"})
    # 1_000_000 bytes over 10 s is 800_000 bit/s overall; minus 100_000 for audio
    assert parse_probe(data, "x.mkv", 1_000_000).video_bitrate == 700_000


def test_zero_duration_is_an_error():
    data = _raw()
    data["format"]["duration"] = "0"
    with pytest.raises(ProbeError):
        parse_probe(data, "x.mkv", 1000)


def test_scan_streams_reports_each_stream_separately(tmp_path):
    from shelfsaver.probe import scan_streams
    src = make_video(tmp_path / "a.mkv", seconds=4, size="320x240")
    short = tmp_path / "b.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-t", "2", "-i", str(src),
                    "-map", "0:v", "-map", "1:a", "-c", "copy", str(short)], check=True)
    info = probe(short, "ffprobe")
    spans = scan_streams(short, "ffprobe")
    video = next(s.index for s in info.streams if s.kind == "video")
    aud = next(s.index for s in info.streams if s.kind == "audio")
    assert spans[video].start == pytest.approx(0.0, abs=0.1)
    assert spans[video].end == pytest.approx(4.0, abs=0.2)
    assert spans[aud].end == pytest.approx(2.0, abs=0.1)
    assert spans[video].max_gap == pytest.approx(0.04, abs=0.01)


def test_scan_streams_reads_avi_video_that_only_has_decode_times(tmp_path):
    from shelfsaver.probe import scan_streams
    path = make_video(tmp_path / "a.avi", seconds=2, size="320x240")  # H.264 with B-frames
    video = next(s.index for s in probe(path, "ffprobe").streams if s.kind == "video")
    spans = scan_streams(path, "ffprobe", strict=False)
    assert spans[video].start == pytest.approx(0.0, abs=0.1)
    assert spans[video].end == pytest.approx(2.0, abs=0.2)


def test_scan_streams_rejects_garbage(tmp_path):
    from shelfsaver.probe import scan_streams
    bad = tmp_path / "x.mkv"
    bad.write_bytes(b"nope")
    with pytest.raises(ProbeError):
        scan_streams(bad, "ffprobe")


def test_scan_streams_tolerance_for_damaged_file(tmp_path):
    import random
    from shelfsaver.probe import scan_streams
    path = make_video(tmp_path / "d.mkv", seconds=4, size="320x240", vcodec="libx265", crf=18)
    data = bytearray(path.read_bytes())
    mid = len(data) // 2
    rng = random.Random(7)
    data[mid:mid + 4000] = bytes(rng.randrange(256) for _ in range(4000))
    path.write_bytes(bytes(data))
    with pytest.raises(ProbeError):
        scan_streams(path, "ffprobe", strict=True)
    assert scan_streams(path, "ffprobe", strict=False)


def test_scan_streams_ties_ffprobe_to_the_app(tmp_path, monkeypatch):
    from shelfsaver import probe as probe_module
    tied = []
    monkeypatch.setattr(probe_module, "kill_with_app", lambda proc: tied.append(proc.pid))
    path = make_video(tmp_path / "a.mkv", seconds=1, size="320x180")
    assert probe_module.scan_streams(path, "ffprobe")
    assert len(tied) == 1


# ---- commentary soundtracks

def _audio(index, title=None, comment=0, kind="audio"):
    raw = {"index": index, "codec_type": kind, "codec_name": "aac",
           "tags": {"language": "eng"}, "disposition": {"default": 0, "comment": comment}}
    if title is not None:
        raw["tags"]["title"] = title
    return raw


def _commentary_flags(*audio_streams):
    video = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}
    data = {"format": {"duration": "10.0"}, "streams": [video, *audio_streams]}
    return [s.is_commentary for s in parse_probe(data, "x.mkv", 1000).streams if s.kind == "audio"]


def test_the_comment_flag_marks_an_audio_track_as_commentary():
    assert _commentary_flags(_audio(1), _audio(2, comment=1)) == [False, True]


@pytest.mark.parametrize("title", [
    "Commentary", "Director's Commentary", "Audio commentary with the cast", "COMMENTARY TRACK",
    "English (Commentary)"])
def test_a_commentary_title_marks_an_audio_track_as_commentary(title):
    assert _commentary_flags(_audio(1, "English"), _audio(2, title)) == [False, True]


@pytest.mark.parametrize("title", [
    None, "", "English", "Main", "Surround 5.1", "Commentator voice-over", "Comments"])
def test_ordinary_audio_titles_are_not_commentary(title):
    assert _commentary_flags(_audio(1, title), _audio(2, "Spanish")) == [False, False]


def test_only_audio_is_ever_marked_commentary():
    video = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "tags": {"title": "Commentary"}, "disposition": {"comment": 1}}
    sub = _audio(2, "Commentary", comment=1, kind="subtitle")
    data = {"format": {"duration": "10.0"}, "streams": [video, _audio(1), sub]}
    assert not any(s.is_commentary for s in parse_probe(data, "x.mkv", 1000).streams)


def test_probe_reads_the_title_of_a_commentary_track_from_a_real_file(tmp_path):
    p = make_video(tmp_path / "t.mkv", audio=[("eng", "English"), ("eng", "Director's Commentary")])
    audio = [s for s in probe(p, "ffprobe").streams if s.kind == "audio"]
    assert [s.is_commentary for s in audio] == [False, True]


def test_probe_reads_the_comment_disposition_from_a_real_file(tmp_path):
    src = make_video(tmp_path / "src.mkv", audio=[("eng", "English"), ("eng", "Extra")])
    out = tmp_path / "flagged.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0", "-c", "copy",
                    "-disposition:a:1", "comment", str(out)], check=True)
    audio = [s for s in probe(out, "ffprobe").streams if s.kind == "audio"]
    assert [s.title for s in audio] == ["English", "Extra"]  # the title alone would not match
    assert [s.is_commentary for s in audio] == [False, True]
