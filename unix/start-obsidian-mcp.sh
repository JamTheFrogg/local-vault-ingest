#!/usr/bin/env bash
# Starts the shared Obsidian MCP server that all your AIs use (macOS/Linux counterpart of
# windows/start-obsidian-mcp.cmd). The vault is VAULT_PATH from .env; edit the settings below if needed.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "${OBSIDIAN_VAULT_PATH:-}" ] && [ -f "$root/.env" ]; then
  OBSIDIAN_VAULT_PATH="$(sed -n 's/^VAULT_PATH=//p' "$root/.env" | head -n1 | sed 's/^["'\'']//; s/["'\'']$//')"
fi
export OBSIDIAN_VAULT_PATH="${OBSIDIAN_VAULT_PATH:?Set VAULT_PATH in .env}"

exe="$root/bin/obsidian-mcp"
[ -x "$exe" ] || exe="$HOME/.cargo/bin/obsidian-mcp"

# keep backups and the inbox out of search results
export OBSIDIAN_EXCLUDE_PATHS="_System/backups/**,00_Inbox/**"
# semantic search through LM Studio (bge-m3), like the Windows script
export OBSIDIAN_EMBEDDINGS=true
export OBSIDIAN_EMBEDDING_PROVIDER=api
export OBSIDIAN_EMBEDDING_API_BASE=http://localhost:1234/v1
export OBSIDIAN_EMBEDDING_API_MODEL=text-embedding-bge-m3
export OBSIDIAN_EMBEDDING_API_KEY=unused

exec "$exe" --http
