"""
Phase B: Archive retrieval — vector embeddings via Ollama (nomic-embed-text), Chroma store.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import (
    ARCHIVE_RETRIEVAL_COUNT,
    ARCHIVE_SNIPPET_TOKEN_LIMIT,
    CHROMA_DIR,
    DATA_DIR,
    OLLAMA_BASE_URL,
    OLLAMA_EMBED_MODEL,
)

logger = logging.getLogger(__name__)

# Approx 4 chars per token
CHARS_PER_TOKEN = 4


@dataclass
class ArchiveSnippet:
    cycle_number: int
    timestamp: str
    active_tension_descriptions: list[str]
    thinking: str  # truncated to ~500 tokens at sentence boundary


def _approx_tokens(text: str) -> int:
    return max(0, len(text) // CHARS_PER_TOKEN)


def _truncate_at_sentence(text: str, max_tokens: int) -> str:
    if _approx_tokens(text) <= max_tokens:
        return text
    max_chars = max_tokens * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    # Break at last sentence boundary before max_chars
    chunk = text[: max_chars + 100]
    match = list(re.finditer(r"[.!?]\s+", chunk))
    if not match:
        return text[:max_chars].rstrip() + " [truncated]"
    for m in reversed(match):
        if m.end() <= max_chars:
            return chunk[: m.end()].rstrip() + " [truncated]"
    return text[:max_chars].rstrip() + " [truncated]"


def embed_text(text: str) -> list[float] | None:
    """Call Ollama embeddings API. Returns 768-dim vector or None if unavailable."""
    if not text.strip():
        return None
    try:
        r = requests.post(
            f"{OLLAMA_BASE_URL.rstrip('/')}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": text[:10000]},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        embeddings = data.get("embeddings")
        if not embeddings or not isinstance(embeddings[0], list):
            return None
        return [float(x) for x in embeddings[0]]
    except Exception as e:
        logger.warning("Ollama embedding failed: %s", e)
        return None


def _get_chroma_collection():
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    # Get or create; we supply embeddings on add so dimension comes from first add
    try:
        coll = client.get_collection(name="archive")
    except Exception:
        coll = client.create_collection(name="archive", metadata={"hnsw:space": "cosine"})
    return coll


def store_cycle_embedding(
    cycle: int,
    thinking: str,
    tensions: list,
    challenge: str,
    timestamp: str | None = None,
) -> None:
    """Compute composite embedding and store in Chroma with metadata."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    tension_descs = [t.description for t in tensions if getattr(t, "status", "") == "active"] if tensions else []
    composite = f"{thinking[:8000]}\n\nTensions: {' '.join(tension_descs[:10])}\n\nChallenge: {challenge[:1000]}"
    vec = embed_text(composite)
    if vec is None:
        logger.warning("Skipping store_cycle_embedding for cycle %s: no embedding", cycle)
        return
    coll = _get_chroma_collection()
    doc_id = f"cycle-{cycle:06d}"
    # Chroma metadata: str, int, float, bool. Store tension descriptions for snippet display.
    active = [t for t in (tensions or []) if getattr(t, "status", None) == "active"]
    tension_ids = [getattr(t, "tension_id", "") for t in active[:20]]
    tension_descriptions = [getattr(t, "description", "")[:200] for t in active[:20]]
    meta = {
        "cycle_number": cycle,
        "timestamp": timestamp,
        "active_tension_ids": ",".join(tension_ids),
        "tension_descriptions": " | ".join(tension_descriptions)[:2000],
        "injected_challenge_text": (challenge or "")[:500],
    }
    try:
        coll.add(ids=[doc_id], embeddings=[vec], documents=[thinking[:12000]], metadatas=[meta])
    except Exception as e:
        # Id may already exist (e.g. re-run); try update or skip
        logger.warning("Chroma add failed for %s: %s", doc_id, e)


def retrieve_similar(
    tensions: list,
    challenge: str,
    exclude_recent: int = 5,
    current_cycle: int | None = None,
) -> list[ArchiveSnippet]:
    """Embed query from tensions + challenge, return top N snippets excluding recent cycles."""
    tension_descs = [t.description for t in tensions if getattr(t, "status", "") == "active"] if tensions else []
    query_text = " ".join(tension_descs) + " " + (challenge or "")
    vec = embed_text(query_text)
    if vec is None:
        return []
    coll = _get_chroma_collection()
    n = ARCHIVE_RETRIEVAL_COUNT + exclude_recent
    try:
        results = coll.query(query_embeddings=[vec], n_results=min(n, 100), include=["documents", "metadatas"])
    except Exception as e:
        logger.warning("Chroma query failed: %s", e)
        return []
    if not results or not results["ids"] or not results["ids"][0]:
        return []
    ids = results["ids"][0]
    metadatas = (results.get("metadatas") or [[]])[0]
    documents = (results.get("documents") or [[]])[0]
    snippets = []
    for i, doc_id in enumerate(ids):
        try:
            cycle_num = int(re.search(r"cycle-(\d+)", doc_id).group(1))
        except (AttributeError, ValueError):
            continue
        if current_cycle is not None and cycle_num >= current_cycle - exclude_recent:
            continue
        meta = (metadatas[i] if i < len(metadatas) else {}) or {}
        doc = (documents[i] if i < len(documents) else "") or ""
        tension_descs_str = meta.get("tension_descriptions") or ""
        tension_descs_list = [s.strip() for s in tension_descs_str.split("|") if s.strip()] if tension_descs_str else []
        ts = meta.get("timestamp") or ""
        thinking_truncated = _truncate_at_sentence(doc, ARCHIVE_SNIPPET_TOKEN_LIMIT)
        snippets.append(
            ArchiveSnippet(
                cycle_number=cycle_num,
                timestamp=ts,
                active_tension_descriptions=tension_descs_list,
                thinking=thinking_truncated,
            )
        )
        if len(snippets) >= ARCHIVE_RETRIEVAL_COUNT:
            break
    return snippets
