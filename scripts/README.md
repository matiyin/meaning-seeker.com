# Scripts

## goaccess_report.sh

Regenerates the GoAccess HTML report from Caddy access logs. Used by the admin dashboard for visitor stats.

**Requirements**: GoAccess installed, Caddy configured to write access logs (see `deploy/Caddyfile`).

**Cron** (e.g. daily at 2 AM):

```
0 2 * * * /path/to/meaning-seeker.com/scripts/goaccess_report.sh
```

**Env overrides**: `LOG_DIR` (default `/var/log/caddy`), `BOTS_FILE`, `REPORT_PATH`, `DATA_DIR`.

## sync_challenge_status_from_cycles.py

Repairs `human_challenges.json` when a challenge was woven into a cycle but still shows as "Waiting" (e.g. due to a race or missed update). Scans `archive/cycles` for `injection.woven_challenge` and sets those challenges to `status=woven` with `linked_cycle`/`linked_journal`. Run on the server if the queue and journal get out of sync.

```bash
.venv/bin/python scripts/sync_challenge_status_from_cycles.py
```

## backfill_challenges_from_quarantine.py

Backfills legacy human_challenges with `submitted_at`, `score`, `raw_text` from quarantine, and restores over-distilled `text` to the visitor's original words.

```bash
.venv/bin/python scripts/backfill_challenges_from_quarantine.py
```

## regenerate_images.py

Re-runs image art direction and generation for existing cycles using the current engine settings. Useful for A/B testing prompt changes without waiting for new cycles.

For each cycle, it sends the journal thinking to the inquiry model for a fresh `image_decision`, then generates an image with the current pipeline. Results are saved to `data/image-tests/<run_id>/` so they don't overwrite production images.

```bash
# All cycles
.venv/bin/python scripts/regenerate_images.py

# Specific cycles
.venv/bin/python scripts/regenerate_images.py --cycles 5,10,13

# Last N cycles
.venv/bin/python scripts/regenerate_images.py --last 4

# Dry run — get art direction decisions without generating images
.venv/bin/python scripts/regenerate_images.py --dry-run

# Replace — overwrite production images in data/images/ and update cycle records + journal MD
.venv/bin/python scripts/regenerate_images.py --replace

# Combine flags
.venv/bin/python scripts/regenerate_images.py --cycles 14 --dry-run
.venv/bin/python scripts/regenerate_images.py --last 4 --replace
```

Each run creates a timestamped folder under `data/image-tests/` containing:
- `NNNNNN-decision.json` — the art direction metadata (beyond_words, visual_energy, texture, palette, temperature, prompt)
- `NNNNNN-img-001.png` — the generated image (unless `--dry-run`)

With `--replace`, images are written to `data/images/` instead, overwriting existing cycle images. Cycle JSON files and journal MD files are updated with the new `image_decision` and image reference.
