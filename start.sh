#!/bin/bash
set -e

# Mirror dashboard-ref-only's startup: create every directory hermes expects
# and seed a default config.yaml if the volume is empty. Without these,
# `hermes dashboard` endpoints that hit logs/, sessions/, cron/, etc. can fail
# with opaque errors even though no auth is actually involved.
mkdir -p /data/.hermes/cron /data/.hermes/sessions /data/.hermes/logs \
         /data/.hermes/memories /data/.hermes/skills /data/.hermes/pairing \
         /data/.hermes/hooks /data/.hermes/image_cache /data/.hermes/audio_cache \
         /data/.hermes/workspace

if [ ! -f /data/.hermes/config.yaml ] && [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
  cp /opt/hermes-agent/cli-config.yaml.example /data/.hermes/config.yaml
fi

[ ! -f /data/.hermes/.env ] && touch /data/.hermes/.env

# Clear any stale gateway PID file left over from the previous container.
# `hermes gateway` writes /data/.hermes/gateway.pid on start but does not
# remove it on SIGTERM. Since /data is a persistent volume, the file
# survives container restarts and causes every subsequent boot to exit with
# "ERROR gateway.run: PID file race lost to another gateway instance".
# No hermes process can be running at this point (we're pre-exec in a fresh
# container), so removing the file unconditionally is safe.
rm -f /data/.hermes/gateway.pid

# ── GBrain — initialize brain database on first boot ─────────────────────────
mkdir -p "$GBRAIN_HOME"
if [ ! -f "$GBRAIN_HOME/.initialized" ]; then
    echo "[gbrain] First boot — initializing brain database..."
    gbrain init </dev/null 2>&1 | head -20 || true
    # Create brain repo directories for business data
    mkdir -p "$GBRAIN_HOME/brain/data/ventas"              "$GBRAIN_HOME/brain/companies"              "$GBRAIN_HOME/brain/people"              "$GBRAIN_HOME/brain/concepts"
    if [ ! -d "$GBRAIN_HOME/brain/.git" ]; then
        cd "$GBRAIN_HOME/brain" && git init -b main >/dev/null 2>&1 && \
        printf '# Body Nostrum LLC Brain\n\nKnowledge base para Body Nostrum LLC.\n' > README.md && \
        git add . && git commit -m "init brain repo" >/dev/null 2>&1 || true
    fi
    touch "$GBRAIN_HOME/.initialized"
    echo "[gbrain] Brain initialized. GBRAIN_HOME=$GBRAIN_HOME"
fi

# ── Provider config — write Railway env vars to Hermes .env on every boot ──────
# (Hermes reads its own .env for model credentials, separate from Railway env vars)
if [ -n "$ANTHROPIC_API_KEY" ]; then
    grep -q "ANTHROPIC_API_KEY" /data/.hermes/.env 2>/dev/null || \
        echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> /data/.hermes/.env
fi
if ! grep -q "^  default:" /data/.hermes/config.yaml 2>/dev/null; then
    # First boot: set model to Haiku (fast + cheap for Telegram)
    sed -i 's/provider: auto/provider: anthropic/' /data/.hermes/config.yaml 2>/dev/null || true
fi

# ── Telegram gateway — auto-start with --replace to kill any stale process ────
echo "[hermes] Starting Telegram gateway..."
nohup hermes gateway run --replace > /data/.hermes/logs/gateway.log 2>&1 &
echo "[hermes] Gateway started (PID $!)"

exec python /app/server.py
