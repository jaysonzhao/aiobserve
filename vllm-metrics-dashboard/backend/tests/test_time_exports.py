from datetime import UTC, datetime

from app.exports import export_snapshot
from app.time_utils import daily_intervals, parse_bounds


def test_daily_interval_is_local_calendar_day_and_respects_dst():
    start, end, zone = parse_bounds("2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00", "America/New_York")
    items = daily_intervals(start, end, zone)
    assert len(items) == 1
    assert items[0]["duration_seconds"] == 23 * 60 * 60
    assert items[0]["partial"] is False


def test_naive_dates_and_ranges_over_31_days_are_rejected():
    try:
        parse_bounds("2026-10-01T00:00", "2026-10-02T00:00", "UTC")
        assert False, "naive time should be rejected"
    except ValueError as exc:
        assert "offsets" in str(exc)


def test_csv_escapes_formula_like_string_cells():
    snapshot = {
        "filters": {"namespace": "llm", "range": {"start_utc": "2026-10-22T00:00:00Z", "end_utc": "2026-10-22T01:00:00Z"}},
        "metrics": {"4": {"name": "P95", "unit": "seconds", "state": "valid", "series": [{"deployment": "=HYPERLINK(1)", "model_name": "m", "labels": {}, "points": [{"timestamp": 1.0, "value": 2.0}]}], "summary": []}},
        "daily_tokens": {"rows": []},
    }
    filename, media_type, body = export_snapshot(snapshot, "csv")
    content = body.decode("utf-8-sig")
    assert filename.endswith(".csv")
    assert media_type.startswith("text/csv")
    assert "'=HYPERLINK(1)" in content


def test_exports_retain_workload_and_promql_provenance():
    snapshot = {
        "filters": {
            "namespace": "llm", "models": [{"model_name": "m", "deployment": "model-a"}],
            "range": {
                "start_utc": "2026-10-22T00:00:00Z", "end_utc": "2026-10-22T01:00:00Z",
                "start_local": "2026-10-22T08:00:00+08:00", "end_local": "2026-10-22T09:00:00+08:00",
                "timezone": "Asia/Hong_Kong", "scope": "pod", "step_seconds": 30,
            },
        },
        "metrics": {
            "4": {
                "name": "P95 TTFT", "unit": "seconds", "summary_unit": "seconds",
                "aggregation": "histogram_quantile", "window": "5m", "scope": "pod",
                "state": "valid", "series": [],
                "summary": [{"deployment": "model-a", "model_name": "m", "value": 0.4}],
                "selected_range_total": [], "queries": [{"deployment": "model-a", "promql": "histogram_quantile(0.95, sum by (le) (rate(vllm:ttft_bucket[5m])))"}],
            },
        },
        "daily_tokens": {"rows": []}, "warnings": [], "evidence_note": "Observed data only.",
    }

    _, _, csv_body = export_snapshot(snapshot, "csv")
    csv_content = csv_body.decode("utf-8-sig")
    assert "promql_query" in csv_content
    assert "model-a" in csv_content
    assert "histogram_quantile" in csv_content

    _, xlsx_type, xlsx_body = export_snapshot(snapshot, "xlsx")
    assert xlsx_type.startswith("application/vnd.openxmlformats-officedocument")
    assert xlsx_body.startswith(b"PK")

    _, pdf_type, pdf_body = export_snapshot(snapshot, "pdf")
    assert pdf_type == "application/pdf"
    assert pdf_body.startswith(b"%PDF")
