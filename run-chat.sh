#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "${PROJECT_DIR}"
. "${PROJECT_DIR}/scripts/workspace-ca.sh"
. "${PROJECT_DIR}/scripts/privacy-env.sh"
configure_workspace_ca
configure_private_runtime
exec uv run carmen-chat "$@"
