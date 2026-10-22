# Disconnected deployment package

This folder contains an offline Dockerfile and a Kustomize overlay for deploying the vLLM Metrics Dashboard when cluster nodes cannot pull from public registries. It reuses the dashboard's existing application, Route, ServiceAccount, namespace RBAC, and monitoring RBAC manifests.

The offline Dockerfile installs Python requirements only from `python-wheelhouse/`, which is populated on a connected preparation host. The frontend bundle is built before transfer. The Python base image and OAuth proxy image are carried as OCI archives. In the disconnected environment, load those archives, build the dashboard image without pulling, then push both workload images to a registry reachable by the cluster.

The overlay points both containers directly at that registry. It does not require a cluster-wide image mirror policy. The registry must be reachable by every cluster node and trusted by the cluster. The `llm` namespace must already exist. Applying the overlay also creates two cluster-scoped RoleBindings, so use an identity authorized to create them.

## 1. Prepare offline build inputs on a connected host

Use a connected host with Podman, Node.js 22, npm, and access to the public registries and Python package index. The commands below target `linux/amd64`; change the platform and archive names to match the cluster node architecture. If the connected host cannot run that architecture, use a connected Linux host with the same architecture as the cluster nodes to prepare the wheelhouse and base image.

From the repository root, build the frontend bundle and prepare a Python wheelhouse using the same Python base image and target architecture as the offline build:

```bash
cd vllm-metrics-dashboard
(cd frontend && npm ci && npm run build)
mkdir -p disconnected/python-wheelhouse
podman pull --platform linux/amd64 python:3.12.11-slim-bookworm
podman run --rm --platform linux/amd64 \
  -v "$PWD:/src" -w /src \
  python:3.12.11-slim-bookworm \
  python -m pip download --only-binary=:all: --dest disconnected/python-wheelhouse \
  -r backend/requirements.lock
podman save --format oci-archive \
  -o disconnected/python-base-linux-amd64.oci \
  python:3.12.11-slim-bookworm
```

Fetch and save the OAuth proxy image used by the deployment manifest:

```bash
podman pull --platform linux/amd64 quay.io/openshift/origin-oauth-proxy:4.20.0
podman save --format oci-archive \
  -o disconnected/oauth-proxy-linux-amd64.oci \
  quay.io/openshift/origin-oauth-proxy:4.20.0
```

Transfer the complete `vllm-metrics-dashboard` source directory, including the generated `frontend/dist/`, `disconnected/python-wheelhouse/`, and both `.oci` files, to a build host inside the restricted environment. These generated artifacts are ignored by Git.

## 2. Build and publish the images inside the restricted environment

Load the Python base archive and build the dashboard without any registry pulls or package-index access:

```bash
cd vllm-metrics-dashboard
podman load -i disconnected/python-base-linux-amd64.oci
podman build --platform linux/amd64 --pull=never \
  --file disconnected/Dockerfile \
  --tag vllm-metrics-dashboard:1.0.0 .
```

Load and retag the OAuth proxy archive, then log in to a registry that is reachable by the cluster. Update the example hostname to your registry:

```bash
podman load -i disconnected/oauth-proxy-linux-amd64.oci
podman login --authfile /secure/path/registry-auth.json registry.disconnected.example.com
podman tag vllm-metrics-dashboard:1.0.0 \
  registry.disconnected.example.com/llm/vllm-metrics-dashboard:1.0.0
podman tag quay.io/openshift/origin-oauth-proxy:4.20.0 \
  registry.disconnected.example.com/openshift/origin-oauth-proxy:4.20.0
podman push --authfile /secure/path/registry-auth.json \
  registry.disconnected.example.com/llm/vllm-metrics-dashboard:1.0.0
podman push --authfile /secure/path/registry-auth.json \
  registry.disconnected.example.com/openshift/origin-oauth-proxy:4.20.0
```

## 3. Point the overlay at the registry and deploy

Edit the two image references in `deployment.patch.yaml` if your registry hostname or repository paths differ. Check the current cluster and namespace, then create the dashboard's private secrets and apply the overlay:

```bash
oc whoami
oc project llm
oc create secret generic vllm-metrics-dashboard-secrets -n llm \
  --from-literal=cookie-secret="$(openssl rand -hex 16)" \
  --from-literal=scope-signing-key="$(openssl rand -hex 32)"
oc apply -k vllm-metrics-dashboard/disconnected
oc rollout status deployment/vllm-metrics-dashboard -n llm
oc get route vllm-metrics-dashboard -n llm
```

Run `oc create secret` only when the dashboard secret does not already exist. If the registry requires workload pull credentials, log in with an auth file and create a pull secret from it, then link it to the dashboard ServiceAccount only:

```bash
oc create secret generic dashboard-registry-pull -n llm \
  --from-file=.dockerconfigjson=/secure/path/registry-auth.json \
  --type=kubernetes.io/dockerconfigjson
oc secrets link vllm-metrics-dashboard dashboard-registry-pull --for=pull -n llm
```

The registry CA and node trust configuration must already be installed by the cluster administrator when the registry uses a private CA. Confirm the rollout, route, dashboard logs, and authorized model discovery using the checks in the main [deployment guide](../README.md#build-and-deploy-to-openshift).

## Files

- `Dockerfile` builds from the transferred Python wheelhouse and prebuilt frontend bundle without network access.
- `kustomization.yaml` applies the existing OpenShift resources with this overlay.
- `deployment.patch.yaml` switches both containers to registry-hosted images.
- `python-wheelhouse/` is populated during connected preparation and is not committed.

No cluster token, registry credential, cookie secret, or signing key belongs in this folder.
