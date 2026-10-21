---
name: deploy-aiobs-ocp420-rhoai35
description: Audit, modify, and deploy rh-ai-quickstart/ai-observability-summarizer on OpenShift 4.20.39 with Red Hat OpenShift AI 3.5.1. Use for compatibility assessment, authenticated vLLM or MaaS integration, OGX migration decisions, Helm deployment, and metrics/logs/traces validation.
---

# Deploy AI Observability Summarizer on OpenShift 4.20 / OpenShift AI 3.5

## Scope and evidence

Target OpenShift **4.20.39** and OpenShift AI **3.5.1**. Audit date: **2026-10-21**.
Use upstream commit **048f784a19d049fdf6881d8b8e32c870921d48ea** (2026-06-09).
Re-audit a different commit before applying the embedded patch.

Treat this as a reusable source audit and deployment workflow, not a live cluster inventory or certification. The original source audit was pinned to upstream commit `048f784a19d049fdf6881d8b8e32c870921d48ea`; recheck installed CSV versions, CRD schemas, image availability, architecture, endpoint access, and monitoring on every target cluster. This repository already contains the compatibility changes and the checked-in deployment profile at [deploy/profiles/ocp420-rhoai351-maas](../../deploy/profiles/ocp420-rhoai351-maas/README.md). Use those files as the concrete deployment reference; do not rerun the one-time patch against this repository.

**Deployment record (2026-10-21):** the MaaS credential was configured in the cluster Secret and is intentionally absent from Git. GPT-OSS inference, metrics-backed summary, and report export were verified. Generic MCP function calling did not return tool calls, so that path remains unresolved. No MaaS serving-side configuration was changed. Recheck these results before relying on them in another deployment.

Use the supplied **MaaS endpoint through the OpenAI-compatible API** as the default: run MCP plus the Console plugin and reuse an existing OpenShift AI vLLM/MaaS endpoint. Here `external: true` means external to this application's bundled Llama Stack; the model can remain entirely inside the cluster. This profile needs no extra model GPU, Llama Stack, OGX server, pgvector, or sample RAG documents.

Configure the reference profile for `https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b`. The OpenAI-compatible base is the model resource URL plus `/v1`; the initial served model ID is `gpt-oss-20b`. Confirm both against the live endpoint. Store the MaaS key only in the namespace-scoped `ai-maas-credentials` Secret (or through the application's Settings UI); never place it in this skill, values files, shell history, or Git. If a key is not available, deploy UI/metrics and mark AI checks pending. The checked-in profile uses HTTPS and certificate validation; do not downgrade it to HTTP or disable TLS verification.

Do not blindly run `make install`, install the quickstart Operator alongside Helm, or upgrade the existing OpenShift AI installation. The requested versions meet the published platform version range; application compatibility still needs validation. The current support matrix lists OpenShift 4.20 for OpenShift AI 3.5 and replaces the Llama Stack Operator with OGX. Apply the patch to a working branch; review `git diff` before cluster changes.

## Modification decisions

| Area / source | Audited behavior | Required action for this target |
|---|---|---|
| `Makefile`: RHOAI detection | Treats all 3.x installations as the old Llama Stack generation | Set `RHOAI_VERSION=3` explicitly for any make target; choose an endpoint profile, not inferred Llama Stack compatibility |
| `Makefile`, RAG dependency `llama-stack` 0.8.6 | Checks `llamastackdistributions.llamastack.io` and patches `llamastackoperator` | Skip bundled RAG. For a native 3.5 server use OGX, as described below; the old CR cannot be fixed by a version flag |
| `Makefile:process_llm_url` | Appends `:8080/v1` to a URL without an explicit numeric port, corrupting HTTPS Routes | Preserve the supplied complete base URL; patch below. Supply `/v1` exactly once |
| MCP `values.yaml` versus `templates/deployment.yaml` | `env.MAAS_API_URL` exists in values but is not emitted into the pod | Patch template to emit it; add `extraEnv` for CA/client configuration |
| `src/chatbots/factory.py` | Routes chat by model ID prefix, not only config `provider` | Use config key `maas/gpt-oss-20b` for this profile; the `maas/` factory rule selects the OpenAI-compatible SDK path before other provider heuristics |
| `model_config_manager.py`, `api_key_manager.py` | Runtime config is `ai-model-config`; MaaS per-model keys are in `ai-maas-credentials` | Seed the ConfigMap now; add the `gpt-oss-20b` Secret field later; do not rely only on `MODEL_CONFIG`, `LLM_URL`, or `LLM_API_TOKEN` |
| MCP `llm.url` value | Sets `LLAMA_STACK_URL`, not a general vLLM endpoint | Leave it unset in the direct profile; use model `apiUrl` instead |
| Console plugin registration | Chart creates `aiobs-console-plugin`; make recipe additionally enables `openshift-ai-observability` | Fix make name; use individual Helm chart and its correct registration hook |
| MCP Route / Console proxy | MCP Route enabled by default; Console proxy uses `authorization: None` and backend uses shared SA | Set `route.enabled=false`; restrict ingress. Treat this as a shared-identity pilot until user authorization is implemented |
| CA volume | Injected service CA replaces the public CA bundle location | Supply a combined public + service + private CA bundle, disable chart ownership of its ConfigMap, set both Requests and OpenAI SDK CA environment variables |
| UWM installer script | Can apply its own `cluster-monitoring-config` and changes user Alertmanager | Merge only `enableUserWorkload` into existing YAML; preserve other settings. User Alertmanager is unnecessary for this profile |
| `aiobs-stack/Chart.yaml` | Infrastructure dependencies lack `condition` gates | Use individual charts. `loki.enabled=false` or `tempo.enabled=false` does not disable these umbrella dependencies |
| Image tags | MCP defaults to `v3.2.0`; plugin values use `3.2.0`; make uses `v3.2.0` | Verify each exact image independently; do not normalize all tags by guessing. Pin approved immutable tags or add digest support |
| Metrics | GPU/vLLM discovery exists; some fixed UI queries and prompts still assume names | Query live series and labels first; change only demonstrated mismatches. Do not globally replace `vllm:` names |

## 1. Collect inputs and preflight

Collect application namespace, CPU architecture, existing telemetry topology, model base URL ending `/v1`, exact `/v1/models` ID, authentication method and secure credential source, CA chain, and approved image references. For a new model, deploy through OpenShift AI 3.5.1 using a supported serving runtime and hardware, configure authentication, and verify readiness first. Use a model/runtime with working OpenAI tool calling for chat. Do not copy the quickstart's old serving runtime or require Serverless/Service Mesh for every serving mode; choose the supported deployment mode for the actual model.

Require `oc` logged into the intended cluster, Helm 3, Git, Python 3 with PyYAML, `jq`, Mike Farah `yq` v4, and `curl`. Require cluster-admin or delegated equivalent only for namespace/cluster RBAC, monitoring configuration, ConsolePlugin, and operator installation. Do not grant cluster-admin to the application SA. For source image builds also require Podman/Docker; front-end builds require the repository's Node 24 / Yarn toolchain. Chart-only changes below reuse existing application images.

```bash
oc whoami
oc whoami --show-server
oc get clusterversion version -o jsonpath='{.status.desired.version}{"\n"}'
oc get co
oc get csv -A -o json | jq -r '.items[] | select(.metadata.name|test("rhods-operator")) | [.metadata.namespace,.metadata.name,.spec.version,.status.phase]|@tsv'
oc get datasciencecluster -o yaml
oc get crd ogxservers.ogx.io
oc get crd inferenceservices.serving.kserve.io
oc get nodes -L kubernetes.io/arch
oc get storageclass
oc get consoleplugin
helm list -A
oc get subscription -A
oc get servicemonitor,podmonitor -A
oc get crd | rg 'lokistacks|tempostacks|opentelemetrycollectors|clusterlogforwarders|uiplugins'
```

Record absence of optional APIs without treating it as a metrics-only blocker. Stop on unhealthy required operators. Confirm exactly 4.20.39 / 3.5.1 before claiming validation for those patch versions. Do not infer compatibility from a 3.x CSV substring alone. The upstream image build defaults to amd64; inspect multi-architecture manifests or build matching images on other architectures.

Choose **A: existing monitoring and model endpoint** by default; enable logs/traces only when configured. Choose **B: isolated full-telemetry pilot** only if dedicated infrastructure is needed. Never take ownership of existing Helm releases, shared Loki/Tempo stores, object storage, or installed operator subscriptions merely to satisfy the quickstart defaults.

## 2. Pin the source and apply the compatibility patch only when needed

```bash
git clone https://github.com/rh-ai-quickstart/ai-observability-summarizer.git
cd ai-observability-summarizer
git checkout 048f784a19d049fdf6881d8b8e32c870921d48ea
git switch -c compat/ocp420-rhoai351
```

Save the following as `patch_source.py` outside the repository, run `python3 /path/to/patch_source.py .` once, and review the diff. The patch checks the commit and every source anchor before writing. Do not rerun it on an already patched tree; keep or revert the reviewed changes explicitly.

```python
#!/usr/bin/env python3
"""Apply narrowly scoped compatibility changes to the audited upstream commit."""
import argparse
import subprocess
from pathlib import Path

PIN = "048f784a19d049fdf6881d8b8e32c870921d48ea"


def patch(root):
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if head != PIN:
        raise SystemExit(f"Re-audit required: expected {PIN}, got {head}")
    replacements = []

    def replace(relative, old, new):
        path = root / relative
        text = next((after for p, before, after in reversed(replacements) if p == path), path.read_text())
        if text.count(old) != 1:
            raise SystemExit(f"Expected one source anchor in {relative}; found {text.count(old)}")
        replacements.append((path, text, text.replace(old, new, 1)))

    make = (root / "Makefile").read_text()
    start = make.index("# Process LLM_URL to add default port")
    end = make.index("helm_llama_stack_args =", start)
    replace("Makefile", make[start:end], "# Preserve the complete operator-supplied base URL, including scheme and port.\n# Supply /v1 explicitly; never append :8080 to an HTTPS Route.\ndefine process_llm_url\n$(patsubst %/,%,$(LLM_URL))\nendef\n\n")
    # Only change the install/uninstall recipes; retain source identity elsewhere.
    make = replacements[-1][2]
    start = make.index(".PHONY: install-console-plugin")
    end = make.index(".PHONY: install-react-ui", start)
    replace("Makefile", make[start:end], make[start:end].replace("openshift-ai-observability", "aiobs-console-plugin"))

    deploy = "deploy/helm/mcp-server/templates/deployment.yaml"
    replace(deploy, "          env:\n", """          env:
            - name: MAAS_API_URL
              value: {{ .Values.env.MAAS_API_URL | quote }}
            {{- with .Values.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
""")
    replace(deploy, "      restartPolicy: Always\n", """      restartPolicy: Always
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
""")
    replace(deploy, "        - name: mcp-server\n", """        - name: mcp-server
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
""")
    ca = "deploy/helm/mcp-server/templates/configmap-ca.yaml"
    original = (root / ca).read_text()
    replace(ca, original, '{{- if ne .Values.trustedCA.create false }}\n' + original + '\n{{- end }}\n')
    # Write only after every source anchor has been checked.
    for path, before, after in replacements:
        path.write_text(after)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    args = parser.parse_args()
    patch(args.repository.resolve())
```

```bash
git diff --check
git diff --stat
helm lint deploy/helm/mcp-server
helm lint deploy/helm/openshift-console-plugin
```

Do not automatically rewrite Llama Stack CRs to OGX CRs. This patch supports bypassing that bundled dependency, not migrating it.

## 3. Configure the MaaS profile and store its key separately

Use a dedicated namespace. Export these non-secret inputs from the repository root. Preserve the model-specific `/llm/gpt-oss-20b` path; do not point the client at the host root or append `:8080`.

| Setting | Selected value |
|---|---|
| Supplied model resource URL | `https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b` |
| Assumed OpenAI base URL | `https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b/v1` |
| Assumed completion URL | `https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b/v1/chat/completions` |
| Initial served model ID / UI selection | `gpt-oss-20b` / `maas/gpt-oss-20b` |
| Credential storage | Secret `ai-maas-credentials`, field `gpt-oss-20b` |

```bash
export NS=ai-observability
export MAAS_RESOURCE_URL=https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b
export MODEL_BASE_URL="${MAAS_RESOURCE_URL%/}/v1"
export MODEL_ID=gpt-oss-20b
export MODEL_SECRET_FIELD=gpt-oss-20b
export MCP_REPOSITORY=quay.io/ecosystem-appeng/aiobs-mcp-server
export MCP_TAG=v3.2.0
export PLUGIN_REPOSITORY=quay.io/ecosystem-appeng/aiobs-console-plugin
export PLUGIN_TAG=3.2.0
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
    '_metadata': {'source': 'user', 'description': 'GPT-OSS-20B MaaS; credential stored separately in a Kubernetes Secret'}
}}
with open('model-config.maas.json', 'w') as f:
    json.dump(config, f, indent=2)
PYCODE
oc -n "$NS" create configmap ai-model-config \
  --from-file=model-config.json=model-config.maas.json --dry-run=client -o yaml | oc apply -f -
```

For a new namespace, run the seed command above. On upgrades export and merge the existing `ai-model-config` so other models are preserved. Verify both application images and `PLUGIN_PATCHER_IMAGE` are pullable, inspect their digests, and use approved mirrored references where needed. The hook image must provide `oc`, `jq`, and Bash. Keep pull secrets scoped to workload SAs.

Keep `requiresApiKey: true` while credentials are pending. The `maas/` prefix controls provider routing; it is stripped before sending the actual served model ID to the endpoint. This profile uses per-model MaaS credentials, not `ai-openai-credentials`, `LLM_API_TOKEN`, an HF token, or the OpenShift console login token. Setting `env.MAAS_API_URL` alone does not supply credentials or replace the model ConfigMap's `apiUrl`.

The MaaS endpoint uses **HTTPS**. Preserve certificate validation for MaaS and internal OpenShift services; do not silently downgrade to HTTP or disable TLS verification. Configure the combined trust bundle described below for both the MaaS endpoint and internal monitoring.

### Configure or rotate the real API key

If the key is not yet configured, add it through AI Observability → Settings → Add Model, choose **Model as a Service (MaaS)**, set model ID `gpt-oss-20b` (without `maas/`), enter the full HTTPS base URL above, and store the key. For a CLI-managed credential, set the non-secret exports above and run without shell tracing; this preserves other model keys in an existing Secret:

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

Enable UWM only when missing. Back up `openshift-monitoring/cluster-monitoring-config`, extract `.data["config.yaml"]`, change only `.enableUserWorkload = true` with yq, and patch that string back; preserve resourceVersion or re-read and compare immediately before patching. If absent, create the ConfigMap. Do not apply the quickstart's replacement ConfigMap to a configured cluster. Do not enable user Alertmanager unless alerting is in scope.

```bash
oc -n openshift-monitoring get cm cluster-monitoring-config -o yaml > cluster-monitoring-config.before.yaml
oc -n openshift-monitoring get cm cluster-monitoring-config -o jsonpath='{.data.config\.yaml}' > monitoring-current.yaml
yq eval '.enableUserWorkload = true' monitoring-current.yaml > monitoring-updated.yaml
jq -n --rawfile config monitoring-updated.yaml '{data:{"config.yaml":$config}}' > monitoring-patch.json
# Re-read and compare current config to the backup before applying on a shared cluster.
oc -n openshift-monitoring patch cm cluster-monitoring-config --type=merge --patch-file monitoring-patch.json
oc -n openshift-user-workload-monitoring get pods
```

Use that snippet only when the ConfigMap already exists and a monitoring change is needed. For an absent ConfigMap use `oc create configmap ... --from-file=config.yaml=monitoring-updated.yaml`, where the file contains `enableUserWorkload: true`. Enable scraping of the **model workload namespace**, not only the application namespace. Inspect its namespace monitoring labels and any existing ServiceMonitor/PodMonitor; do not create duplicate scrapers.

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

Create `values-mcp-420.yaml`:

```yaml
env:
  DEV_MODE: "false"
  MAAS_API_URL: "https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b/v1"
  PROMETHEUS_URL: "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091"
  LLM_TIMEOUT_SECONDS: 180
route:
  enabled: false
trustedCA:
  enabled: true
  create: false
extraEnv:
  - name: REQUESTS_CA_BUNDLE
    value: /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
  - name: SSL_CERT_FILE
    value: /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
```

Merge actual `env.TEMPO_URL`, `env.TEMPO_TENANT_ID`, `env.KORREL8R_URL` and `env.MAAS_API_URL` if used. Do not point these at guessed service names. The application can start with only metrics; label missing logs/traces as unavailable until their acceptance tests pass. Do not set `VERIFY_SSL=false`. Avoid duplicating built-in env names in `extraEnv`.

Review cluster-scoped RBAC in `deploy/helm/mcp-server/templates/crb.yaml` before install. The chart grants cluster `view`, `cluster-monitoring-view`, and broader Prometheus permissions; it also references `korrel8r-view` and a quickstart-specific Loki role. A binding to an absent role can exist while access still fails. For a new dedicated installation the default `createGrafanaRole=true` creates the fixed-name `grafana-prometheus-reader`; if it already exists, set that value false and inspect its rules/ownership instead of adopting it. Narrow or condition optional bindings in a site overlay as required. Bind actual Loki tenant-read and Tempo tenant-read roles to `mcp-analyzer` when enabling those features.

```bash
helm template mcp-server deploy/helm/mcp-server -n "$NS" \
  -f values-mcp-420.yaml --set-string image.repository="$MCP_REPOSITORY" \
  --set-string image.tag="$MCP_TAG" > rendered-mcp.yaml
helm template aiobs-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  --set-string plugin.image.repository="$PLUGIN_REPOSITORY" \
  --set-string plugin.image.tag="$PLUGIN_TAG" \
  --set-string plugin.jobs.patchConsoles.image="$PLUGIN_PATCHER_IMAGE" > rendered-plugin.yaml
oc apply --dry-run=server -f rendered-mcp.yaml
oc apply --dry-run=server -f rendered-plugin.yaml
helm upgrade --install mcp-server deploy/helm/mcp-server -n "$NS" \
  -f values-mcp-420.yaml --set-string image.repository="$MCP_REPOSITORY" \
  --set-string image.tag="$MCP_TAG" --atomic --wait --timeout 10m
helm upgrade --install aiobs-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  --set-string plugin.image.repository="$PLUGIN_REPOSITORY" \
  --set-string plugin.image.tag="$PLUGIN_TAG" \
  --set-string plugin.jobs.patchConsoles.image="$PLUGIN_PATCHER_IMAGE" --atomic --wait --timeout 10m
oc -n "$NS" rollout status deployment/aiobs-mcp-server --timeout=5m
oc -n "$NS" rollout status deployment/aiobs-console-plugin --timeout=5m
oc get consoleplugin aiobs-console-plugin -o yaml
oc get console.operator.openshift.io cluster -o jsonpath='{.spec.plugins}{"\n"}'
```

Retain the chart's post-install registration hook, which merges `aiobs-console-plugin` into the existing plugin list. Do not replace the entire list or register `openshift-ai-observability`. If a stale name was added by an earlier quickstart install, remove only that confirmed stale entry. Refresh the console browser after the plugin is enabled.

The MCP SA mounts a manually created service-account-token Secret. This remains functional on OpenShift 4.20 when populated by the token controller; verify it is populated without printing it. For production rotation, migrate to projected tokens with the appropriate API audience and **retain** API `ca.crt`, service CA and namespace files; update clients that cache the token at process start. Replacing the volume alone does not complete token rotation.

## 6. Validate from the MCP pod

When the API key is not configured, validate pod readiness, Console plugin loading, monitoring access and real metric series; mark inference/chat/summary checks as pending. Run the following combined model and Prometheus test only after the real key is configured, using the same trust and credentials as the application. Run the following through `oc -n "$NS" exec -i deploy/aiobs-mcp-server -- python -`; it prints no credentials:

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

Some MaaS gateways intentionally omit `/models`; use its documented discovery method and record that exception, then prove completion and tool calls using the selected model ID. A successful completion alone does not prove tool calling. Check prompt plus generation tokens against the served context limit; summaries default to up to 6000 generated tokens in `src/core/llm_client.py`. If the serving limit is lower, make that limit configurable or reduce it in source, rebuild the MCP image, and retest a realistic summary. In the UI select `maas/gpt-oss-20b` (or the confirmed served ID with the `maas/` prefix), ask for CPU usage in a known namespace, and require actual metric discovery, PromQL execution, and a grounded answer. Also test vLLM summary and report export separately.

Use Prometheus queries to discover actual runtime series, for example `{__name__=~"vllm:.*|vllm_.*"}` and `{__name__=~"DCGM_.*|dcgm_.*"}`. Select representative counters/histograms and inspect labels (`namespace`, `pod`, model identifiers). Generate inference traffic before evaluating a rate. An empty result is missing telemetry, not zero activity.

If a ServiceMonitor is absent, discover service labels and its **named metrics port**, metrics path, TLS, and authentication first; create a `monitoring.coreos.com/v1` monitor in the model namespace with the matching selector and port name. For a TLS/authenticated metrics endpoint include the required CA and Secret references. Do not assume the inference Route's key also authenticates `/metrics`, or that monitoring scrapes an arbitrary Route. Confirm the target is up in UWM and the same series appears through Thanos. Inspect deployed llm-d services/PodMonitors when the workload uses distributed inference instead of assuming one InferenceService predictor.

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

Render and server-dry-run **each** individual chart before applying. Create object storage and bucket credentials first; then Tempo/Loki; then collectors and Korrel8r. Use existing production S3-compatible storage if available. For a disposable pilot, review `make install-minio`, `make install-observability`, and `make install-korrel8r` with `RHOAI_VERSION=3 USE_LLAMA_STACK_OPERATOR=false` and their namespace variables; do not run them unchanged against shared infrastructure. These helpers include cleanup/skip behavior and can retain old configuration.

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

Pass acceptance only when a known application log and trace are ingested, queryable by the intended tenant identity, shown through the application, and linked by Korrel8r. OpenShift AI installation does not automatically generate application traces or provide Loki/Tempo.

## 8. Optional native OGX integration

Use direct vLLM/MaaS for the baseline. When an OGX server is explicitly required, follow the 3.5 migration documentation and inspect `oc explain datasciencecluster.spec.components --recursive` and `oc explain ogxserver.spec --recursive` on the actual cluster. Do not treat a renamed Kind as the complete migration.

| Legacy assumption | OpenShift AI 3.5 migration target |
|---|---|
| `llamastack.io/v1alpha1`, `LlamaStackDistribution` | `ogx.io/v1beta1`, `OGXServer` |
| `llamastackdistributions.llamastack.io` | `ogxservers.ogx.io` |
| `spec.server.distribution` | `spec.distribution` |
| Flat replicas/storage/container environment | `spec.workload.replicas`, `.storage`, `.overrides.env` |
| `spec.network.exposeRoute` | `spec.network.externalAccess.enabled` |
| `spec.network.allowedFrom` | `spec.network.policy.ingress` rules |
| `.status.routeURL` | `.status.externalURL` |
| `LLS_PORT`, `LLAMA_STACK_CONFIG` | `OGX_PORT`, `OGX_CONFIG` |

Enable the OGX DSC component using the field served by the installed 3.5.1 schema; the upstream `llamastackoperator` patch is obsolete for this path. Provision the documented PostgreSQL dependency and server distribution configuration, authentication, CA bundle and namespace network access. Discover the created Service/Route instead of inventing `llamastack-service`. Verify `/v1/models`, `/v1/chat/completions` and required tool behavior with its authenticated identity. Configure the application using the same `openai/<id>` external profile against the OGX base URL. This avoids coupling to the application's old Llama Stack client/provider assumptions. Keep `rag.enabled=false` in any modified umbrella profile, or use individual charts as above. Do not delete legacy CRs/PVCs during an unverified migration.

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

1. Exact cluster/AI versions, audited commit, image tags/digests, architecture and applied diff.
2. Successful Helm lint/render and server validation; ready application deployments and correct enabled plugin name.
3. Before the key is added: record AI features as pending, never passed. After adding it: verify endpoint/model ID, transport, authentication, chat completion, tool calling, summary and export success; confirm the gpt-oss serving runtime supports the required chat/tool behavior.
4. A known OpenShift metric, live vLLM series and optional GPU series confirmed via MCP's own identity.
5. Separate pass/unavailable results for logs, traces and correlation; do not call missing signals healthy.
6. Explicit access-control scope, negative authorization results, and any remaining production blockers.

| Symptom | Check / fix |
|---|---|
| Missing LlamaStackDistribution API | Skip old RAG/operator flow; use direct model profile or migrate to OGX |
| URL contains `/v1:8080/v1` | Remove old URL processing; preserve the full HTTPS base |
| Wrong provider or “RAG unavailable” | Use `openai/<actual-model-id>` config key, external true, full apiUrl and provider Secret |
| `404` on model calls | Compare served model ID; check doubled/missing `/v1` and completion suffix |
| `401/403` model calls | Verify API key/subscription expiry and endpoint auth; distinguish model credentials from SA tokens |
| CA verification failure | Verify full bundle, SAN/hostname, Requests + SDK CA settings; avoid `-k` as a deployment fix |
| Prometheus `403` | Check `mcp-analyzer` role bindings and actual API authorization, not only pod readiness |
| vLLM/GPU empty | Check UWM targets, model namespace, metrics port/auth, accelerator exporter, traffic and actual metric names |
| Trace `403` | Bind the actual tenant read role and match gateway tenant path |
| Plugin absent | Check plugin manifest fetch, serving certificate, enabled name, browser console and proxy path |
| Existing resource Helm ownership error | Inspect existing owner and reuse it; do not forcibly annotate it for Helm adoption |
| `ImagePullBackOff` | Check per-component tag spelling, architecture, mirror and pull secret |

Before upgrading an existing application release, save `helm get values -a` and `helm history` for its actual names. Roll back only owned application releases with `helm rollback ... --wait`. For a new install, remove only this plugin's entry from the existing Console plugin list, then `helm uninstall aiobs-plugin` and `helm uninstall mcp-server` in the application namespace. Keep the namespace until its configuration and credentials are intentionally retired. Do not call blanket `make uninstall ... UNINSTALL_OBSERVABILITY=true`, delete shared operators/storage, restore an old whole monitoring ConfigMap over newer changes, or delete unrelated Jobs. Helm does not reverse external effects of the Console registration hook; verify the final plugin list explicitly.

## Sources

Read the pinned repository paths for code claims and live product documentation for platform claims:

- [Audited source tree](https://github.com/rh-ai-quickstart/ai-observability-summarizer/tree/048f784a19d049fdf6881d8b8e32c870921d48ea): `Makefile`; `deploy/helm/{rag,aiobs-stack,mcp-server,openshift-console-plugin}`; `scripts/enable-user-workload-monitoring.sh`; `src/core/{config,api_key_manager,model_config_manager,llm_client}.py`; `src/chatbots/{factory,openai_bot,base}.py`; `src/mcp_server/tools/model_config_tools.py`.
- [OpenShift AI supported configurations](https://access.redhat.com/articles/rhoai-supported-configs): OpenShift 4.20 support and OGX replacement in 3.5. Recheck the table for the selected architecture.
- [OpenShift AI 3.5.1 release notes](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/release_notes/index).
- [Llama Stack to OGX migration](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/working_with_ogx/llama-stack-to-ogx-migration_rag).
- [Working with OGX](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/working_with_ogx/index): deployment, PostgreSQL, authentication, TLS and network policy.
- [OpenShift 4.20 ConsolePlugin API](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/console_apis/consoleplugin-console-openshift-io-v1).
- [OpenShift 4.20 user workload monitoring](https://docs.redhat.com/en/documentation/monitoring_stack_for_red_hat_openshift/4.20/html/configuring_user_workload_monitoring/preparing-to-configure-the-monitoring-stack-uwm).
