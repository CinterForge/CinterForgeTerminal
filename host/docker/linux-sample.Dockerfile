# The Linux counterpart to the exact same authored WebView C++ example.
# Built and exported once by Mac Setup; subsequent builds reuse the image.
FROM --platform=linux/amd64 debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates bash g++ cmake ninja-build pkg-config python3 nodejs \
       libgtk-3-dev libwebkit2gtk-4.1-dev xvfb xauth dbus-x11 \
    && rm -rf /var/lib/apt/lists/*
ENV WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
ENV LIBGL_ALWAYS_SOFTWARE=1
