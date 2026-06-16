#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SOURCE_ENV="$PROJECT_ROOT/.env"
TARGET_ENV="$SCRIPT_DIR/.env"

if [ -f "$TARGET_ENV" ]; then
  echo "Using existing $TARGET_ENV"
  exit 0
fi

if [ -f "$SOURCE_ENV" ]; then
  cp "$SOURCE_ENV" "$TARGET_ENV"
  echo "Created $TARGET_ENV from $SOURCE_ENV"
else
  cp "$SCRIPT_DIR/.env.example" "$TARGET_ENV"
  echo "Created $TARGET_ENV from $SCRIPT_DIR/.env.example"
fi

echo "Docker env is ready: $TARGET_ENV"
