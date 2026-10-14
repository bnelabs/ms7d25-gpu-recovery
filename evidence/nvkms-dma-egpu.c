/*
 * SPDX-FileCopyrightText: Copyright (c) 1993-2013 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: MIT
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
 * DEALINGS IN THE SOFTWARE.
 */

#include <stddef.h>

#include "nvkms-dma.h"
#include "nvkms-utils.h"
#include "nvkms-rmapi.h"
#include "class/clc57d.h" // NVC57D_{PUT,GET}
#include "nvos.h"

#define NV_DMA_PUSHER_CHASE_PAD 5
#define NV_EVO_NOTIFIER_SHORT_TIMEOUT_USEC 3000000 // 3 seconds

/*
 * Backstop for the "engine dead in place" failure mode: the eGPU's power browned
 * out but its Thunderbolt link stayed up, so there is no PCI surprise-removal and
 * RM never detects the loss (NV_PMC_BOOT_0 still reads valid) -- the GPU-lost
 * event never fires. In that case the only evidence is that the channel's GET
 * pointer stops advancing. If GET makes no progress at all for this long, treat
 * the GPU as lost. This is deliberately far above the longest legitimate single
 * display wait (5 s) and the kicked-off-then-slow case, but well under the kernel
 * hung-task watchdog (120 s), and it only fires when GET does not advance -- a
 * slow-but-alive GPU keeps advancing GET and never trips it.
 */
#define NV_EVO_GPU_LOST_BACKSTOP_USEC 30000000 // 30 seconds

static void EvoCoreKickoff(NVDmaBufferEvoPtr push_buffer, NvU32 putOffset);

/*
 * The "GPU is lost" flag on NVDevEvoRec is written lock-free from resman context
 * (the NV2080_NOTIFIERS_GPU_UNAVAILABLE callback) and from the no-progress
 * backstop below, and read by the push-buffer waiters and nvSetDispModeEvo. Use
 * C11 atomics with release/acquire ordering -- nvidia-modeset has no portAtomic
 * or kernel atomic_t, and a plain volatile would give no inter-CPU ordering.
 */
void nvEvoSetDeviceLost(NVDevEvoPtr pDevEvo)
{
    __atomic_store_n(&pDevEvo->gpuLost, 1, __ATOMIC_RELEASE);
}

NvBool nvEvoDeviceIsLost(const NVDevEvoRec *pDevEvo)
{
    return __atomic_load_n(&pDevEvo->gpuLost, __ATOMIC_ACQUIRE) != 0;
}

/*
 * When nvEvoMakeRoom() abandons a wait because the GPU is lost, it must NOT
 * leave the pushbuffer with fifo_free_count <= count: its caller
 * (nvDmaSetStartEvoMethod) would then write the method and do
 * "fifo_free_count -= countPlusHeader", underflowing the NvU32, after which it
 * never calls nvEvoMakeRoom() again and keeps advancing pb.buffer past the end
 * of the ring -- a CPU buffer overrun. Instead, reset the ring to a fresh-empty
 * state (buffer at base, a full free count), exactly as a normal wrap-to-base
 * would. Subsequent methods go nowhere (the GPU is gone) but stay in-bounds:
 * the next time the free count is exhausted, nvEvoMakeRoom() is re-entered and
 * resets again, so pb.buffer oscillates within [base, base + offset_max].
 */
static void EvoResetPushBufferForLostGpu(NVDmaBufferEvoPtr push_buffer)
{
    push_buffer->buffer = push_buffer->base;
    push_buffer->fifo_free_count = push_buffer->offset_max >> 2;
}

void nvDmaKickoffEvo(NVEvoChannelPtr pChannel)
{
    NVDmaBufferEvoPtr p = &pChannel->pb;
    NvU32 putOffset = (NvU32)((char *)p->buffer - (char *)p->base);

    if (p->put_offset == putOffset) {
        return;
    }

    EvoCoreKickoff(p, putOffset);
}

static void EvoCoreKickoff(NVDmaBufferEvoPtr push_buffer, NvU32 putOffset)
{
    nvAssert(putOffset % 4 == 0);
    nvAssert(putOffset <= push_buffer->offset_max);

#if NVCPU_IS_X86_64
    __asm__ __volatile__ ("sfence\n\t" : : : "memory");
#elif NVCPU_IS_FAMILY_ARM
    __asm__ __volatile__ ("dsb sy\n\t" : : : "memory");
#endif

    push_buffer->put_offset = putOffset;
    nvDmaStorePioMethod(push_buffer->control, NVC57D_PUT, putOffset);
}

/* Read GET from an EVO core channel */
static NvU32 EvoCoreReadGet(NVDmaBufferEvoPtr push_buffer)
{
    void *pControl = push_buffer->control;
    return nvDmaLoadPioMethod(pControl, NVC57D_GET);
}

NvBool nvEvoPollForEmptyChannel(NVEvoChannelPtr pChannel, NvU32 sd,
                                NvU64 *pStartTime, const NvU32 timeout)
{
    NVDmaBufferEvoPtr push_buffer = &pChannel->pb;

    do {
        if (EvoCoreReadGet(push_buffer) == push_buffer->put_offset) {
            break;
        }

        if (nvExceedsTimeoutUSec(push_buffer->pDevEvo, pStartTime, timeout)) {
            return FALSE;
        }

        nvkms_yield();
   } while (TRUE);

    return TRUE;
}

void nvEvoMakeRoom(NVEvoChannelPtr pChannel, NvU32 count)
{
    NVDmaBufferEvoPtr push_buffer = &pChannel->pb;
    NVDevEvoPtr pDevEvo = push_buffer->pDevEvo;
    NvU32 getOffset;
    NvU32 putOffset;
    NvU64 startTime = 0;
    NvU64 noProgressStartTime = 0;
    NvU32 lastGetOffset = ~0;
    const NvU64 timeout = 5000000; /* 5 seconds */

    putOffset = (NvU32) ((char *)push_buffer->buffer -
                         (char *)push_buffer->base);

    if (putOffset >= push_buffer->offset_max) {
        *(push_buffer->buffer) = 0x20000000;
        push_buffer->buffer = push_buffer->base;
        nvDmaKickoffEvo(pChannel);
        putOffset = 0;
    }

    while (1) {
        /*
         * If the GPU has been flagged lost (by the GPU-unavailable event for a
         * surprise-removal, or by the no-progress backstop below / another
         * waiter), stop spinning. Methods already written to the dead channel
         * are harmless; the modeset/flip will fail cleanly via the gpuLost
         * check in nvSetDispModeEvo and the bounded RM-backed idle waits.
         */
        if (nvEvoDeviceIsLost(pDevEvo)) {
            EvoResetPushBufferForLostGpu(push_buffer);
            return;
        }

        getOffset = EvoCoreReadGet(push_buffer);

        if (putOffset >= getOffset) {
            push_buffer->fifo_free_count =
                (push_buffer->offset_max - putOffset) >> 2;

            if (push_buffer->fifo_free_count <= count) {
                if (getOffset) {
                    *(push_buffer->buffer) = 0x20000000;
                    push_buffer->buffer = push_buffer->base;
                    nvDmaKickoffEvo(pChannel);
                    putOffset = 0;
                }
                else if (putOffset != push_buffer->put_offset) {
                    nvDmaKickoffEvo(pChannel);
                    // Put offset will have changed if a tail was inserted.
                    putOffset = push_buffer->put_offset;
                }
            }
        }
        else {
            getOffset = (getOffset > push_buffer->offset_max) ?
               push_buffer->offset_max : getOffset;

            if ((putOffset + (NV_DMA_PUSHER_CHASE_PAD * 4)) >= getOffset)
                push_buffer->fifo_free_count = 0;
            else
                push_buffer->fifo_free_count =
                   ((getOffset - putOffset) >> 2) - 1;
        }
        if (push_buffer->fifo_free_count > count) {
            break;
        }

        /*
         * If we have been waiting too long, print an error message.  There
         * isn't much we can do as currently structured, so just reset
         * startTime.
         */
        if (nvExceedsTimeoutUSec(push_buffer->pDevEvo, &startTime, timeout)) {
            nvEvoLogDev(push_buffer->pDevEvo, EVO_LOG_ERROR,
                "Error while waiting for GPU progress: "
                "0x%08x:%d %d:%d:%d:%d",
                pChannel->hwclass, pChannel->instance,
                count, push_buffer->fifo_free_count, getOffset, putOffset);
            startTime = 0;
        }

        /*
         * No-progress backstop for the engine-dead-in-place case (link up, no
         * removal event). Reset the timer whenever GET advances; if GET has not
         * advanced at all for NV_EVO_GPU_LOST_BACKSTOP_USEC, declare the GPU
         * lost (so all waiters and the modeset path observe the same flag) and
         * stop spinning.
         */
        if (getOffset != lastGetOffset) {
            lastGetOffset = getOffset;
            noProgressStartTime = 0;
        } else if (nvExceedsTimeoutUSec(pDevEvo, &noProgressStartTime,
                                        NV_EVO_GPU_LOST_BACKSTOP_USEC)) {
            nvEvoLogDev(pDevEvo, EVO_LOG_ERROR,
                "GPU made no progress for %u seconds; assuming the GPU is lost "
                "and abandoning the wait (0x%08x:%d).",
                (NvU32)(NV_EVO_GPU_LOST_BACKSTOP_USEC / 1000000),
                pChannel->hwclass, pChannel->instance);
            nvEvoSetDeviceLost(pDevEvo);
            EvoResetPushBufferForLostGpu(push_buffer);
            return;
        }

        nvkms_yield();
   }
}

static inline void EvoWriteNotifier(volatile NvU32 *pNotifier, NvU32 value)
{
   /*
     * Note that we don't need to flush to vidmem here; any subsequent GPU
     * write will always be triggered by kicking off pushbuffer methods,
     * which will perform a general FB flush.  This does assume that the
     * pushbuffer and its associated notifier surfaces are either both in
     * sysmem or both in vidmem, however.
     */

    *pNotifier = value;
}

/* Write the EVO core notifier at the given offset to the given value. */
void nvWriteEvoCoreNotifier(
    const NVDispEvoRec *pDispEvo,
    NvU32 offset,
    NvU32 value)
{
    NVDevEvoPtr pDevEvo = pDispEvo->pDevEvo;
    volatile NvU32 *pNotifier = pDevEvo->core->notifierDma.cpuAddress;

    EvoWriteNotifier(pNotifier + offset, value);
}

static NvBool EvoCheckNotifier(const NVDispEvoRec *pDispEvo,
                               NvU32 offset, NvU32 done_base_bit,
                               NvU32 done_extent_bit, NvU32 done_value,
                               NvBool wait)
{
    const NvU32 sd = pDispEvo->displayOwner;
    NVDevEvoPtr pDevEvo = pDispEvo->pDevEvo;
    NVDmaBufferEvoPtr p = &pDevEvo->core->pb;
    volatile NvU32 *pNotifier = pDevEvo->core->notifierDma.cpuAddress;
    NvU64 startTime = 0;
    NvU64 noProgressStartTime = 0;
    NvU32 lastGetOffset = ~0;

    nvAssert(pNotifier != NULL);
    pNotifier += offset;

    // While the completion notifier is not set to done_true
    do {
        const NvU32 val = *pNotifier;
        const NvU32 done_mask = DRF_SHIFTMASK(done_extent_bit:done_base_bit);
        const NvU32 done_val = done_value << done_base_bit;

        if ((val & done_mask) == done_val) {
            return TRUE;
        }

        if (!wait) {
            return FALSE;
        }

        /*
         * If the GPU has been flagged lost (GPU-unavailable event for a
         * surprise-removal, or the no-progress backstop below / nvEvoMakeRoom),
         * abandon the wait. Return FALSE -- do NOT fake completion by writing
         * the notifier -- so the caller and the teardown path see that the
         * notifier did not complete and unwind correctly.
         */
        if (nvEvoDeviceIsLost(pDevEvo)) {
            nvEvoLogDisp(pDispEvo, EVO_LOG_WARN,
                         "GPU is lost; abandoning display notifier wait "
                         "(%d:0x%08x).", sd, val);
            return FALSE;
        }

        if (nvExceedsTimeoutUSec(
                pDevEvo,
                &startTime,
                NV_EVO_NOTIFIER_SHORT_TIMEOUT_USEC) &&
            (p->put_offset == EvoCoreReadGet(p)))
        {
            /*
             * The channel has drained (put == GET) but the notifier was never
             * written: a genuinely-alive GPU that dropped a notifier write.
             * This is a legitimate recovery -- fake completion and continue.
             */
            nvEvoLogDisp(pDispEvo, EVO_LOG_WARN,
                         "Lost display notification (%d:0x%08x); "
                         "continuing.", sd, val);
            EvoWriteNotifier(pNotifier, done_value << done_base_bit);
            return TRUE;
        }

        /*
         * No-progress backstop for the engine-dead-in-place case (link up, no
         * removal event, so gpuLost is never set by the event path). If GET has
         * not advanced at all for NV_EVO_GPU_LOST_BACKSTOP_USEC, declare the GPU
         * lost and abandon the wait. GET advancing resets the timer, so a
         * slow-but-alive GPU never trips this.
         */
        {
            const NvU32 getOffset = EvoCoreReadGet(p);
            if (getOffset != lastGetOffset) {
                lastGetOffset = getOffset;
                noProgressStartTime = 0;
            } else if (nvExceedsTimeoutUSec(pDevEvo, &noProgressStartTime,
                                            NV_EVO_GPU_LOST_BACKSTOP_USEC)) {
                nvEvoLogDisp(pDispEvo, EVO_LOG_ERROR,
                             "GPU made no progress for %u seconds; assuming the "
                             "GPU is lost and abandoning the display notifier "
                             "wait (%d:0x%08x).",
                             (NvU32)(NV_EVO_GPU_LOST_BACKSTOP_USEC / 1000000),
                             sd, val);
                nvEvoSetDeviceLost(pDevEvo);
                return FALSE;
            }
        }

        nvkms_yield();
    } while (TRUE);
}

/*
 * Used by NV_EVO_WAIT_FOR_NOTIFIER() and NV_EVO_WAIT_FOR_CAPS_NOTIFIER()
 */
void nvEvoWaitForCoreNotifier(const NVDispEvoRec *pDispEvo, NvU32 offset,
                              NvU32 done_base_bit, NvU32 done_extent_bit,
                              NvU32 done_value)
{
    EvoCheckNotifier(pDispEvo, offset,
                     done_base_bit, done_extent_bit, done_value, TRUE);
}

/*
 * Used by the EVO HAL IsNotifierComplete functions.  Returns TRUE if the
 * notifier is complete.
 */
NvBool nvEvoIsCoreNotifierComplete(NVDispEvoPtr pDispEvo, NvU32 offset,
                                   NvU32 done_base_bit, NvU32 done_extent_bit,
                                   NvU32 done_value)
{
    return EvoCheckNotifier(pDispEvo,
                            offset, done_base_bit, done_extent_bit,
                            done_value, FALSE);
}

/*!
 * Reads CRC values from the notifier.
 *
 * This function will attempt to read in the first 'entry_count' CRC notifier
 * entries that HW generated. The actual number of entries that are read may
 * be less.
 *
 * \param[in]  pCRC32Notifier   Pointer to the CRC notifier memory.
 * \param[in]  entry_stride     Stride of a single CRC notifier entry
 * \param[in]  entry_count      Expected count of notifier entries to read
 * \param[in]  status_offset    Offset for Status flags header in CRC notifier
 * \param[in]  field_count      Number of fields to read from each CRC notifier
 *                              entry.
 * \param[in]  flag_count       Number of flags to read from the Status Header
 * \param[in out] field_info    Specifies the offset/base/extent info for each field.
 *                              Each 'field_info' contains an output array for
 *                              storing 'entry_count' field values.
 * \param[in]     flag_info     Specifies the base/extent info for each flag.
 *                              Each 'flag_info' contains a 'flag_type' for
 *                              addressing error cases related to the flags.
 *
 * \return  Returns the MIN(count, entry_count) of successfully
 *          read entries.
 */
NvU32 nvEvoReadCRC32Notifier(volatile NvU32 *pCRC32Notifier,
                             NvU32 entry_stride,
                             NvU32 entry_count,
                             NvU32 status_offset,
                             NvU32 field_count,
                             NvU32 flag_count,
                             const CRC32NotifierEntryRec *field_info,
                             const CRC32NotifierEntryFlags *flag_info)
{
    NvU32 count = 0;
    NvU32 i, j, k;

    nvAssert(pCRC32Notifier != NULL);

    // Iterate over flags (unique at start of the CRC32Notifier Struct)
    for (k = 0; k < flag_count; k++) {
        CRC32NotifierEntryFlags info = flag_info[k];
        volatile NvU32 *pFlag = pCRC32Notifier + status_offset;
        NvU32 flag_mask =
              DRF_SHIFTMASK((info.flag_extent_bit):(info.flag_base_bit));
        NvU32 flag = (*pFlag & flag_mask) >> info.flag_base_bit;

        switch (info.flag_type)
        {
            case NVEvoCrc32NotifierFlagCount:
                count = flag;
                // entry_count is max of each field_frame_values[i] array
                if (count > entry_count) {
                    nvEvoLog(EVO_LOG_WARN, "Too many CRC32 generated entries "
                             "(%d expected; %d found)", entry_count, count);
                    count = entry_count;
                }
                break;

            case NVEvoCrc32NotifierFlagCrcOverflow:
                if (flag) {
                    count = 0;
                    nvEvoLog(EVO_LOG_ERROR, "CRC Overflow occured, "
                             "CRC value unable to be processed fast enough.\n"
                             "Failing flag index in status_info array: %d",
                              k);

                    return count;
                }
                break;
        }
    }

    // Iterate over each collection of fields, for count pairs of values
    for (i = 0; i < count; i++) {
        for (j = 0; j < field_count; j++) {
            CRC32NotifierEntryRec info = field_info[j];
            volatile NvU32 *pEntry = pCRC32Notifier + info.field_offset;
            NvU32 field_mask =
                DRF_SHIFTMASK((info.field_extent_bit):(info.field_base_bit));

            info.field_frame_values[i].value =
                (*pEntry & field_mask) >> info.field_base_bit;
            info.field_frame_values[i].supported = TRUE;
        }
        pCRC32Notifier += entry_stride;
    }

    return count;
}

void nvEvoResetCRC32Notifier(volatile NvU32 *pCRC32Notifier,
                             NvU32 offset,
                             NvU32 reset_base_bit,
                             NvU32 reset_value)
{
    const NvU32 reset_val = reset_value << reset_base_bit;

    nvAssert(pCRC32Notifier != NULL);
    pCRC32Notifier += offset;

    EvoWriteNotifier(pCRC32Notifier, reset_val);
}

NvBool nvEvoWaitForCRC32Notifier(const NVDevEvoPtr pDevEvo,
                                 volatile NvU32 *pCRC32Notifier,
                                 NvU32 offset,
                                 NvU32 done_base_bit,
                                 NvU32 done_extent_bit,
                                 NvU32 done_value)
{
    const NvU32 done_mask = DRF_SHIFTMASK(done_extent_bit:done_base_bit);
    const NvU32 done_val = done_value << done_base_bit;
    NvU64 startTime = 0;

    nvAssert(pCRC32Notifier != NULL);
    pCRC32Notifier += offset;

    do {
        const NvU32 status = *pCRC32Notifier;

        if ((status & done_mask) == done_val) {
            return TRUE;
        }

        if (nvExceedsTimeoutUSec(
                pDevEvo,
                &startTime,
                NV_EVO_NOTIFIER_SHORT_TIMEOUT_USEC)) {
            return FALSE;
        }

        nvkms_yield();

    } while (TRUE);

    return FALSE;
}
