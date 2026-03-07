# Scripts

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
