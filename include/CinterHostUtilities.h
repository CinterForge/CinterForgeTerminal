#pragma once

// Application-neutral primitives extracted from the Steam Python bridge's
// dispatch-thread and Python->C++ message-queue responsibilities. This header
// depends only on the C++17 standard library and does not include Python.h.

#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <future>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace cinterforge {

struct HostMessage {
    uint64_t id = 0;
    std::string source;
    std::string type;
    std::string payload_json;
    std::string target;
    std::string response_json;
};

// The application decides how to interpret a message; no JS, Steam or game
// routing is baked into the queue. The dispatcher may drain it on its UI thread.
class HostMessageQueue {
public:
    void enqueue(HostMessage message) {
        { std::lock_guard<std::mutex> guard(mutex_); messages_.push_back(std::move(message)); }
    }
    bool try_dequeue(HostMessage& output) {
        std::lock_guard<std::mutex> guard(mutex_);
        if (messages_.empty()) return false;
        output = std::move(messages_.front()); messages_.pop_front();
        return true;
    }
    size_t pending() const {
        std::lock_guard<std::mutex> guard(mutex_);
        return messages_.size();
    }
    void clear() { std::lock_guard<std::mutex> guard(mutex_); messages_.clear(); }
private:
    mutable std::mutex mutex_;
    std::deque<HostMessage> messages_;
};

// A dedicated serialized execution thread. Steam currently implements this
// inline in its embedded Python bridge. `scope` can wrap jobs in GIL
// acquire/release; the host itself must not own a CPython global interpreter.
class HostTaskDispatcher {
public:
    using Job = std::function<void()>;
    using JobScope = std::function<void(const Job&)>;
    explicit HostTaskDispatcher(JobScope scope = {}) : scope_(std::move(scope)), thread_([this] { run(); }) {}
    ~HostTaskDispatcher() { stop(); }
    HostTaskDispatcher(const HostTaskDispatcher&) = delete;
    HostTaskDispatcher& operator=(const HostTaskDispatcher&) = delete;

    void post(Job task) {
        {
            std::lock_guard<std::mutex> guard(mutex_);
            if (stopping_) throw std::logic_error("Task dispatcher has stopped");
            queue_.push_back(std::move(task));
        }
        wake_.notify_one();
    }

    template <typename Function>
    auto call(Function&& function) -> decltype(function()) {
        using Result = decltype(function());
        if (std::this_thread::get_id() == thread_.get_id()) return function();
        auto work = std::make_shared<std::packaged_task<Result()>>(std::forward<Function>(function));
        auto answer = work->get_future();
        post([work] { (*work)(); });
        return answer.get();
    }

    void stop() noexcept {
        {
            std::lock_guard<std::mutex> guard(mutex_);
            stopping_ = true;
        }
        wake_.notify_all();
        if (thread_.joinable() && thread_.get_id() != std::this_thread::get_id()) thread_.join();
    }
private:
    void run() {
        for (;;) {
            Job job;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                wake_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
                if (queue_.empty() && stopping_) return;
                job = std::move(queue_.front()); queue_.pop_front();
            }
            try { if (scope_) scope_(job); else job(); }
            catch (...) { /* asynchronous jobs cannot propagate into callers */ }
        }
    }
    JobScope scope_;
    std::mutex mutex_;
    std::condition_variable wake_;
    std::deque<Job> queue_;
    bool stopping_ = false;
    std::thread thread_;
};
} // namespace cinterforge
