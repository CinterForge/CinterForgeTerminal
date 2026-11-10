#!/bin/sh
# Shipping CFOS Ubuntu AMD64 acceptance: real Linux and Windows execution.
# Called within the installed OCI rootfs; no source repo or external files.
set -eu
test "$(dpkg --print-architecture)" = amd64
grep -q 'Ubuntu 24.04' /etc/os-release
dpkg --print-foreign-architectures | grep -qx i386
# The installed image must provide an independent, writable non-root account.
test "$(id -un)" = cinterforge
test "$(id -u)" -ge 1000
test -w /opt/cinterforge
for tool in gcc g++ clang cmake ninja meson make pkg-config python3 git \
            autoconf automake bison flex gfortran qmake6 hugo \
            Xvfb xvfb-run wine x86_64-w64-mingw32-gcc \
            i686-w64-mingw32-gcc ffmpeg wineserver timeout; do
    command -v "$tool" >/dev/null || { echo "Missing headless tool: $tool" >&2; exit 1; }
done
# The shared workstation contract must validate its *real*, independently
# available toolchains before application Build consumes it. GCC 10 native and the
# Ardour GCC 10 Windows cross-compiler are deliberately not conflated.
for tool in gcc-10 g++-10 clang-cl lld-link llvm-rc llvm-mt \
            rustc cargo xwin node npm go java javac mono \
            git-lfs nasm yasm swig glslangValidator pwsh dotnet; do
    command -v "$tool" >/dev/null || { echo "Missing shared build tool: $tool" >&2; exit 1; }
done
gcc-10 -dumpfullversion | grep -Eq '^10\.'
g++-10 -dumpfullversion | grep -Eq '^10\.'
rustc --version | grep -q '^rustc 1\.98\.'
node --version | grep -q '^v22\.16\.0$'
xwin --version >/dev/null
# Check the real shipping Microsoft build tools, as the non-root guest user.
# Both LTS SDK generations must be present, not just a dotnet runtime.
dotnet --list-sdks | grep -q '^8\.0\.'
dotnet --list-sdks | grep -q '^10\.0\.'
dotnet msbuild -version >/dev/null
dotnet nuget --help >/dev/null
pwsh -NoLogo -NoProfile -NonInteractive -Command \
  'if ($PSVersionTable.PSVersion.ToString() -ne "7.6.6") { throw "PowerShell version differs from image contract" }; Write-Output "CFOS-POWERSHELL-OK"' \
  | grep -qx CFOS-POWERSHELL-OK
clang-cl --version >/dev/null
lld-link --version >/dev/null
ldconfig -p | grep -q libglibmm-2.4.so.1
ldconfig -p | grep -q libgtkmm-2.4.so.1
ldconfig -p | grep -q liblrdf.so.0
ldconfig -p | grep -q liblilv-0.so.0
# Ardour's inspected dependency additions must survive into the installed
# read-only image. A build recipe must not need to install these on demand.
for package in libwebsockets-dev libcppunit-dev libgtkmm-2.4-dev \
               vamp-plugin-sdk lv2-dev libsratom-dev libboost-system-dev; do
    dpkg-query -W -f='${Status}' "$package" | grep -qx 'install ok installed' || {
        echo "Missing inspected Ardour build prerequisite: $package" >&2
        exit 1
    }
done
# Whole-image toolchain closure: require every major compiler/linker/runtime
# family to survive the Docker export/import boundary and execute as the
# unprivileged guest. App-specific versions may additionally be pinned later.
for tool in gcc-11 g++-11 gcc-12 g++-12 gcc-13 g++-13 gcc-14 g++-14 \
            clang-17 clang-18 clang-19 clang++-19 clangd-18 \
            clang-tidy-18 clang-format-18 flang-new-18 \
            ld.gold ld.lld-18 mold gdb-multiarch lldb-18 \
            aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ \
            arm-linux-gnueabihf-gcc arm-linux-gnueabihf-g++ \
            x86_64-w64-mingw32-gcc-win32 \
            i686-w64-mingw32-gcc-win32 \
            musl-gcc emcc wasm-opt wat2wasm \
            rustup rustfmt cargo clippy-driver rust-analyzer \
            node npm npx corepack tsc tsserver pnpm yarn esbuild node-gyp \
            python3 pipx cython javac java mvn ant gradle gradle9 go \
            ruby perl php lua5.4 luajit tclsh mono pwsh dotnet; do
    command -v "$tool" >/dev/null || { echo "Missing broad workstation tool: $tool" >&2; exit 1; }
done
gcc-14 -dumpfullversion | grep -Eq '^14\.'
clang-19 --version | grep -q 'version 19\.'
node --version | grep -qx 'v22.16.0'
tsc --version | grep -qx 'Version 5.9.3'
pnpm --version | grep -qx '10.16.1'
yarn --version | grep -qx '1.22.22'
node-gyp --version | grep -qx 'v11.4.2'
rustfmt --version >/dev/null
for target in aarch64-unknown-linux-gnu x86_64-unknown-linux-musl \
              x86_64-pc-windows-gnu i686-pc-windows-gnu \
              x86_64-pc-windows-msvc wasm32-unknown-unknown; do
    rustup target list --installed | grep -qx "$target" || {
        echo "Missing pinned Rust cross target: $target" >&2; exit 1;
    }
done
for pkg in gcc-14 g++-14 clang-19 lld-19 flang-18 mold \
           gcc-aarch64-linux-gnu g++-aarch64-linux-gnu \
           gcc-arm-linux-gnueabihf g++-arm-linux-gnueabihf \
           emscripten binaryen wabt openjdk-21-jdk-headless \
           libwebkit2gtk-4.1-dev libayatana-appindicator3-dev \
           qt6-websockets-dev; do
    dpkg-query -W -f='${Status}' "$pkg" | grep -qx 'install ok installed' || {
        echo "Missing baked OS prerequisite: $pkg" >&2; exit 1;
    }
done
test -s /opt/cinterforge/Receipts/Ubuntu-Toolchain-Packages.tsv
test -s /opt/cinterforge/Receipts/Ubuntu-NonAPT-Toolchains.txt
# Alternative native linkers, ARM targets and WebAssembly must be usable,
# not merely binary names installed into the release archive.
# The shared distribution must include development headers, not just
# dynamically linked runtime libraries. App-specific version pins remain
# declared by inspected Creative Suite recipes and baked in through CFOS Setup.
# CFOS_PKG_CONFIG_BEGIN
# Check every SDK independently. pkg-config --exists on a collection masks
# which one failed, leaving the installer with an opaque exit code after the
# entire image has already been built. Print both the missing module and the
# owning Ubuntu dev package; never skip the requirement or install ad hoc.
for entry in \
    'gtk+-3.0:libgtk-3-dev' \
    'gtk4:libgtk-4-dev' \
    'libcurl:libcurl4-openssl-dev' \
    'vulkan:libvulkan-dev' \
    'Qt6Core:qt6-base-dev' \
    'gstreamer-1.0:libgstreamer1.0-dev' \
    'sdl3:CFOS-pinned-SDL3'; do
    module=${entry%%:*}
    package=${entry#*:}
    if ! pkg-config --exists "$module"; then
        echo "CFOS Ubuntu missing pkg-config module: $module (expected from $package)" >&2
        pkg-config --print-errors --exists "$module" >&2 || :
        if command -v dpkg-query >/dev/null && [ "$package" != CFOS-pinned-SDL3 ]; then
            dpkg-query -W -f='${Status} ${Version}\n' "$package" >&2 || :
        fi
        exit 1
    fi
done
# CFOS_PKG_CONFIG_END
# The GNU unwind SDK is intentionally isolated from LLVM's alternative
# libunwind-18-dev package; validate both the real .pc file and linkage.
test -r /opt/cinterforge/SDKs/GNUlibunwind/usr/include/x86_64-linux-gnu/libunwind.h
pkg-config --exists libunwind
test "$(pkg-config --variable=libdir libunwind)" = \
    /opt/cinterforge/SDKs/GNUlibunwind/usr/lib/x86_64-linux-gnu
pkg-config --cflags --libs gstreamer-1.0 >/dev/null
pkg-config --modversion sdl3 | grep -qx '3.4.18'
test -r /opt/cinterforge/SDKs/Windows/x86_64/include/SDL3/SDL.h
test -r /opt/cinterforge/SDKs/Windows/x86_64/lib/libSDL3.dll.a
test -r /opt/cinterforge/SDKs/Windows/x86_64/bin/SDL3.dll
tmp=$(mktemp -d)
# CFOS_WINE_CLEANUP_BEGIN
# Wine may keep its wineserver alive after the smoke EXE exits. In particular,
# removing a live WINEPREFIX can race with wineserver's final registry writes,
# causing `rm -rf` to fail even though every compiler/runtime smoke passed.
# This prefix is unique to this test; never stop or delete a user's Wine state.
cleanup_cfos_smoke() {
    cfos_result=$?
    trap - EXIT
    if [ "${WINEPREFIX:-}" = "$tmp/wine-prefix" ]; then
        if ! timeout 60 wineserver -w; then
            echo '[CFOS Ubuntu] Wine server did not quiesce; stopping isolated smoke prefix' >&2
            wineserver -k || :
            timeout 20 wineserver -w || :
        fi
    fi
    if ! rm -rf -- "$tmp"; then
        echo '[CFOS Ubuntu] Failed to remove isolated smoke scratch directory' >&2
        if [ "$cfos_result" -eq 0 ]; then cfos_result=1; fi
    fi
    exit "$cfos_result"
}
trap cleanup_cfos_smoke EXIT
# CFOS_WINE_CLEANUP_END
# Both headers and linkable GNU libunwind/GStreamer libraries are required.
# LLVM libunwind's virtual APT dependency alone cannot satisfy this check.
cat > "$tmp/check_unwind.c" <<'EOF'
#include <libunwind.h>
#include <stdio.h>
int main(void) {
    unw_context_t context;
    if (unw_getcontext(&context) != 0) return 1;
    puts("CFOS-UNWIND-OK");
    return 0;
}
EOF
cc $(pkg-config --cflags libunwind) "$tmp/check_unwind.c" \
    -o "$tmp/unwind-check" $(pkg-config --libs libunwind)
"$tmp/unwind-check" | grep -qx CFOS-UNWIND-OK
cat > "$tmp/check_gstreamer.c" <<'EOF'
#include <gst/gst.h>
#include <stdio.h>
int main(void) {
    gst_init(NULL, NULL);
    puts("CFOS-GSTREAMER-OK");
    return 0;
}
EOF
cc $(pkg-config --cflags gstreamer-1.0) "$tmp/check_gstreamer.c" \
    -o "$tmp/gstreamer-check" $(pkg-config --libs gstreamer-1.0)
"$tmp/gstreamer-check" | grep -qx CFOS-GSTREAMER-OK
# Execute the cross-platform C# build graph with the real MSBuild SDK and
# run it, rather than counting `dotnet --version` as a passed smoke test.
mkdir -p "$tmp/dotnet-check" "$tmp/nuget-empty"
cat > "$tmp/dotnet-check/dotnet-check.csproj" <<'EOF'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
  </PropertyGroup>
</Project>
EOF
printf '%s\n' 'System.Console.WriteLine("CFOS-DOTNET-OK");' > "$tmp/dotnet-check/Program.cs"
dotnet restore "$tmp/dotnet-check/dotnet-check.csproj" \
  --source "$tmp/nuget-empty" --ignore-failed-sources >/dev/null
dotnet msbuild "$tmp/dotnet-check/dotnet-check.csproj" \
  -target:Build -property:Configuration=Release >/dev/null
dotnet "$tmp/dotnet-check/bin/Release/net10.0/dotnet-check.dll" | grep -qx CFOS-DOTNET-OK
printf '%s\n' '#include <stdio.h>' 'int main(void) { puts("CFOS-LINUX-OK"); return 0; }' > "$tmp/check.c"
gcc -O2 "$tmp/check.c" -o "$tmp/linux-check"
"$tmp/linux-check" | grep -qx CFOS-LINUX-OK
printf '%s\n' '#include <iostream>' 'int main() { std::cout << "CFOS-CXX-OK\n"; }' > "$tmp/check.cpp"
g++ -std=c++17 -O2 "$tmp/check.cpp" -o "$tmp/linux-cxx-check"
"$tmp/linux-cxx-check" | grep -qx CFOS-CXX-OK
gcc-10 -O2 "$tmp/check.c" -o "$tmp/gcc10-check"
"$tmp/gcc10-check" | grep -qx CFOS-LINUX-OK
g++-10 -std=c++17 -O2 "$tmp/check.cpp" -o "$tmp/gcc10-cxx-check"
"$tmp/gcc10-cxx-check" | grep -qx CFOS-CXX-OK
gcc-14 -O2 "$tmp/check.c" -o "$tmp/gcc14-check"
"$tmp/gcc14-check" | grep -qx CFOS-LINUX-OK
g++ -std=c++17 -fuse-ld=mold "$tmp/check.cpp" -o "$tmp/mold-cxx-check"
"$tmp/mold-cxx-check" | grep -qx CFOS-CXX-OK
g++ -std=c++17 -fuse-ld=gold "$tmp/check.cpp" -o "$tmp/gold-cxx-check"
"$tmp/gold-cxx-check" | grep -qx CFOS-CXX-OK
clang++-19 -std=c++17 -fuse-ld=lld "$tmp/check.cpp" -o "$tmp/clang19-cxx-check"
"$tmp/clang19-cxx-check" | grep -qx CFOS-CXX-OK
aarch64-linux-gnu-g++ -std=c++17 -c "$tmp/check.cpp" -o "$tmp/arm64-cxx-check.o"
file "$tmp/arm64-cxx-check.o" | grep -qi 'aarch64'
arm-linux-gnueabihf-g++ -std=c++17 -c "$tmp/check.cpp" -o "$tmp/armhf-cxx-check.o"
file "$tmp/armhf-cxx-check.o" | grep -q 'ARM'
printf '%s\n' '(module (func (export "answer") (result i32) i32.const 42))' > "$tmp/check.wat"
wat2wasm "$tmp/check.wat" -o "$tmp/check.wasm"
test -s "$tmp/check.wasm"
wasm-opt "$tmp/check.wasm" -O -o "$tmp/check-opt.wasm"
test -s "$tmp/check-opt.wasm"
emcc --version >/dev/null
# Python/Cython/venv toolchain from the shipping account, no download.
python3 -m venv "$tmp/py-venv"
"$tmp/py-venv/bin/python3" -c 'print("CFOS-PYTHON-OK")' | grep -qx CFOS-PYTHON-OK
# A real Java/JVM build, independent of Java version selection.
cat > "$tmp/CFOSCheck.java" <<'EOF'
public class CFOSCheck {
  public static void main(String[] args) { System.out.println("CFOS-JAVA-OK"); }
}
EOF
javac -d "$tmp" "$tmp/CFOSCheck.java"
java -cp "$tmp" CFOSCheck | grep -qx CFOS-JAVA-OK
mvn --version >/dev/null
GRADLE_USER_HOME="$tmp/gradle-home" gradle --version | grep -q 'Gradle 8.14.6'
GRADLE_USER_HOME="$tmp/gradle-home" gradle9 --version | grep -q 'Gradle 9.8.2' 
# Go native and Windows cross-target CGo-free pipelines.
printf '%s\n' 'package main' 'import "fmt"' 'func main() { fmt.Println("CFOS-GO-OK") }' > "$tmp/check.go"
export GOCACHE="$tmp/go-cache" GOPATH="$tmp/go-path"
GO111MODULE=off CGO_ENABLED=0 go build -o "$tmp/go-check" "$tmp/check.go"
"$tmp/go-check" | grep -qx CFOS-GO-OK
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -o "$tmp/go-windows-check.exe" "$tmp/check.go"
file "$tmp/go-windows-check.exe" | grep -q PE32+
# The MSVC-compatible LLVM frontend and linker must emit a real Windows
# COFF/PE binary without silently using the GNU/MinGW build path. This DLL
# has no Microsoft CRT imports, so proprietary SDK redistribution is not needed.
printf '%s\n' '__declspec(dllexport) int cfos_abi(void) { return 42; }' > "$tmp/check_msvc.c"
clang-cl /nologo /c /Fo"$tmp/check_msvc.obj" "$tmp/check_msvc.c"
lld-link /nologo /dll /noentry /nodefaultlib \
    /out:"$tmp/check_msvc.dll" "$tmp/check_msvc.obj"
file "$tmp/check_msvc.dll" | grep -q PE32+
printf '%s\n' 'fn main() { println!("CFOS-RUST-OK"); }' > "$tmp/rust_check.rs"
rustc "$tmp/rust_check.rs" -o "$tmp/rust-check"
"$tmp/rust-check" | grep -qx CFOS-RUST-OK
# Rust/Cargo must build a real offline crate and Windows GNU cross target.
# Use the scratch Cargo home; the frozen global toolchain stays read-only.
export CARGO_HOME="$tmp/cargo-home" CARGO_TARGET_DIR="$tmp/cargo-target"
cargo new --quiet --vcs none --bin "$tmp/cargo-check"
printf '%s\n' 'fn main() { println!("CFOS-CARGO-OK"); }' > "$tmp/cargo-check/src/main.rs"
cargo build --quiet --offline --manifest-path "$tmp/cargo-check/Cargo.toml"
"$CARGO_TARGET_DIR/debug/cargo-check" | grep -qx CFOS-CARGO-OK
cargo build --quiet --offline --manifest-path "$tmp/cargo-check/Cargo.toml" \
    --target x86_64-pc-windows-gnu
file "$CARGO_TARGET_DIR/x86_64-pc-windows-gnu/debug/cargo-check.exe" | grep -q PE32+
# TypeScript must compile and execute locally without an npm install.
printf '%s\n' 'const value: number = 42;' \
    'console.log(value === 42 ? "CFOS-TS-OK" : "bad");' > "$tmp/check.ts"
tsc --target es2020 --module commonjs --outDir "$tmp" "$tmp/check.ts"
node "$tmp/check.js" | grep -qx CFOS-TS-OK
node -e 'console.log("CFOS-NODE-OK")' | grep -qx CFOS-NODE-OK
# CinterForgeSteam Linux and Windows must use the SAME verified SDL3 API.
# Exercise the dynamic loader under the exported/imported production rootfs.
cat > "$tmp/check_sdl3.c" <<'EOF'
#include <SDL3/SDL.h>
#include <stdio.h>
int main(void) {
    const int version = SDL_GetVersion();
    if (version < SDL_VERSIONNUM(3, 4, 18)) return 3;
    puts("CFOS-SDL3-OK");
    return 0;
}
EOF
cc -O2 $(pkg-config --cflags sdl3) "$tmp/check_sdl3.c" \
    -o "$tmp/sdl3-linux-check" $(pkg-config --libs sdl3)
"$tmp/sdl3-linux-check" | grep -qx CFOS-SDL3-OK
sdk=/opt/cinterforge/SDKs/Windows/x86_64
x86_64-w64-mingw32-gcc -O2 "$tmp/check_sdl3.c" \
    -I"$sdk/include" -L"$sdk/lib" -lSDL3 -o "$tmp/sdl3-windows-check.exe"
file "$tmp/sdl3-windows-check.exe" | grep -q PE32+
cp "$sdk/bin/SDL3.dll" "$tmp/SDL3.dll"
x86_64-w64-mingw32-gcc -O2 "$tmp/check.c" -o "$tmp/windows-check.exe"
file "$tmp/windows-check.exe" | grep -q 'PE32+'
x86_64-w64-mingw32-g++ -std=c++17 -O2 "$tmp/check.cpp" -o "$tmp/windows-cxx-check.exe"
file "$tmp/windows-cxx-check.exe" | grep -q 'PE32+'
i686-w64-mingw32-gcc -O2 "$tmp/check.c" -o "$tmp/windows-x86-check.exe"
file "$tmp/windows-x86-check.exe" | grep -q 'PE32 executable'
x86_64-w64-mingw32-gcc-win32 -O2 "$tmp/check.c" -o "$tmp/windows-win32-api-check.exe"
file "$tmp/windows-win32-api-check.exe" | grep -q 'PE32+'
i686-w64-mingw32-gcc-win32 -O2 "$tmp/check.c" -o "$tmp/windows-i686-win32-check.exe"
file "$tmp/windows-i686-win32-check.exe" | grep -q 'PE32 executable'
# Launch a real Windows console PE in a clean, headless Wine prefix. Only
# checking wine --version is insufficient for Windows application testing.
export WINEPREFIX="$tmp/wine-prefix" WINEDEBUG=-all WINEDLLOVERRIDES=mscoree,mshtml=
xvfb-run -a timeout 120 wine "$tmp/windows-check.exe" > "$tmp/windows.log" 2>&1 || {
    cat "$tmp/windows.log" >&2
    echo 'CFOS Windows Wine execution failed' >&2
    exit 1
}
grep -q CFOS-LINUX-OK "$tmp/windows.log" || {
    cat "$tmp/windows.log" >&2
    echo 'CFOS Windows console output missing' >&2
    exit 1
}
# Test that Windows SDL3 DLLs load under Wine in the shipping image.
xvfb-run -a timeout 120 wine "$tmp/sdl3-windows-check.exe" > "$tmp/sdl3-windows.log" 2>&1 || {
    cat "$tmp/sdl3-windows.log" >&2
    echo 'CFOS Windows SDL3 Wine execution failed' >&2
    exit 1
}
grep -q CFOS-SDL3-OK "$tmp/sdl3-windows.log" || {
    cat "$tmp/sdl3-windows.log" >&2
    echo 'CFOS Windows SDL3 output missing' >&2
    exit 1
}
# All Wine applications must finish before this isolated prefix can be removed.
timeout 60 wineserver -w || {
    echo '[CFOS Ubuntu] Wine smoke subprocesses did not terminate cleanly' >&2
    exit 1
}
echo '[CFOS Steam workstation] PASS: pinned Linux SDL3 and Windows x64 SDL3 DLL/import library, real Linux and Wine execution'
echo '[CFOS Ubuntu toolchain] PASS GCC 10-14, Clang 17-19/LLVM/MSVC-clang, gold/lld/mold, Rust/Cargo cross-targets, Node/TypeScript/pnpm/yarn, Python, Java, Go, WebAssembly, Linux/ARM and Windows cross-compilers, Wine/SDL3, PowerShell and .NET 8/10'
# Every shipping image also contains KDE; the build must not treat a missing
# graphical payload as a successful headless-only distribution.
for tool in startplasma-x11 kwin_x11 x11vnc websockify; do
    command -v "$tool" >/dev/null || { echo "Missing Ubuntu desktop tool: $tool" >&2; exit 1; }
done
test -s /usr/share/novnc/vnc.html
test -x /usr/local/bin/cfos-ubuntu-desktop
test -x /usr/local/bin/cfos-ubuntu-shell
# The Bash service controller is sourced by BOTH guest launchers. Docker COPY
# may preserve root-only permissions from a host checkout; fail explicitly.
if ! test -r /usr/local/lib/cfos-ubuntu-service.sh; then
    echo '[CFOS Ubuntu] Guest-local service controller not readable by cinterforge: /usr/local/lib/cfos-ubuntu-service.sh' >&2
    ls -ld /usr/local/lib /usr/local/lib/cfos-ubuntu-service.sh >&2 || :
    exit 1
fi
/bin/bash -n /usr/local/lib/cfos-ubuntu-service.sh
echo '[CFOS Ubuntu distribution] PASS headless build/test tools and desktop payload are in the SAME image'

