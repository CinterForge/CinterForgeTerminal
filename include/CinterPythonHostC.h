#ifndef CINTER_PYTHON_HOST_C_H
#define CINTER_PYTHON_HOST_C_H

/* Stable C99 ABI for the out-of-process CinterForge Python host.
 * No CPython headers, C++ objects, source checkout or global Python state.
 * Callers supply absolute, bundled interpreter and worker paths.
 * The handle is single-consumer: do not call its methods concurrently.
 */
#include <stdint.h>

#ifdef _WIN32
# ifdef CPH_BUILD_STATIC
#  define CPH_API
# elif defined(CPH_BUILD_SHARED)
#  define CPH_API __declspec(dllexport)
# else
#  define CPH_API __declspec(dllimport)
# endif
#else
# define CPH_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct CPH_Process CPH_Process;
typedef enum CPH_Status {
    CPH_OK = 0,
    CPH_ERROR = 1,
    CPH_TIMEOUT = 2
} CPH_Status;

/* Called synchronously from cph_start / cph_call. Do not reenter the handle. */
typedef void (*CPH_EventCallback)(const char* json_event, void* user_data);

CPH_API CPH_Process* cph_create(void);
CPH_API void cph_destroy(CPH_Process* handle);
CPH_API void cph_set_event_callback(CPH_Process* handle, CPH_EventCallback callback, void* user_data);

/* Waits for worker's ready event. Optional args are passed to worker.py. */
CPH_API CPH_Status cph_start(CPH_Process* handle, const char* python, const char* worker,
                              const char* const* args, int argc, uint32_t timeout_ms);
/* All results are complete JSON response objects; free with cph_free_string. */
CPH_API CPH_Status cph_call(CPH_Process* handle, const char* method, const char* params_json,
                             uint32_t timeout_ms, char** json_response);
/* A raw JSON-lines transport for non-RPC workers. */
CPH_API CPH_Status cph_send(CPH_Process* handle, const char* json_line);
CPH_API CPH_Status cph_receive(CPH_Process* handle, uint32_t timeout_ms, char** json_line);
CPH_API int cph_running(CPH_Process* handle);
CPH_API CPH_Status cph_stop(CPH_Process* handle, uint32_t grace_ms);
/* Error pointer belongs to handle and lasts until its next operation. */
CPH_API const char* cph_last_error(const CPH_Process* handle);
CPH_API void cph_free_string(char* value);
CPH_API uint32_t cph_api_version(void);

#ifdef __cplusplus
}
#endif
#endif
