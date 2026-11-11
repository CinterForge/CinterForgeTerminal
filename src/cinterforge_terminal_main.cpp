#include "CinterApplicationShell.h"
#include <iostream>
#include <stdexcept>
#include <string>
#include <filesystem>
#include <fstream>
#include <vector>
#include <cstdlib>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif
#ifdef __APPLE__
#include <mach-o/dyld.h>
#include <cstdlib>
#endif

#ifdef __APPLE__
namespace
{
    namespace fs = std::filesystem;

    // Finder launches the *Mach-O* CFBundleExecutable, not a shell script.
    // Resolve the application's own resource closure, or its sibling Builds in
    // a developer stage. Do not depend on the current working directory.
    fs::path currentExecutable()
    {
        std::vector<char> buffer(4096);
        uint32_t size = static_cast<uint32_t>(buffer.size());
        if (_NSGetExecutablePath(buffer.data(), &size) != 0)
        {
            buffer.resize(size + 1);
            if (_NSGetExecutablePath(buffer.data(), &size) != 0)
                throw std::runtime_error("Cannot resolve CinterForge Terminal executable");
        }
        return fs::weakly_canonical(fs::path(buffer.data()));
    }

    void useIfUnset(const char *name, const std::string &value)
    {
        if (!std::getenv(name) && !value.empty())
            setenv(name, value.c_str(), 0);
    }

    void fillMacAppDefaults(cinterforge::ApplicationShellConfig &cfg)
    {
        const fs::path app = currentExecutable().parent_path().parent_path().parent_path();
        const fs::path embedded = app / "Contents/Resources/Builds";
        fs::path builds;
        if (!cfg.builds.empty())
            builds = cfg.builds;
        else if (fs::is_directory(embedded))
            builds = embedded;
        else
        {
            fs::path candidate = app;
            while (candidate != candidate.root_path() && candidate.filename() != "Builds")
                candidate = candidate.parent_path();
            if (candidate.filename() != "Builds")
                throw std::runtime_error("Cannot locate Builds for macOS Terminal (missing bundled runtime)");
            builds = candidate;
        }
        builds = fs::weakly_canonical(builds);
        cfg.builds = builds.string();
        const fs::path hostBase = builds / "Python/Host/mac/arm64";
        fs::path release = app / "Contents/Resources/HostRelease.txt";
        std::string releaseName;
        if (fs::is_regular_file(release))
        {
            std::ifstream stream(release);
            std::getline(stream, releaseName);
        }
        if (releaseName.empty())
        {
            // Existing developer stages may not yet contain the release hint.
            // Use only complete service directories, newest version first.
            if (fs::is_directory(hostBase))
            {
                for (const auto &entry : fs::directory_iterator(hostBase))
                {
                    if (!entry.is_directory() ||
                        !fs::is_regular_file(entry.path() / "webapp/CinterForgeTerminal.py") ||
                        !fs::is_regular_file(entry.path() / "python/worker.py"))
                        continue;
                    if (entry.path().filename().string() > releaseName)
                        releaseName = entry.path().filename().string();
                }
            }
        }
        if (releaseName.empty() || releaseName.find('/') != std::string::npos ||
            releaseName.find('\\') != std::string::npos || releaseName == "..")
            throw std::runtime_error("Packaged macOS Terminal host release is missing or invalid");
        const fs::path host = hostBase / releaseName;
        if (cfg.python.empty())
            cfg.python = (builds / "Python/Environments/mac/arm64/cinterterminalenv/bin/python").string();
        if (cfg.worker.empty())
            cfg.worker = (host / "python/worker.py").string();
        if (cfg.service.empty())
            cfg.service = (host / "webapp/CinterForgeTerminal.py").string();
        // A packaged app reads its embedded Builds-shaped resource closure but
        // must never treat it as a writable developer Builds tree. In developer
        // sessions the Python PTY exports CINTER_BUILDS_ROOT explicitly.
        if (cfg.developer)
            setenv("CINTER_BUILDS_ROOT", cfg.builds.c_str(), 1);
        if (cfg.developer)
        {
            // Explicit developer launcher: lifecycle and VM state belong to Builds.
            // A downloaded app must not inherit runtime VM paths inside the signed
            // Contents/Resources/Builds payload; Python chooses its Steam root.
            const fs::path state = builds / "CFTVM";
            useIfUnset("COLIMA_HOME", (state / "colima").string());
            useIfUnset("LIMA_HOME", (state / "lima").string());
            useIfUnset("COLIMA_CACHE_HOME", (state / "cache/colima").string());
            useIfUnset("DOCKER_CONFIG", (state / "docker").string());
            useIfUnset("DOCKER_CONTEXT", "colima-cinterforge-windows");
        }
        // On HOPE-DEV, developer tools may exist in sibling Builds. The lightweight
        // released app does not require them for its normal WebView startup.
        const fs::path tools = builds / "CinterForgeTerminal/Toolchains/mac";
        if (fs::is_directory(tools))
        {
            const std::string prior = std::getenv("PATH") ? std::getenv("PATH") : "";
            setenv("PATH", ((tools / "lima/bin").string() + ":" + (tools / "env/bin").string() + ":" + prior).c_str(), 1);
        }
    }
} // namespace
#endif

#if defined(_WIN32) || defined(__linux__)
namespace
{
    namespace fs = std::filesystem;

    fs::path ownExecutable()
    {
#ifdef _WIN32
        std::vector<wchar_t> buffer(32768);
        const DWORD count = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (!count || count >= buffer.size())
            throw std::runtime_error("Cannot determine installed Windows Terminal executable path");
        return fs::weakly_canonical(fs::path(std::wstring(buffer.data(), count)));
#else
        return fs::canonical("/proc/self/exe");
#endif
    }

    std::string pathString(const fs::path &path)
    {
#ifdef _WIN32
        // Windows filesystem is UTF-16; use UTF-8 for the Host's argument interface.
        return path.u8string();
#else
        return path.string();
#endif
    }

    void fillPackagedDefaults(cinterforge::ApplicationShellConfig &cfg)
    {
        const fs::path exe = ownExecutable();
        const fs::path root = exe.parent_path();
        // A packaged app owns its read-only runtime next to the launcher. This is
        // NOT the user's mutable Steam library or the developer's sibling Builds.
        if (!cfg.builds.empty() && !cfg.python.empty() &&
            !cfg.worker.empty() && !cfg.service.empty())
            return;
        const fs::path embedded = root / "Builds";
        if (cfg.builds.empty() && !fs::is_directory(embedded))
            return;
        const fs::path builds = fs::weakly_canonical(cfg.builds.empty() ? embedded : fs::path(cfg.builds));
#ifdef _WIN32
        constexpr const char *platform = "win";
        constexpr const char *arch = "x64";
        constexpr const char *pythonExe = "python.exe";
#else
        constexpr const char *platform = "linux";
        constexpr const char *arch = "x64";
        constexpr const char *pythonExe = "bin/python";
#endif
        const fs::path runtime = builds / "Python/Environments" / platform / arch / "cinterterminalenv" / pythonExe;
        const fs::path hostBase = builds / "Python/Host" / platform / arch;
        fs::path host;
        // Filename ordering here is only a fallback for an older stage without a
        // version hint. The packaged Host must have both required entrypoints.
        const fs::path releaseHint = root / "HostRelease.txt";
        if (fs::is_regular_file(releaseHint))
        {
            std::ifstream input(releaseHint);
            std::string version;
            std::getline(input, version);
            if (!version.empty() && version != "." && version != ".." &&
                version.find_first_of("/\\") == std::string::npos)
                host = hostBase / version;
        }
        if (host.empty() && fs::is_directory(hostBase))
        {
            for (const auto &item : fs::directory_iterator(hostBase))
            {
                if (!item.is_directory() ||
                    !fs::is_regular_file(item.path() / "python/worker.py") ||
                    !fs::is_regular_file(item.path() / "webapp/CinterForgeTerminal.py"))
                    continue;
                if (host.empty() || item.path().filename().string() > host.filename().string())
                    host = item.path();
            }
        }
        if (cfg.builds.empty())
            cfg.builds = pathString(builds);
        if (cfg.python.empty())
            cfg.python = pathString(runtime);
        if (cfg.worker.empty() && !host.empty())
            cfg.worker = pathString(host / "python/worker.py");
        if (cfg.service.empty() && !host.empty())
            cfg.service = pathString(host / "webapp/CinterForgeTerminal.py");
#ifdef _WIN32
        const fs::path webview = builds / "CinterForgeTerminal/Dependencies/WebView2FixedRuntime";
        if (fs::is_regular_file(webview / "msedgewebview2.exe") &&
            !std::getenv("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"))
            _putenv_s("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", pathString(webview).c_str());
#endif
        if (!fs::is_regular_file(cfg.python) || cfg.worker.empty() || cfg.service.empty() ||
            !fs::is_regular_file(cfg.worker) || !fs::is_regular_file(cfg.service))
            throw std::runtime_error("Incomplete packaged Terminal runtime next to " + pathString(exe) +
                                     ": expected Python interpreter and Host worker/server under " + pathString(builds));
    }
} // namespace
#endif

int main(int argc, char **argv)
{
    cinterforge::ApplicationShellConfig cfg;
    try
    {
        for (int i = 1; i < argc; ++i)
        {
            const std::string flag(argv[i]);
            if (flag == "--help" || flag == "-h")
            {
                std::cout << "CinterForgeTerminal --python <runtime> --worker <worker.py> "
                             "--service <server.py> --builds <Builds> [--title name] [--developer] [--smoke-js] [--accept-ui]\n";
                return 0;
            }
            if (flag == "--developer")
            {
                cfg.developer = true;
                continue;
            }
            if (flag == "--accept-ui")
            {
                cfg.accept_ui = true;
                continue;
            }
            if (flag == "--smoke-js")
                return cinterforge::smokeWebViewJavaScript();
#ifdef __APPLE__
            // Xcode may pass this Cocoa document-versioning preference as an
            // argument pair. It is not one of Terminal's CLI options.
            if (flag == "-NSDocumentRevisionsDebugMode")
            {
                if (i + 1 >= argc)
                    throw std::invalid_argument("Missing value for " + flag);
                ++i;
                continue;
            }
#endif
            if (i + 1 >= argc)
                throw std::invalid_argument("Missing value for " + flag);
            const std::string value(argv[++i]);
            if (flag == "--python")
                cfg.python = value;
            else if (flag == "--worker")
                cfg.worker = value;
            else if (flag == "--service")
                cfg.service = value;
            else if (flag == "--builds")
                cfg.builds = value;
            else if (flag == "--title")
                cfg.title = value;
            else
                throw std::invalid_argument("Unknown option: " + flag);
        }
#ifdef __APPLE__
        fillMacAppDefaults(cfg);
#elif defined(_WIN32) || defined(__linux__)
        fillPackagedDefaults(cfg);
#endif
        return cinterforge::runApplicationShell(cfg);
    }
    catch (const std::exception &e)
    {
        std::cerr << "[CinterForgeTerminal] FAIL: " << e.what() << '\n';
        return 1;
    }
}

#ifdef _WIN32
// Windows-subsystem GUI entry point; preserve argv handling in main (including
// --smoke-js and --help) so native acceptance is identical to the console build.
int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int)
{
    return main(__argc, __argv);
}
#endif
