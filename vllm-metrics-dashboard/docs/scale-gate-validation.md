# Qwen3.8-27B four-metric scale gate: review notes

## Decision rule implemented

The same-pod rule is evaluated every 30 seconds by default:

```text
latency_breach = TTFT_P95 > TTFT_target OR request_TPOT_P95 > TPOT_target
capacity_pressure = mean_waiting_requests > waiting_target OR current_KV_fraction > KV_target
candidate = latency_breach AND capacity_pressure
capacity_review = candidate remains true continuously for the configured hold
```

TTFT and request-level TPOT use P95 histogram queries over the configured rolling latency window; each has its own observation floor. Waiting requests use the configured rolling mean summed across engines within a pod. KV occupancy uses the current maximum engine fraction within that pod. The example defaults are TTFT 3 seconds, request TPOT 0.15 seconds/token, mean waiting 5 requests, KV occupancy 0.90, 5-minute latency and queue windows, a 5-minute hold, and a 30-second evaluation step. They are examples, not calibrated product defaults.

All four values must be valid and fresh before either predicate is evaluated. Missing metrics, insufficient histogram counts, stale samples, gaps, or pod identity changes leave the state insufficient and reset hold. Comparisons are strict `>`; a value equal to its target does not breach. Evaluation is per pod, so latency on one pod never joins pressure from another. The application shows a capacity-review signal only; it does not scale replicas.

## Correctness findings and qualifications

- vLLM documents `vllm:time_to_first_token_seconds` and `vllm:request_time_per_output_token_seconds` as distinct histograms, `vllm:num_requests_waiting` as a gauge, and `vllm:kv_cache_usage_perc` as a fraction in the 0–1 range ([vLLM metrics](https://docs.vllm.ai/en/v0.31.0/design/metrics/)). A cluster runtime still needs its own family/label compatibility check before live gate results are trusted.
- Request TPOT is intentionally not substituted with inter-token latency (ITL). vLLM defines request TPOT using request timing and output-token count; single-token requests can contribute zero. Validate the measure against the workload before treating its target as an SLO ([vLLM metrics](https://docs.vllm.ai/en/v0.31.0/design/metrics/)).
- Red Hat's OpenShift AI guidance emphasizes TTFT and ITL for latency-sensitive workloads and suggests queue-based scaling for uniform workloads; the request-level TPOT gate here is a proposed alternative that requires workload validation ([OpenShift AI 3.5 monitoring guidance](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/managing_and_monitoring_models/managing_and_monitoring_models)).
- The four thresholds, count floors, rolling windows, step, and hold are workload-specific policy inputs, not vLLM or Red Hat defaults. A 30-observation P95 floor is only a review guard; it is not a statistical-confidence claim.
- A queue or KV breach alone does not prove GPU compute saturation. Admission limits, scheduling, burst shape and upstream delays can also affect the result. Use the gate to prompt investigation, then inspect per-pod routing, readiness, demand and workload context before making capacity changes.
- The 141 GB premise does not uniquely identify an H200. NVIDIA's supported configuration tables list H20 96 GB and H20 141 GB configurations, while H200 is also listed at 141 GB ([NVIDIA NIM supported models](https://docs.nvidia.com/nim/large-language-models/1.15.0/supported-models.html)). Verify the exact accelerator/SKU and exposed memory from the node before using the brief's hardware premise. NVIDIA specifies 141 GB HBM3e for H200 ([H200 specifications](https://www.nvidia.com/en-au/data-center/h200/)).
- The Qwen3.8-27B model repository reports about 55.6 GB of files and a native 262,144-token context, with 64 layers ([Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B)). Those facts do not by themselves determine the deployed weight format, effective context, hybrid KV/recurrent-state allocation or usable concurrency. The brief should treat them as model metadata, not a KV-capacity calculation.

## Three dashboard tabs

Capacity overview exposes the four threshold inputs and the latest per-pod values, counts, predicates and state. Scale gate history shows the captured per-pod evaluations, candidate/confirmation milestones and incomplete intervals. Scale gate guide diagrams the four measures, the two OR predicates, the same-pod AND, and the continuous-hold requirement. All three views use the policy hash captured with the snapshot. Editing a policy requires applying a fresh snapshot before history is recalculated.

## Dashboard interpretation

The selected-range service-pooled latency metrics, throughput, request lengths, routing and readiness remain supporting evidence. A confirmed state is an advisory classification from monitored signals, not proof of GPU saturation or a replica-count recommendation. Missing or stale evidence must be reported as insufficient rather than interpreted as healthy capacity.
