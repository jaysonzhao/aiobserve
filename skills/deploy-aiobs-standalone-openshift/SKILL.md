---
name: deploy-aiobs-standalone-openshift
description: Deploy rh-ai-quickstart/ai-observability-summarizer on standalone OpenShift without OpenShift AI, KServe, OGX, or local GPUs; the checked deployment was verified on OpenShift 4.18.56. Build an OpenShift-metrics-only dashboard with no vLLM/GPU pages, use external GPT-OSS-20B MaaS with deferred API-key setup, and document source patches, custom image builds, validation, and rollback.
---

# Deploy AI Observability on standalone OpenShift

## Scope and deployment decisions

The runbook supplied with this task targeted OpenShift **4.20.39**, but the cluster actually used for deployment reported **OpenShift 4.18.56**. This skill records live validation on 4.18.56 only; do not treat that as version-specific acceptance for 4.20.39 or any other release. Audit date: **2026-10-21**. Use upstream commit **048f784a19d049fdf6881d8b8e32c870921d48ea** (2026-06-09); re-audit other revisions. Keep the earlier OpenShift AI skill unchanged and use this separate profile on a fresh checkout. Present only OpenShift CPU, memory, pods, nodes, workload health, control-plane, storage and networking metrics; exclude vLLM and all GPU/accelerator metric pages, cards, catalogs, suggestions and tools even if the cluster has GPUs.

Treat this as a source-audited custom deployment profile. The upstream README lists OpenShift AI as a prerequisite; this profile bypasses its bundled serving stack. The live profile and measured acceptance record are in [deploy/profiles/ocp418-standalone-maas](../../deploy/profiles/ocp418-standalone-maas/README.md).

Deploy the MCP server and Console plugin with **individual Helm charts**. Use the supplied external MaaS endpoint for inference. Do not run `make install`, `make depend`, the umbrella `aiobs-stack` chart, or the quickstart Operator: those paths include infrastructure or model-serving assumptions. Do not set a fabricated `RHOAI_VERSION`, install OpenShift AI/KServe/OGX, patch a DataScienceCluster, or create a local model just to deploy the summarizer.

| Component | Standalone requirement |
|---|---|
| OpenShift Console, service CA, cluster monitoring/Thanos | Required |
| MCP server, Console plugin, reviewed RBAC | Required |
| External GPT-OSS-20B MaaS | Required for AI features, after the API key is added |
| UWM and matching ServiceMonitor/PodMonitor | Only when collecting application/exporter metrics |
| Loki, Tempo, OpenTelemetry, Korrel8r, object storage | Optional for logs, traces and correlation |
| OpenShift AI, KServe, OGX/Llama Stack, RAG, pgvector | Not installed by this profile |
| GPU on the summarizer cluster | Not required; external model compute is separate |

Keep the supplied MaaS resource URL `http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b`. The deployment configured base URL resource + `/v1` and served ID `gpt-oss-20b`, but sent no MaaS request; reachability, served ID, authentication, completion and tool calls remain unverified. The endpoint uses HTTP, so do not send a real bearer key over this connection. No API key or dummy Secret was created. OpenShift metrics passed live checks; model-backed chat, summaries and exports remain **pending protected transport and API-key configuration**.

Apply the companion source patch `aiobs-standalone-openshift-source.patch` (the pinned patch is available at `assets/aiobs-standalone-openshift-source.patch`). The patch changes the dashboard, backend metric scope and MCP Helm templates: emit MaaS/extra environment variables, add a restricted container security context, allow an externally managed combined CA ConfigMap, and replace cluster RBAC with bindings to built-in `view` and `cluster-monitoring-view`. Add optional existing telemetry ClusterRoles explicitly. Rebuild **both MCP and Console plugin images**; the old released images do not contain this profile. The application's InferenceService discovery returns an empty result on an absent KServe API (404), preserving configured external models; the `maas/` chat branch uses the OpenAI SDK without the local RAG branch.

## 1. Collect inputs and preflight

Collect namespace, architecture, model endpoint, deferred-key plan, trusted CA roots, image registry access and telemetry scope. Use the external MaaS model already supplied; do not require any model-serving software on this cluster. Verify model/runtime tool calling when the real key becomes available. Use the native Console plugin UI (`DEV_MODE=false`); “standalone OpenShift” describes the absence of OpenShift AI, not the development React UI mode.

Require `oc` logged into the intended cluster, Helm 3, Git, Python 3 with PyYAML, `jq`, Mike Farah `yq` v4, and `curl`. Require cluster-admin or delegated equivalent only for namespace/cluster RBAC, monitoring configuration, ConsolePlugin, and operator installation. Do not grant cluster-admin to the application SA. For source image builds, use Podman/Docker or OpenShift Binary BuildConfigs; front-end builds require the repository's Node 24 / Yarn toolchain. The validated cluster build used Binary BuildConfigs because no local container daemon was available. Do not reuse the upstream public application image tags after changing source.

```bash
oc whoami
oc whoami --show-server
oc get clusterversion version -o jsonpath='{.status.desired.version}{"\n"}'
oc get co
oc get nodes -L kubernetes.io/arch
oc get storageclass
oc get consoleplugin
oc -n openshift-monitoring get svc thanos-querier
oc get clusterrole view cluster-monitoring-view
helm list -A
oc get subscription -A
oc get servicemonitor,podmonitor -A
oc get crd | rg 'lokistacks|tempostacks|opentelemetrycollectors|clusterlogforwarders|uiplugins'
```

Record absence of optional APIs without treating it as a metrics-only blocker. Stop on unhealthy required operators. Record the exact cluster version and re-audit APIs and schemas on every target; this deployment was checked on 4.18.56, not 4.20.39. Absence of OpenShift AI, DataScienceCluster, OGX and KServe APIs is expected, not a prerequisite failure. The upstream image build defaults to amd64; inspect multi-architecture manifests or build matching images on other architectures.

Choose **A: existing monitoring and model endpoint** by default; enable logs/traces only when configured. Choose **B: isolated full-telemetry pilot** only if dedicated infrastructure is needed. Never take ownership of existing Helm releases, shared Loki/Tempo stores, object storage, or installed operator subscriptions merely to satisfy the quickstart defaults.

## 2. Pin the source and apply the compatibility patch

```bash
git clone https://github.com/rh-ai-quickstart/ai-observability-summarizer.git
cd ai-observability-summarizer
git checkout 048f784a19d049fdf6881d8b8e32c870921d48ea
git switch -c compat/standalone-openshift
```

Download the companion source patch next to this skill (or use `assets/aiobs-standalone-openshift-source.patch` in an installed skill). It applies only to the pinned commit above. Set `SOURCE_PATCH` to its absolute path; keep the patch outside the upstream checkout:

```bash
export SOURCE_PATCH=/absolute/path/aiobs-standalone-openshift-source.patch
git apply --check "$SOURCE_PATCH"
git apply "$SOURCE_PATCH"
git diff --check
helm lint deploy/helm/mcp-server
helm lint deploy/helm/openshift-console-plugin
```

Review the exact changes in `git diff` and the following source map:

| Source path (relative to repository) | Modification |
|---|---|
| `openshift-plugin/console-extensions.json`, `openshift-plugin/src/plugin/console-extensions.json` | Remove vLLM route; direct retained plugin routes through the OpenShift-only shell |
| `openshift-plugin/package.json` | Remove vLLM exposed module; update plugin description |
| `openshift-plugin/src/core/pages/AIObservabilityPage.tsx` | Replace overview/accelerator navigation with OpenShift Metrics + Chat; retain MaaS Settings; remove GPU availability probes/banner/model-insight cards |
| `openshift-plugin/src/core/pages/OpenShiftMetricsPage.tsx` | Remove GPU fleet card/component and accelerator category summary from the OpenShift page itself |
| `openshift-plugin/src/core/data/openshiftMetricsConfig.ts` | Remove accelerator metric category and all its metric definitions |
| `openshift-plugin/src/core/components/AIModelSettings/tabs/MetricsSettingsTab.tsx` | Keep OpenShift metrics and scoped chat catalog; remove vLLM settings/download tab |
| `openshift-plugin/src/core/components/MetricsChatPanel.tsx` | Remove accelerator-category suggestions from platform chat |
| `openshift-plugin/src/core/hooks/useChatHistory.ts` | Use a separate history key so prior GPU/vLLM conversations are not loaded into the new dashboard |
| `openshift-plugin/src/react-ui/{App,Navigation,Layout}.tsx` | Keep development UI navigation consistent; remove GPU probes and metric navigation |
| `src/core/metric_scope.py` | Add a shared filter for known GPU/accelerator/vLLM families |
| `src/core/metrics.py` | Exclude accelerator categories and queries from platform metric groups |
| `src/core/metrics_catalog.py` | Disable accelerator discovery; filter catalogs and validation additions so excluded categories cannot reappear |
| `src/core/chat_with_prometheus.py` | Filter raw-name/metadata discovery and returned named series; reject explicitly excluded metric queries |
| `src/chatbots/{base,openai_bot}.py` | Replace both the shared system prompt and SDK-specific examples with OpenShift-only instructions; preserve namespace scoping and MaaS transport |
| `src/mcp_server/observability_mcp.py` | Remove vLLM/GPU MCP registrations; retain shared model configuration, OpenShift and Prometheus tools |
| `src/mcp_server/tools/{model_config_tools,time_range}.py` | Add MaaS model-list/config helpers and keep OpenShift time parsing independent from removed vLLM tools |
| `deploy/helm/mcp-server/templates/{deployment,configmap-ca,crb,llamastack-config}.yaml` and chart values | MaaS/CA env wiring, security context, external CA ownership, built-in monitoring role bindings, and disabled Llama Stack configuration |
| `openshift-plugin/__tests__/pages/OpenShiftOnlyDashboard.test.tsx` | Verify the remaining tabs, default cluster dashboard, scoped chat and MaaS settings |

The exact patch is the implementation, not a CSS-only hide. Known metric-family filtering is product scope, not a complete Prometheus authorization boundary; apply the access controls below. The external MaaS model remains available for analysis; its model identifier in Settings is not a vLLM metrics dashboard.

Build assets before the plugin container (its Dockerfile copies `dist/plugin`). From the repository root:

```bash
export APP_IMAGE_PREFIX=quay.io/YOUR_ORGANIZATION
export MCP_REPOSITORY="$APP_IMAGE_PREFIX/aiobs-mcp-server"
export PLUGIN_REPOSITORY="$APP_IMAGE_PREFIX/aiobs-console-plugin"
export MCP_TAG=openshift-only-20261021
export PLUGIN_TAG=openshift-only-20261021
pushd openshift-plugin
CYPRESS_INSTALL_BINARY=0 yarn install --frozen-lockfile
yarn typecheck
yarn test --runInBand __tests__/pages/OpenShiftOnlyDashboard.test.tsx
yarn build:plugin
popd
podman build -f src/mcp_server/Dockerfile -t "$MCP_REPOSITORY:$MCP_TAG" src
podman build -f openshift-plugin/Dockerfile.plugin -t "$PLUGIN_REPOSITORY:$PLUGIN_TAG" openshift-plugin
podman push "$MCP_REPOSITORY:$MCP_TAG"
podman push "$PLUGIN_REPOSITORY:$PLUGIN_TAG"
```

For a different registry, use architecture-appropriate builds and pull credentials. Record the source commit, patch hash and immutable image digests. Do not run make helpers. This deployment passed Python compilation, metric-scope checks, frontend typecheck, the focused OpenShift-only Jest test, plugin bundle build, Helm lint, Helm rendering/server dry-runs, in-cluster image builds, and live rollout checks. The live MCP exposed 29 tools with no GPU/vLLM/Tempo/Korrel8r tools and 18 categories with no GPU/accelerator category; an in-pod Thanos query returned HTTP 200 and Prometheus success with live series. The Console plugin manifest returned HTTP 200, both Deployments were 1/1 Ready, and the Console operator was Available and not Degraded. Model API access, chat inference, summaries, report export, and tool calling remain unverified because the configured MaaS route is HTTP and no key was added. The upstream suite still contains tests for retired vLLM behavior that require corresponding updates if running the full suite.

## 3. Configure MaaS now; add its API key later

Use a dedicated namespace. Export these non-secret inputs from the repository root. Preserve the model-specific `/llm/gpt-oss-20b` path; do not point the client at the host root or append `:8080`.

| Setting | Selected value |
|---|---|
| Supplied model resource URL | `http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b` |
| Assumed OpenAI base URL | `http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b/v1` |
| Assumed completion URL | `http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b/v1/chat/completions` |
| Initial served model ID / UI selection | `gpt-oss-20b` / `maas/gpt-oss-20b` |
| Later credential | Secret `ai-maas-credentials`, field `gpt-oss-20b` |

```bash
export NS=ai-observability-standalone
export MAAS_RESOURCE_URL=http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b
export MODEL_BASE_URL="${MAAS_RESOURCE_URL%/}/v1"
export MODEL_ID=gpt-oss-20b
export MODEL_SECRET_FIELD=gpt-oss-20b
# Retain MCP_REPOSITORY/MCP_TAG and PLUGIN_REPOSITORY/PLUGIN_TAG from the custom builds above.
export PLUGIN_PATCHER_IMAGE='registry.redhat.io/openshift4/ose-tools-rhel9@sha256:ee65b244fc94d5765514c604dc0a288517dcdce68de65128d047622b6b30a618'
oc create namespace "$NS" --dry-run=client -o yaml | oc apply -f -
python3 - <<'PYCODE'
import json, os, re
from urllib.parse import urlsplit
base = os.environ['MODEL_BASE_URL'].rstrip('/')
parsed = urlsplit(base)
if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment or not parsed.path.endswith('/v1'):
    raise SystemExit('Supply a complete base URL ending /v1, with no query or fragment')
model = os.environ['MODEL_ID']
field = os.environ['MODEL_SECRET_FIELD']
if not re.fullmatch(r'[A-Za-z0-9._-]+', field):
    raise SystemExit('Invalid Kubernetes Secret field')
config = {'maas/' + model: {
    'external': True, 'requiresApiKey': True, 'provider': 'maas',
    'serviceName': None, 'apiUrl': base + '/chat/completions', 'modelName': model,
    'apiKeySecretField': field,
    'cost': {'prompt_rate': 0.0, 'output_rate': 0.0},
    '_metadata': {'source': 'user', 'description': 'GPT-OSS-20B MaaS; key configured later'}
}}
with open('model-config.maas.json', 'w') as f:
    json.dump(config, f, indent=2)
PYCODE
oc -n "$NS" create configmap ai-model-config \
  --from-file=model-config.json=model-config.maas.json --dry-run=client -o yaml | oc apply -f -
```

For a new namespace, run the seed command above. On upgrades export and merge the existing `ai-model-config` so other models are preserved. Verify both application images and `PLUGIN_PATCHER_IMAGE` are pullable, inspect their digests, and use approved mirrored references where needed. The hook image must provide `oc`, `jq`, and Bash. Keep pull secrets scoped to workload SAs.

Keep `requiresApiKey: true` while credentials are pending. The `maas/` prefix controls provider routing; it is stripped before sending the actual served model ID to the endpoint. This profile uses per-model MaaS credentials, not `ai-openai-credentials`, `LLM_API_TOKEN`, an HF token, or the OpenShift console login token. Setting `env.MAAS_API_URL` alone does not supply credentials or replace the model ConfigMap's `apiUrl`.

The supplied endpoint uses **HTTP**, so a bearer key would travel without TLS. Preserve the supplied URL during configuration; use an available HTTPS endpoint or another protected transport before sending the real key. Do not silently change scheme or disable TLS verification. Service-CA trust is still required for internal OpenShift monitoring even when MaaS uses HTTP.

### Later: configure the real API key

Skip this entire block during initial deployment. Alternatively, later open AI Observability → Settings → Add Model, choose **Model as a Service (MaaS)**, set model ID `gpt-oss-20b` (without `maas/`), enter the full base URL above and the key. For the CLI, set the non-secret exports above, then run without shell tracing; the patch preserves other model keys in an existing Secret:

```bash
umask 077
read -r -s -p 'MaaS API key: ' MODEL_API_KEY; printf '\n'
if [ -n "$MODEL_API_KEY" ]; then
  credential_file=$(mktemp)
  credential_patch=$(mktemp)
  printf '%s' "$MODEL_API_KEY" > "$credential_file"
  jq -n --rawfile key "$credential_file" --arg field "$MODEL_SECRET_FIELD" \
    '{stringData:{($field):$key}}' > "$credential_patch"
  if oc -n "$NS" get secret ai-maas-credentials >/dev/null 2>&1; then
    oc -n "$NS" patch secret ai-maas-credentials --type=merge --patch-file "$credential_patch"
  else
    oc -n "$NS" create secret generic ai-maas-credentials --from-file="$MODEL_SECRET_FIELD=$credential_file"
  fi
  rm -f "$credential_file" "$credential_patch"
else
  printf 'No key entered; credentials remain pending.\n'
fi
unset MODEL_API_KEY
```

After adding the key, retry model discovery, chat, tool calling, summaries and exports. Refresh the UI; if configuration remains cached, restart only `deployment/aiobs-mcp-server` and wait for its rollout. If `/v1/models` returns a different served ID, update the ConfigMap key/modelName and the UI selection together, retaining a valid `apiKeySecretField`. Do not redeploy the model or the bundled RAG stack merely to configure MaaS access.

## 4. Preserve monitoring and establish trust

For application/exporter metrics only, enable UWM when missing; built-in cluster metrics do not require UWM. Back up `openshift-monitoring/cluster-monitoring-config`, extract `.data["config.yaml"]`, change only `.enableUserWorkload = true` with yq, and patch that string back; preserve resourceVersion or re-read and compare immediately before patching. If absent, create the ConfigMap. Do not apply the quickstart's replacement ConfigMap to a configured cluster. Do not enable user Alertmanager unless alerting is in scope.

```bash
oc -n openshift-monitoring get cm cluster-monitoring-config -o yaml > cluster-monitoring-config.before.yaml
oc -n openshift-monitoring get cm cluster-monitoring-config -o jsonpath='{.data.config\.yaml}' > monitoring-current.yaml
yq eval '.enableUserWorkload = true' monitoring-current.yaml > monitoring-updated.yaml
jq -n --rawfile config monitoring-updated.yaml '{data:{"config.yaml":$config}}' > monitoring-patch.json
# Re-read and compare current config to the backup before applying on a shared cluster.
oc -n openshift-monitoring patch cm cluster-monitoring-config --type=merge --patch-file monitoring-patch.json
oc -n openshift-user-workload-monitoring get pods
```

Use that snippet only when the ConfigMap already exists and a monitoring change is needed. For an absent ConfigMap use `oc create configmap ... --from-file=config.yaml=monitoring-updated.yaml`, where the file contains `enableUserWorkload: true`. Enable scraping of the actual application workload namespace only when those platform metrics are in scope. Inspect its namespace monitoring labels and any existing ServiceMonitor/PodMonitor; do not create duplicate scrapers.

Build a full PEM trust bundle. The chart's injected service CA alone does not include public roots or private Route CAs. Use a maintained public root bundle approved for the runtime, append OpenShift service CA, and append the enterprise CA chain if the model/Route uses it. Do not use a leaf server certificate as a CA or disable verification.

```bash
oc -n "$NS" create configmap aiobs-service-ca --dry-run=client -o yaml | oc apply -f -
oc -n "$NS" annotate cm aiobs-service-ca service.beta.openshift.io/inject-cabundle=true --overwrite
# Wait for the injected data to appear, then export it.
oc -n "$NS" get cm aiobs-service-ca -o jsonpath='{.data.service-ca\.crt}' > service-ca.pem
test -s service-ca.pem
export PUBLIC_CA_FILE=/path/to/approved-public-root-bundle.pem
cat "$PUBLIC_CA_FILE" service-ca.pem > ca-bundle.pem
# If applicable: cat /path/to/enterprise-root-and-intermediates.pem >> ca-bundle.pem
oc -n "$NS" create configmap aiobs-mcp-server-trusted-ca-bundle \
  --from-file=service-ca.crt=ca-bundle.pem --dry-run=client -o yaml | oc apply -f -
```

The `service-ca.crt` key name is required by the existing chart volume; its contents intentionally contain the full bundle. Leave **no** service-CA injection annotation on this combined ConfigMap, because injection would overwrite the combined data. Set `trustedCA.create: false` below to prevent Helm from creating/annotating it. Maintain this bundle through CA rotations and restart the MCP deployment after updates. The service CA and public/private roots are separate trust inputs.

## 5. Install individual application charts

Create `values-mcp-standalone.yaml`:

```yaml
env:
  DEV_MODE: "false"
  MAAS_API_URL: "http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b/v1"
  PROMETHEUS_URL: "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091"
  LLM_TIMEOUT_SECONDS: 180
route:
  enabled: false
tls:
  enabled: true
  secretName: aiobs-mcp-server-tls
  serviceCertAnnotation: service.beta.openshift.io/serving-cert-secret-name
trustedCA:
  enabled: true
  create: false
rbac:
  extraClusterRoles: []
extraEnv:
  - name: REQUESTS_CA_BUNDLE
    value: /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
  - name: SSL_CERT_FILE
    value: /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
```

Merge actual `env.TEMPO_URL`, `env.TEMPO_TENANT_ID`, `env.KORREL8R_URL` and `env.MAAS_API_URL` if used. Do not point these at guessed service names. The application can start with only metrics; label missing logs/traces as unavailable until their acceptance tests pass. Do not set `VERIFY_SSL=false`. Avoid duplicating built-in env names in `extraEnv`.

Review the patched RBAC before install. It binds the application SA to built-in `view` and `cluster-monitoring-view`; it creates no KServe discovery role, custom Grafana role, or references to absent Loki/Korrel8r roles. Cluster `view` permits broad read access; keep the shared-identity deployment restricted as described below. The namespaced credential/config Role remains in the chart. For optional telemetry, add **actual existing** tenant-reader roles in `rbac.extraClusterRoles`; verify those roles before the upgrade. Do not grant cluster-admin to the MCP SA.
```bash
helm template aiobs-mcp-server deploy/helm/mcp-server -n "$NS" \
  -f values-mcp-standalone.yaml --set-string image.repository="$MCP_REPOSITORY" \
  --set-string image.tag="$MCP_TAG" > rendered-mcp.yaml
helm template aiobs-console-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  --set-string plugin.image.repository="$PLUGIN_REPOSITORY" \
  --set-string plugin.image.tag="$PLUGIN_TAG" \
  --set-string plugin.jobs.patchConsoles.image="$PLUGIN_PATCHER_IMAGE" > rendered-plugin.yaml
oc apply --dry-run=server -f rendered-mcp.yaml
oc apply --dry-run=server -f rendered-plugin.yaml
helm upgrade --install aiobs-mcp-server deploy/helm/mcp-server -n "$NS" \
  -f values-mcp-standalone.yaml --set-string image.repository="$MCP_REPOSITORY" \
  --set-string image.tag="$MCP_TAG" --atomic --wait --timeout 10m
helm upgrade --install aiobs-console-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  --set-string plugin.image.repository="$PLUGIN_REPOSITORY" \
  --set-string plugin.image.tag="$PLUGIN_TAG" \
  --set-string plugin.jobs.patchConsoles.image="$PLUGIN_PATCHER_IMAGE" --atomic --wait --timeout 10m
oc -n "$NS" rollout status deployment/aiobs-mcp-server --timeout=5m
oc -n "$NS" rollout status deployment/aiobs-console-plugin --timeout=5m
oc get consoleplugin aiobs-console-plugin -o yaml
oc get console.operator.openshift.io cluster -o jsonpath='{.spec.plugins}{"\n"}'
```

Retain the chart's post-install registration hook, which merges `aiobs-console-plugin` into the existing plugin list. Do not replace the entire list or register `openshift-ai-observability`. If a stale name was added by an earlier quickstart install, remove only that confirmed stale entry. Refresh the console browser after the plugin is enabled.

The MCP SA mounts a service-account-token Secret populated by the token controller on the validated OpenShift 4.18.56 cluster; verify it is populated without printing it. For production rotation, migrate to projected tokens with the appropriate API audience and **retain** API `ca.crt`, service CA and namespace files; update clients that cache the token at process start. Replacing the volume alone does not complete token rotation.

## 6. Validate from the MCP pod

Before adding the API key, validate pod readiness and plugin loading, then run this no-model-key metrics test from the repository root. It uses the application's own service account and CA bundle:

```bash
oc -n "$NS" exec -i deploy/aiobs-mcp-server -- python - <<'PYMETRICS'
import os, requests
sa = open('/var/run/secrets/kubernetes.io/serviceaccount/token').read().strip()
r = requests.get(os.environ['PROMETHEUS_URL'] + '/api/v1/query',
    params={'query': 'up'}, headers={'Authorization': 'Bearer ' + sa}, timeout=30)
r.raise_for_status()
assert r.json()['status'] == 'success' and r.json()['data']['result']
print('PASS: local cluster metrics are queryable without a MaaS API key')
PYMETRICS
```

Open `/observe/ai-observability` in the OpenShift Console and confirm Fleet Overview and Node Metrics display numeric values, with no vLLM, GPU, accelerator tabs/cards or accelerator-unavailable banner. A direct Thanos/MCP query does not verify the Console proxy path. The Console proxy connects to the MCP Service over HTTPS: keep `tls.enabled: true`, use the Service serving-certificate annotation and mounted Secret, and confirm the Secret has been issued. If the cards show em dashes or “No metrics data available,” inspect Console and MCP logs for TLS handshake errors, then confirm Console proxy `/config` and `/mcp` requests reach MCP successfully. Mark chat, AI summaries and AI-generated reports as pending while the key is missing. After configuring the key in section 3, run the following through `oc -n "$NS" exec -i deploy/aiobs-mcp-server -- python -`; it prints no credentials:

```python
import base64, json, os, requests
ns = os.environ['NAMESPACE']
sa = open('/var/run/secrets/kubernetes.io/serviceaccount/token').read().strip()
api_ca = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
api = 'https://kubernetes.default.svc/api/v1/namespaces/' + ns
headers = {'Authorization': 'Bearer ' + sa}
def get(path):
    r = requests.get(api + path, headers=headers, verify=api_ca, timeout=20)
    r.raise_for_status()
    return r.json()
config = json.loads(get('/configmaps/ai-model-config')['data']['model-config.json'])
model = next(v for k, v in config.items() if k.startswith('maas/'))
secret = get('/secrets/ai-maas-credentials')
key = base64.b64decode(secret['data'][model['apiKeySecretField']]).decode()
url = model['apiUrl']
base = url.removesuffix('/chat/completions')
auth = {'Authorization': 'Bearer ' + key}
r = requests.get(base + '/models', headers=auth, timeout=30)
r.raise_for_status()
assert model['modelName'] in [x['id'] for x in r.json()['data']]
r = requests.post(url, headers=auth, json={
    'model': model['modelName'], 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
    'max_tokens': 32, 'stream': False}, timeout=180)
r.raise_for_status()
assert r.json().get('choices'), 'No chat choices'
from openai import OpenAI
sdk = OpenAI(api_key=key, base_url=base)
assert sdk.chat.completions.create(model=model['modelName'], messages=[
    {'role': 'user', 'content': 'Reply with OK.'}], max_tokens=32).choices
p = requests.get(os.environ['PROMETHEUS_URL'] + '/api/v1/query',
    params={'query': 'up'}, headers=headers, timeout=30)
p.raise_for_status()
assert p.json()['status'] == 'success'
assert p.json()['data']['result'], 'No up series returned'
print('PASS: exact model ID, Requests chat, SDK chat, and Prometheus access')
```

Some MaaS gateways intentionally omit `/models`; use its documented discovery method and record that exception, then prove completion and tool calls using the selected model ID. A successful completion alone does not prove tool calling. Check prompt plus generation tokens against the served context limit; summaries default to up to 6000 generated tokens in `src/core/llm_client.py`. If the serving limit is lower, make that limit configurable or reduce it in source, rebuild the MCP image, and retest a realistic summary. In the UI select `maas/gpt-oss-20b` (or the confirmed served ID with the `maas/` prefix), ask for CPU usage in a known namespace, and require actual metric discovery, PromQL execution, and a grounded answer. Also test OpenShift analysis and report export separately.

Validate all category selectors, Settings metric downloads, chat suggestions and chart/report exports for the requested platform scope. Even when device exporters exist in the cluster, the patched catalog must contain no `gpu_ai`, `vllm`, DCGM, NVIDIA, Gaudi/Habana, ROCm or other known accelerator metric families. Confirm the MCP tool list excludes `get_gpu_info`, `analyze_vllm`, `fetch_vllm_metrics_data`, `list_vllm_namespaces` and `chat_vllm` while keeping `list_summarization_models`, `analyze_openshift` and `chat`.

Open retired `/observe/ai-observability/vllm` URLs directly: they must not render a vLLM page. Check browser networking for absence of GPU availability or vLLM metric calls on page load and tab/settings transitions. Rebuild rather than merely changing Helm values when old pages remain; verify deployed image digests and refresh plugin assets in the console browser.

## 7. Add logs, traces, and correlation when required

For **existing telemetry**, retain installed operators, CRs and release ownership. Record actual LokiStack/TempoStack gateway URLs, tenant names, tenant-read ClusterRoles, collectors, storage, and retention. Configure MCP values and Korrel8r stores consistently. The default MCP `TEMPO_TENANT_ID=dev` must match the configured gateway tenant path; do not substitute an arbitrary backend UUID. Bind the MCP and Korrel8r SAs to their actual tenant authorization roles and test a known trace/log record.

For **new telemetry**, use supported operator channels offered by this cluster's `redhat-operators` catalog for Cluster Observability, Red Hat build of OpenTelemetry, Tempo, Logging, and Loki. Inspect existing subscriptions, namespaces and OperatorGroups; do not install a second operator to fix a missing CRD. Wait for CSV `Succeeded` and confirm these served APIs before rendering resources:

| Resource | Upstream manifest API |
|---|---|
| `LokiStack` | `loki.grafana.com/v1` |
| `TempoStack` | `tempo.grafana.com/v1alpha1` |
| `OpenTelemetryCollector`, `Instrumentation` | `opentelemetry.io/v1beta1`, `opentelemetry.io/v1alpha1` |
| `ClusterLogForwarder` | `observability.openshift.io/v1` |
| `UIPlugin` | Inspect the chart and installed `observability.openshift.io` schema |

Render and server-dry-run **each** individual chart before applying. Create object storage and bucket credentials first; then Tempo/Loki; then collectors and Korrel8r. Use existing production S3-compatible storage if available. Use individual Helm charts and explicit values for optional infrastructure; do not call make helpers or set an RHOAI version. These observability Operators belong to OpenShift and do not require installing OpenShift AI.

For a new dedicated pilot, prepare reviewed `values-storage.yaml`, `values-tempo.yaml`, `values-loki.yaml`, `values-otel.yaml` and `values-korrel8r.yaml` using each chart's actual keys. Use a unique strong object-store credential in the storage file's `minio.secret` and the matching `minio.s3` entries for Tempo and Loki. Treat these local values and Helm release history as sensitive. For existing S3, use the chart's real secret/endpoint/bucket fields and skip MinIO. Remove the broad Tempo binding before this sequence. After operator readiness and schema dry-runs, install in this order:

```bash
helm upgrade --install minio-observability-storage deploy/helm/minio -n observability-hub --create-namespace -f values-storage.yaml --wait --timeout 10m
helm upgrade --install tempo deploy/helm/observability/tempo -n observability-hub -f values-tempo.yaml --wait --timeout 10m
helm upgrade --install loki-stack deploy/helm/observability/loki -n openshift-logging --create-namespace -f values-loki.yaml --wait --timeout 15m
# Check TempoStack/LokiStack conditions and gateway endpoints; Helm --wait alone is insufficient for CR readiness.
oc -n observability-hub get tempostack -o yaml
oc -n openshift-logging get lokistack,clusterlogforwarder -o yaml
helm upgrade --install otel-collector deploy/helm/observability/otel-collector -n observability-hub -f values-otel.yaml --wait --timeout 10m
helm upgrade --install korrel8r-summarizer deploy/helm/observability/korrel8r -n openshift-cluster-observability-operator --create-namespace -f values-korrel8r.yaml --wait --timeout 10m
# Use the actual trace-reader role created by the reviewed Tempo chart.
oc adm policy add-cluster-role-to-user '<actual-tempo-tenant-read-role>' "system:serviceaccount:$NS:mcp-analyzer"
```

Set `route.enabled=false` in Korrel8r values unless authenticated external access is intended. Validate its log/trace store URLs and tenant settings against the actual gateways. Check the selected forwarder's collector SA permissions and its namespace filters before collecting. Render/dry-run with the same release names, namespaces and values used in these commands.

Replace `admin/minio123` defaults and `1x.demo` Loki sizing before a persistent deployment; set storage class, PVC capacity, retention, access, replication and requests from actual load. The full telemetry stack needs materially more resources than just MCP + plugin; calculate rendered requests/PVCs rather than treating the README's 4 CPU / 8 GiB as total cluster capacity. Disable sample RAG document downloads by skipping RAG entirely.

In the stock Tempo RBAC template, `system:authenticated` receives trace-read access. Replace that subject with the explicit MCP/collector/Korrel8r SAs and authorized groups before deploying in a shared cluster. Enable only required log sources/namespaces; do not duplicate a cluster-wide log forwarder. Auto-instrument only explicitly selected workloads; do not annotate entire unrelated namespaces or restart their pods.

Pass acceptance only when a known application log and trace are ingested, queryable by the intended tenant identity, shown through the application, and linked by Korrel8r. Installing the summarizer does not generate application traces; instrument the actual observed workloads separately.

## 8. Keep the inference endpoint separate from observed data

Use MaaS only to explain platform observations. Do not add GPU operators/exporters, vLLM scraping, remote model telemetry federation or accelerator credentials to this profile. The model endpoint and its API key are inference dependencies; local Thanos supplies the OpenShift CPU, memory, pods, storage, network and control-plane signals. Preserve the supplied endpoint configuration and defer its key as requested. If additional ordinary workload metrics are required, use supported UWM ServiceMonitor/PodMonitor targets and real port/TLS/auth settings rather than treating an inference Route as a metrics endpoint.

## 9. Access controls, acceptance, troubleshooting, and rollback

Disable the public MCP Route and restrict TCP 8085 ingress to the Console namespace using a NetworkPolicy, adding only explicitly required clients. Verify namespace labels and the Console deployment's actual network path. A suitable starting selector is `kubernetes.io/metadata.name: openshift-console`; verify it on this cluster. Permit backend egress to DNS, Kubernetes API, Thanos, model endpoints, and configured telemetry. NetworkPolicy is transport control, not per-user authorization.

For the default application labels, apply a reviewed namespace-scoped MCP ingress policy such as:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: aiobs-mcp-from-console
  # Apply with oc -n "$NS"; no fixed namespace in this manifest.
spec:
  podSelector:
    matchLabels:
      app: aiobs-mcp-server
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: openshift-console
      ports:
        - protocol: TCP
          port: 8085
```

Review other policies that select these pods: Kubernetes allowed ingress is additive. Test from permitted and unpermitted namespaces; do not assume this policy overrides an existing allow-all policy. Add separate ingress for other required MCP clients only after identifying them.

The Console proxy uses `authorization: None`; backend tools use the privileged shared SA and can change application model/credential configuration. This baseline is a controlled administrator pilot. For production with differently privileged users, implement backend authentication and Kubernetes authorization checks, or an authenticated authorization proxy; forward/validate user identity and enforce namespace/tenant access per request. Merely setting `authorization: UserToken`, hiding a menu, or removing the Route does not implement that authorization. Test unauthenticated requests, ordinary users, cross-namespace access and settings writes before broad access.

Require these evidence items in the execution report:

1. Exact OpenShift version and recorded absence of OpenShift AI, audited commit, image tags/digests, architecture and applied diff.
2. Successful Helm lint/render and server validation; ready application deployments and correct enabled plugin name.
3. Before the key is added: record AI features as pending, never passed. After adding it: verify endpoint/model ID, transport, authentication, chat completion, tool calling, summary and export success; confirm the gpt-oss serving runtime supports the required chat/tool behavior.
4. Real CPU, memory and pod metrics confirmed via MCP's own identity; no vLLM/GPU pages, cards, metric categories, suggestions, tools or accelerator discovery calls, including on a GPU-equipped cluster.
5. Separate pass/unavailable results for logs, traces and correlation; do not call missing signals healthy.
6. Explicit access-control scope, negative authorization results, and any remaining production blockers.

| Symptom | Check / fix |
|---|---|
| Missing KServe/OGX/DSC APIs | Expected in this profile; install only the two application charts, not the RAG/operator stack |
| URL contains `/v1:8080/v1` | Preserve the full supplied base URL; bypass Makefile URL processing |
| Wrong provider or “RAG unavailable” | Use `maas/gpt-oss-20b`, external true, full apiUrl and per-model MaaS Secret field |
| `404` on model calls | Compare served model ID; check doubled/missing `/v1` and completion suffix |
| `401/403` model calls | Verify API key/subscription expiry and endpoint auth; distinguish model credentials from SA tokens |
| CA verification failure | Verify full bundle, SAN/hostname, Requests + SDK CA settings; avoid `-k` as a deployment fix |
| Dashboard cards show em dashes or no metrics | Confirm MCP TLS is enabled, the Service has the OpenShift serving-certificate annotation and its Secret is populated; check Console/MCP logs for TLS handshake errors and verify proxied `/config` and `/mcp` requests |
| Prometheus `403` | Check `mcp-analyzer` role bindings and actual API authorization, not only pod readiness |
| Old vLLM/GPU content remains | Verify both custom image digests, applied source patch, scoped catalog and browser asset refresh; do not enable accelerator exporters |
| Trace `403` | Bind the actual tenant read role and match gateway tenant path |
| Plugin absent | Check plugin manifest fetch, serving certificate, enabled name, browser console and proxy path |
| Existing resource Helm ownership error | Inspect existing owner and reuse it; do not forcibly annotate it for Helm adoption |
| `ImagePullBackOff` | Check per-component tag spelling, architecture, mirror and pull secret |

Before upgrading an existing application release, save `helm get values -a` and `helm history` for its actual names. Roll back only owned application releases with `helm rollback ... --wait`. For a new install, remove only this plugin's entry from the existing Console plugin list, then `helm uninstall aiobs-console-plugin` and `helm uninstall aiobs-mcp-server` in the application namespace. Keep the namespace until its configuration and credentials are intentionally retired. Do not call blanket `make uninstall ... UNINSTALL_OBSERVABILITY=true`, delete shared operators/storage, restore an old whole monitoring ConfigMap over newer changes, or delete unrelated Jobs. Helm does not reverse external effects of the Console registration hook; verify the final plugin list explicitly.

## Sources

Read the pinned repository paths for code claims and live product documentation for platform claims:

- [Audited source tree](https://github.com/rh-ai-quickstart/ai-observability-summarizer/tree/048f784a19d049fdf6881d8b8e32c870921d48ea): `Makefile`; `deploy/helm/{rag,aiobs-stack,mcp-server,openshift-console-plugin}`; `scripts/enable-user-workload-monitoring.sh`; `src/core/{config,api_key_manager,model_config_manager,llm_client}.py`; `src/chatbots/{factory,openai_bot,base}.py`; `src/mcp_server/tools/model_config_tools.py`.
- [OpenShift 4.18 ConsolePlugin API](https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/console_apis/consoleplugin-console-openshift-io-v1).
- [OpenShift 4.18 accessing metrics](https://docs.redhat.com/en/documentation/monitoring_stack_for_red_hat_openshift/4.18/html-single/accessing_metrics/): built-in monitoring role and query API.
- [OpenShift 4.18 user workload monitoring](https://docs.redhat.com/en/documentation/monitoring_stack_for_red_hat_openshift/4.18/html/configuring_user_workload_monitoring/preparing-to-configure-the-monitoring-stack-uwm).
