import json
import subprocess
import sys
from pathlib import Path

import pytest

from shelfsaver.encode import (build_command, encoder_works, output_extension, parse_fps,
                               parse_speed, run_ffmpeg)
from shelfsaver.models import Stream
from shelfsaver.probe import probe
from shelfsaver.quality import LEVELS
from tests.helpers import convert, eng_spa, info, make_video, strip


@pytest.mark.gpu
def test_encoder_check_passes_on_a_working_graphics_card(hardware_encoder):
    assert encoder_works("ffmpeg", hardware_encoder) is True


def test_encoder_check_fails_when_ffmpeg_reports_an_error():
    # Python rejects FFmpeg's arguments and exits with an error, as a failed encode would.
    assert encoder_works(sys.executable) is False


def test_encoder_check_fails_when_ffmpeg_cannot_start(tmp_path):
    assert encoder_works(tmp_path / "missing.exe") is False

S, O = Path("in.mkv"), Path("out.mkv")


def _streams(*extra, first_audio="aac"):
    base = [Stream(0, "video", "h264", is_default=True),
            Stream(1, "audio", first_audio, "eng", is_default=True)]
    return tuple(base) + tuple(extra)


@pytest.mark.parametrize("ext,out", [
    (".avi", ".mkv"), (".mpg", ".mkv"), (".wmv", ".mkv"), (".mkv", ".mkv"),
    (".mp4", ".mp4"), (".m4v", ".m4v"),
])
def test_output_extension(ext, out):
    assert output_extension(info(ext=ext, streams=_streams()), convert()) == out


def test_mp4_with_pgs_subs_switches_to_mkv():
    streams = _streams(Stream(2, "subtitle", "hdmv_pgs_subtitle", "eng"))
    assert output_extension(info(ext=".mp4", streams=streams), convert()) == ".mkv"


def test_mp4_with_unsupported_audio_switches_to_mkv():
    assert output_extension(info(ext=".mp4", streams=_streams(first_audio="dts")), convert()) == ".mkv"


def test_mp4_ignores_removed_unsupported_audio():
    streams = _streams(Stream(2, "audio", "dts", "spa"))
    assert output_extension(info(ext=".mp4", streams=streams), convert(remove_audio=(2,))) == ".mp4"


def test_cq_comes_from_level():
    cmd = build_command(info(), convert(), "medium", S, O, "ffmpeg")
    assert cmd[cmd.index("-cq") + 1] == str(LEVELS["medium"].cq)


def test_removed_audio_is_not_mapped():
    cmd = build_command(info(streams=eng_spa()), convert(remove_audio=(2,)), "medium", S, O, "ffmpeg")
    assert "0:2" not in cmd and "0:1" in cmd


def test_data_streams_are_not_mapped():
    streams = _streams(Stream(2, "data", "bin_data"))
    cmd = build_command(info(streams=streams), convert(), "medium", S, O, "ffmpeg")
    assert "0:2" not in cmd


def test_strip_only_copies_video():
    cmd = build_command(info(streams=eng_spa()), strip(remove_audio=(2,)), "medium", S, O, "ffmpeg")
    assert "hevc_nvenc" not in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_interlaced_gets_deinterlaced():
    assert "bwdif" in " ".join(build_command(info(field_order="tt"), convert(), "medium", S, O, "ffmpeg"))


@pytest.mark.parametrize("order", ["progressive", "unknown", None])
def test_progressive_is_not_deinterlaced(order):
    assert "bwdif" not in " ".join(build_command(info(field_order=order), convert(), "medium", S, O, "ffmpeg"))


def test_8bit_and_10bit_settings():
    c8 = build_command(info(bit_depth=8), convert(), "medium", S, O, "ffmpeg")
    c10 = build_command(info(bit_depth=10), convert(), "medium", S, O, "ffmpeg")
    assert "yuv420p" in c8 and "main" in c8 and "p010le" not in c8
    assert "p010le" in c10 and "main10" in c10


def test_color_tags_copied_when_present():
    cmd = build_command(info(color_transfer="smpte2084", color_primaries="bt2020",
                             colorspace="bt2020nc"), convert(), "medium", S, O, "ffmpeg")
    assert cmd[cmd.index("-color_trc:v:0") + 1] == "smpte2084"
    assert "-color_trc:v:0" not in build_command(info(), convert(), "medium", S, O, "ffmpeg")


def test_mp4_output_is_tagged_hvc1():
    cmd = build_command(info(ext=".mp4"), convert(), "medium", Path("a.mp4"), Path("b.mp4"), "ffmpeg")
    assert "hvc1" in cmd


def test_encoder_targets_main_video_not_cover_art():
    streams = (Stream(0, "video", "mjpeg", is_cover_art=True),
               Stream(1, "video", "h264", is_default=True),
               Stream(2, "audio", "aac", "eng"))
    cmd = build_command(info(ext=".mp4", field_order="tt", streams=streams), convert(), "medium",
                        Path("a.mp4"), Path("b.mp4"), "ffmpeg")
    assert cmd[cmd.index("-c:v:1") + 1] == "hevc_nvenc"
    assert cmd[cmd.index("-c:v:0") + 1] == "copy"
    assert cmd[cmd.index("-filter:v:1") + 1] == "bwdif"
    assert "-vf" not in cmd and "-filter:v:0" not in cmd


def test_mov_text_to_mkv_becomes_srt():
    streams = _streams(Stream(2, "subtitle", "mov_text", "eng"))
    cmd = build_command(info(ext=".mp4", streams=streams), convert(), "medium", Path("a.mp4"), O, "ffmpeg")
    assert cmd[cmd.index("-c:s:0") + 1] == "srt"


def _run(src, out, encoder, decision=None):
    media = probe(src, "ffprobe")
    cmd = build_command(media, decision or convert(), "medium", src, out, "ffmpeg", encoder)
    result = run_ffmpeg(cmd, media.duration, lambda f: None, lambda: False)
    assert result.returncode == 0, result.log_tail
    return probe(out, "ffprobe")


@pytest.mark.gpu
def test_real_encode_keeps_tracks_chapters_and_cover_art(tmp_path, hardware_encoder):
    base = make_video(tmp_path / "base.mkv", seconds=2, size="640x360",
                      audio=[("eng", "A"), ("spa", "B")], subs=[("eng", None)], cover_art=True)
    meta = tmp_path / "meta.txt"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n")
    src = tmp_path / "s.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(base), "-i", str(meta), "-map", "0",
                    "-map_metadata", "1", "-c", "copy", str(src)], check=True)
    out = tmp_path / "o.mkv"
    result = _run(src, out, hardware_encoder)
    kinds = [s.kind for s in result.streams]
    assert result.video_codec == "hevc"
    assert kinds.count("audio") == 2 and kinds.count("subtitle") == 1
    assert any(s.is_cover_art for s in result.streams)
    chapters = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", str(out)],
        capture_output=True, check=True).stdout)["chapters"]
    assert len(chapters) == 2


@pytest.mark.gpu
def test_real_encode_with_mp4_cover_art_targets_main_video(tmp_path, hardware_encoder):
    src = make_video(tmp_path / "s.mp4", seconds=2, size="640x360", cover_art=True)
    result = _run(src, tmp_path / "o.mp4", hardware_encoder)
    assert result.video_codec == "hevc"
    assert any(s.is_cover_art for s in result.streams)


@pytest.mark.gpu
def test_real_encode_deinterlaces_main_video_only(tmp_path, hardware_encoder):
    src = make_video(tmp_path / "s.mp4", seconds=2, size="640x360", interlaced=True, cover_art=True)
    result = _run(src, tmp_path / "o.mp4", hardware_encoder)
    assert result.video_codec == "hevc"
    assert result.field_order in ("progressive", None, "unknown")
    assert any(s.is_cover_art for s in result.streams)


@pytest.mark.gpu
def test_real_encode_keeps_hdr(tmp_path, hardware_encoder):
    src = make_video(tmp_path / "s.mkv", seconds=2, size="640x360", vcodec="libx265", hdr=True)
    out = tmp_path / "o.mkv"
    result = _run(src, out, hardware_encoder)
    assert result.bit_depth == 10 and result.color_transfer == "smpte2084"
    assert result.color_primaries == "bt2020"
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
         "-show_frames", "-print_format", "json", str(out)], capture_output=True, check=True).stdout
    side = [d["side_data_type"] for d in json.loads(frames)["frames"][0].get("side_data_list", [])]
    assert "Mastering display metadata" in side, side
    assert "Content light level metadata" in side, side


@pytest.mark.gpu
def test_mp4_text_subs_survive_move_to_mkv(tmp_path, hardware_encoder):
    src = make_video(tmp_path / "s.mp4", seconds=2, size="640x360", subs=[("eng", "English")])
    result = _run(src, tmp_path / "o.mkv", hardware_encoder)
    subs = [s for s in result.streams if s.kind == "subtitle"]
    assert len(subs) == 1 and subs[0].codec == "subrip" and subs[0].language == "eng"


def test_stop_terminates_ffmpeg():
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=600",
           "-c:v", "libx264", "-preset", "veryslow", "-f", "null", "-"]
    seen = []
    result = run_ffmpeg(cmd, 600, seen.append, lambda: bool(seen))
    assert result.stopped is True and result.returncode != 0


_PARENT = r'''
import subprocess, sys, time
import shelfsaver.encode as enc

pidfile = sys.argv[1]
real = subprocess.Popen

class Recording(real):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        open(pidfile, "w").write(str(self.pid))

enc.subprocess.Popen = Recording
cmd = ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=25",
       "-t", "600", "-f", "null", "-"]
enc.run_ffmpeg(cmd, 600, lambda f: None, lambda: False)
'''


def _alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return "ffmpeg" in out.lower()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows job object; on Linux this is a documented no-op")
def test_ffmpeg_dies_when_the_app_is_killed(tmp_path):
    import time
    pidfile = tmp_path / "pid.txt"
    parent = subprocess.Popen([sys.executable, "-c", _PARENT, str(pidfile)])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.1)
        pid = int(pidfile.read_text())
        time.sleep(1)
        assert _alive(pid)
        subprocess.run(["taskkill", "/PID", str(parent.pid), "/F"], capture_output=True)
        for _ in range(100):
            if not _alive(pid):
                break
            time.sleep(0.1)
        assert not _alive(pid)
    finally:
        parent.kill()


# ---- conversion speed

@pytest.mark.parametrize("text, expected", [
    ("148.3", 148.3), ("  62 ", 62.0), ("0.00", None), ("0", None), ("N/A", None), ("", None),
    ("nan", None), ("inf", None), ("-5", None),
])
def test_frame_rate_is_read_from_the_progress_line(text, expected):
    assert parse_fps(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("6.2x", 6.2), ("   3.41x", 3.41), ("N/A", None), ("0x", None), ("", None), ("x", None),
    ("nanx", None),
])
def test_speed_is_read_from_the_progress_line(text, expected):
    assert parse_speed(text) == expected


def test_conversion_reports_its_frame_rate_and_speed():
    # -re reads the clip at playback speed, so FFmpeg has time to settle on a frame rate.
    cmd = ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=3",
           "-c:v", "libx264", "-preset", "ultrafast", "-f", "null", "-"]
    rates = []
    result = run_ffmpeg(cmd, 3, lambda f: None, lambda: False,
                        on_rate=lambda fps, speed: rates.append((fps, speed)))
    assert result.returncode == 0
    assert any(fps and fps > 0 for fps, _ in rates), rates
    assert any(speed and speed > 0 for _, speed in rates), rates
