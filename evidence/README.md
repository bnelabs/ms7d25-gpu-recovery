# Evidence inventory

These records distinguish actual server results from hypotheses and historical failed approaches.

| File / directory | Purpose |
| --- | --- |
| `ms7d25-boardview.cad` | Supplied GENCAD 1.4 boardview with net connectivity |
| `boardview-index.json` | Parsed signal/component index |
| `live-gpio-registers.txt`, `live-gpio-mux.txt` | Read-only live controller configuration before the successful reset |
| `ms7d25-gpio.h`, `ms7d25-devicetree.cb` | Open coreboot board configuration used as reference |
| `nvkms-dma.c` | Exact NVIDIA 615.71.09 source of the stuck wait |
| `nvkms-dma-egpu.c` | Experimental upstream-fork lost-device wait implementation |
| `nvEvoMakeRoom-disassembly.txt`, `EvoCheckNotifier-disassembly.txt` | Disassembly of the installed module; basis for exact guarded offsets |
| `stalled-driver-stack.txt`, `stalled-suspend-stack.txt` | Kernel stacks captured during the failure |
| `kernel-experiments.log` | Earlier failed reset experiments |
| `recovery-success-kernel.log` | Successful hardware reset/teardown/reload sequence, including warnings and the corrected BAR-restoration error |
| `installed-script-test.log` | Actual installed script's compatibility rejection, complete seven-second recovery test, and CUDA correctness result |
| `inference-after-recovery.json` | Real llama-server response after recovery |
| `acpi/` | Captured ACPI tables, disassembly, and live GPU namespace |
| `investigation-before-boardview.md` | Historical findings, superseded by the final findings |
| `SHA256SUMS` | Checksums of saved evidence |

Reproduce the CAD reset-net trace:

```bash
python3 tools/trace_nets.py
```

The installed NVIDIA binary is not duplicated here. Its checksum is pinned in `manifest.json`; use the matching installed driver package to reproduce its disassembly. Generated helper modules and CUDA executables are rebuilt with `build.sh` and ignored by Git.

The boardview was user-supplied. Firmware tables and logs are specific to this server. This repository's original-code license does not grant redistribution rights to all reference data.
