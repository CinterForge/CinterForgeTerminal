# CinterForgeTerminal's conventional Linux/amd64 cross-build environment.
# Provisioned ONCE during Setup; exported to sibling Builds for offline reuse.
# Not used for native Windows acceptance (which uses MSYS2 UCRT64).
FROM --platform=linux/amd64 debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         bash ca-certificates cmake ninja-build pkg-config python3 \
         gcc-mingw-w64-x86-64-posix g++-mingw-w64-x86-64-posix \
         binutils-mingw-w64-x86-64 wine64 wine xvfb xauth p7zip-full cabextract fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*
