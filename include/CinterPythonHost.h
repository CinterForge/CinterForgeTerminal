#pragma once

#include <chrono>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace cinterforge {

// Owns one Python worker process. Python code, interpreter, and cwd must be
// resolved from the caller's application bundle or verified Builds/Python
// manifest, never from another source repository at runtime.
class PythonProcess {
public:
    PythonProcess();
    ~PythonProcess();
    PythonProcess(const PythonProcess&) = delete;
    PythonProcess& operator=(const PythonProcess&) = delete;
    PythonProcess(PythonProcess&&) noexcept;
    PythonProcess& operator=(PythonProcess&&) noexcept;

    void start(const std::string& interpreter,
               const std::string& script,
               const std::vector<std::string>& arguments = {});
    void send(const std::string& jsonLine);
    // Returns one complete UTF-8 JSON-lines response; the caller interprets
    // request ids, progress, errors and notifications. Throws on timeout/EOF.
    std::string receive(std::chrono::milliseconds timeout);
    bool running();
    void stop(std::chrono::milliseconds grace = std::chrono::seconds(3));

private:
    struct State;
    std::unique_ptr<State> state_;
};

// Simple linked-library interface. Applications supply paths to a verified,
// bundled interpreter/worker and (optionally) their own RPC handler module.
// The library owns process lifetime, readiness, request IDs and JSON-lines IPC.
// One host instance is used by one calling thread at a time; callbacks execute
// synchronously while start()/call() processes incoming messages.
class PythonHost final {
public:
    using EventHandler = std::function<void(const std::string& jsonEvent)>;

    PythonHost();
    ~PythonHost();
    PythonHost(const PythonHost&) = delete;
    PythonHost& operator=(const PythonHost&) = delete;
    PythonHost(PythonHost&&) = delete;
    PythonHost& operator=(PythonHost&&) = delete;

    void onEvent(EventHandler callback);
    void start(const std::string& interpreter, const std::string& worker,
               const std::string& service = {},
               std::chrono::milliseconds readyTimeout = std::chrono::seconds(5));
    // Returns the JSON-RPC envelope {"id":..., "result":...} or
    // {"id":..., "error":...}; an application-level error is a valid reply.
    std::string call(const std::string& method, const std::string& paramsJson = "{}",
                     std::chrono::milliseconds timeout = std::chrono::seconds(30));
    bool running() const;
    void stop(std::chrono::milliseconds grace = std::chrono::seconds(3));

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace cinterforge
