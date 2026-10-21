# OpenShift 4.20 / OpenShift AI 3.5 MaaS profile

This profile deploys the MCP server and Console plugin into `ai-observability`, using the existing MaaS `gpt-oss-20b` model. The live cluster was confirmed as OpenShift 4.20.39 and OpenShift AI 3.5.1. The MaaSModelRef in `llm` reported `Ready=True`, a healthy runtime, and the alias `publishers/llm/models/gpt-oss-20b`.

The MaaS gateway publishes an HTTPS endpoint. Its TLS certificate validated from the deployment workstation, and an unauthenticated request to the model's `/v1/models` path returned HTTP 401. The exact served API model ID and inference/tool behavior remain pending an API key. The model profile is already seeded with `requiresApiKey: true`; no key or placeholder Secret is included here.

The profile enables cluster metrics through Thanos and disables the chart's optional Grafana, Loki, and Korrel8r permissions. Logs, traces, and correlation remain disabled until their existing gateways and tenant authorization are identified. The MCP Route is disabled. The NetworkPolicy allows ingress to the MCP pod from the `openshift-console` namespace. The Console proxy uses the chart's shared service identity, so limit use to an administrator pilot until per-user authorization is implemented.

## Deployment

Run from the repository root after logging in to the intended cluster. The commands below keep the MaaS credential out of the repository and preserve model configuration on repeat deployments.

```bash
export NS=ai-observability
export PROFILE=deploy/profiles/ocp420-rhoai351-maas
export PUBLIC_CA_FILE="$(python3 -c 'import certifi; print(certifi.where())')"
test "$(oc whoami --show-server)" = "https://api.cluster-v4vsc.v4vsc.sandbox287.opentlc.com:6443"
test "$(oc get clusterversion version -o jsonpath='{.status.desired.version}')" = "4.20.39"

oc get namespace "$NS" >/dev/null 2>&1 || oc create namespace "$NS"
if ! oc -n "$NS" get configmap ai-model-config >/dev/null 2>&1; then
  oc -n "$NS" create configmap ai-model-config \
    --from-file=model-config.json="$PROFILE/model-config.json" \
    --dry-run=client -o yaml | oc apply -f -
fi
if ! oc -n "$NS" get configmap aiobs-service-ca >/dev/null 2>&1; then
  oc apply -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: aiobs-service-ca
  namespace: ai-observability
data: {}
YAML
fi
oc -n "$NS" annotate configmap aiobs-service-ca \
  service.beta.openshift.io/inject-cabundle=true --overwrite

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
for attempt in $(seq 1 30); do
  oc -n "$NS" get configmap aiobs-service-ca \
    -o jsonpath='{.data.service-ca\.crt}' > "$workdir/service-ca.pem"
  test -s "$workdir/service-ca.pem" && break
  sleep 2
done
test -s "$workdir/service-ca.pem"
cat "$PUBLIC_CA_FILE" "$workdir/service-ca.pem" > "$workdir/ca-bundle.pem"
oc -n "$NS" create configmap aiobs-mcp-server-trusted-ca-bundle \
  --from-file=service-ca.crt="$workdir/ca-bundle.pem" \
  --dry-run=client -o yaml | oc apply -f -
oc -n "$NS" annotate configmap aiobs-mcp-server-trusted-ca-bundle \
  service.beta.openshift.io/inject-cabundle- --overwrite 2>/dev/null || true

# Restrict ingress before the MCP Deployment starts.
oc -n "$NS" apply -f "$PROFILE/networkpolicy-mcp.yaml"

helm lint deploy/helm/mcp-server
helm lint deploy/helm/openshift-console-plugin
helm template mcp-server deploy/helm/mcp-server -n "$NS" \
  -f "$PROFILE/values-mcp.yaml" > "$workdir/rendered-mcp.yaml"
helm template aiobs-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  -f "$PROFILE/values-console-plugin.yaml" > "$workdir/rendered-plugin.yaml"
oc apply --dry-run=server -f "$workdir/rendered-mcp.yaml"
oc apply --dry-run=server -f "$workdir/rendered-plugin.yaml"

helm upgrade --install mcp-server deploy/helm/mcp-server -n "$NS" \
  -f "$PROFILE/values-mcp.yaml" --atomic --wait --timeout 10m
helm upgrade --install aiobs-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  -f "$PROFILE/values-console-plugin.yaml" --atomic --wait --timeout 10m
oc -n "$NS" rollout status deployment/aiobs-mcp-server --timeout=5m
oc -n "$NS" rollout status deployment/aiobs-console-plugin --timeout=5m
oc get consoleplugin aiobs-console-plugin
oc get console.operator.openshift.io cluster -o jsonpath='{.spec.plugins}{"\\n"}'
```

User workload monitoring is already enabled on the target cluster, and the model namespace has existing ServiceMonitor/PodMonitor resources. The profile does not change shared monitoring configuration.

## Add the API key later

Use **AI Observability → Settings** to add the MaaS key for `gpt-oss-20b`. The server stores it in the namespace-scoped `ai-maas-credentials` Secret under the `gpt-oss-20b` key. If using a CLI, read the key without echo and pass it directly to a Kubernetes Secret creation/patch command; never put it in a values file, shell history, or Git. The HTTPS endpoint is configured so the key travels over TLS.

After adding the key, verify `/v1/models`, a chat completion, tool calling, a summary, and report export. Until those checks pass, treat AI inference features as pending. Confirm the actual served model ID from `/v1/models`; update both `modelName` and the selected UI model if it differs from `gpt-oss-20b`.
