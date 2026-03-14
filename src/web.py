"""
Phase C: Web server — FastAPI application serving the public-facing website.

Routes:
  GET  /                    Home page
  GET  /journal             Journal list
  GET  /journal/{cycle}     Single journal entry
  GET  /gallery             Image gallery
  GET  /about               About page
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
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import markdown as md_lib
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import resistance
from .art_direction import ART_DIRECTION_PROMPT
from .config import (
    ADMIN_PASSWORD,
    ADMIN_SALT,
    BLUESKY_PROFILE_URL,
    CYCLE_DAILY_AT_UTC_PARSED,
    CYCLE_INTERVAL_SECONDS,
    MAX_REVIEW_CHALLENGES,
    CSRF_SECRET,
    DATA_DIR,
    IMAGE_MODEL,
    INSTAGRAM_PROFILE_URL,
    MODEL_ID,
    MONITOR_MODEL_ID,
    SITE_NAME,
    SITE_URL,
    SUBMISSION_MAX_LENGTH,
    SUBMISSION_MIN_LENGTH,
    SUBMISSION_RATE_LIMIT,
    THREADS_PROFILE_URL,
    VPS_MONTHLY_ESTIMATE_EUR,
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


_csrf_serializer: URLSafeTimedSerializer | None = None


def _get_csrf_serializer() -> URLSafeTimedSerializer:
    global _csrf_serializer
    if _csrf_serializer is None:
        _csrf_serializer = URLSafeTimedSerializer(CSRF_SECRET, salt="csrf")
        if CSRF_SECRET == "dev-only-change-in-production":
            logger.warning("CSRF_SECRET not set — using default. Set CSRF_SECRET in production.")
    return _csrf_serializer


def _generate_csrf_token(max_age_seconds: int = 3600) -> str:
    """Generate a time-limited CSRF token (valid for max_age_seconds)."""
    payload = secrets.token_hex(16)
    return _get_csrf_serializer().dumps(payload)


def _verify_csrf_token(token: str, max_age_seconds: int = 3600) -> bool:
    """Verify a CSRF token; returns False if invalid or expired."""
    if not token or not token.strip():
        return False
    try:
        _get_csrf_serializer().loads(token, max_age=max_age_seconds)
        return True
    except BadSignature:
        return False


# ── Admin auth ─────────────────────────────────────────────────────────────────

_admin_serializer: URLSafeTimedSerializer | None = None
ADMIN_COOKIE = "admin_session"
ADMIN_SESSION_MAX_AGE = 86400  # 24 hours


def _get_admin_serializer() -> URLSafeTimedSerializer:
    global _admin_serializer
    if _admin_serializer is None:
        _admin_serializer = URLSafeTimedSerializer(ADMIN_SALT, salt="admin")
    return _admin_serializer


def _verify_admin_password(password: str) -> bool:
    """Verify admin password. Returns False if ADMIN_PASSWORD not set."""
    if not ADMIN_PASSWORD:
        return False
    return secrets.compare_digest(password, ADMIN_PASSWORD)


def _create_admin_session() -> str:
    """Create a signed admin session token."""
    payload = secrets.token_hex(16)
    return _get_admin_serializer().dumps(payload)


def _verify_admin_session(token: str) -> bool:
    """Verify admin session cookie. Returns True if valid."""
    if not token or not token.strip():
        return False
    try:
        _get_admin_serializer().loads(token, max_age=ADMIN_SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


class _AdminAuthRequired(Exception):
    """Raised when admin route requires authentication."""


async def _require_admin(request: Request) -> None:
    """Dependency: raise redirect to /admin if not logged in."""
    token = request.cookies.get(ADMIN_COOKIE, "")
    if not _verify_admin_session(token):
        raise _AdminAuthRequired()


def _fmt_tokens(n: int) -> str:
    """Format token count for display (e.g. 15000 -> 15k)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _usage_fmt(u: dict | None) -> str:
    """Format usage dict as p/c/t."""
    if not u:
        return "—"
    p = u.get("prompt_tokens", 0) or 0
    c = u.get("completion_tokens", 0) or 0
    t = u.get("total_tokens", 0) or 0
    if t == 0:
        return "—"
    return f"{_fmt_tokens(p)}/{_fmt_tokens(c)}/{_fmt_tokens(t)}"


def _date_dmy(iso_date: str) -> str:
    """Convert YYYY-MM-DD to DD-MM-YYYY."""
    if not iso_date or len(iso_date) < 10:
        return iso_date
    y, m, d = iso_date[:10].split("-")
    return f"{d}-{m}-{y}"


def _week_monday_sunday(sunday_iso: str) -> tuple[str, str]:
    """Given Sunday YYYY-MM-DD, return (monday_iso, sunday_iso) for that week."""
    if not sunday_iso or len(sunday_iso) < 10:
        return ("", sunday_iso)
    try:
        dt = datetime.strptime(sunday_iso[:10], "%Y-%m-%d")
        monday = dt - timedelta(days=6)
        return (monday.strftime("%Y-%m-%d"), sunday_iso[:10])
    except (ValueError, TypeError):
        return ("", sunday_iso[:10])


def _build_admin_token_rows(cycle_records: list[dict]) -> list[dict]:
    """Build token table rows: cycle rows + weekly summary rows after each weekly_review.
    Weekly summary appears after the Sunday weekly_review and sums all cycles whose date
    falls within that week (Monday through Sunday).
    """
    if not cycle_records:
        return []

    rows: list[dict] = []
    for i, rec in enumerate(cycle_records):
        usage = rec.get("usage") or {}
        inv = usage.get("inquiry") or {}
        mon = usage.get("monitoring") or {}
        img = usage.get("image") or {}
        total = usage.get("total_tokens", 0) or (
            inv.get("total_tokens", 0) + mon.get("total_tokens", 0) + img.get("total_tokens", 0)
        )

        injection = rec.get("injection") or {}
        mode = rec.get("mode") or (injection.get("source") or "explore")
        if mode == "weekly_review":
            mode = "weekly_review"
        model_id = rec.get("model_id") or ""
        if "/" in model_id:
            model_id = model_id.split("/")[-1]
        image_model = rec.get("image_model") or ""
        if "/" in image_model:
            image_model = image_model.split("/")[-1]

        # Image: show tokens when present; show image_model when an image was generated (even if usage not recorded)
        img_tokens = _usage_fmt(img) if (img.get("total_tokens") or img.get("prompt_tokens")) else "—"
        if image_model:
            img_display = f"{img_tokens} ({image_model})" if img_tokens != "—" else f"— ({image_model})"
        else:
            img_display = img_tokens

        rows.append({
            "cycle": rec.get("cycle"),
            "date": _date_dmy((rec.get("timestamp") or "")[:10]),
            "mode": mode,
            "model": model_id,
            "inquiry": _usage_fmt(inv),
            "monitoring": _usage_fmt(mon),
            "image": img_display,
            "total": _fmt_tokens(total),
            "is_week_summary": False,
            "usage": usage,
        })

        # After each weekly_review (Sunday), insert week summary for that week
        if injection.get("source") == "weekly_review":
            sunday_iso = (rec.get("timestamp") or "")[:10]
            monday_iso, sunday_iso = _week_monday_sunday(sunday_iso)
            week_records = [
                r for r in cycle_records
                if monday_iso <= (r.get("timestamp") or "")[:10] <= sunday_iso
            ]

            sum_inv = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            sum_mon = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            sum_img = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            for wr in week_records:
                u = wr.get("usage") or {}
                for acc, key in [(sum_inv, "inquiry"), (sum_mon, "monitoring"), (sum_img, "image")]:
                    part = u.get(key) or {}
                    acc["prompt_tokens"] += part.get("prompt_tokens", 0) or 0
                    acc["completion_tokens"] += part.get("completion_tokens", 0) or 0
                    acc["total_tokens"] += part.get("total_tokens", 0) or 0
            week_total = sum_inv["total_tokens"] + sum_mon["total_tokens"] + sum_img["total_tokens"]
            week_date = _date_dmy(sunday_iso)

            rows.append({
                "cycle": None,
                "date": f"Week ending {week_date}",
                "mode": None,
                "model": None,
                "inquiry": _usage_fmt(sum_inv),
                "monitoring": _usage_fmt(sum_mon),
                "image": _usage_fmt(sum_img) if sum_img["total_tokens"] else "—",
                "total": _fmt_tokens(week_total),
                "is_week_summary": True,
            })

    return rows


BASE = Path(__file__).parent.parent

app = FastAPI(title="Meaning Seeker", docs_url=None, redoc_url=None)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(_AdminAuthRequired)
async def _admin_auth_handler(request: Request, exc: _AdminAuthRequired):
    return RedirectResponse(url="/admin", status_code=302)

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


def _static_version() -> str:
    """Cache-busting version for static assets. Prefer STATIC_VERSION env; else use style.css mtime."""
    if os.environ.get("STATIC_VERSION"):
        return os.environ["STATIC_VERSION"]
    style_css = _static_dir / "style.css"
    if style_css.exists():
        return str(int(style_css.stat().st_mtime))
    return "0"


templates.env.globals["static_version"] = _static_version()


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
    # Live status: state modified within live window (min 15 min, or full cycle interval for long cycles)
    state_path = DATA_DIR / "state.json"
    if state_path.exists():
        import os
        mtime_ms = state_path.stat().st_mtime * 1000
        live_window_ms = max(15 * 60, CYCLE_INTERVAL_SECONDS) * 1000
        raw["_live"] = (time.time() * 1000 - mtime_ms) < live_window_ms
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
    woven = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
        elif stripped.startswith("**Cycle ") and "**Date:**" in stripped:
            cm = re.search(r"\*\*Cycle\s+(\d+)\*\*", stripped)
            if cm:
                cycle = int(cm.group(1))
            dm = re.search(r"\*\*Date:\*\*\s*([\d-]+)", stripped)
            if dm:
                date_str = dm.group(1)
            mm = re.search(r"\*\*Mode:\*\*\s*([\w-]+)", stripped)
            if mm:
                mode = mm.group(1).lower()
            if "**Woven:**" in stripped and re.search(r"\*\*Woven:\*\*\s+sub-[\w-]+", stripped):
                woven = True
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
        "woven": woven,
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


def _score_to_band(score: int | None) -> tuple[str, str]:
    """Map numeric score to (band, band_key)."""
    if score is None:
        return "Accepted", "accepted"
    if score >= 9:
        return "Highly relevant", "high"
    if score >= 7:
        return "Relevant", "relevant"
    if score >= 5:
        return "Accepted", "accepted"
    return "Low relevance", "low"


def _load_challenge_queue_context(state: dict) -> dict:
    """Build pending_challenges, encountered_challenges, queue_stats for challenge page."""
    challenges = resistance.load_human_challenges()
    last_review = state.get("last_review_at") or ""

    pending = sorted(
        [c for c in challenges if c.get("status") == "pending"],
        key=lambda c: c.get("submitted_at") or "",
        reverse=True,
    )
    for c in pending:
        score = c.get("score")
        c["score_band"], c["score_band_key"] = _score_to_band(score)

    encountered = sorted(
        [c for c in challenges if c.get("status") in ("woven", "reviewed")],
        key=lambda c: c.get("resolved_at") or c.get("submitted_at") or "",
        reverse=True,
    )[:50]

    lapsed = sum(
        1 for c in challenges
        if c.get("status") == "lapsed"
        and (c.get("resolved_at") or "") >= last_review
    ) if last_review else 0

    return {
        "pending_challenges": pending,
        "encountered_challenges": encountered,
        "queue_stats": {
            "pending": len(pending),
            "encountered": len([c for c in challenges if c.get("status") in ("woven", "reviewed")]),
            "lapsed": lapsed,
        },
        "max_review_challenges": MAX_REVIEW_CHALLENGES,
    }


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

    social_profile_links = []
    if X_PROFILE_URL.strip():
        social_profile_links.append({"name": "X", "url": X_PROFILE_URL.strip()})
    if BLUESKY_PROFILE_URL.strip():
        social_profile_links.append({"name": "Bluesky", "url": BLUESKY_PROFILE_URL.strip()})
    if THREADS_PROFILE_URL.strip():
        social_profile_links.append({"name": "Threads", "url": THREADS_PROFILE_URL.strip()})
    if INSTAGRAM_PROFILE_URL.strip():
        social_profile_links.append({"name": "Instagram", "url": INSTAGRAM_PROFILE_URL.strip()})

    # Next run: last activity + cycle interval, or next daily-at UTC (for ticker countdown)
    last_ms = state.get("_lastModifiedMs") or 0
    if CYCLE_DAILY_AT_UTC_PARSED:
        hour, minute = CYCLE_DAILY_AT_UTC_PARSED
        now = datetime.now(timezone.utc)
        today_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        next_run = today_at if now < today_at else today_at + timedelta(days=1)
        next_run_at_ms = int(next_run.timestamp() * 1000)
    else:
        next_run_at_ms = last_ms + CYCLE_INTERVAL_SECONDS * 1000 if last_ms else 0

    return templates.TemplateResponse("home.html", {
        "request": request,
        "active_nav": "home",
        "state": state,
        "cycle_interval_seconds": CYCLE_INTERVAL_SECONDS,
        "next_run_at_ms": next_run_at_ms,
        "journals": journals,
        "active_tensions": active_tensions,
        "active_commitments": active_commitments,
        "paradigm_shifts": paradigm_shifts,
        "total_tokens": total_tokens,
        "latest_image": latest_image,
        "slider_entries": slider_entries,
        "manuscript_excerpt": manuscript_excerpt,
        "manuscript_excerpt_html": manuscript_excerpt_html,
        "social_profile_links": social_profile_links,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


def _journal_mode_counts(journals: list) -> dict[str, int]:
    """Count journal entries per mode for filter bar. Keys: all, explore, synthesize, critique, evolve, sit, confess, weekly_review, other."""
    known = {"explore", "synthesize", "critique", "evolve", "sit", "confess", "weekly_review"}
    counts: dict[str, int] = {
        "all": len(journals),
        "explore": 0,
        "synthesize": 0,
        "critique": 0,
        "evolve": 0,
        "sit": 0,
        "confess": 0,
        "weekly_review": 0,
        "other": 0,
    }
    for j in journals:
        mode = (j.get("mode") or "").strip().lower()
        if mode in known:
            counts[mode] = counts.get(mode, 0) + 1
        elif mode:
            counts["other"] += 1
    return counts


@app.get("/journal", response_class=HTMLResponse)
async def journal_list(request: Request):
    journals = _list_journals()
    mode_counts = _journal_mode_counts(journals)
    social_profile_links = []
    if X_PROFILE_URL.strip():
        social_profile_links.append({"name": "X", "url": X_PROFILE_URL.strip()})
    if BLUESKY_PROFILE_URL.strip():
        social_profile_links.append({"name": "Bluesky", "url": BLUESKY_PROFILE_URL.strip()})
    if THREADS_PROFILE_URL.strip():
        social_profile_links.append({"name": "Threads", "url": THREADS_PROFILE_URL.strip()})
    if INSTAGRAM_PROFILE_URL.strip():
        social_profile_links.append({"name": "Instagram", "url": INSTAGRAM_PROFILE_URL.strip()})
    return templates.TemplateResponse("journal_list.html", {
        "request": request,
        "active_nav": "journal",
        "journals": journals,
        "journals_json": json.dumps(journals),
        "mode_counts": mode_counts,
        "social_profile_links": social_profile_links,
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
    # Split before tensions section for CTA placement
    tensions_match = re.search(
        r"\n---\s*\n+\s*### (?:New tensions carried forward|Tensions resolved this cycle|Transition)\b",
        body_md,
    )
    if tensions_match:
        body_before_md = body_md[: tensions_match.start()].strip()
        body_after_md = body_md[tensions_match.start() :].lstrip()
        body_before_html = _render_md(body_before_md)
        body_after_html = _render_md(body_after_md)
    else:
        body_before_html = _render_md(body_md)
        body_after_html = None

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
        "body_before_html": body_before_html,
        "body_after_html": body_after_html,
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
        "art_direction_prompt": ART_DIRECTION_PROMPT,
        "inquiry_model": MODEL_ID,
        "image_model": IMAGE_MODEL,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
    })


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    social_profile_links = []
    if X_PROFILE_URL.strip():
        social_profile_links.append({"name": "X", "url": X_PROFILE_URL.strip()})
    if BLUESKY_PROFILE_URL.strip():
        social_profile_links.append({"name": "Bluesky", "url": BLUESKY_PROFILE_URL.strip()})
    if THREADS_PROFILE_URL.strip():
        social_profile_links.append({"name": "Threads", "url": THREADS_PROFILE_URL.strip()})
    if INSTAGRAM_PROFILE_URL.strip():
        social_profile_links.append({"name": "Instagram", "url": INSTAGRAM_PROFILE_URL.strip()})

    # Running costs: cumulative LLM (OpenRouter) from cycle records; VPS from config estimate
    llm_cost_total = 0.0
    first_cycle_ts: str | None = None
    for r in _load_recent_cycle_records(9999):
        llm_cost_total += float((r.get("usage") or {}).get("total_cost", 0) or 0)
        ts = (r.get("timestamp") or "")[:10]
        if ts and (first_cycle_ts is None or ts < first_cycle_ts):
            first_cycle_ts = ts

    vps_monthly_eur = VPS_MONTHLY_ESTIMATE_EUR
    vps_cumulative_eur: float | None = None
    if vps_monthly_eur is not None and vps_monthly_eur > 0 and first_cycle_ts:
        try:
            start = datetime.strptime(first_cycle_ts, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            months = max(0, (now - start).days / 30.0)
            vps_cumulative_eur = round(vps_monthly_eur * months, 2)
        except ValueError:
            pass

    return templates.TemplateResponse("about.html", {
        "request": request,
        "active_nav": "about",
        "social_profile_links": social_profile_links,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
        "llm_cost_total": llm_cost_total,
        "vps_monthly_eur": vps_monthly_eur,
        "vps_cumulative_eur": vps_cumulative_eur,
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
    queue_ctx = _load_challenge_queue_context(state)
    return templates.TemplateResponse("challenge.html", {
        "request": request,
        "active_nav": "challenge",
        "letterbox_tension": letterbox_tension,
        "social_replies": social_replies,
        "social_profile_links": social_profile_links,
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
        "csrf_token": _generate_csrf_token(),
        **queue_ctx,
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


# ── Admin routes ───────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Login page or redirect to dashboard if already logged in."""
    token = request.cookies.get(ADMIN_COOKIE, "")
    if _verify_admin_session(token):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse("admin_login.html", {
        "request": request,
        "site_name": SITE_NAME,
    })


@app.post("/admin/login")
@limiter.limit("5/minute")
async def admin_login(request: Request, password: str = Form("")):
    """Verify password, set session cookie, redirect to dashboard."""
    if not _verify_admin_password(password.strip()):
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "site_name": SITE_NAME,
            "error": "Invalid password.",
        }, status_code=401)
    session_token = _create_admin_session()
    response = RedirectResponse(url="/admin/dashboard", status_code=302)
    response.set_cookie(
        key=ADMIN_COOKIE,
        value=session_token,
        max_age=ADMIN_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request):
    """Clear session, redirect to /admin."""
    response = RedirectResponse(url="/admin", status_code=302)
    response.delete_cookie(ADMIN_COOKIE)
    return response


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: None = Depends(_require_admin)):
    """Main dashboard: inquiry stats, token table, GoAccess iframe, challenges."""
    state = _load_state()
    journals = _list_journals()
    tensions_raw = _load_json(DATA_DIR / "tensions.json", [])
    active_tensions = [t for t in tensions_raw if t.get("status") == "active"]
    commitments_raw = _load_json(DATA_DIR / "commitments.json", [])
    active_commitments = [c for c in commitments_raw if c.get("status") == "active"]

    total_tokens = 0
    cycle_records = _load_recent_cycle_records(9999)
    for r in cycle_records:
        total_tokens += (r.get("usage") or {}).get("total_tokens", 0)

    # Build token table rows (cycles + weekly summary rows)
    token_rows = _build_admin_token_rows(cycle_records)

    # Challenges by state
    challenges = resistance.load_human_challenges()
    rejected = resistance.load_rejected_challenges()
    quarantine_dir = DATA_DIR / "injections" / "quarantine"
    quarantine_entries = []
    if quarantine_dir.exists():
        for path in quarantine_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") == "pending":
                    quarantine_entries.append(data)
            except (json.JSONDecodeError, OSError):
                continue
    quarantine_entries.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    challenges_by_state = {
        "pending": [c for c in challenges if c.get("status") == "pending"],
        "woven": [c for c in challenges if c.get("status") == "woven"],
        "reviewed": [c for c in challenges if c.get("status") == "reviewed"],
        "lapsed": [c for c in challenges if c.get("status") == "lapsed"],
        "rejected": rejected,
        "quarantine": quarantine_entries,
    }

    has_goaccess_report = (DATA_DIR / "admin" / "goaccess_report.html").exists()

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "site_name": SITE_NAME,
        "cycles_complete": len(journals),
        "active_tensions": active_tensions,
        "active_commitments": active_commitments,
        "total_tokens": total_tokens,
        "token_rows": token_rows,
        "challenges_by_state": challenges_by_state,
        "monitor_model_id": MONITOR_MODEL_ID,
        "has_goaccess_report": has_goaccess_report,
    })


@app.get("/admin/stats")
async def admin_stats(request: Request, _: None = Depends(_require_admin)):
    """Serve GoAccess HTML report (or placeholder if not yet generated)."""
    report_path = DATA_DIR / "admin" / "goaccess_report.html"
    if report_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(report_path), media_type="text/html")
    raise HTTPException(404, detail="GoAccess report not yet generated. Run scripts/goaccess_report.sh.")


# ── Submission endpoint (Task 3) ───────────────────────────────────────────────

@app.post("/api/submit")
@limiter.limit(SUBMISSION_RATE_LIMIT)
async def submit_challenge(
    request: Request,
    challenge: str = Form(...),
    submitter_name: str = Form(""),
    website: str = Form(""),  # honeypot
    csrf_token: str = Form(""),
):
    # CSRF check
    if not _verify_csrf_token(csrf_token):
        raise HTTPException(403, detail="Invalid or expired form. Please refresh the page and try again.")

    # Honeypot check
    if website.strip():
        return JSONResponse({"status": "ok", "message": "Thank you for your contribution."})

    # Blocklist check
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

    submission_id = f"sub-{int(time.time() * 1000)}"
    from . import filtering
    result = filtering.process_single_submission(
        text=text,
        submitter_name=submitter_name.strip()[:50] if submitter_name else "",
        submission_id=submission_id,
    )

    if result.get("block_ip"):
        _add_blocked_hash(ip_hash)
        raise HTTPException(
            403,
            detail="Your submission was rejected for violating our community guidelines (harmful or abusive content). Your access to this form has been restricted.",
        )

    logger.info("Submission %s: %s", submission_id, result.get("status", "?"))
    return JSONResponse(result)


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
