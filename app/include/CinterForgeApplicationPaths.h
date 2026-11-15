#pragma once
// CinterForgeApplicationPaths.h — contract for native launch configuration.
// PURPOSE: Keep path/URL validation and safe child process arguments separate
// from platform-native menus, WebView bindings and HTTP service ownership.
// METHODS: jsonEscape, validLanUrl, validateStaged, terminalArgs are the only
// public helpers; none provisions tools or creates new guest VM instances.
// STARTUP: called from the one CinterForgeTerminal application shell.
// SHUTDOWN: no resources owned; the native shell remains lifecycle owner.
#include "CinterApplicationShell.h"
#include <string>
#include <vector>

namespace cinterforge {
struct CinterForgeApplicationPaths final {
    static std::string jsonEscape(const std::string &value);
    static bool validLanUrl(const std::string &url);
    static void validateStaged(const ApplicationShellConfig &config);
    static std::vector<std::string> terminalArgs(const ApplicationShellConfig &config);
};
} // namespace cinterforge
