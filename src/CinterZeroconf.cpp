#include "CinterZeroconf.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <map>
#include <string>
#include <utility>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
using Socket = SOCKET;
static constexpr Socket badSocket = INVALID_SOCKET;
static void closeSocket(Socket s) { if (s != badSocket) closesocket(s); }
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
using Socket = int;
static constexpr Socket badSocket = -1;
static void closeSocket(Socket s) { if (s != badSocket) close(s); }
#endif

namespace {

using Clock = std::chrono::steady_clock;
constexpr unsigned short kMdnsPort = 5353;
constexpr const char* kMdnsGroup = "224.0.0.251";
constexpr uint32_t kRecordTtl = 120;
constexpr size_t kMaxPacket = 9000;

// czc_create() has no context to own an error if socket initialization fails.
// Keep the last creation error per caller thread; the C++ wrapper includes it
// in its exception, and C users can query it after a NULL return.
thread_local std::string creationError;

std::string socketError(const char* operation) {
#ifdef _WIN32
    return std::string(operation) + " (Winsock error " + std::to_string(WSAGetLastError()) + ")";
#else
    const int code = errno;  // Capture before allocating/formatting strings.
    return std::string(operation) + ": " + std::strerror(code) +
           " (errno " + std::to_string(code) + ")";
#endif
}

std::string lower(std::string input) {
    std::transform(input.begin(), input.end(), input.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return input;
}

bool labelValid(const std::string& label) {
    if (label.empty() || label.size() > 63) return false;
    for (unsigned char c : label) {
        if (!(std::isalnum(c) || c == '-' || c == '_')) return false;
    }
    return true;
}

// DNS-SD type input can omit .local., but never points to an arbitrary domain.
std::string normalizeType(const char* raw) {
    if (!raw) return {};
    std::string type(raw);
    if (type.empty() || type.size() > 150) return {};
    if (type.back() != '.') type += '.';
    const std::string folded = lower(type);
    if (folded.size() < 7 || folded.rfind(".local.") != folded.size() - 7)
        type += "local.";
    const size_t first = type.find('.');
    const size_t second = type.find('.', first == std::string::npos ? first : first + 1);
    if (first == std::string::npos || second == std::string::npos || first < 2) return {};
    const std::string service = type.substr(0, first);
    const std::string protocol = lower(type.substr(first + 1, second - first - 1));
    if (service.front() != '_' || !labelValid(service) ||
        (protocol != "_tcp" && protocol != "_udp") ||
        lower(type.substr(second)) != ".local.") return {};
    return type;
}

bool validInstanceName(const char* name) {
    if (!name || !*name) return false;
    const size_t length = std::strlen(name);
    if (length > 63) return false;
    for (size_t i = 0; i < length; ++i) {
        const unsigned char c = static_cast<unsigned char>(name[i]);
        if (c == '.' || c < 32 || c == 127) return false;
    }
    return true;
}

void write16(std::vector<uint8_t>& p, uint16_t n) {
    p.push_back(static_cast<uint8_t>(n >> 8));
    p.push_back(static_cast<uint8_t>(n));
}
void write32(std::vector<uint8_t>& p, uint32_t n) {
    write16(p, static_cast<uint16_t>(n >> 16));
    write16(p, static_cast<uint16_t>(n));
}
uint16_t read16(const uint8_t* p) {
    return static_cast<uint16_t>((p[0] << 8) | p[1]);
}
uint32_t read32(const uint8_t* p) {
    return (static_cast<uint32_t>(read16(p)) << 16) | read16(p + 2);
}

bool writeName(std::vector<uint8_t>& bytes, const std::string& domain) {
    size_t start = 0;
    while (start < domain.size()) {
        const size_t dot = domain.find('.', start);
        if (dot == std::string::npos) return false;
        const size_t length = dot - start;
        if (!length || length > 63 || bytes.size() + length + 2 >= kMaxPacket) return false;
        bytes.push_back(static_cast<uint8_t>(length));
        bytes.insert(bytes.end(), domain.begin() + static_cast<std::ptrdiff_t>(start),
                     domain.begin() + static_cast<std::ptrdiff_t>(dot));
        start = dot + 1;
    }
    bytes.push_back(0);
    return true;
}

// Supports compressed DNS names with loop/bounds protection. offset advances
// only through the original field, not into compression-pointer targets.
bool readName(const uint8_t* bytes, size_t size, size_t& offset, std::string& result) {
    size_t cursor = offset;
    bool jumped = false;
    unsigned hops = 0;
    result.clear();
    while (cursor < size && ++hops <= 128) {
        const uint8_t length = bytes[cursor++];
        if (length == 0) {
            if (!jumped) offset = cursor;
            return true;
        }
        if ((length & 0xc0) == 0xc0) {
            if (cursor >= size) return false;
            const size_t target = ((length & 0x3f) << 8) | bytes[cursor++];
            if (target >= size) return false;
            if (!jumped) offset = cursor;
            cursor = target;
            jumped = true;
            continue;
        }
        if (length > 63 || cursor + length > size || result.size() + length + 1 > 255)
            return false;
        result.append(reinterpret_cast<const char*>(bytes + cursor), length);
        result.push_back('.');
        cursor += length;
    }
    return false;
}

struct Record {
    std::string name;
    uint16_t type = 0;
    uint32_t ttl = 0;
    std::string domain;
    std::string address;
    uint16_t port = 0;
};

// Do not act on a partial or malformed DNS response. The end of each RR is
// checked before any field is decoded, including compressed SRV/PTR names.
bool parseRecords(const uint8_t* bytes, size_t size, std::vector<Record>& records,
                  std::vector<std::pair<std::string, uint16_t>>& questions, bool& response) {
    if (size < 12) return false;
    response = (bytes[2] & 0x80) != 0;
    const size_t questionCount = read16(bytes + 4);
    const size_t recordCount = read16(bytes + 6) + read16(bytes + 8) + read16(bytes + 10);
    if (questionCount > 64 || recordCount > 256) return false;
    size_t offset = 12;
    for (size_t i = 0; i < questionCount; ++i) {
        std::string name;
        if (!readName(bytes, size, offset, name) || offset + 4 > size) return false;
        const uint16_t type = read16(bytes + offset);
        offset += 4;
        questions.emplace_back(lower(name), type);
    }
    for (size_t i = 0; i < recordCount; ++i) {
        Record record;
        if (!readName(bytes, size, offset, record.name) || offset + 10 > size) return false;
        record.type = read16(bytes + offset);
        record.ttl = read32(bytes + offset + 4);
        const size_t length = read16(bytes + offset + 8);
        offset += 10;
        if (offset + length > size) return false;
        const size_t end = offset + length;
        if (record.type == 12 && length > 0) {
            if (!readName(bytes, size, offset, record.domain) || offset != end) return false;
        } else if (record.type == 33 && length >= 7) {
            record.port = read16(bytes + offset + 4);
            offset += 6;
            if (!readName(bytes, size, offset, record.domain) || offset != end) return false;
        } else if (record.type == 1 && length == 4) {
            char ip[INET_ADDRSTRLEN] = {};
            if (!inet_ntop(AF_INET, bytes + offset, ip, sizeof(ip))) return false;
            record.address = ip;
        }
        offset = end;
        records.push_back(std::move(record));
    }
    return true;
}

void writeRecord(std::vector<uint8_t>& packet, const std::string& name,
                 uint16_t type, uint16_t dnsClass, uint32_t ttl,
                 const std::vector<uint8_t>& data) {
    writeName(packet, name);
    write16(packet, type);
    write16(packet, dnsClass);
    write32(packet, ttl);
    write16(packet, static_cast<uint16_t>(data.size()));
    packet.insert(packet.end(), data.begin(), data.end());
}

struct Discovered {
    std::string name;
    std::string fullName;
    std::string host;
    std::string address;
    uint16_t port = 0;
    bool seenPtr = false;
    bool delivered = false;
    std::string priorHost;
    std::string priorAddress;
    uint16_t priorPort = 0;
    Clock::time_point expires = Clock::now();
};

} // namespace

struct CZC_Context {
    Socket socket = badSocket;
    sockaddr_in multicast {};
    std::string lastError;
    std::string publishedName;
    std::string publishedType;
    std::string localHost;
    std::string localAddress;
    uint16_t publishedPort = 0;
    std::string browseType;
    CZC_ServiceCallback callback = nullptr;
    void* callbackData = nullptr;
    Clock::time_point nextQuery = Clock::now();
    Clock::time_point nextAnnouncement = Clock::now();
    std::map<std::string, Discovered> services;
    std::map<std::string, std::string> addresses;

    bool fail(const std::string& why) {
        lastError = why;
        return false;
    }
    bool sendPacket(const std::vector<uint8_t>& bytes) {
        const int sent = sendto(socket, reinterpret_cast<const char*>(bytes.data()),
                                static_cast<int>(bytes.size()), 0,
                                reinterpret_cast<sockaddr*>(&multicast), sizeof(multicast));
        return sent == static_cast<int>(bytes.size()) || fail("Unable to send mDNS datagram");
    }
    bool announce(uint32_t ttl) {
        if (publishedName.empty()) return true;
        const std::string fullName = publishedName + "." + publishedType;
        std::vector<uint8_t> p(12, 0);
        p[2] = 0x84; // Response + authoritative answer.
        p[7] = 4;    // PTR, SRV, TXT, A.
        std::vector<uint8_t> ptr, srv, txt {0}, a(4);
        if (!writeName(ptr, fullName)) return fail("Invalid DNS-SD instance name");
        write16(srv, 0); write16(srv, 0); write16(srv, publishedPort);
        if (!writeName(srv, localHost)) return fail("Invalid local host name");
        in_addr ip {};
        if (inet_pton(AF_INET, localAddress.c_str(), &ip) != 1) return fail("Invalid local IPv4 address");
        std::memcpy(a.data(), &ip, 4);
        writeRecord(p, publishedType, 12, 1, ttl, ptr);
        writeRecord(p, fullName, 33, 0x8001, ttl, srv);
        writeRecord(p, fullName, 16, 0x8001, ttl, txt);
        writeRecord(p, localHost, 1, 0x8001, ttl, a);
        return sendPacket(p);
    }
    bool query() {
        if (browseType.empty()) return true;
        std::vector<uint8_t> p(12, 0);
        p[5] = 1;
        if (!writeName(p, browseType)) return fail("Invalid DNS-SD service type");
        write16(p, 12); write16(p, 1); // PTR, IN.
        return sendPacket(p);
    }
    int emit(CZC_Event event, const Discovered& service) {
        if (!callback) return 0;
        const CZC_Service value {service.name.c_str(), browseType.c_str(), service.host.c_str(),
                                 service.address.c_str(), service.port};
        callback(event, &value, callbackData);
        return 1;
    }
    int process(const uint8_t* bytes, size_t size) {
        std::vector<Record> records;
        std::vector<std::pair<std::string, uint16_t>> questions;
        bool response = false;
        if (!parseRecords(bytes, size, records, questions, response)) return 0;
        if (!response) {
            if (!publishedName.empty()) {
                const std::string full = lower(publishedName + "." + publishedType);
                const std::string type = lower(publishedType);
                for (const auto& q : questions) {
                    if ((q.first == type && (q.second == 12 || q.second == 255)) ||
                        (q.first == full && (q.second == 33 || q.second == 16 || q.second == 255)) ||
                        (q.first == lower(localHost) && (q.second == 1 || q.second == 255))) {
                        announce(kRecordTtl);
                        break;
                    }
                }
            }
            return 0;
        }
        if (browseType.empty()) return 0;
        const auto now = Clock::now();
        const auto type = lower(browseType);
        int callbacks = 0;
        // First collect PTR/SRV and address records, regardless of RR order.
        for (const auto& record : records) {
            if (record.type == 1 && !record.address.empty() && record.ttl > 0)
                addresses[lower(record.name)] = record.address;
            if (record.type != 12 || lower(record.name) != type || record.domain.empty()) continue;
            const std::string key = lower(record.domain);
            if (key.size() <= type.size() + 1 || key.substr(key.size() - type.size()) != type ||
                key[key.size() - type.size() - 1] != '.') continue;
            if (record.ttl == 0) {
                const auto found = services.find(key);
                if (found != services.end()) {
                    if (found->second.delivered) callbacks += emit(CZC_REMOVED, found->second);
                    services.erase(found);
                }
                continue;
            }
            auto& entry = services[key];
            entry.fullName = record.domain;
            entry.name = record.domain.substr(0, record.domain.size() - type.size() - 1);
            entry.seenPtr = true;
            entry.expires = now + std::chrono::seconds(record.ttl);
        }
        for (const auto& record : records) {
            if (record.type != 33) continue;
            auto found = services.find(lower(record.name));
            if (found == services.end()) continue;
            if (record.ttl == 0) {
                if (found->second.delivered) callbacks += emit(CZC_REMOVED, found->second);
                services.erase(found);
                continue;
            }
            found->second.host = record.domain;
            found->second.port = record.port;
            found->second.expires = now + std::chrono::seconds(record.ttl);
        }
        for (auto& item : services) {
            auto& service = item.second;
            const auto address = addresses.find(lower(service.host));
            if (address != addresses.end()) service.address = address->second;
            if (!service.seenPtr || !service.port || service.address.empty()) continue;
            if (!service.delivered || service.priorAddress != service.address ||
                service.priorHost != service.host || service.priorPort != service.port) {
                callbacks += emit(service.delivered ? CZC_UPDATED : CZC_ADDED, service);
                service.delivered = true;
                service.priorAddress = service.address;
                service.priorHost = service.host;
                service.priorPort = service.port;
            }
        }
        return callbacks;
    }
};

extern "C" {

CZC_Context* czc_create(void) {
    creationError.clear();
#ifdef _WIN32
    WSADATA data {};
    const int startupStatus = WSAStartup(MAKEWORD(2, 2), &data);
    if (startupStatus != 0) {
        creationError = "WSAStartup failed (error " + std::to_string(startupStatus) + ")";
        return nullptr;
    }
#endif
    CZC_Context* context = new CZC_Context;
    context->socket = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    bool ok = context->socket != badSocket;
    if (!ok) creationError = socketError("Create IPv4 mDNS UDP socket");
    if (ok) {
        int reuse = 1;
        // mDNSResponder/Bonjour normally already owns UDP 5353 on macOS.
        // RFC 6762 section 15.1 recommends port reuse for coexisting
        // responders. Darwin requires BOTH SO_REUSEADDR and SO_REUSEPORT
        // before bind(); SO_REUSEADDR alone can fail with EADDRINUSE.
        if (setsockopt(context->socket, SOL_SOCKET, SO_REUSEADDR,
                       reinterpret_cast<const char*>(&reuse), sizeof(reuse)) != 0) {
            creationError = socketError("Set SO_REUSEADDR on UDP 5353");
            ok = false;
        }
#if defined(SO_REUSEPORT) && !defined(_WIN32)
        if (ok && setsockopt(context->socket, SOL_SOCKET, SO_REUSEPORT,
                             reinterpret_cast<const char*>(&reuse), sizeof(reuse)) != 0) {
            creationError = socketError("Set SO_REUSEPORT on UDP 5353");
            ok = false;
        }
#endif
        if (ok) {
            sockaddr_in local {};
            local.sin_family = AF_INET;
            local.sin_port = htons(kMdnsPort);
            local.sin_addr.s_addr = htonl(INADDR_ANY);
            if (bind(context->socket, reinterpret_cast<const sockaddr*>(&local), sizeof(local)) != 0) {
                // Include the OS errno rather than a generic initialization
                // failure. On macOS, port 5353 is normally in use by
                // mDNSResponder; SO_REUSEADDR and SO_REUSEPORT are required.
                creationError = socketError("Bind mDNS UDP port 5353");
                ok = false;
            }
        }
    }
    if (ok) {
        ip_mreq group {};
        inet_pton(AF_INET, kMdnsGroup, &group.imr_multiaddr);
        group.imr_interface.s_addr = htonl(INADDR_ANY);
        if (setsockopt(context->socket, IPPROTO_IP, IP_ADD_MEMBERSHIP,
                       reinterpret_cast<const char*>(&group), sizeof(group)) != 0) {
            creationError = socketError("Join IPv4 mDNS multicast group 224.0.0.251");
            ok = false;
        }
    }
    if (ok) {
        const unsigned char ttl = 255; // RFC 6762 mDNS hop limit.
        const unsigned char loop = 1;  // Local browse/publish smoke test.
        if (setsockopt(context->socket, IPPROTO_IP, IP_MULTICAST_TTL,
                       reinterpret_cast<const char*>(&ttl), sizeof(ttl)) != 0 ||
            setsockopt(context->socket, IPPROTO_IP, IP_MULTICAST_LOOP,
                       reinterpret_cast<const char*>(&loop), sizeof(loop)) != 0) {
            creationError = socketError("Configure IPv4 mDNS multicast sender");
            ok = false;
        }
        context->multicast.sin_family = AF_INET;
        context->multicast.sin_port = htons(kMdnsPort);
        inet_pton(AF_INET, kMdnsGroup, &context->multicast.sin_addr);
    }
    if (!ok) {
        closeSocket(context->socket);
        delete context;
#ifdef _WIN32
        WSACleanup();
#endif
        return nullptr;
    }
    // A temporary UDP socket selects the system's default multicast-capable
    // interface; no remote Internet connection or network request is needed.
    Socket probe = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (probe != badSocket) {
        connect(probe, reinterpret_cast<const sockaddr*>(&context->multicast),
                sizeof(context->multicast));
        sockaddr_in assigned {};
#ifdef _WIN32
        int length = sizeof(assigned);
#else
        socklen_t length = sizeof(assigned);
#endif
        if (getsockname(probe, reinterpret_cast<sockaddr*>(&assigned), &length) == 0) {
            char ip[INET_ADDRSTRLEN] {};
            if (inet_ntop(AF_INET, &assigned.sin_addr, ip, sizeof(ip))) context->localAddress = ip;
        }
        closeSocket(probe);
    }
    char hostname[256] {};
    if (gethostname(hostname, sizeof(hostname) - 1) == 0) {
        std::string label(hostname);
        const auto dot = label.find('.');
        if (dot != std::string::npos) label.resize(dot);
        for (char& c : label) {
            if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '-')) c = '-';
        }
        if (label.empty()) label = "cinterforge";
        if (label.size() > 63) label.resize(63);
        context->localHost = label + ".local.";
    } else {
        context->localHost = "cinterforge.local.";
    }
    return context;
}

void czc_unpublish(CZC_Context* context) {
    if (!context) return;
    if (!context->publishedName.empty()) context->announce(0);
    context->publishedName.clear();
    context->publishedType.clear();
    context->publishedPort = 0;
}

void czc_stop_browse(CZC_Context* context) {
    if (!context) return;
    context->browseType.clear();
    context->callback = nullptr;
    context->callbackData = nullptr;
    context->services.clear();
    context->addresses.clear();
}

void czc_stop(CZC_Context* context) {
    czc_unpublish(context);
    czc_stop_browse(context);
}

void czc_destroy(CZC_Context* context) {
    if (!context) return;
    czc_stop(context);
    closeSocket(context->socket);
    delete context;
#ifdef _WIN32
    WSACleanup();
#endif
}

int czc_publish(CZC_Context* context, const char* name, const char* type, uint16_t port) {
    if (!context) return -1;
    context->lastError.clear();
    const std::string normalized = normalizeType(type);
    if (!validInstanceName(name) || normalized.empty() || port == 0) {
        context->fail("Publish requires a valid instance, DNS-SD type and nonzero port");
        return -1;
    }
    if (context->localAddress.empty() || context->localAddress == "0.0.0.0") {
        context->fail("No IPv4 multicast interface available for publishing");
        return -1;
    }
    if (!context->publishedName.empty()) context->announce(0);
    context->publishedName = name;
    context->publishedType = normalized;
    context->publishedPort = port;
    if (!context->announce(kRecordTtl)) return -1;
    context->nextAnnouncement = Clock::now() + std::chrono::seconds(kRecordTtl / 2);
    return 0;
}

int czc_browse(CZC_Context* context, const char* type, CZC_ServiceCallback callback, void* data) {
    if (!context) return -1;
    context->lastError.clear();
    const std::string normalized = normalizeType(type);
    if (normalized.empty() || !callback) {
        context->fail("Browse requires a valid DNS-SD type and callback");
        return -1;
    }
    context->browseType = normalized;
    context->callback = callback;
    context->callbackData = data;
    context->services.clear();
    context->addresses.clear();
    if (!context->query()) return -1;
    context->nextQuery = Clock::now() + std::chrono::seconds(3);
    return 0;
}

int czc_poll(CZC_Context* context, int timeout_ms) {
    if (!context) return -1;
    if (timeout_ms < 0) {
        context->fail("Negative poll timeout");
        return -1;
    }
    const auto now = Clock::now();
    if (!context->browseType.empty() && now >= context->nextQuery) {
        if (!context->query()) return -1;
        context->nextQuery = now + std::chrono::seconds(3);
    }
    if (!context->publishedName.empty() && now >= context->nextAnnouncement) {
        if (!context->announce(kRecordTtl)) return -1;
        context->nextAnnouncement = now + std::chrono::seconds(kRecordTtl / 2);
    }
    fd_set sockets;
    FD_ZERO(&sockets);
    FD_SET(context->socket, &sockets);
    timeval timeout {timeout_ms / 1000, (timeout_ms % 1000) * 1000};
    const int ready = select(static_cast<int>(context->socket) + 1, &sockets, nullptr, nullptr, &timeout);
    if (ready < 0) {
        context->fail("mDNS socket poll failed");
        return -1;
    }
    int callbacks = 0;
    if (ready > 0) {
        std::array<uint8_t, kMaxPacket> packet {};
        sockaddr_in sender {};
#ifdef _WIN32
        int length = sizeof(sender);
#else
        socklen_t length = sizeof(sender);
#endif
        const int received = recvfrom(context->socket, reinterpret_cast<char*>(packet.data()),
                                      static_cast<int>(packet.size()), 0,
                                      reinterpret_cast<sockaddr*>(&sender), &length);
        if (received > 0) callbacks += context->process(packet.data(), static_cast<size_t>(received));
    }
    for (auto it = context->services.begin(); it != context->services.end();) {
        if (Clock::now() >= it->second.expires) {
            if (it->second.delivered) callbacks += context->emit(CZC_REMOVED, it->second);
            it = context->services.erase(it);
        } else ++it;
    }
    return callbacks;
}

const char* czc_last_error(const CZC_Context* context) {
    return context ? context->lastError.c_str() : "Zeroconf context is null";
}

const char* czc_last_create_error(void) { return creationError.c_str(); }

uint32_t czc_api_version(void) { return 1; }

} // extern "C"
