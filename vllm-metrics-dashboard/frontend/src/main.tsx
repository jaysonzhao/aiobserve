import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, TitleComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { DEFAULT_TIMEZONE, localInput, wallTimeToIso } from "./time";
import { TOP10_IDS } from "./registry";
import "./style.css";

echarts.use([LineChart, DataZoomComponent, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

type Metric = {
  id: number; name: string; category: string; unit: string; kind: string;
  family: string; metric?: string | null; aggregation: string; window: string;
  scope: string; description: string; state?: string; reason?: string | null;
  capability?: string | null;
};
type Model = {
  model_id: string | null; model_name: string; deployment: string;
  deployment_resolved: boolean; deployment_source: string; pod_names: string[];
  job_values: string[]; instance_values: string[]; engine_values: string[]; source_values: string[];
  duplicate_scrape_paths: boolean;
};
type Point = { timestamp: number; value: number | null; raw: string };
type Series = { labels: Record<string, string>; deployment: string; model_name: string; points: Point[] };
type MetricResult = Metric & {
  summary_kind?: string; summary_unit?: string;
  series: Series[]; summary: Array<{ deployment: string; value: number | null }>;
  selected_range_total: Array<{ deployment: string; value: number | null }>;
  observations: Array<{ deployment: string; value: number | null }>;
  scale_gate_series?: Series[]; scale_gate_observations?: Series[];
  scale_gate_reasons?: Array<{ deployment: string; reason: string }>;
  coverage: Array<{ deployment: string; freshness_seconds: number | null; gaps: number }>;
  queries: Array<{ deployment: string; promql: string | null; summary_promql?: string | null; scale_gate_promql?: string | null; scale_gate_observation_promql?: string | null }>;
  warnings: string[]; scope_states: Array<{ deployment: string; state: string; reason?: string | null }>;
};
type Snapshot = {
  snapshot_id: string; filters: { namespace: string; model_id: string; models: Array<{ deployment: string; model_name: string; pods: string[] }>; range: { start_local: string; end_local: string; start_utc: string; end_utc: string; timezone: string; step_seconds: number; scope: string; window: string; percentile: number }; selectors: Record<string, string | null> };
  metrics: Record<string, MetricResult>; daily_tokens: { state: string; rows: Array<{ interval: { local_date: string; partial: boolean }; deployment: string; metric: string; unit: string; estimate: number | null; state: string; reason?: string }> };
  warnings: string[]; created_utc: string; evidence_note: string;
  scale_gate?: { window_seconds: number; windows: number; scope: string; queue_slo_seconds: number; queue_min_observations: number; queue_slo_is_provisional: boolean };
};

const REQ = { "Content-Type": "application/json", "X-Requested-With": "vllm-metrics-dashboard" };
const SECTION_METRIC_IDS: Record<string, number[]> = {
  latency: [1, 3, 4, 5, 6, 7, 8, 9, 29, 30, 31, 39],
  queues: [2, 10, 11, 12, 13, 14, 15, 16, 17, 18, 32, 33],
  traffic: [19, 20, 21, 22, 23, 24, 25, 28, 34, 35, 36],
  reliability: [26, 27, 37, 38, 39, 40, 41],
};

function statusLabel(state?: string) {
  return ({ valid: "Available", valid_zero: "Valid zero", stale: "Stale", no_observations: "No observations", not_exported: "Not exported", not_authorized: "Not authorized", query_error: "Query error", configuration_required: "Configure capability", unavailable: "Unavailable" } as Record<string, string>)[state || ""] || "Not queried";
}
function fmt(value: number | null | undefined, unit = "") {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value)}${unit ? ` ${unit}` : ""}`;
}
function currentMetric(snapshot: Snapshot | null, id: number) { return snapshot?.metrics[String(id)]; }

type ScaleGateWindow = { timestamp: number; queueP95: number | null; queueObservations: number | null; preemptions: number | null };
type ScaleGateEvaluation = {
  timestamp: number; windows: ScaleGateWindow[]; complete: boolean; gateMet: boolean; status: string;
  queueComplete: boolean; queueLowVolume: boolean; queueBreached: boolean; queueOverCount: number;
  preemptionComplete: boolean; recurrentPreemptions: boolean; preemptionWindows: number;
};
type ScaleGateResult = {
  deployment: string; windows: ScaleGateWindow[]; history: ScaleGateEvaluation[]; queueComplete: boolean; queueLowVolume: boolean;
  queueBreached: boolean; queueOverCount: number; preemptionComplete: boolean;
  recurrentPreemptions: boolean; preemptionWindows: number; summary: string; detail: string;
};

function scaleGateResults(snapshot: Snapshot, queueSlo: number, observationFloor: number): ScaleGateResult[] {
  const queue = snapshot.metrics["1"];
  const preemption = snapshot.metrics["2"];
  const deployments = snapshot.filters.models.map((model) => model.deployment);
  const seriesPoints = (metric: MetricResult | undefined, key: "scale_gate_series" | "scale_gate_observations", deployment: string) =>
    (metric?.[key] || []).filter((series) => series.deployment === deployment).flatMap((series) => series.points);
  const pointMap = (points: Point[]) => new Map(points.map((point) => [Math.round(point.timestamp), point.value]));
  return deployments.map((deployment) => {
    const queuePoints = seriesPoints(queue, "scale_gate_series", deployment);
    const countPoints = seriesPoints(queue, "scale_gate_observations", deployment);
    const preemptionPoints = seriesPoints(preemption, "scale_gate_series", deployment);
    const queueMap = pointMap(queuePoints);
    const countMap = pointMap(countPoints);
    const preemptionMap = pointMap(preemptionPoints);
    const timestamps = [...new Set([...queuePoints, ...countPoints, ...preemptionPoints].map((point) => Math.round(point.timestamp)))].sort((left, right) => left - right);
    const windows = timestamps.map((timestamp) => ({
      timestamp,
      queueP95: queueMap.get(timestamp) ?? null,
      queueObservations: countMap.get(timestamp) ?? null,
      preemptions: preemptionMap.get(timestamp) ?? null,
    }));
    const history: ScaleGateEvaluation[] = windows.map((_, index) => {
      const evidence = windows.slice(Math.max(0, index - 2), index + 1);
      const consecutive = evidence.length === 3 && evidence[1].timestamp - evidence[0].timestamp === 900 && evidence[2].timestamp - evidence[1].timestamp === 900;
      const queueComplete = consecutive && evidence.every((window) => window.queueP95 !== null && window.queueObservations !== null);
      const queueLowVolume = queueComplete && evidence.some((window) => (window.queueObservations ?? 0) < observationFloor);
      const queueBreached = queueComplete && !queueLowVolume && evidence.every((window) => (window.queueP95 ?? 0) > queueSlo);
      const queueOverCount = evidence.filter((window) => window.queueP95 !== null && window.queueP95 > queueSlo).length;
      const preemptionComplete = consecutive && evidence.every((window) => window.preemptions !== null);
      const preemptionWindows = evidence.filter((window) => (window.preemptions ?? 0) > 0).length;
      const recurrentPreemptions = preemptionComplete && preemptionWindows >= 2;
      const gateMet = queueBreached && recurrentPreemptions;
      const complete = queueComplete && preemptionComplete;
      const status = gateMet ? "Gate met" : !complete ? "Insufficient data" : queueLowVolume && recurrentPreemptions ? "Preemptions only · low-volume queue" : queueLowVolume ? "Low-volume queue" : queueBreached ? "Queue only" : recurrentPreemptions ? "Preemptions only" : "Neither signal";
      return { timestamp: evidence.at(-1)!.timestamp, windows: evidence, complete, gateMet, status, queueComplete, queueLowVolume, queueBreached, queueOverCount, preemptionComplete, recurrentPreemptions, preemptionWindows };
    });
    const latest = history.at(-1);
    const latestWindows = windows.slice(-3);
    const queueComplete = latest?.queueComplete || false;
    const queueLowVolume = latest?.queueLowVolume || false;
    const queueBreached = latest?.queueBreached || false;
    const queueOverCount = latest?.queueOverCount || 0;
    const preemptionComplete = latest?.preemptionComplete || false;
    const preemptionWindows = latest?.preemptionWindows || 0;
    const recurrentPreemptions = latest?.recurrentPreemptions || false;
    const gateReason = [...(queue?.scale_gate_reasons || []), ...(preemption?.scale_gate_reasons || [])]
      .find((item) => item.deployment === deployment)?.reason;
    let summary = "Insufficient data";
    let detail = gateReason || "The gate needs three consecutive completed 15-minute queue, sample-count, and preemption observations within the selected range.";
    if (queueBreached && recurrentPreemptions) {
      summary = "Both signals met — review capacity and KV configuration";
      detail = "Review demand, scheduling and KV-cache configuration before choosing a capacity change. These signals do not select a GPU topology by themselves.";
    } else if (queueBreached) {
      summary = "Queue gate met — capacity review";
      detail = "Consider a data-parallel replica if sustained service demand is the cause; check admission, scheduling and upstream delays before attributing the queue to GPU saturation.";
    } else if (recurrentPreemptions && queueLowVolume) {
      summary = "Preemption signal; queue evidence is low-volume";
      detail = "At least one 15-minute queue window is below the configured observation review floor. Treat its P95 as weak evidence; 30 observations is a review guard, not statistical confidence. Review recurrent preemptions separately as KV-cache pressure.";
    } else if (recurrentPreemptions) {
      summary = "Preemption signal — investigate KV pressure";
      detail = "Review KV-cache headroom, request context lengths and runtime settings. Recurrent preemptions alone do not establish that another GPU or TP=2 is required.";
    } else if (queueLowVolume) {
      summary = "Queue evidence is low-volume";
      detail = "At least one 15-minute queue window is below the configured observation review floor. Treat its P95 as weak evidence; 30 observations is a review guard, not statistical confidence.";
    } else if (queueComplete && preemptionComplete) {
      summary = "Neither gate met in these windows";
      detail = "This is not proof that one GPU meets future or forecast demand; continue monitoring at representative peak load.";
    }
    return { deployment, windows: latestWindows, history, queueComplete, queueLowVolume, queueBreached, queueOverCount, preemptionComplete, recurrentPreemptions, preemptionWindows, summary, detail };
  });
}

function ScaleGatePanel({ snapshot, queueSlo, setQueueSlo, observationFloor, setObservationFloor, sloConfirmed, setSloConfirmed, onApply, settingsDirty }: {
  snapshot: Snapshot; queueSlo: number; setQueueSlo: (value: number) => void;
  observationFloor: number; setObservationFloor: (value: number) => void; sloConfirmed: boolean; setSloConfirmed: (value: boolean) => void;
  onApply: () => void; settingsDirty: boolean;
}) {
  const results = scaleGateResults(snapshot, queueSlo, observationFloor);
  const timezone = snapshot.filters.range.timezone;
  const timeLabel = (timestamp: number) => new Intl.DateTimeFormat("en-GB", { timeZone: timezone, dateStyle: "short", timeStyle: "short" }).format(new Date(timestamp * 1000));
  return <section className="scale-gate" aria-labelledby="scale-gate-title">
    <div className="scale-gate-heading"><div><div className="eyebrow">CAPACITY DECISION PAIR</div><h3 id="scale-gate-title">Two-metric scale gate</h3><p>Service aggregate · latest three completed, non-overlapping 15-minute windows at or before the snapshot end · selected deployment and filters</p></div><span className="capacity-tag">Review only · no scaling action</span></div>
    <div className="scale-gate-controls">
      <label>Queue P95 SLO (seconds)<input type="number" min="0.01" step="0.05" value={queueSlo} onChange={(event) => setQueueSlo(Math.max(0.01, Number(event.target.value) || 0.01))} /><small>0.5 s is a provisional starting point from the brief, not a product default.</small></label>
      <label>Low-volume review floor<input type="number" min="1" step="1" value={observationFloor} onChange={(event) => setObservationFloor(Math.max(1, Math.floor(Number(event.target.value) || 1)))} /><small>Example review guard only; it does not establish statistical confidence.</small></label>
      <label className="gate-confirm"><input type="checkbox" checked={sloConfirmed} onChange={(event) => setSloConfirmed(event.target.checked)} /><span>Product team confirmed this queue SLO</span></label>
      {settingsDirty && <button className="button button-primary gate-apply" onClick={onApply}>Apply gate settings to snapshot</button>}
    </div>
    {!results.length ? <div className="chart-empty">No resolved workload is available for the scale gate.</div> : results.map((result) => <div className="scale-gate-result" key={result.deployment}>
      <div className="scale-gate-status"><b>{result.deployment}</b><strong>{result.summary}</strong><span>{result.detail}</span></div>
      <div className="table-scroll"><table><thead><tr><th>15-minute window ending</th><th>Queue P95</th><th>Queue samples</th><th>Preemptions</th></tr></thead><tbody>{result.windows.map((window) => <tr key={window.timestamp}><td>{timeLabel(window.timestamp)}</td><td>{window.queueP95 === null ? "—" : fmt(window.queueP95, "s")}</td><td>{window.queueObservations === null ? "—" : fmt(window.queueObservations)}</td><td>{window.preemptions === null ? "—" : fmt(window.preemptions)}</td></tr>)}</tbody></table></div>
      <div className="scale-gate-checks"><span>Queue rule: {result.queueComplete ? (result.queueLowVolume ? `low volume · ${result.queueOverCount}/3 over SLO` : `${result.queueOverCount}/3 over SLO`) : "insufficient queue windows"}</span><span>Preemption rule: {result.preemptionComplete ? `${result.preemptionWindows}/3 windows > 0` : "insufficient preemption windows"}</span></div>
    </div>)}
    {settingsDirty && <p className="scale-gate-footnote">The edited thresholds are a preview. Apply them to save the policy with this snapshot and its exports.</p>}
    <p className="scale-gate-footnote">The queue gate requires all three P95 values to exceed the editable SLO. The preemption signal is recurrent when more than zero events appear in at least two windows. Preemptions indicate KV-cache pressure and warrant configuration review; neither signal alone proves a specific GPU or tensor-parallel topology is required. Apply an interactive SLO only when the selected metric population is interactive traffic: this view aggregates the selected serving workload and does not infer an interactive/batch split from request behavior.</p>
  </section>;
}

function ScaleGateHistoryPanel({ snapshot, results, timezone, settingsDirty }: {
  snapshot: Snapshot; results: ScaleGateResult[]; timezone: string; settingsDirty: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const timeLabel = (timestamp: number) => new Intl.DateTimeFormat("en-GB", { timeZone: timezone, dateStyle: "short", timeStyle: "short" }).format(new Date(timestamp * 1000));
  const gate = snapshot.scale_gate;
  return <section className="wide-card scale-history" aria-labelledby="scale-history-title">
    <div className="wide-card-title"><div><h2 id="scale-history-title">Historical scale-gate evaluations</h2><p>Rolling 45-minute evaluations, advancing every 15 minutes across the selected range. A pass requires queue P95 above SLO in all three windows and preemptions in at least two.</p></div><span className="data-note">{snapshot.filters.range.start_local} — {snapshot.filters.range.end_local} · {timezone}</span></div>
    {settingsDirty && <p className="notice">This preview uses edited gate settings. Apply them from Capacity overview to save the policy with the snapshot.</p>}
    <div className="scale-history-policy">Queue P95 SLO {fmt(gate?.queue_slo_seconds ?? 0.5, "s")} · minimum observations/window {fmt(gate?.queue_min_observations ?? 30)} · service aggregate</div>
    {!results.length ? <div className="chart-empty">No resolved workload is available for scale-gate history.</div> : results.map((result) => {
      const complete = result.history.filter((evaluation) => evaluation.complete);
      const passes = complete.filter((evaluation) => evaluation.gateMet);
      const rows = (showAll ? complete : passes).slice().reverse();
      return <div className="scale-history-result" key={result.deployment}>
        <div className="scale-history-head"><div><h3>{result.deployment}</h3><p>{passes.length} gate pass{passes.length === 1 ? "" : "es"} across {complete.length} complete rolling evaluation{complete.length === 1 ? "" : "s"} · {result.history.length - complete.length} incomplete</p></div><button className="button button-quiet" onClick={() => setShowAll((value) => !value)}>{showAll ? "Show gate passes only" : "Show all completed evaluations"}</button></div>
        {rows.length ? <div className="table-scroll scale-history-scroll"><table><thead><tr><th>Rolling 45-minute interval</th><th>Queue P95 · 3 windows</th><th>Queue samples · 3 windows</th><th>Preemptions · 3 windows</th><th>Evaluation</th></tr></thead><tbody>{rows.map((evaluation) => <tr className={evaluation.gateMet ? "gate-history-pass" : ""} key={evaluation.timestamp}>
          <td>{timeLabel(evaluation.timestamp - 2700)} – {timeLabel(evaluation.timestamp)}</td>
          <td>{evaluation.windows.map((window) => fmt(window.queueP95, "s")).join(" / ")}</td>
          <td>{evaluation.windows.map((window) => fmt(window.queueObservations)).join(" / ")}</td>
          <td>{evaluation.windows.map((window) => fmt(window.preemptions)).join(" / ")}</td>
          <td><span className={`gate-history-status ${evaluation.gateMet ? "gate-history-status-met" : ""}`}>{evaluation.status}</span></td>
        </tr>)}</tbody></table></div> : <div className="chart-empty">{complete.length ? "No complete rolling 45-minute interval met the full scale gate in this selected range." : "No complete 45-minute evaluations are available in this selected range."}</div>}
      </div>;
    })}
    <p className="scale-gate-footnote">Only completed evidence is evaluated. Missing metric points and low-volume queue windows do not count as a pass. Historical gate results are observations for review, not proof of GPU saturation or an automatic scaling decision.</p>
  </section>;
}

function latestByDeployment(metric: MetricResult) {
  const grouped = new Map<string, number[]>();
  for (const series of metric.series) {
    const value = series.points.filter((point) => point.value !== null).at(-1)?.value;
    if (value !== null && value !== undefined) grouped.set(series.deployment, [...(grouped.get(series.deployment) || []), value]);
  }
  return [...grouped.entries()].map(([deployment, values]) => ({
    deployment,
    value: metric.kind === "counter_rate" || metric.kind === "counter_increase" ? values.reduce((sum, value) => sum + value, 0) : Math.max(...values),
  }));
}

function selectedRangeByDeployment(metric: MetricResult) {
  const values = metric.kind === "counter_increase" ? metric.selected_range_total : metric.summary;
  const grouped = new Map<string, number[]>();
  for (const item of values) if (item.value !== null && item.value !== undefined) grouped.set(item.deployment, [...(grouped.get(item.deployment) || []), item.value]);
  return [...grouped.entries()].map(([deployment, numeric]) => ({
    deployment,
    value: metric.kind === "counter_increase" || metric.summary_kind === "selected_range_increase" ? numeric.reduce((sum, value) => sum + value, 0) : Math.max(...numeric),
  }));
}

function selectedRangeLabel(metric: MetricResult) {
  if (metric.summary_kind === "selected_range_percentile") return "Selected-range percentile";
  if (metric.summary_kind === "selected_range_increase") return "Selected-range total";
  if (metric.summary_kind === "selected_range_total") return "Selected-range total";
  if (metric.kind === "counter_increase") return `Rolling ${metric.window} at end`;
  return "End of selection";
}

function MetricSummary({ metric }: { metric: MetricResult }) {
  const latest = latestByDeployment(metric);
  const selected = selectedRangeByDeployment(metric);
  const unit = metric.unit === "fraction (0-1)" ? "" : metric.unit;
  return <>
    <div className="metric-value-row"><div className="metric-value">{latest.length === 1 ? fmt(latest[0].value, unit) : latest.length > 1 ? "By workload" : "—"}</div>{latest.length === 1 && metric.unit === "fraction (0-1)" ? <span className="metric-equivalent">{fmt(latest[0].value * 100, "%")}</span> : null}</div>
    {latest.length > 1 && <div className="workload-values">{latest.map((item) => <span key={item.deployment}><b>{item.deployment}</b> {fmt(metric.unit === "fraction (0-1)" ? item.value * 100 : item.value, unit === "" ? "%" : unit)}</span>)}</div>}
    <div className="metric-subvalue">{selectedRangeLabel(metric)}{selected.length === 1 ? `: ${fmt(selected[0].value, metric.summary_unit || metric.unit)}` : selected.length > 1 ? " by workload" : ": —"}</div>
    {selected.length > 1 && <div className="workload-values selected-values">{selected.map((item) => <span key={item.deployment}><b>{item.deployment}</b> {fmt(item.value, metric.summary_unit || metric.unit)}</span>)}</div>}
  </>;
}

function MetricChart({ metric, timezone, rangeLabel, compact = false }: { metric: MetricResult; timezone: string; rangeLabel: string; compact?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const series = metric.series.map((item) => ({
      name: `${item.deployment}${item.labels.pod ? ` · ${item.labels.pod}` : ""}`,
      type: "line" as const, showSymbol: false, smooth: false,
      data: item.points.map((point) => [point.timestamp * 1000, point.value]),
      connectNulls: false, lineStyle: { width: compact ? 1.6 : 2 },
    }));
    chart.setOption({
      animation: false, color: ["#c9190b", "#0066cc", "#5752d1", "#3e8635", "#ec7a08", "#009596"],
      title: compact ? undefined : { text: metric.name, subtext: `${rangeLabel} · ${timezone} · ${metric.unit}`, left: 58, top: 2, textStyle: { color: "#151515", fontSize: 12, fontWeight: 600 }, subtextStyle: { color: "#6a6e73", fontSize: 9 } },
      grid: { left: compact ? 42 : 58, right: 14, top: compact ? 20 : 52, bottom: 48 },
      tooltip: { trigger: "axis", valueFormatter: (value: unknown) => `${fmt(Number(value), metric.unit)}` },
      legend: { show: !compact && series.length > 1, type: "scroll", bottom: 21, textStyle: { color: "#5f6e7c", fontSize: 10 } },
      dataZoom: compact ? [] : [{ type: "inside", filterMode: "none" }, { type: "slider", height: 13, bottom: 2, borderColor: "#d2d2d2", fillerColor: "#f2b8b5", handleStyle: { color: "#c9190b" } }],
      xAxis: { type: "time", axisLabel: { color: "#6a6e73", fontSize: 10, hideOverlap: true }, axisLine: { lineStyle: { color: "#d2d2d2" } }, splitLine: { show: false } },
      yAxis: { type: "value", scale: true, name: metric.unit, nameTextStyle: { color: "#6a6e73", fontSize: 9 }, axisLabel: { color: "#6a6e73", fontSize: 10 }, splitLine: { lineStyle: { color: "#ededed" } } },
      series,
    });
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(ref.current);
    return () => { resize.disconnect(); chart.dispose(); };
  }, [metric, timezone, rangeLabel, compact]);
  const download = () => {
    if (!ref.current) return;
    const chart = echarts.getInstanceByDom(ref.current);
    const link = document.createElement("a");
    link.href = chart?.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#fff" }) || "";
    link.download = `vllm-metric-${metric.id}.png`;
    link.click();
  };
  return <div className="chart-shell"><div className={compact ? "chart chart-compact" : "chart"} ref={ref} />{!compact && <button className="chart-download" onClick={download} title="Download chart PNG">PNG</button>}</div>;
}

type ComboOption = { value: string; label: string; disabled?: boolean };
function FilterCombobox({ value, options, placeholder, onChange, disabled = false }: { value: string; options: ComboOption[]; placeholder: string; onChange: (value: string) => void; disabled?: boolean }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const selected = options.find((option) => option.value === value);
  const visible = options.filter((option) => option.label.toLowerCase().includes(search.toLowerCase()));
  const choose = (option: ComboOption) => {
    if (option.disabled) return;
    onChange(option.value); setSearch(""); setOpen(false);
  };
  return <div className="combobox">
    <input
      role="combobox" aria-expanded={open} aria-controls={id} aria-autocomplete="list"
      aria-label={placeholder} autoComplete="off" disabled={disabled}
      placeholder={placeholder} value={open ? search : (selected?.label || "")}
      onFocus={() => { setSearch(""); setOpen(true); }}
      onChange={(event) => { setSearch(event.target.value); setOpen(true); }}
      onKeyDown={(event) => {
        if (event.key === "Escape") { setOpen(false); setSearch(""); }
        if (event.key === "Enter" && visible.length) { event.preventDefault(); choose(visible[0]); }
        if (event.key === "ArrowDown") setOpen(true);
      }}
      onBlur={() => window.setTimeout(() => setOpen(false), 100)}
    />
    <span className="combo-caret">⌄</span>
    {open && !disabled && <div className="combo-menu" id={id} role="listbox">{visible.length ? visible.map((option) => <button key={`${option.value}-${option.label}`} type="button" role="option" aria-selected={option.value === value} disabled={option.disabled} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(option)}>{option.label}</button>) : <div className="combo-empty">No matches</div>}</div>}
  </div>;
}

function App() {
  const now = useMemo(() => new Date(), []);
  const initialEnd = localInput(now);
  const initialStart = localInput(new Date(now.getTime() - 24 * 60 * 60 * 1000));
  const [catalog, setCatalog] = useState<Metric[]>([]);
  const [namespace, setNamespace] = useState("");
  const [start, setStart] = useState(initialStart);
  const [end, setEnd] = useState(initialEnd);
  const [timezone, setTimezone] = useState(DEFAULT_TIMEZONE);
  const [namespaces, setNamespaces] = useState<string[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [modelId, setModelId] = useState("");
  const [scope, setScope] = useState<"pod" | "service">("pod");
  const [window, setWindow] = useState("5m");
  const [percentile, setPercentile] = useState(95);
  const [queueSlo, setQueueSlo] = useState(0.5);
  const [observationFloor, setObservationFloor] = useState(30);
  const [sloConfirmed, setSloConfirmed] = useState(false);
  const [selectors, setSelectors] = useState<Record<string, string>>({});
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [tab, setTab] = useState("overview");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [lastUpdated, setLastUpdated] = useState("");
  const [dirty, setDirty] = useState(false);
  const [paused, setPaused] = useState(false);
  const [refreshSeconds, setRefreshSeconds] = useState(30);
  const [presetHours, setPresetHours] = useState<number | null>(24);
  const controller = useRef<AbortController | null>(null);
  const revisionRef = useRef("");
  const refreshRef = useRef<() => void>(() => undefined);
  const visibleModels = useMemo(() => models.filter((item) => item.deployment_resolved), [models]);
  const selectedModels = useMemo(() => modelId === "all" ? visibleModels : visibleModels.filter((item) => item.model_id === modelId), [modelId, visibleModels]);
  const selectOptions = useMemo(() => {
    const field: Record<string, keyof Model> = { job: "job_values", instance: "instance_values", pod: "pod_names", engine: "engine_values", prometheus: "source_values" };
    return Object.fromEntries(Object.entries(field).map(([key, property]) => [key, [...new Set(selectedModels.flatMap((item) => (item[property] as string[]) || []))].sort()])) as Record<string, string[]>;
  }, [selectedModels]);
  const orderedTop = TOP10_IDS.map((id) => snapshot?.metrics[String(id)]).filter(Boolean) as MetricResult[];
  const allMetrics = catalog.map((item) => {
    const captured = snapshot?.metrics[String(item.id)];
    return {
      ...item,
      ...(captured || {}),
      state: captured?.state || item.state || "not_queried",
      reason: captured ? captured.reason : "Not included in this snapshot. Select this section to query the metric.",
      series: captured?.series || [],
      summary: captured?.summary || [],
      selected_range_total: captured?.selected_range_total || [],
      observations: captured?.observations || [],
      scale_gate_series: captured?.scale_gate_series || [],
      scale_gate_observations: captured?.scale_gate_observations || [],
      scale_gate_reasons: captured?.scale_gate_reasons || [],
      coverage: captured?.coverage || [],
      queries: captured?.queries || [],
      warnings: captured?.warnings || [],
      scope_states: captured?.scope_states || [],
    };
  }) as MetricResult[];

  useEffect(() => {
    fetch("/api/v1/catalog").then((response) => response.ok ? response.json() : Promise.reject(new Error("Could not load metric catalog."))).then((data) => {
      setCatalog(data.metrics);
      setQueueSlo(data.scale_gate?.queue_slo_default_seconds ?? 0.5);
      setObservationFloor(data.scale_gate?.queue_observation_review_floor ?? 30);
    }).catch((reason) => setError(reason.message));
    const query = new URLSearchParams(location.search);
    if (query.get("start")) setStart(query.get("start")!);
    if (query.get("end")) setEnd(query.get("end")!);
    if (query.get("timezone")) setTimezone(query.get("timezone")!);
    if (query.get("namespace")) setNamespace(query.get("namespace")!);
    if (query.get("model")) setModelId(query.get("model")!);
    if (query.get("scope") === "service") setScope("service");
    if (query.get("window")) setWindow(query.get("window")!);
    if (query.get("percentile")) setPercentile(Number(query.get("percentile")));
    for (const key of ["job", "pod", "instance", "engine", "prometheus"]) if (query.get(key)) setSelectors((prior) => ({ ...prior, [key]: query.get(key)! }));
    fetch("/api/v1/namespaces").then((response) => response.ok ? response.json() : Promise.reject(new Error("Could not load authorized namespaces."))).then((data) => {
      const values = data.namespaces || [];
      setNamespaces(values);
      setNamespace((current) => current && values.includes(current) ? current : (values[0] || ""));
    }).catch((reason) => setError(reason.message));
  }, []);

  useEffect(() => {
    setModels([]);
    if (!namespace) return;
    const abort = new AbortController();
    const params = new URLSearchParams({ namespace, start: wallTimeToIso(start, timezone), end: wallTimeToIso(end, timezone), timezone });
    fetch(`/api/v1/filters?${params}`, { signal: abort.signal }).then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail || "Model discovery failed.")))).then((data) => {
      setModels(data.models || []);
      setMessage(data.warnings?.join(" ") || "");
    }).catch((reason) => { if (reason.name !== "AbortError") setError(reason.message); });
    return () => abort.abort();
  }, [namespace, start, end, timezone]);

  useEffect(() => {
    for (const [key, value] of Object.entries(selectors)) if (value && !selectOptions[key]?.includes(value)) setSelectors((prior) => ({ ...prior, [key]: "" }));
  }, [selectOptions]);

  const markDirty = () => {
    setDirty(true);
    controller.current?.abort();
  };

  const runQuery = async (metricIds?: number[], rangeStart = start, rangeEnd = end, returnToTab?: string) => {
    if (!namespace) { setError("Choose an authorized namespace first."); return; }
    if (!modelId) { setError("Choose a served model or explicitly select All resolved workloads."); return; }
    setBusy(true); setError(""); setMessage("");
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    const revision = crypto.randomUUID();
    revisionRef.current = revision;
    try {
      const capturedIds = Object.keys(snapshot?.metrics || {}).map(Number);
      const activeSectionIds = SECTION_METRIC_IDS[tab] || [];
      const requestedIds = metricIds || (snapshot ? [...new Set([...TOP10_IDS, ...capturedIds, ...activeSectionIds])] : undefined);
      const body = { namespace, model_id: modelId, start: wallTimeToIso(rangeStart, timezone), end: wallTimeToIso(rangeEnd, timezone), timezone, scope, window, percentile, queue_slo_seconds: queueSlo, queue_min_observations: observationFloor, queue_slo_confirmed: sloConfirmed, selectors, metric_ids: requestedIds, revision };
      const response = await fetch("/api/v1/query", { method: "POST", headers: REQ, body: JSON.stringify(body), signal: abort.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Metric query failed.");
      if (revisionRef.current !== revision || abort.signal.aborted) return;
      setSnapshot(data); setQueueSlo(data.scale_gate?.queue_slo_seconds ?? queueSlo); setObservationFloor(data.scale_gate?.queue_min_observations ?? observationFloor); setSloConfirmed(!data.scale_gate?.queue_slo_is_provisional); setDirty(false); setLastUpdated(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: timezone }));
      const params = new URLSearchParams({ namespace, start: rangeStart, end: rangeEnd, timezone, model: modelId, scope, window, percentile: String(percentile) });
      for (const [key, value] of Object.entries(selectors)) if (value) params.set(key, value);
      history.replaceState(null, "", `${location.pathname}?${params}`);
      setTab(returnToTab || "overview"); setMessage(`Snapshot captured ${new Date(data.created_utc).toLocaleString()} · ${data.filters.models.length} workload${data.filters.models.length === 1 ? "" : "s"}`);
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message);
    } finally { if (controller.current === abort) setBusy(false); }
  };

  const selectSection = (section: string) => {
    setTab(section);
    const requested = SECTION_METRIC_IDS[section];
    if (!snapshot || dirty || !requested) return;
    const captured = Object.keys(snapshot.metrics).map(Number);
    if (requested.every((id) => captured.includes(id))) return;
    void runQuery([...new Set([...TOP10_IDS, ...captured, ...requested])], start, end, section);
  };

  const setPreset = (hours: number) => {
    markDirty(); setPresetHours(hours);
    const endDate = new Date(); setEnd(localInput(endDate, timezone)); setStart(localInput(new Date(endDate.getTime() - hours * 3600000), timezone));
  };

  const refreshNow = () => {
    if (presetHours !== null) {
      const endDate = new Date();
      const nextEnd = localInput(endDate, timezone);
      const nextStart = localInput(new Date(endDate.getTime() - presetHours * 3600000), timezone);
      setStart(nextStart); setEnd(nextEnd);
      void runQuery(undefined, nextStart, nextEnd, tab);
    } else void runQuery(undefined, start, end, tab);
  };
  refreshRef.current = refreshNow;

  useEffect(() => {
    if (!snapshot || paused || dirty || refreshSeconds === 0) return;
    const timer = globalThis.setInterval(() => refreshRef.current(), refreshSeconds * 1000);
    return () => globalThis.clearInterval(timer);
  }, [snapshot, paused, dirty, refreshSeconds]);

  const captureAll = async () => {
    setPaused(true);
    await runQuery(catalog.map((metric) => metric.id), start, end, "catalog");
  };

  const changeNamespace = (value: string) => {
    markDirty(); setNamespace(value); setModelId(""); setSelectors({}); setSnapshot(null); setModels([]);
  };
  const downloadExport = async (format: "csv" | "json" | "xlsx" | "pdf") => {
    if (!snapshot) return;
    setError("");
    try {
      const created = await fetch("/api/v1/exports", { method: "POST", headers: REQ, body: JSON.stringify({ snapshot_id: snapshot.snapshot_id, format }) });
      const body = await created.json();
      if (!created.ok) throw new Error(body.detail || "Export could not be created.");
      const response = await fetch(`/api/v1/exports/${body.export_id}`);
      if (!response.ok) throw new Error("Export download failed.");
      const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement("a"); anchor.href = url; anchor.download = body.filename; anchor.click(); URL.revokeObjectURL(url);
    } catch (reason) { setError((reason as Error).message); }
  };

  const setSelector = (key: string, value: string) => { markDirty(); setSelectors((prior) => ({ ...prior, [key]: value })); };
  const rangeLabel = snapshot ? `${snapshot.filters.range.start_local} — ${snapshot.filters.range.end_local}` : "";
  const namespaceOptions = namespaces.map((value) => ({ value, label: value }));
  const modelOptions: ComboOption[] = [
    { value: "all", label: `All resolved workloads (${visibleModels.length})` },
    ...(modelId && modelId !== "all" && !visibleModels.some((item) => item.model_id === modelId) ? [{ value: modelId, label: "Selected model · no series discovered for this range" }] : []),
    ...visibleModels.map((item) => ({ value: item.model_id || "", label: `${item.model_name} · ${item.deployment}` })),
    ...models.filter((item) => !item.deployment_resolved).map((item, index) => ({ value: `unresolved-${index}`, label: `${item.model_name} · owner unresolved`, disabled: true })),
  ];

  return <div className="app-shell">
    <header className="topbar"><div className="brand"><div className="brand-mark">v</div><div><div className="brand-title">vLLM Metrics</div><div className="brand-subtitle">OpenShift AI · capacity signals</div></div></div><div className="header-right"><span className="live-dot" /> Authenticated with OpenShift <span className="header-divider" /> <a href="/api/v1/status" target="_blank" rel="noreferrer">Connection status ↗</a></div></header>
    <main className="main-wrap">
      <section className="page-intro"><div><div className="eyebrow">MODEL SERVING OBSERVABILITY</div><h1>vLLM capacity dashboard</h1><p>Explore engine latency, queue pressure, KV cache, and token throughput for workloads in your cluster.</p></div><div className="intro-meta"><span className="safe-chip">Read only</span><span>Thanos metrics</span></div></section>
      <section className="filter-panel" aria-label="Dashboard filters">
        <div className="filter-heading"><div><h2>Filters</h2><span>Select a model and time range to capture a metrics snapshot.</span></div><button className="button button-quiet" onClick={() => { markDirty(); setSelectors({}); setModelId(""); setScope("pod"); setWindow("5m"); setPercentile(95); setTimezone(DEFAULT_TIMEZONE); setPresetHours(24); const to = new Date(); setEnd(localInput(to, DEFAULT_TIMEZONE)); setStart(localInput(new Date(to.getTime() - 24 * 3600000), DEFAULT_TIMEZONE)); }}>Reset</button></div>
        <div className="filter-grid">
          <label className="field"><span>Namespace</span><FilterCombobox value={namespace} options={namespaceOptions} placeholder="Search authorized namespaces" onChange={changeNamespace} /><small>Limited to namespaces authorized for your account.</small></label>
          <label className="field field-model"><span>Served model / workload</span><FilterCombobox value={modelId} options={modelOptions} placeholder="Search served models and workloads" onChange={(value) => { markDirty(); setModelId(value); setSelectors({}); }} disabled={!namespace} /><small>{visibleModels.length ? `${visibleModels.length} workload identities resolved from pod ownership.` : "Select a namespace to discover model identities."}</small></label>
          <label className="field"><span>View</span><select value={scope} onChange={(event) => { markDirty(); setScope(event.target.value as "pod" | "service"); }}><option value="pod">Per pod</option><option value="service">Service aggregate</option></select><small>Service view pools histogram buckets before percentile.</small></label>
          <label className="field"><span>Timezone</span><select value={timezone} onChange={(event) => { markDirty(); setTimezone(event.target.value); setPresetHours(null); }}><option value="Asia/Hong_Kong">Asia/Hong_Kong (UTC+08:00)</option><option value="UTC">UTC</option><option value="America/New_York">America/New_York</option><option value="Europe/London">Europe/London</option></select><small>Used for calendar-day token estimates.</small></label>
          <label className="field"><span>Start · {timezone}</span><input type="datetime-local" value={start} onChange={(event) => { markDirty(); setPresetHours(null); setStart(event.target.value); }} /></label>
          <label className="field"><span>End · {timezone}</span><input type="datetime-local" value={end} onChange={(event) => { markDirty(); setPresetHours(null); setEnd(event.target.value); }} /></label>
          <label className="field"><span>Latency window</span><select value={window} onChange={(event) => { markDirty(); setWindow(event.target.value); }}><option value="1m">1 minute</option><option value="2m">2 minutes</option><option value="5m">5 minutes</option><option value="10m">10 minutes</option><option value="15m">15 minutes</option><option value="30m">30 minutes</option><option value="1h">1 hour</option></select></label>
          <label className="field"><span>Percentile</span><select value={percentile} onChange={(event) => { markDirty(); setPercentile(Number(event.target.value)); }}><option value="50">P50</option><option value="90">P90</option><option value="95">P95</option><option value="99">P99</option></select></label>
        </div>
        <div className="filter-foot"><div className="presets"><span>Quick range</span>{[[1, "1h"], [6, "6h"], [24, "24h"], [168, "7d"], [720, "30d"]].map(([hours, label]) => <button key={label} className="preset" onClick={() => setPreset(Number(hours))}>{label}</button>)}</div>
          <div className="selector-row">{(["job", "pod", "instance", "engine", "prometheus"] as const).map((key) => <label className="compact-field" key={key}><span>{key === "job" ? "Scrape job" : key === "prometheus" ? "Monitoring source" : key}</span><select value={selectors[key] || ""} onChange={(event) => setSelector(key, event.target.value)}><option value="">All</option>{(selectOptions[key] || []).map((value) => <option key={value}>{value}</option>)}</select></label>)}
            <label className="compact-field refresh-field"><span>Auto refresh</span><select value={refreshSeconds} onChange={(event) => setRefreshSeconds(Number(event.target.value))}><option value="0">Off</option><option value="30">30 seconds</option><option value="60">1 minute</option><option value="120">2 minutes</option></select></label>
            {snapshot && <button className="button button-quiet" onClick={() => { if (!paused) { controller.current?.abort(); setBusy(false); } setPaused(!paused); }}>{paused ? "Resume" : "Pause"}</button>}
            <button className="button button-primary apply-button" disabled={busy || !namespace || !modelId} onClick={() => void runQuery()}>{busy ? <><span className="spinner" /> Querying…</> : snapshot ? "Apply / refresh" : "Apply filters"}</button>
          </div>
        </div>
      </section>
      {error && <div className="alert alert-error" role="alert"><b>Could not load metrics.</b> {error} <button onClick={() => setError("")}>Dismiss</button></div>}
      {message && <div className="notice"><span className="notice-icon">i</span><span>{message}</span><button onClick={() => setMessage("")}>×</button></div>}
      {!snapshot ? <section className="empty-state"><div className="empty-icon">▥</div><h2>Capture a metrics snapshot</h2><p>Choose an authorized namespace, review the discovered vLLM workloads, then apply filters to query the monitoring data.</p><div className="empty-points"><span>✓ Read-only PromQL</span><span>✓ Missing data stays explicit</span><span>✓ Exportable snapshot</span></div></section> : <>
        <section className="snapshot-bar"><div className="snapshot-title"><span className="snapshot-check">✓</span><div><b>{dirty ? "Showing previous snapshot" : "Snapshot ready"}</b><span>{snapshot.filters.namespace} · {snapshot.filters.range.start_local} — {snapshot.filters.range.end_local} ({snapshot.filters.range.timezone}) · {snapshot.filters.models.map((model) => `${model.model_name} / ${model.deployment}`).join(", ") || "No resolved workloads"}</span>{dirty && <span className="dirty-note">Filter inputs changed. Charts and exports remain on this captured selection until Apply.</span>}</div></div><div className="snapshot-right"><span>{paused ? "Refresh paused" : `Updated ${lastUpdated}`}</span><button className="button button-quiet" onClick={refreshNow} disabled={busy}>↻ Refresh</button><button className="button button-quiet" onClick={() => void downloadExport("csv")}>↓ Export</button></div></section>
        <nav className="tabs" aria-label="Dashboard sections">{[["overview", "Capacity overview"], ["gate-history", "Scale gate history"], ["latency", "Latency"], ["queues", "Queues & KV"], ["traffic", "Traffic & shape"], ["reliability", "Reliability & replicas"], ["daily", "Daily tokens"], ["catalog", `Metric catalog (${catalog.length})` ]].map(([id, label]) => <button key={id} disabled={busy} className={tab === id ? "tab active" : "tab"} onClick={() => selectSection(id)}>{label}</button>)}</nav>
        {tab === "overview" && <>
          <div className="section-title"><div><h2>Top capacity signals</h2><p>Ten signals selected for model capacity and latency review.</p></div><span className="data-note">{snapshot.filters.range.step_seconds}s query step · {snapshot.filters.range.scope} scope</span></div>
          <ScaleGatePanel snapshot={snapshot} queueSlo={queueSlo} setQueueSlo={setQueueSlo} observationFloor={observationFloor} setObservationFloor={setObservationFloor} sloConfirmed={sloConfirmed} setSloConfirmed={setSloConfirmed} settingsDirty={queueSlo !== snapshot.scale_gate?.queue_slo_seconds || observationFloor !== snapshot.scale_gate?.queue_min_observations || sloConfirmed === snapshot.scale_gate?.queue_slo_is_provisional} onApply={() => void runQuery()} />
          <div className="metric-grid">{orderedTop.map((metric) => <article className="metric-card" key={metric.id}>
            <div className="metric-head"><div><div className="metric-category">{metric.category}</div><h3>{metric.name}</h3></div><span className={`state-pill state-${metric.state || "unknown"}`}>{statusLabel(metric.state)}</span></div>
            <MetricSummary metric={metric} />
            {metric.series.length ? <MetricChart metric={metric} timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} compact /> : <div className="chart-empty">{metric.reason || "No observations for this selection"}</div>}
            <div className="metric-foot"><span>{metric.series.length} series · {metric.coverage?.reduce((n, item) => n + (item.gaps || 0), 0) || 0} gaps</span><button onClick={() => setExpanded(expanded === metric.id ? null : metric.id)}>{expanded === metric.id ? "Hide details" : "Details"}</button></div>
            {expanded === metric.id && <MetricDetails metric={metric} chartExport timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} />}
          </article>)}</div>
          <section className="capacity-summary"><div><b>Supporting signals remain diagnostic</b><span>TTFT, TPOT, request lengths, KV occupancy and token rates help explain the gate. They do not independently open it; confirm workload and runtime context before choosing capacity or topology.</span></div><span className="capacity-tag">No autoscaling action is taken</span></section>
          <section className="wide-card"><div className="wide-card-title"><div><h3>Input and output token throughput</h3><p>Separate rate series for the selected vLLM engine workloads.</p></div></div><div className="wide-charts">{[20, 21].map((id) => { const metric = currentMetric(snapshot, id); return metric ? <div className="wide-chart" key={id}><div className="chart-label">{metric.name} <span>{statusLabel(metric.state)}</span></div>{metric.series.length ? <MetricChart metric={metric} timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} /> : <div className="chart-empty">{metric.reason}</div>}</div> : null; })}</div></section>
        </>}
        {tab === "gate-history" && <ScaleGateHistoryPanel snapshot={snapshot} results={scaleGateResults(snapshot, queueSlo, observationFloor)} timezone={snapshot.filters.range.timezone} settingsDirty={queueSlo !== snapshot.scale_gate?.queue_slo_seconds || observationFloor !== snapshot.scale_gate?.queue_min_observations || sloConfirmed === snapshot.scale_gate?.queue_slo_is_provisional} />}
        {tab === "latency" && <MetricSection metrics={allMetrics.filter((metric) => SECTION_METRIC_IDS.latency.includes(metric.id))} timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} />}
        {tab === "queues" && <MetricSection metrics={allMetrics.filter((metric) => SECTION_METRIC_IDS.queues.includes(metric.id))} timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} />}
        {tab === "traffic" && <MetricSection metrics={allMetrics.filter((metric) => SECTION_METRIC_IDS.traffic.includes(metric.id))} timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} />}
        {tab === "reliability" && <MetricSection metrics={allMetrics.filter((metric) => SECTION_METRIC_IDS.reliability.includes(metric.id))} timezone={snapshot.filters.range.timezone} rangeLabel={rangeLabel} />}
        {tab === "daily" && <DailyTokens snapshot={snapshot} />}
        {tab === "catalog" && <Catalog metrics={allMetrics} expanded={expanded} setExpanded={setExpanded} onCaptureAll={captureAll} busy={busy} />}
        <footer className="dashboard-footer"><span>{snapshot.evidence_note}</span><div>{(["csv", "json", "xlsx", "pdf"] as const).map((format) => <button key={format} onClick={() => void downloadExport(format)}>{format.toUpperCase()}</button>)}</div></footer>
      </>}
      <footer className="page-footer"><span>vLLM engine metrics only · source prefix <code>vllm:</code></span><span>OpenShift OAuth protected · read-only monitoring</span></footer>
    </main>
  </div>;
}

function MetricDetails({ metric, chartExport = false, timezone = "UTC", rangeLabel = "" }: { metric: MetricResult; chartExport?: boolean; timezone?: string; rangeLabel?: string }) {
  const points = metric.series.flatMap((series) => series.points.slice(-8).map((point) => ({ ...point, deployment: series.deployment, pod: series.labels.pod || "service" }))).slice(-40);
  return <div className="metric-details"><p>{metric.description}</p>{metric.warnings.map((warning, index) => <div className="warning-line" key={index}>ⓘ {warning}</div>)}{metric.coverage?.map((item) => <div className="coverage-line" key={item.deployment}>{item.deployment} · freshness {item.freshness_seconds === null ? "unknown" : `${item.freshness_seconds}s`} · {item.gaps} gap(s)</div>)}{chartExport && metric.series.length > 0 && <MetricChart metric={metric} timezone={timezone} rangeLabel={rangeLabel} />}{points.length > 0 && <details><summary>Recent evaluated points ({points.length})</summary><div className="point-table"><table><thead><tr><th>Time</th><th>Workload / pod</th><th>Value</th></tr></thead><tbody>{points.map((point, index) => <tr key={index}><td>{new Date(point.timestamp * 1000).toISOString()}</td><td>{point.deployment} / {point.pod}</td><td>{point.value === null ? point.raw : fmt(point.value, metric.unit)}</td></tr>)}</tbody></table></div></details>}{metric.queries.map((query, index) => <details key={index}><summary>{query.deployment} · PromQL</summary><code>{query.promql || "No query generated"}</code>{query.summary_promql && <><small>Selected-range / end summary</small><code>{query.summary_promql}</code></>}{query.scale_gate_promql && <><small>Three-window scale gate</small><code>{query.scale_gate_promql}</code>{query.scale_gate_observation_promql && <code>{query.scale_gate_observation_promql}</code>}</>}</details>)}</div>;
}

function MetricSection({ metrics, timezone, rangeLabel }: { metrics: MetricResult[]; timezone: string; rangeLabel: string }) {
  return <><div className="section-title"><div><h2>Additional signals</h2><p>Supporting request, token, reliability, and replica metrics from the same snapshot.</p></div></div><div className="support-grid">{metrics.map((metric) => <article className="support-card" key={metric.id}><div className="support-head"><div><span className="metric-category">{metric.category} · #{metric.id}</span><h3>{metric.name}</h3></div><span className={`state-pill state-${metric.state || "unknown"}`}>{statusLabel(metric.state)}</span></div><p>{metric.description}</p>{metric.series.length ? <MetricChart metric={metric} timezone={timezone} rangeLabel={rangeLabel} /> : <div className="chart-empty">{metric.reason || "No observations"}</div>}<MetricDetails metric={metric} /></article>)}</div></>;
}

function DailyTokens({ snapshot }: { snapshot: Snapshot }) {
  const rows = snapshot.daily_tokens.rows;
  const days = [...new Set(rows.map((row) => row.interval.local_date))];
  const deployments = [...new Set(rows.map((row) => row.deployment))];
  return <section className="wide-card daily-card"><div className="wide-card-title"><div><h2>Daily token estimates</h2><p>Hong Kong or selected local calendar days using half-open intervals. Partial edge days are labeled.</p></div><span className={`state-pill state-${snapshot.daily_tokens.state}`}>{statusLabel(snapshot.daily_tokens.state)}</span></div>{rows.length ? <div className="table-scroll"><table><thead><tr><th>Local date</th><th>Workload</th><th>Metric</th><th>Estimate</th><th>Interval</th><th>Status</th></tr></thead><tbody>{rows.map((row, index) => <tr key={index}><td>{row.interval.local_date}{row.interval.partial && <span className="partial">Partial</span>}</td><td>{row.deployment}</td><td>{row.metric}</td><td>{fmt(row.estimate, row.unit)}</td><td>Calendar day</td><td><span className={`state-pill state-${row.state}`}>{statusLabel(row.state)}</span></td></tr>)}</tbody></table></div> : <div className="chart-empty">No daily counter samples for the selected range. Missing observations are not zero.</div>}<p className="footnote">Daily totals estimate Prometheus counter increases. They are not billing values. {days.length} local day(s), {deployments.length} workload(s).</p></section>;
}

function Catalog({ metrics, expanded, setExpanded, onCaptureAll, busy }: { metrics: MetricResult[]; expanded: number | null; setExpanded: (value: number | null) => void; onCaptureAll: () => void; busy: boolean }) {
  const [filter, setFilter] = useState("");
  const filtered = metrics.filter((metric) => `${metric.id} ${metric.name} ${metric.category} ${metric.description}`.toLowerCase().includes(filter.toLowerCase()));
  return <section className="wide-card catalog-card"><div className="wide-card-title"><div><h2>Metric catalog</h2><p>All {metrics.length} source metrics and derived signals, with exact query expressions and availability.</p><button className="button button-quiet capture-all" disabled={busy} onClick={onCaptureAll}>{busy ? "Capturing…" : `Capture all ${metrics.length} metrics`}</button></div><label className="catalog-search"><span>Search</span><input placeholder="Metric or category" value={filter} onChange={(event) => setFilter(event.target.value)} /></label></div><div className="table-scroll"><table><thead><tr><th>ID</th><th>Signal</th><th>Category</th><th>Source / scope</th><th>Status</th><th></th></tr></thead><tbody>{filtered.map((metric) => <React.Fragment key={metric.id}><tr><td>{metric.id}</td><td><b>{metric.name}</b><small>{metric.description}</small></td><td>{metric.category}</td><td><code>{metric.metric || metric.family}</code><small>{metric.scope} · {metric.aggregation}</small></td><td><span className={`state-pill state-${metric.state || "unknown"}`}>{statusLabel(metric.state)}</span></td><td><button className="text-button" onClick={() => setExpanded(expanded === metric.id ? null : metric.id)}>{expanded === metric.id ? "Close" : "Query"}</button></td></tr>{expanded === metric.id && <tr className="query-row"><td colSpan={6}><MetricDetails metric={metric} /></td></tr>}</React.Fragment>)}</tbody></table></div></section>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
