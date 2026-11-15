/*
 * CinterForgeTerminal.cpp — native application owner and WebView lifecycle.
 * PURPOSE: Own exactly one PythonHost worker, authenticated HTTP start/stop,
 * WebView creation, native callback authorization, debugger dispatch,
 * connection health monitoring, service reboot and orderly VM shutdown.
 * METHODS: smokeWebViewJavaScript, runApplicationShell.
 * STARTUP: Called by CinterTerminalMain.cpp; resolves staged worker and
 * waits for Python service readiness before creating/navigating WebView.
 * SHUTDOWN: Stop owned runtime and worker, never kill an attached service.
 * PLATFORM: CinterForgePlatformMenus.cpp owns OS menu, clipboard, spawning
 * and native inspector implementations; no duplicate OS implementations.
 */
#include "CinterApplicationShell.h"
#include "CinterForgeApplicationPaths.h"
#include "CinterForgePythonServiceHandshake.h"
#include "CinterPythonHost.h"
#include "webview/webview.h"

#include <atomic>
#include <chrono>
#include <cerrno>
#include <cstdlib>
#include <functional>
#include <memory>
#include <system_error>
#include <vector>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#ifdef __linux__
#include <gtk/gtk.h>
#if GTK_MAJOR_VERSION >= 4
#include <webkit/webkit.h>
#else
#include <webkit2/webkit2.h>
#endif
#endif
#include <spawn.h>
extern char **environ;
#ifdef __APPLE__
#include <mach-o/dyld.h>
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <objc/message.h>
#endif
#endif
#include <filesystem>
#include <iostream>
#include <regex>
#include <stdexcept>
#include <string>
#include <thread>
#include "CinterForgePlatformMenus.h"

namespace cinterforge {
    namespace fs = std::filesystem;
    int smokeWebViewJavaScript()
    {
        // Do not mistake linking WebView for a working browser. This creates the
        // actual native window, executes JS in the OS browser engine, then accepts
        // a native callback with an explicitly checked arithmetic result.
        webview::webview window(false, nullptr);
        window.set_title("CinterForgeTerminal JavaScript acceptance");
        window.set_size(460, 240, WEBVIEW_HINT_NONE);
        std::atomic<bool> callback{false};
        std::atomic<bool> completed{false};
        window.bind("cinterJsSmoke", [&](const std::string &arguments) -> std::string
                    {
        // The pinned 0.12.0 synchronous binding receives a JSON argument array.
        // A callback that does not carry the expected result is a hard failure.
        const bool correct = std::regex_search(arguments,
            std::regex(R"re("result"\s*:\s*4([^0-9]|$))re"));
        callback.store(correct);
        if (correct) {
            std::cout << "WEBVIEW_JS_PASS result=4 callback=native platform="
                      << arguments << std::endl;
        } else {
            std::cerr << "WEBVIEW_JS_FAIL unexpected callback: " << arguments << '\n';
        }
        // WebView callbacks are not guaranteed to execute on the GUI thread.
        // Request termination through upstream WebView's main-thread dispatcher.
        window.dispatch([&window] { window.terminate(); });
        return correct ? R"("passed")" : R"("failed")"; });
        window.init(R"js(
      window.addEventListener('DOMContentLoaded', function() {
        console.log('CinterForge JavaScript OK');
        window.cinterJsSmoke({result: 2 + 2, platform: navigator.platform});
      });
    )js");
        window.set_html("<!doctype html><html><body>CinterForgeTerminal JavaScript acceptance</body></html>");
        std::thread watchdog([&]
                             {
        for (int i = 0; i < 120 && !completed.load(); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (!completed.load()) window.dispatch([&window] { window.terminate(); }); });
        try
        {
            window.run();
        }
        catch (...)
        {
            completed.store(true);
            watchdog.join();
            throw;
        }
        completed.store(true);
        watchdog.join();
        if (!callback.load())
            throw std::runtime_error("WEBVIEW_JS_FAIL: native callback missing or invalid");
        return 0;
    }

    int runApplicationShell(const ApplicationShellConfig &config)
    {
        CinterForgeApplicationPaths::validateStaged(config);
        // The authenticated terminal's `debugger` command must work without a
        // separate environment flag supplied by the user at launch time.
#ifdef _WIN32
        if (_putenv_s("CPH_ENABLE_DEBUGGER", "1"))
            throw std::runtime_error("Cannot enable native Terminal debugger");
#else
        if (setenv("CPH_ENABLE_DEBUGGER", "1", 1))
            throw std::runtime_error("Cannot enable native Terminal debugger");
#endif
        if (!config.connect_url.empty() && !CinterForgeApplicationPaths::validLanUrl(config.connect_url))
            throw std::invalid_argument("CinterForge LAN service requires a private IPv4 address on HTTP port 20300-20399");
        auto host = std::make_unique<PythonHost>();
        bool started = false;
        bool hostRunning = false;
        bool runtimeOwner = false;
        std::atomic<bool> relaunchRequested{false};
        try
        {
            std::string url = config.connect_url;
            std::string navigationUrl = url;
            std::string activeService = config.service;
            // This machine's own advertised loopback service is still a native
            // launch; use the existing private grant instead of browser login.
            const bool ownLoopback = url == "http://127.0.0.1:20300/" ||
                                     url == "http://127.0.0.1:20300";
            if (url.empty() || ownLoopback) {
                // The cross-platform Windows process runs under Wine on
                // emulated AMD64 only when the managed launcher explicitly
                // sets this flag. The normal native startup budget stays 10s.
                // Under Wine, Python first import/Win32 process creation can
                // take substantially longer without being a broken worker.
                bool wineGuest = false;
#ifdef _WIN32
                const char *wineFlag = std::getenv("CINTER_WINE_GUEST");
                wineGuest = wineFlag != nullptr && std::string(wineFlag) == "1";
#endif
                try {
                    host->start(config.python, config.worker, config.service,
                                std::chrono::seconds(wineGuest ? 90 : 10));
                } catch (const std::exception &e) {
                    if (wineGuest)
                        throw std::runtime_error(std::string("Wine guest Python worker startup failed after a 90s budget: ") + e.what());
                    throw;
                }
                hostRunning = true;
                const std::string reply = host->call("start", "{\"builds\":\"" + CinterForgeApplicationPaths::jsonEscape(config.builds) + "\"}",
                                                    std::chrono::seconds(wineGuest ? 60 : 15));
                url = CinterForgePythonServiceHandshake::readyUrl(reply);
                navigationUrl = CinterForgePythonServiceHandshake::nativeBootstrapUrl(reply, url);
                runtimeOwner = reply.find("\"attached\": true") == std::string::npos &&
                               reply.find("\"attached\":true") == std::string::npos;
                started = true;
            }
            std::cerr << "[CinterForgeTerminal] managed HTTP service ready: " << url << '\n';
            // One cross-platform webview/webview implementation; platform browser
            // engines are provided by the OS rather than bundled with the product.
#ifdef _WIN32
            std::cerr << "[CinterForgeTerminal] WebView2 constructor starting\n";
#endif
            webview::webview window(true, nullptr); // Native Inspector is a Terminal command, not a build flag.
#ifdef _WIN32
            std::cerr << "[CinterForgeTerminal] WebView2 constructor returned\n";
            auto *controller = static_cast<ICoreWebView2Controller *>(
                webview_get_native_handle(static_cast<webview_t>(&window),
                                          WEBVIEW_NATIVE_HANDLE_KIND_BROWSER_CONTROLLER));
            if (!controller) {
                std::cerr << "[CinterForgeTerminal] WebView2 controller FAILED: native handle unavailable\n";
            } else {
                ICoreWebView2 *browser = nullptr;
                const HRESULT result = controller->get_CoreWebView2(&browser);
                if (FAILED(result) || !browser) {
                    std::cerr << "[CinterForgeTerminal] WebView2 controller FAILED: HRESULT="
                              << static_cast<unsigned long>(result) << "\n";
                } else {
                    std::cerr << "[CinterForgeTerminal] WebView2 controller READY\n";
                    browser->Release();
                }
            }
#endif
#ifdef __APPLE__
            // `inspectable` must be set before the page starts navigating, or the
            // app may never appear in Safari's Develop > Applications listing.
            if (auto *native = webview_get_native_handle(static_cast<webview_t>(&window),
                                                         WEBVIEW_NATIVE_HANDLE_KIND_BROWSER_CONTROLLER))
            {
                WKWebView *browser = (WKWebView *)native;
                if (@available(macOS 13.3, *))
                    browser.inspectable = YES;
            }
#endif
            window.set_title(config.title);
            window.set_size(config.width, config.height, WEBVIEW_HINT_NONE);
#ifdef __APPLE__
            CinterForgePlatformMenus::installMacFileMenu(window, config);
#elif defined(__linux__) && GTK_MAJOR_VERSION < 4
            // GTK 3 desktop/taskbar window icon; GTK 4 uses the generated .desktop
            // application's Icon/StartupWMClass entry instead.
            const fs::path linuxIcon = fs::read_symlink("/proc/self/exe").parent_path() / "AppIcon.png";
            GError *iconError = nullptr;
            if (!gtk_window_set_default_icon_from_file(linuxIcon.string().c_str(), &iconError))
            {
                std::cerr << "[CinterForgeTerminal] Linux window icon: "
                          << (iconError ? iconError->message : "AppIcon.png missing") << '\n';
            }
            if (iconError)
                g_error_free(iconError);
#endif
            // Bonjour-discovered CinterForge services open a new native window
            // at the advertised IP and HTTP port, without replacing the current one.
            window.bind("cinterConnect", [&](const std::string &args) -> std::string
                        {
                std::smatch match;
                if (!std::regex_search(args, match, std::regex(R"re("url"\s*:\s*"(http://[^"\\]+)")re")))
                    return R"("invalid LAN URL")";
                const std::string remoteUrl = match[1].str();
                if (!CinterForgeApplicationPaths::validLanUrl(remoteUrl)) return R"("invalid LAN URL")";
                auto remote = config;
                remote.connect_url = remoteUrl;
                remote.title = "CinterForge Terminal — " + remoteUrl;
                CinterForgePlatformMenus::spawnNewTerminal(remote);
                return R"("window-opened")";
            });
            // Real File commands create a complete independent native Terminal or
            // shut down this WebView. Invalid actions receive no privilege.
            window.bind("cinterWindow", [&](const std::string &args) -> std::string
                        {
            if (std::regex_search(args,std::regex(R"re("action"\s*:\s*"new")re"))) {
                window.dispatch([&window] {
                    window.eval("window.CinterTerminalNewTab && window.CinterTerminalNewTab()");
                });
                return R"("tab-opened")";
            }
            if (std::regex_search(args,std::regex(R"re("action"\s*:\s*"relaunch")re"))) {
                relaunchRequested.store(true);
                window.dispatch([&window] { window.terminate(); });
                return R"("relaunching")";
            }
            if (std::regex_search(args,std::regex(R"re("action"\s*:\s*"quit")re"))) {
                window.dispatch([&window] { window.terminate(); });
                return R"("closing")";
            }
            return R"("invalid action")"; });
#ifdef __APPLE__
            // Browser-origin permissions do not grant navigator.clipboard access
            // reliably in WKWebView. This bridge stays local to the native app
            // and uses the same NSPasteboard as the real macOS Edit menu.
            window.bind("cinterClipboard", [&](const std::string &args) -> std::string
                        {
                NSData *bytes = [NSData dataWithBytes:args.data() length:args.size()];
                id parsed = [NSJSONSerialization JSONObjectWithData:bytes options:0 error:nil];
                NSDictionary *request = [parsed isKindOfClass:[NSArray class]] && [parsed count]
                    ? [parsed firstObject] : parsed;
                if (![request isKindOfClass:[NSDictionary class]]) return "null";
                NSString *action = request[@"action"];
                NSPasteboard *board = [NSPasteboard generalPasteboard];
                if ([action isEqualToString:@"paste"]) {
                    NSString *value = [board stringForType:NSPasteboardTypeString] ?: @"";
                    return "\"" + CinterForgeApplicationPaths::jsonEscape([value UTF8String] ?: "") + "\"";
                }
                if ([action isEqualToString:@"copy"]) {
                    NSString *value = request[@"text"];
                    if (![value isKindOfClass:[NSString class]]) return "false";
                    [board clearContents];
                    return [board setString:value forType:NSPasteboardTypeString] ? "true" : "false";
                }
                return "null";
            });
#endif
#ifdef _WIN32
            // The UI's File/Edit and Ctrl+V use the actual Windows clipboard.
            // Read-only, no cross-origin network API and no text in native logs.
            window.bind("cinterClipboard", [&](const std::string &args) -> std::string
                        {
            if (args.find("\"action\":\"paste\"") == std::string::npos)
                return "null";
            return "\"" + CinterForgeApplicationPaths::jsonEscape(CinterForgePlatformMenus::windowsClipboardText()) + "\""; });
#endif
            // Native bridge on the real staged application (not a synthetic smoke page).
            // This binding accepts telemetry only; privileged terminal commands continue
            // through the authenticated, loopback-only Python service.
            std::atomic<bool> ready{false};
            std::atomic<bool> museum{false};
            std::atomic<bool> returned{false};
            std::atomic<bool> failed{false};
            std::atomic<bool> finished{false};
            // Log native menu transitions, never identical browser heartbeat events.
            std::atomic<int> lastLoggedCodeMenuMode{-1};
            window.bind("cinterNative", [&](const std::string &args) -> std::string
                        {
#ifdef __APPLE__
            if (args.find("\"event\":\"minecraft-menu\"") != std::string::npos) {
                const bool enabled=args.find("\"enabled\":true") != std::string::npos;
                window.dispatch([enabled] {
                    NSMenu *terminal=[[[NSApp mainMenu] itemWithTitle:@"Terminal"] submenu];
                    for (NSMenuItem *item in [terminal itemArray])
                        if ([[item representedObject] isEqual:@"terminal:minecraft"])
                            [item setEnabled:enabled];
                });
            }
#endif
#ifdef __APPLE__
            // code-menu-mode has no Cocoa action: the real menu state is
            // provided by code-menu-model. Do not queue a no-op UI dispatch.
            if (args.find("\"event\":\"code-menu-model\"") != std::string::npos) {
                const std::string payload = args;
                window.dispatch([payload] {
                    NSData *data = [NSData dataWithBytes:payload.data() length:payload.size()];
                    id json = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
                    NSDictionary *object = [json isKindOfClass:[NSArray class]] && [json count]
                        ? [json firstObject] : json;
                    NSString *name = [object isKindOfClass:[NSDictionary class]] ? object[@"name"] : nil;
                    NSArray *items = [object isKindOfClass:[NSDictionary class]] ? object[@"items"] : nil;
                    if (![name isKindOfClass:[NSString class]] || !name.length) return;
                    NSMenu *menu = [[NSApp.mainMenu itemWithTitle:name] submenu];
                    if (!menu || ![items isKindOfClass:[NSArray class]]) return;
                    [menu removeAllItems];
                    CinterForgeFileMenuTarget *target = (CinterForgeFileMenuTarget *)
                        [[[NSApp.mainMenu itemWithTitle:@"LAN"] submenu] itemAtIndex:0].target;
                    // Reproduce Monaco submenus recursively; all leaf commands
                    // route back to the Code workbench, never native substitutes.
                    std::function<void(NSMenu *, NSArray *, int)> fill =
                        [&](NSMenu *destination, NSArray *values, int depth) {
                        if (depth > 4 || values.count > 120) return;
                        for (id item in values) {
                            if (![item isKindOfClass:[NSDictionary class]]) continue;
                            // Separator entries come directly from Code-OSS's
                            // IMenuService. Do not treat them as commands.
                            if ([item[@"separator"] respondsToSelector:@selector(boolValue)] &&
                                [item[@"separator"] boolValue]) {
                                [destination addItem:[NSMenuItem separatorItem]];
                                continue;
                            }
                            NSString *title = item[@"title"], *action = item[@"action"];
                            if (![title isKindOfClass:[NSString class]] || !title.length ||
                                ![action isKindOfClass:[NSString class]] || !action.length) continue;
                            NSMenuItem *entry = [destination addItemWithTitle:title
                                action:@selector(dispatchCodeMenu:) keyEquivalent:@""];
                            [entry setRepresentedObject:action];
                            [entry setTarget:target];
                            // The live menu is replaced during tab switches. Route
                            // Terminal's Edit actions to their actual native handlers;
                            // Code-OSS's code:* actions continue to use its menu service.
                            if ([name isEqualToString:@"Edit"] && depth == 0) {
                                if ([title isEqualToString:@"Cut"]) {
                                    // Code-OSS loses editor focus while the
                                    // native menu is open. A sampled IAction
                                    // can remain disabled even with a live
                                    // selection. Use Cocoa's WKWebView editor
                                    // responder chain instead of that snapshot.
                                    [entry setAction:@selector(cut:)];
                                    [entry setTarget:nil];
                                    [entry setKeyEquivalent:@"x"];
                                } else if ([title isEqualToString:@"Copy"]) {
                                    if ([action isEqualToString:@"edit:copy"])
                                        [entry setAction:@selector(copyTerminal:)];
                                    [entry setKeyEquivalent:@"c"];
                                } else if ([title isEqualToString:@"Paste"]) {
                                    if ([action isEqualToString:@"edit:paste"])
                                        [entry setAction:@selector(pasteTerminal:)];
                                    [entry setKeyEquivalent:@"v"];
                                }
                            }
                            [entry setEnabled:([name isEqualToString:@"Edit"] && depth == 0 &&
                                [title isEqualToString:@"Cut"]) ||
                                ![item[@"enabled"] respondsToSelector:@selector(boolValue)] ||
                                [item[@"enabled"] boolValue]];
                            NSArray *children = item[@"children"];
                            if ([children isKindOfClass:[NSArray class]] && children.count) {
                                NSMenu *sub = [[NSMenu alloc] initWithTitle:title];
                                [entry setSubmenu:sub];
                                fill(sub, children, depth + 1);
                            }
                        }
                    };
                    fill(menu, items, 0);
                });
            }
            if (args.find("\"event\":\"lan-peers\"") != std::string::npos) {
                // Parse only after dispatch: no autoreleased Foundation object
                // crosses the native WebView thread boundary.
                const std::string payload = args;
                window.dispatch([payload] {
                    NSData *encoded = [NSData dataWithBytes:payload.data() length:payload.size()];
                    id parsed = [NSJSONSerialization JSONObjectWithData:encoded options:0 error:nil];
                    NSDictionary *detail = [parsed isKindOfClass:[NSArray class]] && [parsed count] ? [parsed firstObject] : parsed;
                    NSArray *peers = [detail isKindOfClass:[NSDictionary class]] ? detail[@"peers"] : nil;
                    NSMenu *list = [[[NSApp mainMenu] itemWithTitle:@"LAN"] submenu];
                    if (!list) return;
                    while ([list numberOfItems] > 1) [list removeItemAtIndex:1];
                    CinterForgeFileMenuTarget *target = (CinterForgeFileMenuTarget *)[[list itemAtIndex:0] target];
                    for (id peer in peers) {
                        if (![peer isKindOfClass:[NSDictionary class]]) continue;
                        NSString *url = peer[@"url"];
                        NSString *name = peer[@"name"];
                        if (![url isKindOfClass:[NSString class]] || !CinterForgeApplicationPaths::validLanUrl([url UTF8String])) continue;
                        NSMenuItem *item = [list addItemWithTitle:[NSString stringWithFormat:@"%@ — %@", name ?: @"Terminal", url]
                                                         action:@selector(newTerminal:) keyEquivalent:@""];
                        [item setRepresentedObject:[@"lan:" stringByAppendingString:url]];
                        [item setTarget:target];
                    }
                });
            }
#endif
            const bool isReady = args.find("\"event\":\"ready\"") != std::string::npos;
#ifdef _WIN32
            if (isReady) std::cerr << "[CinterForgeTerminal] WebView2 page-ready JS callback received\n";
#endif
            const bool isMuseum = args.find("\"event\":\"machine\"") != std::string::npos &&
                args.find("\"id\":\"mac-plus\"") != std::string::npos &&
                args.find("\"kind\":\"emulator\"") != std::string::npos;
            const bool isTerminal = args.find("\"event\":\"terminal\"") != std::string::npos &&
                args.find("\"id\":\"vt100-terminal\"") != std::string::npos;
            const bool isError = args.find("\"event\":\"error\"") != std::string::npos;
            if (isReady) ready.store(true);
            if (isMuseum) museum.store(true);
            if (isTerminal) returned.store(true);
            if (isError) failed.store(true);
            const bool codeMenuMode = args.find("\"event\":\"code-menu-mode\"") != std::string::npos;
            bool changedCodeMenuMode = true;
            if (codeMenuMode) {
                const int mode = args.find("\"enabled\":true") != std::string::npos ? 1 : 0;
                changedCodeMenuMode = lastLoggedCodeMenuMode.exchange(mode) != mode;
            }
            if (args.find("\"event\":\"code-menu-model\"") == std::string::npos &&
                args.find("\"event\":\"lan-peers\"") == std::string::npos &&
                (!codeMenuMode || changedCodeMenuMode))
                std::cerr << "[CinterForgeTerminal] UI event: " << args << '\n';
            if (config.accept_ui && (isError || (ready.load() && museum.load() && returned.load())))
                window.dispatch([&window] { window.terminate(); });
            return R"("ok")"; });
            // The acceptance flag is part of the real loopback page navigation.
            // Unlike WebView init scripts, URL parameters have consistent timing
            // on WKWebView, WebKitGTK and WebView2. It grants no host privileges.
#ifdef _WIN32
            // Never log native-bootstrap URLs: they include authentication grants.
            std::cerr << "[CinterForgeTerminal] WebView2 navigation requested (URL redacted)\n";
#endif
            window.navigate(config.accept_ui
                ? navigationUrl + (navigationUrl.find('?') == std::string::npos ? "?" : "&") + "accept-ui=1"
                : navigationUrl);
            std::thread watchdog;
            if (config.accept_ui)
            {
                watchdog = std::thread([&]
                                       {
                for (int i = 0; i < 200 && !finished.load(); ++i)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                if (!finished.load()) window.dispatch([&window] { window.terminate(); }); });
            }

            std::atomic<bool> checking{true};
            std::thread inspector;
            if (started) inspector = std::thread([&]
                                  {
            while (checking.load()) {
                try {
                    const auto response = host->call("next_debugger_request", "{}", std::chrono::seconds(3));
                    if (response.find("\"requested\": true") != std::string::npos ||
                        response.find("\"requested\":true") != std::string::npos) {
                        window.dispatch([&window] { CinterForgePlatformMenus::openNativeInspector(window); });
                    }
                    // The HTTP server and Colima share this native owner's
                    // lifetime. If the Python listener dies while the WebView
                    // stays open, quit the app so normal cleanup and the
                    // independent crash watchdog can release the VM as well.
                    if (runtimeOwner) {
                        // An attached second native window has no HTTP server
                        // in *its* Python worker; only the owning window can
                        // query this service's local health and shut it down.
                        const auto health = host->call("service_health", "{}", std::chrono::seconds(3));
                        if (health.find("\"alive\": true") == std::string::npos &&
                            health.find("\"alive\":true") == std::string::npos) {
                            std::cerr << "[CinterForgeTerminal] HTTP listener exited; terminating its native owner\n";
                            window.dispatch([&window] { window.terminate(); });
                            break;
                        }
                    }
                    // Reboot is a strictly atomic runtime lifecycle request:
                    // no build, Setup, Stage, Publish, ZIP or UnZIP. Prepare
                    // the replacement Python worker BEFORE dropping the live
                    // HTTP server. Keep the old worker to roll back if the new
                    // service cannot bind or pass readiness.
                    const auto reboot = host->call("next_lifecycle_reboot_request", "{}",
                                                  std::chrono::seconds(3));
                    if (reboot.find("\"component\": \"app\"") != std::string::npos ||
                        reboot.find("\"component\":\"app\"") != std::string::npos) {
                        relaunchRequested.store(true);
                        window.dispatch([&window] { window.terminate(); });
                        break;
                    }
                    if (reboot.find("\"component\": \"server\"") != std::string::npos ||
                        reboot.find("\"component\":\"server\"") != std::string::npos) {
                        // The server may select the authored webapp service
                        // for developer-only hot reload. The native executable,
                        // Python runtime and worker remain the already-built
                        // artifacts. Installed apps never receive this path.
                        std::smatch liveSource;
                        if (std::regex_search(reboot, liveSource,
                                              std::regex(R"re("service"\s*:\s*"([^"\\]+)")re")) &&
                            !liveSource[1].str().empty())
                            activeService = liveSource[1].str();
                        auto replacement = std::make_unique<PythonHost>();
                        replacement->start(config.python, config.worker, activeService,
                                           std::chrono::seconds(10));
                        const std::string startArgs = "{\"builds\":\"" + CinterForgeApplicationPaths::jsonEscape(config.builds) + "\"}";
                        host->call("stop", "{}", std::chrono::seconds(12));
                        try {
                            const std::string ready = replacement->call("start", startArgs,
                                                                          std::chrono::seconds(30));
                            const std::string renewedUrl = CinterForgePythonServiceHandshake::readyUrl(ready);
                            const std::string renewedNavigation = CinterForgePythonServiceHandshake::nativeBootstrapUrl(ready, renewedUrl);
                            host->stop();
                            host = std::move(replacement);
                            window.dispatch([&window, renewedNavigation] {
                                window.navigate(renewedNavigation);
                            });
                            std::cerr << "[CinterForgeTerminal] Python HTTP server and WebView reloaded\n";
                        } catch (...) {
                            // Replacement failed after old listener was stopped:
                            // restore the SAME already-imported Python worker.
                            try { host->call("start", startArgs, std::chrono::seconds(20)); }
                            catch (const std::exception& e) {
                                std::cerr << "[CinterForgeTerminal] Server rollback FAILED: " << e.what() << '\n';
                            }
                            throw;
                        }
                    }
                } catch (const std::exception& e) {
                    if (checking.load()) std::cerr << "[CinterForgeTerminal] inspector check: " << e.what() << '\n';
                    if (checking.load() && !host->running()) {
                        // A crashed service worker cannot accept a normal
                        // shutdown RPC. Exit so the independent VM watchdog
                        // handles the app-owned Colima profile.
                        window.dispatch([&window] { window.terminate(); });
                        break;
                    }
                }
                for (int i = 0; i < 10 && checking.load(); ++i)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
            } });
            try
            {
                window.run();
            }
            catch (...)
            {
                finished.store(true);
                if (watchdog.joinable())
                    watchdog.join();
                checking = false;
                if (inspector.joinable()) inspector.join();
                throw;
            }
            finished.store(true);
            if (watchdog.joinable())
                watchdog.join();
            checking = false;
            if (inspector.joinable()) inspector.join();
            if (config.accept_ui)
            {
                if (!ready.load() || !museum.load() || !returned.load() || failed.load())
                {
                    std::cerr << "TERMINAL_UI_FAIL ready=" << ready.load()
                              << " museum=" << museum.load()
                              << " terminal=" << returned.load()
                              << " error=" << failed.load() << '\n';
                    throw std::runtime_error("TERMINAL_UI_FAIL: real WebView menu/terminal callback missing");
                }
                std::cout << "TERMINAL_UI_PASS ready=1 museum=mac-plus terminal=vt100-terminal callback=native" << std::endl;
            }
            // After every normal Quit, the macOS event loop must return here
            // rather than letting Cocoa terminate the process mid-cleanup.
            std::cerr << "[CinterForgeTerminal] WebView stopped; releasing native-owned resources\n";
            if (started) {
                std::cerr << "[CinterForgeTerminal] Stopping owned HTTP service\n";
                host->call("stop", "{}", std::chrono::seconds(5));
                started = false;
            }
            if (hostRunning && runtimeOwner) {
                std::cerr << "[CinterForgeTerminal] Stopping app-owned guest runtime\n";
                try { host->call("shutdown_runtime", "{}", std::chrono::seconds(125)); }
                catch (const std::exception& e) {
                    // Detached owner watchdog independently retries when the
                    // native app exits, including under Xcode Stop.
                    std::cerr << "[CinterForgeTerminal] Managed guest cleanup: " << e.what() << '\n';
                }
            }
            if (hostRunning) host->stop();
            std::cerr << "[CinterForgeTerminal] Native shutdown completed\n";
            // Only launch the updated executable AFTER the old HTTP host has
            // released 20300. Never run two live owners during replacement.
            if (relaunchRequested.load() && !config.accept_ui && config.connect_url.empty())
                CinterForgePlatformMenus::spawnNewTerminal(config);
            return 0;
        }
        catch (...)
        {
            if (started)
            {
                try
                {
                    host->call("stop", "{}", std::chrono::seconds(3));
                }
                catch (...)
                {
                }
            }
            if (hostRunning && runtimeOwner) {
                try { host->call("shutdown_runtime", "{}", std::chrono::seconds(125)); }
                catch (const std::exception& e) {
                    std::cerr << "[CinterForgeTerminal] Guest cleanup after error: " << e.what() << '\n';
                }
            }
            if (hostRunning) host->stop();
            throw;
        }
    }
} // namespace cinterforge
