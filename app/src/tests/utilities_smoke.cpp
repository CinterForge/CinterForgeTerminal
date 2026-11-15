/*
 * CINTERFORGE SOURCE HEADER — app/src/tests/utilities_smoke.cpp
 * PURPOSE: Implements the native utilities smoke helper for the existing CinterForgeTerminal application and Python Host.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   main: Start the current executable and return its status.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
#include "CinterHostUtilities.h"
#include <atomic>
#include <iostream>

int main()
{
    cinterforge::HostMessageQueue queue;
    queue.enqueue({1, "Python", "progress", "{\"percent\":50}"});
    queue.enqueue({2, "Python", "done", "{}"});
    if (queue.pending() != 2)
        return 1;
    cinterforge::HostMessage msg;
    if (!queue.try_dequeue(msg) || msg.id != 1 || queue.pending() != 1)
        return 2;
    std::atomic<int> executed{0};
    cinterforge::HostTaskDispatcher dispatcher;
    if (dispatcher.call([&]
                        { return ++executed; }) != 1)
        return 3;
    dispatcher.post([&]
                    { ++executed; });
    dispatcher.stop();
    if (executed != 2)
        return 4;
    std::cout << "Host queue and dispatcher smoke passed\n";
    return 0;
}
