#!/bin/sh

configure_private_runtime() {
  # Do not send library telemetry or implicitly reuse a Hugging Face token.
  export DO_NOT_TRACK=1
  export HF_HUB_DISABLE_TELEMETRY=1
  export HF_HUB_DISABLE_IMPLICIT_TOKEN=1

  # Privacy-first default: dependencies and model weights must already be cached.
  # Set CARMEN_ALLOW_NETWORK=1 only for an intentional dependency/model download.
  if [ "${CARMEN_ALLOW_NETWORK:-0}" != "1" ]; then
    export HF_HUB_OFFLINE=1
    export TRANSFORMERS_OFFLINE=1
    export UV_OFFLINE=1
  fi
}
