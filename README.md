# meaning-seeker.com v3

Fresh start for v3 redesign.

## Reference materials

- **reference/web.mjs** — v2 web UI (design, layout, CSS)
- **reference/assets/** — v2 icons, images, manifest
- **reference/docs/** — v3 proposal and related docs
- **reference/.env** — Copy to root as `.env` (not committed; contains API keys)

## Branches

- **v2-legacy** — Previous production code
- **v3** — This branch: clean slate for v3 implementation

## Next steps

1. Copy `reference/.env` to `.env` for API keys
2. Build v3 per the proposal in `reference/docs/`
3. To make v3 the default branch: `git branch -D main && git branch -m v3 main && git push -f origin main`
