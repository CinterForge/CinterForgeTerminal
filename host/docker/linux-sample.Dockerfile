# CINTERFORGE SOURCE HEADER — host/docker/linux-sample.Dockerfile
# PURPOSE: Maintains linux sample within CinterForgeTerminal without duplicating external ownership.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# INTERFACE: This file declares presentation/build rules rather than callable methods.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
# The Linux counterpart to the exact same authored WebView C++ example.
# Built and exported once by Mac Setup; subsequent builds reuse the image.
FROM --platform=linux/amd64 debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates bash g++ cmake ninja-build pkg-config python3 nodejs \
       libgtk-3-dev libwebkit2gtk-4.1-dev xvfb xauth openbox x11vnc websockify novnc feh x11-utils dbus-x11 procps \
    && rm -rf /var/lib/apt/lists/*
COPY gui_preflight.py /usr/local/lib/cinterforge/gui_preflight.py
ENV WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
ENV LIBGL_ALWAYS_SOFTWARE=1

# Reject a partially built GUI image during Setup rather than surfacing an
# ambiguous "missing packages" message at native Launcher runtime.
RUN set -eu; \
    for tool in Xvfb xdpyinfo openbox x11vnc websockify feh pgrep; do \
        command -v "$tool" >/dev/null; \
    done; \
    test -f /usr/share/novnc/vnc.html
