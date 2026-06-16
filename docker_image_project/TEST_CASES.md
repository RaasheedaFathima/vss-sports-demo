# VSS2 Docker Image Pre-Publish Tests

Run these before pushing the image to OCIR.

## Automated Test

From the repository root:

```bash
docker_image_project/test-image.sh
```

The script checks:

1. `docker_image_project/.env` exists or can be created from the app `.env`
2. required env keys are present without printing secret values
3. `ADB_WALLET_HOST_DIR` exists on the host
4. Docker Compose can render the config
5. both services resolve to the same OCIR image
6. the single image builds successfully
7. the app container starts
8. `/health` returns successfully
9. `/` serves the bundled frontend

The script removes Compose containers on exit. To keep containers for debugging:

```bash
CLEANUP_ON_EXIT=0 docker_image_project/test-image.sh
```

To also start the worker container:

```bash
FULL_STACK=1 docker_image_project/test-image.sh
```

Use `FULL_STACK=1` only when ADB and OCI instance-principal access are expected
to work from the host.

## Manual Publish Check

After the automated test passes:

```bash
docker login ocir.us-ashburn-1.oci.oraclecloud.com
docker compose --env-file docker_image_project/.env -f docker_image_project/docker-compose.yml push app
```

Expected image:

```text
ocir.us-ashburn-1.oci.oraclecloud.com/idxkccw2srke/ai_demo:demo
```

This path is built from `CONTAINER_REGISTRY`, `TENANCY_NAMESPACE`,
`IMAGE_NAME`, and `IMAGE_TAG` in `docker_image_project/.env`.

## Pull Test

On a clean machine with Docker Compose, or on this machine after the image has
been pushed:

```bash
docker login ocir.us-ashburn-1.oci.oraclecloud.com
docker_image_project/test-pulled-image.sh
```

The script removes the local image by default before pulling. To keep the local
image:

```bash
REMOVE_LOCAL_IMAGE=0 docker_image_project/test-pulled-image.sh
```

To also start the worker from the pulled image:

```bash
FULL_STACK=1 docker_image_project/test-pulled-image.sh
```
