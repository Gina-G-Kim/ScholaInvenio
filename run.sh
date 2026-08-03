#!/usr/bin/env bash
# ScholaInvenio launch helper, works on both mac and Linux.
# Falls back to `docker run` when the `docker compose` plugin isn't installed.
set -euo pipefail

IMAGE="scholainvenio:1.0.0"
NAME="scholainvenio"
PORT="${HOST_PORT:-8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env explicitly so both the compose and docker-run paths see it the
# same way (docker compose auto-loads .env for variable substitution, but
# plain `docker run` does not).
if [ -f "$HERE/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env"
  set +a
fi

have_compose() { docker compose version >/dev/null 2>&1; }

up() {
  mkdir -p "$HERE/data"
  if have_compose; then
    docker compose -f "$HERE/docker-compose.yml" up -d --build
  else
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker build -t "$IMAGE" "$HERE"
    docker run -d --name "$NAME" \
      -p "${PORT}:8000" \
      -v "$HERE/data:/data" \
      --shm-size=1g \
      -e OPENALEX_API_KEY="${OPENALEX_API_KEY:-}" \
      -e OPENALEX_MAILTO="${OPENALEX_MAILTO:-}" \
      -e GSCHOLAR_SLEEP_MIN="${GSCHOLAR_SLEEP_MIN:-5.0}" \
      -e GSCHOLAR_SLEEP_MAX="${GSCHOLAR_SLEEP_MAX:-10.0}" \
      -e TZ="${TZ:-UTC}" \
      --restart unless-stopped \
      "$IMAGE" >/dev/null
  fi
  echo "-> open http://localhost:${PORT}"
}

case "${1:-up}" in
  up)   up ;;
  down) have_compose && docker compose -f "$HERE/docker-compose.yml" down || docker rm -f "$NAME" >/dev/null 2>&1 || true
        echo "-> stopped" ;;
  logs) have_compose && docker compose -f "$HERE/docker-compose.yml" logs -f || docker logs -f "$NAME" ;;
  test) docker build -t "$IMAGE" "$HERE"
        docker run --rm -v "$HERE/tests:/srv/tests:ro" "$IMAGE" python -m tests.test_core
        docker run --rm -v "$HERE/tests:/srv/tests:ro" "$IMAGE" \
          sh -c "pip install -q httpx==0.28.1 >/dev/null 2>&1; python -m tests.test_api" ;;
  *)    echo "usage: ./run.sh [up|down|logs|test]"; exit 1 ;;
esac
