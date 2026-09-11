# Ascend910B4 single-card matmul observations

A single-card Ascend910B4 sustains 0.95 of its fp16 and bf16 dense matmul peak and 0.65 of its int8 quantized matmul peak

This is a historical reference record. The source snapshot or measurement has not been independently confirmed; compare it with the current stack and measurements.

## Conditions

- soc: Ascend910B4, 910B4
- cann: not recorded
- driver: not recorded
- python_abi: not recorded
- torch: not recorded
- torch_npu: 2.10.0
- vllm: The timed call is torch_npu's matmul, invoked directly. No vLLM code is on the timed path.
- vllm_ascend: The timed call is torch_npu's matmul, invoked directly. No vllm-ascend plugin code is on the timed path.
- model: A dense matmul microbenchmark on synthetic tensors. No model weights are read.
- topology: tp1
- execution_mode: eager
- component: The sustained fraction is a property of the silicon and the operator library rather than of any one vLLM subsystem; it is consumed by roofline and MFU analysis.

## Subject and method

- id: Ascend910B4
- aliases: 910B4
- aliases: Ascend910B4-32G
- aliases: A2-32G
- aliases: Atlas A2 32G
- family: Atlas A2
- type: microbenchmark
- description: Single-card 910B4, NPU index 4, torch_npu 2.10.0. A 8192x8192x8192 dense matmul timed with torch.npu.Event. The int8 case uses torch_npu.npu_quant_matmul; plain torch.mm does not accept int8 on this stack. The sustained fraction is the observed throughput divided by the CANN platform_config theoretical peak for the same precision.
- parameters: name: matmul_shape_m_n_k
- parameters: value: 8192x8192x8192
- parameters: name: device_index
- parameters: value: 4
- parameters: name: cards
- parameters: value: 1
- parameters: name: timing
- parameters: value: torch.npu.Event
- parameters: name: int8_operator
- parameters: value: torch_npu.npu_quant_matmul
- parameters: name: memory_gib
- parameters: value: 32
- source: kind: run_manifest
- source: ref: ascend-microbenchmark-npu4-2026-06-03
- source: note: Run manifest id as recorded by the contributing workspace, with the internal machine slot removed under redaction profile r2. The manifest itself is not public; the method description is what makes the number reproducible here.

## Recorded quantities

| Quantity | Basis | Value | Unit | Notes |
|---|---|---|---|---|
| fp16_dense_matmul_sustained | measured | 232.332682 | tflops | 8192x8192x8192 dense matmul, single card |
| bf16_dense_matmul_sustained | measured | 231.833233 | tflops | 8192x8192x8192 dense matmul, single card |
| int8_quant_matmul_sustained | measured | 319.337134 | tops | 8192x8192x8192 dense matmul, single card |
| fp16_dense_matmul_sustained_fraction | sustained | 0.95 | ratio | observed throughput divided by the CANN platform_config theoretical peak for the same precision |
| bf16_dense_matmul_sustained_fraction | sustained | 0.95 | ratio | observed throughput divided by the CANN platform_config theoretical peak for the same precision |
| int8_quant_matmul_sustained_fraction | sustained | 0.65 | ratio | observed throughput divided by the CANN platform_config theoretical peak for the same precision |

## Notes

- MFU denominator should still use theoretical peak for industry-comparable MFU.
- Sustained peak is intended for operator roofline expectation and reclaim ranking.
- INT8 uses torch_npu.npu_quant_matmul, not torch.mm; ordinary torch.mm does not support int8 on this stack.
- The CANN version, driver, Python ABI and torch version of the measuring stack were not recorded, so those dimensions are bounded on neither side rather than guessed. A sustained fraction plausibly moves with all four, so re-measuring on a recorded stack is what would let this be promoted.

Recorded 2026-09-08 by maoxx241 from vllm-ascend-workspace/vllm-ascend-workspace.
