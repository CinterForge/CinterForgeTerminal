#ifndef CINTER_ZEROCONF_H
#define CINTER_ZEROCONF_H

/* Optional DNS-SD / mDNS service discovery API for CinterForge Python Host.
 *
 * Include this header alongside CinterPythonHost.h or CinterPythonHostC.h.
 * This API is independent of Python, has a stable C ABI, and requires no
 * Apple Bonjour SDK, Avahi daemon, or third-party discovery package.
 *
 * All operations are synchronous to the caller. Call czc_poll() periodically
 * (or from a dedicated thread) to process announcements, browsing responses,
 * timeouts, and service departures. Callbacks run from czc_poll(); do not
 * reenter the same context from a callback. One context supports one active
 * publication and one active browse filter. Service fields are valid only
 * for the duration of the callback.
 *
 * Uses multicast DNS on the local IPv4 link (224.0.0.251:5353); it does not
 * discover services across routed networks, authenticate remote services, or
 * replace the application's HTTP/RPC transport.
 */
#include <stdint.h>

#ifdef _WIN32
# if defined(CPH_BUILD_STATIC)
#  define CZC_API
# elif defined(CPH_BUILD_SHARED)
#  define CZC_API __declspec(dllexport)
# else
#  define CZC_API __declspec(dllimport)
# endif
#else
# define CZC_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct CZC_Context CZC_Context;

typedef enum CZC_Event {
    CZC_ADDED = 1,
    CZC_UPDATED = 2,
    CZC_REMOVED = 3
} CZC_Event;

typedef struct CZC_Service {
    const char* name;       /* Instance display name, e.g. "CinterForge". */
    const char* type;       /* Canonical DNS-SD type, e.g. "_Cinter._tcp.local." */
    const char* host;       /* Local network host name. */
    const char* address;    /* Resolved IPv4 address; do not trust without a connection. */
    uint16_t port;         /* Host byte order. */
} CZC_Service;

typedef void (*CZC_ServiceCallback)(CZC_Event event, const CZC_Service* service,
                                     void* user_data);

/* Returns NULL on socket initialization failure. If this fails, the caller
 * can inspect czc_last_create_error() on the same thread. Unlike
 * czc_last_error(), no context is required to get this diagnostic. */
CZC_API CZC_Context* czc_create(void);
CZC_API const char* czc_last_create_error(void);
CZC_API void czc_destroy(CZC_Context* context);

/* Publish one local TCP/UDP service. Type may be _Cinter._tcp. or
 * _Cinter._tcp.local.; both are normalized to DNS-SD's .local. domain.
 * A port of zero, invalid service type, or invalid name is rejected.
 */
CZC_API int czc_publish(CZC_Context* context, const char* name,
                        const char* type, uint16_t port);
/* Listen for one service type; service callbacks are delivered in poll(). */
CZC_API int czc_browse(CZC_Context* context, const char* type,
                       CZC_ServiceCallback callback, void* user_data);
/* Returns callback count, or -1 on error. timeout_ms must be >= 0. */
CZC_API int czc_poll(CZC_Context* context, int timeout_ms);
/* Stop publishing without canceling an active browse, or vice versa. */
CZC_API void czc_unpublish(CZC_Context* context);
CZC_API void czc_stop_browse(CZC_Context* context);
/* Send a goodbye announcement, cancel browsing and clear the local cache. */
CZC_API void czc_stop(CZC_Context* context);
/* Last error is owned by context and valid until next mutating operation. */
CZC_API const char* czc_last_error(const CZC_Context* context);
CZC_API uint32_t czc_api_version(void);

#ifdef __cplusplus
}

#include <chrono>
#include <functional>
#include <stdexcept>
#include <string>
#include <utility>

namespace cinterforge {

/* Thin RAII wrapper. The C API remains usable directly from Swift/ObjC/FFI. */
class Zeroconf {
public:
    using Callback = std::function<void(CZC_Event, const CZC_Service&)>;

    Zeroconf() : context_(czc_create()) {
        if (!context_) {
            throw std::runtime_error(std::string("Unable to initialize mDNS socket: ") +
                                     czc_last_create_error());
        }
    }
    ~Zeroconf() { czc_destroy(context_); }
    Zeroconf(const Zeroconf&) = delete;
    Zeroconf& operator=(const Zeroconf&) = delete;
    // Not movable: active browse callbacks retain the address of this instance.
    Zeroconf(Zeroconf&&) = delete;
    Zeroconf& operator=(Zeroconf&&) = delete;

    void publish(const std::string& name, const std::string& type, uint16_t port) {
        check(czc_publish(context_, name.c_str(), type.c_str(), port));
    }
    void browse(const std::string& type, Callback callback) {
        callback_ = std::move(callback);
        check(czc_browse(context_, type.c_str(), &Zeroconf::deliver, this));
    }
    int poll(std::chrono::milliseconds timeout = std::chrono::milliseconds(100)) {
        const int events = czc_poll(context_, static_cast<int>(timeout.count()));
        if (events < 0) throw std::runtime_error(czc_last_error(context_));
        return events;
    }
    void unpublish() { czc_unpublish(context_); }
    void stopBrowsing() { czc_stop_browse(context_); }
    void stop() { czc_stop(context_); }

private:
    void check(int status) {
        if (status != 0) throw std::runtime_error(czc_last_error(context_));
    }
    static void deliver(CZC_Event event, const CZC_Service* service, void* user_data) {
        auto* self = static_cast<Zeroconf*>(user_data);
        if (self->callback_ && service) self->callback_(event, *service);
    }
    CZC_Context* context_ = nullptr;
    Callback callback_;
};

} // namespace cinterforge
#endif
#endif
