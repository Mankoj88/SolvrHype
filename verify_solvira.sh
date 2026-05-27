#!/bin/bash
# verify_solvira.sh — Verifikasi bot Solvira running pada commit terbaru
# Usage: bash verify_solvira.sh
# Letakkan di ~/solvira/ atau jalankan dari sana

set -o pipefail

SOLVIRA_DIR="$HOME/solvira"
SERVICE="solvira"
HEALTH_URL="http://localhost:8080/health"

# Warna untuk output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()    { echo -e "${GREEN}✓${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
info()  { echo -e "${BLUE}i${NC} $1"; }

ALL_PASS=true

echo "═══════════════════════════════════════════════════════════"
echo "  SOLVIRA DEPLOYMENT VERIFICATION"
echo "  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "═══════════════════════════════════════════════════════════"

# ─── 1. Git commit ───────────────────────────────────────────
echo ""
echo "[1] Git Commit Status"
cd "$SOLVIRA_DIR" || { fail "Cannot cd to $SOLVIRA_DIR"; exit 1; }

LOCAL_COMMIT=$(git rev-parse HEAD)
LOCAL_SHORT=$(git rev-parse --short HEAD)
LOCAL_MSG=$(git log -1 --format='%s')
LOCAL_TIME=$(git log -1 --format='%ai')

info "Local commit:  $LOCAL_SHORT — $LOCAL_MSG"
info "Commit time:   $LOCAL_TIME"

# Fetch latest dari remote tanpa merge, just untuk compare
if git fetch origin --quiet 2>/dev/null; then
    REMOTE_COMMIT=$(git rev-parse origin/main)
    REMOTE_SHORT=$(git rev-parse --short origin/main)

    if [[ "$LOCAL_COMMIT" == "$REMOTE_COMMIT" ]]; then
        ok "Local matches origin/main ($LOCAL_SHORT)"
    else
        fail "Local ($LOCAL_SHORT) != origin/main ($REMOTE_SHORT)"
        warn "Run: git pull origin main && sudo systemctl restart $SERVICE"
        ALL_PASS=false
    fi
else
    warn "Cannot fetch from origin (network?). Skipping remote compare."
fi

# Cek uncommitted changes
if [[ -n "$(git status --porcelain)" ]]; then
    warn "Uncommitted changes detected in working tree:"
    git status --short | head -10
fi

# ─── 2. Systemd service ─────────────────────────────────────
echo ""
echo "[2] Systemd Service"

if sudo systemctl is-active --quiet "$SERVICE"; then
    ok "Service is active (running)"
    SERVICE_PID=$(sudo systemctl show "$SERVICE" --property=MainPID --value)
    SERVICE_START=$(sudo systemctl show "$SERVICE" --property=ActiveEnterTimestamp --value)
    info "Main PID:    $SERVICE_PID"
    info "Started:     $SERVICE_START"

    # Bandingkan start time vs commit time
    SERVICE_START_EPOCH=$(date -d "$SERVICE_START" +%s 2>/dev/null || echo 0)
    COMMIT_TIME_EPOCH=$(date -d "$LOCAL_TIME" +%s 2>/dev/null || echo 0)

    if [[ $SERVICE_START_EPOCH -lt $COMMIT_TIME_EPOCH ]]; then
        fail "Service started BEFORE current commit was made"
        warn "Bot running stale code. Run: sudo systemctl restart $SERVICE"
        ALL_PASS=false
    else
        ok "Service started after current commit (code in memory should be current)"
    fi
else
    fail "Service is NOT active"
    warn "Last log lines:"
    sudo journalctl -u "$SERVICE" -n 20 --no-pager
    ALL_PASS=false
fi

# ─── 3. Process working directory ───────────────────────────
echo ""
echo "[3] Process Working Directory"

if [[ -n "$SERVICE_PID" && "$SERVICE_PID" != "0" ]]; then
    if [[ -r "/proc/$SERVICE_PID/cwd" ]]; then
        PROC_CWD=$(sudo readlink "/proc/$SERVICE_PID/cwd" 2>/dev/null)
        if [[ "$PROC_CWD" == "$SOLVIRA_DIR" ]]; then
            ok "Process CWD matches: $PROC_CWD"
        else
            fail "Process CWD: $PROC_CWD (expected: $SOLVIRA_DIR)"
            ALL_PASS=false
        fi
    else
        warn "Cannot read /proc/$SERVICE_PID/cwd (need sudo)"
    fi
fi

# ─── 4. Log freshness ────────────────────────────────────────
echo ""
echo "[4] Log Freshness (bot harus log dalam 5 menit terakhir)"

LAST_LOG=$(sudo journalctl -u "$SERVICE" -n 1 --no-pager --output=short-iso 2>/dev/null | tail -1 | awk '{print $1}')

if [[ -n "$LAST_LOG" ]]; then
    LAST_LOG_EPOCH=$(date -d "$LAST_LOG" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    AGE_SECONDS=$((NOW_EPOCH - LAST_LOG_EPOCH))

    info "Last log entry: $LAST_LOG (${AGE_SECONDS}s ago)"

    if [[ $AGE_SECONDS -lt 300 ]]; then
        ok "Log is fresh (<5 min)"
    elif [[ $AGE_SECONDS -lt 900 ]]; then
        warn "Log is ${AGE_SECONDS}s old — might be normal kalau scanner interval long"
    else
        fail "No log entry for >15 min. Bot mungkin stuck atau idle."
        ALL_PASS=false
    fi
else
    warn "No journal entries found for $SERVICE"
fi

# ─── 5. Error grep di log terkini ────────────────────────────
echo ""
echo "[5] Recent Errors"

ERR_COUNT=$(sudo journalctl -u "$SERVICE" --since "10 minutes ago" --no-pager 2>/dev/null | \
    grep -iE "error|exception|traceback|critical|failed" | wc -l)

if [[ $ERR_COUNT -eq 0 ]]; then
    ok "No errors in last 10 minutes"
else
    warn "$ERR_COUNT error-related lines in last 10 minutes:"
    sudo journalctl -u "$SERVICE" --since "10 minutes ago" --no-pager 2>/dev/null | \
        grep -iE "error|exception|traceback|critical|failed" | tail -5
    info "Run: sudo journalctl -u $SERVICE --since '10 minutes ago' | grep -iE 'error|exception'"
fi

# ─── 6. Health endpoint ─────────────────────────────────────
echo ""
echo "[6] Health Endpoint"

if command -v curl &>/dev/null; then
    HTTP_CODE=$(curl -s -o /tmp/health_resp -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null)
    if [[ "$HTTP_CODE" == "200" ]]; then
        ok "Health endpoint returns 200"
        info "Response: $(cat /tmp/health_resp | head -c 200)"
    elif [[ -z "$HTTP_CODE" || "$HTTP_CODE" == "000" ]]; then
        fail "Health endpoint unreachable (connection refused / timeout)"
        warn "Health server di bot mungkin belum start atau crash"
        ALL_PASS=false
    else
        fail "Health endpoint returned HTTP $HTTP_CODE"
        ALL_PASS=false
    fi
    rm -f /tmp/health_resp
else
    warn "curl not installed, skip health check"
fi

# ─── 7. Environment flags ───────────────────────────────────
echo ""
echo "[7] Environment Flags (sanity check)"

# Cek .env.age existence saja, jangan decrypt (sensitif)
if [[ -f "$SOLVIRA_DIR/.env.age" ]]; then
    ok ".env.age exists (encrypted)"
    info "Verifikasi DRY_RUN / USE_TESTNET manual via decrypt jika ragu"
elif [[ -f "$SOLVIRA_DIR/.env" ]]; then
    DRY_RUN=$(grep -E '^DRY_RUN=' "$SOLVIRA_DIR/.env" | cut -d= -f2)
    USE_TESTNET=$(grep -E '^USE_TESTNET=' "$SOLVIRA_DIR/.env" | cut -d= -f2)
    info "DRY_RUN=$DRY_RUN  USE_TESTNET=$USE_TESTNET"
    if [[ "$DRY_RUN" != "true" && "$USE_TESTNET" != "true" ]]; then
        warn "Both DRY_RUN and USE_TESTNET are false — bot is in LIVE MAINNET mode"
    fi
else
    warn "No .env or .env.age found at expected location"
fi

# ─── Summary ─────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
if $ALL_PASS; then
    echo -e "${GREEN}  ALL CHECKS PASSED${NC}"
    echo "  Bot Solvira running pada commit $LOCAL_SHORT"
    exit 0
else
    echo -e "${RED}  SOME CHECKS FAILED — review output di atas${NC}"
    exit 1
fi

