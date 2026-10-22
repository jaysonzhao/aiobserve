"""Runtime configuration. Secrets are accepted only as files, never as URLs."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    prometheus_url: str
    prometheus_token_file: str
    prometheus_ca_file: str
    kubernetes_url: str
    kubernetes_token_file: str
    kubernetes_ca_file: str
    allowed_namespaces: tuple[str, ...]
    default_timezone: str
    local_dev_user: str
    scope_signing_key: str
    max_range_days: int
    query_timeout_seconds: float
    max_concurrency: int
    max_points_per_series: int
    max_scale_gate_points: int
    max_series_per_chart: int
    max_discovery_series: int
    max_export_rows: int
    scrape_interval_seconds: int
    max_snapshots: int
    snapshot_ttl_seconds: int
    http_handler_regex: str
    http_method_label: str
    http_handler_label: str
    http_status_label: str

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.getenv(
            "PROMETHEUS_URL",
            "https://thanos-querier.openshift-monitoring.svc:9091",
        ).rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("PROMETHEUS_URL must be an absolute HTTP(S) URL.")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Plain HTTP is allowed only for a local development endpoint.")
        allowed_hosts = set(_csv(os.getenv(
            "ALLOWED_PROMETHEUS_HOSTS",
            "thanos-querier.openshift-monitoring.svc,localhost,127.0.0.1",
        )))
        if parsed.hostname not in allowed_hosts:
            raise ValueError("PROMETHEUS_URL host is outside ALLOWED_PROMETHEUS_HOSTS.")

        key = os.getenv("SCOPE_SIGNING_KEY", "")
        if not key:
            key = secrets.token_urlsafe(48)
        allowed = _csv(os.getenv("ALLOWED_NAMESPACES", ""))
        if any(not name or len(name) > 63 for name in allowed):
            raise ValueError("ALLOWED_NAMESPACES contains an invalid namespace name.")

        return cls(
            prometheus_url=url,
            prometheus_token_file=os.getenv(
                "PROMETHEUS_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"
            ),
            prometheus_ca_file=os.getenv("PROMETHEUS_CA_FILE", "/etc/monitoring-ca/service-ca.crt"),
            kubernetes_url=os.getenv("KUBERNETES_API_URL", "https://kubernetes.default.svc"),
            kubernetes_token_file=os.getenv(
                "KUBERNETES_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"
            ),
            kubernetes_ca_file=os.getenv(
                "KUBERNETES_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
            ),
            allowed_namespaces=allowed,
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Hong_Kong"),
            local_dev_user=os.getenv("LOCAL_DEV_USER", "").strip(),
            scope_signing_key=key,
            max_range_days=int(os.getenv("MAX_RANGE_DAYS", "31")),
            query_timeout_seconds=float(os.getenv("QUERY_TIMEOUT_SECONDS", "15")),
            max_concurrency=int(os.getenv("UPSTREAM_CONCURRENCY", "6")),
            max_points_per_series=int(os.getenv("MAX_POINTS_PER_SERIES", "2000")),
            max_scale_gate_points=int(os.getenv("MAX_SCALE_GATE_POINTS", "25000")),
            max_series_per_chart=int(os.getenv("MAX_SERIES_PER_CHART", "100")),
            max_discovery_series=int(os.getenv("MAX_DISCOVERY_SERIES", "2000")),
            max_export_rows=int(os.getenv("MAX_EXPORT_ROWS", "500000")),
            scrape_interval_seconds=int(os.getenv("SCRAPE_INTERVAL_SECONDS", "30")),
            max_snapshots=int(os.getenv("MAX_SNAPSHOTS", "20")),
            snapshot_ttl_seconds=int(os.getenv("SNAPSHOT_TTL_SECONDS", "1800")),
            http_handler_regex=os.getenv("HTTP_HANDLER_REGEX", ""),
            http_method_label=os.getenv("HTTP_METHOD_LABEL", "method"),
            http_handler_label=os.getenv("HTTP_HANDLER_LABEL", "handler"),
            http_status_label=os.getenv("HTTP_STATUS_LABEL", "status_code"),
        )


settings = Settings.from_env()
