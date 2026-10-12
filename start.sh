#!/usr/bin/env bash
# Starts everything from cold (the web build, the app with speech-to-text, and the LLM), then shows the logs.
# Ctrl+C stops and removes the containers.
#   ./start.sh          Gemma (fast)
#   ./start.sh qwen     Qwen (slower, more careful with edits)
set -euo pipefail
cd "$(dirname "$0")"

model=${1:-gemma}
case $model in
  gemma) other=qwen ;;
  qwen) other=gemma ;;
  *) echo "Usage: $0 [gemma|qwen]" >&2; exit 1 ;;
esac
file=docker-compose.$model.yml

# So files the containers write in ./data stay yours.
export UID
export GID=$(id -g)

compose() { docker compose -f "$file" "$@"; }

stop() {
  trap - INT TERM
  echo
  echo "Stopping..."
  compose down
  exit 0
}
trap stop INT TERM

# Both setups use the same container names, so the other one has to go first.
docker compose -f "docker-compose.$other.yml" down 2>/dev/null || true

compose up -d --force-recreate web app

lan_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
echo
echo "Open https://${lan_ip:-localhost}:8443  (the models take a minute or two to load)"
if [[ ! -s data/accounts.json ]]; then
  echo "No accounts yet. Create one (the first takes over existing notes) with:"
  echo "  docker compose -f $file exec app /venv/bin/python -m server.accounts add <name>"
fi
echo "Ctrl+C to stop."
echo

compose logs -f --since 1m app llm || true
stop
