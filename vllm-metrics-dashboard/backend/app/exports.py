"""Bounded exports from captured immutable dashboard snapshots."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

from .registry import BY_ID, TOP10_IDS
from .settings import settings

FIELDS = [
    "record_type", "metric_id", "metric", "state", "namespace", "deployment", "model_name",
    "timestamp_utc", "timestamp_local", "value", "raw_value", "unit", "labels_json",
    "range_start_utc", "range_end_utc", "promql", "reason",
]


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:80] or "selection"


def _timestamp_pair(value: float | str | None, timezone: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    try:
        parsed = datetime.fromtimestamp(float(value), UTC)
        return parsed.isoformat().replace("+00:00", "Z"), parsed.astimezone(ZoneInfo(timezone)).isoformat()
    except (ValueError, TypeError, OSError):
        return str(value), None


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    filters = snapshot.get("filters", {})
    range_info = filters.get("range", {})
    timezone = range_info.get("timezone", "UTC")
    for metric_id, metric in snapshot.get("metrics", {}).items():
        observed = False
        for series in metric.get("series", []):
            for point in series.get("points", []):
                observed = True
                timestamp_utc, timestamp_local = _timestamp_pair(point.get("timestamp"), timezone)
                rows.append({
                    "record_type": "metric_point", "metric_id": metric_id, "metric": metric.get("name"),
                    "state": metric.get("state"), "namespace": filters.get("namespace"),
                    "deployment": series.get("deployment"), "model_name": series.get("model_name"),
                    "timestamp_utc": timestamp_utc, "timestamp_local": timestamp_local,
                    "value": point.get("value"), "raw_value": point.get("raw"), "unit": metric.get("unit"),
                    "labels_json": json.dumps(series.get("labels", {}), ensure_ascii=False, sort_keys=True),
                    "range_start_utc": range_info.get("start_utc"), "range_end_utc": range_info.get("end_utc"),
                    "reason": metric.get("reason"),
                })
        for summary in metric.get("summary", []):
            observed = True
            timestamp_utc, timestamp_local = _timestamp_pair(summary.get("timestamp"), timezone)
            rows.append({
                "record_type": metric.get("summary_kind", "summary"), "metric_id": metric_id,
                "metric": metric.get("name"), "state": metric.get("state"),
                "namespace": filters.get("namespace"), "deployment": summary.get("deployment"),
                "model_name": summary.get("model_name"), "timestamp_utc": timestamp_utc,
                "timestamp_local": timestamp_local, "value": summary.get("value"), "raw_value": summary.get("raw"),
                "unit": metric.get("summary_unit", metric.get("unit")),
                "labels_json": json.dumps(summary.get("labels", {}), ensure_ascii=False, sort_keys=True),
                "range_start_utc": range_info.get("start_utc"), "range_end_utc": range_info.get("end_utc"),
                "reason": metric.get("reason"),
            })
        for total in metric.get("selected_range_total", []):
            observed = True
            rows.append({
                "record_type": "selected_range_total", "metric_id": metric_id, "metric": metric.get("name"),
                "state": metric.get("state"), "namespace": filters.get("namespace"),
                "deployment": total.get("deployment"), "model_name": total.get("model_name"),
                "timestamp_utc": range_info.get("end_utc"), "timestamp_local": range_info.get("end_local"),
                "value": total.get("value"), "raw_value": total.get("raw"), "unit": metric.get("unit"),
                "labels_json": json.dumps(total.get("labels", {}), ensure_ascii=False, sort_keys=True),
                "range_start_utc": range_info.get("start_utc"), "range_end_utc": range_info.get("end_utc"),
                "reason": metric.get("reason"),
            })
        for query in metric.get("queries", []):
            for kind in ("promql", "summary_promql", "selected_range_promql", "observation_promql"):
                expression = query.get(kind)
                if expression:
                    rows.append({
                        "record_type": "promql_query", "metric_id": metric_id, "metric": metric.get("name"),
                        "state": metric.get("state"), "namespace": filters.get("namespace"),
                        "deployment": query.get("deployment"), "unit": metric.get("unit"),
                        "labels_json": json.dumps({"query_kind": kind}, ensure_ascii=False, sort_keys=True),
                        "range_start_utc": range_info.get("start_utc"), "range_end_utc": range_info.get("end_utc"),
                        "promql": expression, "reason": metric.get("reason"),
                    })
        if not observed:
            rows.append({
                "record_type": "metric_state", "metric_id": metric_id, "metric": metric.get("name"),
                "state": metric.get("state"), "namespace": filters.get("namespace"),
                "timestamp_utc": range_info.get("end_utc"), "timestamp_local": range_info.get("end_local"),
                "unit": metric.get("unit"), "labels_json": "{}", "range_start_utc": range_info.get("start_utc"),
                "range_end_utc": range_info.get("end_utc"), "reason": metric.get("reason"),
            })
    for item in snapshot.get("daily_tokens", {}).get("rows", []):
        interval = item.get("interval", {})
        rows.append({
            "record_type": "daily_token_estimate", "metric_id": item.get("metric_id"),
            "metric": item.get("metric"), "state": item.get("state"),
            "namespace": filters.get("namespace"), "deployment": item.get("deployment"),
            "timestamp_utc": interval.get("start_utc"), "timestamp_local": interval.get("start_local"),
            "value": item.get("estimate"), "unit": item.get("unit"),
            "labels_json": json.dumps(interval, ensure_ascii=False, sort_keys=True),
            "range_start_utc": interval.get("start_utc"), "range_end_utc": interval.get("end_utc"),
            "reason": item.get("reason"),
        })
    if len(rows) > settings.max_export_rows:
        raise OverflowError(f"Export exceeds the configured {settings.max_export_rows:,}-row limit.")
    return rows


def _filename(snapshot: dict[str, Any], extension: str) -> str:
    filters = snapshot["filters"]
    bounds = filters["range"]
    models = filters.get("models", [])
    model = _slug(models[0]["model_name"] if len(models) == 1 else "all-models")
    start = _slug(bounds.get("start_utc", "start").replace(":", "").replace("-", ""))
    end = _slug(bounds.get("end_utc", "end").replace(":", "").replace("-", ""))
    return f"vllm-metrics-{_slug(filters['namespace'])}-{model}-{start}-{end}.{extension}"


def _summary_by_deployment(metric: dict[str, Any], models: list[dict[str, Any]]) -> list[tuple[str, float | None]]:
    values: dict[str, list[float]] = {}
    candidates = metric.get("selected_range_total") or metric.get("summary", [])
    if not candidates:
        for series in metric.get("series", []):
            latest = next((point.get("value") for point in reversed(series.get("points", [])) if point.get("value") is not None), None)
            if latest is not None:
                values.setdefault(str(series.get("deployment") or "Selection"), []).append(float(latest))
    else:
        for item in candidates:
            if item.get("value") is not None:
                values.setdefault(str(item.get("deployment") or "Selection"), []).append(float(item["value"]))
    sum_values = metric.get("aggregation") in {"sum", "sum increase", "sum rate", "sum of per-series max_over_time", "60 * sum rate"} or metric.get("summary_kind") in {"selected_range_increase", "selected_range_total"}
    selected = {str(item.get("deployment")) for item in models if item.get("deployment")}
    selected.update(values)
    if not selected:
        selected.add("Selection")
    return [
        (deployment, (sum if sum_values else max)(values[deployment]) if values.get(deployment) else None)
        for deployment in sorted(selected)
    ]


def _xlsx(snapshot: dict[str, Any], rows: list[dict[str, Any]]) -> bytes:
    import xlsxwriter

    output_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="vllm-metrics-", suffix=".xlsx", dir="/tmp", delete=False) as temporary:
            output_path = temporary.name
        workbook = xlsxwriter.Workbook(output_path, {
            "constant_memory": True, "tmpdir": "/tmp", "strings_to_formulas": False, "strings_to_urls": False,
        })
        header = workbook.add_format({"bold": True, "bg_color": "#eeeeee", "bottom": 1})

        def add_sheet(name: str, headers: list[str], data: list[list[Any]]) -> None:
            sheet = workbook.add_worksheet(name)
            for col, title in enumerate(headers):
                sheet.write_string(0, col, title, header)
            for row_num, row in enumerate(data, start=1):
                for col, value in enumerate(row):
                    if value is None:
                        continue
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        sheet.write_number(row_num, col, value)
                    else:
                        sheet.write_string(row_num, col, str(value))
            sheet.freeze_panes(1, 0)
            if headers:
                sheet.autofilter(0, 0, max(0, len(data)), len(headers) - 1)
                sheet.set_column(0, len(headers) - 1, 22)

        capacity_rows: list[list[Any]] = []
        for rank, metric_id in enumerate(TOP10_IDS, start=1):
            metric = snapshot.get("metrics", {}).get(str(metric_id), {})
            spec = BY_ID[metric_id]
            values = _summary_by_deployment(metric, snapshot.get("filters", {}).get("models", [])) if metric else [("Selection", None)]
            for deployment, value in values:
                capacity_rows.append([
                    rank, metric_id, spec.name, deployment, value,
                    metric.get("summary_unit", spec.unit), metric.get("state", "not_queried"),
                    metric.get("window", spec.window), metric.get("scope", spec.scope),
                    metric.get("aggregation", spec.aggregation), metric.get("description", spec.description),
                ])
        add_sheet("Capacity Top 10", ["Rank", "Catalog ID", "Signal", "Workload", "End or selected-range value", "Unit", "State", "Window", "Scope", "Aggregation", "Interpretation"], capacity_rows)

        daily_rows = [[
            row.get("interval", {}).get("local_date"), row.get("deployment"), row.get("metric"), row.get("estimate"),
            row.get("unit"), row.get("state"), row.get("interval", {}).get("partial"),
            row.get("interval", {}).get("start_utc"), row.get("interval", {}).get("end_utc"), row.get("reason"),
        ] for row in snapshot.get("daily_tokens", {}).get("rows", [])]
        add_sheet("Daily tokens", ["Local date", "Deployment", "Metric", "Estimate", "Unit", "State", "Partial day", "Start UTC", "End UTC", "Reason"], daily_rows)

        filters = snapshot.get("filters", {})
        bounds = filters.get("range", {})
        metadata = [
            ["Snapshot schema", "1.0"], ["Generated UTC", snapshot.get("created_utc")],
            ["Namespace", filters.get("namespace")], ["Served models and deployments", json.dumps(filters.get("models", []), ensure_ascii=False)],
            ["Start UTC", bounds.get("start_utc")], ["End UTC", bounds.get("end_utc")],
            ["Start local", bounds.get("start_local")], ["End local", bounds.get("end_local")],
            ["Timezone", bounds.get("timezone")], ["Step seconds", bounds.get("step_seconds")],
            ["Rate window", bounds.get("window")], ["Percentile", bounds.get("percentile")],
            ["View scope", bounds.get("scope")], ["Adapter", json.dumps(snapshot.get("adapter", {}), ensure_ascii=False)],
            ["Registry version", snapshot.get("registry_version")], ["Evidence note", snapshot.get("evidence_note")],
        ]
        add_sheet("Metadata", ["Property", "Value"], metadata)
        add_sheet("Data", FIELDS, [[row.get(field) for field in FIELDS] for row in rows])
        query_rows: list[list[Any]] = []
        for metric_id, metric in snapshot.get("metrics", {}).items():
            for query in metric.get("queries", []):
                for kind in ("promql", "summary_promql", "selected_range_promql", "observation_promql"):
                    if query.get(kind):
                        query_rows.append([metric_id, metric.get("name"), query.get("deployment"), kind, query[kind]])
        add_sheet("PromQL", ["Catalog ID", "Metric", "Deployment", "Query kind", "Expression"], query_rows)
        workbook.close()
        with open(output_path, "rb") as stream:
            return stream.read()
    finally:
        if output_path and os.path.exists(output_path):
            os.unlink(output_path)


def _pdf(snapshot: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph

    output = io.BytesIO()
    bounds = snapshot["filters"]["range"]
    namespace = _slug(snapshot["filters"]["namespace"])
    doc = SimpleDocTemplate(output, pagesize=landscape(letter), title="vLLM Capacity Planning Snapshot")
    styles = getSampleStyleSheet()
    content = [Paragraph("vLLM Capacity Planning Snapshot", styles["Title"]), Spacer(1, 8)]
    content.append(Paragraph(
        f"Namespace: {namespace} | Local range: {bounds['start_local']} to {bounds['end_local']} ({bounds['timezone']}) | Scope: {bounds['scope']} | Step: {bounds['step_seconds']}s",
        styles["BodyText"],
    ))
    content.append(Spacer(1, 12))
    data = [["Rank / ID", "Capacity signal", "Workload", "Value", "Unit", "Window / scope", "State", "Evidence / note"]]
    for rank, metric_id in enumerate(TOP10_IDS, start=1):
        metric = snapshot.get("metrics", {}).get(str(metric_id), {})
        spec = BY_ID[metric_id]
        unit = metric.get("summary_unit", spec.unit)
        reason = escape(metric.get("reason") or "; ".join(metric.get("warnings", [])) or spec.description)
        for deployment, value in _summary_by_deployment(metric, snapshot.get("filters", {}).get("models", [])) if metric else [("Selection", None)]:
            data.append([
                f"{rank} / {metric_id}", spec.name, escape(deployment), "—" if value is None else f"{value:.4g}", unit,
                f"{metric.get('window', spec.window)} / {metric.get('scope', spec.scope)}",
                metric.get("state", "not_queried"), Paragraph(reason, styles["BodyText"]),
            ])
    table = Table(data, repeatRows=1, colWidths=[45, 105, 95, 48, 58, 82, 58, 180])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#bbbbbb")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
    ]))
    content.extend([table, Spacer(1, 12), Paragraph("Review interpretation", styles["Heading2"]),
        Paragraph("Add replicas when concurrent demand exceeds the capacity that meets latency targets. Evaluate GPU memory or tensor parallelism when an individual request is too slow or cannot fit its memory needs. These metrics support review; they do not trigger scaling or establish a purchase decision.", styles["BodyText"]),
        Spacer(1, 8), Paragraph("No product SLO, headroom factor, or load-tested per-replica capacity was configured for this snapshot. Thresholds should be set by the service owner and validated with representative load tests.", styles["BodyText"]),
        Spacer(1, 8), Paragraph(snapshot.get("evidence_note", ""), styles["BodyText"]),
        Spacer(1, 12), Paragraph("Daily token estimates", styles["Heading2"])])
    daily_data = [["Local date", "Deployment", "Metric", "Estimate", "Unit", "State", "Partial"]]
    for item in snapshot.get("daily_tokens", {}).get("rows", []):
        interval = item.get("interval", {})
        daily_data.append([interval.get("local_date"), item.get("deployment"), item.get("metric"), "—" if item.get("estimate") is None else f"{item['estimate']:.4g}", item.get("unit"), item.get("state"), interval.get("partial")])
    if len(daily_data) == 1:
        daily_data.append(["—", "—", "No observations", "—", "—", snapshot.get("daily_tokens", {}).get("state", "no_observations"), "—"])
    daily_table = Table(daily_data, repeatRows=1, colWidths=[85, 140, 160, 90, 110, 95, 70])
    daily_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#bbbbbb")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 7),
    ]))
    content.extend([daily_table, Spacer(1, 8), Paragraph("Calendar-day counters are estimates of Prometheus increases, not billing totals. Missing data is not zero.", styles["BodyText"])])
    doc.build(content)
    return output.getvalue()


def export_snapshot(snapshot: dict[str, Any], format_name: str) -> tuple[str, str, bytes]:
    rows = _rows(snapshot)
    if format_name == "json":
        document = {
            "schema_version": "1.0", "generated_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "snapshot": snapshot,
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        return _filename(snapshot, "json"), "application/json; charset=utf-8", payload
    if format_name == "csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: _csv_safe(value) for key, value in row.items()} for row in rows)
        return _filename(snapshot, "csv"), "text/csv; charset=utf-8", output.getvalue().encode("utf-8-sig")
    if format_name == "xlsx":
        import xlsxwriter  # noqa: F401 - optional dependency loaded only when requested

        return _filename(snapshot, "xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _xlsx(snapshot, rows)
    if format_name == "pdf":
        return _filename(snapshot, "pdf"), "application/pdf", _pdf(snapshot)
    raise ValueError("Choose csv, json, xlsx, or pdf.")
