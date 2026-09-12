# MEDIA runtime image (ADR-0030). Shared by every component that declares `runtime.profile: media` —
# the component code is MOUNTED read-only at /drivers by the launcher; this image only carries the
# common media toolchain.
#
# It exists because ffmpeg belongs HERE and nowhere else. The alternative — adding ffmpeg to the shared
# reference image — would put a 60MB media toolchain into the image ~9 platform services run from, to
# benefit one driver. A profile keeps the blast radius to components that ask for it.
#
# Deliberately stdlib-only on the Python side: rtsp-ingest speaks to the broker over plain HTTP and
# shells out to ffmpeg. Nothing here should grow a client library for a camera vendor — vendor quirks
# belong in a component, not in the runtime every media component inherits.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version | head -1

# Driver code is mounted read-only at /drivers; its ONLY registry/file I/O is the broker (AIRAWAT_API).
# The RTSP socket is the one exception and it is NOT brokered — see ADR-0077 and the note in
# drivers/rtsp-ingest/meta.yaml. The launcher runs:
#   docker run --rm -v <driver-dir>:/drivers:ro -e RUN_TOKEN=… -e AIRAWAT_API=… \
#     manishsv/airawatos-runtime-media:0.1.0  python /drivers/run.py
WORKDIR /drivers
