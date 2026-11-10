"""The hardware H.265 encoders ShelfSaver can drive, and how ffmpeg is told to use each.

Why this module exists: the ffmpeg command used to hard-code NVIDIA's NVENC (encoder name,
rate-control flags, pixel format, the `nvidia-smi` GPU tile, the self-test). Intel Quick Sync
(QSV) and others need a different command, and need it *per operating system* (NVENC is the same
on Windows and Linux; QSV starts its device differently on each). Putting everything an encoder
decides behind one small interface means `encode.build_command` stays a single readable function
and adding an encoder touches this file, not the whole app.

What an encoder decides, and what it does not:
- It decides: how to start its hardware (`input_args`), the encoder options (`codec_args`),
  any video filters it needs (`filters`), how to test itself (`self_test_args`), and how to read
  its load and name for the status pages (`usage_percent`, `device_info`).
- It does not decide: stream mapping, containers, subtitles, audio, colour tags, or the
  quality *level* names. Those are the same for every encoder, so they stay in `encode.py`.

The NVENC command must stay byte-for-byte what it was before this module existed.
`tests/test_encoders.py` checks that against a recorded snapshot of the original commands.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from . import hostos
from .quality import LEVELS

# Runs a small diagnostic program and returns its text output, or None if it could not run.
# (mcp_server._run_tool). Passed in so tests can replace it and nothing here spawns a process.
RunTool = Callable[[list], Optional[str]]


class Encoder:
    """One hardware H.265 encoder. Subclasses fill in the details."""

    name = ""    # short id used in settings and logs, e.g. "nvenc"
    label = ""   # what people see, e.g. "NVIDIA NVENC"
    codec = ""   # the ffmpeg encoder name, e.g. "hevc_nvenc"

    @classmethod
    def from_config(cls, config) -> "Encoder":
        """Build the encoder from the app settings. Most need none; QSV reads its device."""
        return cls()

    def input_args(self) -> list:
        """Arguments that must come before the first `-i` (hardware device setup)."""
        return []

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        """`-c:v:<stream> <codec>` plus its rate control, pixel format and profile.

        `level` is "low"/"medium"/"high"; `deep` is true for 10-bit sources (HDR and similar),
        which must be kept at 10 bits or banding appears in dark gradients.
        """
        raise NotImplementedError

    def filters(self, deinterlace: bool) -> list:
        """Video filters, in order, to run before encoding (joined with commas by the caller)."""
        raise NotImplementedError

    def self_test_args(self) -> list:
        """Arguments that encode one blank frame, placed after a `-f lavfi -i color=...` input."""
        raise NotImplementedError

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        """How busy the hardware is, 0-100, or None when it cannot be read."""
        return None

    def device_info(self, run_tool: RunTool) -> dict:
        """{"available": bool, "name": str|None, "driver": str|None} for the health page."""
        return {"available": False, "name": None, "driver": None}


class Nvenc(Encoder):
    """NVIDIA NVENC. Identical on Windows and Linux: the driver exposes it with no device setup."""

    name = "nvenc"
    label = "NVIDIA NVENC"
    codec = "hevc_nvenc"

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        args = [
            f"-c:v:{stream}", self.codec, "-preset", "p7", "-tune", "hq",
            "-rc", "vbr", "-cq", str(LEVELS[level].cq), "-b:v", "0",
            "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
        ]
        if deep:
            args += [f"-pix_fmt:v:{stream}", "p010le", f"-profile:v:{stream}", "main10"]
        else:
            args += [f"-pix_fmt:v:{stream}", "yuv420p", f"-profile:v:{stream}", "main"]
        return args

    def filters(self, deinterlace: bool) -> list:
        # NVENC takes ordinary system-memory frames, so the plain software deinterlacer works.
        return ["bwdif"] if deinterlace else []

    def self_test_args(self) -> list:
        return ["-frames:v", "1", "-c:v", self.codec]

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        """The busier of the card and its video encoder: encoding barely shows as general use."""
        out = run_tool(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder",
                        "--format=csv,noheader,nounits"])
        lines = (out or "").strip().splitlines()
        if not lines:
            return None
        numbers = [int(part) for part in lines[0].split(",") if part.strip().isdigit()]
        return max(numbers) if numbers else None

    def device_info(self, run_tool: RunTool) -> dict:
        out = run_tool(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
        line = (out or "").strip().splitlines()
        if not line:
            return {"available": False, "name": None, "driver": None}
        name, _, driver = line[0].partition(",")
        return {"available": True, "name": name.strip(), "driver": driver.strip()}


class Qsv(Encoder):
    """Intel Quick Sync Video (iGPUs and Arc cards), on Windows and on Linux.

    Checked two ways: against FFmpeg's own documentation (the "QSV Encoders" section and the
    `-init_hw_device` section of ffmpeg.texi) and by encoding on real hardware (an Intel UHD 770
    under Linux, FFmpeg 7.1 with Intel's iHD driver). The Windows device option is the documented
    one, but was NOT run: no Intel GPU was available there. The PR description lists exactly what
    was and was not tested.

    How it differs from NVENC:
    - Device: NVENC needs none. QSV is started on a named device, which is operating-system
      specific. Linux: a DRM render node such as /dev/dri/renderD128. Windows: a DirectX adapter,
      through D3D11 (`child_device_type=d3d11va`).
    - Frames: hevc_qsv accepts ordinary system-memory frames in nv12 or p010le (it does *not* list
      yuv420p, which NVENC uses). FFmpeg converts on the way in, so the CPU deinterlacer used for
      NVENC works unchanged and no explicit hardware upload step is needed.
    - Quality: the constant-quality mode is `-global_quality` (1 to 51, 1 is best), not `-cq`.
    """

    name = "qsv"
    label = "Intel Quick Sync (QSV)"
    codec = "hevc_qsv"

    # Quality number for each level (see quality.LEVELS). PROVISIONAL. 21 is the value the
    # owner's Tdarr already runs on the same iGPU for H.265; the other two keep the same spacing
    # as NVENC's 22/26/30. The scale is not the same as NVENC's, so these want tuning with
    # scripts/make_compare_clips.py on real files before anyone relies on the size savings.
    QUALITY = {"low": 21, "medium": 25, "high": 29}
    DEFAULT_LINUX_DEVICE = "/dev/dri/renderD128"  # the first (usually only) render node

    def __init__(self, device: str = "") -> None:
        # Linux: path of a render node. Windows: DirectX adapter number, or "" for the default.
        self.device = (device or "").strip()

    @classmethod
    def from_config(cls, config) -> "Qsv":
        return cls(getattr(config, "qsv_device", ""))

    def _linux_device(self) -> str:
        return self.device or self.DEFAULT_LINUX_DEVICE

    def input_args(self) -> list:
        # `qsv:hw,...` is the documented form: MFX_IMPL_HARDWARE on the given subdevice.
        if hostos.IS_WINDOWS:
            spec = "qsv:hw,child_device_type=d3d11va"
            if self.device.isdigit():  # more than one GPU: pick the adapter by number
                spec += f",child_device={self.device}"
            return ["-init_hw_device", spec]
        return ["-init_hw_device", f"qsv:hw,child_device={self._linux_device()}"]

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        # nv12 / p010le are what hevc_qsv takes; profile main10 keeps 10-bit sources at 10 bits.
        pix, profile = ("p010le", "main10") if deep else ("nv12", "main")
        return [f"-c:v:{stream}", self.codec, "-preset", "slow",
                "-global_quality", str(self.QUALITY[level]),
                f"-pix_fmt:v:{stream}", pix, f"-profile:v:{stream}", profile]

    def filters(self, deinterlace: bool) -> list:
        # Same CPU deinterlacer as NVENC. (vpp_qsv could do this on the GPU, but it needs an
        # upload step and gains little for an unattended batch tool; kept simple on purpose.)
        return ["bwdif"] if deinterlace else []

    def self_test_args(self) -> list:
        # The blank test frame is RGB, so name the pixel format QSV takes.
        return ["-frames:v", "1", "-c:v", self.codec, "-pix_fmt", "nv12"]

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        # Intel exposes no cheap, unprivileged "encoder busy" figure (intel_gpu_top needs extra
        # permissions). The tile shows a dash instead of a number that would mislead.
        return None

    def device_info(self, run_tool: RunTool) -> dict:
        if hostos.IS_WINDOWS:
            return {"available": None, "name": None, "driver": None}  # not probed; see class note
        device = self._linux_device()
        if not os.access(device, os.R_OK | os.W_OK):
            return {"available": False, "name": None, "driver": None}
        # vainfo, if installed, names the driver ("Intel iHD driver ... - 25.2.3").
        out = run_tool(["vainfo", "--display", "drm", "--device", device]) or ""
        match = re.search(r"Driver version:\s*(.+)", out)
        return {"available": True, "name": "Intel Quick Sync (render node)",
                "driver": match.group(1).strip() if match else None}


# Every encoder ShelfSaver knows, by `name`. The first is the default and what runs today.
ENCODERS = {"nvenc": Nvenc, "qsv": Qsv}
DEFAULT_NAME = "nvenc"


def encoder_for(config) -> Encoder:
    """The encoder the settings ask for. An unknown name falls back to the default rather than
    stopping the app: config.load_config already drops names it does not recognise."""
    cls = ENCODERS.get(getattr(config, "encoder", DEFAULT_NAME), ENCODERS[DEFAULT_NAME])
    return cls.from_config(config)


DEFAULT_ENCODER = ENCODERS[DEFAULT_NAME]()
