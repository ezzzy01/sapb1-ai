#!/usr/bin/env bash
# One SAP B1 AI per company: own settings, SQL login, data, chat service and chat site. Same AI model.
#   ./company.sh add <id> <B1 database> "<name shown>"   create companies/<id>/ (then fill in its SQL login)
#   ./company.sh start <id>                              check the login, start its chat service and site, first refresh
#   ./company.sh stop <id>                               stop its chat service and site (data and chats are kept)
#   ./company.sh list                                    companies, databases and web addresses
#   ./company.sh install                                 install the background services (after a git pull)
#   ./company.sh run-all refresh|briefing                used by the nightly / morning timers: one company at a time
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPANIES="$ROOT/companies"
UNITS="$HOME/.config/systemd/user"

die() { echo "Error: $*" >&2; exit 1; }
env_of() { echo "$COMPANIES/$1/company.env"; }
get() { grep -E "^$2=" "$(env_of "$1")" | tail -1 | cut -d= -f2-; }
ids() { for f in "$COMPANIES"/*/company.env; do [[ -e $f ]] && basename "$(dirname "$f")"; done; }
need() { [[ -f $(env_of "$1") ]] || die "unknown company '$1' (see ./company.sh list)"; }
compose() { docker compose -f "$ROOT/docker-compose.yml" --env-file "$(env_of "$1")" "${@:2}"; }

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

WEBUI_NAME=SAP B1 AI - $name
WEBUI_PORT=$(next_port WEBUI_PORT 3100)
WEBUI_PROJECT=sapb1-ai-$id
WEBUI_CONTAINER=sapb1-ai-$id-webui
AGENT_PORT=$(next_port AGENT_PORT 8100)
AGENT_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

# Optional warnings for this company only (leave empty to turn off)
NTFY_URL=
ALERT_WEBHOOK_URL=
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
  compose "$id" up -d
  if [[ ! -f $ROOT/data/$(get "$id" SAPB1_DB)/sync_meta.json ]]; then
    echo "== First data refresh running in the background (see: journalctl --user -fu sapb1-ai-refresh@$id)"
    systemctl --user start --no-block "sapb1-ai-refresh@$id"
  fi
  echo "Started. Open http://$(hostname):$(get "$id" WEBUI_PORT) - the first person to sign up there becomes its admin."
}

cmd_stop() {
  local id=${1:-}; need "$id"
  systemctl --user disable --now "sapb1-ai-agent@$id"
  compose "$id" stop
}

cmd_list() {
  printf '%-14s %-18s %-7s %-7s %s\n' COMPANY DATABASE SITE CHAT NAME
  for id in $(ids); do
    printf '%-14s %-18s %-7s %-7s %s\n' "$id" "$(get "$id" SAPB1_DB)" ":$(get "$id" WEBUI_PORT)" ":$(get "$id" AGENT_PORT)" "$(get "$id" WEBUI_NAME)"
  done
}

cmd_install() {
  cp "$ROOT"/deploy/systemd/* "$UNITS/"
  systemctl --user daemon-reload
  systemctl --user enable --now sapb1-ai-refresh.timer sapb1-ai-briefing.timer
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
  stop) cmd_stop "${@:2}" ;;
  list) cmd_list ;;
  install) cmd_install ;;
  run-all) cmd_run_all "${@:2}" ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
