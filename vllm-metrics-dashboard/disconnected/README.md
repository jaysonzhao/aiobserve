# Disconnected deployment package

This folder contains the image-mirroring inputs and a Kustomize overlay for deploying the vLLM Metrics Dashboard when cluster nodes cannot pull from public registries. It reuses the dashboard's existing application, Route, ServiceAccount, namespace RBAC, and monitoring RBAC manifests.

The overlay points both containers directly at the mirror registry and removes the ImageStream trigger. It does not require a cluster-wide image mirror policy. The mirror registry must be reachable by every cluster node and trusted by the cluster. The `llm` namespace must already exist. Applying the overlay also creates two cluster-scoped RoleBindings, so use an identity authorized to create them.

## 1. Build and stage the application image on a connected host

Use a connected build host with Podman, access to the public base-image registries, and access to a staging registry that `oc-mirror` can read. The full application image contains the frontend build and Python runtime, so the disconnected cluster does not need Node.js, Python package-index, or npm-registry access.

From the repository root, build and push a versioned image. Log in to the staging registry interactively before pushing; do not put credentials in this folder.

```bash
podman build --pull=always \
  --file vllm-metrics-dashboard/Dockerfile \
  --tag registry.connected.example.com/llm/vllm-metrics-dashboard:1.0.0 \
  vllm-metrics-dashboard
podman push registry.connected.example.com/llm/vllm-metrics-dashboard:1.0.0
```

Edit `imageset-config.yaml` if you used a different staging registry, repository, or tag. Keep the OAuth proxy source image aligned with `deployment.patch.yaml`.

## 2. Mirror the images to a transfer directory

Install the `oc-mirror` v2 plugin compatible with the target OpenShift release. Authenticate to the staging source registry and `quay.io` using a protected auth file. Generate the image archive from a connected host:

```bash
oc mirror --v2 \
  --config vllm-metrics-dashboard/disconnected/imageset-config.yaml \
  --authfile /secure/path/containers-auth.json \
  file:///var/tmp/vllm-metrics-dashboard-image-set
```

Transfer the complete `/var/tmp/vllm-metrics-dashboard-image-set` directory to a host that can reach the disconnected mirror registry. The transfer method is determined by your environment.

## 3. Copy the image set into the disconnected mirror registry

On the registry-connected host inside the restricted environment, use an auth file with push credentials for the mirror registry:

```bash
oc mirror --v2 \
  --config vllm-metrics-dashboard/disconnected/imageset-config.yaml \
  --from file:///var/tmp/vllm-metrics-dashboard-image-set \
  --authfile /secure/path/mirror-auth.json \
  docker://registry.mirror.example.com
```

Replace the example mirror host with your registry. The default mirror layout retains the source repository paths, so the example overlay expects these references:

- `registry.mirror.example.com/llm/vllm-metrics-dashboard:1.0.0`
- `registry.mirror.example.com/openshift/origin-oauth-proxy:4.20.0`

If your registry or mirroring configuration maps repositories differently, update the two image references in `deployment.patch.yaml` to match the actual mirror destinations.

## 4. Set the overlay's mirror references and deploy

Edit `deployment.patch.yaml` to use the mirror registry hostname and image paths from the previous step. Check the current cluster and namespace, then create the dashboard's private secrets and apply the overlay:

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

Run `oc create secret` only when the dashboard secret does not already exist. If the mirror registry requires workload pull credentials, create a pull secret in `llm` and link it to the dashboard ServiceAccount only:

```bash
oc create secret docker-registry dashboard-mirror-pull -n llm \
  --from-file=.dockerconfigjson=/secure/path/mirror-auth.json \
  --type=kubernetes.io/dockerconfigjson
oc secrets link vllm-metrics-dashboard dashboard-mirror-pull --for=pull -n llm
```

The registry CA and node trust configuration must already be installed by the cluster administrator when the registry uses a private CA. Confirm the rollout, route, dashboard logs, and authorized model discovery using the checks in the main [deployment guide](../README.md#build-and-deploy-to-openshift).

## Files

- `imageset-config.yaml` lists the built dashboard and OAuth proxy images for `oc-mirror` v2.
- `kustomization.yaml` applies the existing OpenShift resources with this overlay.
- `deployment.patch.yaml` switches both containers to mirrored image references and disables the connected BuildConfig ImageStream trigger.

No cluster token, registry credential, cookie secret, or signing key belongs in this folder.
