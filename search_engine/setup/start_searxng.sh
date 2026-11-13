#!/usr/bin/env bash
# Starts the local SearXNG for the payer-policy search engine. Run inside WSL (Debian). Ctrl+C stops it.
set -e
source "$HOME/searx-pyenv/bin/activate"
cd "$HOME/searxng-src"
export SEARXNG_SETTINGS_PATH="$HOME/searxng-settings/settings.yml"
echo "SearXNG on http://127.0.0.1:8888  (Ctrl+C to stop)"
exec python -m searx.webapp
