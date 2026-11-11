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
