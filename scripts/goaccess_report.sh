#!/bin/bash
# Regenerate GoAccess HTML report from Caddy access logs.
# Run daily via cron, e.g.: 0 2 * * * /path/to/meaning-seeker.com/scripts/goaccess_report.sh
#
# Requires: GoAccess installed, Caddy writing access logs to LOG_DIR.
# Caddy log path is configured in deploy/Caddyfile (default: /var/log/caddy/meaning-seeker-access.log).
#
# For local dev (no Caddy logs): set LOG_DIR to a path with logs, or the script
# will write a placeholder report. On the server, ensure /var/log/caddy exists
# and Caddy is configured to write access logs there.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${DATA_DIR:-$PROJECT_ROOT/data}"
# Default: /var/log/caddy on server; fallback to data/logs for local dev
if [ -z "${LOG_DIR}" ]; then
  if [ -d "/var/log/caddy" ]; then
    LOG_DIR="/var/log/caddy"
  else
    LOG_DIR="$DATA_DIR/logs"
  fi
fi
BOTS_FILE="${BOTS_FILE:-$PROJECT_ROOT/config/goaccess_bots.txt}"
REPORT_PATH="${REPORT_PATH:-$PROJECT_ROOT/data/admin/goaccess_report.html}"

# Ensure admin dir exists
mkdir -p "$(dirname "$REPORT_PATH")"

# Collect logs: current + rotated, oldest first for correct ordering
# Caddy rolls to meaning-seeker-access-{timestamp}-{size|time}.log
LOGS=$(ls -tr "$LOG_DIR"/meaning-seeker-access*.log 2>/dev/null || true)

if [ -n "$LOGS" ]; then
  if [ -f "$BOTS_FILE" ]; then
    goaccess $LOGS \
      --log-format=CADDY \
      --ignore-crawlers \
      --browsers-file="$BOTS_FILE" \
      --output="$REPORT_PATH" \
      -o html \
      --no-progress
  else
    goaccess $LOGS \
      --log-format=CADDY \
      --ignore-crawlers \
      --output="$REPORT_PATH" \
      -o html \
      --no-progress
  fi
  echo "GoAccess report written to $REPORT_PATH"
else
  # No logs: write placeholder so admin dashboard can display a message
  echo "No access logs in $LOG_DIR — writing placeholder report"
  cat > "$REPORT_PATH" << 'PLACEHOLDER'
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>GoAccess Report</title></head>
<body style="font-family:system-ui;padding:2rem;background:#1a1a1c;color:#ddd;">
  <h1>Visitor Stats</h1>
  <p>No access logs yet. On the server:</p>
  <ol>
    <li>Configure Caddy to write access logs (see deploy/Caddyfile)</li>
    <li>Ensure <code>/var/log/caddy</code> exists and Caddy can write there</li>
    <li>Run <code>scripts/goaccess_report.sh</code> daily via cron</li>
  </ol>
  <p>For local dev, set <code>LOG_DIR</code> to a directory containing Caddy JSON logs.</p>
</body></html>
PLACEHOLDER
  echo "Placeholder report written to $REPORT_PATH"
fi
