import { useEffect, useMemo, useState } from "react";

export type GatePolicy = {
  ttft_target_seconds: number;
  tpot_target_seconds_per_token: number;
  waiting_target_requests: number;
  kv_target_fraction: number;
  latency_window: string;
  queue_window: string;
  hold_seconds: number;
  evaluation_step_seconds: number;
  ttft_min_observations: number;
  tpot_min_observations: number;
  max_sample_age_seconds: number;
};

export type GateInput = { value: number | null; observations?: number | null; age_seconds: number | null };
export type GateEvaluation = {
  timestamp: number;
  state: string;
  valid: boolean;
  inputs: { ttft: GateInput; tpot: GateInput; waiting: GateInput; kv: GateInput };
  latency_breach: boolean | null;
  capacity_pressure: boolean | null;
  candidate: boolean | null;
  candidate_start: number | null;
  confirmed_at: number | null;
  remaining_hold_seconds: number | null;
  reasons: string[];
};
export type GatePod = { pod: string; state: string; latest?: GateEvaluation | null; evaluations: GateEvaluation[]; reason?: string | null };
export type GateWorkload = { deployment: string; model_name: string; expected_pod_count: number; observed_pod_count: number; pods: GatePod[] };
export type GateSnapshot = {
  policy_id: string;
  policy_version: string;
  policy: GatePolicy;
  scope: string;
  workloads: GateWorkload[];
  warmup_available: boolean;
};

const number = (value: number | null | undefined, unit = "") => {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value)}${unit ? ` ${unit}` : ""}`;
};
const time = (value: number | null | undefined, timezone: string) => value === null || value === undefined
  ? "—" : new Intl.DateTimeFormat("en-GB", { timeZone: timezone, dateStyle: "short", timeStyle: "medium" }).format(new Date(value * 1000));
const pct = (value: number | null | undefined) => value === null || value === undefined ? "—" : number(value * 100, "%");
const stateName: Record<string, string> = {
  within_targets: "Within targets", pressure_warning: "Pressure warning", latency_investigation: "Latency investigation",
  review_pending: "Review pending", scale_out_review: "Capacity review", insufficient_data: "Insufficient data",
};
const stateClass: Record<string, string> = {
  within_targets: "gate-state-good", pressure_warning: "gate-state-pressure", latency_investigation: "gate-state-latency",
  review_pending: "gate-state-pending", scale_out_review: "gate-state-review", insufficient_data: "gate-state-unknown",
};
const predicate = (value: boolean | null) => value === null ? "Unknown" : value ? "Breach" : "Clear";

function latestSummary(workload: GateWorkload) {
  const latest = workload.pods.map((pod) => pod.latest).filter((item): item is GateEvaluation => Boolean(item));
  const confirmed = latest.filter((item) => item.state === "scale_out_review").length;
  const pending = latest.filter((item) => item.state === "review_pending").length;
  const unknown = workload.pods.filter((pod) => pod.state === "insufficient_data").length;
  const latency = latest.filter((item) => item.state === "latency_investigation").length;
  const pressure = latest.filter((item) => item.state === "pressure_warning").length;
  if (confirmed) return { label: "Capacity review signal", detail: `${confirmed} pod${confirmed === 1 ? "" : "s"} sustained both predicates for the configured hold.` };
  if (unknown || latest.length < workload.expected_pod_count) return { label: "Insufficient data", detail: `${unknown || Math.max(0, workload.expected_pod_count - latest.length)} pod${unknown === 1 ? "" : "s"} lack complete fresh four-input evidence.` };
  if (pending) return { label: "Review pending", detail: `${pending} pod${pending === 1 ? "" : "s"} currently meet both predicates and are accumulating hold time.` };
  if (latency && pressure) return { label: "Signals on separate pods", detail: "Latency and pressure are observed, but not together on the same pod; they do not combine into a gate candidate." };
  if (latency) return { label: "Latency investigation", detail: "At least one pod exceeds a latency target without simultaneous queue or KV pressure." };
  if (pressure) return { label: "Pressure warning", detail: "At least one pod exceeds a queue or KV target without simultaneous latency breach." };
  return { label: "Within targets", detail: "No pod currently has both latency and capacity pressure predicates." };
}

function GateEvidenceCells({ evaluation }: { evaluation: GateEvaluation | null | undefined }) {
  const values = evaluation?.inputs;
  return <>
    <td>{number(values?.ttft?.value, "s")} <small>{number(values?.ttft?.observations)} obs</small></td>
    <td>{number(values?.tpot?.value, "s/token")} <small>{number(values?.tpot?.observations)} obs</small></td>
    <td>{number(values?.waiting?.value, " requests")}</td>
    <td>{pct(values?.kv?.value)}</td>
    <td>{predicate(evaluation?.latency_breach ?? null)}</td>
    <td>{predicate(evaluation?.capacity_pressure ?? null)}</td>
  </>;
}

export function ScaleGatePanel({
  gate, policy, onPolicyChange, onApply, settingsDirty, timezone,
}: {
  gate: GateSnapshot; policy: GatePolicy; onPolicyChange: (next: GatePolicy) => void;
  onApply: () => void; settingsDirty: boolean; timezone: string;
}) {
  const [numericDrafts, setNumericDrafts] = useState<Partial<Record<keyof GatePolicy, string>>>({});
  useEffect(() => setNumericDrafts({}), [policy]);
  const patch = <K extends keyof GatePolicy>(key: K, value: GatePolicy[K]) => onPolicyChange({ ...policy, [key]: value });
  const numeric = (key: keyof GatePolicy, title: string, unit: string, min: number, step: number, max?: number) => <label className="gate-policy-field" key={key}>
    <span>{title}</span><div><input type="number" min={min} step={step} onChange={(event) => {
      setNumericDrafts((prior) => ({ ...prior, [key]: event.target.value }));
    }} onBlur={() => {
      const draft = numericDrafts[key];
      if (draft === undefined) return;
      const value = Number(draft);
      if (draft.trim() && Number.isFinite(value) && value >= min && (max === undefined || value <= max)) patch(key, value as GatePolicy[typeof key]);
      setNumericDrafts((prior) => { const next = { ...prior }; delete next[key]; return next; });
    }} value={numericDrafts[key] ?? String(policy[key])} /><small>{unit}</small></div>
  </label>;
  const select = (key: "latency_window" | "queue_window" | "evaluation_step_seconds" | "hold_seconds", title: string, options: Array<[string, string]>) => <label className="gate-policy-field" key={key}>
    <span>{title}</span><select value={String(policy[key])} onChange={(event) => patch(key, (key.endsWith("window") ? event.target.value : Number(event.target.value)) as GatePolicy[typeof key])}>{options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
  </label>;
  return <section className="scale-gate" aria-labelledby="scale-gate-title">
    <div className="scale-gate-heading"><div><div className="eyebrow">PER-POD CAPACITY REVIEW</div><h3 id="scale-gate-title">Four-metric scale gate</h3><p>Latency breach AND capacity pressure must be true on the same pod, with complete fresh evidence, continuously for the hold.</p></div><span className="capacity-tag">Review only · no scaling action</span></div>
    <div className="gate-policy-block">
      <div className="gate-policy-label">Editable SLO and pressure limits · prefilled with uncalibrated example defaults</div>
      <div className="gate-policy-controls">
        {numeric("ttft_target_seconds", "TTFT P95 SLO", "seconds", 0.001, 0.1)}
        {numeric("tpot_target_seconds_per_token", "Request TPOT P95 SLO", "seconds/token", 0.001, 0.01)}
        {numeric("waiting_target_requests", "Mean waiting target", "requests", 0, 1)}
        {numeric("kv_target_fraction", "KV occupancy target", "fraction · 0–1", 0, 0.01, 1)}
      </div>
      <details className="gate-policy-advanced"><summary>Timing, sample and freshness settings</summary><div className="gate-policy-controls gate-policy-advanced-grid">
        {select("latency_window", "TTFT and TPOT window", [["1m", "1 minute"], ["2m", "2 minutes"], ["5m", "5 minutes"], ["10m", "10 minutes"], ["15m", "15 minutes"]])}
        {select("queue_window", "Waiting average window", [["1m", "1 minute"], ["2m", "2 minutes"], ["5m", "5 minutes"], ["10m", "10 minutes"], ["15m", "15 minutes"]])}
        {select("hold_seconds", "Continuous hold", [["60", "1 minute"], ["120", "2 minutes"], ["300", "5 minutes"], ["600", "10 minutes"], ["900", "15 minutes"]])}
        {select("evaluation_step_seconds", "Evaluation step", [["30", "30 seconds"], ["60", "1 minute"], ["120", "2 minutes"], ["300", "5 minutes"]])}
        {numeric("ttft_min_observations", "TTFT sample floor", "observations/window", 1, 1)}
        {numeric("tpot_min_observations", "TPOT sample floor", "observations/window", 1, 1)}
        {numeric("max_sample_age_seconds", "Maximum sample age", "seconds", 1, 1)}
      </div></details>
    </div>
    {settingsDirty && <p className="notice">Edited settings are not evaluated against this captured data yet. Apply the policy to query a new snapshot and recalculate history.</p>}
    {!gate.workloads.length ? <div className="chart-empty">No resolved workload is available for the scale gate.</div> : gate.workloads.map((workload) => {
      const summary = latestSummary(workload);
      return <div className="scale-gate-result" key={`${workload.model_name}/${workload.deployment}`}>
        <div className="scale-gate-status"><b>{workload.deployment}</b><strong>{summary.label}</strong><span>{summary.detail} {workload.pods.length} observed pod identities · policy {gate.policy_id}.</span></div>
        <div className="table-scroll"><table><thead><tr><th>Latest evaluation · pod</th><th>TTFT P95</th><th>Request TPOT P95</th><th>{policy.queue_window} mean waiting</th><th>Current KV occupancy</th><th>Latency</th><th>Pressure</th><th>State</th></tr></thead><tbody>
          {workload.pods.map((pod) => <tr key={pod.pod}>
            <td>{time(pod.latest?.timestamp, timezone)}<small>{pod.pod}</small></td>
            <GateEvidenceCells evaluation={pod.latest} />
            <td><span className={`gate-state ${stateClass[pod.state] || stateClass.insufficient_data}`}>{stateName[pod.state] || stateName.insufficient_data}</span>{pod.reason && <small>{pod.reason}</small>}</td>
          </tr>)}
        </tbody></table></div>
      </div>;
    })}
    <p className="scale-gate-footnote">Strict thresholds: T = TTFT P95 above target OR request-level TPOT P95 above target; P = {policy.queue_window} mean waiting above target OR current KV occupancy above target. T AND P must hold on the same pod. Histogram count floors and sample freshness must pass; missing or stale inputs reset hold. TPOT is request-level and is not ITL.</p>
    {settingsDirty && <button className="button button-primary gate-apply" onClick={onApply}>Apply gate policy and refresh snapshot</button>}
  </section>;
}

export function ScaleGateHistoryPanel({ gate, timezone, rangeLabel, settingsDirty }: {
  gate: GateSnapshot; timezone: string; rangeLabel: string; settingsDirty: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const allRows = useMemo(() => gate.workloads.flatMap((workload) => workload.pods.flatMap((pod) =>
    pod.evaluations.map((evaluation, index) => ({ workload, pod, evaluation, previous: pod.evaluations[index - 1] })))), [gate]);
  const milestones = useMemo(() => allRows.filter(({ evaluation, previous }) => {
    if (evaluation.state === "review_pending") return !previous || !["review_pending", "scale_out_review"].includes(previous.state);
    if (evaluation.state === "scale_out_review") return !previous || previous.state !== "scale_out_review";
    return Boolean(previous && ["review_pending", "scale_out_review"].includes(previous.state) && evaluation.state === "insufficient_data");
  }), [allRows]);
  const rows = (showAll ? allRows : milestones).slice().reverse();
  const confirmedCount = milestones.filter(({ evaluation }) => evaluation.state === "scale_out_review").length;
  const pendingCount = milestones.filter(({ evaluation }) => evaluation.state === "review_pending").length;
  return <section className="wide-card scale-history" aria-labelledby="scale-history-title">
    <div className="wide-card-title"><div><h2 id="scale-history-title">Historical four-metric evaluations</h2><p>Per-pod evaluation every {gate.policy.evaluation_step_seconds}s across the captured range. A capacity review is recorded only after a valid same-pod candidate persists for the configured hold.</p></div><span className="data-note">{rangeLabel}</span></div>
    {settingsDirty && <p className="notice">History below uses the captured policy. Apply edited gate settings from Capacity overview to capture and evaluate them.</p>}
    <div className="scale-history-policy">TTFT P95 &gt; {number(gate.policy.ttft_target_seconds, "s")} OR request TPOT P95 &gt; {number(gate.policy.tpot_target_seconds_per_token, "s/token")} · mean waiting &gt; {number(gate.policy.waiting_target_requests, " requests")} OR KV &gt; {pct(gate.policy.kv_target_fraction)} · hold {number(gate.policy.hold_seconds, "s")} · policy {gate.policy_id}</div>
    <div className="scale-history-summary"><span><b>{confirmedCount}</b> review confirmation{confirmedCount === 1 ? "" : "s"}</span><span><b>{pendingCount}</b> pending candidate start{pendingCount === 1 ? "" : "s"}</span><span><b>{allRows.filter(({ evaluation }) => evaluation.state === "insufficient_data").length}</b> insufficient evaluations</span></div>
    <button className="button button-quiet" onClick={() => setShowAll((value) => !value)}>{showAll ? "Show gate milestones" : "Show all evaluations"}</button>
    {rows.length ? <div className="table-scroll scale-history-scroll"><table><thead><tr><th>Evaluation time</th><th>Workload / pod</th><th>TTFT P95</th><th>Request TPOT P95</th><th>Mean waiting</th><th>KV occupancy</th><th>Latency</th><th>Pressure</th><th>State</th><th>Hold evidence</th></tr></thead><tbody>{rows.map(({ workload, pod, evaluation }) => <tr className={evaluation.state === "scale_out_review" ? "gate-history-pass" : ""} key={`${workload.deployment}/${pod.pod}/${evaluation.timestamp}`}>
      <td>{time(evaluation.timestamp, timezone)}</td><td>{workload.deployment}<small>{pod.pod}</small></td>
      <td>{number(evaluation.inputs.ttft.value, "s")}<small>{number(evaluation.inputs.ttft.observations)} obs</small></td>
      <td>{number(evaluation.inputs.tpot.value, "s/token")}<small>{number(evaluation.inputs.tpot.observations)} obs</small></td>
      <td>{number(evaluation.inputs.waiting.value, " requests")}</td><td>{pct(evaluation.inputs.kv.value)}</td>
      <td>{predicate(evaluation.latency_breach)}</td><td>{predicate(evaluation.capacity_pressure)}</td>
      <td><span className={`gate-state ${stateClass[evaluation.state] || stateClass.insufficient_data}`}>{stateName[evaluation.state] || evaluation.state}</span></td>
      <td>{evaluation.candidate_start === null ? evaluation.reasons.join("; ") || "—" : `candidate since ${time(evaluation.candidate_start, timezone)}${evaluation.confirmed_at ? ` · confirmed ${time(evaluation.confirmed_at, timezone)}` : ` · ${number(evaluation.remaining_hold_seconds, "s")} remaining`}`}</td>
    </tr>)}</tbody></table></div> : <div className="chart-empty">No candidate or confirmation milestones were recorded for this selected period. Use “Show all evaluations” to inspect within-target and incomplete intervals.</div>}
    <p className="scale-gate-footnote">Recorded points are reconstructed from the captured monitoring data and current snapshot policy; this dashboard does not persist an independent audit log. A gap, stale input, low histogram count or restart breaks continuity and resets the hold. The evaluator never combines evidence across pods.</p>
  </section>;
}

export function ScaleGateGuidePage({ policy, settingsDirty, policyId }: { policy: GatePolicy; settingsDirty: boolean; policyId: string }) {
  return <section className="wide-card scale-guide" aria-labelledby="scale-guide-title">
    <div className="wide-card-title"><div><h2 id="scale-guide-title">How the four-metric scale gate works</h2><p>One latency predicate and one capacity-pressure predicate must be true at the same evaluation on the same pod.</p></div><span className="capacity-tag">Review only · no scaling action</span></div>
    {settingsDirty && <p className="notice">The diagram previews edited thresholds and timing. Apply them from Capacity overview before they are evaluated against monitoring data.</p>}
    <div className="scale-guide-policy">
      <span>TTFT P95 target <b>{number(policy.ttft_target_seconds, "s")}</b></span>
      <span>Request TPOT P95 target <b>{number(policy.tpot_target_seconds_per_token, "s/token")}</b></span>
      <span>Mean waiting target <b>{number(policy.waiting_target_requests, " requests")}</b></span>
      <span>KV occupancy target <b>{pct(policy.kv_target_fraction)}</b></span>
      <span>{settingsDirty ? "Draft policy" : "Captured policy"} <b>{settingsDirty ? "Not yet evaluated" : policyId}</b> · uncalibrated example</span>
    </div>
    <div className="scale-guide-diagram gate-four-diagram" role="group" aria-label="Four-metric scale gate evaluation flow diagram">
      <div className="scale-guide-stage">
        <div className="scale-guide-step"><span>STEP 1</span><div><h3>Measure one pod on a fixed grid</h3><p>Every {policy.evaluation_step_seconds} seconds, compare fresh values from the selected model and workload identity.</p></div></div>
        <div className="gate-four-inputs">
          <article><span>T · LATENCY</span><b>TTFT P95</b><small>{policy.latency_window} histogram window · {policy.ttft_min_observations}+ observations</small></article>
          <article><span>P · LATENCY</span><b>Request TPOT P95</b><small>{policy.latency_window} histogram window · {policy.tpot_min_observations}+ observations</small></article>
          <article><span>Q · PRESSURE</span><b>Mean waiting requests</b><small>{policy.queue_window} average across engines in the pod</small></article>
          <article><span>K · PRESSURE</span><b>Current KV occupancy</b><small>Worst engine fraction in the pod</small></article>
        </div>
        <div className="scale-guide-drop" aria-hidden="true">↓</div>
        <div className="scale-guide-step"><span>STEP 2</span><div><h3>Form two OR predicates</h3><p>Use strict greater-than thresholds; all four inputs must be valid and fresh before the predicates can be evaluated.</p></div></div>
        <div className="scale-guide-signals gate-four-signals">
          <article className="scale-guide-signal scale-guide-queue"><div className="scale-guide-signal-mark">T/P</div><div><div className="eyebrow">LATENCY BREACH</div><h3>TTFT P95 &gt; {number(policy.ttft_target_seconds, "s")} OR request TPOT P95 &gt; {number(policy.tpot_target_seconds_per_token, "s/token")}</h3><p>TTFT and request-level TPOT stay separate from inter-token latency. A missing/low-count histogram makes this predicate unknown.</p><span className="scale-guide-rule">T OR P</span></div></article>
          <article className="scale-guide-signal scale-guide-kv"><div className="scale-guide-signal-mark">Q/K</div><div><div className="eyebrow">CAPACITY PRESSURE</div><h3>Mean waiting &gt; {number(policy.waiting_target_requests, " requests")} OR KV occupancy &gt; {pct(policy.kv_target_fraction)}</h3><p>Queue is the mean per-engine waiting gauge; KV pressure is the maximum current engine occupancy within the same pod.</p><span className="scale-guide-rule">Q OR K</span></div></article>
        </div>
        <div className="scale-guide-join"><span className="scale-guide-and">AND</span><span>Latency breach AND capacity pressure · same pod · same evaluation</span></div>
        <div className="gate-hold-flow"><div><b>Candidate starts</b><span>Both predicates true and all four inputs valid.</span></div><div className="gate-hold-line"><span>Continuous for {number(policy.hold_seconds, "s")}</span></div><div><b>Capacity review</b><span>Confirm at the first valid evaluation at or after the hold.</span></div></div>
        <div className="scale-guide-outcome"><div><span className="eyebrow">RESULT · ADVISORY ONLY</span><h3>Review capacity, routing and KV-cache configuration</h3><p>This gate does not establish GPU saturation, choose replica count or trigger automatic scaling.</p></div><span className="scale-guide-check" aria-hidden="true">✓</span></div>
      </div>
    </div>
    <div className="scale-guide-notes">
      <div><b>Incomplete evidence</b><span>Missing or stale samples, a low histogram count, evaluation gap or pod restart makes the state insufficient and resets the hold.</span></div>
      <div><b>No cross-pod joins</b><span>Latency on one pod cannot combine with waiting or KV pressure on another. Service-pooled latency remains a separate diagnostic.</span></div>
      <div><b>Calibrate before use</b><span>Example thresholds are not Red Hat defaults. Request TPOT is a deliberate alternative to ITL and should be validated against workload and output length.</span></div>
    </div>
  </section>;
}
