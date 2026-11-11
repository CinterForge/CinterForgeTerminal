#include "CinterZeroconf.h"

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <thread>
#include <iostream>
#include <stdexcept>
#include <string>

#ifndef _WIN32
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

// Keep an independent port-5353 listener alive while creating our contexts.
// This reproduces the essential Mac Bonjour port-sharing requirement and
// catches regressions that configure SO_REUSEADDR without SO_REUSEPORT.
#ifndef _WIN32
class ExistingMdnsListener
{
public:
    ExistingMdnsListener()
    {
        socket_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (socket_ < 0)
            throw std::runtime_error("Cannot create shared-port test socket");
        int yes = 1;
        if (::setsockopt(socket_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) != 0)
        {
            ::close(socket_);
            throw std::runtime_error("Cannot enable SO_REUSEADDR on test listener");
        }
#if defined(SO_REUSEPORT)
        if (::setsockopt(socket_, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes)) != 0)
        {
            ::close(socket_);
            throw std::runtime_error("Cannot enable SO_REUSEPORT on test listener");
        }
#endif
        sockaddr_in local{};
        local.sin_family = AF_INET;
        local.sin_addr.s_addr = htonl(INADDR_ANY);
        local.sin_port = htons(5353);
        if (::bind(socket_, reinterpret_cast<const sockaddr *>(&local), sizeof(local)) != 0)
        {
            ::close(socket_);
            throw std::runtime_error("Cannot reserve UDP 5353 for shared-port test");
        }
    }
    ~ExistingMdnsListener() { ::close(socket_); }
    ExistingMdnsListener(const ExistingMdnsListener &) = delete;
    ExistingMdnsListener &operator=(const ExistingMdnsListener &) = delete;

private:
    int socket_ = -1;
};
#endif

namespace
{
    volatile std::sig_atomic_t keepRunning = 1;
    void onSignal(int) { keepRunning = 0; }

    int runLanCommand(int argc, char **argv)
    {
        const std::string action(argv[1]);
        std::string serviceName = "CinterForge Python Host";
        std::string type = "_Cinter._tcp.";
        int port = 20300;
        int seconds = 10;
        for (int i = 2; i < argc; ++i)
        {
            const std::string flag(argv[i]);
            if (i + 1 >= argc)
                throw std::invalid_argument("Missing value for " + flag);
            const std::string value(argv[++i]);
            if (flag == "--name")
                serviceName = value;
            else if (flag == "--type")
                type = value;
            else if (flag == "--port")
                port = std::stoi(value);
            else if (flag == "--seconds")
                seconds = std::stoi(value);
            else
                throw std::invalid_argument("Unknown option: " + flag);
        }
        if (port < 1 || port > 65535 || seconds < 1 || seconds > 3600)
            throw std::invalid_argument("Invalid port or browse duration");
        cinterforge::Zeroconf zeroconf;
        if (action == "--publish")
        {
            zeroconf.publish(serviceName, type, static_cast<uint16_t>(port));
            std::signal(SIGINT, onSignal);
            std::signal(SIGTERM, onSignal);
            std::cout << "[CPH Zeroconf] Advertising '" << serviceName << "' as "
                      << type << " on TCP " << port << std::endl;
            while (keepRunning)
                zeroconf.poll(std::chrono::milliseconds(250));
            zeroconf.stop(); // Send mDNS goodbye before exit.
            return 0;
        }
        if (action == "--browse")
        {
            zeroconf.browse(type, [](CZC_Event event, const CZC_Service &service)
                            {
            if (event == CZC_REMOVED) return;
            std::cout << "[CPH Zeroconf] " << service.name << " http://"
                      << service.address << ':' << service.port << "/" << std::endl; });
            std::cout << "[CPH Zeroconf] Browsing " << type << " for "
                      << seconds << "s" << std::endl;
            const auto until = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
            while (std::chrono::steady_clock::now() < until)
                zeroconf.poll(std::chrono::milliseconds(200));
            return 0;
        }
        throw std::invalid_argument("Use --publish or --browse");
    }
} // namespace

// A local two-client multicast test. It exercises a real PTR/SRV/A exchange,
// including source expiration on goodbye. Does not require a Python process.
int main(int argc, char **argv)
{
    try
    {
        if (argc > 1)
            return runLanCommand(argc, argv);
#ifndef _WIN32
        ExistingMdnsListener existingListener;
#endif
        cinterforge::Zeroconf publisher;
        cinterforge::Zeroconf browser;
        bool arrived = false;
        bool departed = false;
        browser.browse("_cphsmoke._tcp.", [&](CZC_Event event, const CZC_Service &svc)
                       {
            if (std::string(svc.name) != "CPH-Zeroconf-Test") return;
            if (event == CZC_ADDED && svc.port == 20380 && svc.address[0]) arrived = true;
            if (event == CZC_REMOVED) departed = true; });
        publisher.publish("CPH-Zeroconf-Test", "_cphsmoke._tcp.", 20380);
        for (int i = 0; i < 100 && !arrived; ++i)
        {
            publisher.poll(std::chrono::milliseconds(15));
            browser.poll(std::chrono::milliseconds(15));
        }
        if (!arrived)
            throw std::runtime_error("mDNS browse did not resolve a local publication");
        publisher.unpublish();
        for (int i = 0; i < 100 && !departed; ++i)
        {
            publisher.poll(std::chrono::milliseconds(15));
            browser.poll(std::chrono::milliseconds(15));
        }
        if (!departed)
            throw std::runtime_error("mDNS goodbye did not remove the service");
        std::cout << "DNS-SD publish, browse, resolve and goodbye OK\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "DNS-SD test failure: " << error.what() << '\n';
        return 1;
    }
}
