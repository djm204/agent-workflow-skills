#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_SRC="$SKILL_DIR/templates/plugin"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/pm-swarm"

if [ ! -f "$PLUGIN_SRC/plugin.yaml" ] || [ ! -f "$PLUGIN_SRC/__init__.py" ] || [ ! -f "$PLUGIN_SRC/core.py" ]; then
  echo "pm-swarm plugin template is incomplete under $PLUGIN_SRC" >&2
  exit 1
fi

mkdir -p "$PLUGIN_DIR"
cp "$PLUGIN_SRC/plugin.yaml" "$PLUGIN_SRC/__init__.py" "$PLUGIN_SRC/core.py" "$PLUGIN_DIR/"

if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable pm-swarm
  hermes plugins list --json | python3 -c 'import json,sys; print([p for p in json.load(sys.stdin) if p.get("name") == "pm-swarm"])'
else
  echo "Installed plugin files to $PLUGIN_DIR"
  echo "Install Hermes on PATH, then run: hermes plugins enable pm-swarm"
fi

echo "pm-swarm plugin installed at $PLUGIN_DIR"
echo "Restart Hermes or start a fresh session for model tool registry reload."
