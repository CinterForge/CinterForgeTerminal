@echo off
setlocal EnableExtensions
rem CinterForgeTerminal native Windows bootstrap. Requires only Windows built-in cmd/curl.
rem Keep all provisioned tools in the sibling Builds directory.
set "CF_REPO=%~dp0"
for %%I in ("%CF_REPO%..") do set "CF_WORKSPACE=%%~fI"
set "CF_BUILDS=%CF_WORKSPACE%\Builds"
set "CF_MSYS=%CF_BUILDS%\CinterForgeTerminal\Toolchains\Windows\MSYS2"
set "CF_UCRT=%CF_MSYS%\ucrt64\bin"
set "CF_DOWNLOAD=%CF_BUILDS%\CinterForgeTerminal\Toolchains\Windows\Downloads"
set "CF_INSTALLER=%CF_DOWNLOAD%\msys2-x86_64-20260927.exe"
set "CF_URL=https://repo.msys2.org/distrib/x86_64/msys2-x86_64-20260927.exe"
if not exist "%CF_BUILDS%" mkdir "%CF_BUILDS%" || goto :error
rem A partially populated UCRT64 installation must not be treated as complete.
for %%F in ("%CF_MSYS%\usr\bin\bash.exe" "%CF_MSYS%\usr\bin\pacman.exe" "%CF_UCRT%\gcc.exe" "%CF_UCRT%\g++.exe" "%CF_UCRT%\cmake.exe" "%CF_UCRT%\ninja.exe" "%CF_UCRT%\python.exe" "%CF_UCRT%\node.exe") do (
    if not exist "%%~fF" goto :provision
)
goto :launch
:provision
if not exist "%CF_MSYS%\usr\bin\pacman.exe" (
    if exist "%CF_MSYS%" (
        echo [CinterForgeTerminal] Incomplete MSYS2 directory: "%CF_MSYS%"
        echo [CinterForgeTerminal] Preserving it; refusing destructive reinstall.
        goto :error
    )
    if not exist "%CF_DOWNLOAD%" mkdir "%CF_DOWNLOAD%" || goto :error
    if not exist "%CF_INSTALLER%" (
        echo [CinterForgeTerminal] Downloading official pinned MSYS2 installer...
        where curl.exe >nul 2>&1 || (echo [CinterForgeTerminal] Windows curl.exe unavailable & goto :error)
        curl.exe --fail --location --retry 3 --output "%CF_INSTALLER%.partial" "%CF_URL%" || goto :error
        move /Y "%CF_INSTALLER%.partial" "%CF_INSTALLER%" >nul || goto :error
    )
    echo [CinterForgeTerminal] Installing MSYS2 into Builds...
    "%CF_INSTALLER%" in --confirm-command --accept-messages --root "%CF_MSYS%" || goto :error
)
set "MSYSTEM=UCRT64"
set "CHERE_INVOKING=1"
set "MSYS2_PATH_TYPE=inherit"
set "PATH=%CF_UCRT%;%CF_MSYS%\usr\bin;%PATH%"
echo [CinterForgeTerminal] Installing required UCRT64 build tools...
"%CF_MSYS%\usr\bin\pacman.exe" -Syu --noconfirm || goto :error
"%CF_MSYS%\usr\bin\pacman.exe" -S --needed --noconfirm mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-ninja mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-nodejs mingw-w64-ucrt-x86_64-pkgconf mingw-w64-ucrt-x86_64-binutils || goto :error
:launch
if not exist "%CF_UCRT%\python.exe" (echo [CinterForgeTerminal] MSYS2 Python missing & goto :error)
set "MSYSTEM=UCRT64"
set "CHERE_INVOKING=1"
set "MSYS2_PATH_TYPE=inherit"
set "PATH=%CF_UCRT%;%CF_MSYS%\usr\bin;%PATH%"
"%CF_UCRT%\python.exe" -B "%CF_REPO%Cinter" %*
exit /b %ERRORLEVEL%
:error
>&2 echo [CinterForgeTerminal] Windows bootstrap failed; no success receipt created.
exit /b 1
