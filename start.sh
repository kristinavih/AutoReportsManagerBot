#!/bin/sh

echo "[START] Starting main Telegram bot..."
python -u bot.py &
BOT_PID=$!

echo "[START] Starting Telethon leads collector..."
python -u telethon_leads.py &
LEADS_PID=$!

cleanup() {
    echo "[STOP] Stopping processes..."

    kill "$BOT_PID" 2>/dev/null || true
    kill "$LEADS_PID" 2>/dev/null || true

    wait "$BOT_PID" 2>/dev/null || true
    wait "$LEADS_PID" 2>/dev/null || true
}

trap 'cleanup; exit 0' INT TERM

while true; do
    if ! kill -0 "$BOT_PID" 2>/dev/null; then
        wait "$BOT_PID"
        EXIT_CODE=$?
        echo "[ERROR] bot.py stopped with code $EXIT_CODE"
        break
    fi

    if ! kill -0 "$LEADS_PID" 2>/dev/null; then
        wait "$LEADS_PID"
        EXIT_CODE=$?
        echo "[ERROR] telethon_leads.py stopped with code $EXIT_CODE"
        break
    fi

    sleep 2
done

cleanup
exit "$EXIT_CODE"