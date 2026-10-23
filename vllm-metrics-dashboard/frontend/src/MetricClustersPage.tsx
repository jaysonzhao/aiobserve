import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { ParallelChart } from "echarts/charts";
import { GridComponent, ParallelComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { GateSnapshot } from "./ScaleGateViews";

echarts.use([ParallelChart, GridComponent, ParallelComponent, TooltipComponent, CanvasRenderer]);

type Observation = {
  timestamp: number;
  deployment: string;
  modelName: string;
  pod: string;
  values: [number, number, number, number];
};

type ClusteredObservation = Observation & { cluster: number; standardized: number[] };
type ClusterResult = {
  rows: ClusteredObservation[];
  centroids: Array<[number, number, number, number]>;
  iterations: number;
  converged: boolean;
  uniquePatterns: number;
};

const FEATURES = [
  { key: "ttft", name: "TTFT P95", unit: "s" },
  { key: "tpot", name: "Request TPOT P95", unit: "s/token" },
  { key: "waiting", name: "Mean waiting", unit: "requests" },
  { key: "kv", name: "KV occupancy", unit: "%" },
] as const;
const COLORS = ["#c9190b", "#0066cc", "#3e8635", "#ec7a08", "#5752d1", "#009596", "#8f4700", "#a30000"];

function metricText(value: number, index: number) {
  const display = index === 3 ? value * 100 : value;
  const digits = Math.abs(display) >= 100 ? 0 : Math.abs(display) >= 10 ? 1 : 2;
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(display)}${index === 3 ? "%" : ` ${FEATURES[index].unit}`}`;
}

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" })[character] || character);
}

function historicalObservations(gate: GateSnapshot) {
  const rows: Observation[] = [];
  let excluded = 0;
  for (const workload of gate.workloads) {
    for (const pod of workload.pods) {
      for (const evaluation of pod.evaluations) {
        const values = [
          evaluation.inputs.ttft.value,
          evaluation.inputs.tpot.value,
          evaluation.inputs.waiting.value,
          evaluation.inputs.kv.value,
        ];
        const usable = evaluation.valid
          && values.every((value): value is number => value !== null && Number.isFinite(value))
          && values[0] >= 0 && values[1] >= 0 && values[2] >= 0 && values[3] >= 0 && values[3] <= 1;
        if (!usable) {
          excluded += 1;
          continue;
        }
        rows.push({
          timestamp: evaluation.timestamp,
          deployment: workload.deployment,
          modelName: workload.model_name,
          pod: pod.pod,
          values: values as [number, number, number, number],
        });
      }
    }
  }
  rows.sort((left, right) => left.timestamp - right.timestamp || left.pod.localeCompare(right.pod));
  return { rows, excluded };
}

function distanceSquared(left: number[], right: number[]) {
  let distance = 0;
  for (let index = 0; index < left.length; index += 1) distance += (left[index] - right[index]) ** 2;
  return distance;
}

function trainKMeans(rows: Observation[], k: number): ClusterResult {
  const mean = FEATURES.map((_, index) => rows.reduce((sum, row) => sum + row.values[index], 0) / rows.length);
  const deviation = FEATURES.map((_, index) => {
    const variance = rows.reduce((sum, row) => sum + (row.values[index] - mean[index]) ** 2, 0) / rows.length;
    return Math.sqrt(variance) || 1;
  });
  const standardized = rows.map((row) => row.values.map((value, index) => (value - mean[index]) / deviation[index]));
  const uniquePatterns = new Set(standardized.map((row) => row.map((value) => value.toFixed(8)).join("|"))).size;
  if (uniquePatterns < k) throw new Error(`Only ${uniquePatterns} distinct four-metric patterns are available. Select k ≤ ${uniquePatterns} or widen the historical range.`);

  // Deterministic farthest-first seeding makes retraining reproducible for the same snapshot.
  const centers: number[][] = [standardized[0].slice()];
  while (centers.length < k) {
    let bestIndex = -1;
    let bestDistance = -1;
    for (let index = 0; index < standardized.length; index += 1) {
      const nearest = Math.min(...centers.map((center) => distanceSquared(standardized[index], center)));
      if (nearest > bestDistance) {
        bestDistance = nearest;
        bestIndex = index;
      }
    }
    centers.push(standardized[bestIndex].slice());
  }

  let labels = new Array<number>(rows.length).fill(-1);
  let converged = false;
  let iterations = 0;
  for (let iteration = 1; iteration <= 100; iteration += 1) {
    iterations = iteration;
    const nextLabels = standardized.map((row) => {
      let best = 0;
      let bestDistance = distanceSquared(row, centers[0]);
      for (let cluster = 1; cluster < centers.length; cluster += 1) {
        const distance = distanceSquared(row, centers[cluster]);
        if (distance < bestDistance) {
          best = cluster;
          bestDistance = distance;
        }
      }
      return best;
    });
    const counts = new Array<number>(k).fill(0);
    nextLabels.forEach((label) => { counts[label] += 1; });
    for (let empty = 0; empty < k; empty += 1) {
      if (counts[empty] !== 0) continue;
      let candidate = -1;
      let largestError = -1;
      for (let index = 0; index < standardized.length; index += 1) {
        const label = nextLabels[index];
        if (counts[label] <= 1) continue;
        const error = distanceSquared(standardized[index], centers[label]);
        if (error > largestError) {
          candidate = index;
          largestError = error;
        }
      }
      if (candidate >= 0) {
        counts[nextLabels[candidate]] -= 1;
        nextLabels[candidate] = empty;
        counts[empty] += 1;
      }
    }
    const nextCenters = Array.from({ length: k }, () => new Array<number>(FEATURES.length).fill(0));
    nextLabels.forEach((label, rowIndex) => {
      standardized[rowIndex].forEach((value, feature) => { nextCenters[label][feature] += value; });
    });
    nextCenters.forEach((center, cluster) => {
      if (counts[cluster]) center.forEach((_, feature) => { center[feature] /= counts[cluster]; });
      else center.splice(0, center.length, ...centers[cluster]);
    });
    const sameLabels = nextLabels.every((label, index) => label === labels[index]);
    labels = nextLabels;
    centers.splice(0, centers.length, ...nextCenters);
    if (sameLabels) {
      converged = true;
      break;
    }
  }

  const clusteredRows = rows.map((row, index) => ({ ...row, cluster: labels[index], standardized: standardized[index] }));
  const rawCentroids = centers.map((center) => center.map((value, index) => value * deviation[index] + mean[index]) as [number, number, number, number]);
  return { rows: clusteredRows, centroids: rawCentroids, iterations, converged, uniquePatterns };
}

function ParallelDiagram({ result }: { result: ClusterResult }) {
  const ref = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => Array.from({ length: result.centroids.length }, (_, cluster) => result.rows.filter((row) => row.cluster === cluster)), [result]);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const extents = FEATURES.map((_, feature) => {
      const values = result.rows.map((row) => row.standardized[feature]);
      const min = Math.min(...values);
      const max = Math.max(...values);
      const padding = (max - min) * 0.08 || 0.5;
      return [min - padding, max + padding];
    });
    chart.setOption({
      animation: false,
      color: COLORS,
      tooltip: {
        trigger: "item",
        confine: true,
        formatter: (rawParams: unknown) => {
          const params = rawParams as { seriesIndex?: number; dataIndex?: number };
          const row = groups[params.seriesIndex || 0]?.[params.dataIndex || 0];
          if (!row) return "";
          return `<b>Cluster ${row.cluster + 1}</b><br/>${escapeHtml(new Date(row.timestamp * 1000).toLocaleString())}<br/>${escapeHtml(row.deployment)} · ${escapeHtml(row.pod)}<br/>${FEATURES.map((feature, index) => `${escapeHtml(feature.name)}: ${escapeHtml(metricText(row.values[index], index))}`).join("<br/>")}`;
        },
      },
      parallel: { left: 72, right: 30, top: 48, bottom: 30 },
      parallelAxis: FEATURES.map((feature, dim) => ({
        dim,
        name: `${feature.name} · z-score`,
        type: "value",
        min: extents[dim][0],
        max: extents[dim][1],
        nameLocation: "end",
        nameTextStyle: { color: "#4f5255", fontSize: 10, padding: [0, 0, 12, 0] },
        axisLabel: { color: "#6a6e73", fontSize: 9, formatter: (value: number) => Number(value).toFixed(1) },
        axisLine: { lineStyle: { color: "#8a8d90" } },
        splitLine: { lineStyle: { color: "#ededed" } },
      })),
      series: groups.map((items, cluster) => ({
        name: `Cluster ${cluster + 1}`,
        type: "parallel",
        lineStyle: { color: COLORS[cluster % COLORS.length], width: 1.3, opacity: 0.28 },
        emphasis: { lineStyle: { width: 3, opacity: 0.9 } },
        data: items.map((row) => row.standardized),
      })),
    });
    const resize = () => chart.resize();
    const observer = new ResizeObserver(resize);
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [groups, result]);
  return <div className="cluster-chart" ref={ref} role="img" aria-label="Parallel coordinates diagram of historical four-metric observations colored by K-means cluster" />;
}

export function MetricClustersPage({ gate, rangeLabel, snapshotKey, stale }: {
  gate: GateSnapshot;
  rangeLabel: string;
  snapshotKey: string;
  stale: boolean;
}) {
  const [k, setK] = useState(3);
  const [result, setResult] = useState<ClusterResult | null>(null);
  const [training, setTraining] = useState(false);
  const [trainedAt, setTrainedAt] = useState("");
  const [error, setError] = useState("");
  const { rows, excluded } = useMemo(() => historicalObservations(gate), [gate]);
  const uniquePatterns = useMemo(() => new Set(rows.map((row) => row.values.map((value) => value.toPrecision(8)).join("|"))).size, [rows]);
  const resultKey = useRef(snapshotKey);

  useEffect(() => {
    if (resultKey.current !== snapshotKey) {
      resultKey.current = snapshotKey;
      setResult(null);
      setTrainedAt("");
      setError("");
    }
  }, [snapshotKey]);

  const train = async () => {
    setTraining(true);
    setError("");
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    try {
      setResult(trainKMeans(rows, k));
      setTrainedAt(new Date().toLocaleString());
    } catch (reason) {
      setResult(null);
      setError((reason as Error).message);
    } finally {
      setTraining(false);
    }
  };

  return <section className="wide-card metric-clusters" aria-labelledby="metric-clusters-title">
    <div className="wide-card-title">
      <div><div className="eyebrow">UNSUPERVISED HISTORICAL ANALYSIS</div><h2 id="metric-clusters-title">Four-metric K-means clusters</h2><p>Group valid per-pod history by TTFT P95, request TPOT P95, mean waiting requests, and KV occupancy.</p></div>
      <span className="capacity-tag">Exploratory · no scaling action</span>
    </div>
    <div className="cluster-controls">
      <label><span>Clusters (k)</span><select value={k} onChange={(event) => { setK(Number(event.target.value)); setResult(null); setTrainedAt(""); setError(""); }}>{Array.from({ length: 7 }, (_, index) => index + 2).map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
      <span className="cluster-sample-count"><b>{rows.length.toLocaleString()}</b> complete, fresh samples · {uniquePatterns.toLocaleString()} distinct patterns · {excluded.toLocaleString()} excluded</span>
      <button className="button button-primary" onClick={() => void train()} disabled={training || stale || rows.length < k || uniquePatterns < k}>{training ? <><span className="spinner" /> Training…</> : "Retrain and generate diagram"}</button>
    </div>
    {stale && <p className="notice">Dashboard filters or gate policy have changed. Apply them to capture matching history before training.</p>}
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {!result && !error && <div className="cluster-empty">{rows.length < k || uniquePatterns < k ? `Need at least ${k} complete samples and ${k} distinct metric patterns; this selection has ${rows.length} samples and ${uniquePatterns} patterns. Select a wider range or a smaller k.` : "Choose k, then train to generate the historical cluster diagram."}</div>}
    {result && <>
      <div className="cluster-result-heading"><div><b>Cluster diagram · {rangeLabel}</b><span>Trained {trainedAt} · {result.rows.length.toLocaleString()} samples · {result.iterations} iteration(s) · {result.converged ? "converged" : "iteration limit reached"}</span></div><span className="data-note">Standardized features · same pod per observation</span></div>
      <ParallelDiagram result={result} />
      <div className="table-scroll cluster-centroids"><table><thead><tr><th>Cluster</th><th>Samples</th><th>TTFT P95 centroid</th><th>Request TPOT P95 centroid</th><th>Mean waiting centroid</th><th>KV occupancy centroid</th></tr></thead><tbody>
        {result.centroids.map((centroid, cluster) => {
          const count = result.rows.filter((row) => row.cluster === cluster).length;
          return <tr key={cluster}><td><span className="cluster-color" style={{ background: COLORS[cluster % COLORS.length] }} />Cluster {cluster + 1}</td><td>{count.toLocaleString()}</td>{centroid.map((value, feature) => <td key={feature}>{metricText(value, feature)}</td>)}</tr>;
        })}
      </tbody></table></div>
      <p className="footnote">Distances are computed after z-score standardization so seconds, request counts, and occupancy do not dominate by unit scale. The diagram uses standardized axes; the centroid table reports original units. Clusters describe patterns in the selected snapshot and are not a scale-gate decision.</p>
    </>}
  </section>;
}
