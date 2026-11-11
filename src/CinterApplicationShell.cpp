#include "CinterApplicationShell.h"
#include "CinterPythonHost.h"
#include "webview/webview.h"

#include <atomic>
#include <chrono>
#include <cerrno>
#include <cstdlib>
#include <functional>
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

#ifdef __APPLE__
// Genuine macOS File menu, anchored in the one existing WebView window.
// No second app/framework or separate source unit is introduced.
@interface CinterFileMenuTarget : NSObject
{
@public
    std::function<void(const std::string &)> newTerminalAction;
    std::function<void()> quitAction;
    std::function<void()> debuggerAction;
    std::function<void()> copyAction;
    std::function<void()> pasteAction;
    NSMenuItem *minecraftItem;
}
- (void)newTerminal:(id)sender;
- (void)quitTerminal:(id)sender;
- (void)openDebugger:(id)sender;
- (void)copyTerminal:(id)sender;
- (void)pasteTerminal:(id)sender;
@end
@implementation CinterFileMenuTarget
- (void)newTerminal:(id)sender
{
    NSString *kind = [sender representedObject];
    if (newTerminalAction && [kind isKindOfClass:[NSString class]])
        newTerminalAction(std::string([kind UTF8String]));
}
- (void)quitTerminal:(id)sender
{
    (void)sender;
    if (quitAction)
        quitAction();
}
- (void)openDebugger:(id)sender
{
    (void)sender;
    if (debuggerAction)
        debuggerAction();
}
- (void)copyTerminal:(id)sender
{
    (void)sender;
    if (copyAction)
        copyAction();
}
- (void)pasteTerminal:(id)sender
{
    (void)sender;
    if (pasteAction)
        pasteAction();
}
@end
#endif

namespace cinterforge
{
    namespace
    {
        namespace fs = std::filesystem;

        std::string jsonEscape(const std::string &value)
        {
            std::string result;
            for (const unsigned char c : value)
            {
                if (c == '\\' || c == '"')
                {
                    result += '\\';
                    result += static_cast<char>(c);
                }
                else if (c < 32)
                {
                    constexpr char digits[] = "0123456789abcdef";
                    result += "\\u00";
                    result += digits[(c >> 4) & 15];
                    result += digits[c & 15];
                }
                else
                    result += static_cast<char>(c);
            }
            return result;
        }

        // Remote windows accept literal private IPv4 addresses only; no arbitrary
        // URL can be used to load a privileged native-bridge WebView.
        bool validLanUrl(const std::string &url)
        {
            const std::regex re(R"(^http://(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}):20300/?$)");
            if (!std::regex_match(url, re)) return false;
            const std::string ip = url.substr(7, url.find(":20300") - 7);
            std::size_t at = 0;
            int octets = 0;
            while (at < ip.size()) {
                const std::size_t dot = ip.find('.', at);
                const std::string part = ip.substr(at, dot == std::string::npos ? dot : dot - at);
                if (part.empty() || std::stoi(part) > 255) return false;
                ++octets;
                if (dot == std::string::npos) break;
                at = dot + 1;
            }
            return octets == 4;
        }

        void validateStaged(const ApplicationShellConfig &config)
        {
            if (config.builds.empty())
                throw std::invalid_argument("Missing packaged runtime root (--builds); launch the extracted app from its complete release folder");
            if (config.python.empty())
                throw std::invalid_argument("Missing packaged Python interpreter (--python)");
            if (config.worker.empty())
                throw std::invalid_argument("Missing packaged Python worker (--worker)");
            if (config.service.empty())
                throw std::invalid_argument("Missing packaged HTTP service (--service)");
            const fs::path builds = fs::weakly_canonical(config.builds);
            if (builds.filename() != "Builds" || !fs::is_directory(builds))
                throw std::runtime_error("Application Shell requires an existing sibling Builds directory");
            for (const std::string *item : {&config.worker, &config.service})
            {
                const fs::path file = fs::weakly_canonical(*item);
                if (!fs::is_regular_file(file) || file.string().compare(0, builds.string().size(), builds.string()) != 0 ||
                    file.string().size() <= builds.string().size() ||
                    (file.string()[builds.string().size()] != fs::path::preferred_separator))
                    throw std::runtime_error("Application Shell worker and service must be staged inside Builds: " + *item);
            }
            if (!fs::is_regular_file(fs::weakly_canonical(config.python)))
                throw std::runtime_error("Application Shell Python interpreter is missing: " + config.python);
        }

        // Spawn a fresh application *process*: each window gets an independent PTY,
        // Python service, session token and machine selection. No shell command is
        // constructed from WebView data; all arguments come from validated config.
        std::vector<std::string> terminalArgs(const ApplicationShellConfig &cfg)
        {
            std::vector<std::string> args = {"CinterForgeTerminal", "--python", cfg.python,
                                             "--worker", cfg.worker, "--service", cfg.service,
                                             "--builds", cfg.builds, "--title", cfg.title};
            if (cfg.developer)
                args.push_back("--developer");
            if (!cfg.connect_url.empty()) { args.push_back("--connect"); args.push_back(cfg.connect_url); }
            return args;
        }

#ifdef _WIN32
        std::wstring utf8(const std::string &value)
        {
            const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.c_str(), -1, nullptr, 0);
            if (!count)
                throw std::runtime_error("Unable to encode Windows Terminal path");
            std::wstring output(static_cast<std::size_t>(count), L'\0');
            MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.c_str(), -1, output.data(), count);
            output.pop_back();
            return output;
        }
        // Win32 clipboard text is the reliable fallback for WebView2's restricted
        // navigator.clipboard.readText on a loopback-origin packaged application.
        // Only the installed page invokes this user-gesture-bound paste bridge.
        std::string windowsClipboardText()
        {
            if (!OpenClipboard(nullptr))
                throw std::runtime_error("Windows clipboard is busy");
            HANDLE handle = GetClipboardData(CF_UNICODETEXT);
            if (!handle)
            {
                CloseClipboard();
                return {};
            }
            const wchar_t *wide = static_cast<const wchar_t *>(GlobalLock(handle));
            if (!wide)
            {
                CloseClipboard();
                throw std::runtime_error("Cannot read Windows clipboard");
            }
            const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
                                                  wide, -1, nullptr, 0, nullptr, nullptr);
            if (!bytes)
            {
                GlobalUnlock(handle);
                CloseClipboard();
                throw std::runtime_error("Windows clipboard has invalid Unicode text");
            }
            std::string value(static_cast<std::size_t>(bytes), '\0');
            const int written = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
                                                    wide, -1, value.data(), bytes, nullptr, nullptr);
            GlobalUnlock(handle);
            CloseClipboard();
            if (!written)
                throw std::runtime_error("Cannot decode Windows clipboard");
            value.pop_back(); // trailing NUL returned by WideCharToMultiByte
            return value;
        }
        std::wstring quoteWindows(const std::wstring &value)
        {
            std::wstring result = L"\"";
            std::size_t slash = 0;
            for (wchar_t c : value)
            {
                if (c == L'\\')
                {
                    ++slash;
                    continue;
                }
                if (c == L'\"')
                {
                    result.append(slash * 2 + 1, L'\\');
                    result += c;
                    slash = 0;
                    continue;
                }
                result.append(slash, L'\\');
                slash = 0;
                result += c;
            }
            result.append(slash * 2, L'\\');
            return result + L'\"';
        }
        void spawnNewTerminal(const ApplicationShellConfig &cfg)
        {
            std::vector<wchar_t> path(32768);
            DWORD count = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
            if (!count || count >= path.size())
                throw std::runtime_error("Cannot locate Windows Terminal executable");
            const std::wstring exe(path.data(), count);
            auto args = terminalArgs(cfg);
            std::wstring command = quoteWindows(exe);
            for (std::size_t i = 1; i < args.size(); ++i)
                command += L" " + quoteWindows(utf8(args[i]));
            STARTUPINFOW startup{};
            startup.cb = sizeof(startup);
            PROCESS_INFORMATION process{};
            if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE,
                                CREATE_NEW_PROCESS_GROUP, nullptr, nullptr, &startup, &process))
                throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), "New Terminal");
            CloseHandle(process.hThread);
            CloseHandle(process.hProcess);
        }
#else
        void spawnNewTerminal(const ApplicationShellConfig &cfg)
        {
#ifdef __APPLE__
            uint32_t size = 4096;
            std::vector<char> buffer(size);
            if (_NSGetExecutablePath(buffer.data(), &size))
            {
                buffer.resize(size + 1);
                if (_NSGetExecutablePath(buffer.data(), &size))
                    throw std::runtime_error("Cannot locate macOS Terminal executable");
            }
            const std::string executable = buffer.data();
#else
            const std::string executable = fs::read_symlink("/proc/self/exe").string();
#endif
            auto args = terminalArgs(cfg);
            args[0] = executable;
            std::vector<char *> argv;
            for (auto &part : args)
                argv.push_back(part.data());
            argv.push_back(nullptr);
            pid_t pid = 0;
            const int code = posix_spawn(&pid, executable.c_str(), nullptr, nullptr, argv.data(), environ);
            if (code)
                throw std::system_error(code, std::generic_category(), "New Terminal");
        }
#endif

#ifdef __APPLE__
        bool openNativeInspector(webview::webview &window);
        void installMacFileMenu(webview::webview &window, const ApplicationShellConfig &cfg)
        {
            // Keep the selector target alive. NSMenuItem.target is not an owning ref.
            static CinterFileMenuTarget *target = nil;
            if (!target)
                target = [[CinterFileMenuTarget alloc] init];
            target->newTerminalAction = [&window, cfg](const std::string &kind)
            {
                if (kind.rfind("lan:", 0) == 0) {
                    const std::string url = kind.substr(4);
                    if (validLanUrl(url)) {
                        auto remote = cfg; remote.connect_url = url;
                        remote.title = "CinterForge Terminal — " + url;
                        spawnNewTerminal(remote);
                    }
                    return;
                }
                if (kind == "lan-popup") {
                    window.dispatch([&window] { window.eval("window.CinterShowLAN && window.CinterShowLAN()"); });
                    return;
                }
                window.dispatch([&window, kind]
                                { window.eval("window.CinterTerminalNewTab && window.CinterTerminalNewTab(\"" + jsonEscape(kind) + "\")"); });
            };
            target->quitAction = []
            { [NSApp terminate:nil]; };
            target->debuggerAction = [&window]
            { window.dispatch([&window]
                              { openNativeInspector(window); }); };
            // The native menu must copy reliably even when WKWebView's asynchronous
            // navigator.clipboard API lacks permission (or a DOM user gesture).
            target->copyAction = [&window]
            { window.dispatch([&window]
                              {
        WKWebView *browser=(WKWebView*)webview_get_native_handle(
            static_cast<webview_t>(&window),
            WEBVIEW_NATIVE_HANDLE_KIND_BROWSER_CONTROLLER);
        if (!browser) return;
        [browser evaluateJavaScript:@"window.CinterEdit ? window.CinterEdit.transcript() : ''"
                   completionHandler:^(id value, NSError *error) {
            if (error || ![value isKindOfClass:[NSString class]]) return;
            NSPasteboard *pasteboard=[NSPasteboard generalPasteboard];
            [pasteboard clearContents];
            [pasteboard setString:(NSString*)value forType:NSPasteboardTypeString];
        }]; }); };
            target->pasteAction = [&window]
            {
                NSString *value = [[NSPasteboard generalPasteboard] stringForType:NSPasteboardTypeString];
                if (!value)
                    return;
                const std::string utf8 = [value UTF8String] ?: "";
                window.dispatch([&window, utf8]
                                { window.eval("window.CinterEdit && window.CinterEdit.insert(\"" + jsonEscape(utf8) + "\")"); });
            };
            NSMenu *bar = [[NSMenu alloc] initWithTitle:@""];
            NSMenuItem *appItem = [[NSMenuItem alloc] initWithTitle:@"CinterForge Terminal" action:nil keyEquivalent:@""];
            NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"CinterForge Terminal"];
            [appMenu addItemWithTitle:@"About CinterForge Terminal" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
            [appMenu addItem:[NSMenuItem separatorItem]];
            [appMenu addItemWithTitle:@"Hide CinterForge Terminal" action:@selector(hide:) keyEquivalent:@"h"];
            [appMenu addItemWithTitle:@"Hide Others" action:@selector(hideOtherApplications:) keyEquivalent:@"h"];
            [[appMenu itemAtIndex:3] setKeyEquivalentModifierMask:(NSEventModifierFlagCommand | NSEventModifierFlagOption)];
            [appMenu addItemWithTitle:@"Show All" action:@selector(unhideAllApplications:) keyEquivalent:@""];
            [appMenu addItem:[NSMenuItem separatorItem]];
            NSMenuItem *appQuit = [appMenu addItemWithTitle:@"Quit CinterForge Terminal" action:@selector(terminate:) keyEquivalent:@"q"];
            [appQuit setTarget:NSApp];
            [appItem setSubmenu:appMenu];
            [bar addItem:appItem];

            NSMenuItem *fileItem = [[NSMenuItem alloc] initWithTitle:@"File" action:nil keyEquivalent:@""];
            NSMenu *fileMenu = [[NSMenu alloc] initWithTitle:@"File"];
            // New Terminal actions always open Sessions in this WebView window.
            // Minecraft attaches to a running Bedrock server, never starts one.
            NSDictionary *labels = @{@"mac" : @"New Mac Bash Terminal",
                                     @"win" : @"New Windows Bash Terminal",
                                     @"linux" : @"New Linux Bash Terminal",
                                     @"minecraft" : @"New Minecraft Console",
                                     @"server" : @"New Server Log Terminal"};
            for (NSString *kind in @[ @"mac", @"win", @"linux", @"minecraft", @"server" ])
            {
                NSString *key = [kind isEqualToString:@"mac"] ? @"n" : @"";
                NSMenuItem *item = [fileMenu addItemWithTitle:labels[kind]
                                                       action:@selector(newTerminal:)
                                                keyEquivalent:key];
                [item setRepresentedObject:kind];
                [item setTarget:target];
                if ([kind isEqualToString:@"minecraft"])
                {
                    target->minecraftItem = item;
                    [item setEnabled:NO]; // Enabled only after confirmed Bedrock status.
                }
            }
            [fileMenu addItem:[NSMenuItem separatorItem]];
            NSMenuItem *lanItem = [fileMenu addItemWithTitle:@"Visible LAN Terminals" action:nil keyEquivalent:@""];
            [lanItem setSubmenu:[[NSMenu alloc] initWithTitle:@"Visible LAN Terminals"]];
            NSMenuItem *connectItem = [[lanItem submenu] addItemWithTitle:@"Connect / Refresh…" action:@selector(newTerminal:) keyEquivalent:@""];
            [connectItem setRepresentedObject:@"lan-popup"];
            [connectItem setTarget:target];
            [fileMenu addItem:[NSMenuItem separatorItem]];
            NSMenuItem *debugItem = [fileMenu addItemWithTitle:@"Open Debugger" action:@selector(openDebugger:) keyEquivalent:@"d"];
            [debugItem setTarget:target];
            [fileMenu addItem:[NSMenuItem separatorItem]];
            NSMenuItem *quitItem = [fileMenu addItemWithTitle:@"Quit" action:@selector(quitTerminal:) keyEquivalent:@""];
            [quitItem setTarget:target];
            [fileItem setSubmenu:fileMenu];
            [bar addItem:fileItem];

            NSMenuItem *editItem = [[NSMenuItem alloc] initWithTitle:@"Edit" action:nil keyEquivalent:@""];
            NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
            for (NSString *name in @[ @"Undo", @"Redo", @"Cut" ])
            {
                NSMenuItem *item = [editMenu addItemWithTitle:name action:nil keyEquivalent:@""];
                [item setEnabled:NO];
            }
            [editMenu addItem:[NSMenuItem separatorItem]];
            NSMenuItem *copyItem = [editMenu addItemWithTitle:@"Copy" action:@selector(copyTerminal:) keyEquivalent:@"c"];
            [copyItem setTarget:target];
            NSMenuItem *pasteItem = [editMenu addItemWithTitle:@"Paste" action:@selector(pasteTerminal:) keyEquivalent:@"v"];
            [pasteItem setTarget:target];
            [editMenu addItem:[NSMenuItem separatorItem]];
            for (NSString *name in @[ @"Delete", @"Select All" ])
            {
                NSMenuItem *item = [editMenu addItemWithTitle:name action:nil keyEquivalent:@""];
                [item setEnabled:NO];
            }
            [editItem setSubmenu:editMenu];
            [bar addItem:editItem];

            // Mirror the four HTML historical-machine menus from the exact staged
            // profiles manifest. No hard-coded partial Mac/BeOS/NeXTSTEP catalog.
            NSArray<NSString *> *groups = @[ @"Terminals", @"Macintosh", @"BeOS", @"NeXTSTEP" ];
            NSMutableDictionary<NSString *, NSMenu *> *menus = [NSMutableDictionary dictionary];
            for (NSString *group in groups)
            {
                NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:group action:nil keyEquivalent:@""];
                NSMenu *menu = [[NSMenu alloc] initWithTitle:group];
                [item setSubmenu:menu];
                [bar addItem:item];
                menus[group] = menu;
            }
            fs::path stagedProfiles = fs::path(cfg.service).parent_path() / "terminal_styles.json";
            NSData *data = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:stagedProfiles.string().c_str()]];
            NSDictionary *catalog = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
            NSMutableArray<NSDictionary *> *entries = [NSMutableArray array];
            if ([catalog isKindOfClass:[NSDictionary class]])
            {
                for (NSString *source in @[ @"profiles", @"emulators" ])
                {
                    NSArray *items = catalog[source];
                    if ([items isKindOfClass:[NSArray class]])
                        for (id entry in items)
                            if ([entry isKindOfClass:[NSDictionary class]])
                                [entries addObject:entry];
                }
                if ([catalog[@"codeoss"] isKindOfClass:[NSDictionary class]])
                    [entries addObject:catalog[@"codeoss"]];
            }
            [entries sortUsingComparator:^NSComparisonResult(NSDictionary *a, NSDictionary *b) {
              NSInteger ay = [a[@"year"] respondsToSelector:@selector(integerValue)] ? [a[@"year"] integerValue] : 0;
              NSInteger by = [b[@"year"] respondsToSelector:@selector(integerValue)] ? [b[@"year"] integerValue] : 0;
              if (ay != by)
                  return ay < by ? NSOrderedAscending : NSOrderedDescending;
              return [a[@"name"] compare:b[@"name"]];
            }];
            NSMutableSet<NSString *> *added = [NSMutableSet set];
            for (NSDictionary *entry in entries)
            {
                NSString *identifier = entry[@"id"], *name = entry[@"name"];
                if (![identifier isKindOfClass:[NSString class]] ||
                    ![name isKindOfClass:[NSString class]] || identifier.length > 100 || !identifier.length ||
                    [identifier rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyz0123456789-"] invertedSet]].location != NSNotFound)
                    continue;
                if ([identifier isEqualToString:@"quadra-650"] || [added containsObject:identifier])
                    continue;
                [added addObject:identifier];
                NSString *group = @"Terminals";
                if ([entry[@"kind"] isEqualToString:@"emulator"])
                {
                    if ([identifier hasPrefix:@"beos-"])
                        group = @"BeOS";
                    else if ([identifier hasPrefix:@"next-"])
                        group = @"NeXTSTEP";
                    else
                        group = @"Macintosh";
                }
                NSNumber *year = entry[@"year"];
                NSString *title = [NSString stringWithFormat:@"%@ %@", year ?: @"", name];
                NSMenuItem *item = [menus[group] addItemWithTitle:title action:@selector(newTerminal:) keyEquivalent:@""];
                [item setRepresentedObject:[@"profile:" stringByAppendingString:identifier]];
                [item setTarget:target];
            }
            [NSApp setMainMenu:bar];
        }

#endif

        // A typed `debugger` command is handled by the real native browser engine,
        // not by JavaScript `debugger;` (which merely pauses an attached inspector).
        // Called exclusively on the WebView UI thread.
        bool openNativeInspector(webview::webview &window)
        {
            void *handle = webview_get_native_handle(static_cast<webview_t>(&window),
                                                     WEBVIEW_NATIVE_HANDLE_KIND_BROWSER_CONTROLLER);
            if (!handle)
            {
                std::cerr << "[TERMINAL DEBUGGER] native browser controller unavailable\n";
                return false;
            }
#ifdef _WIN32
            auto *controller = static_cast<ICoreWebView2Controller *>(handle);
            ICoreWebView2 *browser = nullptr;
            const HRESULT acquired = controller->get_CoreWebView2(&browser);
            if (FAILED(acquired) || !browser)
            {
                std::cerr << "[TERMINAL DEBUGGER] WebView2 browser unavailable: " << acquired << '\n';
                return false;
            }
            const HRESULT opened = browser->OpenDevToolsWindow();
            browser->Release();
            if (FAILED(opened))
            {
                std::cerr << "[TERMINAL DEBUGGER] WebView2 OpenDevToolsWindow failed: " << opened << '\n';
                return false;
            }
            std::cerr << "[TERMINAL DEBUGGER] WebView2 DevTools opened\n";
            return true;
#elif defined(__linux__)
            auto *browser = WEBKIT_WEB_VIEW(handle);
            auto *settings = webkit_web_view_get_settings(browser);
            g_object_set(G_OBJECT(settings), "enable-developer-extras", TRUE, nullptr);
            auto *inspector = webkit_web_view_get_inspector(browser);
            if (!inspector)
            {
                std::cerr << "[TERMINAL DEBUGGER] WebKitGTK inspector unavailable\n";
                return false;
            }
            webkit_web_inspector_show(inspector);
            std::cerr << "[TERMINAL DEBUGGER] WebKitGTK inspector requested\n";
            return true;
#elif defined(__APPLE__)
            WKWebView *browser = (WKWebView *)handle;
            if (@available(macOS 13.3, *))
                browser.inspectable = YES;
            // macOS exposes inspectability publicly, but no public WKWebView API
            // directly opens the inspector. Try the installed WebKit inspector SPI;
            // if Apple removes it, preserve the official Safari Develop fallback.
            SEL accessor = NSSelectorFromString(@"_inspector");
            // WebKit's _WKInspector does NOT implement `show` in current SDKs.
            // Its private connect/attach entry points are checked dynamically and
            // never assumed to succeed. Public inspectable remains the fallback.
            SEL connect = NSSelectorFromString(@"connect");
            SEL attach = NSSelectorFromString(@"attach");
            SEL isVisible = NSSelectorFromString(@"isVisible");
            if ([browser respondsToSelector:accessor])
            {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Warc-performSelector-leaks"
                id inspector = [browser performSelector:accessor];
                if (inspector && [inspector respondsToSelector:connect] &&
                    [inspector respondsToSelector:attach])
                {
                    [inspector performSelector:connect];
                    [inspector performSelector:attach];
                    BOOL visible = [inspector respondsToSelector:isVisible] &&
                                   ((BOOL (*)(id, SEL))objc_msgSend)(inspector, isVisible);
                    std::cerr << "[TERMINAL DEBUGGER] WKWebView inspector connect/attach; visible="
                              << visible << '\n';
                    if (visible)
                        return true;
                }
#pragma clang diagnostic pop
            }
            std::cerr << "[TERMINAL DEBUGGER] WKWebView is inspectable; open Safari > Develop > "
                         "CinterForge Terminal to inspect (no public macOS direct-open API)\n";
            return false;
#else
            return false;
#endif
        }

        std::string readyUrl(const std::string &rpc)
        {
            // An application RPC error is a valid JSONL response. Never navigate to
            // arbitrary/untrusted URLs from it or accept a forged readiness message.
            static const std::regex url(R"re("url"\s*:\s*"(http://127\.0\.0\.1:[0-9]{1,5}/?)")re");
            std::smatch match;
            if (rpc.find("\"error\"") != std::string::npos || !std::regex_search(rpc, match, url))
                throw std::runtime_error("Hosted Python HTTP service did not return a loopback URL: " + rpc);
            return match[1];
        }
    } // namespace

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
        validateStaged(config);
        // The authenticated terminal's `debugger` command must work without a
        // separate environment flag supplied by the user at launch time.
#ifdef _WIN32
        if (_putenv_s("CPH_ENABLE_DEBUGGER", "1"))
            throw std::runtime_error("Cannot enable native Terminal debugger");
#else
        if (setenv("CPH_ENABLE_DEBUGGER", "1", 1))
            throw std::runtime_error("Cannot enable native Terminal debugger");
#endif
        if (!config.connect_url.empty() && !validLanUrl(config.connect_url))
            throw std::invalid_argument("LAN Terminal connection requires a private IPv4 address on port 20300");
        PythonHost host;
        bool started = false;
        bool hostRunning = false;
        try
        {
            std::string url = config.connect_url;
            if (url.empty()) {
                host.start(config.python, config.worker, config.service, std::chrono::seconds(10));
                hostRunning = true;
                const std::string reply = host.call("start", "{\"builds\":\"" + jsonEscape(config.builds) + "\"}",
                                                    std::chrono::seconds(15));
                url = readyUrl(reply);
                started = true;
            }
            std::cerr << "[CinterForgeTerminal] managed HTTP service ready: " << url << '\n';
            // One cross-platform webview/webview implementation; platform browser
            // engines are provided by the OS rather than bundled with the product.
            webview::webview window(true, nullptr); // Native Inspector is a Terminal command, not a build flag.
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
            installMacFileMenu(window, config);
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
            // Visible LAN Terminal entries open a separate native application
            // window/process, connected to that host's existing HTTP service.
            window.bind("cinterConnect", [&](const std::string &args) -> std::string
                        {
                std::smatch match;
                if (!std::regex_search(args, match, std::regex(R"re("url"\s*:\s*"(http://[^"\\]+)")re")))
                    return R"("invalid LAN URL")";
                const std::string remoteUrl = match[1].str();
                if (!validLanUrl(remoteUrl)) return R"("invalid LAN URL")";
                auto remote = config;
                remote.connect_url = remoteUrl;
                remote.title = "CinterForge Terminal — " + remoteUrl;
                spawnNewTerminal(remote);
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
            if (std::regex_search(args,std::regex(R"re("action"\s*:\s*"quit")re"))) {
                window.dispatch([&window] { window.terminate(); });
                return R"("closing")";
            }
            return R"("invalid action")"; });
#ifdef _WIN32
            // The UI's File/Edit and Ctrl+V use the actual Windows clipboard.
            // Read-only, no cross-origin network API and no text in native logs.
            window.bind("cinterClipboard", [&](const std::string &args) -> std::string
                        {
            if (args.find("\"action\":\"paste\"") == std::string::npos)
                return "null";
            return "\"" + jsonEscape(windowsClipboardText()) + "\""; });
#endif
            // Native bridge on the real staged application (not a synthetic smoke page).
            // This binding accepts telemetry only; privileged terminal commands continue
            // through the authenticated, loopback-only Python service.
            std::atomic<bool> ready{false};
            std::atomic<bool> museum{false};
            std::atomic<bool> returned{false};
            std::atomic<bool> failed{false};
            std::atomic<bool> finished{false};
            window.bind("cinterNative", [&](const std::string &args) -> std::string
                        {
#ifdef __APPLE__
            if (args.find("\"event\":\"minecraft-menu\"") != std::string::npos) {
                const bool enabled=args.find("\"enabled\":true") != std::string::npos;
                window.dispatch([enabled] {
                    NSMenu *file=[[[NSApp mainMenu] itemWithTitle:@"File"] submenu];
                    for (NSMenuItem *item in [file itemArray])
                        if ([[item representedObject] isEqual:@"minecraft"])
                            [item setEnabled:enabled];
                });
            }
#endif
#ifdef __APPLE__
            if (args.find("\"event\":\"lan-peers\"") != std::string::npos) {
                // Parse only after dispatch: no autoreleased Foundation object
                // crosses the native WebView thread boundary.
                const std::string payload = args;
                window.dispatch([payload] {
                    NSData *encoded = [NSData dataWithBytes:payload.data() length:payload.size()];
                    id parsed = [NSJSONSerialization JSONObjectWithData:encoded options:0 error:nil];
                    NSDictionary *detail = [parsed isKindOfClass:[NSArray class]] && [parsed count] ? [parsed firstObject] : parsed;
                    NSArray *peers = [detail isKindOfClass:[NSDictionary class]] ? detail[@"peers"] : nil;
                    NSMenu *file = [[[NSApp mainMenu] itemWithTitle:@"File"] submenu];
                    NSMenu *list = [[file itemWithTitle:@"Visible LAN Terminals"] submenu];
                    if (!list) return;
                    while ([list numberOfItems] > 1) [list removeItemAtIndex:1];
                    CinterFileMenuTarget *target = (CinterFileMenuTarget *)[[list itemAtIndex:0] target];
                    for (id peer in peers) {
                        if (![peer isKindOfClass:[NSDictionary class]]) continue;
                        NSString *url = peer[@"url"];
                        NSString *name = peer[@"name"];
                        if (![url isKindOfClass:[NSString class]] || !validLanUrl([url UTF8String])) continue;
                        NSMenuItem *item = [list addItemWithTitle:[NSString stringWithFormat:@"%@ — %@", name ?: @"Terminal", url]
                                                         action:@selector(newTerminal:) keyEquivalent:@""];
                        [item setRepresentedObject:[@"lan:" stringByAppendingString:url]];
                        [item setTarget:target];
                    }
                });
            }
#endif
            const bool isReady = args.find("\"event\":\"ready\"") != std::string::npos;
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
            std::cerr << "[CinterForgeTerminal] UI event: " << args << '\n';
            if (config.accept_ui && (isError || (ready.load() && museum.load() && returned.load())))
                window.dispatch([&window] { window.terminate(); });
            return R"("ok")"; });
            // The acceptance flag is part of the real loopback page navigation.
            // Unlike WebView init scripts, URL parameters have consistent timing
            // on WKWebView, WebKitGTK and WebView2. It grants no host privileges.
            window.navigate(config.accept_ui ? url + "?accept-ui=1" : url);
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
                    const auto response = host.call("next_debugger_request", "{}", std::chrono::seconds(3));
                    if (response.find("\"requested\": true") != std::string::npos ||
                        response.find("\"requested\":true") != std::string::npos) {
                        window.dispatch([&window] { openNativeInspector(window); });
                    }
                } catch (const std::exception& e) {
                    if (checking.load()) std::cerr << "[CinterForgeTerminal] inspector check: " << e.what() << '\n';
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
            if (started) { host.call("stop", "{}", std::chrono::seconds(5)); started = false; }
            if (hostRunning) host.stop();
            return 0;
        }
        catch (...)
        {
            if (started)
            {
                try
                {
                    host.call("stop", "{}", std::chrono::seconds(3));
                }
                catch (...)
                {
                }
            }
            if (hostRunning) host.stop();
            throw;
        }
    }
} // namespace cinterforge
