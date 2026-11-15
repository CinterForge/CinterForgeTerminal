#pragma once
// CinterForgePythonServiceHandshake.h — private native/Python startup boundary.
// PURPOSE: Declare the two existing URL/grant decoders used after Host RPC start.
// METHODS: readyUrl extracts the loopback HTTP listener and nativeBootstrapUrl
// combines it with a verified private native launch grant. No HTTP calls here.
// STARTUP: one caller in the existing CinterForgeTerminal application shell.
// SHUTDOWN: no mutable state, VM or HTTP listener ownership.
#include <string>
namespace cinterforge {
struct CinterForgePythonServiceHandshake final {
    static std::string nativeBootstrapUrl(const std::string &rpc, const std::string &base);
    static std::string readyUrl(const std::string &rpc);
};
}
