#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "${PROJECT_DIR}"
. "${PROJECT_DIR}/scripts/workspace-ca.sh"
. "${PROJECT_DIR}/scripts/privacy-env.sh"
configure_workspace_ca
configure_private_runtime

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "MLX LoRA training requires an Apple Silicon Mac." >&2
  echo "The Carmen agent itself is cross-platform; use Ollama for local inference." >&2
  exit 2
fi

uv sync --extra mlx --extra dev
uv run carmen-prepare
exec uv run mlx_lm.lora --config configs/lora.yaml "$@"
