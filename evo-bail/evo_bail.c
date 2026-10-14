// SPDX-License-Identifier: GPL-2.0
/* Incident-specific rescue of 615.71.09 nvEvoMakeRoom teardown.
 * OFF by default. Match the actual binary and inspect the frame first.
 * Not a general driver fix. Never apply to a live workload or another PID.
 */
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/uaccess.h>
#include <linux/sched.h>

static int target_pid;
static bool armed;
static bool notifier_armed;
static unsigned long hits, rescues;
static unsigned long notifier_hits, notifier_rescues;
module_param(target_pid, int, 0400);
module_param(armed, bool, 0600);
module_param(notifier_armed, bool, 0600);
module_param(hits, ulong, 0400);
module_param(rescues, ulong, 0400);
module_param(notifier_hits, ulong, 0400);
module_param(notifier_rescues, ulong, 0400);
static struct kprobe probe;
static struct kprobe yield_probe;
static struct kprobe notifier_probe;
static unsigned long last_caller;

static int inspect_yield(struct kprobe *p, struct pt_regs *r)
{
    unsigned long caller;
    if (current->pid == target_pid &&
        !copy_from_kernel_nofault(&caller, (void *)r->sp, sizeof(caller)) &&
        caller != last_caller) {
        last_caller = caller;
        pr_info("evo_bail: teardown yield caller %pS\n", (void *)caller);
    }
    return 0;
}

static void post(struct kprobe *p, struct pt_regs *r, unsigned long flags) { }

static int notifier_before(struct kprobe *p, struct pt_regs *r)
{
    u8 snapshot[0xb0], wait;
    u32 klass, instance, capacity;
    if (current->pid != target_pid ||
        copy_from_kernel_nofault(snapshot, (void *)r->r14, sizeof(snapshot)) ||
        copy_from_kernel_nofault(&wait, (void *)(r->sp + 0x1c), 1))
        return 0;
    memcpy(&klass, snapshot + 0x30, 4);
    memcpy(&instance, snapshot + 0x34, 4);
    memcpy(&capacity, snapshot + 0x98, 4);
    if (klass != 0xc67d || instance != 0 || capacity != 4048 || wait != 1)
        return 0;
    if (!notifier_hits++)
        pr_info("evo_bail: validated stalled notifier in target teardown, armed=%d\n", notifier_armed);
    if (!READ_ONCE(notifier_armed)) return 0;
    /* Return FALSE, not a fabricated completed notifier. Same lost-device
     * exit as the experimental source patch. Void outer API ignores status.
     */
    r->ax = 0;
    r->ip = (unsigned long)p->addr - 0x9f + 0xf5;
    notifier_rescues++;
    WRITE_ONCE(notifier_armed, false);
    pr_info("evo_bail: aborted this lost-device notifier wait with FALSE\n");
    return 1;
}

static int before(struct kprobe *p, struct pt_regs *r)
{
    u8 snapshot[0xb0];
    unsigned long channel = r->bx, base, buffer;
    u32 capacity, free_count, klass, instance;
    if (current->pid != target_pid)
        return 0;
    if (copy_from_kernel_nofault(snapshot, (void *)channel, sizeof(snapshot)))
        return 0;
    memcpy(&base, snapshot + 0x80, 8);
    memcpy(&buffer, snapshot + 0x88, 8);
    memcpy(&capacity, snapshot + 0x98, 4);
    memcpy(&free_count, snapshot + 0xa0, 4);
    memcpy(&klass, snapshot + 0x30, 4);
    memcpy(&instance, snapshot + 0x34, 4);
    if ((klass != 0xc67d && klass != 0xc67e) || instance > 7 || capacity != 4048 ||
        r->r14 != 2 || r->r12 != 4040 || buffer != base + 4040 ||
        base < 0xffff800000000000UL || (base & 3) || free_count > 2)
        return 0;
    if (!hits++)
        pr_info("evo_bail: validated target PID %d, class=%x capacity=%u offset=%lu count=%lu free=%u; armed=%d\n",
                target_pid, klass, capacity, r->r12, r->r14, free_count, armed);
    if (!READ_ONCE(armed))
        return 0;
    /* Same host-ring reset required by the published lost-device patch.
     * The target owns NvKMS's lock. Keep writes in bounds for the caller.
     */
    WRITE_ONCE(*(unsigned long *)(channel + 0x88), base);
    WRITE_ONCE(*(u32 *)(channel + 0xa0), capacity >> 2);
    rescues++;
    WRITE_ONCE(armed, false);
    /* Verified epilogue: add rsp,8; pop rbx,rbp,r12,r13,r14,r15; return thunk.
     * The probe sits at the yield call, with precisely that stack frame.
     */
    r->ip = (unsigned long)p->addr - 0xcf + 0x67;
    pr_info("evo_bail: restored host ring and escaped this stalled teardown wait\n");
    return 1;
}

static int __init begin(void)
{
    int rc;
    u8 start[16], end[12];
    static const u8 expected_start[] = {
        0xf3,0x0f,0x1e,0xfa,0x41,0x57,0x41,0x56,
        0x41,0x89,0xf6,0x41,0x55,0x41,0x54,0x55 };
    static const u8 expected_end[] = {
        0x48,0x83,0xc4,0x08,0x5b,0x5d,0x41,0x5c,0x41,0x5d,0x41,0x5e };
    if (target_pid <= 0 || armed || notifier_armed)
        return -EINVAL;
    probe.symbol_name = "nvEvoMakeRoom";
    probe.offset = 0xcf;
    probe.pre_handler = before;
    probe.post_handler = post; /* Prevent jump optimization. */
    probe.flags = KPROBE_FLAG_DISABLED;
    rc = register_kprobe(&probe);
    if (rc)
        return rc;
    if (copy_from_kernel_nofault(start, (u8 *)probe.addr - 0xcf, sizeof(start)) ||
        copy_from_kernel_nofault(end, (u8 *)probe.addr - 0xcf + 0x67, sizeof(end)) ||
        memcmp(start, expected_start, sizeof(start)) ||
        memcmp(end, expected_end, sizeof(end))) {
        unregister_kprobe(&probe);
        return -EINVAL;
    }
    yield_probe.symbol_name = "nvkms_yield";
    yield_probe.pre_handler = inspect_yield;
    rc = register_kprobe(&yield_probe);
    if (rc) { unregister_kprobe(&probe); return rc; }
    notifier_probe.symbol_name = "EvoCheckNotifier";
    notifier_probe.offset = 0x9f;
    notifier_probe.pre_handler = notifier_before;
    notifier_probe.post_handler = post;
    notifier_probe.flags = KPROBE_FLAG_DISABLED;
    rc = register_kprobe(&notifier_probe);
    if (rc) { unregister_kprobe(&yield_probe); unregister_kprobe(&probe); return rc; }
    {
        static const u8 expected[] = { 0x48,0x83,0xc4,0x28,0x5b,0x5d,0x41,0x5c,0x41,0x5d,0x41,0x5e };
        if (copy_from_kernel_nofault(end, (u8 *)notifier_probe.addr - 0x9f + 0xf5, sizeof(end)) ||
            memcmp(end, expected, sizeof(end))) {
            unregister_kprobe(&notifier_probe); unregister_kprobe(&yield_probe); unregister_kprobe(&probe);
            return -EINVAL;
        }
    }
    rc = enable_kprobe(&notifier_probe);
    if (rc) { unregister_kprobe(&notifier_probe); unregister_kprobe(&yield_probe); unregister_kprobe(&probe); return rc; }
    rc = enable_kprobe(&probe);
    if (rc) { unregister_kprobe(&notifier_probe); unregister_kprobe(&yield_probe); unregister_kprobe(&probe); }
    return rc;
}
static void __exit finish(void) { unregister_kprobe(&notifier_probe); unregister_kprobe(&yield_probe); unregister_kprobe(&probe); }
module_init(begin);
module_exit(finish);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Disabled-by-default incident-specific EVO teardown rescue probe");
