#pragma once

#include <string>

namespace cinterforge
{

    // One linked C++ application shell API for macOS, Linux, and Windows.
    // The Python HTTP service, worker and assets must be staged by the producer.
    struct ApplicationShellConfig
    {
        std::string python;  // The packaged Python interpreter.
        std::string worker;  // The staged Python Host JSONL worker.
        std::string service; // The staged application's HTTP service module.
        std::string builds;  // Absolute shared sibling Builds root.
        std::string title = "CinterForge Terminal";
        std::string connect_url; // Remote LAN WebView (no second local server).
        int width = 1080;
        int height = 720;
        bool developer = false;
        bool accept_ui = false; // Native acceptance of the real staged terminal and museum UI.
    };

    // Real WebView JS -> native callback acceptance (requires a usable desktop display).
    int smokeWebViewJavaScript();

    // Owns the managed Python service and native WebView from start to shutdown.
    // Returns only when the window closes; throws if the service cannot become ready.
    int runApplicationShell(const ApplicationShellConfig &config);

} // namespace cinterforge
