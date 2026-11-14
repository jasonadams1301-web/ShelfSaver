import pytest

from shelfsaver.decide import audio_to_remove, commentary_to_remove, decide
from shelfsaver.models import Stream
from shelfsaver.quality import LEVELS, MIN_SAVINGS, resolution_bucket
from tests.helpers import audio, eng_spa, info


@pytest.mark.parametrize("codec,reason", [("hevc", "Already H.265"), ("av1", "Already an efficient format")])
def test_efficient_formats_skip(codec, reason):
    assert decide(info(video_codec=codec), "medium", False).reason == reason


def test_vp9_is_efficient_format():
    assert decide(info(video_codec="vp9"), "medium", False).reason == "Already an efficient format"


def test_bloated_h264_converts():
    assert decide(info(width=1920, video_bitrate=30_000_000), "medium", False).action == "convert"


def test_small_h264_skips():
    assert decide(info(width=1920, video_bitrate=3_000_000), "medium", False).reason == "Already small"


def test_cropped_film_uses_width_bucket():
    assert resolution_bucket(1920) == "1080"   # 1920x800 stays 1080


@pytest.mark.parametrize("width,bucket", [
    (720, "sd"), (1024, "sd"), (1025, "720"), (1400, "720"),
    (1401, "1080"), (2200, "1080"), (2201, "4k"), (3840, "4k"),
])
def test_bucket_boundaries(width, bucket):
    assert resolution_bucket(width) == bucket


def test_level_table_values():
    assert {k: v.cq for k, v in LEVELS.items()} == {"low": 22, "medium": 26, "high": 30}
    assert LEVELS["medium"].min_mbps == {"sd": 1.5, "720": 3.0, "1080": 6.0, "4k": 18.0}
    assert MIN_SAVINGS == 0.10


def test_level_changes_threshold():
    i = info(width=1920, video_bitrate=5_000_000)
    assert decide(i, "low", False).action == "skip"
    assert decide(i, "high", False).action == "convert"


def test_unknown_bitrate_converts():
    assert decide(info(video_bitrate=None), "medium", False).action == "convert"


def test_dolby_vision_skips_even_with_english_only():
    d = decide(info(dolby_vision=True, video_codec="hevc", streams=eng_spa()), "high", True)
    assert d.action == "skip" and d.reason == "Dolby Vision, left as is"


def test_hardlink_skips(): assert decide(info(), "high", False, nlink=2).reason == "Linked to another copy"
def test_read_only_skips(): assert decide(info(), "high", False, read_only=True).reason == "Read-only file"


def test_no_video_skips():
    assert decide(info(video_codec=None), "high", False).reason == "No video"


def test_precedence_hardlink_then_read_only_then_no_video_then_dv():
    i = info(video_codec=None, dolby_vision=True)
    assert decide(i, "high", False, nlink=2, read_only=True).reason == "Linked to another copy"
    assert decide(i, "high", False, read_only=True).reason == "Read-only file"
    assert decide(i, "high", False).reason == "No video"
    assert decide(info(dolby_vision=True), "high", False).reason == "Dolby Vision, left as is"


UNREADABLE_SUBTITLES = "Unreadable subtitles, left as is"


def _with_subtitle(codec, tracks=()):
    subtitle = Stream(9, "subtitle", codec, "eng", None, False, False)
    return (*(tracks or audio(("eng", None, True))), subtitle)


def test_unreadable_subtitle_skips_a_file_worth_converting():
    d = decide(info(streams=_with_subtitle(None)), "medium", False)
    assert d.action == "skip" and d.reason == UNREADABLE_SUBTITLES


def test_readable_subtitle_still_converts():
    assert decide(info(streams=_with_subtitle("subrip")), "medium", False).action == "convert"


def test_unreadable_subtitle_skips_a_soundtrack_removal():
    streams = _with_subtitle(None, eng_spa())
    d = decide(info(video_codec="hevc", streams=streams), "medium", True)
    assert d.action == "skip" and d.reason == UNREADABLE_SUBTITLES


def test_unreadable_subtitle_keeps_the_usual_reason_when_nothing_would_be_done():
    d = decide(info(video_codec="hevc", streams=_with_subtitle(None)), "medium", False)
    assert d.action == "skip" and d.reason == "Already H.265"


def test_removes_non_english():
    assert audio_to_remove(audio(("eng", None, True), ("spa", None, False))) == (2,)


def test_keeps_all_when_first_track_not_english():
    assert audio_to_remove(audio(("jpn", None, True), ("eng", None, False))) == ()


def test_keeps_all_when_default_not_english():
    assert audio_to_remove(audio(("eng", None, False), ("fre", None, True))) == ()


@pytest.mark.parametrize("title", ["French Dub", "DUBBED", "dub"])
def test_keeps_all_when_title_says_dub(title):
    assert audio_to_remove(audio(("eng", None, True), ("fre", title, False))) == ()


def test_title_containing_dub_inside_a_word_does_not_count():
    assert audio_to_remove(audio(("eng", None, True), ("spa", "Dublin", False))) == (2,)


def test_keeps_all_when_no_english():
    assert audio_to_remove(audio(("spa", None, True), ("fre", None, False))) == ()


@pytest.mark.parametrize("lang", [None, "", "   ", "und", "UND", "und-US", "und_US"])
def test_keeps_all_when_any_track_unlabeled(lang):
    assert audio_to_remove(audio(("eng", None, True), (lang, None, False))) == ()


def test_english_commentary_kept():
    assert audio_to_remove(audio(("eng", None, True), ("eng", "Commentary", False), ("spa", None, False))) == (3,)


@pytest.mark.parametrize("lang", ["en", "English", "ENG"])
def test_english_spellings(lang):
    assert audio_to_remove(audio((lang, None, True), ("spa", None, False))) == (2,)


def test_x265_with_extra_audio_is_strip_only():
    d = decide(info(video_codec="hevc", streams=eng_spa()), "medium", True)
    assert d.action == "strip_only" and d.remove_audio


def test_small_file_with_extra_audio_is_strip_only():
    d = decide(info(video_bitrate=1_000_000, streams=eng_spa()), "medium", True)
    assert d.action == "strip_only" and d.reason == "Already small" and d.remove_audio == (2,)


def test_efficient_without_extra_audio_stays_skip():
    d = decide(info(video_codec="hevc", streams=audio(("eng", None, True))), "medium", True)
    assert d.action == "skip" and d.remove_audio == ()


def test_convert_carries_audio_removal_when_option_on():
    d = decide(info(streams=eng_spa()), "medium", True)
    assert d.action == "convert" and d.remove_audio == (2,)


def test_option_off_never_removes():
    assert decide(info(streams=eng_spa(), video_bitrate=30_000_000), "medium", False).remove_audio == ()


@pytest.mark.parametrize("lang", ["en-US", "en-GB", "eng-US", "EN_us", " en-au "])
def test_english_with_a_region_counts_as_english(lang):
    tracks = audio(("eng", None, True), (lang, "Commentary", False), ("spa", None, False))
    assert audio_to_remove(tracks) == (3,)


@pytest.mark.parametrize("lang", ["end", "enm", "ena-US"])
def test_tags_that_only_start_like_english_are_not_english(lang):
    assert audio_to_remove(audio(("eng", None, True), (lang, None, False))) == (2,)


def test_removes_everything_but_the_chosen_language():
    tracks = audio(("spa", None, True), ("eng", None, False), ("jpn", None, False))
    assert audio_to_remove(tracks, "spa") == (2, 3)


def test_keeps_all_when_first_track_is_not_the_chosen_language():
    assert audio_to_remove(audio(("eng", None, True), ("spa", None, False)), "spa") == ()


def test_keeps_all_when_default_track_is_not_the_chosen_language():
    assert audio_to_remove(audio(("spa", None, False), ("eng", None, True)), "spa") == ()


def test_keeps_all_when_the_chosen_language_is_missing():
    assert audio_to_remove(audio(("eng", None, True), ("fre", None, False)), "jpn") == ()


def test_a_dubbed_movie_keeps_all_whichever_language_is_chosen():
    assert audio_to_remove(audio(("spa", None, True), ("eng", "Dub", False)), "spa") == ()


def test_the_chosen_language_counts_under_its_other_tags():
    assert audio_to_remove(audio(("fre", None, True), ("fra", None, False), ("eng", None, False)),
                           "fra") == (3,)


def test_decide_uses_the_chosen_language():
    tracks = audio(("spa", None, True), ("eng", None, False))
    d = decide(info(video_codec="hevc", streams=tracks), "medium", True, language="spa")
    assert d.action == "strip_only" and d.remove_audio == (2,)
    assert decide(info(video_codec="hevc", streams=tracks), "medium", True).action == "skip"


# ---- Shrinking oversized H.265 files ----

def _h265(**overrides):
    fields = dict(video_codec="hevc", width=1920, video_bitrate=10_000_000)
    fields.update(overrides)
    return info(**fields)


def test_oversized_h265_is_sampled_first_when_the_library_allows_it():
    d = decide(_h265(), "medium", False, shrink_h265=True)
    assert (d.action, d.reason, d.sample_first) == ("convert", "Worth converting", True)
    assert d.why == "Already H.265, but uses far more data than it needs at this resolution"


def test_oversized_h265_is_left_alone_when_the_library_does_not_allow_it():
    d = decide(_h265(), "medium", False)
    assert (d.action, d.reason, d.sample_first) == ("skip", "Already H.265", False)


@pytest.mark.parametrize("overrides", [
    dict(video_bitrate=5_000_000),            # under the medium 1080p limit
    dict(video_bitrate=None),                 # unknown is never treated as oversized
    dict(color_transfer="smpte2084"),         # HDR is left for now
    dict(color_transfer="arib-std-b67"),
])
def test_h265_that_is_not_clearly_oversized_still_skips(overrides):
    assert decide(_h265(**overrides), "medium", False, shrink_h265=True).reason == "Already H.265"


def test_h265_that_already_showed_too_little_saving_is_not_sampled_again():
    d = decide(_h265(), "medium", False, shrink_h265=True, tried="Sample showed too little saving")
    assert (d.action, d.reason) == ("skip", "Sample showed too little saving")
    d = decide(_h265(), "medium", False, shrink_h265=False, tried="Sample showed too little saving")
    assert (d.action, d.reason) == ("skip", "Already H.265")


def test_h265_that_showed_too_little_saving_still_loses_extra_audio():
    d = decide(_h265(streams=eng_spa()), "medium", True, shrink_h265=True,
               tried="Would not save enough")
    assert (d.action, d.remove_audio, d.sample_first) == ("strip_only", (2,), False)


def test_oversized_h265_keeps_its_extra_audio_removal():
    d = decide(_h265(streams=eng_spa()), "medium", True, shrink_h265=True)
    assert (d.action, d.remove_audio, d.sample_first) == ("convert", (2,), True)


def test_dolby_vision_h265_is_never_sampled():
    d = decide(_h265(dolby_vision=True), "medium", False, shrink_h265=True)
    assert d.reason == "Dolby Vision, left as is"


def test_h264_is_never_sampled_first():
    assert decide(info(), "medium", False, shrink_h265=True).sample_first is False


# ---- commentary soundtracks

def commentary_streams(*tracks) -> tuple:
    """Video, then audio tracks of (language, is_default, is_commentary)."""
    streams = [Stream(0, "video", "h264", None, None, True, False)]
    for i, (lang, default, commentary) in enumerate(tracks, start=1):
        streams.append(Stream(i, "audio", "aac", lang, None, default, False, commentary))
    return tuple(streams)


def test_commentary_track_is_removed_when_a_main_track_stays():
    streams = commentary_streams(("eng", True, False), ("eng", False, True))
    assert commentary_to_remove(streams) == (2,)


def test_every_commentary_track_is_removed():
    streams = commentary_streams(("eng", True, False), ("eng", False, True), ("eng", False, True))
    assert commentary_to_remove(streams) == (2, 3)


def test_a_file_without_commentary_loses_nothing():
    assert commentary_to_remove(commentary_streams(("eng", True, False), ("spa", False, False))) == ()


def test_a_single_audio_track_is_never_removed_even_if_marked_commentary():
    assert commentary_to_remove(commentary_streams(("eng", True, True))) == ()


def test_a_file_whose_audio_is_all_commentary_is_left_alone():
    streams = commentary_streams(("eng", True, True), ("eng", False, True))
    assert commentary_to_remove(streams) == ()


def test_a_commentary_default_track_means_unsure_so_nothing_is_removed():
    streams = commentary_streams(("eng", True, True), ("eng", False, False))
    assert commentary_to_remove(streams) == ()


def test_commentary_is_not_removed_when_the_language_rule_already_took_the_main_track():
    # The language rule would drop the Spanish main track, leaving only the commentary: keep it.
    streams = commentary_streams(("eng", True, False), ("eng", False, True), ("spa", False, False))
    assert commentary_to_remove(streams, already_removed=(1, 3)) == ()


def test_commentary_removal_is_off_unless_asked_for():
    streams = commentary_streams(("eng", True, False), ("eng", False, True))
    d = decide(info(width=1920, video_bitrate=30_000_000, streams=streams), "medium", False)
    assert d.action == "convert" and d.remove_audio == ()


def test_commentary_is_removed_from_a_file_that_is_converted():
    streams = commentary_streams(("eng", True, False), ("eng", False, True))
    d = decide(info(width=1920, video_bitrate=30_000_000, streams=streams), "medium", False,
               remove_commentary=True)
    assert d.action == "convert" and d.remove_audio == (2,)


def test_commentary_alone_makes_an_h265_file_worth_rewriting():
    streams = commentary_streams(("eng", True, False), ("eng", False, True))
    d = decide(info(video_codec="hevc", streams=streams), "medium", False, remove_commentary=True)
    assert d.action == "strip_only" and d.remove_audio == (2,)


def test_commentary_removal_adds_to_the_language_removal():
    streams = commentary_streams(("eng", True, False), ("eng", False, True), ("spa", False, False))
    d = decide(info(video_codec="hevc", streams=streams), "medium", True, remove_commentary=True)
    assert d.action == "strip_only" and d.remove_audio == (2, 3)


def test_both_rules_never_remove_every_audio_track():
    streams = commentary_streams(("eng", True, False), ("eng", False, True), ("spa", False, False))
    for first in (True, False):
        d = decide(info(video_codec="hevc", streams=streams), "medium", first, remove_commentary=True)
        kept = {s.index for s in streams if s.kind == "audio"} - set(d.remove_audio)
        assert kept, d


def test_commentary_removal_does_not_override_a_dolby_vision_skip():
    streams = commentary_streams(("eng", True, False), ("eng", False, True))
    d = decide(info(dolby_vision=True, streams=streams), "high", False, remove_commentary=True)
    assert d.action == "skip" and d.reason == "Dolby Vision, left as is"


def test_the_reason_for_removing_only_tracks_does_not_claim_they_are_other_languages():
    streams = commentary_streams(("eng", True, False), ("eng", False, True))
    d = decide(info(video_codec="hevc", streams=streams), "medium", False, remove_commentary=True)
    assert d.why == "Only the extra audio tracks can be removed"
