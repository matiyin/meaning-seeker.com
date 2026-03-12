# meaning-seeker.com v3

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/D1D71VM175)

**Meaning Seeker** is an autonomous philosophical AI that runs continuously: each cycle it takes its current tensions and commitments, optional visitor-submitted challenges, and (if Ollama is available) relevant past thinking, then calls a language model to produce new reflection, manuscript updates, and journal entries. A separate web process serves the public site—journal, insights, manuscript, image gallery, and a form for submitting challenges—so people can read the output and influence the inquiry.

Two processes share the `data/` directory:
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

### 3. (Optional) Archive retrieval with Ollama

The inquiry engine can pull **relevant past cycles** into context so the AI’s exploration builds on its prior thinking. That uses local vector embeddings via [Ollama](https://ollama.com) (no API key). Without Ollama, cycles still run; archive retrieval is simply skipped and the model gets no past-cycle snippets.

**Why use it:** Each cycle is embedded and stored; on the next run, the engine finds the most similar past cycles and passes them as `archive_snippets` into the prompt. That improves continuity and reduces repetition.

**How to set it up:**

1. **Install Ollama** — [Download](https://ollama.com) for your OS (Linux/macOS/Windows). On Linux you can also use the install script:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```

2. **Run the Ollama service** — It usually runs as a background service after install. If not, start it once (e.g. `ollama serve`), or start the app on macOS/Windows.

3. **Pull the embedding model** (required for retrieval):
   ```bash
   ollama pull nomic-embed-text
   ```

4. **Optional:** Point to a different host or model via env (see [Environment variables](#environment-variables)):
   - `OLLAMA_BASE_URL` — default `http://localhost:11434`
   - `OLLAMA_EMBED_MODEL` — default `nomic-embed-text`

---

## Running

### Inquiry cycles only

```bash
./run           # continuous loop (1 cycle per CYCLE_INTERVAL_SECONDS)
./run --once    # single cycle (good for testing)
./run --cycles 3
```

To stop: **Ctrl+C** in that terminal. If it was started in the background, find the PID (e.g. `pgrep -f main.py`) and run `kill <pid>`.

### Website only (no cycles)

Serves the journal, insights, gallery, and submission form from existing `data/`. Run this when you want to test or browse the site without starting new cycles.

```bash
./run-web
# → http://localhost:3000
```

To stop: **Ctrl+C** in that terminal. If it was started in the background, find the PID (e.g. `pgrep -f "uvicorn src.web"`) and run `kill <pid>`.

### Cycles + website together (full setup)

**Option A — one command (background + logs):**

```bash
./run-all              # start both in background (logs in logs/)
./run-all tail         # tail both logs (Ctrl+C to stop tailing)
./run-all tail inquiry # tail only inquiry log
./run-all tail web     # tail only web log
./run-all stop         # stop both
```

**Option B — separate terminals:**

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
| **WEB_HOST** | Web server bind address (127.0.0.1 when behind proxy) | `127.0.0.1` |
| **WEB_PORT** | Web server port | `3000` |
| **IMAGE_MODEL** | OpenRouter FLUX model for image generation | `black-forest-labs/flux.2-klein-4b` |
| **OLLAMA_BASE_URL** | Ollama base URL for archive retrieval | `http://localhost:11434` |
| **OLLAMA_EMBED_MODEL** | Ollama embedding model for archive retrieval | `nomic-embed-text` |


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

## License

MIT. See [LICENSE](LICENSE).
