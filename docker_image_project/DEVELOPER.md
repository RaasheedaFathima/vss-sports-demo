# VSS2 Docker Developer Guide

This guide is for the image publisher or developer. Give customers `README.md`,
`docker-compose.yml`, and `.env.example` instead.

## Image

The published image is:

```text
ocir.us-ashburn-1.oci.oraclecloud.com/idxkccw2srke/ai_demo:demo
```

The build context is the repository root. `Dockerfile.dockerignore` keeps the
image build scoped to only the app, worker, and frontend files needed by
`Dockerfile`.

## Check Docker

```bash
docker compose version
docker info
```

If Docker points at Podman:

```bash
unset DOCKER_HOST
unset CONTAINER_HOST
unset DOCKER_CONTEXT
```

On Oracle Linux 9, install Docker Engine and Compose:

```bash
sudo dnf remove -y podman-docker
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker opc
newgrp docker
docker run hello-world
docker compose version
```

## Prepare Env

From the repository root:

```bash
cd /home/opc/vss-sports-demo
docker_image_project/prepare-env.sh
vi docker_image_project/.env
```

Check image values:

```bash
awk -F= '/^(CONTAINER_REGISTRY|TENANCY_NAMESPACE|IMAGE_NAME|IMAGE_TAG)=/ {print}' docker_image_project/.env
```

Expected:

```bash
CONTAINER_REGISTRY=ocir.us-ashburn-1.oci.oraclecloud.com
TENANCY_NAMESPACE=idxkccw2srke
IMAGE_NAME=ai_demo
IMAGE_TAG=demo
```

Check wallet path:

```bash
awk -F= '/^(ADB_WALLET_DIR|ADB_WALLET_HOST_DIR)=/ {print}' docker_image_project/.env
test -d "$(awk -F= '$1=="ADB_WALLET_HOST_DIR" {print $2}' docker_image_project/.env)" && echo "wallet path ok"
```

## Validate Compose

```bash
docker compose \
  --env-file docker_image_project/.env \
  -f docker_image_project/docker-compose.yml \
  config
```

## Build

```bash
docker compose \
  --env-file docker_image_project/.env \
  -f docker_image_project/docker-compose.yml \
  build app
```

Verify:

```bash
docker image inspect ocir.us-ashburn-1.oci.oraclecloud.com/idxkccw2srke/ai_demo:demo
```

## Pre-Publish Test

```bash
docker_image_project/test-image.sh
```

Full app plus worker test:

```bash
FULL_STACK=1 docker_image_project/test-image.sh
```

Keep containers after failure:

```bash
CLEANUP_ON_EXIT=0 docker_image_project/test-image.sh
```

## Login To OCIR

```bash
docker login ocir.us-ashburn-1.oci.oraclecloud.com
```

Username format:

```text
idxkccw2srke/<oci-username>
```

For identity-domain users:

```text
idxkccw2srke/oracleidentitycloudservice/<oci-username>
```

Use an OCI auth token as the password.

## Push

```bash
docker compose \
  --env-file docker_image_project/.env \
  -f docker_image_project/docker-compose.yml \
  push app
```

## Pulled Image Test

```bash
docker_image_project/test-pulled-image.sh
```

Skip local image removal:

```bash
REMOVE_LOCAL_IMAGE=0 docker_image_project/test-pulled-image.sh
```

Run app plus worker:

```bash
FULL_STACK=1 docker_image_project/test-pulled-image.sh
```

Keep containers after failure:

```bash
CLEANUP_ON_EXIT=0 docker_image_project/test-pulled-image.sh
```
