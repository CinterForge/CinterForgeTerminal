# CINTERFORGE SOURCE HEADER — host/docker/windows-cross.Dockerfile
# PURPOSE: Maintains windows cross within CinterForgeTerminal without duplicating external ownership.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# INTERFACE: This file declares presentation/build rules rather than callable methods.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
# CinterForgeTerminal Setup-only Windows cross/guest image (linux/amd64).
# Use MSYS2's patched Wine binary toolchain, NOT the experimental MSYS2 image:
# the latter disables pacman signature checks and is unsuitable for staging.
# Digest verified from ghcr.io/msys2/msys2-docker-build-base on 2026-11-12.
# Upstream builds Wine 11.15 plus MSYS2/Cygwin compatibility patches.
FROM --platform=linux/amd64 ghcr.io/msys2/msys2-docker-build-base@sha256:19c2b7a96a437f71222656462e4fa5014245f2eb2633ac3988dd9cf208f51ed4 AS patched-wine
FROM --platform=linux/amd64 docker.io/library/debian@sha256:7378260c0a098d27ac206225db5e7d92b8c7c84ea7afccc86eeb513756bbd392
ENV DEBIAN_FRONTEND=noninteractive

# The ONE Debian AMD64 image contains the Linux WebKitGTK builder, patched
# Wine/MSYS2 Windows cross-build tools, WiX, and both GUI session dependencies.
# Retain Debian's signed APT dependency closure; never duplicate a Linux
# runtime image just to provide Linux Bash in a second container. No package signature bypass.
# Match the distribution in the MSYS2 Wine build stage to avoid glibc ABI skew.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash ca-certificates cmake ninja-build pkg-config python3 nodejs dpkg-dev xz-utils \
        gcc g++ zlib1g-dev libarchive-dev libgtk-3-dev libwebkit2gtk-4.1-dev libwebkit2gtk-4.1-0 dbus-x11 \
        gcc-mingw-w64-x86-64-posix g++-mingw-w64-x86-64-posix \
        binutils-mingw-w64-x86-64 libz-mingw-w64-dev \
        xvfb xauth openbox x11vnc websockify novnc feh x11-utils \
        procps p7zip-full cabextract msitools wixl fonts-dejavu-core fontconfig \
        assimp-utils ffmpeg imagemagick \
        wine64; \
    apt-get remove -y wine64 libwine; \
    rm -rf /var/lib/apt/lists/*

# Setup-owned Windows SDK for the bundled CraftMaster CLI.  Linux zlib1g-dev
# and libarchive-dev cannot be linked into a Windows PE.  Debian supplies the
# MinGW zlib SDK; build a matching static Windows libarchive in this one pinned
# AMD64 image from Debian's authenticated source repository.  Keep all fetches
# and dependency provisioning in image Setup, NEVER in CraftMaster Build/Stage.
# Disable non-ZIP codecs and dynamic linkage: the shipped converter only needs
# the ZIP+deflate path, and must not require a libarchive DLL at user runtime.
RUN set -eu; \
    test -f /usr/x86_64-w64-mingw32/include/zlib.h; \
    test -s /usr/x86_64-w64-mingw32/lib/libz.a; \
    test -f /etc/apt/sources.list.d/debian.sources; \
    sed 's/^Types: deb$/Types: deb-src/' \
        /etc/apt/sources.list.d/debian.sources \
        > /etc/apt/sources.list.d/craftmaster-source.sources; \
    apt-get update; \
    mkdir -p /tmp/craftmaster-libarchive-source; \
    cd /tmp/craftmaster-libarchive-source; \
    apt-get source libarchive; \
    source_dir="$(find . -mindepth 1 -maxdepth 1 -type d -name 'libarchive-*' -print -quit)"; \
    test -n "$source_dir"; \
    cmake -S "$source_dir" -B /tmp/craftmaster-libarchive-win -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_SYSTEM_NAME=Windows \
        -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc \
        -DCMAKE_RC_COMPILER=x86_64-w64-mingw32-windres \
        -DCMAKE_FIND_ROOT_PATH=/usr/x86_64-w64-mingw32 \
        -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER \
        -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY \
        -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY \
        -DCMAKE_INSTALL_PREFIX=/usr/x86_64-w64-mingw32 \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DBUILD_SHARED_LIBS=OFF \
        -DENABLE_TEST=OFF -DENABLE_TAR=OFF -DENABLE_CPIO=OFF \
        -DENABLE_CAT=OFF -DENABLE_UNZIP=OFF \
        -DENABLE_OPENSSL=OFF -DENABLE_CNG=OFF -DENABLE_WIN32_XMLLITE=OFF \
        -DENABLE_BZip2=OFF -DENABLE_LZMA=OFF \
        -DENABLE_LZ4=OFF -DENABLE_LZO=OFF \
        -DENABLE_ZSTD=OFF -DENABLE_LIBB2=OFF \
        -DENABLE_LIBXML2=OFF -DENABLE_EXPAT=OFF \
        -DENABLE_PCREPOSIX=OFF -DENABLE_PCRE2POSIX=OFF \
        -DENABLE_ICONV=OFF -DENABLE_XATTR=OFF -DENABLE_ACL=OFF \
        -DENABLE_ZLIB=ON \
        -DZLIB_LIBRARY=/usr/x86_64-w64-mingw32/lib/libz.a \
        -DZLIB_INCLUDE_DIR=/usr/x86_64-w64-mingw32/include; \
    cmake --build /tmp/craftmaster-libarchive-win --parallel 4; \
    cmake --install /tmp/craftmaster-libarchive-win; \
    test -f /usr/x86_64-w64-mingw32/include/archive.h; \
    test -f /usr/x86_64-w64-mingw32/include/archive_entry.h; \
    test -s /usr/x86_64-w64-mingw32/lib/libarchive.a; \
    printf '%s\n' '#include <zlib.h>' '#include <archive.h>' \
        'int main(void) { struct archive *a = archive_write_new(); if (!a) return 1; archive_write_set_format_zip(a); archive_write_free(a); return zlibVersion() == 0; }' \
        > /tmp/craftmaster-win-sdk-smoke.c; \
    x86_64-w64-mingw32-gcc \
        /tmp/craftmaster-win-sdk-smoke.c \
        -I/usr/x86_64-w64-mingw32/include \
        -o /tmp/craftmaster-win-sdk-smoke.exe \
        /usr/x86_64-w64-mingw32/lib/libarchive.a \
        /usr/x86_64-w64-mingw32/lib/libz.a \
        -lws2_32 -ladvapi32 -lbcrypt -lcrypt32; \
    test "$(od -An -tx1 -N2 /tmp/craftmaster-win-sdk-smoke.exe | tr -d ' \n')" = '4d5a'; \
    rm -rf /tmp/craftmaster-libarchive-source /tmp/craftmaster-libarchive-win \
        /tmp/craftmaster-win-sdk-smoke.c /tmp/craftmaster-win-sdk-smoke.exe; \
    rm -f /etc/apt/sources.list.d/craftmaster-source.sources; \
    rm -rf /var/lib/apt/lists/*; \
    echo '[CRAFTMASTER WINDOWS SDK] PASS MinGW zlib + static libarchive PE link'

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
    pkg-config --exists gtk+-3.0 webkit2gtk-4.1; \
    python3 -c 'import ctypes; ctypes.CDLL("libwebkit2gtk-4.1.so.0")'; \
    command -v g++; command -v node; \
    echo '[MSYS2 WINE] PASS pinned patched Wine 11.15, native guest GUI tools'
# Runtime ABI linking is asserted above as well as by GUI preflight.
# A header/pkg-config check alone cannot certify the native ELF launcher.
COPY gui_preflight.py /usr/local/lib/cinterforge/gui_preflight.py
ENV TZ=UTC
ENV WINEDLLOVERRIDES="mshtml=d;mscoree=d"

ENV WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
ENV LIBGL_ALWAYS_SOFTWARE=1
