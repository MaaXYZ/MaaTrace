#pragma once
#include <stdint.h>

#if defined(_WIN32)
# if defined(MAA_TRACE_BUILD)
#  define MAA_TRACE_API __declspec(dllexport)
# else
#  define MAA_TRACE_API __declspec(dllimport)
# endif
#else
# define MAA_TRACE_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* MaaFramework plugin ABI v1. All callback arguments are borrowed. */
MAA_TRACE_API const char* GetPluginVersion(void);
MAA_TRACE_API uint32_t GetApiVersion(void);
MAA_TRACE_API void OnResourceEvent(void*, const char*, const char*, void*);
MAA_TRACE_API void OnControllerEvent(void*, const char*, const char*, void*);
MAA_TRACE_API void OnTaskerEvent(void*, const char*, const char*, void*);
MAA_TRACE_API void OnContextEvent(void*, const char*, const char*, void*);

/* Wait for previously accepted events to reach the file stream (not fsync).
 * Returns 0 on recording errors. Never call from DllMain. */
MAA_TRACE_API int MaaTraceFlush(void);
/* Flush and stop. Idempotent; recording cannot restart in this process.
 * Call after all taskers stop and before unloading the framework. */
MAA_TRACE_API int MaaTraceShutdown(void);
/* UTF-8 JSON; pointer valid until the next call on the same thread.
 * Counters are process-wide. Does not initialize the recorder. */
MAA_TRACE_API const char* MaaTraceGetStatus(void);

/* Optional application events. tasker is the native MaaTasker handle, or NULL
 * for a session-level event. Strings are copied during the call. */
MAA_TRACE_API void MaaTraceEmit(void* tasker, const char* event_name, const char* details_json);
/* Call immediately before the host requests stop; this records intent only. */
MAA_TRACE_API void MaaTraceRecordStopRequest(void* tasker, const char* reason);

#ifdef __cplusplus
}
#endif
