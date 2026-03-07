"""
Phase C: Web server — FastAPI application serving the public-facing website.

Routes:
  GET  /                    Home page
  GET  /journal             Journal list
  GET  /journal/{cycle}     Single journal entry
  GET  /gallery             Image gallery
  GET  /insights            Insights page
  GET  /api/state           JSON: current state
  GET  /api/journals        JSON: journal list metadata
  GET  /api/tensions        JSON: active tensions
  GET  /api/commitments     JSON: active commitments
  POST /api/submit          Visitor submission endpoint
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import markdown as md_lib
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import (
    BLUESKY_PROFILE_URL,
    DATA_DIR,
    INSTAGRAM_PROFILE_URL,
    SITE_NAME,
    SITE_URL,
    SUBMISSION_MAX_LENGTH,
    SUBMISSION_MIN_LENGTH,
    SUBMISSION_RATE_LIMIT,
    THREADS_PROFILE_URL,
    X_PROFILE_URL,
)

logger = logging.getLogger(__name__)

BLOCKED_HASHES_PATH = DATA_DIR / "injections" / "blocked_ip_hashes.json"
_blocked_hashes: set[str] | None = None


def _load_blocked_hashes() -> set[str]:
    """Load set of blocked IP hashes (lazy, cached)."""
    global _blocked_hashes
    if _blocked_hashes is not None:
        return _blocked_hashes
    _blocked_hashes = set()
    if BLOCKED_HASHES_PATH.exists():
        try:
            data = json.loads(BLOCKED_HASHES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _blocked_hashes = set(data)
            elif isinstance(data, dict) and "hashes" in data:
                _blocked_hashes = set(data["hashes"])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load blocked IP hashes: %s", e)
    return _blocked_hashes


def _add_blocked_hash(ip_hash: str) -> None:
    """Add an IP hash to the blocklist and persist."""
    hashes = _load_blocked_hashes()
    hashes.add(ip_hash)
    BLOCKED_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOCKED_HASHES_PATH.write_text(
        json.dumps(sorted(hashes), indent=0), encoding="utf-8"
    )
    logger.info("Blocked IP hash added to blocklist (harmful submission)")

BASE = Path(__file__).parent.parent

app = FastAPI(title="Meaning Seeker", docs_url=None, redoc_url=None)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Static files and templates
_static_dir = BASE / "static"
_templates_dir = BASE / "templates"
_images_dir = DATA_DIR / "images"

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

if _images_dir.exists():
    app.mount("/images", StaticFiles(directory=str(_images_dir)), name="images")

# Mount favicon/assets at root level matching web.mjs behaviour
_assets_icons = _static_dir / "assets" / "icons"
if _assets_icons.exists():
    app.mount("/icons", StaticFiles(directory=str(_assets_icons)), name="icons")

templates = Jinja2Templates(directory=str(_templates_dir))


def _format_shift_desc(text: str) -> str:
    """Wrap the single-quoted portion of a paradigm shift description in bold double quotes."""
    return re.sub(r"'([^']+)'", r'<strong>"\1"</strong>', text)


templates.env.filters["format_shift_desc"] = _format_shift_desc


# ── Data helpers ───────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _load_state() -> dict:
    raw = _load_json(DATA_DIR / "state.json", {})
    # Determine live status: state file modified within last 15 min
    state_path = DATA_DIR / "state.json"
    if state_path.exists():
        import os
        mtime_ms = state_path.stat().st_mtime * 1000
        raw["_live"] = (time.time() * 1000 - mtime_ms) < 15 * 60 * 1000
        raw["_lastModifiedMs"] = int(mtime_ms)
        raw["_lastModified"] = datetime.fromtimestamp(
            state_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    else:
        raw["_live"] = False
    return raw


def _parse_journal_header(content: str) -> dict:
    """Parse v3 journal header:
      # Title
      **Cycle N** · **Date:** YYYY-MM-DD · **Mode:** mode
      **Tokens:** ...
      *Summary in italics.*   <- optional; used as card excerpt when present
      ---
      body...
    """
    lines = content.split("\n")
    title = ""
    cycle = None
    date_str = ""
    mode = "other"
    tokens_total = 0
    summary_line: str | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
        elif stripped.startswith("**Cycle ") and "**Date:**" in stripped:
            # **Cycle 41** · **Date:** 2026-03-04  **Mode:** sit
            cm = re.search(r"\*\*Cycle\s+(\d+)\*\*", stripped)
            if cm:
                cycle = int(cm.group(1))
            dm = re.search(r"\*\*Date:\*\*\s*([\d-]+)", stripped)
            if dm:
                date_str = dm.group(1)
            mm = re.search(r"\*\*Mode:\*\*\s*(\w+)", stripped)
            if mm:
                mode = mm.group(1).lower()
        elif stripped.startswith("**Tokens:**"):
            tm = re.search(r"\*\*total\*\*\s+([\d,]+)", stripped)
            if tm:
                try:
                    tokens_total = int(tm.group(1).replace(",", ""))
                except ValueError:
                    pass
        elif stripped.startswith("*") and stripped.endswith("*") and len(stripped) > 2:
            # Italic summary line (engine summary, 120–160 chars) — use for card excerpt
            summary_line = stripped[1:-1].strip()
        elif stripped == "---":
            break

    # Extract body (everything after first ---)
    sep = content.find("\n---\n")
    body = content[sep + 5:].strip() if sep != -1 else content

    # Excerpt: prefer parsed summary when present, else first ~220 chars of body
    if summary_line and summary_line.strip():
        excerpt = summary_line.strip()
    else:
        plain = re.sub(r"[#*_`>\[\]!]", "", body).replace("\n", " ").strip()
        plain = re.sub(r"\s+", " ", plain)
        excerpt = plain[:220] + ("…" if len(plain) > 220 else "")

    # Find image in body
    img_match = re.search(r"!\[[^\]]*\]\((/images/[^)]+)\)", body)
    thumbnail_url = img_match.group(1) if img_match else None

    return {
        "title": title,
        "cycle": cycle,
        "date": date_str,
        "mode": mode,
        "excerpt": excerpt,
        "thumbnail_url": thumbnail_url,
        "tokens_total": tokens_total,
        "body_md": body,
    }


def _list_journals() -> list[dict]:
    """Return list of journal metadata dicts, newest first."""
    journal_dir = DATA_DIR / "archive" / "journal"
    if not journal_dir.exists():
        return []
    entries = []
    for path in sorted(journal_dir.glob("*.md"), key=lambda p: p.stem, reverse=True):
        try:
            content = path.read_text(encoding="utf-8")
            meta = _parse_journal_header(content)
            meta["filename"] = path.name
            if meta["cycle"] is None:
                # Fallback: parse from filename
                try:
                    meta["cycle"] = int(path.stem)
                except ValueError:
                    meta["cycle"] = 0
            entries.append(meta)
        except OSError:
            continue
    return entries


def _load_cycle_record(cycle: int) -> dict:
    path = DATA_DIR / "archive" / "cycles" / f"{cycle:06d}.json"
    return _load_json(path, {})


def _load_recent_cycle_records(n: int = 20) -> list[dict]:
    cycles_dir = DATA_DIR / "archive" / "cycles"
    if not cycles_dir.exists():
        return []
    files = sorted(cycles_dir.glob("*.json"), key=lambda p: int(p.stem), reverse=True)[:n]
    records = []
    for path in files:
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def _load_social_replies() -> list[dict]:
    """Load collected social media replies (source x_reply) from quarantine, newest first."""
    quarantine_dir = DATA_DIR / "injections" / "quarantine"
    if not quarantine_dir.exists():
        return []
    replies = []
    for path in quarantine_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("source") == "x_reply":
                replies.append({
                    "text": data.get("text", ""),
                    "timestamp": data.get("timestamp", ""),
                    "source": "x_reply",
                    "platform": "X",
                    "submission_id": data.get("submission_id", ""),
                })
        except (json.JSONDecodeError, OSError):
            continue
    replies.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return replies


def _render_md(text: str) -> str:
    """Render markdown to HTML with extras."""
    return md_lib.markdown(
        text,
        extensions=["extra", "nl2br"],
        output_format="html",
    )


def _md_to_inline_html(text: str) -> str:
    """Render a single line of markdown to HTML (no block elements)."""
    html = md_lib.markdown(text, extensions=[])
    # Strip wrapping <p> tags for inline use
    html = re.sub(r"^<p>|</p>\s*$", "", html.strip())
    return html


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    state = _load_state()
    journals = _list_journals()

    tensions_raw = _load_json(DATA_DIR / "tensions.json", [])
    active_tensions = [t for t in tensions_raw if t.get("status") == "active"]
    commitments_raw = _load_json(DATA_DIR / "commitments.json", [])
    active_commitments = [c for c in commitments_raw if c.get("status") == "active"]
    paradigm_shifts = _load_json(DATA_DIR / "paradigm_shifts.json", [])

    # Token totals from recent cycle records
    total_tokens = 0
    for r in _load_recent_cycle_records(9999):
        total_tokens += (r.get("usage") or {}).get("total_tokens", 0)

    # Latest image entry
    latest_image = next((j for j in journals if j.get("thumbnail_url")), None)

    # Recent entries with excerpt for slider
    slider_entries = [j for j in journals[:10] if j.get("excerpt") or j.get("title")]

    # Manuscript excerpt (first two substantive paragraphs) for home
    manuscript_path = DATA_DIR / "manuscript.md"
    manuscript_excerpt = ""
    if manuscript_path.exists():
        raw = manuscript_path.read_text(encoding="utf-8").strip()
        paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
        taken = []
        for p in paragraphs:
            if p.startswith("#"):
                continue
            taken.append(p)
            if len(taken) >= 2:
                break
        if taken:
            manuscript_excerpt = "\n\n".join(taken)
            if len(manuscript_excerpt) > 900:
                manuscript_excerpt = manuscript_excerpt[:897].rsplit(" ", 1)[0] + "…"
        elif paragraphs:
            manuscript_excerpt = paragraphs[0].lstrip("#").strip()[:500]

    manuscript_excerpt_html = _render_md(manuscript_excerpt) if manuscript_excerpt else ""

    return templates.TemplateResponse("home.html", {
        "request": request,
        "active_nav": "home",
        "state": state,
        "journals": journals,
        "active_tensions": active_tensions,
        "active_commitments": active_commitments,
        "paradigm_shifts": paradigm_shifts,
        "total_tokens": total_tokens,
        "latest_image": latest_image,
        "slider_entries": slider_entries,
        "manuscript_excerpt": manuscript_excerpt,
        "manuscript_excerpt_html": manuscript_excerpt_html,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/journal", response_class=HTMLResponse)
async def journal_list(request: Request):
    journals = _list_journals()
    return templates.TemplateResponse("journal_list.html", {
        "request": request,
        "active_nav": "journal",
        "journals": journals,
        "journals_json": json.dumps(journals),
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/journal/{cycle:int}", response_class=HTMLResponse)
async def journal_entry(request: Request, cycle: int):
    path = DATA_DIR / "archive" / "journal" / f"{cycle:06d}.md"
    if not path.exists():
        raise HTTPException(404, "Journal entry not found")

    content = path.read_text(encoding="utf-8")
    meta = _parse_journal_header(content)
    # When we show the entry image full-width above, omit it from the body to avoid duplicate
    body_md = meta["body_md"]
    if meta.get("thumbnail_url"):
        body_md = re.sub(r"\n*!\[[^\]]*\]\([^)]+\)\n*", "\n\n", body_md, count=1).strip()
    body_html = _render_md(body_md)

    # Load cycle record for social output, injection info
    cycle_record = _load_cycle_record(cycle)

    # Previous/next
    journals = _list_journals()
    cycles_sorted = sorted([j["cycle"] for j in journals if j.get("cycle")])
    idx = cycles_sorted.index(cycle) if cycle in cycles_sorted else -1
    prev_cycle = cycles_sorted[idx - 1] if idx > 0 else None
    next_cycle = cycles_sorted[idx + 1] if idx >= 0 and idx < len(cycles_sorted) - 1 else None

    prev_meta = None
    next_meta = None
    if prev_cycle:
        pm = next((j for j in journals if j["cycle"] == prev_cycle), None)
        if pm:
            prev_meta = pm
    if next_cycle:
        nm = next((j for j in journals if j["cycle"] == next_cycle), None)
        if nm:
            next_meta = nm

    image_decision = (cycle_record or {}).get("image_decision") or {}
    image_prompt = image_decision.get("prompt") or ""
    image_art_direction = {
        "beyond_words": image_decision.get("beyond_words") or "",
        "visual_energy": image_decision.get("visual_energy") or "",
        "texture": image_decision.get("texture") or "",
        "palette": image_decision.get("palette") or "",
        "temperature": image_decision.get("temperature") or "",
        "prompt": image_prompt,
        # Legacy fields for old cycle records
        "style": image_decision.get("style") or "",
        "medium": image_decision.get("medium") or "",
        "artist_reference": image_decision.get("artist_reference") or "",
        "concept": image_decision.get("concept") or "",
        "why": image_decision.get("why") or "",
    }

    return templates.TemplateResponse("journal_entry.html", {
        "request": request,
        "active_nav": "journal",
        "meta": meta,
        "body_html": body_html,
        "cycle": cycle,
        "cycle_record": cycle_record,
        "prev_meta": prev_meta,
        "next_meta": next_meta,
        "image_prompt": image_prompt,
        "image_art_direction": image_art_direction,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/manuscript", response_class=HTMLResponse)
async def manuscript_page(request: Request):
    manuscript_path = DATA_DIR / "manuscript.md"
    text = manuscript_path.read_text(encoding="utf-8").strip() if manuscript_path.exists() else ""
    body_html = _render_md(text) if text else ""
    last_updated = None
    if manuscript_path.exists():
        mtime = manuscript_path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        last_updated = dt.strftime("%d %b %Y").lstrip("0")
    return templates.TemplateResponse("manuscript.html", {
        "request": request,
        "active_nav": "manuscript",
        "body_html": body_html,
        "manuscript_last_updated": last_updated,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/gallery", response_class=HTMLResponse)
async def gallery(request: Request):
    journals = _list_journals()
    gallery_items = []
    for j in journals:
        if not j.get("thumbnail_url"):
            continue
        cycle_record = _load_cycle_record(j["cycle"]) if j.get("cycle") is not None else {}
        image_decision = cycle_record.get("image_decision") or {}
        prompt = image_decision.get("prompt") or ""
        gallery_items.append({
            "cycle": j["cycle"],
            "mode": j["mode"],
            "date": j["date"],
            "title": j.get("title", ""),
            "thumbnail_url": j["thumbnail_url"],
            "prompt": prompt,
            "beyond_words": image_decision.get("beyond_words") or "",
            "visual_energy": image_decision.get("visual_energy") or "",
            "texture": image_decision.get("texture") or "",
            "palette": image_decision.get("palette") or "",
            "temperature": image_decision.get("temperature") or "",
            # Legacy
            "style": image_decision.get("style") or "",
            "medium": image_decision.get("medium") or "",
            "artist_reference": image_decision.get("artist_reference") or "",
            "concept": image_decision.get("concept") or "",
            "why": image_decision.get("why") or "",
        })
    return templates.TemplateResponse("gallery.html", {
        "request": request,
        "active_nav": "gallery",
        "gallery_items": gallery_items,
        "gallery_items_json": json.dumps(gallery_items),
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/insights", response_class=HTMLResponse)
async def insights(request: Request):
    state = _load_state()
    tensions_raw = _load_json(DATA_DIR / "tensions.json", [])
    commitments_raw = _load_json(DATA_DIR / "commitments.json", [])
    paradigm_shifts = _load_json(DATA_DIR / "paradigm_shifts.json", [])
    creator_observations = _load_json(DATA_DIR / "creator_observations.json", [])
    recent_cycles = _load_recent_cycle_records(20)

    current_cycle = state.get("cycle", 0)
    active_tensions = sorted(
        [t for t in tensions_raw if t.get("status") == "active"],
        key=lambda t: t.get("created_cycle", 0),
    )
    # Annotate with age
    for t in active_tensions:
        t["age_cycles"] = current_cycle - t.get("created_cycle", 0)

    recently_resolved = sorted(
        [t for t in tensions_raw if t.get("status") == "resolved"],
        key=lambda t: (t.get("resolution") or {}).get("cycle", 0),
        reverse=True,
    )[:5]
    # Add resolution date for each (from cycle record)
    for t in recently_resolved:
        res_cycle = (t.get("resolution") or {}).get("cycle")
        if res_cycle is not None:
            rec = _load_cycle_record(res_cycle)
            t["resolution_date"] = (rec.get("timestamp") or "")[:10]
        else:
            t["resolution_date"] = ""

    active_commitments = [c for c in commitments_raw if c.get("status") == "active"]

    # Oldest active tension for letterbox context
    letterbox_tension = active_tensions[0] if active_tensions else None

    return templates.TemplateResponse("insights.html", {
        "request": request,
        "active_nav": "insights",
        "state": state,
        "active_tensions": active_tensions,
        "recently_resolved": recently_resolved,
        "active_commitments": active_commitments,
        "paradigm_shifts": paradigm_shifts,
        "creator_observations": creator_observations,
        "recent_cycles": recent_cycles,
        "letterbox_tension": letterbox_tension,
        "current_cycle": current_cycle,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/challenge", response_class=HTMLResponse)
async def challenge_page(request: Request):
    tensions_raw = _load_json(DATA_DIR / "tensions.json", [])
    state = _load_state()
    current_cycle = state.get("cycle", 0)
    active_tensions = sorted(
        [t for t in tensions_raw if t.get("status") == "active"],
        key=lambda t: t.get("created_cycle", 0),
    )
    for t in active_tensions:
        t["age_cycles"] = current_cycle - t.get("created_cycle", 0)
    letterbox_tension = active_tensions[0] if active_tensions else None
    social_replies = _load_social_replies()
    social_profile_links = []
    if X_PROFILE_URL.strip():
        social_profile_links.append({"name": "X", "url": X_PROFILE_URL.strip()})
    if BLUESKY_PROFILE_URL.strip():
        social_profile_links.append({"name": "Bluesky", "url": BLUESKY_PROFILE_URL.strip()})
    if THREADS_PROFILE_URL.strip():
        social_profile_links.append({"name": "Threads", "url": THREADS_PROFILE_URL.strip()})
    if INSTAGRAM_PROFILE_URL.strip():
        social_profile_links.append({"name": "Instagram", "url": INSTAGRAM_PROFILE_URL.strip()})
    return templates.TemplateResponse("challenge.html", {
        "request": request,
        "active_nav": "challenge",
        "letterbox_tension": letterbox_tension,
        "social_replies": social_replies,
        "social_profile_links": social_profile_links,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


# ── JSON API routes ─────────────────────────────────────────────────────────────

@app.get("/api/state")
async def api_state():
    state = _load_state()
    tensions_raw = _load_json(DATA_DIR / "tensions.json", [])
    commitments_raw = _load_json(DATA_DIR / "commitments.json", [])
    paradigm_shifts = _load_json(DATA_DIR / "paradigm_shifts.json", [])

    active_tensions = [t for t in tensions_raw if t.get("status") == "active"]
    active_commitments = [c for c in commitments_raw if c.get("status") == "active"]

    state["activeTensions"] = active_tensions
    state["activeTensionsCount"] = len(active_tensions)
    state["coreClaimsCount"] = len(active_commitments)
    state["paradigmShifts"] = paradigm_shifts

    # Total tokens across all cycles
    total_tokens = 0
    for r in _load_recent_cycle_records(9999):
        total_tokens += (r.get("usage") or {}).get("total_tokens", 0)
    state["totalTokensIn"] = total_tokens
    state["totalTokensOut"] = 0

    # Latest manuscript as "thesis"
    manuscript_path = DATA_DIR / "manuscript.md"
    if manuscript_path.exists():
        manuscript = manuscript_path.read_text(encoding="utf-8").strip()
        # Use first paragraph as thesis
        paragraphs = [p.strip() for p in manuscript.split("\n\n") if p.strip()]
        state["thesis"] = paragraphs[0][:500] if paragraphs else ""

    return JSONResponse(state)


@app.get("/api/journals")
async def api_journals():
    journals = _list_journals()
    return JSONResponse(journals)


@app.get("/api/tensions")
async def api_tensions():
    tensions = _load_json(DATA_DIR / "tensions.json", [])
    return JSONResponse(tensions)


@app.get("/api/commitments")
async def api_commitments():
    commitments = _load_json(DATA_DIR / "commitments.json", [])
    return JSONResponse(commitments)


@app.get("/api/cycles")
async def api_cycles():
    records = _load_recent_cycle_records(20)
    return JSONResponse(records)


# ── Submission endpoint (Task 3) ───────────────────────────────────────────────

@app.post("/api/submit")
@limiter.limit(SUBMISSION_RATE_LIMIT)
async def submit_challenge(
    request: Request,
    challenge: str = Form(...),
    website: str = Form(""),  # honeypot
):
    # Honeypot check — bots fill this hidden field
    if website.strip():
        return JSONResponse({"status": "ok", "message": "Thank you for your contribution."})

    # Blocklist check — reject banned IPs before any other processing
    ip = get_remote_address(request) or "unknown"
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    if ip_hash in _load_blocked_hashes():
        raise HTTPException(
            403,
            detail="Your access to this form has been restricted due to a previous violation of our community guidelines.",
        )

    text = challenge.strip()
    if len(text) < SUBMISSION_MIN_LENGTH:
        raise HTTPException(400, f"Submission must be at least {SUBMISSION_MIN_LENGTH} characters.")
    if len(text) > SUBMISSION_MAX_LENGTH:
        raise HTTPException(400, f"Submission must be at most {SUBMISSION_MAX_LENGTH} characters.")

    submission = {
        "submission_id": f"sub-{int(time.time() * 1000)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "ip_hash": ip_hash,
        "source": "website",
        "filter_scores": None,
        "status": "pending",
        "rejection_reason": None,
    }

    # Hygiene check at submit time: toxicity and prompt injection → block IP and reject
    from . import filtering
    hygiene = filtering._hygiene_check(submission)
    if not hygiene["pass"]:
        reason = hygiene.get("reason", "")
        if reason in ("toxicity", "prompt_injection"):
            _add_blocked_hash(ip_hash)
            raise HTTPException(
                403,
                detail="Your submission was rejected for violating our community guidelines (harmful or abusive content). Your access to this form has been restricted.",
            )
        if reason == "duplicate":
            raise HTTPException(400, detail="You have already submitted this or a very similar challenge.")
        if reason == "invalid_encoding":
            raise HTTPException(400, detail="Submission contained invalid characters. Please use plain text.")
        raise HTTPException(400, detail="Submission did not meet our guidelines. Please try again.")

    quarantine_dir = DATA_DIR / "injections" / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    path = quarantine_dir / f"{submission['submission_id']}.json"
    path.write_text(json.dumps(submission, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Submission received: %s", submission["submission_id"])
    return JSONResponse({"status": "ok", "message": "Thank you for your contribution."})


# ── Favicon / asset shortcuts ──────────────────────────────────────────────────

@app.get("/favicon.ico")
async def favicon_ico():
    path = _static_dir / "assets" / "icons" / "favicon.ico"
    if path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(path))
    raise HTTPException(404)


@app.get("/favicon.svg")
async def favicon_svg():
    path = _static_dir / "assets" / "icons" / "favicon.svg"
    if path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(path))
    raise HTTPException(404)


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    path = _static_dir / "assets" / "icons" / "apple-touch-icon.png"
    if path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(path))
    raise HTTPException(404)


@app.get("/site.webmanifest")
async def webmanifest():
    path = _static_dir / "assets" / "site.webmanifest"
    if path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(path), media_type="application/manifest+json")
    raise HTTPException(404)


@app.get("/assets/TheMeaningSeeker.jpg")
async def og_image():
    path = _static_dir / "assets" / "TheMeaningSeeker.jpg"
    if path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(path), media_type="image/jpeg")
    raise HTTPException(404)


# ── Entry point ────────────────────────────────────────────────────────────────

def serve():
    import uvicorn
    from .config import WEB_HOST, WEB_PORT
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)


if __name__ == "__main__":
    serve()
