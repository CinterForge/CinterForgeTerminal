// CinterForgeApplicationPaths.cpp — Native application launch and path policy.
// PURPOSE: Define one place to validate staged Python/HTTP resources, canonical
// Builds ownership, LAN connect URLs and arguments for a child Terminal window.
// METHODS: jsonEscape escapes only JSON string content; validLanUrl restricts
// remote targets to approved local address ranges; validateStaged ensures the
// Python, worker and HTTP service belong to the current staged Builds closure;
// terminalArgs forwards the verified configuration without shell interpolation.
// STARTUP: runApplicationShell calls these before opening its WebView/worker.
// SHUTDOWN: this unit owns no processes or mutable state; CinterForgeTerminal
// remains responsible for stopping the Python service and owned guest VM.
// MAINTENANCE: keep the CinterForgeApplicationPaths API shared by all platforms;
// no source checkout or alternate SDK fallback may be introduced here.
#include "CinterForgeApplicationPaths.h"
#include <filesystem>
#include <regex>
#include <stdexcept>

namespace cinterforge {
namespace fs = std::filesystem;

std::string CinterForgeApplicationPaths::jsonEscape(const std::string &value)
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

// A CinterForge Bonjour service advertises its own HTTP port.
// Connect only to literal LAN addresses in the reserved 20300-20399 range.
bool CinterForgeApplicationPaths::validLanUrl(const std::string &url)
{
    const std::regex urlPattern(R"(^http://(([0-9]{1,3}\.){3}[0-9]{1,3}):(203[0-9]{2})(/[A-Za-z0-9/_-]*)?$)");
    std::smatch match;
    if (!std::regex_match(url, match, urlPattern))
        return false;
    const std::string ip = match[1].str();
    std::vector<int> octets;
    std::size_t start = 0;
    for (int i = 0; i < 4; ++i)
    {
        const std::size_t dot = ip.find('.', start);
        const std::string part = ip.substr(start, dot == std::string::npos ? dot : dot - start);
        if (part.empty() || part.size() > 3)
            return false;
        const int value = std::stoi(part);
        if (value > 255)
            return false;
        octets.push_back(value);
        if (dot == std::string::npos && i != 3)
            return false;
        start = dot == std::string::npos ? ip.size() : dot + 1;
    }
    return octets[0] == 10 || octets[0] == 127 ||
        (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31) ||
        (octets[0] == 192 && octets[1] == 168) ||
        (octets[0] == 169 && octets[1] == 254);
}

void CinterForgeApplicationPaths::validateStaged(const ApplicationShellConfig &config)
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
std::vector<std::string> CinterForgeApplicationPaths::terminalArgs(const ApplicationShellConfig &cfg)
{
    std::vector<std::string> args = {"CinterForgeTerminal", "--python", cfg.python,
                                     "--worker", cfg.worker, "--service", cfg.service,
                                     "--builds", cfg.builds, "--title", cfg.title};
    if (cfg.developer)
        args.push_back("--developer");
    if (!cfg.connect_url.empty()) { args.push_back("--connect"); args.push_back(cfg.connect_url); }
    return args;
}


} // namespace cinterforge
