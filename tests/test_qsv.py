"""Intel Quick Sync (QSV): the command it builds, per operating system, and how it is chosen.

These run on every OS by faking which one the encoder believes it is on (`hostos.IS_WINDOWS`), so
both device setups are checked everywhere. What this file cannot show is that the Windows option
works on a real Intel GPU: no Intel GPU was available there. The Linux commands below were also run
for real on an Intel UHD 770 (see the PR description); only the text of the command is checked here.
"""
from __future__ import annotations

import json

import pytest

from shelfsaver import hostos
from shelfsaver.config import Config, load_config
from shelfsaver.encode import build_command, encoder_works
from shelfsaver.encoders import ENCODERS, Nvenc, Qsv, encoder_for
from shelfsaver.quality import LEVELS
from tests.helpers import convert, info


def on(monkeypatch, windows: bool):
    monkeypatch.setattr(hostos, "IS_WINDOWS", windows)


# ---- device setup, per operating system

def test_linux_uses_the_default_render_node(monkeypatch):
    on(monkeypatch, False)
    assert Qsv().input_args() == ["-init_hw_device", "qsv:hw,child_device=/dev/dri/renderD128"]


def test_linux_can_be_pointed_at_another_render_node(monkeypatch):
    on(monkeypatch, False)
    assert Qsv("/dev/dri/renderD129").input_args()[1] == "qsv:hw,child_device=/dev/dri/renderD129"


def test_windows_uses_d3d11_and_lets_windows_pick_the_gpu(monkeypatch):
    on(monkeypatch, True)
    assert Qsv().input_args() == ["-init_hw_device", "qsv:hw,child_device_type=d3d11va"]


def test_windows_can_name_the_adapter_by_number(monkeypatch):
    on(monkeypatch, True)
    assert Qsv("1").input_args()[1] == "qsv:hw,child_device_type=d3d11va,child_device=1"


def test_windows_ignores_a_device_that_is_not_an_adapter_number(monkeypatch):
    # A Linux-style path in a shared config.json must not be pasted into a Windows command.
    on(monkeypatch, True)
    assert Qsv("/dev/dri/renderD129").input_args()[1] == "qsv:hw,child_device_type=d3d11va"


# ---- the encoder options

def test_quality_levels_are_ordered_and_inside_the_documented_range():
    values = [Qsv.QUALITY[name] for name in ("low", "medium", "high")]
    assert values == sorted(values) and len(set(values)) == 3  # low saves least, high saves most
    assert all(1 <= v <= 51 for v in values)                   # -global_quality is 1 to 51
    assert set(Qsv.QUALITY) == set(LEVELS)                      # every level has a number


def test_eight_bit_uses_nv12_and_main():
    args = Qsv().codec_args(0, "medium", deep=False)
    assert args == ["-c:v:0", "hevc_qsv", "-preset", "slow", "-global_quality", "25",
                    "-pix_fmt:v:0", "nv12", "-profile:v:0", "main"]


def test_ten_bit_is_kept_at_ten_bits():
    args = Qsv().codec_args(1, "low", deep=True)
    assert args[:2] == ["-c:v:1", "hevc_qsv"] and "-global_quality" in args
    assert args[args.index("-pix_fmt:v:1") + 1] == "p010le"
    assert args[args.index("-profile:v:1") + 1] == "main10"


def test_it_never_asks_for_yuv420p_which_hevc_qsv_does_not_list():
    for deep in (False, True):
        assert "yuv420p" not in Qsv().codec_args(0, "high", deep)


def test_deinterlacing_is_the_same_cpu_filter_as_nvenc():
    assert Qsv().filters(True) == Nvenc().filters(True) == ["bwdif"]
    assert Qsv().filters(False) == []


def test_the_self_test_names_a_pixel_format_qsv_takes():
    args = Qsv().self_test_args()
    assert args == ["-frames:v", "1", "-c:v", "hevc_qsv", "-pix_fmt", "nv12"]


# ---- the whole command

def _cmd(windows, monkeypatch, **media):
    on(monkeypatch, windows)
    return build_command(info(**media), convert(), "medium", "in.mkv", "out.mkv", "ffmpeg", Qsv())


@pytest.mark.parametrize("windows", [False, True])
def test_the_device_is_set_up_before_the_input(monkeypatch, windows):
    cmd = _cmd(windows, monkeypatch)
    assert cmd.index("-init_hw_device") < cmd.index("-i")


@pytest.mark.parametrize("windows", [False, True])
def test_nothing_nvidia_leaks_into_a_qsv_command(monkeypatch, windows):
    cmd = _cmd(windows, monkeypatch, bit_depth=10, field_order="tt")
    assert not any("nvenc" in str(a) or a in ("-cq", "p7", "hq") for a in cmd)
    assert "hevc_qsv" in cmd


def test_a_full_linux_command_reads_as_expected(monkeypatch):
    # The whole command, exactly. This is the one that was run for real on an Intel UHD 770.
    assert _cmd(False, monkeypatch) == [
        "ffmpeg", "-y", "-v", "warning", "-init_hw_device", "qsv:hw,child_device=/dev/dri/renderD128",
        "-i", "in.mkv", "-c:a", "copy", "-c:s", "copy", "-c:t", "copy",
        "-c:v:0", "hevc_qsv", "-preset", "slow", "-global_quality", "25",
        "-pix_fmt:v:0", "nv12", "-profile:v:0", "main",
        "-map_metadata", "0", "-map_chapters", "0", "-max_muxing_queue_size", "4096", "out.mkv",
    ]


def test_a_full_windows_command_reads_as_expected(monkeypatch):
    # Same command with the documented Windows device option. Text only: not run on Intel hardware.
    assert _cmd(True, monkeypatch)[:8] == [
        "ffmpeg", "-y", "-v", "warning", "-init_hw_device", "qsv:hw,child_device_type=d3d11va",
        "-i", "in.mkv"]


def test_the_parts_the_encoder_does_not_own_are_unchanged(monkeypatch):
    # Colour tags, interlace handling and the MP4 tag are shared by every encoder.
    on(monkeypatch, False)
    media = info(bit_depth=10, field_order="tt", color_primaries="bt2020",
                 color_transfer="smpte2084", colorspace="bt2020nc")
    cmd = build_command(media, convert(), "high", "in.mkv", "out.mp4", "ffmpeg", Qsv())
    assert cmd[cmd.index("-color_primaries:v:0") + 1] == "bt2020"
    assert cmd[cmd.index("-filter:v:0") + 1] == "bwdif"
    assert cmd[cmd.index("-tag:v:0") + 1] == "hvc1"


def test_the_same_file_differs_between_nvenc_and_qsv_only_where_the_encoder_decides(monkeypatch):
    on(monkeypatch, False)
    args = dict(info=info(), decision=convert(), level="medium", source="in.mkv",
                output="out.mkv", ffmpeg="ffmpeg")
    nv, qs = build_command(**args), build_command(**args, encoder=Qsv())
    assert nv != qs
    # Everything before the encoder's own options (mapping, copy flags) and after them (metadata,
    # chapters, muxing) is shared, so only the middle may differ.
    assert nv[:4] == qs[:4] == ["ffmpeg", "-y", "-v", "warning"]
    assert nv[nv.index("-c:a"):nv.index("-c:v:0")] == qs[qs.index("-c:a"):qs.index("-c:v:0")]
    assert nv[nv.index("-map_metadata"):] == qs[qs.index("-map_metadata"):]


# ---- self-test and diagnostics

def test_the_self_test_runs_the_device_setup_then_the_frame_test(monkeypatch):
    on(monkeypatch, False)
    seen = []

    class Recorder:
        def __init__(self, cmd, **kwargs):
            seen.append(cmd)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("shelfsaver.encode.subprocess.Popen", Recorder)
    monkeypatch.setattr("shelfsaver.encode.kill_with_app", lambda proc: None)
    assert encoder_works("ffmpeg", Qsv()) is True
    cmd = seen[0]
    assert cmd.index("-init_hw_device") < cmd.index("-i")
    assert cmd[cmd.index("-frames:v"):] == ["-frames:v", "1", "-c:v", "hevc_qsv", "-pix_fmt", "nv12",
                                            "-f", "null", "-"]


def test_load_is_unknown_because_intel_offers_no_cheap_reading():
    assert Qsv().usage_percent(lambda args: "anything") is None


def test_device_info_on_linux_needs_a_usable_render_node(monkeypatch):
    on(monkeypatch, False)
    monkeypatch.setattr("shelfsaver.encoders.os.access", lambda path, mode: False)
    assert Qsv().device_info(lambda args: "") == {"available": False, "name": None, "driver": None}


def test_device_info_on_linux_names_the_driver_when_vainfo_is_there(monkeypatch):
    on(monkeypatch, False)
    monkeypatch.setattr("shelfsaver.encoders.os.access", lambda path, mode: True)
    asked = []

    def run(args):
        asked.append(args)
        return "vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 25.2.3 ()\n"

    got = Qsv("/dev/dri/renderD129").device_info(run)
    assert got["available"] is True and got["driver"].startswith("Intel iHD driver")
    assert asked[0][-1] == "/dev/dri/renderD129"  # asks about the configured device


def test_device_info_still_says_available_without_vainfo(monkeypatch):
    on(monkeypatch, False)
    monkeypatch.setattr("shelfsaver.encoders.os.access", lambda path, mode: True)
    got = Qsv().device_info(lambda args: None)
    assert got["available"] is True and got["driver"] is None


def test_device_info_on_windows_does_not_pretend_to_know(monkeypatch):
    on(monkeypatch, True)
    assert Qsv().device_info(lambda args: "x")["available"] is None


# ---- choosing it

def test_qsv_is_registered_and_picked_by_name():
    assert ENCODERS["qsv"] is Qsv
    assert isinstance(encoder_for(Config(data_dir=".", encoder="qsv")), Qsv)


def test_the_configured_device_reaches_the_encoder():
    assert encoder_for(Config(data_dir=".", encoder="qsv", qsv_device="/dev/dri/renderD129")).device \
        == "/dev/dri/renderD129"


def test_the_encoder_setting_is_cleaned_the_same_in_the_file_and_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    monkeypatch.delenv("SHELFSAVER_ENCODER", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"encoder": "  QSV "}))
    assert load_config().encoder == "qsv"                 # reviewer's point: file matches env
    monkeypatch.setenv("SHELFSAVER_ENCODER", " Nvenc ")
    assert load_config().encoder == "nvenc"               # environment still wins


def test_the_qsv_device_comes_from_the_environment_then_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    monkeypatch.delenv("SHELFSAVER_QSV_DEVICE", raising=False)
    assert load_config().qsv_device == ""
    (tmp_path / "config.json").write_text(json.dumps({"qsv_device": "/dev/dri/renderD129"}))
    assert load_config().qsv_device == "/dev/dri/renderD129"
    (tmp_path / "config.json").write_text(json.dumps({"qsv_device": 1}))  # an adapter number
    assert load_config().qsv_device == "1"
    (tmp_path / "config.json").write_text(json.dumps({"qsv_device": True}))  # not a device
    assert load_config().qsv_device == ""
    monkeypatch.setenv("SHELFSAVER_QSV_DEVICE", " /dev/dri/renderD130 ")
    assert load_config().qsv_device == "/dev/dri/renderD130"
