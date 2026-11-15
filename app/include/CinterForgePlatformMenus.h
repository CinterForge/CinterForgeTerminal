/*
 * CinterForgePlatformMenus — native OS and WebView integration.
 * PURPOSE: Defines the single platform menu target, new-window process
 * launching, system clipboard and actual browser inspector invocation.
 * METHODS: spawnNewTerminal, windowsClipboardText, installMacFileMenu,
 *          openNativeInspector, and the six Cocoa selector methods below.
 * STARTUP: Called by CinterForgeTerminal.cpp after WebView creation.
 * SHUTDOWN: The menu never owns PythonHost, Docker or app runtime guests.
 * DEPENDENCIES: Existing WebView handle, ApplicationShellConfig, Cocoa/Win32/
 * WebKitGTK. No second window or service is created except explicit New Window.
 */
#pragma once
#include <functional>
#include <string>
#include "CinterApplicationShell.h"
#include "webview/webview.h"
#ifdef __APPLE__
#import <Cocoa/Cocoa.h>
@interface CinterForgeFileMenuTarget : NSObject
{
@public
    std::function<void(const std::string &)> newTerminalAction;
    std::function<void()> quitAction;
    std::function<void()> debuggerAction;
    std::function<void()> copyAction;
    std::function<void()> pasteAction;
    std::function<void(const std::string &)> codeMenuAction;
    NSMenuItem *minecraftItem;
}
- (void)newTerminal:(id)sender;
- (void)quitTerminal:(id)sender;
- (void)openDebugger:(id)sender;
- (void)copyTerminal:(id)sender;
- (void)pasteTerminal:(id)sender;
- (void)dispatchCodeMenu:(id)sender;
@end
#endif

namespace cinterforge {
namespace CinterForgePlatformMenus {
    void spawnNewTerminal(const ApplicationShellConfig &config);
    std::string windowsClipboardText(); // Windows pasteboard bridge only
    void installMacFileMenu(webview::webview &window, const ApplicationShellConfig &config);
    bool openNativeInspector(webview::webview &window);
}
}
