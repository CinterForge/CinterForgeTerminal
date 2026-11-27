/*
 * CinterForgeTerminal.cpp — native application owner and WebView lifecycle.
 * PURPOSE: Own exactly one PythonHost worker, authenticated HTTP start/stop,
 * WebView creation, native callback authorization, debugger dispatch,
 * connection health monitoring, service reboot and orderly VM shutdown.
 * METHODS: smokeWebViewJavaScript, runApplicationShell.
 * STARTUP: Called by CinterTerminalMain.cpp; resolves staged worker and
 * displays native startup artwork while Python initializes, then navigates WebView.
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
#include <future>
#include <cstring>
#include "CinterStartupSplashBytes.h"
#include <memory>
#include <system_error>
#include <vector>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <objidl.h>
#include <gdiplus.h>
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
#include <unistd.h>
#ifdef __APPLE__
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#elif defined(__linux__)
#include <sys/socket.h>
#include <sys/un.h>
#include <cstddef>
#endif
extern char **environ;
#ifdef __APPLE__
#include <mach-o/dyld.h>
#import <Cocoa/Cocoa.h>
#import <CoreFoundation/CoreFoundation.h>
#import <QuartzCore/QuartzCore.h>
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
    // A local Terminal owns the HTTP service for its entire native lifetime.
    // Use OS-local, crash-released IPC, independent of which app bundle or
    // version is launched. No runtime data, port, or build cache is created.
    // Remote LAN viewer windows deliberately do not acquire this guard.
    class LocalTerminalInstance final {
    public:
        LocalTerminalInstance() = default;
        LocalTerminalInstance(const LocalTerminalInstance&) = delete;
        LocalTerminalInstance& operator=(const LocalTerminalInstance&) = delete;
        ~LocalTerminalInstance() { release(); }
        bool acquire() {
#ifdef _WIN32
            // Separate Windows sessions (including the Wine guest) keep their
            // own instance namespace. Close a duplicate handle immediately.
            handle_ = CreateMutexW(nullptr, FALSE, L"Local\\CinterForgeTerminal");
            const DWORD error = GetLastError();
            if (!handle_) throw std::system_error(error, std::system_category(), "Terminal instance guard");
            if (error == ERROR_ALREADY_EXISTS) { release(); return false; }
#elif defined(__APPLE__)
            // Darwin's existing private per-user IPC directory is shared by
            // Finder, Steam and Xcode, not tied to an app's writable root.
            const size_t size = confstr(_CS_DARWIN_USER_TEMP_DIR, nullptr, 0);
            if (!size) throw std::runtime_error("Cannot locate macOS user IPC directory");
            std::vector<char> directory(size);
            if (!confstr(_CS_DARWIN_USER_TEMP_DIR, directory.data(), size))
                throw std::runtime_error("Cannot resolve macOS user IPC directory");
            const auto path = fs::path(directory.data()) / "com.cinterforge.terminal.instance.lock";
            fd_ = open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
            if (fd_ < 0) throw std::system_error(errno, std::generic_category(), "Terminal instance guard");
            struct stat info{};
            if (fstat(fd_, &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != geteuid() ||
                (info.st_mode & 0077) != 0 || info.st_nlink != 1) {
                release(); throw std::runtime_error("Unsafe macOS Terminal instance guard");
            }
            if (flock(fd_, LOCK_EX | LOCK_NB) != 0) {
                const int error = errno;
                release();
                if (error == EWOULDBLOCK || error == EAGAIN) return false;
                throw std::system_error(error, std::generic_category(), "Terminal instance guard");
            }
            // Leave the empty inode in place: unlinking it would allow another
            // process to lock a different inode while this one is still held.
#elif defined(__linux__)
            // Abstract Unix socket: atomic exclusive name, no file or TCP port.
            fd_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
            if (fd_ < 0) throw std::system_error(errno, std::generic_category(), "Terminal instance guard");
            sockaddr_un address{};
            address.sun_family = AF_UNIX;
            const char name[] = "com.cinterforge.terminal.instance";
            static_assert(sizeof(name) <= sizeof(address.sun_path), "IPC name too long");
            std::memcpy(address.sun_path + 1, name, sizeof(name) - 1);
            if (bind(fd_, reinterpret_cast<const sockaddr*>(&address),
                     static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + sizeof(name))) != 0) {
                const int error = errno;
                release();
                if (error == EADDRINUSE) return false;
                throw std::system_error(error, std::generic_category(), "Terminal instance guard");
            }
#endif
            return true;
        }
        void release() noexcept {
#ifdef _WIN32
            if (handle_) { CloseHandle(handle_); handle_ = nullptr; }
#else
            if (fd_ >= 0) { close(fd_); fd_ = -1; }
#endif
        }
    private:
#ifdef _WIN32
        HANDLE handle_ = nullptr;
#else
        int fd_ = -1;
#endif
    };

    static void alreadyRunningDialog() {
        std::cerr << "[TERMINAL STARTUP] Another local CinterForge Terminal is already running; exiting second launch\n";
#ifdef __APPLE__
        // Same native notification API as the existing startup failure dialog;
        // no NSApplication event pump before WebView's Cocoa launch loop.
        CFUserNotificationDisplayAlert(0, kCFUserNotificationNoteAlertLevel,
            nullptr, nullptr, nullptr, CFSTR("CinterForge Terminal"),
            CFSTR("CinterForge Terminal is already running. Please use the existing window."),
            CFSTR("OK"), nullptr, nullptr, nullptr);
#elif defined(_WIN32)
        MessageBoxW(nullptr, L"CinterForge Terminal is already running. Please use the existing window.",
                    L"CinterForge Terminal", MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND);
#elif defined(__linux__)
#if GTK_MAJOR_VERSION >= 4
        if (!gtk_init_check()) return;
#else
        if (!gtk_init_check(nullptr, nullptr)) return;
#endif
        GtkWidget *dialog = gtk_message_dialog_new(nullptr, GTK_DIALOG_MODAL,
            GTK_MESSAGE_INFO, GTK_BUTTONS_OK, "%s", "CinterForge Terminal is already running.");
        gtk_window_set_title(GTK_WINDOW(dialog), "CinterForge Terminal");
        gtk_message_dialog_format_secondary_text(GTK_MESSAGE_DIALOG(dialog), "%s", "Please use the existing window.");
#if GTK_MAJOR_VERSION >= 4
        GMainLoop *loop = g_main_loop_new(nullptr, FALSE);
        g_signal_connect(dialog, "response", G_CALLBACK(+[](GtkDialog*, gint, gpointer data) {
            g_main_loop_quit(static_cast<GMainLoop*>(data));
        }), loop);
        g_signal_connect(dialog, "close-request", G_CALLBACK(+[](GtkWindow*, gpointer data) -> gboolean {
            g_main_loop_quit(static_cast<GMainLoop*>(data)); return TRUE;
        }), loop);
        gtk_window_present(GTK_WINDOW(dialog));
        g_main_loop_run(loop);
        gtk_window_destroy(GTK_WINDOW(dialog));
        g_main_loop_unref(loop);
#else
        gtk_dialog_run(GTK_DIALOG(dialog));
        gtk_widget_destroy(dialog);
#endif
#endif
    }

    enum class StartupPhase { Worker, Import, WorkerReady, Http, WebView, Terminal };
    static const char *startupStatus(StartupPhase phase) {
        switch (phase) {
        case StartupPhase::Worker: return "Starting Python worker...";
        case StartupPhase::Import: return "Loading Terminal services...";
        case StartupPhase::WorkerReady: return "Finishing Python worker initialization...";
        case StartupPhase::Http: return "Starting Terminal HTTP service...";
        case StartupPhase::WebView: return "Preparing Terminal window...";
        case StartupPhase::Terminal: return "Loading Terminal interface...";
        }
        return "Starting CinterForge Terminal...";
    }

    // Native-only startup presentation. No second WebView, service request,
    // asset lookup or external process. All UI calls run on the GUI thread.
    class StartupSplash final {
    public:
        StartupSplash() = default;
        StartupSplash(const StartupSplash&) = delete;
        StartupSplash& operator=(const StartupSplash&) = delete;
        ~StartupSplash() { dismiss(); }

        void show() {
#ifdef __APPLE__
            [NSApplication sharedApplication];
            NSData *data = [NSData dataWithBytes:cinterStartupSplashBytes
                                         length:sizeof(cinterStartupSplashBytes)];
            NSImage *image = [[NSImage alloc] initWithData:data];
            if (!image) return;
            const NSSize size = NSMakeSize(920, 440);
            NSRect rect = NSMakeRect(0, 0, size.width, size.height);
            panel_ = [[NSPanel alloc] initWithContentRect:rect
                         styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
                           backing:NSBackingStoreBuffered defer:NO];
            [panel_ setReleasedWhenClosed:NO];
            [panel_ setLevel:NSFloatingWindowLevel];
            [panel_ setHidesOnDeactivate:NO];
            [panel_ setIgnoresMouseEvents:YES];
            NSView *content = [[NSView alloc] initWithFrame:rect];
            NSImageView *view = [[NSImageView alloc] initWithFrame:NSMakeRect(0, 36, 920, 404)];
            [view setImage:image];
            [view setImageScaling:NSImageScaleProportionallyUpOrDown];
            [view setAutoresizingMask:NSViewWidthSizable | NSViewHeightSizable];
            NSView *bar = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 920, 36)];
            [bar setWantsLayer:YES];
            [[bar layer] setBackgroundColor:[[NSColor colorWithSRGBRed:0.10 green:0.13 blue:0.16 alpha:1] CGColor]];
            statusLabel_ = [[NSTextField alloc] initWithFrame:NSMakeRect(16, 8, 888, 20)];
            [statusLabel_ setEditable:NO];
            [statusLabel_ setSelectable:NO];
            [statusLabel_ setBordered:NO];
            [statusLabel_ setDrawsBackground:NO];
            [statusLabel_ setFont:[NSFont systemFontOfSize:13]];
            [statusLabel_ setTextColor:[NSColor whiteColor]];
            [statusLabel_ setAlignment:NSTextAlignmentLeft];
            [statusLabel_ setStringValue:@"Starting Python worker..."];
            [bar addSubview:statusLabel_];
            [content addSubview:view];
            [content addSubview:bar];
            [panel_ setContentView:content];
#if !__has_feature(objc_arc)
            [statusLabel_ release];
            [bar release];
            [content release];
            [view release];
            [image release];
#endif
            [panel_ center];
            [panel_ orderFrontRegardless];
            [panel_ display];
#elif defined(_WIN32)
            Gdiplus::GdiplusStartupInput input;
            if (Gdiplus::GdiplusStartup(&gdiplus_, &input, nullptr) != Gdiplus::Ok) {
                gdiplus_ = 0; return;
            }
            HGLOBAL bytes = GlobalAlloc(GMEM_MOVEABLE, sizeof(cinterStartupSplashBytes));
            if (!bytes) { dismiss(); return; }
            void *data = GlobalLock(bytes);
            if (!data) { GlobalFree(bytes); dismiss(); return; }
            std::memcpy(data, cinterStartupSplashBytes, sizeof(cinterStartupSplashBytes));
            GlobalUnlock(bytes);
            if (FAILED(CreateStreamOnHGlobal(bytes, TRUE, &stream_))) {
                GlobalFree(bytes); dismiss(); return;
            }
            image_ = Gdiplus::Image::FromStream(stream_);
            if (!image_ || image_->GetLastStatus() != Gdiplus::Ok) { dismiss(); return; }
            const HINSTANCE instance = GetModuleHandleW(nullptr);
            WNDCLASSW cls{};
            cls.lpfnWndProc = paintWindow;
            cls.hInstance = instance;
            cls.lpszClassName = L"CinterForgeTerminalStartupSplash";
            if (!RegisterClassW(&cls) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
                dismiss(); return;
            }
            POINT cursor{}; GetCursorPos(&cursor);
            MONITORINFO monitor{}; monitor.cbSize = sizeof(monitor);
            GetMonitorInfoW(MonitorFromPoint(cursor, MONITOR_DEFAULTTOPRIMARY), &monitor);
            const RECT r = monitor.rcWork;
            int width = 920, height = 440;
            const int available = r.right-r.left;
            if (available > 0 && available < width) { width = available; height = width*404/920 + 36; }
            hwnd_ = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                       cls.lpszClassName, L"CinterForge Terminal", WS_POPUP,
                       r.left+(r.right-r.left-width)/2, r.top+(r.bottom-r.top-height)/2,
                       width, height, nullptr, nullptr, instance, this);
            if (!hwnd_) { dismiss(); return; }
            ShowWindow(hwnd_, SW_SHOWNOACTIVATE);
            UpdateWindow(hwnd_);
#elif defined(__linux__)
#if GTK_MAJOR_VERSION >= 4
            if (!gtk_init_check()) return;
#else
            if (!gtk_init_check(nullptr, nullptr)) return;
#endif
            GdkPixbufLoader *loader = gdk_pixbuf_loader_new();
            GError *error = nullptr;
            const bool decoded = gdk_pixbuf_loader_write(loader, cinterStartupSplashBytes,
                                        sizeof(cinterStartupSplashBytes), &error) &&
                                 gdk_pixbuf_loader_close(loader, &error);
            GdkPixbuf *image = decoded ? gdk_pixbuf_loader_get_pixbuf(loader) : nullptr;
            if (!image) {
                if (error) g_error_free(error);
                g_object_unref(loader); return;
            }
#if GTK_MAJOR_VERSION >= 4
            gtk_ = GTK_WINDOW(gtk_window_new());
            GtkWidget *picture = gtk_picture_new_for_pixbuf(image);
            GtkWidget *layout = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
            gtk_box_append(GTK_BOX(layout), picture);
            statusLabel_ = gtk_label_new("Starting Python worker...");
            gtk_box_append(GTK_BOX(layout), statusLabel_);
            gtk_window_set_child(gtk_, layout);
            g_signal_connect(gtk_, "close-request", G_CALLBACK(+[](GtkWindow*, gpointer) -> gboolean { return TRUE; }), nullptr);
#else
            gtk_ = GTK_WINDOW(gtk_window_new(GTK_WINDOW_TOPLEVEL));
            GtkWidget *layout = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
            gtk_box_pack_start(GTK_BOX(layout), gtk_image_new_from_pixbuf(image), FALSE, FALSE, 0);
            statusLabel_ = gtk_label_new("Starting Python worker...");
            gtk_box_pack_start(GTK_BOX(layout), statusLabel_, FALSE, FALSE, 0);
            gtk_container_add(GTK_CONTAINER(gtk_), layout);
            gtk_window_set_position(gtk_, GTK_WIN_POS_CENTER);
            gtk_window_set_keep_above(gtk_, TRUE);
            gtk_window_set_skip_taskbar_hint(gtk_, TRUE);
            gtk_window_set_type_hint(gtk_, GDK_WINDOW_TYPE_HINT_SPLASHSCREEN);
            gtk_window_set_accept_focus(gtk_, FALSE);
            g_signal_connect(gtk_, "delete-event", G_CALLBACK(+[](GtkWidget*, GdkEvent*, gpointer) -> gboolean { return TRUE; }), nullptr);
#endif
            gtk_label_set_xalign(GTK_LABEL(statusLabel_), 0.0f);
            gtk_widget_set_margin_start(statusLabel_, 16);
            gtk_widget_set_margin_end(statusLabel_, 16);
            gtk_widget_set_margin_top(statusLabel_, 8);
            gtk_widget_set_margin_bottom(statusLabel_, 8);
            gtk_widget_set_size_request(statusLabel_, -1, 20);
            g_object_unref(loader);
            if (error) g_error_free(error);
            gtk_window_set_title(gtk_, "CinterForge Terminal");
            gtk_window_set_decorated(gtk_, FALSE);
            gtk_window_set_resizable(gtk_, FALSE);
            gtk_window_set_default_size(gtk_, 920, 440);
#if GTK_MAJOR_VERSION >= 4
            gtk_window_present(gtk_);
#else
            gtk_widget_show_all(GTK_WIDGET(gtk_));
#endif
#endif
            pump();
        }

        // Only the GUI thread touches native widgets. The worker thread
        // publishes an atomic phase, never a UI pointer or arbitrary text.
        void setPhase(StartupPhase phase) {
            if (phase == shownPhase_) return;
            shownPhase_ = phase;
            const char *text = startupStatus(phase);
#ifdef __APPLE__
            if (statusLabel_) {
                [statusLabel_ setStringValue:[NSString stringWithUTF8String:text]];
                [statusLabel_ displayIfNeeded];
            }
#elif defined(_WIN32)
            status_.assign(text, text + std::strlen(text)); // fixed ASCII messages
            if (hwnd_) { InvalidateRect(hwnd_, nullptr, FALSE); UpdateWindow(hwnd_); }
#elif defined(__linux__)
            if (statusLabel_) gtk_label_set_text(GTK_LABEL(statusLabel_), text);
#endif
        }

        void pump() {
#ifdef __APPLE__
            @autoreleasepool {
                for (int i=0; i<32; ++i) {
                    NSEvent *event = [NSApp nextEventMatchingMask:NSEventMaskAny
                                        untilDate:[NSDate distantPast]
                                           inMode:NSDefaultRunLoopMode dequeue:YES];
                    if (!event) break;
                    [NSApp sendEvent:event];
                }
                [NSApp updateWindows];
            }
#elif defined(_WIN32)
            MSG message;
            for (int i=0; i<32 && PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE); ++i) {
                if (message.message == WM_QUIT) { PostQuitMessage(static_cast<int>(message.wParam)); break; }
                TranslateMessage(&message); DispatchMessageW(&message);
            }
#elif defined(__linux__)
            for (int i=0; i<32 && g_main_context_pending(nullptr); ++i)
                g_main_context_iteration(nullptr, FALSE);
#endif
        }

        void followMainWindow(webview::webview &window) {
#if defined(__linux__)
            // Wayland controls window position and stacking; associate the
            // splash with the actual application as soon as it exists.
            if (gtk_) {
                auto *main = static_cast<GtkWindow*>(webview_get_window(static_cast<webview_t>(&window)));
                if (main) gtk_window_set_transient_for(gtk_, main);
#if GTK_MAJOR_VERSION >= 4
                gtk_window_present(gtk_);
#endif
            }
#else
            (void)window;
#endif
        }

        void dismiss() noexcept {
#ifdef __APPLE__
            if (panel_) {
                statusLabel_ = nil;
                [panel_ orderOut:nil]; [panel_ close];
#if !__has_feature(objc_arc)
                [panel_ release];
#endif
                panel_ = nil;
            }
#elif defined(_WIN32)
            if (hwnd_) { DestroyWindow(hwnd_); hwnd_ = nullptr; }
            delete image_; image_ = nullptr;
            if (stream_) { stream_->Release(); stream_ = nullptr; }
            if (gdiplus_) { Gdiplus::GdiplusShutdown(gdiplus_); gdiplus_ = 0; }
#elif defined(__linux__)
            if (gtk_) {
                statusLabel_ = nullptr;
#if GTK_MAJOR_VERSION >= 4
                gtk_window_destroy(gtk_);
#else
                gtk_widget_destroy(GTK_WIDGET(gtk_));
#endif
                gtk_ = nullptr;
            }
#endif
        }
    private:
        StartupPhase shownPhase_ = StartupPhase::Worker;
#ifdef __APPLE__
        NSPanel *panel_ = nil;
        NSTextField *statusLabel_ = nil;
#elif defined(_WIN32)
        HWND hwnd_ = nullptr;
        ULONG_PTR gdiplus_ = 0;
        IStream *stream_ = nullptr;
        Gdiplus::Image *image_ = nullptr;
        std::wstring status_ = L"Starting Python worker...";
        static LRESULT CALLBACK paintWindow(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
            if (msg == WM_NCCREATE) {
                auto *create = reinterpret_cast<CREATESTRUCTW*>(lp);
                SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(create->lpCreateParams));
            }
            auto *self = reinterpret_cast<StartupSplash*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
            if (msg == WM_PAINT) {
                PAINTSTRUCT ps{}; HDC dc = BeginPaint(hwnd, &ps);
                if (self && self->image_) {
                    RECT r{}; GetClientRect(hwnd, &r);
                    const int barTop = r.bottom - 36;
                    {
                        Gdiplus::Graphics graphics(dc);
                        graphics.DrawImage(self->image_, 0, 0, static_cast<INT>(r.right), static_cast<INT>(barTop));
                    }
                    RECT bar{0, barTop, r.right, r.bottom};
                    HBRUSH background = CreateSolidBrush(RGB(26, 33, 41));
                    FillRect(dc, &bar, background);
                    DeleteObject(background);
                    bar.left += 16; bar.right -= 16;
                    SetBkMode(dc, TRANSPARENT);
                    SetTextColor(dc, RGB(255,255,255));
                    HGDIOBJ oldFont = SelectObject(dc, GetStockObject(DEFAULT_GUI_FONT));
                    DrawTextW(dc, self->status_.c_str(), -1, &bar, DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);
                    SelectObject(dc, oldFont);
                }
                EndPaint(hwnd, &ps); return 0;
            }
            if (msg == WM_ERASEBKGND) return 1;
            if (msg == WM_CLOSE) return 0;
            return DefWindowProcW(hwnd, msg, wp, lp);
        }
#elif defined(__linux__)
        GtkWindow *gtk_ = nullptr;
        GtkWidget *statusLabel_ = nullptr;
#endif
    };

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
        const bool ownLoopback = config.connect_url == "http://127.0.0.1:20300/" ||
                                 config.connect_url == "http://127.0.0.1:20300";
        const bool local = config.connect_url.empty() || ownLoopback;
        LocalTerminalInstance instance;
        if (local && !instance.acquire()) {
            // Native acceptance must fail rather than treat another running app
            // as a passed UI test, and must never block on a modal dialog.
            if (config.accept_ui) throw std::runtime_error("Terminal is already running; close it before UI acceptance");
            alreadyRunningDialog();
            return 0;
        }
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
        std::atomic<StartupPhase> phase{StartupPhase::Worker};
        auto host = std::make_unique<PythonHost>();
        bool started = false;
        bool hostRunning = false;
        bool runtimeOwner = false;
        std::atomic<bool> relaunchRequested{false};
        StartupSplash splash;
        try
        {
            std::string url = config.connect_url;
            std::string navigationUrl = url;
            std::string activeService = config.service;
            // This machine's own advertised loopback service is still a native
            // launch; use the existing private grant instead of browser login.
            // Cocoa WebView 0.12.0 owns the first NSApplication launch loop.
            // Construct it BEFORE creating/pumping any AppKit splash window;
            // pre-launch event pumping conflicts with that initialization.
            // Keep the main window out of sight during the slow Python startup.
            std::unique_ptr<webview::webview> mainWindow;
#ifdef __APPLE__
            if (url.empty() || ownLoopback) {
                std::cerr << "[TERMINAL STARTUP] START Cocoa WebView constructor\n";
                mainWindow = std::make_unique<webview::webview>(true, nullptr);
                auto *native = webview_get_window(static_cast<webview_t>(mainWindow.get()));
                if (!native) throw std::runtime_error("Cocoa WebView window unavailable");
                [(NSWindow *)native orderOut:nil];
                std::cerr << "[TERMINAL STARTUP] PASS Cocoa WebView constructor\n";
            }
#endif
            if (url.empty() || ownLoopback) {
                splash.show();
                // Keep the native UI thread servicing splash paint events while
                // the same owned worker and HTTP startup execute off-thread.
                auto startup = std::async(std::launch::async, [&] {
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
                    const auto workerBudget = std::chrono::seconds(wineGuest ? 90 : 45);
                    const auto httpBudget = std::chrono::seconds(wineGuest ? 60 : 45);
                    std::cerr << "[TERMINAL STARTUP] START Python worker/service import (budget="
                              << workerBudget.count() << "s)\n";
                    // Only fixed startup phases are logged. General Python events
                    // can include session data and must never be dumped here.
                    host->onEvent([&phase](const std::string& event) {
                        if (event.find("\"event\": \"startup\"") != std::string::npos) {
                            if (event.find("\"import-service\"") != std::string::npos) {
                                phase.store(StartupPhase::Import);
                                std::cerr << "[TERMINAL STARTUP] Python worker reached service import\n";
                            }
                            if (event.find("\"service-imported\"") != std::string::npos) {
                                phase.store(StartupPhase::WorkerReady);
                                std::cerr << "[TERMINAL STARTUP] Python service import completed\n";
                            }
                        }
                    });
                    try {
                        host->start(config.python, config.worker, config.service, workerBudget);
                    } catch (const std::exception &e) {
                        throw std::runtime_error("Python worker/service readiness failed (budget=" +
                            std::to_string(workerBudget.count()) + "s): " + e.what());
                    }
                    std::cerr << "[TERMINAL STARTUP] PASS Python worker ready\n";
                    hostRunning = true;
                    phase.store(StartupPhase::Http);
                    std::cerr << "[TERMINAL STARTUP] START Python HTTP service (budget="
                              << httpBudget.count() << "s)\n";
                    std::string reply;
                    try {
                        reply = host->call("start", "{\"builds\":\"" +
                            CinterForgeApplicationPaths::jsonEscape(config.builds) + "\"}", httpBudget);
                    } catch (const std::exception &e) {
                        throw std::runtime_error("Python HTTP service readiness failed (budget=" +
                            std::to_string(httpBudget.count()) + "s): " + e.what());
                    }
                    std::cerr << "[TERMINAL STARTUP] PASS Python HTTP service replied\n";
                    url = CinterForgePythonServiceHandshake::readyUrl(reply);
                    navigationUrl = CinterForgePythonServiceHandshake::nativeBootstrapUrl(reply, url);
                    runtimeOwner = reply.find("\"attached\": true") == std::string::npos &&
                                   reply.find("\"attached\":true") == std::string::npos;
                    started = true;
                });
                while (startup.wait_for(std::chrono::milliseconds(16)) != std::future_status::ready) {
                    splash.setPhase(phase.load());
                    splash.pump();
                }
                startup.get();
                // Compatibility with a previously installed release that did
                // not have the new native guard. Never stop its attached HTTP
                // service or its runtime; only stop our own worker.
                if (!runtimeOwner) {
                    started = false;
                    host->stop();
                    hostRunning = false;
                    splash.dismiss();
                    if (config.accept_ui) throw std::runtime_error("Terminal is already running; close it before UI acceptance");
                    alreadyRunningDialog();
                    return 0;
                }
                splash.setPhase(StartupPhase::WebView);
                splash.pump();
            }
            std::cerr << "[CinterForgeTerminal] managed HTTP service ready: " << url << '\n';
            // One cross-platform webview/webview implementation; platform browser
            // engines are provided by the OS rather than bundled with the product.
#ifdef _WIN32
            std::cerr << "[CinterForgeTerminal] WebView2 constructor starting\n";
#endif
            // Reuse the already initialized Cocoa WebView; other platforms
            // and remote connections retain their normal construction path.
            if (!mainWindow) mainWindow = std::make_unique<webview::webview>(true, nullptr);
            auto &window = *mainWindow; // Native Inspector is a Terminal command, not a build flag.
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
            splash.followMainWindow(window);
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
            // Do not consume the macOS Copy/Paste key equivalents when an
            // embedded browser is active. They must enter WKWebView through the
            // ordinary responder chain, allowing WebKit to deliver paste
            // to Infinite Mac's focused iframe. Re-dispatching from a custom
            // menu target can lose the original key event/gesture in WebKit.
            if (args.find("\"event\":\"clipboard-context\"") != std::string::npos) {
                const bool embedded = args.find("\"embedded\":true") != std::string::npos;
                window.dispatch([embedded] {
                    id owner = [NSApp delegate];
                    if (![owner isKindOfClass:[CinterForgeFileMenuTarget class]]) return;
                    CinterForgeFileMenuTarget *target = (CinterForgeFileMenuTarget *)owner;
                    target->embeddedClipboardActive = embedded ? YES : NO;
                    NSMenu *edit = [[[NSApp mainMenu] itemWithTitle:@"Edit"] submenu];
                    for (NSMenuItem *item in [edit itemArray]) {
                        if ([[item title] isEqualToString:@"Copy"]) {
                            [item setAction:embedded ? @selector(copy:) : @selector(copyTerminal:)];
                            [item setTarget:embedded ? nil : target];
                            [item setEnabled:YES];
                        } else if ([[item title] isEqualToString:@"Paste"]) {
                            [item setAction:embedded ? @selector(paste:) : @selector(pasteTerminal:)];
                            [item setTarget:embedded ? nil : target];
                            [item setEnabled:YES];
                        }
                    }
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
                                    // Embedded editors must receive the native
                                    // Copy action, never our Terminal transcript handler.
                                    if (target->embeddedClipboardActive) {
                                        [entry setAction:@selector(copy:)];
                                        [entry setTarget:nil];
                                    } else if ([action isEqualToString:@"edit:copy"]) {
                                        [entry setAction:@selector(copyTerminal:)];
                                    }
                                    [entry setKeyEquivalent:@"c"];
                                } else if ([title isEqualToString:@"Paste"]) {
                                    if (target->embeddedClipboardActive) {
                                        [entry setAction:@selector(paste:)];
                                        [entry setTarget:nil];
                                    } else if ([action isEqualToString:@"edit:paste"]) {
                                        [entry setAction:@selector(pasteTerminal:)];
                                    }
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
            if (isReady || isError) window.dispatch([&splash] { splash.dismiss(); });
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
            splash.setPhase(StartupPhase::Terminal);
            window.navigate(config.accept_ui
                ? navigationUrl + (navigationUrl.find('?') == std::string::npos ? "?" : "&") + "accept-ui=1"
                : navigationUrl);
#ifdef __APPLE__
            // Navigation is configured; reveal the main window behind the
            // floating splash. The existing ready/error callback dismisses it.
            if (config.connect_url.empty() || ownLoopback) {
                if (auto *native = webview_get_window(static_cast<webview_t>(&window)))
                    [(NSWindow *)native makeKeyAndOrderFront:nil];
            }
#endif
            std::thread watchdog;
            // In ordinary launches a failed navigation or absent JS bridge
            // must not leave a floating splash masking the application forever.
            if (config.accept_ui || config.connect_url.empty() || ownLoopback)
            {
                watchdog = std::thread([&]
                                       {
                const int ticks = config.accept_ui ? 200 : 450;
                for (int i = 0; i < ticks && !finished.load(); ++i) {
                    if (!config.accept_ui && (ready.load() || failed.load())) return;
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
                if (!finished.load()) window.dispatch([&] {
                    splash.dismiss();
                    if (config.accept_ui) window.terminate();
                }); });
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
            splash.dismiss();
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
            if (relaunchRequested.load() && !config.accept_ui && config.connect_url.empty()) {
                instance.release(); // HTTP and worker are already stopped.
                CinterForgePlatformMenus::spawnNewTerminal(config);
            }
            return 0;
        }
        catch (...)
        {
            splash.dismiss();
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
