# OpenShift 4.18 standalone MaaS profile

This profile records the standalone deployment of `rh-ai-quickstart/ai-observability-summarizer` on 2026-10-21 in namespace `ai-observability-standalone`. The upstream source is pinned to `048f784a19d049fdf6881d8b8e32c870921d48ea`; the chart changes and OpenShift-only UI/backend scope are in the companion patch at [the skill asset](../../../skills/deploy-aiobs-standalone-openshift/assets/aiobs-standalone-openshift-source.patch). Patch SHA-256: `4280b4fb9c358dedb3eb9f3e0304aead0437841377a2d79e53f7e6d72f3f844a`. Images were built for `amd64`.

The attached runbook described OpenShift 4.20.39, while the supplied cluster reported **OpenShift 4.18.56**. The recorded live validation applies to 4.18.56 only; rerun the preflight and schema checks before using this profile on another version. OpenShift AI, KServe and OGX APIs were absent as expected for this standalone profile.

## Live deployment record

- Helm releases `aiobs-mcp-server` and `aiobs-console-plugin` are `deployed`; both Deployments are 1/1 Ready.
- The MCP Service requests an OpenShift serving certificate in Secret `aiobs-mcp-server-tls`; MCP TLS is enabled so the Console service proxy can securely reach it.
- MCP image: `image-registry.openshift-image-registry.svc:5000/ai-observability-standalone/aiobs-mcp-server@sha256:c533b9fbb2315128aa24ca811c1e3e1c8fbc887aa5044347677327681ad6a71d`.
- Console plugin image: `image-registry.openshift-image-registry.svc:5000/ai-observability-standalone/aiobs-console-plugin@sha256:3141fe27a9ed65330431215c391199345f8cbd4f0630af72d46f9af26a740690`.
- The plugin manifest returned HTTP 200 from the plugin pod. The registration hook merged `aiobs-console-plugin` into the existing Console plugin list; the Console operator was Available and not Degraded.
- The cluster reported one unrelated operator degradation: `cloud-credential` had 1 of 7 credential requests failing to sync. The Console operator remained Available and not Degraded.
- The live MCP endpoint exposed 29 tools and none named for GPU, vLLM, Tempo or Korrel8r. Its catalog returned 18 categories and no GPU/accelerator category.
- A verified in-pod Thanos query returned HTTP 200, Prometheus `success`, and live `up` series (including 245 running pods at validation time). MCP startup completed catalog validation against Thanos.
- The first dashboard load had no metric values because the MCP backend was plain HTTP while the OpenShift Console service proxy connected over HTTPS. Enabling the chart's existing TLS support and the Service serving-certificate annotation fixed the proxy path. After refreshing `/observe/ai-observability`, Fleet Overview displayed live values (245 running pods, 5 nodes, CPU 6.57%, memory 24.24% at verification time); MCP logs showed successful console-proxy `/config` and `/mcp` requests.
- Image builds used OpenShift Binary BuildConfigs. Temporary BuildConfigs and Build objects were removed after the builds; ImageStreams remain because the Deployments use their images.

The configured MaaS URL is `http://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/llm/gpt-oss-20b`. Its model ID and API compatibility remain configuration assumptions: no MaaS request was sent and no MaaS Secret was created. The API key stays pending until a protected HTTPS endpoint or another protected transport is available. OpenShift metrics passed. MaaS-backed chat, summaries and exports remain unaccepted until protected transport and the API key are available.

The ConsolePlugin proxy uses `authorization: None`, so MCP requests share the `mcp-analyzer` service-account identity. That account has built-in `view` and `cluster-monitoring-view`; the MCP ingress NetworkPolicy allows the `openshift-console` namespace. Treat this as an administrator pilot, not a multi-user production authorization boundary.

## Build and deploy

The checked-in values pin the exact images in this cluster's internal registry and namespace. To reproduce in this cluster/namespace, first apply the source patch to the pinned upstream checkout and build the frontend and MCP image. For a different cluster or namespace, rebuild both images there and replace the profile image repositories/digests with the new immutable references.

The actual build used the cluster's binary builder, so no local Docker/Podman daemon was required. From the upstream checkout after applying the patch:

```bash
export NS=ai-observability-standalone
export PROFILE=/path/to/aiobserve/deploy/profiles/ocp418-standalone-maas
oc get namespace "$NS" >/dev/null 2>&1 || oc create namespace "$NS"

# Build the React console bundle using the repository lockfile.
cd openshift-plugin
CYPRESS_INSTALL_BINARY=0 yarn install --frozen-lockfile
yarn typecheck
yarn test --runInBand __tests__/pages/OpenShiftOnlyDashboard.test.tsx
yarn build:plugin
cd ..

# Apply the profile's Binary BuildConfigs and build the MCP image.
oc apply -f "$PROFILE/buildconfigs.yaml"
oc start-build aiobs-mcp-server -n "$NS" --from-dir=src --follow

# The plugin Dockerfile only needs dist/plugin, so avoid uploading node_modules.
workdir="$(mktemp -d)"
mkdir -p "$workdir/dist"
cp openshift-plugin/Dockerfile.plugin "$workdir/Dockerfile.plugin"
cp -R openshift-plugin/dist/plugin "$workdir/dist/plugin"
oc start-build aiobs-console-plugin -n "$NS" --from-dir="$workdir" --follow
rm -rf "$workdir"
```

The skill documents the cluster preflight, service-CA plus public-root bundle, server-side dry-runs, individual chart deployment, live validation, MaaS key handling, access limits, troubleshooting and rollback. Use the configured trust bundle name in `values-mcp.yaml`; do not commit a cluster CA bundle or credential. Keep `tls.enabled: true` for the MCP Service behind the Console proxy; the service-serving certificate is issued into the configured Secret.

After creating the namespace and the combined trusted CA ConfigMap described in the skill, deploy the charts:

```bash
oc create namespace "$NS" --dry-run=client -o yaml | oc apply -f -
oc -n "$NS" apply -f "$PROFILE/networkpolicy-mcp.yaml"
helm lint deploy/helm/mcp-server
helm lint deploy/helm/openshift-console-plugin
helm template aiobs-mcp-server deploy/helm/mcp-server -n "$NS" \
  -f "$PROFILE/values-mcp.yaml" > /tmp/aiobs-mcp-rendered.yaml
helm template aiobs-console-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  -f "$PROFILE/values-console-plugin.yaml" > /tmp/aiobs-plugin-rendered.yaml
oc apply --dry-run=server -f /tmp/aiobs-mcp-rendered.yaml
oc apply --dry-run=server -f /tmp/aiobs-plugin-rendered.yaml
helm upgrade --install aiobs-mcp-server deploy/helm/mcp-server -n "$NS" \
  -f "$PROFILE/values-mcp.yaml" --atomic --wait --timeout 10m
helm upgrade --install aiobs-console-plugin deploy/helm/openshift-console-plugin -n "$NS" \
  -f "$PROFILE/values-console-plugin.yaml" --atomic --wait --timeout 10m
oc -n "$NS" rollout status deployment/aiobs-mcp-server --timeout=5m
oc -n "$NS" rollout status deployment/aiobs-console-plugin --timeout=5m
```

Verify Console registration without replacing its existing plugin entries:

```bash
oc get consoleplugin aiobs-console-plugin
oc get consoles.config.openshift.io cluster -o jsonpath='{.spec.plugins}{"\n"}'
oc get co console
```

Do not add `ai-maas-credentials` until the MaaS endpoint uses protected transport. Keep `requiresApiKey: true`; mark model-backed chat and summaries as pending until you can validate endpoint discovery, authentication, completion and tool calling.
