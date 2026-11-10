#include "CinterPythonHostC.h"
#include "CinterPythonHost.h"

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

using Clock = std::chrono::steady_clock;

struct CPH_Process {
    cinterforge::PythonProcess worker;
    CPH_EventCallback callback = nullptr;
    void* callback_user = nullptr;
    uint64_t next_id = 1;
    std::string last_error;
};

namespace {
std::string quote(const std::string& s) {
    std::string out = "\"";
    for (unsigned char c : s) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 32) {
                    static constexpr char hex[] = "0123456789abcdef";
                    out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15];
                } else out += static_cast<char>(c);
        }
    }
    return out + '"';
}

// Extract a top-level JSON member, ignoring nested members and escaped quotes.
// We intentionally do not parse the payload or interpret application data.
std::string member(const std::string& json, const std::string& key) {
    int depth = 0;
    bool quoted = false, escaped = false;
    for (size_t i = 0; i < json.size(); ++i) {
        const char c = json[i];
        if (quoted) {
            if (escaped) { escaped = false; continue; }
            if (c == '\\') { escaped = true; continue; }
            if (c == '"') quoted = false;
            continue;
        }
        if (c == '{' || c == '[') { ++depth; continue; }
        if (c == '}' || c == ']') { --depth; continue; }
        if (c != '"') continue;
        const size_t start = i + 1;
        quoted = true;
        bool escape = false;
        size_t end = start;
        for (; end < json.size(); ++end) {
            if (escape) { escape = false; continue; }
            if (json[end] == '\\') { escape = true; continue; }
            if (json[end] == '"') break;
        }
        if (end == json.size()) return {};
        if (depth == 1 && json.substr(start, end - start) == key) {
            size_t colon = json.find_first_not_of(" \n\r\t", end + 1);
            if (colon != std::string::npos && json[colon] == ':') {
                size_t value = json.find_first_not_of(" \n\r\t", colon + 1);
                if (value == std::string::npos) return {};
                if (json[value] == '"') {
                    size_t close = value + 1;
                    bool esc = false;
                    for (; close < json.size(); ++close) {
                        if (esc) { esc = false; continue; }
                        if (json[close] == '\\') { esc = true; continue; }
                        if (json[close] == '"') break;
                    }
                    return json.substr(value, close - value + (close < json.size()));
                }
                return json.substr(value, json.find_first_of(",}] \n\r\t", value) - value);
            }
        }
        i = end;
        quoted = false;
    }
    return {};
}

CPH_Status error(CPH_Process* h, const std::exception& e) {
    if (!h) return CPH_ERROR;
    h->last_error = e.what();
    return h->last_error.find("timed out") != std::string::npos ? CPH_TIMEOUT : CPH_ERROR;
}

char* owned_string(const std::string& s) {
    if (s.size() >= std::numeric_limits<size_t>::max() - 1) throw std::bad_alloc();
    auto* out = static_cast<char*>(std::malloc(s.size() + 1));
    if (!out) throw std::bad_alloc();
    std::memcpy(out, s.c_str(), s.size() + 1);
    return out;
}

std::chrono::milliseconds remaining(Clock::time_point deadline) {
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now());
    if (ms.count() <= 0) throw std::runtime_error("Python response timed out");
    return ms;
}

void deliver_event(CPH_Process* h, const std::string& line) {
    if (h->callback) h->callback(line.c_str(), h->callback_user);
}
}

extern "C" {
CPH_Process* cph_create(void) { try { return new CPH_Process(); } catch (...) { return nullptr; } }
void cph_destroy(CPH_Process* h) { delete h; }
void cph_set_event_callback(CPH_Process* h, CPH_EventCallback callback, void* user) {
    if (h) { h->callback = callback; h->callback_user = user; }
}
uint32_t cph_api_version(void) { return 1; }
void cph_free_string(char* s) { std::free(s); }
const char* cph_last_error(const CPH_Process* h) { return h ? h->last_error.c_str() : "Null host handle"; }
int cph_running(CPH_Process* h) { return h && h->worker.running(); }
CPH_Status cph_start(CPH_Process* h, const char* python, const char* script,
                     const char* const* args, int argc, uint32_t timeout_ms) {
    if (!h) return CPH_ERROR;
    try {
        h->last_error.clear();
        if (!python || !script || argc < 0 || (argc > 0 && !args))
            throw std::invalid_argument("Interpreter, worker and valid argument vector required");
        std::vector<std::string> argv;
        for (int i = 0; i < argc; ++i) {
            if (!args[i]) throw std::invalid_argument("Null worker argument");
            argv.emplace_back(args[i]);
        }
        h->worker.start(python, script, argv);
        const auto deadline = Clock::now() + std::chrono::milliseconds(timeout_ms);
        for (;;) {
            auto event = h->worker.receive(remaining(deadline));
            deliver_event(h, event);
            if (member(event, "ready") == "true") return CPH_OK;
            if (!member(event, "error").empty()) throw std::runtime_error("Worker readiness failed: " + event);
        }
    } catch (const std::exception& e) { h->worker.stop(); return error(h, e); }
}
CPH_Status cph_call(CPH_Process* h, const char* method, const char* params_json,
                    uint32_t timeout_ms, char** output) {
    if (output) *output = nullptr;
    if (!h) return CPH_ERROR;
    try {
        h->last_error.clear();
        if (!output || !method || !*method) throw std::invalid_argument("Method and output required");
        const auto id = h->next_id++;
        const std::string params = params_json && *params_json ? params_json : "{}";
        h->worker.send("{\"id\":" + std::to_string(id) + ",\"method\":" + quote(method) + ",\"params\":" + params + "}");
        const auto deadline = Clock::now() + std::chrono::milliseconds(timeout_ms);
        for (;;) {
            const auto line = h->worker.receive(remaining(deadline));
            if (member(line, "id") == std::to_string(id)) {
                *output = owned_string(line);
                return CPH_OK; // Application-level JSON error is still a valid reply.
            }
            if (!member(line, "id").empty())
                throw std::runtime_error("Unexpected Python response ID: " + line);
            deliver_event(h, line);
        }
    } catch (const std::exception& e) { return error(h, e); }
}
CPH_Status cph_send(CPH_Process* h, const char* line) {
    if (!h) return CPH_ERROR;
    try {
        h->last_error.clear();
        if (!line) throw std::invalid_argument("JSON line required");
        h->worker.send(line);
        return CPH_OK;
    } catch (const std::exception& e) { return error(h, e); }
}
CPH_Status cph_receive(CPH_Process* h, uint32_t timeout_ms, char** output) {
    if (output) *output = nullptr;
    if (!h) return CPH_ERROR;
    try {
        h->last_error.clear();
        if (!output) throw std::invalid_argument("Output buffer required");
        *output = owned_string(h->worker.receive(std::chrono::milliseconds(timeout_ms)));
        return CPH_OK;
    } catch (const std::exception& e) { return error(h, e); }
}
CPH_Status cph_stop(CPH_Process* h, uint32_t grace_ms) {
    if (!h) return CPH_ERROR;
    try {
        h->last_error.clear();
        if (h->worker.running()) {
            // Best effort graceful protocol shutdown, then OS termination.
            char* response = nullptr;
            (void)cph_call(h, "shutdown", "{}", grace_ms, &response);
            cph_free_string(response);
        }
        h->worker.stop(std::chrono::milliseconds(grace_ms));
        return CPH_OK;
    } catch (const std::exception& e) { return error(h, e); }
}
}
