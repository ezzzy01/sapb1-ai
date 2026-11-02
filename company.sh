#!/usr/bin/env bash
# SAP B1 AI for several companies behind one address and one sign-in. People choose the company when they sign in
# and may use only the companies their account allows; each company has its own chat site (own Open WebUI: chats,
# accounts, files), settings, AI instructions, SQL login, data and chat service.
#   ./company.sh add <id> <B1 database> "<name shown>"   create companies/<id>/ (then fill in its SQL login)
#   ./company.sh start <id>                              check the login, start its chat service, put it on the site
#   ./company.sh restart <id>                            apply changed settings or instructions.md for that company
#   ./company.sh stop <id>                               take it off the site and stop its chat service (data kept)
#   ./company.sh list                                    companies, databases, and whether they are on the site
#   ./company.sh site                                    start the sign-in page and every company's chat site
#   ./company.sh user ...                                people and the companies they may use (./company.sh user)
#   ./company.sh install                                 install the background services (after a git pull)
#   ./company.sh run-all refresh|briefing                used by the nightly / morning timers: one company at a time
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Address of the chat site over Tailscale (falls back to this PC's name)
SITE="http://$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || uname -n):3100"
COMPANIES="$ROOT/companies"
UNITS="$HOME/.config/systemd/user"

die() { echo "Error: $*" >&2; exit 1; }
env_of() { echo "$COMPANIES/$1/company.env"; }
get() { grep -E "^$2=" "$(env_of "$1")" | tail -1 | cut -d= -f2-; }
ids() { for f in "$COMPANIES"/*/company.env; do [[ -e $f ]] && basename "$(dirname "$f")"; done; }
need() { [[ -f $(env_of "$1") ]] || die "unknown company '$1' (see ./company.sh list)"; }
started() { for id in $(ids); do systemctl --user is-enabled --quiet "sapb1-ai-agent@$id" && echo "$id"; done; return 0; }
dc() { docker compose -f "$ROOT/docker-compose.yml" -f "$ROOT/site/docker-compose.companies.yml" "$@"; }

sync_site() {  # one chat site per started company, behind the shared sign-in
  local ids id i
  ids=$(cd "$ROOT" && uv run --frozen python -m sapb1_ai.site build)
  systemctl --user is-active --quiet sapb1-ai-portal || die "the sign-in service is not running (./company.sh install)"
  dc up -d 2>&1 | grep -v -i orphan || true
  docker exec sapb1-ai-proxy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || docker restart sapb1-ai-proxy >/dev/null
  for id in $ids; do
    for i in $(seq 120); do
      [[ $(docker inspect -f '{{.State.Health.Status}}' "sapb1-ai-webui-$id" 2>/dev/null) == healthy ]] && break
      sleep 1
    done
  done
  [[ -n $ids ]] && (cd "$ROOT" && uv run --frozen python -m sapb1_ai.webui register $ids)
  return 0
}

next_port() {  # one above the highest port any company uses, starting at $2
  local max=$(( $2 - 1 )) p
  for id in $(ids); do p=$(get "$id" "$1"); [[ $p =~ ^[0-9]+$ ]] && (( p > max )) && max=$p; done
  echo $(( max + 1 ))
}

cmd_add() {
  local id=${1:-} db=${2:-} name=${3:-}
  [[ $id =~ ^[a-z0-9][a-z0-9-]*$ && -n $db && -n $name ]] || die 'usage: ./company.sh add <id> <B1 database> "<name shown>"   (id: lowercase, digits, dashes)'
  [[ -e $COMPANIES/$id ]] && die "company '$id' already exists"
  for other in $(ids); do [[ $(get "$other" SAPB1_DB) == "$db" ]] && die "database $db is already used by company '$other'"; done
  local host port
  host=$(grep -E '^SAPB1_SQL_HOST=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- || true)
  port=$(grep -E '^SAPB1_SQL_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- || true)
  mkdir -p "$COMPANIES/$id"
  cp "$ROOT"/config/*.csv "$COMPANIES/$id/"
  (umask 077; cat > "$(env_of "$id")" <<EOF
# SAP B1 AI settings for "$name". Private to this company; git-ignored. Nothing here is shared with other companies.
SAPB1_DB=$db
SAPB1_SQL_HOST=$host
SAPB1_SQL_PORT=${port:-1433}
# A read-only SQL login that can open ONLY this company's database (db_datareader on $db, nothing else)
SAPB1_SQL_USER=ai_$(echo "$id" | tr - _)
SAPB1_SQL_PASSWORD=

# Name in the chat site's company menu; also the name of the group whose members may use it
COMPANY_NAME=$name
AGENT_MODEL_ID=sapb1-ai-$id
AGENT_PORT=$(next_port AGENT_PORT 8100)
AGENT_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

# Optional warnings for this company only (leave empty to turn off)
NTFY_URL=
ALERT_WEBHOOK_URL=

# How this company's AI works (optional; remove the # and change the value, then: ./company.sh restart $id).
# Its own wishes in plain words (tone, language, what to focus on, house terms) go in instructions.md next to this file.
#AGENT_BASE_MODEL=sapb1-stock   # Ollama model that answers (another model per company = GPU swaps, slower)
#DEFAULT_LEAD_DAYS=14           # supplier lead time when B1 has none
#REVIEW_WEEKS=1                 # how often stock is reviewed and ordered
#ANOMALY_DAYS=90                # how far back the transaction checks look
#PRICE_DEV_PCT=25               # price this far from usual is flagged
#PO_PRICE_TOL_PCT=2             # invoice vs purchase order price tolerance
#MARKET_FX_BASE=USD             # exchange rates against this currency
#MARKET_FX_SYMBOLS=EUR,GBP,JPY,CNY,MYR,SGD
EOF
  )
  echo "Created $COMPANIES/$id/"
  echo "Next: put the SQL login for $db in $(env_of "$id") (SAPB1_SQL_USER / SAPB1_SQL_PASSWORD),"
  echo "      optionally list its critical items in $COMPANIES/$id/critical_items.csv, then: ./company.sh start $id"
}

cmd_start() {
  local id=${1:-}; need "$id"
  echo "== Checking the SQL login for $id"
  (cd "$ROOT" && SAPB1_COMPANY=$id uv run --frozen sapb1-check)
  systemctl --user daemon-reload
  systemctl --user enable --now "sapb1-ai-agent@$id"
  sync_site
  if [[ ! -f $ROOT/data/$(get "$id" SAPB1_DB)/sync_meta.json ]]; then
    echo "== First data refresh running in the background (see: journalctl --user -fu sapb1-ai-refresh@$id)"
    systemctl --user start --no-block "sapb1-ai-refresh@$id"
  fi
  echo "Started. People sign in on $SITE and choose \"$(get "$id" COMPANY_NAME)\"."
  echo "Give someone access:  ./company.sh user companies <email> <companies,...,$id>   (new person: ./company.sh user add)"
}

cmd_restart() {  # after changing a company's settings or instructions.md
  local id=${1:-}; need "$id"
  systemctl --user restart "sapb1-ai-agent@$id"
  sync_site
}

cmd_stop() {
  local id=${1:-}; need "$id"
  systemctl --user disable --now "sapb1-ai-agent@$id"
  sync_site
  docker stop "sapb1-ai-webui-$id" >/dev/null 2>&1 || true   # its chats stay in its volume
}

cmd_list() {
  printf '%-12s %-14s %-6s %-9s %s\n' COMPANY DATABASE CHAT ON-SITE "NAME (= group)"
  for id in $(ids); do
    local on=no; systemctl --user is-enabled --quiet "sapb1-ai-agent@$id" && on=yes
    printf '%-12s %-14s %-6s %-9s %s\n' "$id" "$(get "$id" SAPB1_DB)" ":$(get "$id" AGENT_PORT)" "$on" "$(get "$id" COMPANY_NAME)"
  done
  echo; echo "Sign in: $SITE (choose the company there)"
}

cmd_site() {
  sync_site
}

cmd_install() {
  cp "$ROOT"/deploy/systemd/* "$UNITS/"
  systemctl --user daemon-reload
  systemctl --user enable --now sapb1-ai-refresh.timer sapb1-ai-briefing.timer sapb1-ai-portal.service
  echo "Installed background services into $UNITS"
}

cmd_run_all() {
  [[ ${1:-} == refresh || ${1:-} == briefing ]] || die "usage: ./company.sh run-all refresh|briefing"
  local failed=0
  for id in $(ids); do
    systemctl --user start --wait "sapb1-ai-$1@$id" || { echo "$1 failed for $id" >&2; failed=1; }
  done
  return $failed
}

case ${1:-} in
  add) cmd_add "${@:2}" ;;
  start) cmd_start "${@:2}" ;;
  restart) cmd_restart "${@:2}" ;;
  stop) cmd_stop "${@:2}" ;;
  list) cmd_list ;;
  site) cmd_site ;;
  user) (cd "$ROOT" && uv run --frozen python -m sapb1_ai.portal "${@:2}") ;;
  install) cmd_install ;;
  run-all) cmd_run_all "${@:2}" ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
