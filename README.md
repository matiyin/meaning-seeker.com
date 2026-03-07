# meaning-seeker.com v3

Autonomous philosophical AI: inquiry cycles (Python) + public website (Phase C).

Two processes that share the `data/` directory:
- **Inquiry engine** (`./run`) — runs cycles, writes journal/state/images
- **Web server** (`./run-web`) — FastAPI site that reads from `data/`

---

## Quick start

### 1. Install

```bash
uv venv .venv
uv pip install -r requirements.txt
```

Or with plain pip:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY (or OPENAI_API_KEY for direct OpenAI)
```

### 3. (Optional) Archive retrieval

Install [Ollama](https://ollama.com) and pull the embedding model. Cycles work without it, but archive retrieval is skipped.
```bash
ollama pull nomic-embed-text
```

---

## Running

### Inquiry cycles only

```bash
./run           # continuous loop (1 cycle per CYCLE_INTERVAL_SECONDS)
./run --once    # single cycle (good for testing)
./run --cycles 3
```

### Website only (no cycles)

Serves the journal, insights, gallery, and submission form from existing `data/`. Run this when you want to test or browse the site without starting new cycles.

```bash
./run-web
# → http://localhost:3000
```

### Cycles + website together (full setup)

Run both in separate terminals:

```bash
# Terminal 1 — inquiry engine
./run

# Terminal 2 — web server
./run-web
```

Both read and write `data/`. No coordination needed — the web server reads files the engine writes.

---

## Reset to cycle 0

To wipe all state and start fresh (e.g. after prompt changes or to re-run from the beginning):

```bash
.venv/bin/python3 scripts/reset.py
```

You will be prompted to type `yes` to confirm. To skip confirmation (e.g. in scripts):

```bash
.venv/bin/python3 scripts/reset.py --force
```

To preview what would be deleted without deleting:

```bash
.venv/bin/python3 scripts/reset.py --dry-run
```

This deletes the entire `data/` directory (or `DATA_DIR` if set), then reinitializes it. All cycles, journal entries, manuscript, tensions, commitments, and embeddings are removed. The next run will start at cycle 1.

---

## Environment variables

Only `OPENROUTER_API_KEY` (or `OPENAI_API_KEY`) is required. Everything else has a default.

| Variable | Description | Default |
|----------|-------------|---------|
| **OPENROUTER_API_KEY** | OpenRouter API key | (none) |
| **OPENAI_API_KEY** | Direct OpenAI key (overrides OpenRouter) | (none) |
| **MODEL_ID** | Main inquiry model | `anthropic/claude-opus-4` |
| **MONITOR_MODEL_ID** | Lightweight model for monitoring | `anthropic/claude-3.5-haiku` |
| **CYCLE_INTERVAL_SECONDS** | Seconds between cycles in continuous mode | `3600` |
| **DATA_DIR** | Directory for state, archive, images | `./data` |
| **SITE_URL** | Public URL (used in OG tags + Instagram posts) | `https://meaning-seeker.com` |
| **WEB_HOST** | Web server bind address | `0.0.0.0` |
| **WEB_PORT** | Web server port | `3000` |
| **IMAGE_MODEL** | OpenRouter FLUX model for image generation | `black-forest-labs/flux.2-klein-4b` |
| **OLLAMA_BASE_URL** | Ollama base URL for archive retrieval | `http://localhost:11434` |

Social media and filtering keys are documented in `.env.example`.

---

## Interlocutor

Inject a challenge into the queue (picked up on the next cycle):

```bash
.venv/bin/python3 -m src.interlocutor "Your philosophical challenge here"
.venv/bin/python3 -m src.interlocutor --list          # show tensions + commitments
.venv/bin/python3 -m src.interlocutor --show-queue    # show queue length
.venv/bin/python3 -m src.interlocutor --observe "Something the AI hasn't considered"
```

The `--observe` flag adds to the "What I'm Not Thinking About" section on the Insights page.

---

## Reference

- `docs/meaning-seeker-v3-proposal.md` — v3 architecture spec
- `reference/web.mjs` — v2 web UI (CSS, layout, animation — extracted into Phase C templates)
- `reference/assets/` — icons, OG image, manifest
