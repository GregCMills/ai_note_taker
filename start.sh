#!/usr/bin/env bash
# Starts everything from cold (the web build, the app with speech-to-text, and the LLM), then shows the logs.
# Ctrl+C stops and removes the containers.
#   ./start.sh          Gemma (fast)
#   ./start.sh qwen     Qwen (slower, more careful with edits)
#   ./start.sh api      just the app, using the LLM and speech-to-text services set in .env
set -euo pipefail
cd "$(dirname "$0")"

setup=${1:-gemma}
case $setup in
  gemma | qwen) logged=(app llm) ;;
  api) logged=(app) ;;
  *) echo "Usage: $0 [gemma|qwen|api]" >&2; exit 1 ;;
esac
file=docker-compose.$setup.yml

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

# All setups use the same container names, so any other one has to go first.
for other in gemma qwen api; do
  [[ $other == "$setup" ]] || docker compose -f "docker-compose.$other.yml" down 2>/dev/null || true
done

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

compose logs -f --since 1m "${logged[@]}" || true
stop
