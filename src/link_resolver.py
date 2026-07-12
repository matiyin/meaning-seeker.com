"""
Resolve named people and works mentioned in journal entries to external sources,
maintain a link dictionary, and annotate journal markdown at render time.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from .config import BASE_DIR, DATA_DIR, MONITOR_MODEL_ID, API_BASE_URL, API_KEY
from .models import LinkCandidate

logger = logging.getLogger(__name__)

SEED_PATH = BASE_DIR / "seed" / "link_dictionary_seed.json"
DICTIONARY_PATH = DATA_DIR / "link_dictionary.json"

WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/api.php"

SUMMARY_MAX_LEN = 320
REQUEST_TIMEOUT = 12
WIKIPEDIA_RATE_DELAY = 0.35

_client = None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    return slug.strip("-") or "entity"


def _truncate_summary(text: str, max_len: int = SUMMARY_MAX_LEN) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def _normalize_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def load_seed() -> dict:
    if not SEED_PATH.exists():
        return {}
    try:
        data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load link seed %s: %s", SEED_PATH, e)
        return {}


def load_dictionary() -> dict:
    seed = load_seed()
    dictionary: dict = {}
    for key, entry in seed.items():
        if isinstance(entry, dict):
            dictionary[key] = dict(entry)
    if DICTIONARY_PATH.exists():
        try:
            runtime = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
            if isinstance(runtime, dict):
                for key, entry in runtime.items():
                    if isinstance(entry, dict):
                        dictionary[key] = {**dictionary.get(key, {}), **entry}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load link dictionary %s: %s", DICTIONARY_PATH, e)
    _apply_seed_overrides(dictionary, seed)
    for key, entry in dictionary.items():
        entry.setdefault("slug", _slugify(entry.get("canonical_name") or key))
        entry.setdefault("canonical_name", key)
        aliases = entry.get("aliases") or []
        if isinstance(aliases, list):
            entry["aliases"] = list(dict.fromkeys(str(a).strip() for a in aliases if str(a).strip()))
        else:
            entry["aliases"] = []
    return dictionary


def save_dictionary(dictionary: dict) -> None:
    DICTIONARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DICTIONARY_PATH.write_text(
        json.dumps(dictionary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


SEED_OVERRIDE_FIELDS = ("url", "source_name", "summary", "canonical_name", "kind", "slug")
USER_AGENT = "MeaningSeeker/1.0 (meaning-seeker.com; journal link resolver)"


def _apply_seed_overrides(dictionary: dict, seed: dict) -> None:
    """Curated seed entries win over stale runtime values for core fields."""
    for key, seed_entry in seed.items():
        if not isinstance(seed_entry, dict) or key not in dictionary:
            continue
        entry = dictionary[key]
        for field in SEED_OVERRIDE_FIELDS:
            if seed_entry.get(field):
                entry[field] = seed_entry[field]
        seed_aliases = seed_entry.get("aliases") or []
        runtime_aliases = entry.get("aliases") or []
        entry["aliases"] = list(dict.fromkeys(
            str(a).strip() for a in [*seed_aliases, *runtime_aliases] if str(a).strip()
        ))


def validate_url(url: str) -> dict:
    """Check whether a URL responds successfully. Returns ok, status_code, final_url, error."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "status_code": None, "final_url": "", "error": "empty url"}

    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT, headers=headers)
        if resp.status_code in (405, 501) or resp.status_code >= 400:
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=REQUEST_TIMEOUT,
                headers=headers,
                stream=True,
            )
            resp.close()
        ok = 200 <= resp.status_code < 400
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "final_url": str(resp.url),
            "error": "" if ok else f"HTTP {resp.status_code}",
        }
    except requests.RequestException as e:
        return {"ok": False, "status_code": None, "final_url": url, "error": str(e)}


def validate_dictionary(dictionary: dict | None = None) -> list[dict]:
    """Validate every URL in the dictionary. Returns per-entry results."""
    dictionary = dictionary if dictionary is not None else load_dictionary()
    results: list[dict] = []
    for key, entry in sorted(dictionary.items()):
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        check = validate_url(url)
        results.append({
            "key": key,
            "canonical_name": entry.get("canonical_name", key),
            "url": url,
            "source_name": entry.get("source_name", ""),
            **check,
        })
    return results


def apply_seed_to_runtime_dictionary() -> int:
    """Persist seed overrides into data/link_dictionary.json. Returns number of keys updated."""
    seed = load_seed()
    if not DICTIONARY_PATH.exists():
        dictionary = {k: dict(v) for k, v in seed.items() if isinstance(v, dict)}
        save_dictionary(dictionary)
        return len(dictionary)

    runtime = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        runtime = {}

    updated = 0
    for key, seed_entry in seed.items():
        if not isinstance(seed_entry, dict):
            continue
        current = runtime.get(key)
        if not isinstance(current, dict):
            current = {}
        before = json.dumps(current, sort_keys=True)
        merged = {**current}
        for field in SEED_OVERRIDE_FIELDS:
            if seed_entry.get(field):
                merged[field] = seed_entry[field]
        merged["aliases"] = list(dict.fromkeys(
            str(a).strip()
            for a in [*merged.get("aliases", []), *(seed_entry.get("aliases") or [])]
            if str(a).strip()
        ))
        runtime[key] = merged
        if json.dumps(merged, sort_keys=True) != before:
            updated += 1

    save_dictionary(runtime)
    return updated


def _find_existing_entry(dictionary: dict, canonical_name: str, aliases: list[str] | None = None) -> str | None:
    target = _normalize_key(canonical_name)
    names = {_normalize_key(canonical_name)}
    for alias in aliases or []:
        names.add(_normalize_key(alias))
    for key, entry in dictionary.items():
        entry_names = {_normalize_key(entry.get("canonical_name", key))}
        for alias in entry.get("aliases") or []:
            entry_names.add(_normalize_key(alias))
        if names & entry_names:
            return key
    return None


def _lookup_seed(canonical_name: str, kind: str) -> dict | None:
    seed = load_seed()
    key = _find_existing_entry(seed, canonical_name)
    if key:
        return dict(seed[key])
    for entry in seed.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == kind and _normalize_key(entry.get("canonical_name", "")) == _normalize_key(canonical_name):
            return dict(entry)
    return None


def _wikipedia_title_variants(name: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", name.strip())
    variants = [cleaned.replace(" ", "_")]
    if "'" in cleaned:
        variants.append(cleaned.replace("'", "%27").replace(" ", "_"))
    return list(dict.fromkeys(variants))


def _fetch_wikipedia_summary(title: str) -> dict | None:
    for variant in _wikipedia_title_variants(title):
        url = WIKIPEDIA_SUMMARY_URL.format(title=quote(variant, safe=""))
        try:
            resp = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            page_type = data.get("type", "")
            if page_type == "disambiguation":
                continue
            extract = (data.get("extract") or "").strip()
            content_url = (data.get("content_urls") or {}).get("desktop", {}).get("page", "")
            if not extract or not content_url:
                continue
            return {
                "canonical_name": data.get("title") or title,
                "url": content_url,
                "source_name": "Wikipedia",
                "summary": _truncate_summary(extract),
            }
        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            logger.debug("Wikipedia summary failed for %r: %s", title, e)
            continue
    return None


def _search_wikipedia(title: str) -> dict | None:
    try:
        resp = requests.get(
            WIKIPEDIA_SEARCH_URL,
            params={
                "action": "query",
                "list": "search",
                "srsearch": title,
                "format": "json",
                "srlimit": 3,
            },
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        results = (resp.json().get("query") or {}).get("search") or []
        for hit in results:
            page_title = hit.get("title")
            if not page_title:
                continue
            resolved = _fetch_wikipedia_summary(page_title)
            if resolved:
                return resolved
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        logger.debug("Wikipedia search failed for %r: %s", title, e)
    return None


def _work_lookup_queries(title: str, author: str | None) -> list[str]:
    """Build Wikipedia lookup queries for a work, author-first for disambiguation."""
    title = re.sub(r"\s+", " ", title.strip())
    author = re.sub(r"\s+", " ", (author or "").strip())
    queries: list[str] = []
    if author:
        last_name = author.split()[-1] if author.split() else author
        queries.extend([
            f"{last_name}'s {title}",
            f"{author}'s {title}",
            f"{title} ({last_name})",
            f"{title} ({author})",
            f"{author} {title}",
        ])
    queries.append(title)
    return list(dict.fromkeys(q for q in queries if q.strip()))


def _resolve_via_wikipedia(titles: list[str]) -> dict | None:
    for title in titles:
        resolved = _fetch_wikipedia_summary(title)
        if resolved:
            return resolved
    for title in titles:
        resolved = _search_wikipedia(title)
        if resolved:
            return resolved
    return None


def resolve_candidate(
    text: str,
    kind: str,
    canonical_name: str,
    *,
    author: str | None = None,
    dictionary: dict | None = None,
) -> tuple[dict | None, str]:
    """Resolve one candidate. Returns (entry_dict, status)."""
    dictionary = dictionary if dictionary is not None else load_dictionary()
    author = (author or "").strip() or None
    aliases = [text] if text and _normalize_key(text) != _normalize_key(canonical_name) else []
    existing_key = _find_existing_entry(dictionary, canonical_name, aliases)
    if existing_key:
        current = dictionary[existing_key]
        needs_author_refresh = (
            kind == "work"
            and author
            and (not current.get("author") or current.get("author") != author)
        )
        if not needs_author_refresh:
            return current, "cached"

    seed_entry = _lookup_seed(canonical_name, kind)
    if seed_entry:
        seed_entry.setdefault("slug", _slugify(seed_entry.get("canonical_name") or canonical_name))
        seed_entry.setdefault("kind", kind)
        if author:
            seed_entry["author"] = author
        if text and text not in (seed_entry.get("aliases") or []):
            seed_entry.setdefault("aliases", []).append(text)
        return seed_entry, "seed"

    time.sleep(WIKIPEDIA_RATE_DELAY)
    lookup_titles = (
        _work_lookup_queries(canonical_name, author)
        if kind == "work"
        else [canonical_name]
    )
    resolved = _resolve_via_wikipedia(lookup_titles)
    if not resolved:
        return None, "unresolved"

    entry = {
        "slug": _slugify(resolved["canonical_name"]),
        "canonical_name": resolved["canonical_name"],
        "aliases": list(dict.fromkeys(a for a in [text, canonical_name] if a and a.strip())),
        "kind": kind,
        "url": resolved["url"],
        "source_name": resolved["source_name"],
        "summary": resolved["summary"],
    }
    if author:
        entry["author"] = author
    return entry, "wikipedia"


def merge_entry(dictionary: dict, entry: dict) -> str:
    """Merge entry into dictionary. Returns the dictionary key."""
    canonical = entry.get("canonical_name") or ""
    aliases = entry.get("aliases") or []
    existing_key = _find_existing_entry(dictionary, canonical, aliases)
    key = existing_key or _slugify(canonical) or "entity"

    if existing_key:
        current = dictionary[existing_key]
        merged_aliases = list(dict.fromkeys(
            [*(current.get("aliases") or []), *(entry.get("aliases") or [])]
        ))
        current["aliases"] = merged_aliases
        refresh_work = entry.get("kind") == "work" and entry.get("author")
        for field in ("url", "source_name", "summary", "kind", "slug", "canonical_name", "author"):
            if field in entry and entry[field]:
                if refresh_work or not current.get(field):
                    current[field] = entry[field]
        dictionary[existing_key] = current
        return existing_key

    entry = dict(entry)
    entry.setdefault("slug", _slugify(canonical or key))
    entry.setdefault("canonical_name", canonical or key)
    entry["aliases"] = list(dict.fromkeys(a for a in entry.get("aliases") or [] if a and a.strip()))
    dictionary[key] = entry
    return key


def resolve_and_merge_candidates(
    candidates: list[LinkCandidate],
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Resolve link candidates and merge into the runtime dictionary."""
    dictionary = load_dictionary()
    report: list[dict] = []

    for candidate in candidates:
        text = (candidate.text or "").strip()
        canonical = (candidate.canonical_name or text).strip()
        kind = (candidate.kind or "person").strip().lower()
        author = (candidate.author or "").strip() or None
        if not canonical:
            continue

        entry, status = resolve_candidate(
            text, kind, canonical, author=author, dictionary=dictionary,
        )
        item = {
            "text": text,
            "canonical_name": canonical,
            "kind": kind,
            "author": author,
            "status": status,
            "entry": entry,
        }
        report.append(item)

        if entry and status != "cached" and not dry_run:
            merge_entry(dictionary, entry)

    if not dry_run:
        save_dictionary(dictionary)
    return report


def _get_monitor_client():
    global _client
    if _client is None:
        from openai import OpenAI

        kwargs = {"api_key": API_KEY, "timeout": 60.0}
        if API_BASE_URL is not None:
            kwargs["base_url"] = API_BASE_URL
        _client = OpenAI(**kwargs)
    return _client


_EXTRACT_PROMPT = """Read the journal thinking text below and list named people and named works a curious reader might want to look up.

Include: philosophers, writers, poets, musicians, historical figures, books, poems, prayers, liturgical texts, musical works.
Exclude: the journal's own coined terms, generic concepts (e.g. "meaning", "the Absurd" unless naming Camus's concept explicitly as his), tension IDs (T-0001), commitment IDs (C-0001), cycle numbers, and pronouns.

When a person and their work appear together (e.g. "Spinoza's Ethics", "Wittgenstein's Tractatus"), flag BOTH as separate candidates: the person (kind "person", text as the name alone) and the work (kind "work", text as the title alone). For every work, include "author" with the creator's standard full name — required for lookup (e.g. text "Ethics", canonical_name "Ethics", author "Baruch Spinoza").

Return JSON only:
{
  "candidates": [
    {"text": "exact substring as it appears in the text", "kind": "person|work|text", "canonical_name": "standard full name or title", "author": "creator name (required for kind work, else omit)"}
  ]
}

If none qualify, return {"candidates": []}.

Journal text:
"""


def extract_candidates_from_text(thinking: str) -> list[LinkCandidate]:
    """Use the monitor model to extract link candidates from archived thinking."""
    thinking = (thinking or "").strip()
    if not thinking:
        return []

    client = _get_monitor_client()
    prompt = _EXTRACT_PROMPT + thinking[:12000]
    try:
        response = client.chat.completions.create(
            model=MONITOR_MODEL_ID,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1024,
        )
    except Exception as e:
        logger.error("Link extraction API error: %s", e)
        raise

    raw = ""
    if response.choices:
        raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return []

    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
        if match:
            raw = match.group(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Link extraction returned invalid JSON")
        return []

    out: list[LinkCandidate] = []
    for item in data.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        canonical = (item.get("canonical_name") or text).strip()
        kind = (item.get("kind") or "person").strip().lower()
        author = (item.get("author") or "").strip() or None
        if not canonical:
            continue
        out.append(LinkCandidate(
            text=text or canonical,
            kind=kind,
            canonical_name=canonical,
            author=author,
        ))
    return out


def _is_inside_skip_zone(text: str, start: int, end: int) -> bool:
    before = text[:start]
    after = text[end:]

    if re.search(r"<[^>]*$", before):
        return True
    if "entity-trigger" in before:
        last_open = before.rfind('<span class="entity-trigger"')
        last_close = before.rfind("</span>")
        if last_open > last_close:
            return True

    open_bracket = before.rfind("[")
    close_bracket = before.rfind("]")
    if open_bracket > close_bracket:
        close_paren = after.find(")")
        open_paren = after.find("(")
        if open_paren != -1 and (close_paren == -1 or open_paren < close_paren):
            return True

    line_start = before.rfind("\n") + 1
    line = text[line_start:end]
    if re.match(r"^#{1,6}\s", line):
        return True

    if before.rfind("![") > before.rfind(")"):
        return True

    return False


def _compile_entity_pattern(name: str) -> re.Pattern[str]:
    """Match entity names only at word boundaries, not inside longer words."""
    escaped = re.escape(name)
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def annotate_links(markdown_text: str, dictionary: dict | None = None) -> tuple[str, list[dict]]:
    """Wrap first occurrence of each known entity with entity-trigger spans."""
    dictionary = dictionary if dictionary is not None else load_dictionary()
    if not markdown_text or not dictionary:
        return markdown_text, []

    match_specs: list[tuple[str, dict]] = []
    for entry in dictionary.values():
        if not isinstance(entry, dict):
            continue
        names = [entry.get("canonical_name", "")]
        names.extend(entry.get("aliases") or [])
        seen: set[str] = set()
        for name in sorted((n for n in names if n and str(n).strip()), key=len, reverse=True):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            match_specs.append((str(name), entry))

    match_specs.sort(key=lambda item: len(item[0]), reverse=True)

    text = markdown_text
    used_slugs: set[str] = set()
    used_entities: list[dict] = []

    for name, entry in match_specs:
        slug = entry.get("slug") or _slugify(entry.get("canonical_name", ""))
        if slug in used_slugs:
            continue

        pattern = _compile_entity_pattern(name)
        matched = False
        for match in pattern.finditer(text):
            if _is_inside_skip_zone(text, match.start(), match.end()):
                continue
            matched_text = match.group(0)
            replacement = (
                f'<span class="entity-trigger" data-entity="{slug}" tabindex="0" '
                f'role="button" aria-haspopup="dialog" aria-controls="entity-popup-{slug}">'
                f"{matched_text}</span>"
            )
            text = text[: match.start()] + replacement + text[match.end() :]
            used_slugs.add(slug)
            used_entities.append(entry)
            matched = True
            break
        if matched:
            continue

    return text, used_entities


_COMMITMENT_ID_RE = re.compile(r"\b(C-\d{4})\b")
_TENSION_ID_RE = re.compile(r"\b(T-\d{4})\b")
_CYCLE_REF_RE = re.compile(r"\bcycle\s+(\d+)\b", re.IGNORECASE)


def _journal_index() -> dict[int, dict]:
    """Map cycle number to journal metadata for internal cycle links."""
    index: dict[int, dict] = {}
    journal_dir = DATA_DIR / "archive" / "journal"
    if not journal_dir.exists():
        return index
    for path in journal_dir.glob("*.md"):
        try:
            cycle = int(path.stem)
        except ValueError:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = ""
        excerpt = ""
        for line in content.split("\n"):
            stripped = line.strip()
            if not title and stripped.startswith("# "):
                title = stripped[2:].strip()
            elif stripped.startswith("*") and stripped.endswith("*") and not excerpt:
                excerpt = stripped.strip("*").strip()
            if title and excerpt:
                break
        index[cycle] = {
            "title": title or f"Cycle {cycle}",
            "excerpt": excerpt,
        }
    return index


def _load_commitment_map() -> dict[str, dict]:
    path = DATA_DIR / "commitments.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        item["commitment_id"]: item
        for item in raw
        if isinstance(item, dict) and item.get("commitment_id")
    }


def _load_tension_map() -> dict[str, dict]:
    path = DATA_DIR / "tensions.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        item["tension_id"]: item
        for item in raw
        if isinstance(item, dict) and item.get("tension_id")
    }


def _internal_wrap(slug: str, matched_text: str) -> str:
    return (
        f'<span class="entity-trigger entity-trigger-internal" data-entity="{slug}" tabindex="0" '
        f'role="button" aria-haspopup="dialog" aria-controls="entity-popup-{slug}">'
        f"{matched_text}</span>"
    )


def _apply_internal_pattern(
    text: str,
    pattern: re.Pattern[str],
    resolver,
    used_slugs: set[str],
    used_entities: list[dict],
) -> str:
    matches = list(pattern.finditer(text))
    for match in reversed(matches):
        if _is_inside_skip_zone(text, match.start(), match.end()):
            continue
        entry = resolver(match)
        if not entry:
            continue
        slug = entry["slug"]
        if slug not in used_slugs:
            used_slugs.add(slug)
            used_entities.append(entry)
        matched_text = match.group(0)
        replacement = _internal_wrap(slug, matched_text)
        text = text[: match.start()] + replacement + text[match.end() :]
    return text


def annotate_internal_links(
    markdown_text: str,
    *,
    journal_index: dict[int, dict] | None = None,
) -> tuple[str, list[dict]]:
    """Link commitment IDs, tension IDs, and cycle references to journal entries."""
    if not markdown_text:
        return markdown_text, []

    journal_index = journal_index if journal_index is not None else _journal_index()
    commitments = _load_commitment_map()
    tensions = _load_tension_map()
    used_slugs: set[str] = set()
    used_entities: list[dict] = []
    text = markdown_text

    def commitment_entry(match: re.Match[str]) -> dict | None:
        cid = match.group(1)
        data = commitments.get(cid)
        if not data:
            return None
        origin = data.get("origin_cycle")
        if not origin or int(origin) not in journal_index:
            return None
        conf = data.get("confidence")
        conf_note = f" Confidence: {conf:.0%}." if isinstance(conf, (int, float)) else ""
        return {
            "slug": f"int-{cid.lower()}",
            "canonical_name": cid,
            "summary": _truncate_summary((data.get("statement") or "") + conf_note),
            "url": f"/journal/{int(origin)}",
            "source_name": "Commitment",
            "internal": True,
            "kind": "commitment",
        }

    def tension_entry(match: re.Match[str]) -> dict | None:
        tid = match.group(1)
        data = tensions.get(tid)
        if not data:
            return None
        created = data.get("created_cycle")
        if not created or int(created) not in journal_index:
            return None
        status = data.get("status", "active")
        status_note = f" Status: {status}." if status else ""
        return {
            "slug": f"int-{tid.lower()}",
            "canonical_name": tid,
            "summary": _truncate_summary((data.get("description") or "") + status_note),
            "url": f"/journal/{int(created)}",
            "source_name": "Tension",
            "internal": True,
            "kind": "tension",
        }

    def cycle_entry(match: re.Match[str]) -> dict | None:
        cycle_num = int(match.group(1))
        meta = journal_index.get(cycle_num)
        if not meta:
            return None
        summary = meta.get("excerpt") or meta.get("title") or f"Cycle {cycle_num}"
        return {
            "slug": f"int-cycle-{cycle_num}",
            "canonical_name": f"Cycle {cycle_num}",
            "summary": _truncate_summary(summary),
            "url": f"/journal/{cycle_num}",
            "source_name": "Journal",
            "internal": True,
            "kind": "cycle",
        }

    text = _apply_internal_pattern(text, _COMMITMENT_ID_RE, commitment_entry, used_slugs, used_entities)
    text = _apply_internal_pattern(text, _TENSION_ID_RE, tension_entry, used_slugs, used_entities)
    text = _apply_internal_pattern(text, _CYCLE_REF_RE, cycle_entry, used_slugs, used_entities)
    return text, used_entities
