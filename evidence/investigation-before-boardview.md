# No-reboot GPU recovery investigation — 2026-10-14

Outcome: NOT RECOVERED. No working recovery script has been demonstrated on this server. The original recovery watcher is disabled and inactive. Linux boot ID remains b5f63608-9751-454b-864e-2a0b648a2dc7.

Hardware: MSI PRO Z690-A WIFI DDR4 (MS-7D25), RTX 3090 GA102, NVIDIA open driver 615.71.09, Ubuntu kernel 7.0.0-38-generic. GPU 0000:01:00.0 and audio 0000:01:00.1 are the only children of root port 0000:00:01.0. No unrelated storage or network device was reset.

## Actual hardware tests

| Test | Observed result |
| --- | --- |
| NVIDIA NVML GPU reset, clients stopped | Cannot acquire GPU handle; reset unavailable |
| Driver unload | UVM unloaded; DRM unload remains stuck in nvEvoMakeRoom / nvkms_yield |
| Isolated secondary bus hot reset | GPU PCI vendor remains ffff |
| Link Disable toggle and retrain | GPU PCI vendor remains ffff; DLL Active remains clear |
| Temporary Gen1 retrain | GPU vendor remains ffff; original Gen4 target restored |
| Root-port D3hot / D0 transition | Root returned to D0; GPU vendor remains ffff |
| Linux root-port sysfs reset (PM method) | Sysfs write returned successfully; GPU vendor remains ffff |
| Explicit Gen3 equalization through Intel LCTL3.A34 | GPU vendor remains ffff; original target restored |
| Extended five-second secondary bus reset | GPU vendor remains ffff |
| Live ACPI DL23 / L23D methods on PEG1 | Methods executed; GPU vendor remains ffff; LTSSM value 1 |
| Read-only GPU BAR0 PMC_BOOT_0 | ffffffff; direct MMIO also cannot reach GPU |
| S3 freezer-only preliminary test | Blocked before freezing or entering hardware sleep, inside NVIDIA PM notifier -> nvkms_suspend |

The suspend command remains blocked in the kernel despite SIGKILL. A request to restore pm_test to none is also blocked behind the system-sleep mutex. The currently selected test mode remains freezer, which stops before hardware sleep. No real S3 power transition occurred. Do not issue another suspend or power-management test while these kernel calls remain blocked. User-space timeout cannot terminate a task stalled in an uninterruptible kernel wait. Harness and llama user services were restarted; inference cannot function with the lost GPU.

## Firmware and low-level inspection

Dumped all ACPI tables with acpidump and disassembled DSDT/SSDTs with iasl. Examined the running namespace through Linux acpidbg, not just static tables.

Actual GPU path: \\_SB.PC00.PEG1.PEGP. PEG1 has DL23/L23D link-state methods. Its _PS0/_PS3 wrappers conditionally call PPS0/PPS3, which are absent from the running PEG1 namespace. There are no _OFF/_ON/_RST methods or GPU power resources in that namespace. VMD PEG power methods elsewhere in the firmware are separate objects and were not invoked.

Root-port Slot Capabilities report PwrCtrl- and HotPlug-. The writable /sys/bus/pci/slots/0/power belongs to 0000:05:00, NOT the GPU slot; it was not touched. No IPMI device/controller is exposed. The host I801 SMBus adapter exists, but no documented RTX 3090 management/reset endpoint was established; no speculative SMBus writes or broad bus probes were performed.

## Driver teardown and live patching

Retrieved the exact 615.71.09 nvkms-dma.c source. nvEvoMakeRoom logs an error every five seconds but resets the timeout and keeps waiting. The observed stack and repeated 2:0:4048:4040 error match this loop.

Reviewed the experimental eGPU lost-device patch. It both bounds waits and resets the host pushbuffer correctly to avoid a CPU buffer overrun when abandoning a lost-GPU queue. Blindly returning from the existing void function would omit this requirement.

The stuck nvEvoMakeRoom function is absent from this running kernel's available_filter_functions. Only nvkms_yield is traceable among the inspected wait functions. Standard ftrace-based livepatch cannot replace the affected function here. Replacing nvkms_yield or forcing a thread to exit does not safely unwind driver locks, objects, or pushbuffer accounting. No unverified kernel text, stack, or firmware-flash modifications were applied.

## What remains unproved / possible future engineering

A patched driver loaded BEFORE a failure may permit orderly detach and rebind; that fixes teardown, not necessarily a physically lost link. This cannot currently be loaded in place of the stuck module through normal module management. GPU-only electrical power control or a verified independent PERST# control would provide another reset mechanism, but no such interface has been found on this installed motherboard/card. Dedicated hot-plug hardware may provide that mechanism; ordinary live unplugging or manipulating GPU power connectors is not a substitute.

The investigation does not prove that every Xid79 is impossible to recover without reboot. It establishes that all tested interfaces failed for this specific incident, and no successful software-only method has been found for this server.

## Primary sources

- NVIDIA Xid79/recovery prototype and its correction: https://github.com/NVIDIA/open-gpu-kernel-modules/discussions/1327 — the author explicitly says the inaccessible-GPU event did not recover.
- Experimental eGPU teardown patch and hardware tests: https://github.com/NVIDIA/open-gpu-kernel-modules/discussions/1190 — clean surprise removal, not proof that this motherboard can power-reset a lost card.
- Exact installed driver source: https://raw.githubusercontent.com/NVIDIA/open-gpu-kernel-modules/615.71.09/src/nvidia-modeset/src/nvkms-dma.c
- Linux livepatch mechanics and consistency: https://docs.kernel.org/livepatch/livepatch.html
- Linux PCI power management: https://docs.kernel.org/power/pci.html
- Intel 12th generation LCTL3 register: https://edc.intel.com/content/www/us/en/design/publications/12th-generation-core-processor-datasheet-volume-2-of-2/link-control-3-lctl3-offset-a34/

Evidence: acpi/*.dat, acpi/*.dsl, acpi/live-gpu-namespace.txt, stalled-driver-stack.txt, stalled-suspend-stack.txt, kernel-experiments.log, nvkms-dma.c, nvkms-dma-egpu.c.

## Additional investigation: motherboard signals and installed machine code

Retrieved upstream coreboot's source for this exact board:

- https://github.com/coreboot/coreboot/blob/main/src/mainboard/msi/ms7d25/gpio.h
- https://github.com/coreboot/coreboot/blob/main/src/mainboard/msi/ms7d25/devicetree.cb

Saved these as ms7d25-gpio.h and ms7d25-devicetree.cb. Collected live read-only pin configuration and ownership in live-gpio-registers.txt and live-gpio-mux.txt. GPP_B13 is identified as PLTRST# in the board firmware configuration and is in native mode in the running machine. This is a platform reset signal, not a verified independently routed GPU reset. Several GPIO outputs are configured but their motherboard net connections are not identified in the source. No GPIO outputs were toggled.

Extracted the installed nvidia-modeset.ko.zst and disassembled nvEvoMakeRoom into nvEvoMakeRoom-disassembly.txt. The binary confirms the source-level indefinite retry: the error path resets its timeout and jumps back into the loop. The affected core function has no ftrace entry despite the module containing instrumentation for other functions. A custom kprobe/register/stack intervention would be a separate, unvalidated runtime patch; it is not equivalent to safely loading the published lost-device driver patch. It also would not by itself restore the electrical PCIe link. No such intervention was installed.

Remaining missing hardware information: a board netlist/schematic proving how PCI_E1 PERST# and slot power connect to controllable signals, and whether any purported independent control actually excludes storage, networking, CPU, and chipset reset. Publicly indexed boardview archives for MS-7D25 Rev 1.1 were found; an attempted attachment fetch returned HTTP403, and another publisher requires paid membership. No authentication or paid-download restrictions were bypassed. The boardview itself was not obtained, so no independent GPU reset pin has been established.
