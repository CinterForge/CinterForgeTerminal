/*
 * CinterForgePlatformMenus.cpp — real native platform actions and menus.
 * PURPOSE: Implements the platform-specific work previously embedded in
 * CinterForgeTerminal.cpp, without changing native API payloads or menu IDs.
 * METHODS: Win32 clipboard conversion, UTF-8 process launching, Cocoa file
 * menu/Code menu target dispatch and WebView2/WebKitGTK/WKWebView debugger.
 * STARTUP: installMacFileMenu is called after the existing WebView is created.
 * SHUTDOWN: No Python, VM, Docker or Linux/Windows guest is owned here.
 */
#include "CinterForgePlatformMenus.h"
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

#ifdef __APPLE__
@implementation CinterForgeFileMenuTarget
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
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender
{
    // Cocoa's normal terminate: exits the process directly; that bypasses the
    // PythonHost and VM cleanup after window.run(). Stop the WebView event loop
    // instead, then allow runApplicationShell() to unwind its owned resources.
    (void)sender;
    std::cerr << "[CinterForgeTerminal] Native Quit requested; stopping WebView run loop for owned cleanup\n";
    if (quitAction)
        quitAction();
    else
        [NSApp stop:nil];
    return NSTerminateCancel;
}
- (BOOL)respondsToSelector:(SEL)selector
{
    return [super respondsToSelector:selector] ||
           (originalApplicationDelegate && [originalApplicationDelegate respondsToSelector:selector]);
}
- (id)forwardingTargetForSelector:(SEL)selector
{
    if (originalApplicationDelegate && [originalApplicationDelegate respondsToSelector:selector])
        return originalApplicationDelegate;
    return [super forwardingTargetForSelector:selector];
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
- (void)dispatchCodeMenu:(id)sender
{
    NSString *action = [sender representedObject];
    // Terminal Edit always uses the native pasteboard. Code-OSS owns only its
    // own code:* actions; they must not replace the Terminal clipboard route.
    if ([action isEqualToString:@"edit:copy"]) { [self copyTerminal:sender]; return; }
    if ([action isEqualToString:@"edit:paste"]) { [self pasteTerminal:sender]; return; }
    if (codeMenuAction && [action isKindOfClass:[NSString class]])
        codeMenuAction(std::string([action UTF8String]));
}
@end
#endif

namespace cinterforge {
namespace CinterForgePlatformMenus {
    namespace fs = std::filesystem;
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
            auto args = CinterForgeApplicationPaths::terminalArgs(cfg);
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
            auto args = CinterForgeApplicationPaths::terminalArgs(cfg);
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
            static CinterForgeFileMenuTarget *target = nil;
            if (!target)
                target = [[CinterForgeFileMenuTarget alloc] init];
            target->newTerminalAction = [&window, cfg](const std::string &kind)
            {
                if (kind.rfind("lan:", 0) == 0) {
                    const std::string url = kind.substr(4);
                    if (CinterForgeApplicationPaths::validLanUrl(url)) {
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
                                { window.eval("window.CinterTerminalNewTab && window.CinterTerminalNewTab(\"" + CinterForgeApplicationPaths::jsonEscape(kind) + "\")"); });
            };
            // Stop NSApplication.run, do not call terminate: (which bypasses
            // runApplicationShell's C++ destructor and process cleanup).
            target->quitAction = [] { [NSApp stop:nil]; };
            // webview/webview installs its own application delegate. Proxy its
            // optional callbacks so Dock Quit, Cmd+Q and close-window requests
            // all unwind through the same native shutdown path.
            if ([NSApp delegate] != target) {
                target->originalApplicationDelegate = [[NSApp delegate] retain];
                [NSApp setDelegate:target];
            }
            target->codeMenuAction = [&window](const std::string &action) {
                window.dispatch([&window, action] {
                    window.eval("window.CinterMenuDispatch && window.CinterMenuDispatch(\"" +
                                CinterForgeApplicationPaths::jsonEscape(action) + "\")");
                });
            };
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
        // Do not steal standard clipboard commands from a separate Web Inspector.
        if (NSApp.keyWindow && NSApp.keyWindow != browser.window) {
            [NSApp sendAction:@selector(copy:) to:nil from:nil];
            return;
        }
        [browser evaluateJavaScript:@"window.CinterEdit ? window.CinterEdit.transcript() : ''"
                   completionHandler:^(id value, NSError *error) {
            if (error || ![value isKindOfClass:[NSString class]]) return;
            NSPasteboard *pasteboard=[NSPasteboard generalPasteboard];
            [pasteboard clearContents];
            [pasteboard setString:(NSString*)value forType:NSPasteboardTypeString];
        }]; }); };
            target->pasteAction = [&window]
            {
                WKWebView *browser=(WKWebView*)webview_get_native_handle(
                    static_cast<webview_t>(&window),
                    WEBVIEW_NATIVE_HANDLE_KIND_BROWSER_CONTROLLER);
                if (browser && NSApp.keyWindow && NSApp.keyWindow != browser.window) {
                    [NSApp sendAction:@selector(paste:) to:nil from:nil];
                    return;
                }
                NSString *value = [[NSPasteboard generalPasteboard] stringForType:NSPasteboardTypeString];
                if (!value)
                    return;
                const std::string utf8 = [value UTF8String] ?: "";
                window.dispatch([&window, utf8]
                                { window.eval("window.CinterEdit && window.CinterEdit.insert(\"" + CinterForgeApplicationPaths::jsonEscape(utf8) + "\")"); });
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
            NSMenuItem *appQuit = [appMenu addItemWithTitle:@"Quit CinterForge Terminal" action:@selector(quitTerminal:) keyEquivalent:@"q"];
            [appQuit setTarget:target];
            [appItem setSubmenu:appMenu];
            [bar addItem:appItem];

            // Every app-wide editor menu is represented once. Code-OSS sends the
            // live actions when its workbench is ready; these native items are
            // enabled only in Code mode. Terminal and the quit/copy/paste items
            // remain usable in the historical terminal modes.
            NSArray<NSString *> *codeGroups = @[@"File", @"Edit", @"Selection", @"View",
                                                @"Go", @"Run", @"Terminal", @"Help"];
            NSMutableDictionary<NSString *, NSMenu *> *menus = [NSMutableDictionary dictionary];
            for (NSString *group in codeGroups)
            {
                NSMenuItem *entry = [[NSMenuItem alloc] initWithTitle:group action:nil keyEquivalent:@""];
                NSMenu *menu = [[NSMenu alloc] initWithTitle:group];
                [entry setSubmenu:menu];
                [entry setEnabled:YES]; // All modes share the same menu skeleton.
                [bar addItem:entry];
                menus[group] = menu;
            }
            const auto addAction = [&](NSString *group, NSString *name, NSString *action) {
                NSMenuItem *item = [menus[group] addItemWithTitle:name
                                                           action:@selector(dispatchCodeMenu:)
                                                    keyEquivalent:@""];
                [item setRepresentedObject:action];
                [item setTarget:target];
                return item;
            };
            addAction(@"File", @"Relaunch CinterForge Terminal", @"app:relaunch");
            addAction(@"File", @"Quit CinterForge Terminal", @"app:quit");
            // Native Edit menu owns the standard macOS keyboard equivalents.
            // Route them to the same working actions used by mouse selection.
            NSMenuItem *copyItem = addAction(@"Edit", @"Copy", @"edit:copy");
            NSMenuItem *pasteItem = addAction(@"Edit", @"Paste", @"edit:paste");
            [copyItem setAction:@selector(copyTerminal:)];
            [pasteItem setAction:@selector(pasteTerminal:)];
            [copyItem setKeyEquivalent:@"c"];
            [pasteItem setKeyEquivalent:@"v"];
            NSDictionary *terminalLabels = @{
                @"code": @"New CinterForge Code Terminal", @"mac": @"New Mac Bash",
                @"linux": @"Open Linux Bash in Code-OSS", @"win": @"Open Windows Bash in Code-OSS",
                @"launcher:mac": @"Mac Launcher", @"launcher:win": @"Windows Launcher",
                @"launcher:linux": @"Linux Launcher",
                @"server": @"New Server Log Terminal", @"if": @"New Z-Machine Terminal",
                @"minecraft": @"New Minecraft Terminal"
            };
            for (NSString *kind in @[@"code", @"mac", @"linux", @"win", @"launcher:mac",
                                    @"launcher:win", @"launcher:linux", @"server", @"if", @"minecraft"])
            {
                NSMenuItem *item = addAction(@"Terminal", terminalLabels[kind],
                    [@"terminal:" stringByAppendingString:kind]);
                if ([kind isEqualToString:@"minecraft"])
                {
                    target->minecraftItem = item;
                    [item setEnabled:NO];
                }
            }
            // Finder/Explorer opens the active writable Logs tree. The
            // authenticated native WebView calls the host, which resolves the
            // actual installed Steam library or developer sibling Builds.
            addAction(@"Terminal", @"Open Logs Folder", @"app:open-logs");
            // These CinterForge catalogs stay at the far right of both bars.
            for (NSString *group in @[@"LAN", @"Macintosh", @"BeOS", @"NeXTSTEP", @"Machines"])
            {
                NSMenuItem *entry = [[NSMenuItem alloc] initWithTitle:group action:nil keyEquivalent:@""];
                NSMenu *menu = [[NSMenu alloc] initWithTitle:group];
                [entry setSubmenu:menu];
                [bar addItem:entry];
                menus[group] = menu;
            }
            NSMenuItem *connect = [menus[@"LAN"] addItemWithTitle:@"Connect / Refresh…"
                                                            action:@selector(newTerminal:) keyEquivalent:@""];
            [connect setRepresentedObject:@"lan-popup"];
            [connect setTarget:target];
            fs::path stagedProfiles = fs::path(cfg.service).parent_path() / "data/terminal_styles.json";
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
                NSString *group = @"Machines";
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

} // namespace CinterForgePlatformMenus
} // namespace cinterforge
