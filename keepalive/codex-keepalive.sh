#!/usr/bin/env bash
set -uo pipefail
tick_epoch="$(date +%s)"
TOKEN_HELPER="$(cd "$(dirname "$0")" && pwd)/token.py"
if [ $((tick_epoch % 1800)) -ge 30 ]; then
  echo "SKIP outside the half-hour start window: $(date -u +%FT%TZ)" >>"${CODEX_KEEPALIVE_LOG:-$HOME/keepalive/logs/codex-keepalive.log}"
  exit 0
fi
KEEPALIVE_DEADLINE=$((tick_epoch - tick_epoch % 1800 + 55))
MODEL="${CODEX_KEEPALIVE_MODEL:-gpt-6-luna}"
PROMPT="${CODEX_KEEPALIVE_PROMPT:-Reply with exactly one word: pong}"
CODEX_BIN="${CODEX_KEEPALIVE_BIN:-$HOME/.local/bin/codex}"
TIMEOUT_SECONDS="${CODEX_KEEPALIVE_TIMEOUT_SECONDS:-60}"
ATTEMPTS="${CODEX_KEEPALIVE_ATTEMPTS:-3}"
RETRY_SLEEP_SECONDS="${CODEX_KEEPALIVE_RETRY_SLEEP_SECONDS:-3}"
LOG="${CODEX_KEEPALIVE_LOG:-$HOME/keepalive/logs/codex-keepalive.log}"
STATE_PATH="${CODEX_KEEPALIVE_STATE_PATH:-$HOME/.local/state/codex-keepalive/state.json}"
LOCK="${CODEX_KEEPALIVE_LOCK:-$HOME/.local/state/codex-keepalive/lock}"
RUN_DIR="${CODEX_KEEPALIVE_RUN_DIR:-$HOME/.local/state/codex-keepalive/cwd}"
LOG_MAX_LINES="${CODEX_KEEPALIVE_LOG_MAX_LINES:-2000}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE_PATH")" "$RUN_DIR"
if [ -z "${CODEX_KEEPALIVE_LOCKED:-}" ]; then
  export CODEX_KEEPALIVE_LOCKED=1
  exec flock -n "$LOCK" /bin/bash "$0" "$@"
fi
exec >>"$LOG" 2>&1
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
save_state(){ local st="$1" at="$2" dt="${3:-}"; local tmp="${STATE_PATH}.tmp"
  printf '{\n  "status": "%s",\n  "checked_at": "%s",\n  "model": "%s",\n  "attempt": %s,\n  "detail": %s\n}\n' \
    "$st" "$(date -u +%FT%TZ)" "$MODEL" "$at" "$(printf %s "$dt" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo \"\")" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE_PATH" 2>/dev/null || true; }
echo "===== codex-keepalive $(date -u +%FT%TZ) ($(date '+%F %T %Z')) ====="
[ -x "$CODEX_BIN" ] || { log "FATAL: codex CLI not executable at $CODEX_BIN"; save_state error 0 "codex CLI missing"; exit 1; }
# --ignore-user-config drops the plugins and MCP servers of the shared config.toml: the window starts when the reply completes, so startup time is drift
run_ping(){ cd "$RUN_DIR" || return 1; : >"$RUN_DIR/last-message"; python3 "$TOKEN_HELPER" codex "$CODEX_KEEPALIVE_AUTH_SOURCE" "$CODEX_HOME" 2>&1 || return 1; perl -e 'alarm shift; exec @ARGV' "$TIMEOUT_SECONDS" "$CODEX_BIN" exec --skip-git-repo-check --sandbox read-only --ephemeral --ignore-user-config --ignore-rules -c model_reasoning_effort="none" --output-last-message "$RUN_DIR/last-message" -m "$MODEL" "$PROMPT" </dev/null 2>&1; }
get_reset_at(){ python3 - "$@" <<'PY'
import json, os, sys, time, urllib.request
def primary_window():
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    t = json.load(open(os.path.join(home, "auth.json")))["tokens"]
    req = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers={
        "Authorization": "Bearer " + t["access_token"], "chatgpt-account-id": t.get("account_id", ""),
        "User-Agent": "codex_cli_rs", "originator": "codex_cli_rs"})
    return json.load(urllib.request.urlopen(req, timeout=15))["rate_limit"]["primary_window"]
try:
    w = primary_window()
    # With no window open the API still reports a reset: a full window ahead, moving with the clock.
    if w["reset_after_seconds"] >= w["limit_window_seconds"] - 2:
        if sys.argv[1:] != ["confirm"]:
            print("IDLE"); raise SystemExit
        first = w["reset_at"]; time.sleep(3); w = primary_window()
        if w["reset_at"] - first >= 2:
            print("IDLE"); raise SystemExit
    print(int(w["reset_at"]))
except Exception:
    print("ERR")
PY
}
local_time(){ date -d "@$1" '+%F %T %Z'; }
finish(){ local after_reset; after_reset="$(get_reset_at confirm)"
  case "$after_reset" in ''|*[!0-9]*) after_reset=0;; esac
  if [ "$after_reset" -gt "$(date +%s)" ]; then
    log "OK (attempt $attempt/$ATTEMPTS) reset=$(local_time "$after_reset")"
    save_state ok "$attempt" "reset=$after_reset"
  else
    log "UNVERIFIED (attempt $attempt/$ATTEMPTS) request accepted, but the quota shows no open window"
    save_state unverified "$attempt" "request accepted; no open window reported"
  fi
  if [ "$(wc -l <"$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX_LINES" ]; then tail -n "$LOG_MAX_LINES" "$LOG" >"${LOG}.tmp" && mv -f "${LOG}.tmp" "$LOG"; fi
  exit 0; }
current_reset="$(get_reset_at)"
case "$current_reset" in ''|*[!0-9]*) ;; *)
  now="$(date +%s)"
  if [ "$current_reset" -ge "$now" ] && [ $((current_reset + 2)) -le $((KEEPALIVE_DEADLINE - 20)) ]; then
    log "Waiting $((current_reset - now + 2))s for the current window to expire"
    sleep $((current_reset - now + 2))
  fi ;;
esac
for attempt in $(seq 1 "$ATTEMPTS"); do
  remaining=$((KEEPALIVE_DEADLINE - $(date +%s)))
  if [ "$remaining" -lt 15 ]; then break; fi
  if [ "$TIMEOUT_SECONDS" -gt "$remaining" ]; then TIMEOUT_SECONDS="$remaining"; fi
  out="$(run_ping)"; rc=$?
  reply="$(tr -d '\r\n[:space:]' <"$RUN_DIR/last-message" 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  if [ $rc -eq 0 ] && [ "$reply" = pong ]; then finish; fi
  log "FAIL (attempt $attempt/$ATTEMPTS) rc=$rc: $(printf '%s' "$out" | tail -c 600)"
  sleep "$RETRY_SLEEP_SECONDS"
done
log "No request succeeded in the first minute of the slot; next request at the next half hour"
save_state error "$attempt" "request failed"
exit 1
