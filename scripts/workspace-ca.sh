#!/bin/sh

configure_workspace_ca() {
  if [ -n "${SSL_CERT_FILE:-}" ] && [ -s "${SSL_CERT_FILE}" ]; then
    return
  fi

  carmen_ca_path="${PROJECT_DIR}/work/koi-root-ca.pem"
  if [ ! -s "${carmen_ca_path}" ] && command -v security >/dev/null 2>&1; then
    mkdir -p "${PROJECT_DIR}/work"
    carmen_ca_temp=$(mktemp "${PROJECT_DIR}/work/koi-root-ca.XXXXXX")
    if security find-certificate \
      -c "Koi Root CA" \
      -p /Library/Keychains/System.keychain \
      > "${carmen_ca_temp}" 2>/dev/null \
      && openssl x509 -in "${carmen_ca_temp}" -noout >/dev/null 2>&1; then
      mv "${carmen_ca_temp}" "${carmen_ca_path}"
    else
      rm -f "${carmen_ca_temp}"
    fi
  fi

  if [ -s "${carmen_ca_path}" ]; then
    export SSL_CERT_FILE="${carmen_ca_path}"
  fi
}
