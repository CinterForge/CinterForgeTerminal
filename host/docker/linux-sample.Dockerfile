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
