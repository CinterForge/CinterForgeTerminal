// Exactly one C++ example for macOS, Linux and Windows.  Compile against
// upstream webview/webview; the browser engine is supplied by each OS.
#ifndef CINTER_BOOTSTRAP_CONSOLE_ONLY
#include "webview/webview.h"
#endif
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <regex>
#include <string>
#include <thread>

namespace fs = std::filesystem;

int main(int argc, char **argv) {
    const std::string mode = argc > 1 ? argv[1] : "--probe";
    if (mode == "--probe") {
#if defined(_WIN32)
        const char *platform = "windows";
#elif defined(__APPLE__)
        const char *platform = "macos";
#else
        const char *platform = "linux";
#endif
        std::cout << "CINTER_SAMPLE_BEGIN platform=" << platform << '\n';
        std::cout << "CINTER_SAMPLE_CWD " << fs::current_path().string() << '\n';
#if defined(_WIN32)
        // Wine 8 can fail on cmd.exe's volume query for mounted drives even
        // when the Windows filesystem APIs work. The directory_iterator below
        // enumerates the actual directory through those APIs and is checked
        // against a marker created by Setup; do not use cmd.exe as a gate.
        std::cout << "CINTER_SAMPLE_NATIVE_DIRECTORY_ENUMERATION win32\n" << std::flush;
#else
        std::cout << "CINTER_SAMPLE_NATIVE_COMMAND /bin/bash -c pwd; ls -la\n" << std::flush;
        if (std::system("/bin/bash -c 'pwd; ls -la'") != 0) return 3;
#endif
        for (const auto &item : fs::directory_iterator(fs::current_path()))
            std::cout << "CINTER_SAMPLE_DIR " << item.path().filename().string() << '\n';
        std::cout << "CINTER_SAMPLE_DIRECTORY_PASS platform=" << platform << std::endl;
        return 0;
    }
#ifdef CINTER_BOOTSTRAP_CONSOLE_ONLY
    std::cerr << "JS smoke requires the actual upstream WebView build\n";
    return 2;
#else
    if (mode != "--smoke-js") {
        std::cerr << "Usage: CinterBootstrapSample --probe|--smoke-js\n";
        return 2;
    }
    try {
        webview::webview view(false, nullptr);
        view.set_title("CinterForgeTerminal — shared WebView sample");
        view.set_size(420, 220, WEBVIEW_HINT_NONE);
        std::atomic<bool> valid{false};
        std::atomic<bool> finished{false};
        view.bind("cinterSample", [&](const std::string &args) -> std::string {
            bool good = std::regex_search(args, std::regex(R"re("result"\s*:\s*4([^0-9]|$))re"));
            valid.store(good);
            std::cout << (good ? "CINTER_SAMPLE_WEBVIEW_JS_PASS " : "CINTER_SAMPLE_WEBVIEW_JS_FAIL ")
                      << args << std::endl;
            view.dispatch([&view] { view.terminate(); });
            return good ? R"("ok")" : R"("error")";
        });
        view.init(R"js(window.addEventListener('DOMContentLoaded', () => {
          console.log('CinterForge sample JS OK');
          window.cinterSample({result: 2 + 2, platform: navigator.platform});
        }));)js");
        view.set_html("<!doctype html><html><body>CinterForgeTerminal WebView JS acceptance</body></html>");
        std::thread watchdog([&]() {
            for (int i = 0; i < 150 && !finished.load(); ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            if (!finished.load()) view.dispatch([&view] { view.terminate(); });
        });
        try {
            view.run();
        } catch (...) {
            finished.store(true);
            watchdog.join();
            throw;
        }
        finished.store(true);
        watchdog.join();
        if (!valid.load()) {
            std::cerr << "CINTER_SAMPLE_WEBVIEW_JS_FAIL no valid native callback\n";
            return 1;
        }
        return 0;
    } catch (const std::exception &e) {
        std::cerr << "CINTER_SAMPLE_WEBVIEW_JS_FAIL " << e.what() << '\n';
        return 1;
    }
#endif
}
