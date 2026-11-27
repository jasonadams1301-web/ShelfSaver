# ShelfSaver container image (Linux: NVIDIA NVENC, Intel Quick Sync, or AMD through VAAPI).
#
# Base: Debian 13 ("trixie"). Chosen for its FFmpeg: 7.1 or newer is needed because FFmpeg 6.1
# (Ubuntu 24.04's) drops HDR10 metadata when encoding with hevc_nvenc; 8.x keeps it.
# Debian's FFmpeg is built with hevc_nvenc, hevc_qsv and hevc_vaapi, so one image serves all three.
FROM debian:trixie-slim

# Standard labels. "source" is what GitHub's container registry reads to connect the package to
# this repository's page. They are plain metadata: nothing is sent anywhere.
LABEL org.opencontainers.image.source="https://github.com/jasonadams1301-web/ShelfSaver" \
      org.opencontainers.image.licenses="Apache-2.0"

# `apt-get upgrade` pulls in any security fix published since the base image was built (a stale
# base is the usual way an image ends up with known vulnerabilities). Rebuild regularly.
# non-free is needed only for intel-media-va-driver-non-free (Intel's complete media driver).
# --no-install-recommends keeps the image small; each package below is there for a reason:
#   ffmpeg                   the encoder itself (+ ffprobe)
#   python3 / python3-venv   ShelfSaver is Python; the venv keeps its libraries off the system's
#   libvpl2, libmfx-gen1.2   oneVPL runtime FFmpeg's Quick Sync support loads at run time
#   intel-media-va-driver-non-free  Intel's driver for the above (Quick Sync only)
#   mesa-va-drivers          Mesa's VAAPI driver for AMD Radeon cards (AMD only). Trixie's is
#                            Mesa 25.0; RX 9000 (RDNA 4) support is checked on real hardware.
#   vainfo                   lets ShelfSaver name the Intel or AMD GPU in its status (encoders.py)
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-venv libvpl2 libmfx-gen1.2 intel-media-va-driver-non-free mesa-va-drivers vainfo \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY shelfsaver ./shelfsaver
# pip is upgraded first: the copy bundled with Debian's Python has published security advisories.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir . \
 && rm -rf /app/build

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

# Settings for a container. All can be overridden at `docker run` / in the Unraid template.
ENV PATH="/opt/venv/bin:$PATH" \
    SHELFSAVER_DATA=/config \
    SHELFSAVER_MEDIA_ROOTS=/media \
    SHELFSAVER_SCRATCH_ROOTS=/scratch \
    SHELFSAVER_LOG_STDOUT=1 \
    SHELFSAVER_DOCKER=1 \
    PUID=99 PGID=100 UMASK=022 \
    NVIDIA_DRIVER_CAPABILITIES=video,utility \
    NVIDIA_VISIBLE_DEVICES=all

EXPOSE 8642
VOLUME /config

# "Is the app answering?" Asks /healthz, which needs no login and returns only {"ok": true}. It
# fails (HTTP 503) if the database cannot be read, so a hung or broken app is noticed, not only
# a closed port. urllib raises on any non-200 answer, which is what makes the check fail. Reads
# the port the same way the app does.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (os.environ.get('SHELFSAVER_PORT') or 8642), timeout=3)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "shelfsaver"]
