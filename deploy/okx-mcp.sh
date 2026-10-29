#!/usr/bin/env bash
# Launch the OKX agent-trade-kit MCP server for this repo (see .mcp.json).
#
# - OKX US site (us.okx.com): the operator's key is only valid there.
# - Read-only: the server registers no order, transfer or bot write tools.
# - The okx-pilot proxy binary is disabled: it is a native download that
#   relays traffic through alternate nodes, and this repo never routes around
#   a geo-block.
# - Credentials: SURGE_OKX_* from the repo's gitignored .env are mapped to the
#   OKX_* names the server reads. With no .env the server runs public-only
#   (market data needs no key). Values are never printed.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${SURGE_ENV_FILE:-$repo/.env}"

# Parse KEY=value lines instead of sourcing, so .env is never executed.
read_env() {
	[[ -f "$env_file" ]] || return 0
	grep -E "^[[:space:]]*$1=" "$env_file" | tail -n 1 | cut -d= -f2- |
		sed -E 's/^[[:space:]]+|[[:space:]]+$//g; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

key="$(read_env SURGE_OKX_API_KEY)"
secret="$(read_env SURGE_OKX_API_SECRET)"
pass="$(read_env SURGE_OKX_API_PASSPHRASE)"
if [[ -n "$key" && -n "$secret" && -n "$pass" ]]; then
	export OKX_API_KEY="$key" OKX_SECRET_KEY="$secret" OKX_PASSPHRASE="$pass"
fi
unset key secret pass

export OKX_PILOT_BINARY_PATH="${OKX_PILOT_BINARY_PATH:-/nonexistent/okx-pilot-disabled}"

exec npx -y @okx_ai/okx-trade-mcp@1.4.9 --site us --read-only --modules "${OKX_MCP_MODULES:-all}" "$@"
