# CinterForgeTerminal Setup-only Windows cross/guest image (linux/amd64).
# Use MSYS2's patched Wine binary toolchain, NOT the experimental MSYS2 image:
# the latter disables pacman signature checks and is unsuitable for staging.
# Digest verified from ghcr.io/msys2/msys2-docker-build-base on 2026-11-12.
# Upstream builds Wine 11.15 plus MSYS2/Cygwin compatibility patches.
FROM --platform=linux/amd64 ghcr.io/msys2/msys2-docker-build-base@sha256:19c2b7a96a437f71222656462e4fa5014245f2eb2633ac3988dd9cf208f51ed4 AS patched-wine
FROM --platform=linux/amd64 debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive

# Retain Debian's signed APT dependency closure for patched Wine, MinGW, wixl,
# the shared graphical Launcher, and console probes. No package signature bypass.
# Match the distribution in the MSYS2 Wine build stage to avoid glibc ABI skew.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash ca-certificates cmake ninja-build pkg-config python3 \
        gcc-mingw-w64-x86-64-posix g++-mingw-w64-x86-64-posix \
        binutils-mingw-w64-x86-64 \
        xvfb xauth openbox x11vnc websockify novnc feh x11-utils \
        procps p7zip-full cabextract msitools wixl fonts-dejavu-core fontconfig \
        wine64; \
    apt-get remove -y wine64 libwine; \
    rm -rf /var/lib/apt/lists/*

# The pinned MSYS2 build base includes Wine's x86_64+i386 new-WoW64 build.
# Do not install WineHQ stable on top, which would overwrite its Cygwin fixes.
COPY --from=patched-wine /wine /
RUN set -eu; \
    ldconfig; \
    test "$(wine --version)" = 'wine-11.15'; \
    command -v wineboot; command -v wineserver; command -v Xvfb; \
    command -v openbox; command -v websockify; command -v xdpyinfo; command -v x11vnc; command -v feh; \
    test -f /usr/share/novnc/vnc.html; \
    test "$(dpkg-query -W -f='${Version}' wixl)" = '0.106+repack-1'; \
    command -v wixl; command -v msiinfo; command -v msiextract; \
    echo '[MSYS2 WINE] PASS pinned patched Wine 11.15, native guest GUI tools'
COPY gui_preflight.py /usr/local/lib/cinterforge/gui_preflight.py
ENV TZ=UTC
ENV WINEDLLOVERRIDES="mshtml=d;mscoree=d"
