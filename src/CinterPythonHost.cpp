#include "CinterPythonHost.h"
#include "CinterPythonHostC.h"

#include <stdexcept>
#include <exception>
#include <functional>
#include <limits>
#include <utility>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#include <algorithm>
#else
#include <cerrno>
#include <csignal>
#include <cstring>
#include <poll.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace cinterforge {

struct PythonProcess::State {
    std::string pending;
#ifdef _WIN32
    HANDLE process = nullptr;
    HANDLE input = nullptr;     // Parent-side write handle.
    HANDLE output = nullptr;    // Parent-side read handle.
#else
    pid_t child = -1;
    int input = -1;
    int output = -1;
#endif
};

PythonProcess::PythonProcess() : state_(std::make_unique<State>()) {}
PythonProcess::~PythonProcess() { try { stop(); } catch (...) {} }
PythonProcess::PythonProcess(PythonProcess&&) noexcept = default;
PythonProcess& PythonProcess::operator=(PythonProcess&& other) noexcept {
    if (this != &other) {
        try { stop(); } catch (...) {}
        state_ = std::move(other.state_);
    }
    return *this;
}

#ifdef _WIN32
static std::string quote_windows(const std::string& arg) {
    // Follow CommandLineToArgvW quoting rules for Windows arguments.
    if (arg.find_first_of(" \t\"") == std::string::npos && !arg.empty()) return arg;
    std::string result = "\"";
    size_t backslashes = 0;
    for (char c : arg) {
        if (c == '\\') { ++backslashes; continue; }
        if (c == '"') { result.append(backslashes * 2 + 1, '\\'); result.push_back('"'); }
        else { result.append(backslashes, '\\'); result.push_back(c); }
        backslashes = 0;
    }
    result.append(backslashes * 2, '\\');
    return result + "\"";
}
#else
static void close_fd(int& fd) { if (fd >= 0) { ::close(fd); fd = -1; } }
#endif

void PythonProcess::start(const std::string& interpreter,
                          const std::string& script,
                          const std::vector<std::string>& arguments) {
    if (interpreter.empty() || script.empty()) throw std::invalid_argument("Python interpreter/script required");
    if (running()) throw std::logic_error("Python worker is already running");
    if (!state_) state_ = std::make_unique<State>();
    state_->pending.clear();
#ifdef _WIN32
    SECURITY_ATTRIBUTES security {sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    HANDLE childStdin = nullptr, childStdout = nullptr;
    if (!CreatePipe(&childStdin, &state_->input, &security, 0)) throw std::runtime_error("CreatePipe stdin failed");
    if (!SetHandleInformation(state_->input, HANDLE_FLAG_INHERIT, 0)) throw std::runtime_error("stdin inheritance failed");
    if (!CreatePipe(&state_->output, &childStdout, &security, 0)) throw std::runtime_error("CreatePipe stdout failed");
    if (!SetHandleInformation(state_->output, HANDLE_FLAG_INHERIT, 0)) throw std::runtime_error("stdout inheritance failed");
    STARTUPINFOA startup {};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = childStdin;
    startup.hStdOutput = childStdout;
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    std::string command = quote_windows(interpreter) + " -B -u " + quote_windows(script);
    for (const auto& arg : arguments) command += " " + quote_windows(arg);
    std::vector<char> mutableCommand(command.begin(), command.end());
    mutableCommand.push_back('\0');
    PROCESS_INFORMATION info {};
    const BOOL ok = CreateProcessA(interpreter.c_str(), mutableCommand.data(), nullptr, nullptr,
                                   TRUE, CREATE_NO_WINDOW, nullptr, nullptr, &startup, &info);
    CloseHandle(childStdin); CloseHandle(childStdout);
    if (!ok) throw std::runtime_error("CreateProcess Python failed: " + std::to_string(GetLastError()));
    CloseHandle(info.hThread);
    state_->process = info.hProcess;
#else
    int toChild[2], fromChild[2];
    if (pipe(toChild) != 0) throw std::runtime_error("pipe stdin failed");
    if (pipe(fromChild) != 0) {
        close(toChild[0]); close(toChild[1]);
        throw std::runtime_error("pipe stdout failed");
    }
    const pid_t child = fork();
    if (child < 0) {
        close(toChild[0]); close(toChild[1]);close(fromChild[0]);close(fromChild[1]);
        throw std::runtime_error("fork failed");
    }
    if (child == 0) {
        dup2(toChild[0], STDIN_FILENO);
        dup2(fromChild[1], STDOUT_FILENO);
        close(toChild[0]); close(toChild[1]);close(fromChild[0]);close(fromChild[1]);
        std::vector<std::string> values {interpreter, "-B", "-u", script};
        values.insert(values.end(), arguments.begin(), arguments.end());
        std::vector<char*> argv;
        for (auto& value : values) argv.push_back(value.data());
        argv.push_back(nullptr);
        execv(interpreter.c_str(), argv.data());
        _exit(127);
    }
    close(toChild[0]); close(fromChild[1]);
    state_->child = child;
    state_->input = toChild[1]; state_->output = fromChild[0];
#endif
}

bool PythonProcess::running() {
    if (!state_) return false;
#ifdef _WIN32
    if (!state_->process) return false;
    return WaitForSingleObject(state_->process, 0) == WAIT_TIMEOUT;
#else
    if (state_->child <= 0) return false;
    int status = 0;
    const pid_t result = waitpid(state_->child, &status, WNOHANG);
    if (result == 0) return true;
    if (result == state_->child || (result < 0 && errno == ECHILD)) state_->child = -1;
    return false;
#endif
}

void PythonProcess::send(const std::string& jsonLine) {
    if (!state_ || !running()) throw std::runtime_error("Python worker is not running");
    if (jsonLine.find('\n') != std::string::npos || jsonLine.find('\r') != std::string::npos)
        throw std::invalid_argument("Protocol requires one JSON value per line");
    std::string data = jsonLine + "\n";
    size_t offset = 0;
    while (offset < data.size()) {
#ifdef _WIN32
        DWORD written = 0;
        if (!WriteFile(state_->input, data.data() + offset, static_cast<DWORD>(data.size() - offset), &written, nullptr) || !written)
            throw std::runtime_error("Python stdin disconnected");
#else
        const ssize_t written = write(state_->input, data.data() + offset, data.size() - offset);
        if (written <= 0) throw std::runtime_error("Python stdin disconnected");
#endif
        offset += static_cast<size_t>(written);
    }
}

std::string PythonProcess::receive(std::chrono::milliseconds timeout) {
    if (!state_) throw std::runtime_error("Python worker is not initialized");
    const auto until = std::chrono::steady_clock::now() + timeout;
    for (;;) {
        auto end = state_->pending.find('\n');
        if (end != std::string::npos) {
            std::string line = state_->pending.substr(0, end);
            state_->pending.erase(0, end + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            return line;
        }
        auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(until - std::chrono::steady_clock::now());
        if (remaining.count() <= 0) throw std::runtime_error("Python response timed out");
        char data[4096];
#ifdef _WIN32
        DWORD available = 0;
        if (!PeekNamedPipe(state_->output, nullptr, 0, nullptr, &available, nullptr))
            throw std::runtime_error("Python stdout disconnected");
        if (!available) { Sleep(static_cast<DWORD>(std::min<long long>(remaining.count(), 10))); continue; }
        DWORD received = 0;
        if (!ReadFile(state_->output, data, static_cast<DWORD>(std::min<size_t>(sizeof(data), available)), &received, nullptr) || !received)
            throw std::runtime_error("Python stdout disconnected");
#else
        struct pollfd fd {state_->output, POLLIN | POLLHUP, 0};
        int result = poll(&fd, 1, static_cast<int>(remaining.count()));
        if (result == 0) throw std::runtime_error("Python response timed out");
        if (result < 0) { if (errno == EINTR) continue; throw std::runtime_error("Python poll failed"); }
        const ssize_t received = read(state_->output, data, sizeof(data));
        if (received <= 0) throw std::runtime_error("Python stdout closed");
#endif
        state_->pending.append(data, static_cast<size_t>(received));
        if (state_->pending.size() > 8 * 1024 * 1024)
            throw std::runtime_error("Python worker message too large");
    }
}

void PythonProcess::stop(std::chrono::milliseconds grace) {
    if (!state_) return;
#ifdef _WIN32
    if (state_->input) { CloseHandle(state_->input); state_->input = nullptr; }
    if (state_->output) { CloseHandle(state_->output); state_->output = nullptr; }
    if (state_->process) {
        if (WaitForSingleObject(state_->process, static_cast<DWORD>(grace.count())) == WAIT_TIMEOUT)
            TerminateProcess(state_->process, 1);
        WaitForSingleObject(state_->process, INFINITE);
        CloseHandle(state_->process); state_->process = nullptr;
    }
#else
    close_fd(state_->input); close_fd(state_->output);
    if (state_->child > 0) {
        kill(state_->child, SIGTERM);
        const auto deadline = std::chrono::steady_clock::now() + grace;
        int status = 0;
        while (std::chrono::steady_clock::now() < deadline) {
            const auto done = waitpid(state_->child, &status, WNOHANG);
            if (done == state_->child || (done < 0 && errno == ECHILD)) {
                state_->child = -1; break;
            }
            usleep(10000);
        }
        if (state_->child > 0) {
            kill(state_->child, SIGKILL);
            waitpid(state_->child, nullptr, 0);
            state_->child = -1;
        }
    }
#endif
}

// PythonHost intentionally delegates to the already-tested C ABI instead of
// maintaining a second RPC implementation. Neither C++ nor its callers need
// to parse request IDs, drain progress events or manage pipe handles.
struct PythonHost::Impl {
    CPH_Process* process = cph_create();
    EventHandler onEvent;
    std::exception_ptr callbackFailure;

    Impl() {
        if (!process) throw std::runtime_error("Unable to allocate Python host");
        cph_set_event_callback(process, [](const char* line, void* context) {
            auto* self = static_cast<Impl*>(context);
            if (!self->onEvent || self->callbackFailure) return;
            try {
                self->onEvent(line ? std::string(line) : std::string());
            } catch (...) {
                // Never unwind a C++ exception through an exported C callback.
                self->callbackFailure = std::current_exception();
            }
        }, this);
    }
    ~Impl() { cph_destroy(process); }

    void raiseCallbackFailure() {
        if (callbackFailure) {
            auto failure = std::exchange(callbackFailure, nullptr);
            std::rethrow_exception(failure);
        }
    }
    void check(CPH_Status status) {
        if (status != CPH_OK)
            throw std::runtime_error(cph_last_error(process));
        raiseCallbackFailure();
    }
};

static uint32_t timeoutMilliseconds(std::chrono::milliseconds duration) {
    if (duration.count() <= 0 ||
        static_cast<uint64_t>(duration.count()) > std::numeric_limits<uint32_t>::max())
        throw std::invalid_argument("Python host timeout must be between 1ms and UINT32_MAX ms");
    return static_cast<uint32_t>(duration.count());
}

PythonHost::PythonHost() : impl_(std::make_unique<Impl>()) {}
PythonHost::~PythonHost() = default;

void PythonHost::onEvent(EventHandler callback) {
    impl_->onEvent = std::move(callback);
}

void PythonHost::start(const std::string& interpreter, const std::string& worker,
                       const std::string& service, std::chrono::milliseconds readyTimeout) {
    if (interpreter.empty() || worker.empty())
        throw std::invalid_argument("Python host requires interpreter and worker paths");
    const char* arg = service.c_str();
    const char* const* arguments = service.empty() ? nullptr : &arg;
    const auto status = cph_start(impl_->process, interpreter.c_str(), worker.c_str(),
                                  arguments, service.empty() ? 0 : 1,
                                  timeoutMilliseconds(readyTimeout));
    impl_->check(status);
}

std::string PythonHost::call(const std::string& method, const std::string& paramsJson,
                             std::chrono::milliseconds timeout) {
    if (method.empty()) throw std::invalid_argument("Python RPC method must not be empty");
    char* response = nullptr;
    const auto status = cph_call(impl_->process, method.c_str(), paramsJson.c_str(),
                                 timeoutMilliseconds(timeout), &response);
    // C ABI owns the returned memory; the caller owns a regular std::string.
    std::string result;
    if (response) result = response;
    cph_free_string(response);
    impl_->check(status);
    return result;
}

bool PythonHost::running() const {
    return cph_running(impl_->process) != 0;
}

void PythonHost::stop(std::chrono::milliseconds grace) {
    impl_->check(cph_stop(impl_->process, timeoutMilliseconds(grace)));
}

} // namespace cinterforge
