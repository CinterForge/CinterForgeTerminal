/*
 * CINTERFORGE SOURCE HEADER — app/src/support/CinterTerminalMain.cpp
 * PURPOSE: Resolves macOS/Windows/Linux executable and resource locations, native launch diagnostics and developer versus installed mode.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   currentExecutable: Handle current executable through this component's established interfaces.
 *   console_: Handle console through this component's established interfaces.
 *   ready: Handle ready through this component's established interfaces.
 *   nativeLogRoot: Handle native log root through this component's established interfaces.
 *   beginNativeLogging: Handle begin native logging through this component's established interfaces.
 *   useIfUnset: Handle use if unset through this component's established interfaces.
 *   fillMacAppDefaults: Handle fill mac app defaults through this component's established interfaces.
 *   ownExecutable: Handle own executable through this component's established interfaces.
 *   pathString: Handle path string through this component's established interfaces.
 *   fillPackagedDefaults: Handle fill packaged defaults through this component's established interfaces.
 *   ...and 2 additional internal entry points; see their local documentation.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
#include "CinterApplicationShell.h"
#include <iostream>
#include <stdexcept>
#include <string>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <streambuf>
#include <chrono>
#include <ctime>
#include <memory>
#include <vector>
#include <algorithm>
#include <cstdlib>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif
#ifdef __APPLE__
#include <mach-o/dyld.h>
#include <CoreFoundation/CoreFoundation.h>
#include <syslog.h>
#include <unistd.h>
#include <cstdlib>
#endif

#ifdef _WIN32
namespace {
    // A Windows-subsystem GUI executable does not reliably inherit cmd.exe's
    // stderr/stdout handles. Record native WebView2 diagnostics independently
    // under the host-owned guest Logs mount selected by the launcher.
    class WindowsGuestLogBuffer final : public std::streambuf {
    public:
        explicit WindowsGuestLogBuffer(const std::filesystem::path &path)
            : stream_(path, std::ios::binary | std::ios::app) {}
        bool ready() const { return stream_.is_open(); }
    protected:
        int_type overflow(int_type ch) override {
            if (traits_type::eq_int_type(ch, traits_type::eof())) return traits_type::not_eof(ch);
            std::lock_guard<std::mutex> lock(mutex_);
            stream_.put(traits_type::to_char_type(ch));
            stream_.flush();
            return ch;
        }
        std::streamsize xsputn(const char *data, std::streamsize n) override {
            std::lock_guard<std::mutex> lock(mutex_);
            stream_.write(data, n);
            stream_.flush();
            return n;
        }
        int sync() override {
            std::lock_guard<std::mutex> lock(mutex_);
            stream_.flush();
            return stream_ ? 0 : -1;
        }
    private:
        std::mutex mutex_;
        std::ofstream stream_;
    };

    void beginWindowsGuestLogging() {
        const char *name = std::getenv("CINTER_WINDOWS_NATIVE_LOG");
        if (!name || !*name) return;
        // Never derive/create Logs from a downloaded Windows app by default.
        // The Mac-owned guest launcher explicitly supplies this persistent path.
        static auto *buffer = new WindowsGuestLogBuffer(std::filesystem::path(name));
        if (!buffer->ready())
            throw std::runtime_error(std::string("Cannot open managed Windows native log: ") + name);
        std::cerr.rdbuf(buffer);
        std::cout.rdbuf(buffer);
        std::cerr << "[CinterForgeTerminal] Windows native guest log opened\n";
    }
}
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

    // Native diagnostics must survive Python startup failure and ordinary
    // process exit. Tee both streams; do not take Xcode/build progress away.
    class NativeTee : public std::streambuf
    {
    public:
        NativeTee(std::streambuf *console, const fs::path &file)
            : console_(console), log_(file, std::ios::binary | std::ios::app) {}
        bool ready() const { return log_.is_open(); }
    protected:
        int_type overflow(int_type ch) override {
            if (traits_type::eq_int_type(ch, traits_type::eof())) return traits_type::not_eof(ch);
            std::lock_guard<std::mutex> guard(lock_);
            auto c = traits_type::to_char_type(ch);
            if (console_) console_->sputc(c);
            log_.put(c);
            if (c == '\n') { if (console_) console_->pubsync(); log_.flush(); }
            return ch;
        }
        std::streamsize xsputn(const char *text, std::streamsize size) override {
            std::lock_guard<std::mutex> guard(lock_);
            if (console_) console_->sputn(text, size);
            log_.write(text, size);
            if (console_) console_->pubsync();
            log_.flush();
            return size;
        }
        int sync() override {
            std::lock_guard<std::mutex> guard(lock_);
            if (console_) console_->pubsync();
            log_.flush();
            return log_ ? 0 : -1;
        }
    private:
        std::mutex lock_;
        std::streambuf *console_;
        std::ofstream log_;
    };

    fs::path nativeLogRoot(const cinterforge::ApplicationShellConfig &cfg)
    {
        if (cfg.developer) return fs::path(cfg.builds) / "Logs/CinterForgeTerminal";
        // An installed app can reside in any selected Steam library.
        const fs::path app = currentExecutable();
        for (auto candidate = app.parent_path(); candidate != candidate.root_path();
             candidate = candidate.parent_path())
            if (candidate.filename() == "steamapps")
                return candidate / "common/CinterForge/Logs/CinterForgeTerminal";
        if (const char *selected = std::getenv("CINTER_STEAMAPPS")) {
            const fs::path steam(selected);
            if (steam.filename() == "steamapps" && fs::is_directory(steam) && !fs::is_symlink(steam))
                return steam / "common/CinterForge/Logs/CinterForgeTerminal";
        }
        // A separately downloaded DMG may be outside the Steam library. Use
        // the already-existing default Steam library only if unambiguous.
        if (const char *home = std::getenv("HOME")) {
            const fs::path steam = fs::path(home) / "Library/Application Support/Steam/steamapps";
            if (fs::is_directory(steam) && !fs::is_symlink(steam) &&
                fs::is_directory(steam / "common/CinterForge"))
                return steam / "common/CinterForge/Logs/CinterForgeTerminal";
        }
        // A direct-download DMG is fully usable without the Steam client.
        // Match the Python installed runtime's standard macOS Logs location.
        if (const char *home = std::getenv("HOME")) {
            const fs::path user(home);
            if (user.is_absolute())
                return user / "Library/Logs/CinterForgeTerminal";
        }
        return {};
    }

    void beginNativeLogging(const cinterforge::ApplicationShellConfig &cfg)
    {
        const fs::path root = nativeLogRoot(cfg);
        if (root.empty()) return;
        const fs::path folder = root / "Native";
        if (fs::is_symlink(root) || fs::is_symlink(folder) ||
            (fs::exists(folder) && !fs::is_directory(folder)))
            throw std::runtime_error("Unsafe native log folder: " + folder.string());
        fs::create_directories(folder);
        const fs::path runtimeFolder = root / "Runtime";
        if (fs::is_symlink(runtimeFolder) ||
            (fs::exists(runtimeFolder) && !fs::is_directory(runtimeFolder)))
            throw std::runtime_error("Unsafe Python worker log folder: " + runtimeFolder.string());
        fs::create_directories(runtimeFolder);
        const std::time_t now = std::time(nullptr);
        std::string stamp = std::to_string(static_cast<long long>(now));
        const fs::path logfile = folder / ("launch-" + stamp + "-" +
                               std::to_string(static_cast<long long>(getpid())) + ".log");
        // Lives for the process lifetime: the original streambuf must remain
        // valid through destruction of the native window and Python Host.
        auto *errors = new NativeTee(std::cerr.rdbuf(), logfile);
        auto *output = new NativeTee(std::cout.rdbuf(), logfile);
        if (!errors->ready() || !output->ready())
            throw std::runtime_error("Cannot open native log: " + logfile.string());
        std::cerr.rdbuf(errors);
        std::cout.rdbuf(output);
        std::cerr << "[CinterForgeTerminal] Native launch log: " << logfile << '\n';
        const fs::path workerLog = runtimeFolder / ("python-worker-" + stamp + "-" +
                                       std::to_string(static_cast<long long>(getpid())) + ".log");
        if (setenv("CINTER_PYTHON_WORKER_STDERR_LOG", workerLog.string().c_str(), 1))
            throw std::runtime_error("Cannot configure Python worker diagnostics");
        std::cerr << "[CinterForgeTerminal] Python worker stderr log: " << workerLog << '\n';
    }

    void useIfUnset(const char *name, const std::string &value)
    {
        if (!std::getenv(name) && !value.empty())
            setenv(name, value.c_str(), 0);
    }

    void fillMacAppDefaults(cinterforge::ApplicationShellConfig &cfg)
    {
        const fs::path app = currentExecutable().parent_path().parent_path().parent_path();
        // Xcode's products are beneath <repo>/../Builds/Xcode/Products.
        // Build & Run must use that developer Builds tree automatically,
        // without an argument entered in Scheme or a Steam installation.
        for (fs::path parent = app.parent_path(); parent != parent.root_path();
             parent = parent.parent_path())
        {
            if (parent.filename() == "Products" &&
                parent.parent_path().filename() == "Xcode" &&
                parent.parent_path().parent_path().filename() == "Builds")
            {
                cfg.developer = true;
                break;
            }
        }
        const fs::path embedded = app / "Contents/Resources/Builds";
        fs::path builds;
        if (cfg.developer)
        {
            // Xcode Debug products live beneath <repo>/../Builds/Xcode/Products.
            // Do not use the signed app's embedded Resources/Builds as a
            // writable development tree: this sent Xcode server/VM logs to
            // the installed-app paths and prevented reproducing guest errors.
            if (!cfg.builds.empty())
                builds = cfg.builds;
            else
            {
                fs::path candidate = app;
                while (candidate != candidate.root_path() && candidate.filename() != "Builds")
                    candidate = candidate.parent_path();
                if (candidate.filename() != "Builds")
                    throw std::runtime_error(
                        "Developer app is not under sibling Builds; pass --builds <repo>/../Builds");
                builds = candidate;
            }
            builds = fs::weakly_canonical(builds);
            if (builds.filename() != "Builds" || !fs::is_directory(builds) ||
                std::any_of(builds.begin(), builds.end(), [](const fs::path &part) {
                    return part.extension() == ".app";
                }))
                throw std::runtime_error("Unsafe Xcode developer Builds directory: " + builds.string());
        }
        else if (!cfg.builds.empty())
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
        {
            setenv("CINTER_BUILDS_ROOT", cfg.builds.c_str(), 1);
            setenv("CINTER_TERMINAL_RUNTIME_MODE", "developer", 1);
            std::cerr << "[CinterForgeTerminal] Xcode/developer runtime: " << cfg.builds
                      << "; Server logs: " << (builds / "Logs/CinterForgeTerminal/Runtime").string() << '\n';
        }
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
                             "--service <server.py> --builds <Builds> [--title name] [--developer] [--smoke-js] [--accept-ui] [--connect http://LAN-IP:20300/]\n";
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
            else if (flag == "--connect")
                cfg.connect_url = value;
            else
                throw std::invalid_argument("Unknown option: " + flag);
        }
#ifdef __APPLE__
        fillMacAppDefaults(cfg);
        beginNativeLogging(cfg);
#elif defined(_WIN32) || defined(__linux__)
        fillPackagedDefaults(cfg);
#ifdef _WIN32
        beginWindowsGuestLogging();
#endif
#endif
        return cinterforge::runApplicationShell(cfg);
    }
    catch (const std::exception &e)
    {
        std::cerr << "[CinterForgeTerminal] FAIL: " << e.what() << '\n';
#ifdef __APPLE__
        // Finder/LaunchServices launches have no visible stderr. If runtime
        // discovery fails before beginNativeLogging(), the error otherwise
        // disappears entirely on a consumer Mac (without any Steam logs).
        // Do not create a second installed-app log directory outside Steam.
        syslog(LOG_ERR, "CinterForgeTerminal startup failed: %s", e.what());
        if (!cfg.accept_ui) {
            CFStringRef details = CFStringCreateWithCString(
                kCFAllocatorDefault, e.what(), kCFStringEncodingUTF8);
            if (details) {
                CFUserNotificationDisplayAlert(
                    0, kCFUserNotificationStopAlertLevel,
                    nullptr, nullptr, nullptr,
                    CFSTR("CinterForge Terminal could not start"), details,
                    CFSTR("OK"), nullptr, nullptr, nullptr);
                CFRelease(details);
            }
        }
#endif
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
