#include "CinterPythonHostC.h"
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

static void print_event(const char* event, void*) { std::cerr << "[CPH event] " << event << '\n'; }
int main(int argc, char** argv) {
    try {
        std::string python, worker, service, method = "ping", params = "{}";
        uint32_t timeout = 30000;
        for (int i = 1; i < argc; ++i) {
            const std::string flag = argv[i];
            if (flag == "--help" || flag == "-h") {
                std::cout << "cinter-python-host --python <absolute interpreter> --worker <worker.py> "
                          << "[--service <module.py>] [--method <RPC method>] [--params <JSON>] [--timeout-ms N]\n";
                return 0;
            }
            if (i + 1 == argc) throw std::invalid_argument("Missing value for " + flag);
            const std::string value = argv[++i];
            if (flag == "--python") python = value;
            else if (flag == "--worker") worker = value;
            else if (flag == "--service") service = value;
            else if (flag == "--method") method = value;
            else if (flag == "--params") params = value;
            else if (flag == "--timeout-ms") timeout = static_cast<uint32_t>(std::stoul(value));
            else throw std::invalid_argument("Unknown option: " + flag);
        }
        if (python.empty() || worker.empty()) throw std::invalid_argument("--python and --worker are required");
        CPH_Process* host = cph_create();
        if (!host) throw std::runtime_error("Unable to allocate Python host");
        cph_set_event_callback(host, print_event, nullptr);
        const char* argv_service[] = {service.c_str()};
        const auto started = cph_start(host, python.c_str(), worker.c_str(),
                                       service.empty() ? nullptr : argv_service,
                                       service.empty() ? 0 : 1, timeout);
        if (started != CPH_OK) {
            std::cerr << "[CPH] " << cph_last_error(host) << '\n';
            cph_destroy(host); return 1;
        }
        char* response = nullptr;
        const auto result = cph_call(host, method.c_str(), params.c_str(), timeout, &response);
        if (result == CPH_OK && response) std::cout << response << '\n';
        else std::cerr << "[CPH] " << cph_last_error(host) << '\n';
        cph_free_string(response);
        cph_stop(host, 2000);
        cph_destroy(host);
        return result == CPH_OK ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << "[CPH] " << e.what() << '\n';
        return 2;
    }
}
