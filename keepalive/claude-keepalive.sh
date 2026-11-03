#!/usr/bin/env bash
set -uo pipefail
tick_epoch="$(date +%s)"
TOKEN_HELPER="$(cd "$(dirname "$0")" && pwd)/token.py"
if [ $((tick_epoch % 1800)) -ge 30 ]; then
  echo "SKIP outside the half-hour start window: $(date -u +%FT%TZ)" >>"${CLAUDE_KEEPALIVE_LOG:-$HOME/keepalive/logs/claude-keepalive.log}"
  exit 0
fi
KEEPALIVE_DEADLINE=$((tick_epoch - tick_epoch % 1800 + 55))
MODEL="${CLAUDE_KEEPALIVE_MODEL:-haiku}"
EFFORT="${CLAUDE_KEEPALIVE_EFFORT:-low}"
PROMPT="${CLAUDE_KEEPALIVE_PROMPT:-Reply with exactly one word: pong}"
CLAUDE_BIN="${CLAUDE_KEEPALIVE_BIN:-$HOME/.local/bin/claude}"
TIMEOUT_SECONDS="${CLAUDE_KEEPALIVE_TIMEOUT_SECONDS:-60}"
ATTEMPTS="${CLAUDE_KEEPALIVE_ATTEMPTS:-3}"
RETRY_SLEEP_SECONDS="${CLAUDE_KEEPALIVE_RETRY_SLEEP_SECONDS:-3}"
LOG="${CLAUDE_KEEPALIVE_LOG:-$HOME/keepalive/logs/claude-keepalive.log}"
STATE_PATH="${CLAUDE_KEEPALIVE_STATE_PATH:-$HOME/.local/state/claude-keepalive/state.json}"
LOCK="${CLAUDE_KEEPALIVE_LOCK:-$HOME/.local/state/claude-keepalive/lock}"
RUN_DIR="${CLAUDE_KEEPALIVE_RUN_DIR:-$HOME/.local/state/claude-keepalive/cwd}"
LOG_MAX_LINES="${CLAUDE_KEEPALIVE_LOG_MAX_LINES:-2000}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE_PATH")" "$RUN_DIR"
if [ -z "${CLAUDE_KEEPALIVE_LOCKED:-}" ]; then
  export CLAUDE_KEEPALIVE_LOCKED=1
  exec flock -n "$LOCK" /bin/bash "$0" "$@"
fi
exec >>"$LOG" 2>&1
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
save_state(){ local st="$1" at="$2" dt="${3:-}" rs="${4:-null}"; local tmp="${STATE_PATH}.tmp"
  printf '{\n  "status": "%s",\n  "checked_at": "%s",\n  "model": "%s",\n  "attempt": %s,\n  "reset_at": %s,\n  "detail": %s\n}\n' \
    "$st" "$(date -u +%FT%TZ)" "$MODEL" "$at" "$rs" "$(printf %s "$dt" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo \"\")" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE_PATH" 2>/dev/null || true; }
echo "===== claude-keepalive $(date -u +%FT%TZ) ($(date '+%F %T %Z')) ====="
[ -x "$CLAUDE_BIN" ] || { log "FATAL: claude CLI not executable at $CLAUDE_BIN"; save_state error 0 "claude CLI missing"; exit 1; }
run_ping(){ cd "$RUN_DIR" || return 1; CLAUDE_CODE_OAUTH_TOKEN="$(python3 "$TOKEN_HELPER" claude "$CLAUDE_KEEPALIVE_TOKEN_SOURCE" 2>&1)" || { echo "$CLAUDE_CODE_OAUTH_TOKEN"; return 1; }; export CLAUDE_CODE_OAUTH_TOKEN; perl -e 'alarm shift; exec @ARGV' "$TIMEOUT_SECONDS" "$CLAUDE_BIN" -p --model "$MODEL" --effort "$EFFORT" --tools "" --setting-sources project --strict-mcp-config --disable-slash-commands --no-session-persistence --system-prompt "$PROMPT" --output-format stream-json --verbose ping </dev/null 2>&1; }
# The reset time comes from the ping itself: a setup token cannot call the usage API.
ping_field(){ python3 -c '
import json, sys
found = ""
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if not isinstance(m, dict):
        continue
    if sys.argv[1] == "reply" and m.get("type") == "result":
        found = m.get("result") or ""
    elif sys.argv[1] == "reset" and m.get("type") == "rate_limit_event":
        info = m.get("rate_limit_info") or {}
        five = (info.get("unifiedWindows") or {}).get("five_hour") or {}
        r = five.get("resetsAt") or (info.get("resetsAt") if info.get("rateLimitType") == "five_hour" else None)
        if r:
            found = round(r)
print(found)' "$1"; }
# A reset more than an hour past means runs were skipped, so the current window is unknown.
saved_reset(){ STATE_PATH="$STATE_PATH" python3 -c '
import json, os, time
try:
    r = int(json.load(open(os.environ["STATE_PATH"]))["reset_at"])
    print(r if r >= time.time() - 3600 else "")
except Exception:
    print("")'; }
reset_at="$(saved_reset)"
[ -n "$reset_at" ] || log "No recent reset time saved; attempting the scheduled request"
renewal_due=0
if [ -n "$reset_at" ] && [ "$reset_at" -lt $((KEEPALIVE_DEADLINE + 5)) ]; then renewal_due=1; fi
for attempt in $(seq 1 "$ATTEMPTS"); do
  now="$(date +%s)"
  if [ "$renewal_due" -eq 1 ] && [ -n "$reset_at" ] && [ "$reset_at" -ge "$now" ] && [ $((reset_at + 2)) -le $((KEEPALIVE_DEADLINE - 30)) ]; then
    log "Waiting $((reset_at - now + 2))s for expiry within the first minute of the slot"
    sleep $((reset_at - now + 2))
  fi
  remaining=$((KEEPALIVE_DEADLINE - $(date +%s)))
  if [ "$remaining" -lt 30 ]; then break; fi
  if [ "$TIMEOUT_SECONDS" -gt "$remaining" ]; then TIMEOUT_SECONDS="$remaining"; fi
  out="$(run_ping)"; rc=$?
  after_reset="$(printf '%s\n' "$out" | ping_field reset)"
  answer="$(printf '%s\n' "$out" | ping_field reply)"
  [ -n "$answer" ] && out="$answer"
  reply="$(printf '%s' "$answer" | tr -d '\r\n[:space:].' | tr '[:upper:]' '[:lower:]')"
  if [ $rc -eq 0 ] && [ "$reply" = pong ]; then
    min_reset="$(date +%s)"
    [ "$renewal_due" -eq 1 ] && min_reset=$((min_reset + 14000))
    if [ -n "$after_reset" ] && [ "$after_reset" -gt "$min_reset" ]; then
      log "OK (attempt $attempt/$ATTEMPTS) reply=pong reset=$(date -d "@$after_reset" '+%F %T %Z')"
      status=ok
      if [ $((after_reset % 1800)) -ge 60 ]; then
        status=pending_alignment
        log "Window is still off the half hour; waiting for expiry and the next half-hourly request"
      fi
      save_state "$status" "$attempt" "reset=$after_reset" "$after_reset"
      if [ "$(wc -l <"$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX_LINES" ]; then tail -n "$LOG_MAX_LINES" "$LOG" >"${LOG}.tmp" && mv -f "${LOG}.tmp" "$LOG"; fi
      exit 0
    fi
    if [ "$renewal_due" -eq 1 ] && [ -n "$after_reset" ] && [ "$after_reset" -gt "$(date +%s)" ]; then
      log "PENDING: request accepted, but the expiring window was not renewed; next opportunity is the next half hour"
      save_state waiting_next_slot "$attempt" "old window still active; reset=$after_reset" "$after_reset"
      exit 0
    fi
    out="pong received but five-hour reset not verified: reset=$after_reset"
  fi
  log "FAIL (attempt $attempt/$ATTEMPTS) rc=$rc: $(printf '%s' "$out" | tail -c 600)"
  remaining=$((KEEPALIVE_DEADLINE - $(date +%s)))
  if [ "$attempt" -ge "$ATTEMPTS" ] || [ "$remaining" -le $((RETRY_SLEEP_SECONDS + 30)) ]; then break; fi
  sleep "$RETRY_SLEEP_SECONDS"
done
log "Attempt failed; next attempt at the next half hour"
save_state error "$attempt" "request or quota verification failed within the first minute of the slot"
exit 1
