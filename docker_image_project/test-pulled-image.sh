#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
ENV_FILE="$SCRIPT_DIR/.env"
HEALTH_TMP="/tmp/vss2-pull-health.$$"
INDEX_TMP="/tmp/vss2-pull-index.$$"

COMPOSE="docker compose --env-file $ENV_FILE -f $COMPOSE_FILE"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

info() {
  echo
  echo "==> $*"
}

load_env_value() {
  key="$1"
  awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1); exit}' "$ENV_FILE"
}

require_env_key() {
  key="$1"
  value=$(load_env_value "$key")
  [ -n "$value" ] || fail "$key is missing or empty in $ENV_FILE"
}

wait_for_health() {
  url="$1"
  attempts="${2:-30}"
  i=1
  while [ "$i" -le "$attempts" ]; do
    if curl -fsS "$url" >"$HEALTH_TMP" 2>/dev/null; then
      cat "$HEALTH_TMP"
      rm -f "$HEALTH_TMP"
      return 0
    fi
    sleep 2
    i=$((i + 1))
  done
  rm -f "$HEALTH_TMP"
  return 1
}

cleanup() {
  rm -f "$HEALTH_TMP" "$INDEX_TMP"
  if [ "${CLEANUP_ON_EXIT:-1}" = "1" ]; then
    $COMPOSE down >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

cd "$PROJECT_ROOT"

info "Preparing Docker env"
"$SCRIPT_DIR/prepare-env.sh"
[ -f "$ENV_FILE" ] || fail "$ENV_FILE was not created"

for key in CONTAINER_REGISTRY TENANCY_NAMESPACE IMAGE_NAME IMAGE_TAG ADB_WALLET_HOST_DIR; do
  require_env_key "$key"
done

image="$(load_env_value CONTAINER_REGISTRY)/$(load_env_value TENANCY_NAMESPACE)/$(load_env_value IMAGE_NAME):$(load_env_value IMAGE_TAG)"
app_port="$(load_env_value APP_PORT)"
[ -n "$app_port" ] || app_port="8000"
wallet_host_dir="$(load_env_value ADB_WALLET_HOST_DIR)"

[ -d "$wallet_host_dir" ] || fail "ADB_WALLET_HOST_DIR does not exist: $wallet_host_dir"

info "Testing pulled image"
echo "Image: $image"

info "Checking Docker Compose availability"
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not in PATH"
docker compose version >/dev/null

info "Stopping any existing stack"
$COMPOSE down >/dev/null 2>&1 || true

if [ "${REMOVE_LOCAL_IMAGE:-1}" = "1" ]; then
  info "Removing local image before pull"
  docker image rm "$image" >/dev/null 2>&1 || true
else
  info "Keeping any existing local image. Set REMOVE_LOCAL_IMAGE=1 to force a fresh pull."
fi

info "Pulling image from registry"
$COMPOSE pull app

info "Verifying pulled image exists locally"
docker image inspect "$image" >/dev/null

info "Starting app from pulled image only"
$COMPOSE up -d --no-build app

if ! wait_for_health "http://localhost:$app_port/health" 30; then
  $COMPOSE logs app || true
  fail "Pulled app image failed health check"
fi

info "Checking frontend response"
curl -fsS "http://localhost:$app_port/" >"$INDEX_TMP"
grep -q "VSS2" "$INDEX_TMP" || fail "Frontend response did not contain VSS2"
rm -f "$INDEX_TMP"

if [ "${FULL_STACK:-0}" = "1" ]; then
  info "Starting full stack from pulled image only"
  $COMPOSE up -d --no-build
  sleep 10
  $COMPOSE ps
  $COMPOSE logs --tail=80 app worker
else
  info "Skipping worker pull test. Set FULL_STACK=1 to run app + worker."
fi

info "PASS: pulled image works"
