#include "CinterPythonHost.h"
#include <chrono>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: host_smoke <absolute-python> <worker.py>\n";
        return 2;
    }
    try {
        cinterforge::PythonProcess worker;
        worker.start(argv[1], argv[2]);
        auto ready = worker.receive(std::chrono::seconds(5));
        if (ready.find("\"ready\": true") == std::string::npos) throw std::runtime_error("missing ready event: " + ready);
        worker.send("{\"id\":1,\"method\":\"ping\",\"params\":{}}");
        auto result = worker.receive(std::chrono::seconds(5));
        if (result.find("\"id\": 1") == std::string::npos || result.find("pong") == std::string::npos)
            throw std::runtime_error("incorrect ping response: " + result);
        worker.stop();

        // Application-facing API: no C handles, request IDs or JSONL parsing.
        cinterforge::PythonHost host;
        int readyEvents = 0;
        host.onEvent([&](const std::string& event) {
            if (event.find("\"ready\": true") != std::string::npos) ++readyEvents;
        });
        host.start(argv[1], argv[2]);
        if (!host.running() || readyEvents != 1)
            throw std::runtime_error("High-level host did not deliver the ready event");
        auto answer = host.call("ping");
        if (answer.find("pong") == std::string::npos || answer.find("\"id\": 1") == std::string::npos)
            throw std::runtime_error("High-level host returned an incorrect response: " + answer);
        host.stop();
        if (host.running()) throw std::runtime_error("High-level host failed to stop");
        std::cout << "CinterPythonHost raw + high-level RPC smoke passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
