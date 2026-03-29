#!/bin/sh
set -eu

TS_CONNECTION_MODE="${TS_CONNECTION_MODE:-self}"
TS_SOCKET="${TS_SOCKET:-/var/run/tailscale/tailscaled.sock}"
TS_HOST_SOCKET="${TS_HOST_SOCKET:-/host-tailscale/tailscaled.sock}"
TS_STATE_DIR="${TS_STATE_DIR:-/var/lib/tailscale}"
APP_PORT="${APP_PORT:-5189}"
APP_HOST="${APP_HOST:-0.0.0.0}"
TS_HOSTNAME="${TS_HOSTNAME:-}"
TS_ACCEPT_DNS="${TS_ACCEPT_DNS:-false}"
TS_ACCEPT_ROUTES="${TS_ACCEPT_ROUTES:-false}"
TS_ADVERTISE_TAGS="${TS_ADVERTISE_TAGS:-}"
TS_EXTRA_ARGS="${TS_EXTRA_ARGS:-}"
TS_USERSPACE="${TS_USERSPACE:-false}"

mkdir -p "${TS_STATE_DIR}" /var/run/tailscale /data

TAILSCALED_PID=""

cleanup() {
  if [ -n "${TAILSCALED_PID}" ] && kill -0 "${TAILSCALED_PID}" 2>/dev/null; then
    kill "${TAILSCALED_PID}" 2>/dev/null || true
    wait "${TAILSCALED_PID}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

wait_for_socket() {
  socket_path="$1"
  socket_label="$2"
  echo "Waiting for ${socket_label} at ${socket_path}"
  attempts=0
  until tailscale --socket="${socket_path}" status >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "${attempts}" -ge 30 ]; then
      echo "${socket_label} did not become ready in time"
      exit 1
    fi
    sleep 1
  done
}

if [ "${TS_CONNECTION_MODE}" = "self" ]; then
  TAILSCALED_ARGS="--state=${TS_STATE_DIR}/tailscaled.state --socket=${TS_SOCKET}"
  if [ "${TS_USERSPACE}" = "true" ]; then
    TAILSCALED_ARGS="${TAILSCALED_ARGS} --tun=userspace-networking"
  fi

  echo "Starting tailscaled in self mode"
  /usr/sbin/tailscaled ${TAILSCALED_ARGS} &
  TAILSCALED_PID=$!
  wait_for_socket "${TS_SOCKET}" "tailscaled socket"

  if [ -n "${TS_AUTHKEY:-}" ]; then
    UP_ARGS="--authkey=${TS_AUTHKEY} --accept-dns=${TS_ACCEPT_DNS} --accept-routes=${TS_ACCEPT_ROUTES} --reset"
    if [ -n "${TS_HOSTNAME}" ]; then
      UP_ARGS="${UP_ARGS} --hostname=${TS_HOSTNAME}"
    fi
    if [ -n "${TS_ADVERTISE_TAGS}" ]; then
      UP_ARGS="${UP_ARGS} --advertise-tags=${TS_ADVERTISE_TAGS}"
    fi

    echo "Running tailscale up"
    # shellcheck disable=SC2086
    tailscale --socket="${TS_SOCKET}" up ${UP_ARGS} ${TS_EXTRA_ARGS}
  else
    echo "TS_AUTHKEY not provided; assuming existing Tailscale state or manual login later"
  fi

  export TAILSCALE_SOCKET="${TAILSCALE_SOCKET:-${TS_SOCKET}}"
elif [ "${TS_CONNECTION_MODE}" = "host" ]; then
  wait_for_socket "${TS_HOST_SOCKET}" "host tailscaled socket"
  export TAILSCALE_SOCKET="${TAILSCALE_SOCKET:-${TS_HOST_SOCKET}}"
  echo "Using host tailscaled via ${TAILSCALE_SOCKET}"
else
  echo "Unsupported TS_CONNECTION_MODE: ${TS_CONNECTION_MODE}"
  exit 1
fi

echo "Starting API on ${APP_HOST}:${APP_PORT}"
exec gunicorn \
  --bind "${APP_HOST}:${APP_PORT}" \
  --workers 1 \
  --threads 4 \
  --timeout 60 \
  app:app
