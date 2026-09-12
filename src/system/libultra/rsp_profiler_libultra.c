#include "rsp_profiler_libultra.h"

#include "debugger/debug.h"
#include "system/display.h"
#include "system/libultra/rsp_scheduler_libultra.h"
#include "system/libultra/threads_libultra.h"
#include "system/time.h"
#include "util/memory.h"

#define PRINT_DL_DEPTH_MAX  20
#define PRINT_DL_LENGTH_MAX 1000

#define VI_EVENT_MSG        666
#define SP_EVENT_MSG        667
#define DP_EVENT_MSG        668

#define MESSAGE_QUEUE_SIZE  4
#define SAMPLES_PER_STEP    10

static void printChildDisplayLists(Gfx* dl, int depth, int* segments) {
    if (depth == PRINT_DL_DEPTH_MAX) {
        debug_printf("dl <depth limit>\n", depth);
        return;
    }

    for (int i = 0; i < PRINT_DL_LENGTH_MAX; ++i) {
        int commandType = _SHIFTR(dl->words.w0, 24, 8);

        switch (commandType) {
            case G_MOVEWORD:
            {
                if (dl->dma.par == G_MW_SEGMENT) {
                    int segmentNum = (dl->dma.len >> 2) % NUM_SEGMENTS;
                    segments[segmentNum] = dl->dma.addr;
                }
                break;
            }
            case G_DL:
            {
                debug_printf("dl 0x%08x%08x\n", dl->words.w0, dl->words.w1);

                int address = dl->dma.addr;
                int segmentNum = SEGMENT_NUMBER(address);
                int segmentedAddress = segments[segmentNum] + (address & 0xffffff);

                printChildDisplayLists((Gfx*)PHYS_TO_K0(segmentedAddress), depth + 1, segments);
                break;
            }
            case G_ENDDL:
                debug_printf("dl 0x%08x%08x\n", dl->words.w0, dl->words.w1);
                return;
        }

        ++dl;
    }

    debug_printf("dl <length limit>\n");
}

static void waitForDPAvailable() {
    while (osDpGetStatus() & (DPC_STATUS_DMA_BUSY | DPC_STATUS_END_VALID | DPC_STATUS_START_VALID));
}

static void setSchedulerQueue(OSMesgQueue* messageQueue) {
    OSIntMask mask = osSetIntMask(OS_IM_NONE);

    osSetEventMesg(OS_EVENT_SP, messageQueue, (OSMesg)SP_EVENT_MSG);
    osSetEventMesg(OS_EVENT_DP, messageQueue, (OSMesg)DP_EVENT_MSG);
    osViSetEvent(messageQueue, (OSMesg)VI_EVENT_MSG, 1);

    osSetIntMask(mask);
}

void rspProfilerRun(OSTask* task, u16* framebuffer) {
    // Block scheduler thread
    OSPri origThreadPriority = osGetThreadPri(NULL);
    osSetThreadPri(NULL, RSP_SCHEDULER_THREAD_PRIORITY + 1);

    waitForDPAvailable();
    zeroMemory(framebuffer, sizeof(u16) * SCREEN_WD * SCREEN_HT);

    // Take over event queues
    OSMesgQueue messageQueue;
    OSMesg messages[MESSAGE_QUEUE_SIZE];
    osCreateMesgQueue(&messageQueue, messages, MESSAGE_QUEUE_SIZE);
    setSchedulerQueue(&messageQueue);

    // Show progress
    osViSwapBuffer(framebuffer);

    debug_printf("Begin RSP profile\n");

    int segments[NUM_SEGMENTS];
    zeroMemory(segments, sizeof(segments));
    printChildDisplayLists((Gfx*)task->t.data_ptr, 0, segments);

    Gfx* curr = (Gfx*)task->t.data_ptr;
    Gfx* end = curr;
    while (_SHIFTR(end->words.w0, 24, 8) != G_RDPFULLSYNC) {
        ++end;
    }

    int length = end - curr;
    Gfx tmp[3];

    while (curr < end) {
        for (int sample = 0; sample < SAMPLES_PER_STEP; ++sample) {
            // End display list at current step
            waitForDPAvailable();
            memCopy(tmp, curr, 3 * sizeof(Gfx));

            Gfx* dl = curr;
            gDPPipeSync(dl++);
            gDPFullSync(dl++);
            gSPEndDisplayList(dl++);
            osWritebackDCache(curr, 3 * sizeof(Gfx));

            // Render
            Time taskStart = timeGetTime();
            osSpTaskStart(task);

            OSMesg msg;
            while (osRecvMesg(&messageQueue, &msg, OS_MESG_NOBLOCK) == -1 || (int)msg != DP_EVENT_MSG);

            // Display list run time up to but not including dummied-out command
            uint64_t taskNs = timeNanoseconds(timeGetTime() - taskStart);

            // Restore original display list
            waitForDPAvailable();
            memCopy(curr, tmp, 3 * sizeof(Gfx));
            osWritebackDCache(curr, 3 * sizeof(Gfx));

            // Report current command info, and time to reach it
            debug_printf(
                "%d/%d 0x%08x%08x %d.%d ms\n",
                (curr - (Gfx*)task->t.data_ptr) + 1,
                length,
                curr->words.w0,
                curr->words.w1,
                (int)(taskNs / 1000000),
                (int)(taskNs % 1000000)
            );
        }

        debug_dumpbinary(framebuffer, SCREEN_WD * SCREEN_HT * 2  /* 16-bit */);

        ++curr;
    }

    debug_printf("End RSP profile\n");

    // Restore queues to scheduler and unblock its thread
    setSchedulerQueue(&rspSchedulerGet()->interruptQ);
    osSetThreadPri(NULL, origThreadPriority);
}
