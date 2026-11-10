# ShelfSaver

ShelfSaver watches the video folders you choose and shrinks the files that are worth
shrinking by converting them to H.265 on your NVIDIA graphics card. It checks every new file
(length, tracks, plays from start to finish, meaningfully smaller) before it replaces the
original, and it never removes anything you did not agree to.

It runs on one Windows 11 PC with an NVIDIA graphics card, and you use it from a web page.

**This app is dumb on purpose.** There are no advanced settings and no secret menu of encoder
knobs. I just wanted to free up some disk space without earning a degree in video compression.
If you came for the fancy version, this isn't it, and I'm thrilled about that.

## Screenshots

These show a fresh install with nothing converted yet, and the folder names are made up.

**Home**

![The home page, with space saved, libraries and history](screenshots/home.png)

**Library**

![A library page, with its stats and history](screenshots/library.png)

**Queue**

![The queue page, with nothing waiting](screenshots/queue.png)

**Settings**

![The settings page, with the scratch disk and holding folder](screenshots/settings.png)

## What it does

- Treats each folder you add as a library, and picks up new files as they arrive.
- Decides for each video whether converting it is worthwhile, and skips the rest.
- Has one setting for how hard to compress: Low, Medium or High.
- Shows each library's space saved, plus every file's old and new size and format.
- Builds new files on a scratch disk you choose, and swaps them in once they pass the checks.
- Runs 24/7 unless you add blackout times, when no new files start.
- Can keep replaced originals in a holding folder until you clear them.
- Can remove soundtracks that aren't in your default language (English unless you change it),
  except on dubbed movies. This is off unless you turn it on.
- Can shrink H.265 files that are far bigger than they need to be. It tests a short sample first
  and only converts when the file would come out at least 25% smaller. HDR files are left as
  they are. This is off unless you turn it on for a library.
- Offers logs and status over a read-only MCP connection.

## Install

You need Windows 11, an NVIDIA graphics card and an internet connection.

1. Download or copy this folder to the PC.
2. Open PowerShell as administrator (right-click PowerShell, choose "Run as administrator").
3. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```

The script installs Python and FFmpeg if they are missing, puts the program in
`C:\Program Files\ShelfSaver`, sets it to start with the PC, opens its port on
Domain, Private and Public networks, and prints the web address (port 8642) at the end.

Your settings, history and login live in `C:\ProgramData\ShelfSaver`. Only administrators
can open that folder.

To update, get the newer copy of this folder and run the same command again. The new version
is built first, then swapped in; if anything goes wrong the old version is put back. Your data
is left alone. Run setup from a folder outside `C:\Program Files\ShelfSaver`.

## Uninstall

In PowerShell as administrator:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Uninstall
```

This stops ShelfSaver and removes the startup task, the firewall rule and the program folder.
Your settings and history are kept. To delete those too, add `-RemoveData`. Your video
libraries, holding folders and scratch disks are never touched.

## Running on Linux (preview)

ShelfSaver now starts and runs on Linux. This is a first step: there is no installer, service
file or container image yet. Converting needs either an NVIDIA card (NVENC, the default) or an
Intel GPU (Quick Sync, see below) with a matching FFmpeg. A Docker image is planned next.
Everything below has been run on Debian with the test suite and an end-to-end check of the real
app. NVENC on Linux has not been tried; Quick Sync on Linux has (Intel UHD 770).

Install FFmpeg and Python 3.10 or newer, then from this folder:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
SHELFSAVER_DATA=/var/lib/shelfsaver SHELFSAVER_MEDIA_ROOTS=/media SHELFSAVER_SCRATCH_ROOTS=/scratch .venv/bin/python -m shelfsaver
```

| Setting | What it does |
|---|---|
| `SHELFSAVER_MEDIA_ROOTS` | Folders libraries may be in (separate several with `:`). **Required on Linux.** |
| `SHELFSAVER_SCRATCH_ROOTS` | Folders the scratch disk may be in. Required before a scratch disk can be chosen. |
| `SHELFSAVER_DATA` | Where settings, history and the login are kept. Default `~/.local/share/ShelfSaver`. |
| `SHELFSAVER_PORT` | Web page port. Default 8642. |
| `SHELFSAVER_ENCODER` | `nvenc` (default) or `qsv`. Works on Windows and Linux. |
| `SHELFSAVER_QSV_DEVICE` | Quick Sync only. Linux: the render node, default `/dev/dri/renderD128`. Windows: the DirectX adapter number, only needed if the PC has more than one GPU. |

The same names can go in `config.json` in the data folder as `media_roots`, `scratch_roots`,
`port`, `encoder` and `qsv_device`; the environment wins if both are set.

### Intel Quick Sync (QSV)

Set `SHELFSAVER_ENCODER=qsv`. Needs an Intel GPU and an FFmpeg built with `hevc_qsv`.

- **Linux:** FFmpeg needs Intel's media driver and the oneVPL runtime (on Debian:
  `intel-media-va-driver-non-free libvpl2 libmfx-gen1.2`; the driver is in the non-free
  component). ShelfSaver must be able to open the render node, so in Docker pass the device in
  (`--device /dev/dri/renderD128`).
- **Windows:** uses D3D11, selected automatically. **This path has not been run on an Intel GPU**;
  the option used is the one FFmpeg documents.
- **Checked on Linux (Intel UHD 770):** ShelfSaver itself scanned, converted, verified and replaced
  an 8-bit MKV. The 10-bit, MP4 (`hvc1` tag, HDR colour tags) and deinterlace cases were run as the
  same ffmpeg commands directly, not yet through ShelfSaver's own conversion.
- **Quality numbers are provisional.** Low/Medium/High map to Quick Sync quality 21/25/29 (21 is what
  one of our own Tdarr setups already uses). Quick Sync's scale is not NVENC's, so the amount saved
  at each level may differ; use `scripts/make_compare_clips.py --encoder qsv` on your own files
  before relying on it.
- **The GPU tile on the home page shows a dash** for Quick Sync: Intel offers no simple reading that
  doesn't need extra permissions, and a made-up number would mislead.

Things that differ from Windows, and why:

- **Folders must be named up front.** On Windows you pick a drive. A Linux machine has one big
  folder tree, and the folder picker would otherwise let anyone who signs in browse all of it.
  Libraries and the picker are limited to the media roots (symlinks are followed, so a link
  inside a root cannot lead outside it).
- **A library can't be the top of a mounted volume.** ShelfSaver keeps its `incoming` and
  `holding` folders at the top of the volume a library is on, so replacing a file is a single
  rename on one filesystem and can never be cut off halfway. In Docker, map a parent folder
  (such as your whole media share to `/media`) and add libraries inside it.
- **Nothing is ever overwritten.** Linux's rename silently replaces an existing file, unlike
  Windows. ShelfSaver asks the kernel to refuse instead (`renameat2` with `RENAME_NOREPLACE`)
  and falls back to checking first on filesystems that can't.
- **Files in use.** Linux lets a file be replaced while another program has it open, so a
  replaced file is never "waiting, in use" the way it can be on Windows.
- **Stopping.** `SIGTERM` (what `docker stop` and systemd send) stops the worker in order.
  If ShelfSaver is killed outright (`kill -9`), ffmpeg is not tied to it as it is on Windows, and
  may finish on its own into the scratch folder. It never writes to a library.
- **No HTTPS yet.** The web page is plain HTTP on every network interface, as on Windows, so the
  password and session cookie cross your network unencrypted. Keep it on a trusted network or put
  it behind a reverse proxy that adds HTTPS. Binding to one address is planned.
- **Logs.** Logs go to `logs/shelfsaver.log` in the data folder, not to the terminal.
- **Unraid.** Tested on Unraid's share layer: file identity survives renames and the no-replace
  rename works, which the replace step depends on.

## Stopping ShelfSaver

ShelfSaver restarts itself if it is stopped (and checks every few minutes), so ending the task in
Task Manager will not keep it stopped. To stop it until you want it back, in PowerShell as
administrator:

```powershell
Disable-ScheduledTask -TaskName ShelfSaver
Stop-ScheduledTask -TaskName ShelfSaver
```

To turn it back on: `Enable-ScheduledTask -TaskName ShelfSaver` then `Start-ScheduledTask -TaskName ShelfSaver`.

## Compare Low, Medium and High

This tool makes short clips from one of your files at each setting. The installer puts it at
`C:\Program Files\ShelfSaver\scripts\make_compare_clips.py`. Run it from PowerShell as administrator:

```powershell
& "C:\Program Files\ShelfSaver\venv\Scripts\python.exe" "C:\Program Files\ShelfSaver\scripts\make_compare_clips.py" --source "D:\Media\Movies\Example.mkv" --start 600 --seconds 30 --out "D:\Clips"
```

(From a development copy of this folder, `scripts\make_compare_clips.py` works the same way.)

This writes `Example Original.mkv`, `Example Low.mkv`, `Example Medium.mkv` and
`Example High.mkv` into the output folder and prints how big each one is. The output folder
must not be inside the folder of the source video. The source is never changed.

## Development

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Tests marked `gpu` need an NVIDIA graphics card. The tests also run on Linux
(`python -m pytest -m "not gpu"`, with `ffmpeg` and `git` installed); the few that check
Windows-only behaviour, such as exclusive file locks, are skipped there and marked in the code.

## Licence

Apache License 2.0. See `LICENSE`.
