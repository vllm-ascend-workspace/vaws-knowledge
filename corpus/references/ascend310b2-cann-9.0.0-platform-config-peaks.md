# Ascend310B2 CANN 9.0.0 platform constants

Ascend310B2 declares 9 platform constants in CANN 9.0.0 platform_config, including its theoretical dense matmul peaks

This is a historical reference record. The source snapshot or measurement has not been independently confirmed; compare it with the current stack and measurements.

## Conditions

- soc: Ascend310B2, Ascend310B
- cann: 9.0.0
- driver: The quantity is read from a CANN platform_config .ini file on disk. No device is opened and no driver call is made, so the driver revision cannot change what the file declares.
- python_abi: A vendor constant parsed out of a text file; no compiled extension participates, so the Python ABI cannot affect it.
- torch: The declaration is made by CANN, not by a framework. torch is not installed on the path that produced this number.
- torch_npu: The declaration is made by CANN, not by the torch_npu plugin, which is not involved in reading platform_config.
- vllm: A hardware capability declared by the vendor toolkit; no vLLM code runs to establish it.
- vllm_ascend: A hardware capability declared by the vendor toolkit; no plugin code runs to establish it.
- model: A peak throughput of the silicon. No weights are loaded and no model is executed.
- topology: A per-device declaration. It is the capability of one SoC and says nothing about, and is unaffected by, how many are used together.
- execution_mode: Nothing is executed. Graph capture and eager dispatch are alike irrelevant to what a configuration file states.
- component: A hardware capability constant. No software subsystem owns it; whichever consumer reads it gets the same number.

## Subject and method

- id: Ascend310B2
- aliases: Ascend310B
- core version: AIC-M-300
- compiler target: dav-m300
- family: Ascend310B
- type: vendor_platform_config
- description: Read out of the CANN 9.0.0 platform_config file for Ascend310B2, which lives under the CANN install root at aarch64-linux/data/platform_config/. Peaks are the values CANN itself declares or that follow from the declared cube shape and clock; no operator was executed. Reproduce by parsing the same .ini from any CANN 9.0.0 installation.
- parameters: name: cube_shape_m_n_k
- parameters: value: 16x16x16
- parameters: name: npu_arch
- parameters: value: 3002
- source: kind: vendor_file
- source: ref: cann-9.0.0:platform_config:Ascend310B2.ini
- source: note: CANN 9.0.0 aarch64-linux platform_config snapshot, taken 2026-06-02. The install prefix is left out of the reference on purpose: it is derivable from the release and adds nothing to followability, while the redaction ruleset reports a long mixed-case path as a possible credential. The machine the snapshot was taken on is not recorded either, and is not part of the claim: every CANN 9.0.0 installation ships this same file.

## Recorded quantities

| Quantity | Basis | Value | Unit | Notes |
|---|---|---|---|---|
| fp16_dense_matmul_peak | theoretical | 10.027008 | tflops | cube dense matmul, from declared cube shape and clock |
| int8_dense_matmul_peak | theoretical | 20.054016 | tops | cube dense matmul, from declared cube shape and clock |
| int4_dense_matmul_peak | theoretical | 40.108032 | tops | cube dense matmul, from declared cube shape and clock |
| memory_bandwidth_peak | theoretical | 20.808 | gbps | derived from the declared DDR rate in bytes per cycle |
| cube_clock | declared | 1224 | mhz |  |
| ai_core_count | declared | 1 | count |  |
| cube_core_count | declared | 1 | count |  |
| vector_core_count | declared | 1 | count |  |
| memory_rate | declared | 17 | bytes_per_cycle |  |

## Notes

- A runtime scan of the local CANN installation is preferable to this snapshot. It exists so that an analysis host without CANN can still resolve a peak instead of guessing one.
- These are theoretical peaks and are the correct MFU denominator. They are not what an operator achieves; a sustained fraction is a separate claim with basis=sustained.

Recorded 2026-09-08 by maoxx241 from vllm-ascend-workspace/vllm-ascend-workspace.
