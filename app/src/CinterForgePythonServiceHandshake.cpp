// CinterForgePythonServiceHandshake.cpp — the existing native HTTP readiness contract.
// PURPOSE: Parse the already-authenticated JSONL PythonHost start response.
// METHODS: readyUrl requires one loopback HTTP URL without RPC error; the
// nativeBootstrapUrl method extracts the private native grant and constructs
// only the existing local /_native_open URL. Both reject malformed input.
// STARTUP: used by the single runApplicationShell path after worker startup.
// SHUTDOWN: stateless; the native application retains process ownership.
// SECURITY: never log the native grant or treat a remote URL as authenticated.
#include "CinterForgePythonServiceHandshake.h"
#include <regex>
#include <stdexcept>

namespace cinterforge {
std::string CinterForgePythonServiceHandshake::nativeBootstrapUrl(const std::string &rpc, const std::string &base)
{
    // The private Host RPC returns this grant only to a local native
    // launch. Never print it, persist it, or expose it to remote windows.
    static const std::regex grant(R"re("native_grant"\s*:\s*"([A-Za-z0-9_-]{40,128})")re");
    std::smatch match;
    if (!std::regex_search(rpc, match, grant))
        throw std::runtime_error("Native Terminal startup grant is missing");
    return base + "_native_open?key=" + match[1].str();
}

std::string CinterForgePythonServiceHandshake::readyUrl(const std::string &rpc)
{
    // An application RPC error is a valid JSONL response. Never navigate to
    // arbitrary/untrusted URLs from it or accept a forged readiness message.
    static const std::regex url(R"re("url"\s*:\s*"(http://127\.0\.0\.1:[0-9]{1,5}/?)")re");
    std::smatch match;
    if (rpc.find("\"error\"") != std::string::npos || !std::regex_search(rpc, match, url))
        throw std::runtime_error("Hosted Python HTTP service did not return a loopback URL: " + rpc);
    return match[1];
}

} // namespace cinterforge
