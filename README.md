# FastGPT Offline Mirrors

A self-hosted PyPI and NPM registry for disconnected Linux AMD64 environments. Visitors can browse versions and copy registry addresses. Administrators can upload, verify, and publish offline bundles.

## Build the images

Run on a connected machine with Docker. The release tags below are used by the supplied Compose files.

```bash
docker build --platform linux/amd64 -t fastgpt-local-pypi:20261027-amd64 docker/pypi
docker build --platform linux/amd64 -t fastgpt-local-npm:20261027-amd64 docker/npm
docker build --platform linux/amd64 -t fastgpt-mirror-manager:1.0.0-amd64 manager
docker save fastgpt-local-pypi:20261027-amd64 fastgpt-local-npm:20261027-amd64 fastgpt-mirror-manager:1.0.0-amd64 | gzip > mirror-images.tar.gz
```

## Install on an offline host

Transfer the source checkout and image archive. On Linux AMD64, as root, set `MIRROR_BIND_IP` to the host's trusted private-interface address. This starts empty registries. For an existing installation, point to its data directories and back up the original registry before proceeding.

```bash
docker load -i mirror-images.tar.gz
MIRROR_BIND_IP=127.0.0.1
DEPLOY_DIR=/opt/fastgpt-offline-mirrors
MIRROR_ROOT="$DEPLOY_DIR/data"
install -d "$MIRROR_ROOT/pypi/packages" "$MIRROR_ROOT/npm/storage"
install -m 0644 compose/npm-config.yaml "$MIRROR_ROOT/npm/config.yaml"
bash scripts/install-manager.sh "$MIRROR_BIND_IP" "$DEPLOY_DIR" "$MIRROR_ROOT"
```

The manager is at `https://<mirror-host>:18082/`. Trust its generated certificate through your organization's normal process or supply your own certificate. The generated initial administrator password is accessible only to root in `manager/secrets/initial-admin-password` under the data root. Store it in a password manager, then remove the initialization copy. Keep the service on a trusted network. Avoid publishing the deployment data, credentials, or state.

For FastGPT Agent Sandbox, set:

```yaml
AGENT_SANDBOX_PYPI_INDEX_URL: http://<mirror-host>:8081/simple
AGENT_SANDBOX_NPM_REGISTRY: http://<mirror-host>:4873
```

The registries have no upstream fallback. This project does not mirror APT or system executables.

## Offline bundles

Use the [bundle guide](manager/static/guide.md) and [shell wrapper](manager/static/mirror-bundle) on an internet-connected staging machine. The manager image serves as the builder and verifier for CPython 3.10/3.12 and Node.js 24 on AMD64. Transfer the generated archive through your approved offline channel. Only a registry administrator can upload, preflight and explicitly publish it.

## Rollback

Run `bash scripts/rollback-manager.sh "$DEPLOY_DIR"` to stop the console and restore the previous NPM storage. Previously published PyPI wheels stay available. The managed NPM store is kept for recovery.
