#!/usr/bin/env bash
# Pull latest data from remote VPS (including injections) for local testing.
# Uses rsync. Requires SSH access to the VPS.
#
# Set in .env or export before running:
#   VPS_HOST       - SSH target: user@host or hostname (e.g. meaning-seeker.com)
#   VPS_DATA_PATH  - Remote path to data dir (e.g. /home/user/meaning-seeker.com/data)
#
# Or pass as args: ./sync_from_vps.sh [user@]host [remote_data_path]
#
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load from .env if present
if [[ -f "$ROOT/.env" ]]; then
  for var in DATA_DIR VPS_HOST VPS_DATA_PATH; do
    val=$(grep -E "^${var}=" "$ROOT/.env" 2>/dev/null | cut -d= -f2- | sed 's/^"\(.*\)"$/\1/')
    [[ -n "$val" ]] && export "$var=$val"
  done
fi

DATA_DIR="${DATA_DIR:-./data}"
[[ "$DATA_DIR" != /* ]] && DATA_DIR="$ROOT/$DATA_DIR"

# VPS connection: args override env
VPS_HOST="${1:-$VPS_HOST}"
VPS_DATA_PATH="${2:-$VPS_DATA_PATH}"

if [[ -z "$VPS_HOST" || -z "$VPS_DATA_PATH" ]]; then
  echo "Usage: $0 [user@]host [remote_data_path]"
  echo "   or: VPS_HOST=user@host VPS_DATA_PATH=/path/to/data $0"
  echo ""
  echo "Pulls from REMOTE:remote_data_path/ to local DATA_DIR ($DATA_DIR)"
  exit 1
fi

# Ensure trailing slash on remote so we sync contents into DATA_DIR
REMOTE="${VPS_DATA_PATH%/}/"

echo "Syncing from $VPS_HOST:$REMOTE → $DATA_DIR"
mkdir -p "$DATA_DIR"
rsync -avz --progress "$VPS_HOST:$REMOTE" "$DATA_DIR/"

echo "Done. Data (including injections) is ready for local testing."
