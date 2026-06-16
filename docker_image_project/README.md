# VSS2 Demo Docker Image

This guide is for customers who want to pull and run the VSS2 demo image with
Docker Compose.

## Files You Need

Use these files:

```text
docker_image_project/
├── docker-compose.yml
├── .env.example
└── README.md
```

The published image is:

```text
ocir.us-ashburn-1.oci.oraclecloud.com/idxkccw2srke/ai_demo:demo
```

Docker Compose runs the same image as two containers:

```text
app    -> web UI and API on port 8000
worker -> background video analysis worker
```

## 1. Check Docker

Run:

```bash
docker compose version
docker info
```

If Docker is pointing at an old Podman socket, clear these variables:

```bash
unset DOCKER_HOST
unset CONTAINER_HOST
unset DOCKER_CONTEXT
```

## 2. Create Env File

From inside `docker_image_project`:

```bash
cp .env.example .env
vi .env
```

Keep these image values:

```bash
CONTAINER_REGISTRY=ocir.us-ashburn-1.oci.oraclecloud.com
TENANCY_NAMESPACE=idxkccw2srke
IMAGE_NAME=ai_demo
IMAGE_TAG=demo
```

Fill in the Oracle ADB and model values in `.env`.

If using an ADB wallet, set:

```bash
ADB_WALLET_DIR=/wallet
ADB_WALLET_HOST_DIR=./wallet
```

Then put the wallet files in:

```text
docker_image_project/wallet/
```

## 3. Login If Required

If the OCIR repository is private, log in:

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

Use an OCI auth token as the password. Do not use your OCI Console password.

If the repository is public, login is not required for pull.

## 4. Pull The Image

From inside `docker_image_project`:

```bash
docker compose --env-file .env -f docker-compose.yml pull app
```

## 5. Run The App

Start the web app:

```bash
docker compose --env-file .env -f docker-compose.yml up -d --no-build app
```

Check health:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"healthy","region":"us-ashburn-1","db":"oracle-adb"}
```

Open the UI:

```text
http://localhost:8000
```

## 6. Run App And Worker

Start both containers:

```bash
docker compose --env-file .env -f docker-compose.yml up -d --no-build
```

Show logs:

```bash
docker compose --env-file .env -f docker-compose.yml logs -f app worker
```

Check container status:

```bash
docker compose --env-file .env -f docker-compose.yml ps
```

## 7. Stop The Demo

Stop and remove containers:

```bash
docker compose --env-file .env -f docker-compose.yml down
```

Remove downloaded volumes if you want a clean reset:

```bash
docker volume rm vss2-docker-image-project_vss2_uploads
docker volume rm vss2-docker-image-project_vss2_fastembed_cache
```

## Troubleshooting

If `curl http://localhost:8000/health` works but the browser does not, try:

```text
http://localhost:8000
```

If running inside WSL and Windows browser cannot access localhost, get the WSL
IP and open it in the browser:

```bash
ip -4 -o addr show eth0 | awk '{print $4}'
```

Use the IP without the CIDR suffix:

```text
http://<WSL_IP>:8000
```

If `curl -I http://localhost:8000/` returns `405 Method Not Allowed`, that is
expected because the app route supports `GET`, not `HEAD`. Use:

```bash
curl -s http://localhost:8000/ | head
```
