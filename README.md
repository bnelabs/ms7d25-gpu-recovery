# MSI MS-7D25 GPU recovery without reboot

Server-specific recovery for an RTX 3090 after NVIDIA Xid79, using an independent motherboard GPIO connected to the GPU slot's PERST# pin.

**Recovered actual Xid79 faults without rebooting Linux.** On 2026-10-15, one automatic attempt failed fresh driver initialization; a later clean manual cycle passed CUDA verification. The updated watcher and adapter keep inference stopped on failure and allow one clean retry only after fresh initialization/CUDA failure. A complete cycle with the updated installed adapter passed CUDA verification and Swift inference. This does not guarantee recovery from every fault.

This repository is private because its evidence includes the supplied boardview, firmware tables, and server logs. Do not republish third-party reference files without checking their licenses.

## Verified server

| Item | Value |
| --- | --- |
| Motherboard | MSI PRO Z690-A WIFI DDR4 (MS-7D25) |
| Boardview | MS-7D25 R1.1, supplied through Google Drive |
| GPU | RTX 3090 GA102, PCI ID `10de:2204` |
| GPU / audio | `0000:01:00.0`, `0000:01:00.1` |
| Isolated root port | `0000:00:01.0` |
| Kernel | `7.0.0-38-generic` |
| NVIDIA open driver | `615.71.09` |
| GPIO controller / line | `INTC1056:00`, `/dev/gpiochip0`, offset `354` |
| Reset control | `GPP_E2`, physical pin `SATAXPCIE_2` |
| GPU BAR1 allocation | 32 GiB |
| GPU services | user `komedi`, UID `1000`, `llama-server.service`, `deepseek-harness.service` |

SMBIOS reports motherboard version `1.0`; the supplied CAD is R1.1. The relevant GPIO was verified against the live controller and successfully reset the installed GPU. This does not establish compatibility for other PCB variants.

## What made recovery possible

The CAD netlist shows:

```text
PCH GPP_E2 -- UTB1 pin 1 (AND input)
platform reset buffer -- R287 -- UTB1 pin 2 (AND input)
UTB1 pin 4 (AND output) -- PCI_E1 pin A11 (GPU PERST#)
```

UTB1 is a `74LVC1G08GV`. Pulling `GPP_E2` low asserts only the GPU slot's reset gate. Restoring it high releases reset. This recovered a card whose PCI vendor ID and GPU MMIO both returned all ones. PCI BARs and Resizable BAR configuration then needed restoration, followed by fresh driver initialization.

The old driver also hung while unloading. An incident-specific, guarded Kprobe helper released its stalled EVO queue/notifier waits. That helper is **experimental kernel intervention**, not an NVIDIA-supported driver fix. Its offsets and register assumptions are pinned to the verified module binary. The script checks the binary's SHA256 before changing anything; upgrades require revalidation.

## Install on this exact server

Required tools: Python 3, `make`, GCC, matching kernel headers, PCI utilities, NVIDIA driver tools, systemd, and the CUDA toolkit containing `nvcc`. The default CUDA compiler is `/usr/local/cuda-13.4/bin/nvcc`.

```bash
git clone git@github.com:bnelabs/ms7d25-gpu-recovery.git
cd ms7d25-gpu-recovery
./build.sh
sudo ./install.sh
sudo gpu-recover-ms7d25 --check
```

Installation copies root-owned files into `/usr/local`, installs the watcher service, and reloads systemd. It does not reset the GPU or enable the watcher automatically.

### Manual recovery

```bash
sudo gpu-recover-ms7d25
```

This interrupts GPU workloads. It persists which configured services were active, stops them, resets/rebuilds the GPU, verifies CUDA, and restarts those services only after verification. Failed recovery leaves them stopped; the saved state is reused by a later recovery. **It also resets a healthy GPU if invoked manually.** Use `--check` for a read-only compatibility check.

### Enable automatic Xid79 recovery

```bash
sudo systemctl enable --now gpu-xid79-recovery.service
sudo systemctl status gpu-xid79-recovery.service
```

The watcher follows new kernel events and checks the expected GPU UUID through NVML every 15 seconds, including at startup. It holds inference services before recovery or suppression. It allows at most three adapter invocations per hour; each invocation permits at most one clean initialization retry. Failed recovery suppresses further automatic attempts until the watcher is restarted. Timeout/teardown failures do not trigger a retry. It never invokes reboot, shutdown, suspend, or hibernate.

```bash
sudo journalctl -u gpu-xid79-recovery.service -f
sudo tail -f /var/log/gpu-recovery.log
```

To disable it:

```bash
sudo systemctl disable --now gpu-xid79-recovery.service
```

## Recovery sequence

1. Verify motherboard, GPU, isolated PCI topology, kernel, module hash, and resource allocation; acquire an exclusive lock.
2. Stop the previously active llama/Harness services and NVIDIA persistence service.
3. If the GPU is electrically inaccessible, pulse its independent PERST line and restore memory decoding for old driver cleanup.
4. Unload old NVIDIA modules. If an identified EVO wait stalls, attach the narrowly guarded helper to that unloading PID, permit individual validated exits, and remove the helper afterward. Abort on timeout or mismatch.
5. With all old modules removed, pulse PERST again. Restore both GPU-function BARs and the original 32 GiB GPU Resizable BAR setting **before** loading a fresh driver.
6. Load NVIDIA/CUDA modules; run a real CUDA transfer/correctness probe; confirm Linux boot ID is unchanged.
7. Restore previously active services only after successful verification. On failure, retain their desired state in `/var/lib/gpu-recovery/services.json` and leave inference stopped.

The helper does not preserve in-flight GPU work. It resets the host command ring to a valid empty state before abandoning a lost-GPU FIFO wait, avoiding the buffer-overrun risk of simply returning. Notifier waits return FALSE rather than manufacturing hardware completion. It acts only on the specified unloading PID and verified frame layouts.

## Tests and evidence

- Real Xid79: PCI vendor ID `ffff`, GPU `PMC_BOOT_0 = ffffffff`, link down, driver unload stuck for hours.
- GPIO recovery: vendor ID became `10de`, link status `3104` (active Gen4 x16), `PMC_BOOT_0 = b72000a1`.
- Driver cleanup: the stuck unload and suspend notifier exited; temporary probes removed; old NVIDIA modules unloaded.
- CUDA after recovery: transfer correctness passed; approximately 14.8 GB/s host-to-device and 14.5 GB/s device-to-host.
- Installed script: complete hardware recovery cycle passed at `18:58:24 +03`.
- Inference afterward: Swift answered `42`; approximately 16.6 GiB GPU memory used; Gen4 x16 measured under load.
- Original and final Linux boot ID: `b5f63608-9751-454b-864e-2a0b648a2dc7`.

See [FINDINGS.md](FINDINGS.md), [script test log](evidence/installed-script-test.log), [inference result](evidence/inference-after-recovery.json), and [kernel recovery log](evidence/recovery-success-kernel.log). The evidence directory also preserves the earlier unsuccessful ACPI/PCI investigations and source/binary analysis.

## Limits

This is verified on one server and one actual failure. It is not a guarantee for every Xid79, every driver state, or another motherboard. The driver rescue is offset-specific and intentionally refuses changed binaries. Kernel warnings were emitted by NVIDIA while disposing of lost GPU state; successful CUDA afterward does not establish that every driver/kernel resource was reclaimed perfectly.

Do not force module removal, bypass the compatibility checks, or toggle the platform-wide `PLTRST#` signal. A stalled kernel task may not respond to SIGKILL; user-space timeout does not make it safe to repeat a blocked operation.

## Sources and licenses

- [Supplied boardview](https://drive.google.com/file/d/1_x6aW7GtP_pAm1x-KpLqp6bqM8fLxPY0/view)
- [Nexperia gate datasheet](https://assets.nexperia.com/documents/data_sheet/74LVC1G08.pdf)
- [coreboot MS-7D25 GPIO configuration](https://github.com/coreboot/coreboot/blob/main/src/mainboard/msi/ms7d25/gpio.h)
- [NVIDIA lost-device teardown patch discussion](https://github.com/NVIDIA/open-gpu-kernel-modules/discussions/1190)
- [NVIDIA no-reboot prototype and its correction](https://github.com/NVIDIA/open-gpu-kernel-modules/discussions/1327)
- [Linux Kprobes execution-path documentation](https://docs.kernel.org/trace/kprobes.html)

Original code in this repository is GPL-2.0-only. Copied upstream sources retain their own copyright and license notices. Boardview and firmware evidence are reference data with no license grant implied.

## Prevent CPU-only fallback in llama-server

The server launcher checks the expected GPU UUID and runs the CUDA correctness probe before starting. Both the router command and model preset defaults require `CUDA0`. Default GPU layers are `all` and fitting is disabled. Models that cannot load with these settings fail instead of silently reducing offload. Q8 KV caches and existing contexts are preserved. Normal CPU sampling, tokenization and other host work still occur.

[The installed launcher](examples/llama-serve.sh) is included for this server. It is managed separately from `install.sh`. Set `device = CUDA0`, `n-gpu-layers = all` and `fit = off` in the wildcard model preset as well, so model workers inherit the requirement.

Validation on 2026-10-15:

- Ten unit tests cover bounded retries, timeout refusal, desired-service persistence, health failures, event suppression and correct multi-module loading.
- With CUDA devices hidden in a separate test process, llama-server exits with `invalid device: CUDA0`; it does not start a CPU worker.
- The updated installed adapter completed a controlled recovery on the physical GPU, passed CUDA transfer correctness, restored both services and preserved boot identity.
- The router and Swift worker both include `--device CUDA0`; Swift replied `42` to `19 + 23`.

The failure/retry branches have controlled unit coverage. A new spontaneous Xid79 has not yet exercised the updated watcher end to end.
