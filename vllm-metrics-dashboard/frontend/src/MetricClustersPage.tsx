import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { ScatterChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { GateEvaluation, GateSnapshot } from "./ScaleGateViews";

echarts.use([ScatterChart, GridComponent, TooltipComponent, CanvasRenderer]);

type Vector = [number, number, number, number];
type Feature = { key: "ttft" | "tpot" | "waiting" | "kv"; name: string; short: string; unit: string };
type PodHistory = { key: string; deployment: string; modelName: string; pod: string; rows: GateEvaluation[] };
type FitObservation = { timestamp: number; values: Vector; standardized: Vector; pc: [number, number]; cluster: number };
type FitPreparation = {
  observations: Array<{ timestamp: number; values: Vector; standardized: Vector }>;
  mean: Vector;
  deviation: Vector;
  eigenvalues: number[];
  components: [Vector, Vector];
  pcaMean: Vector;
  uniquePatterns: number;
  constantFeatures: string[];
  problem?: string;
};
type ClusterFit = {
  rows: FitObservation[];
  centersZ: Vector[];
  centersRaw: Vector[];
  centersPc: Array<[number, number]>;
  counts: number[];
  iterations: number;
  converged: boolean;
  inertia: number;
  explained: [number, number];
  constantFeatures: string[];
};
type PodTraining = { group: PodHistory; fit: ClusterFit | null; problem?: string };

const K = 3;
const MIN_SAMPLES = Math.max(20, 5 * K);
const FEATURES: Feature[] = [
  { key: "ttft", name: "TTFT P95", short: "TTFT", unit: "s" },
  { key: "tpot", name: "Request TPOT P95", short: "TPOT", unit: "ms/token" },
  { key: "waiting", name: "Mean waiting", short: "Waiting", unit: "requests" },
  { key: "kv", name: "KV occupancy", short: "KV", unit: "%" },
];
const COLORS = ["#098879", "#4268C8", "#D87519"];
const PALETTE = { navy: "#183b56", grid: "#d8dee5", unknown: "#9aa3ad" };

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character] || character);
}

function number(value: number, digits = 2) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
}

function metricText(value: number, feature: number) {
  if (feature === 1) return `${number(value * 1000, 0)} ms/token`;
  if (feature === 3) return `${number(value * 100, 1)}%`;
  return `${number(value, feature === 2 ? 2 : 2)} ${FEATURES[feature].unit}`;
}

function formatTime(timestamp: number, timezone: string, withDate = true) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    ...(withDate ? { dateStyle: "medium" as const } : {}),
    timeStyle: "medium",
  }).format(new Date(timestamp * 1000));
}

function podHistories(gate: GateSnapshot): PodHistory[] {
  const groups: PodHistory[] = [];
  for (const workload of gate.workloads) {
    for (const pod of workload.pods) {
      groups.push({
        key: JSON.stringify([workload.deployment, workload.model_name, pod.pod]),
        deployment: workload.deployment,
        modelName: workload.model_name,
        pod: pod.pod,
        rows: [...pod.evaluations].sort((left, right) => left.timestamp - right.timestamp),
      });
    }
  }
  return groups.sort((left, right) => left.modelName.localeCompare(right.modelName)
    || left.deployment.localeCompare(right.deployment) || left.pod.localeCompare(right.pod));
}

function fitValues(evaluation: GateEvaluation): Vector | null {
  const values = [
    evaluation.inputs.ttft.value,
    evaluation.inputs.tpot.value,
    evaluation.inputs.waiting.value,
    evaluation.inputs.kv.value,
  ];
  if (!evaluation.valid || !values.every((value): value is number => value !== null && Number.isFinite(value))) return null;
  const [ttft, tpot, waiting, kv] = values as Vector;
  if (ttft < 0 || tpot < 0 || waiting < 0 || kv < 0 || kv > 1) return null;
  return [ttft, tpot, waiting, kv];
}

function featureValue(evaluation: GateEvaluation, feature: number, gate: GateSnapshot): number | null {
  const input = evaluation.inputs[FEATURES[feature].key];
  const value = input.value;
  if (value === null || !Number.isFinite(value) || value < 0) return null;
  if (input.age_seconds === null || input.age_seconds > gate.policy.max_sample_age_seconds) return null;
  if (feature === 0 && (input.observations === null || input.observations === undefined || input.observations < gate.policy.ttft_min_observations)) return null;
  if (feature === 1 && (input.observations === null || input.observations === undefined || input.observations < gate.policy.tpot_min_observations)) return null;
  if (feature === 3 && value > 1) return null;
  return value;
}

function fitRows(group: PodHistory) {
  return group.rows.flatMap((row) => {
    const values = fitValues(row);
    return values ? [{ timestamp: row.timestamp, values }] : [];
  });
}

function distanceSquared(left: number[], right: number[]) {
  let distance = 0;
  for (let index = 0; index < left.length; index += 1) distance += (left[index] - right[index]) ** 2;
  return distance;
}

function multiplyVector(left: number[], right: number[]) {
  return left.reduce((sum, value, index) => sum + value * right[index], 0);
}

function eigenSymmetric(source: number[][]) {
  const size = source.length;
  const matrix = source.map((row) => row.slice());
  const vectors: number[][] = Array.from({ length: size }, (_, row) => Array.from({ length: size }, (_, column) => row === column ? 1 : 0));
  for (let pass = 0; pass < 100; pass += 1) {
    let p = 0;
    let q = 1;
    let largest = 0;
    for (let row = 0; row < size; row += 1) {
      for (let column = row + 1; column < size; column += 1) {
        if (Math.abs(matrix[row][column]) > largest) {
          largest = Math.abs(matrix[row][column]);
          p = row;
          q = column;
        }
      }
    }
    if (largest < 1e-12) break;
    const offDiagonal = matrix[p][q];
    const tau = (matrix[q][q] - matrix[p][p]) / (2 * offDiagonal);
    const tangent = (tau >= 0 ? 1 : -1) / (Math.abs(tau) + Math.sqrt(1 + tau * tau));
    const cosine = 1 / Math.sqrt(1 + tangent * tangent);
    const sine = tangent * cosine;
    const pp = matrix[p][p];
    const qq = matrix[q][q];
    matrix[p][p] = pp - tangent * offDiagonal;
    matrix[q][q] = qq + tangent * offDiagonal;
    matrix[p][q] = 0;
    matrix[q][p] = 0;
    for (let index = 0; index < size; index += 1) {
      if (index !== p && index !== q) {
        const ip = matrix[index][p];
        const iq = matrix[index][q];
        matrix[index][p] = cosine * ip - sine * iq;
        matrix[p][index] = matrix[index][p];
        matrix[index][q] = sine * ip + cosine * iq;
        matrix[q][index] = matrix[index][q];
      }
      const vp = vectors[index][p];
      const vq = vectors[index][q];
      vectors[index][p] = cosine * vp - sine * vq;
      vectors[index][q] = sine * vp + cosine * vq;
    }
  }
  return Array.from({ length: size }, (_, index) => ({
    value: Math.max(0, matrix[index][index]),
    vector: vectors.map((row) => row[index]),
  })).sort((left, right) => right.value - left.value);
}

function prepareFit(rows: ReturnType<typeof fitRows>): FitPreparation {
  const transformed = rows.map(({ values }) => [Math.log1p(values[0]), Math.log1p(values[1]), Math.log1p(values[2]), values[3]]);
  const mean = FEATURES.map((_, feature) => transformed.reduce((sum, row) => sum + row[feature], 0) / Math.max(1, rows.length)) as Vector;
  const variances = FEATURES.map((_, feature) => transformed.reduce((sum, row) => sum + (row[feature] - mean[feature]) ** 2, 0) / Math.max(1, rows.length)) as Vector;
  const constantFeatures = FEATURES.filter((_, feature) => variances[feature] <= 1e-12).map((feature) => feature.name);
  const deviation = variances.map((variance) => Math.sqrt(variance) || 1) as Vector;
  const standardized = transformed.map((row) => row.map((value, feature) => (value - mean[feature]) / deviation[feature]) as Vector);
  const uniquePatterns = new Set(standardized.map((row) => row.map((value) => value.toFixed(10)).join("|"))).size;
  const problem = rows.length < MIN_SAMPLES
    ? `Needs at least ${MIN_SAMPLES} complete observations for k=3; this pod has ${rows.length}.`
    : uniquePatterns < K
      ? `Needs 3 distinct four-metric vectors; this pod has ${uniquePatterns}.`
      : undefined;
  if (rows.length < 2) {
    return { observations: [], mean, deviation, eigenvalues: [], components: [[0, 0, 0, 0], [0, 0, 0, 0]], pcaMean: [0, 0, 0, 0], uniquePatterns, constantFeatures, problem };
  }
  const pcaMean = FEATURES.map((_, feature) => standardized.reduce((sum, row) => sum + row[feature], 0) / rows.length) as Vector;
  const centered = standardized.map((row) => row.map((value, feature) => value - pcaMean[feature]));
  const covariance = Array.from({ length: 4 }, (_, left) => Array.from({ length: 4 }, (_, right) =>
    centered.reduce((sum, row) => sum + row[left] * row[right], 0) / (rows.length - 1)));
  const eigen = eigenSymmetric(covariance);
  const rank = eigen.filter((item) => item.value > Math.max(1e-12, (eigen[0]?.value || 0) * 1e-10)).length;
  const rankProblem = rank < 2 ? `The four-metric data has rank ${rank}; PCA needs at least two varying directions.` : undefined;
  return {
    observations: rows.map((row, index) => ({ ...row, standardized: standardized[index] })),
    mean,
    deviation,
    eigenvalues: eigen.map((item) => item.value),
    components: [(eigen[0]?.vector || [0, 0, 0, 0]) as Vector, (eigen[1]?.vector || [0, 0, 0, 0]) as Vector],
    pcaMean,
    uniquePatterns,
    constantFeatures,
    problem: problem || rankProblem,
  };
}

function seededRandom(seed: number) {
  let value = seed >>> 0;
  return () => {
    value = (value + 0x6D2B79F5) >>> 0;
    let next = value;
    next = Math.imul(next ^ (next >>> 15), next | 1);
    next ^= next + Math.imul(next ^ (next >>> 7), next | 61);
    return ((next ^ (next >>> 14)) >>> 0) / 4294967296;
  };
}

function weightedIndex(weights: number[], random: () => number) {
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  if (total <= 0) return Math.floor(random() * weights.length);
  const target = random() * total;
  let cumulative = 0;
  for (let index = 0; index < weights.length; index += 1) {
    cumulative += weights[index];
    if (cumulative >= target) return index;
  }
  return weights.length - 1;
}

function kmeansPlusPlus(rows: Vector[], random: () => number): Vector[] {
  const centers = [rows[Math.floor(random() * rows.length)].slice() as Vector];
  while (centers.length < K) {
    const weights = rows.map((row) => Math.min(...centers.map((center) => distanceSquared(row, center))));
    centers.push(rows[weightedIndex(weights, random)].slice() as Vector);
  }
  return centers;
}

function runKMeans(rows: Vector[], initial: Vector[]) {
  let centers = initial.map((center) => center.slice() as Vector);
  let labels = new Array<number>(rows.length).fill(-1);
  let iterations = 0;
  let converged = false;
  for (let iteration = 1; iteration <= 300; iteration += 1) {
    iterations = iteration;
    const nextLabels = rows.map((row) => {
      let closest = 0;
      let nearest = distanceSquared(row, centers[0]);
      for (let cluster = 1; cluster < K; cluster += 1) {
        const distance = distanceSquared(row, centers[cluster]);
        if (distance < nearest) { closest = cluster; nearest = distance; }
      }
      return closest;
    });
    const counts = new Array<number>(K).fill(0);
    nextLabels.forEach((label) => { counts[label] += 1; });
    for (let empty = 0; empty < K; empty += 1) {
      if (counts[empty]) continue;
      let candidate = -1;
      let largestError = -1;
      for (let index = 0; index < rows.length; index += 1) {
        const donor = nextLabels[index];
        if (counts[donor] <= 1) continue;
        const error = distanceSquared(rows[index], centers[donor]);
        if (error > largestError) { candidate = index; largestError = error; }
      }
      if (candidate >= 0) {
        counts[nextLabels[candidate]] -= 1;
        nextLabels[candidate] = empty;
        counts[empty] += 1;
      }
    }
    const nextCenters = Array.from({ length: K }, () => [0, 0, 0, 0] as Vector);
    nextLabels.forEach((label, rowIndex) => {
      rows[rowIndex].forEach((value, feature) => { nextCenters[label][feature] += value; });
    });
    nextCenters.forEach((center, cluster) => {
      if (counts[cluster]) center.forEach((_, feature) => { center[feature] /= counts[cluster]; });
      else nextCenters[cluster] = centers[cluster];
    });
    converged = nextLabels.every((label, index) => label === labels[index]);
    labels = nextLabels;
    centers = nextCenters;
    if (converged) break;
  }
  const inertia = rows.reduce((sum, row, index) => sum + distanceSquared(row, centers[labels[index]]), 0);
  return { centers, labels, iterations, converged, inertia };
}

function trainKMeans(prepared: FitPreparation): ClusterFit {
  if (prepared.problem) throw new Error(prepared.problem);
  const rows = prepared.observations.map((row) => row.standardized);
  let random = seededRandom(42);
  let best: ReturnType<typeof runKMeans> | null = null;
  for (let restart = 0; restart < 20; restart += 1) {
    const candidate = runKMeans(rows, kmeansPlusPlus(rows, random));
    if (!best || candidate.inertia < best.inertia) best = candidate;
  }
  if (!best) throw new Error("K-means did not produce a fit.");

  const order = best.centers.map((center, index) => ({ index, score: center.reduce((sum, value) => sum + value, 0) }))
    .sort((left, right) => left.score - right.score || left.index - right.index);
  const remap = new Array<number>(K);
  order.forEach(({ index }, newIndex) => { remap[index] = newIndex; });
  const centersZ = order.map(({ index }) => best!.centers[index]);
  const centersRaw = centersZ.map((center) => center.map((value, feature) => {
    const transformed = value * prepared.deviation[feature] + prepared.mean[feature];
    return feature < 3 ? Math.expm1(transformed) : transformed;
  }) as Vector);
  const centersPc = centersZ.map((center) => {
    const projected = center.map((value, feature) => value - prepared.pcaMean[feature]);
    return [multiplyVector(projected, prepared.components[0]), multiplyVector(projected, prepared.components[1])] as [number, number];
  });
  const explainedTotal = prepared.eigenvalues.reduce((sum, value) => sum + value, 0) || 1;
  const explained: [number, number] = [prepared.eigenvalues[0] / explainedTotal, prepared.eigenvalues[1] / explainedTotal];
  const fittedRows = prepared.observations.map((row, index) => {
    const centered = row.standardized.map((value, feature) => value - prepared.pcaMean[feature]);
    return { ...row, cluster: remap[best!.labels[index]], pc: [multiplyVector(centered, prepared.components[0]), multiplyVector(centered, prepared.components[1])] as [number, number] };
  });
  const counts = new Array<number>(K).fill(0);
  fittedRows.forEach((row) => { counts[row.cluster] += 1; });
  return {
    rows: fittedRows,
    centersZ,
    centersRaw,
    centersPc,
    counts,
    iterations: best.iterations,
    converged: best.converged,
    inertia: best.inertia,
    explained,
    constantFeatures: prepared.constantFeatures,
  };
}

function ScatterPanel({ fit, timezone }: { fit: ClusterFit; timezone: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const series = COLORS.flatMap((color, cluster) => [
      {
        name: `C${cluster + 1}`,
        type: "scatter" as const,
        symbolSize: 7,
        data: fit.rows.filter((row) => row.cluster === cluster).map((row) => ({ value: row.pc, row })),
        itemStyle: { color, opacity: 0.66 },
        emphasis: { itemStyle: { opacity: 1, borderColor: "#183b56", borderWidth: 1 } },
      },
      {
        name: `C${cluster + 1} center`,
        type: "scatter" as const,
        symbol: "path://M-10,-7 L-7,-10 L0,-3 L7,-10 L10,-7 L3,0 L10,7 L7,10 L0,3 L-7,10 L-10,7 L-3,0 Z",
        symbolSize: 18,
        data: [{ value: fit.centersPc[cluster], cluster }],
        itemStyle: { color, borderColor: "#fff", borderWidth: 1 },
        label: { show: true, formatter: `C${cluster + 1}`, position: "top", color: PALETTE.navy, fontSize: 10, fontWeight: 700 },
        tooltip: { show: false },
        z: 5,
      },
    ]);
    chart.setOption({
      animation: false,
      grid: { left: 54, right: 18, top: 20, bottom: 50 },
      xAxis: { type: "value", name: `PC1 · ${number(fit.explained[0] * 100, 1)}%`, nameLocation: "middle", nameGap: 31, nameTextStyle: { color: PALETTE.navy, fontSize: 10 }, axisLabel: { color: "#596579", fontSize: 9 }, axisLine: { lineStyle: { color: "#8c98a8" } }, splitLine: { lineStyle: { color: PALETTE.grid } } },
      yAxis: { type: "value", name: `PC2 · ${number(fit.explained[1] * 100, 1)}%`, nameLocation: "middle", nameGap: 37, nameTextStyle: { color: PALETTE.navy, fontSize: 10 }, axisLabel: { color: "#596579", fontSize: 9 }, axisLine: { lineStyle: { color: "#8c98a8" } }, splitLine: { lineStyle: { color: PALETTE.grid } } },
      tooltip: {
        trigger: "item",
        confine: true,
        formatter: (rawParams: unknown) => {
          const params = rawParams as { data?: { row?: FitObservation } };
          const row = params.data?.row;
          if (!row) return "";
          return `<b>C${row.cluster + 1}</b><br/>${escapeHtml(formatTime(row.timestamp, timezone))}<br/>${FEATURES.map((feature, index) => `${feature.name}: ${metricText(row.values[index], index)}`).join("<br/>")}`;
        },
      },
      series,
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [fit, timezone]);
  return <div className="cluster-scatter" ref={ref} role="img" aria-label="PCA scatter of four-metric observations, colored by fixed three-cluster K-means fit" />;
}

function interpolateColor(from: [number, number, number], to: [number, number, number], weight: number) {
  return `rgb(${from.map((value, index) => Math.round(value + (to[index] - value) * weight)).join(",")})`;
}

function heatColor(value: number, limit: number) {
  const normalized = Math.max(-1, Math.min(1, value / limit));
  const tint: [number, number, number] = normalized < 0 ? [66, 104, 200] : [216, 117, 25];
  return interpolateColor([255, 255, 255], tint, 0.10 + Math.abs(normalized) * 0.36);
}

function HeatmapPanel({ fit }: { fit: ClusterFit }) {
  const limit = Math.max(0.01, ...fit.centersZ.flat().map((value) => Math.abs(value)));
  return <div className="cluster-heatmap-wrap">
    <table className="cluster-heatmap">
      <thead><tr><th scope="col">Cluster</th>{FEATURES.map((feature) => <th scope="col" key={feature.key}>{feature.short}<small>{feature.unit}</small></th>)}</tr></thead>
      <tbody>{fit.centersZ.map((center, cluster) => <tr key={cluster}>
        <th scope="row"><span className="cluster-color" style={{ background: COLORS[cluster] }} />C{cluster + 1}<small>n={fit.counts[cluster]}</small></th>
        {center.map((z, feature) => <td key={feature} style={{ backgroundColor: heatColor(z, limit) }} title={`Center z-score ${number(z, 3)}. Back-transformed center: ${metricText(fit.centersRaw[cluster][feature], feature)}.`}>{metricText(fit.centersRaw[cluster][feature], feature)}<small>z {z >= 0 ? "+" : ""}{number(z, 2)}</small></td>)}
      </tr>)}</tbody>
    </table>
    <div className="heatmap-scale"><span>Below transformed mean</span><i aria-hidden="true" /><span>Above transformed mean</span></div>
    <p className="panel-caption"><b>Back-transformed cluster centers.</b> Text shows original units; color encodes standardized center values on a symmetric zero-centered scale. Zero is the transformed feature mean for this fit, not a gate threshold.</p>
  </div>;
}

function timelineTime(timestamp: number, timezone: string) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: timezone, month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp * 1000));
}

function TimelinePanel({ group, fit, timezone, stepSeconds, rangeStart, rangeEnd }: { group: PodHistory; fit: ClusterFit | null; timezone: string; stepSeconds: number; rangeStart: number; rangeEnd: number }) {
  const clusterAt = new Map((fit?.rows || []).map((row) => [row.timestamp, row.cluster]));
  const first = Math.ceil(rangeStart / stepSeconds) * stepSeconds;
  const last = Math.floor(rangeEnd / stepSeconds) * stepSeconds;
  const middle = first !== undefined && last !== undefined ? (first + last) / 2 : undefined;
  const grid = last >= first ? Array.from({ length: Math.floor((last - first) / stepSeconds) + 1 }, (_, index) => first + index * stepSeconds) : [];
  const evaluations = new Map(group.rows.map((row) => [row.timestamp, row]));
  return <div className="cluster-timeline">
    <div className="timeline-meta"><span><b>{group.modelName}</b> · {group.deployment} · {group.pod}</span><span>{grid.length.toLocaleString()} grid positions · {group.rows.length.toLocaleString()} recorded evaluations · {stepSeconds}s step · {timezone}</span></div>
    {grid.length ? <>
      <div className="timeline-lane"><b>Cluster</b><div className="timeline-cells" role="img" aria-label={`Historical cluster assignments for ${group.pod}`}>
        {grid.map((timestamp) => {
          const row = evaluations.get(timestamp);
          const cluster = clusterAt.get(timestamp);
          const label = cluster === undefined ? "Unknown" : `C${cluster + 1}`;
          const detail = !row ? "missing evaluation" : row.valid ? "complete evaluation" : `incomplete: ${row.reasons.join("; ") || row.state}`;
          const title = `${formatTime(timestamp, timezone)} · ${label} · ${detail}`;
          return <span className={`timeline-cell ${cluster === undefined ? "timeline-unknown" : `timeline-cluster-${cluster + 1}`}`} key={timestamp} title={title} aria-label={title} />;
        })}
      </div></div>
      <div className="timeline-lane"><b>Gate candidate</b><div className="timeline-cells" role="img" aria-label={`Instantaneous gate candidate states for ${group.pod}`}>
        {grid.map((timestamp) => {
          const row = evaluations.get(timestamp);
          const state = !row || !row.valid || row.candidate === null ? "unknown" : row.candidate ? "on" : "off";
          const label = state === "unknown" ? "Unknown" : state === "on" ? "Candidate" : "Off";
          const title = `${formatTime(timestamp, timezone)} · Gate candidate: ${label}${row ? ` · ${row.state}` : " · missing evaluation"}`;
          return <span className={`timeline-cell timeline-gate-${state}`} key={timestamp} title={title} aria-label={title} />;
        })}
      </div></div>
      <div className="timeline-axis"><span>{timelineTime(first!, timezone)}</span><span>{timelineTime(middle!, timezone)}</span><span>{timelineTime(last!, timezone)}</span></div>
    </> : <div className="chart-empty">The selected interval contains no positions on the {stepSeconds}s gate grid.</div>}
    <div className="timeline-legend"><span><i className="timeline-swatch timeline-cluster-1" />C1</span><span><i className="timeline-swatch timeline-cluster-2" />C2</span><span><i className="timeline-swatch timeline-cluster-3" />C3</span><span><i className="timeline-swatch timeline-gate-on" />Candidate</span><span><i className="timeline-swatch timeline-gate-off" />Off</span><span><i className="timeline-swatch timeline-unknown" />Unknown</span></div>
  </div>;
}

function RawMetricViews({ group, gate, timezone, rangeStart, rangeEnd }: { group: PodHistory; gate: GateSnapshot; timezone: string; rangeStart: number; rangeEnd: number }) {
  return <div className="raw-metric-grid">
    {FEATURES.map((feature, index) => {
      const points = group.rows.map((evaluation) => ({ timestamp: evaluation.timestamp, value: featureValue(evaluation, index, gate) }));
      const values = points.flatMap((point) => point.value === null ? [] : [point.value]);
      const low = values.length ? Math.min(...values) : 0;
      const high = values.length ? Math.max(...values) : 1;
      const padding = high === low ? Math.max(0.25, Math.abs(high) * 0.12) : (high - low) * 0.12;
      const min = Math.max(0, low - padding);
      const max = high + padding || 1;
      const first = rangeStart;
      const last = rangeEnd;
      const x = (timestamp: number) => 12 + (last === first ? 0.5 : (timestamp - first) / (last - first)) * 296;
      const y = (value: number) => 94 - ((value - min) / (max - min || 1)) * 76;
      const segments: string[] = [];
      let segment: string[] = [];
      let previous: number | undefined;
      for (const point of points) {
        const continuous = previous === undefined || point.timestamp - previous === gate.policy.evaluation_step_seconds;
        if (point.value === null || !continuous) {
          if (segment.length) segments.push(segment.join(" "));
          segment = [];
        }
        if (point.value !== null) segment.push(`${segment.length ? "L" : "M"}${number(x(point.timestamp), 2)},${number(y(point.value), 2)}`);
        previous = point.value === null ? undefined : point.timestamp;
      }
      if (segment.length) segments.push(segment.join(" "));
      return <article className="raw-metric-card" key={feature.key}>
        <h4>{feature.name}<span>{feature.unit}</span></h4>
        {values.length ? <svg viewBox="0 0 320 118" role="img" aria-label={`${feature.name} valid historical samples for pod ${group.pod}`}>
          <line x1="12" x2="308" y1="18" y2="18" /><line x1="12" x2="308" y1="56" y2="56" /><line x1="12" x2="308" y1="94" y2="94" />
          {segments.map((path, pathIndex) => <path className="raw-series-line" d={path} key={pathIndex} />)}
          {points.map((point) => point.value === null ? null : <circle cx={x(point.timestamp)} cy={y(point.value)} r="2.4" key={point.timestamp}><title>{formatTime(point.timestamp, timezone)} · {metricText(point.value, index)}</title></circle>)}
        </svg> : <div className="raw-chart-empty">No individually valid samples</div>}
        <div className="raw-metric-range"><span>{number(min, 2)}</span><span>{number(max, 2)}</span></div>
      </article>;
    })}
  </div>;
}

function ClusterDiagram({ group, fit, trainedAt, rangeLabel, timezone, stepSeconds, rangeStart, rangeEnd }: {
  group: PodHistory; fit: ClusterFit; trainedAt: string; rangeLabel: string; timezone: string; stepSeconds: number; rangeStart: number; rangeEnd: number;
}) {
  const safeId = `pod-${encodeURIComponent(group.key).replaceAll("%", "-")}`;
  return <article className="pod-cluster-card">
    <header className="pod-cluster-header">
      <div><span className="eyebrow">{group.modelName} · {group.deployment}</span><h3>{group.pod}</h3><p>{rangeLabel} · {timezone}</p></div>
      <span className="pod-sample-badge">{fit.rows.length.toLocaleString()} fit rows</span>
    </header>
    <div className="cluster-diagram-grid">
      <section className="diagram-panel" aria-labelledby={`${safeId}-pca`}>
        <div className="diagram-panel-title"><div><span>A</span><h4 id={`${safeId}-pca`}>PCA scatter</h4></div><small>2D display projection</small></div>
        <ScatterPanel fit={fit} timezone={timezone} />
        <div className="cluster-legend">{fit.counts.map((count, cluster) => <span key={cluster}><i style={{ backgroundColor: COLORS[cluster] }} />C{cluster + 1} · {count.toLocaleString()}</span>)}</div>
        <p className="panel-caption">PC1 + PC2 retain {number((fit.explained[0] + fit.explained[1]) * 100, 1)}% of standardized variance. K-means is fitted on all four transformed features; PCA only displays the result. X marks show projected centers.</p>
      </section>
      <section className="diagram-panel" aria-labelledby={`${safeId}-centers`}>
        <div className="diagram-panel-title"><div><span>B</span><h4 id={`${safeId}-centers`}>Four-metric centers</h4></div><small>k = 3</small></div>
        <HeatmapPanel fit={fit} />
      </section>
      <section className="diagram-panel timeline-panel" aria-labelledby={`${safeId}-timeline`}>
        <div className="diagram-panel-title"><div><span>C</span><h4 id={`${safeId}-timeline`}>Historical timeline</h4></div><small>Fixed {stepSeconds}s evaluation grid</small></div>
        <TimelinePanel group={group} fit={fit} timezone={timezone} stepSeconds={stepSeconds} rangeStart={rangeStart} rangeEnd={rangeEnd} />
      </section>
    </div>
    <footer className="cluster-fit-footer"><span>Trained {trainedAt} · 20 initializations · seed 42 · {fit.iterations} iterations · {fit.converged ? "converged" : "iteration limit reached"}</span><span>Exploratory fit for this selected interval; cluster IDs and PCA axes can change on retraining.</span></footer>
    {fit.constantFeatures.length > 0 && <p className="notice">Constant feature(s) in this fit: {fit.constantFeatures.join(", ")}. They do not provide cluster separation.</p>}
  </article>;
}

export function MetricClustersPage({ gate, rangeLabel, snapshotKey, stale, namespace, timezone, selectedPod, rangeStart, rangeEnd }: {
  gate: GateSnapshot;
  rangeLabel: string;
  snapshotKey: string;
  stale: boolean;
  namespace: string;
  timezone: string;
  selectedPod: string | null;
  rangeStart: number;
  rangeEnd: number;
}) {
  const groups = useMemo(() => podHistories(gate), [gate]);
  const [podFilter, setPodFilter] = useState("all");
  const [training, setTraining] = useState(false);
  const [trainedAt, setTrainedAt] = useState("");
  const [trainingResults, setTrainingResults] = useState<PodTraining[] | null>(null);
  const resultKey = useRef("");
  const selectionKey = `${snapshotKey}:${podFilter}`;

  useEffect(() => {
    const nextPod = selectedPod ? groups.find((group) => group.pod === selectedPod)?.key || "all" : "all";
    setPodFilter(nextPod);
  }, [snapshotKey, selectedPod, groups]);

  useEffect(() => {
    if (resultKey.current !== selectionKey) {
      resultKey.current = selectionKey;
      setTrainingResults(null);
      setTrainedAt("");
    }
  }, [selectionKey]);

  const visibleGroups = useMemo(() => podFilter === "all" ? groups : groups.filter((group) => group.key === podFilter), [groups, podFilter]);
  const prepared = useMemo(() => visibleGroups.map((group) => {
    const rows = fitRows(group);
    try {
      return { group, rows, preparation: prepareFit(rows), error: "" };
    } catch (reason) {
      return { group, rows, preparation: null, error: (reason as Error).message };
    }
  }), [visibleGroups]);
  const completeCount = prepared.reduce((sum, item) => sum + item.rows.length, 0);
  const excludedCount = visibleGroups.reduce((sum, group) => sum + group.rows.length, 0) - completeCount;
  const distinctCount = prepared.reduce((sum, item) => sum + (item.preparation?.uniquePatterns || 0), 0);
  const incompatibleWindows = gate.policy.latency_window !== gate.policy.queue_window;
  const resultMap = new Map((trainingResults || []).map((item) => [item.group.key, item]));
  const needsRawView = (group: PodHistory) => {
    const item = prepared.find((candidate) => candidate.group.key === group.key);
    const result = resultMap.get(group.key);
    return incompatibleWindows || Boolean(result?.problem) || Boolean(item?.error) || Boolean(item?.preparation?.problem);
  };

  const train = async () => {
    setTraining(true);
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    const result: PodTraining[] = prepared.map((item) => {
      if (incompatibleWindows) return { group: item.group, fit: null, problem: `Cannot fit aligned vectors: latency window ${gate.policy.latency_window} and queue window ${gate.policy.queue_window} differ.` };
      if (item.error || !item.preparation) return { group: item.group, fit: null, problem: item.error || "Could not prepare this pod's metrics." };
      if (item.preparation.problem) return { group: item.group, fit: null, problem: item.preparation.problem };
      try { return { group: item.group, fit: trainKMeans(item.preparation) }; }
      catch (reason) { return { group: item.group, fit: null, problem: (reason as Error).message }; }
    });
    setTrainingResults(result);
    setTrainedAt(new Date().toLocaleString());
    setTraining(false);
  };

  const canTrain = visibleGroups.length > 0 && !stale && !training;
  return <section className="wide-card metric-clusters" aria-labelledby="metric-clusters-title">
    <div className="wide-card-title">
      <div><div className="eyebrow">UNSUPERVISED HISTORICAL ANALYSIS</div><h2 id="metric-clusters-title">Four-metric K-means clusters</h2><p>Fit TTFT P95, request TPOT P95, mean waiting requests and KV occupancy together, with a separate model for each pod.</p></div>
      <span className="capacity-tag">Exploratory · no scaling action</span>
    </div>
    <div className="cluster-controls">
      <label><span>Pod scope</span><select value={podFilter} onChange={(event) => setPodFilter(event.target.value)} disabled={Boolean(selectedPod)}>
        {!selectedPod && <option value="all">All pods · fit separately</option>}
        {groups.map((group) => <option value={group.key} key={group.key}>{group.modelName} · {group.pod}</option>)}
      </select></label>
      {selectedPod && <span className="cluster-selected-pod">Using dashboard pod filter: {selectedPod}</span>}
      <span className="cluster-sample-count"><b>{completeCount.toLocaleString()}</b> complete, fresh rows · {distinctCount.toLocaleString()} distinct patterns across pod fits · {excludedCount.toLocaleString()} incomplete or invalid</span>
      <button className="button button-primary" onClick={() => void train()} disabled={!canTrain || incompatibleWindows}>{training ? <><span className="spinner" /> Training…</> : "Retrain and generate diagram"}</button>
    </div>
    {stale && <p className="notice">Dashboard filters or gate policy have changed. Apply them to capture matching history before training.</p>}
    <div className="cluster-method-note">
      <b>Fixed k = 3.</b> TTFT, TPOT and waiting requests use log1p in canonical seconds/counts, then all four features use StandardScaler-style z-scores. Each pod is fit independently with 20 deterministic k-means++ initializations (seed 42). Cluster IDs are ordered by the sum of their standardized centers.
    </div>
    {incompatibleWindows && <div className="alert alert-error" role="status">The latency window ({gate.policy.latency_window}) and queue window ({gate.policy.queue_window}) differ. The fit requires aligned lookback windows; only individually valid raw metric views are shown.</div>}
    {!visibleGroups.length && <div className="cluster-empty">No per-pod gate history is available for this selection.</div>}
    {visibleGroups.map((group) => {
      const result = resultMap.get(group.key);
      const item = prepared.find((candidate) => candidate.group.key === group.key);
      const fit = result?.fit || null;
      const problem = result?.problem || item?.error || item?.preparation?.problem;
      return <div className="cluster-pod-result" key={group.key}>
        {fit ? <ClusterDiagram group={group} fit={fit} trainedAt={trainedAt} rangeLabel={rangeLabel} timezone={timezone} stepSeconds={gate.policy.evaluation_step_seconds} rangeStart={rangeStart} rangeEnd={rangeEnd} /> : (trainingResults || problem) && <div className="cluster-empty cluster-pod-problem"><b>{group.modelName} · {group.deployment} · {group.pod}</b><span>{problem || "This pod could not be fitted."}</span></div>}
        {needsRawView(group) && <section className="raw-fallback" aria-label={`Raw metric history for ${group.pod}`}>
          <div className="diagram-panel-title"><div><span>↗</span><h4>Raw metric history · {group.pod}</h4></div><small>individual validity checks</small></div>
          <p className="panel-caption">Each trace shows only fresh, valid values for that metric. Missing or stale intervals remain gaps; values are not filled or interpolated.</p>
          <RawMetricViews group={group} gate={gate} timezone={timezone} rangeStart={rangeStart} rangeEnd={rangeEnd} />
        </section>}
      </div>;
    })}
    {!trainingResults && visibleGroups.length > 0 && prepared.every((item) => !item.preparation?.problem && !item.error) && !stale && <div className="cluster-empty">{completeCount.toLocaleString()} complete observations are ready across {visibleGroups.length.toLocaleString()} independently fitted pod(s). Select “Retrain and generate diagram” to calculate the three-panel view.</div>}
    <div className="cluster-policy-note">
      <b>Separate gate lane · instantaneous candidate, no hold applied.</b> Candidate means (TTFT &gt; {number(gate.policy.ttft_target_seconds)}s OR TPOT &gt; {number(gate.policy.tpot_target_seconds_per_token * 1000, 0)}ms/token) AND (waiting &gt; {number(gate.policy.waiting_target_requests)} OR KV &gt; {number(gate.policy.kv_target_fraction * 100, 1)}%). Policy windows: latency {gate.policy.latency_window}, queue {gate.policy.queue_window}; grid {gate.policy.evaluation_step_seconds}s. Unknown means the gate evaluation was incomplete. This lane is independent of cluster membership.
    </div>
    <p className="footnote">Namespace: {namespace} · Fit interval: {rangeLabel} · Timezone: {timezone}. Sample guard: at least {MIN_SAMPLES} complete rows per pod (max of 20 and 5 × k), 3 distinct vectors and rank 2 for PCA; this is a practical workflow guard, not a statistical guarantee. Overlapping rolling windows are exploratory and do not establish forecast accuracy.</p>
  </section>;
}
