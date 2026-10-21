# OpenShift 4.20 / OpenShift AI 3.5 MaaS profile

This profile deploys the MCP server and Console plugin into `ai-observability`, using the existing MaaS `gpt-oss-20b` model. The live cluster was confirmed as OpenShift 4.20.39 and OpenShift AI 3.5.1. The MaaSModelRef in `llm` reported `Ready=True`, a healthy runtime, and the alias `publishers/llm/models/gpt-oss-20b`.

The MaaS gateway publishes an HTTPS endpoint, and its certificate validated from the deployment workstation. The API key was configured in the namespace-scoped `ai-maas-credentials` Secret and is not stored in this repository. In the 2026-10-21 deployment, GPT-OSS inference, metrics-backed summaries, and report export worked. Generic MCP function calling did not return tool calls and remains unresolved; no MaaS serving-side configuration was changed. The profile keeps `requiresApiKey: true` and references the Secret field without including its value.

For the detailed compatibility audit, deployment workflow, security boundaries, and acceptance checks, see the [deployment skill](../../../skills/deploy-aiobs-ocp420-rhoai35/SKILL.md).

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

## MaaS credential management and acceptance

The live deployment stores the MaaS key in the namespace-scoped `ai-maas-credentials` Secret under the `gpt-oss-20b` field. For a new installation or key rotation, use **AI Observability → Settings** or a secure CLI flow; never put the value in a values file, shell history, or Git. The configured HTTPS endpoint preserves TLS verification.

The current validation record is limited: inference, metrics-backed summary, and report export worked; generic MCP function calling did not return tool calls. Confirm the served ID using `/v1/models`, then validate completion, function calling, summary, and export separately after any runtime or application change.
