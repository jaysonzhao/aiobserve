# vLLM Metrics Dashboard

A read-only OpenShift AI dashboard for standard KServe/vLLM engine metrics. The React/TypeScript frontend uses Apache ECharts. A same-origin FastAPI backend queries Thanos and reads workload ownership metadata. An OpenShift OAuth proxy protects the route and passes the authenticated user identity to the backend for namespace authorization checks.

The dashboard reports observed evidence. It does not modify serving workloads, scale replicas, install autoscaling, or make GPU purchase decisions.

## Current cluster compatibility check

The target cluster was inspected read-only before writing the package. It reports OpenShift `4.20.39`, and its in-cluster Thanos Querier is reachable at `https://thanos-querier.openshift-monitoring.svc:9091` with a service-account bearer token and the OpenShift service CA. The app manifest uses the built-in `cluster-monitoring-view` role for that query access and an explicit namespace allowlist.

Live discovery found vLLM engine metric series in namespace `llm` for:

- `gpt-oss-20b`
- `redhataiqwen25-7b-instruct`
- `redhataiqwen25-7b-instruct-vll`

Kubernetes pod owner references resolve these to the KServe engine Deployments. The app queries raw `vllm:` families only. It does not combine them with `vllm_`, `kserve_vllm:`, llm-d, EPP, or WVA metrics. Admission rejection, HTTP handler, and kube-state-metrics replica families were not established during discovery; those entries remain conditional and show unavailable states rather than zero. The dashboard is deployed to the target cluster; see Validation notes for the live smoke test.

## Features

- Ten ranked capacity signals from catalog IDs `4, 7, 9, 10, 1, 11, 14, 2, 21, 20`, plus the full 41-entry catalog.
- A separate two-metric scale-review gate: service P95 queue time over three completed 15-minute windows plus recurrent preemptions in at least two windows. Capacity overview shows the latest evaluation; Scale gate history evaluates rolling 45-minute periods throughout the selected range and highlights passes. The 0.5-second queue SLO is provisional and editable; the gate never scales workloads.
- Namespace, served-model, deployment, pod, instance, job, engine, time range, timezone, percentile, and rate-window filters.
- Per-pod views and service aggregation. Histograms aggregate buckets before quantile calculation; distinct deployments remain separate.
- Hong Kong-local calendar-day input and output token estimates, with partial-day labels. Rolling 24-hour counters remain a separate view.
- Explicit data states for valid values, valid zero, stale data, no observations, unexported metrics, authorization errors, query errors, and configuration-required capabilities.
- ECharts trends with gaps for missing samples and per-chart PNG downloads.
- Snapshot-based CSV, JSON, XLSX, and PDF exports. Export requests read the captured snapshot instead of rerunning PromQL.
- Bounded single-replica snapshot storage, 30-minute expiry, and creator-bound export downloads.

## Repository layout

```text
vllm-metrics-dashboard/
├── backend/app/             FastAPI API, registry, PromQL compiler, query and export services
├── backend/tests/           Backend behavior tests
├── frontend/src/             React/TypeScript UI, ECharts views, date conversion
├── openshift/               OpenShift ServiceAccount, RBAC, Route and Deployment
├── disconnected/            Offline image archive instructions and disconnected deploy overlay
├── Dockerfile               Multi-stage frontend/backend image
├── docs/                    Study review and scale-gate interpretation
└── README.md
```

For restricted or disconnected clusters, use the [disconnected deployment package](disconnected/README.md). It includes an offline Dockerfile, Podman image archive transfer steps, and a separate overlay that points directly at images in a registry reachable by the cluster; it does not require `oc-mirror`.

See [the scale-gate review](docs/scale-gate-validation.md) for the evidence, Prometheus metric-name compatibility, policy caveats and hardware/model qualifications.

## Local frontend and backend development

Use Python 3.12 and Node.js 22 for the container build. The UI can be type-checked and built without cluster credentials:

```bash
cd vllm-metrics-dashboard/frontend
npm ci
npm test
npm run build
```

For the backend:

```bash
cd vllm-metrics-dashboard/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

To run a local authenticated UI against a cluster, configure a local Thanos port-forward, service CA file, and short-lived token file outside the repository. Set `PROMETHEUS_URL`, `PROMETHEUS_CA_FILE`, and `PROMETHEUS_TOKEN_FILE`, plus `ALLOWED_NAMESPACES` and `LOCAL_DEV_USER`. Keep the files private and remove the token file after use. Then run `uvicorn app.main:app --app-dir backend --reload` and `npm run dev`; Vite proxies `/api` to `127.0.0.1:8000`. The local-user bypass is for development only and is not enabled in the OpenShift manifest.

## Build and deploy to OpenShift

Use these commands to deploy this package after reviewing the target cluster's namespace, monitoring endpoint, and RBAC choices. The dashboard container uses the published image `quay.io/haotech/metricsweb:latest`.

1. Create the dashboard service account and its namespace-scoped metadata role before linking optional registry credentials:

```bash
oc apply -f openshift/namespace-access.yaml
```

If the Quay repository requires authentication, create a pull secret from a protected Docker auth file and link it only to the dashboard service account:

```bash
oc create secret generic dashboard-registry-pull -n llm \
  --from-file=.dockerconfigjson=/secure/path/quay-auth.json \
  --type=kubernetes.io/dockerconfigjson
oc secrets link vllm-metrics-dashboard dashboard-registry-pull --for=pull -n llm
```

Skip this step when the Quay repository is public or the pull secret is already linked. Create the secret once; do not commit the auth file or credentials.

2. Create private OAuth cookie and opaque model-scope signing keys. These values are generated locally and are not stored in this repository:

```bash
oc create secret generic vllm-metrics-dashboard-secrets -n llm \
  --from-literal=cookie-secret="$(openssl rand -hex 16)" \
  --from-literal=scope-signing-key="$(openssl rand -hex 32)"
```

3. Apply workload-scoped metadata RBAC, monitoring access, and the app resources:

```bash
oc apply -f openshift/cluster-access.yaml
oc apply -f openshift/application.yaml
oc rollout status deployment/vllm-metrics-dashboard -n llm
```

4. Review the route, probes, logs, and live connection status:

```bash
oc get route vllm-metrics-dashboard -n llm
oc get pods -n llm -l app.kubernetes.io/name=vllm-metrics-dashboard
oc logs deployment/vllm-metrics-dashboard -n llm -c dashboard
oc logs deployment/vllm-metrics-dashboard -n llm -c oauth-proxy
```

Use the HTTPS Route URL printed by `oc get route`. Validate that a user without `list pods` in an allowlisted namespace receives a denial. The backend performs a SubjectAccessReview for every namespace option and query; the service account's own metadata-read role is not used as a substitute for the user's access check.

The snapshot store is in memory and the manifest deliberately runs one replica. If adding workers or replicas, move snapshot/export storage to a shared private store and preserve creator authorization and TTL eviction.

## Configuration

OpenShift defaults are in [application.yaml](openshift/application.yaml). `ALLOWED_NAMESPACES` is an administrator-maintained allowlist and defaults to `llm`. The backend reads projected tokens from files so token rotation does not require an image or environment change. It verifies Thanos TLS using an injected OpenShift service-CA bundle. The dashboard never accepts a monitoring endpoint or raw PromQL from the browser.

HTTP request, admission rejection, and replica signals are conditional. To enable HTTP signals, first inspect the actual exporter and configure a narrow handler regex and verified label names. Do not set an unverified catch-all handler. The replica signals query kube-state-metrics only when the exact `kube_deployment_*` family and `deployment` label are present.

## Validation notes

The backend and frontend test suites passed during initial implementation. The blank-tab fix passed a production TypeScript/Vite build and live browser click-through. On 2026-10-22, the app was built with an OpenShift Binary BuildConfig and deployed to OpenShift 4.20.39 in namespace `llm`. The OAuth-protected HTTPS route returned healthy and ready status; the namespace API returned the authorized `llm` namespace; model discovery resolved three vLLM workloads to Kubernetes-owned Deployments. A live 24-hour snapshot for `gpt-oss-20b` returned metrics for three pods, including input/output token totals, queue signals, and latency signals. Section tabs load uncaptured metric IDs on demand; unavailable capabilities and stale data remain explicit in the UI.
