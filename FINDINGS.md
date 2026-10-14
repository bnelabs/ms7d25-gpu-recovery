# Final findings

## Result

The GPU was recovered from the actual Xid79 incident without reboot, by asserting its dedicated PERST gate through `GPP_E2`. Both firmware access and CUDA operation were restored. The former conclusion that a physical power cycle was necessarily required was too strong: it preceded discovery of this reset gate in the supplied boardview.

## Reset wiring

The provided GENCAD 1.4 file contains 2,247 signal nets and 3,109 components.

| Net | Nodes |
| --- | --- |
| `GPP_E2` | `PCH1 AY40`, `UTB1 1`, `RTB1 1` |
| `N269145723` | `UTB1 2`, `R287 2`, `R296 1` |
| `PLTRST_BU1#_X16` | `PCI_E1 A11`, `UTB1 4`, `R296 2` |
| `PLTRST_BU1#` | reset branches for other slots and `R287 1` |

`UTB1` is `74lvc1g08gv`, a two-input AND gate. `RTB1` is a 10 kΩ pull-up to VCC3. R296 is labeled `x_0r/4`, consistent with a not-fitted bypass option. The successful independent reset is the actual hardware validation; population of every component was not physically inspected.

The live GPIO output, controller label, line offset, initial high state, and lack of a kernel consumer were checked before driving it. The pulse read back `1 -> 0 -> 1`. PCI identity changed from `ffff` to `10de`; PCIe resumed Gen4 x16. Storage and networking stayed online.

## Driver teardown

`nvEvoMakeRoom` in the installed NVIDIA 615.71.09 binary resets its timeout after reporting stalled progress and can retry indefinitely. The source and disassembly explain the observed unkillable module-unloading process.

The temporary helper targeted only that process. Exact prologue/epilogue bytes and live register/queue fields were checked in read-only mode first. It then reset the host ring to its base and full bounded capacity before branching to the verified epilogue. Separate stuck core notifier waits exited FALSE without writing a fabricated completed notification. Multiple display-channel instances required intervention. The unloader and pending NVIDIA suspend notifier both exited afterward.

This is experimental, version-specific kernel code. Binary checksum and frame checks are part of the recovery contract, not optional conveniences. The helper is removed after teardown and is not left armed during normal inference.

## PCI restoration and an implementation error

PERST clears endpoint configuration, including BARs and Resizable BAR controls. The kernel's cached resource allocation remains available, allowing restoration of the existing addresses without resetting any other PCI subtree.

During investigation, one Resizable BAR restoration was mistakenly attempted while the driver remained loaded; disabling bus mastering temporarily produced an NVIDIA firmware fault. That state was discarded, all modules were unloaded, and the independent reset was repeated. The corrected script refuses to continue after module-removal failure and restores Resizable BAR only after all old modules are gone. Subsequent complete script execution and CUDA/inference validation passed.

## Earlier failed approaches

NVML reset could not acquire a GPU handle. Secondary bus reset, link-disable/retrain, Gen1 retraining, explicit Gen3 equalization, root-port D3hot/D0, Linux root-port PM reset, and ACPI DL23/L23D all failed to restore electrical reachability. Direct GPU MMIO returned all ones.

The attempted freezer-only suspend test blocked in NVIDIA's PM notifier before reaching the freezer or hardware sleep. It eventually exited after guarded driver teardown. No actual S3 transition or reboot occurred, and `pm_test` is restored to `none`.

The historical [pre-boardview investigation](evidence/investigation-before-boardview.md) is preserved as a record of findings at that time, not as the current recovery conclusion.

## Validation boundary

The actual failed card was recovered using the documented primitives. The assembled installed script then passed a complete cycle on the healthy card. CUDA transfer correctness and real Swift inference passed. The watcher was enabled afterward; it has not yet received another spontaneous Xid79. Other PCB revisions, kernels, driver binaries, and failure modes remain unverified.
