"""Privacy labels. Structural, not remembered (ARCHITECTURE § Privacy, WORKFLOW § 3).

Contract (implemented by worker, owned by the operator; loosening = full-stop):
- LEVELS ordered public < household < private. `allowed(label, max_level)` → label <= max_level.
- Unknown/missing label is treated as 'private'.
- `label_item(reader_cfg, source_content_public, title, body, url, author) -> (label, reason)`:
    1. any reader pinned term (case-insensitive, word-ish match) in title/body/url/author → 'private', 'pinned:<index>'
       (reason holds the index, never the term, so logs don't leak it)
    2. any reader household term → 'household', 'household:<index>'
    3. source_content_public → 'public', 'public-source'
    4. else 'private', 'default'
- Annotations (notes) are always private regardless of item label.
- A story's privacy = max(privacy of its items, 'private' if why_it_matters is set).
Reader config: data/readers/<id>/reader.toml  →  [privacy] pinned = [...], household = [...]
"""
from __future__ import annotations

LEVELS = ("public", "household", "private")


def rank(label: str | None) -> int:
    """Ordinal of a label; unknown/missing → private's rank (fail closed)."""
    try:
        return LEVELS.index((label or "").strip().lower())
    except ValueError:
        return LEVELS.index("private")


def allowed(label: str | None, max_level: str) -> bool:
    return rank(label) <= rank(max_level)


def _terms(reader_cfg: dict, key: str) -> list[str]:
    return [str(t) for t in (reader_cfg.get("privacy") or {}).get(key) or []]


def _haystack(title, body, url, author) -> str:
    return "\n".join(p for p in (title, body, url, author) if p).lower()


def label_item(
    reader_cfg: dict,
    source_content_public: bool,
    title: str | None,
    body: str | None,
    url: str | None,
    author: str | None,
) -> tuple[str, str]:
    """Label one item from the reader's config. Reason never contains the term itself."""
    hay = _haystack(title, body, url, author)
    for i, term in enumerate(_terms(reader_cfg, "pinned")):
        if term.lower() in hay:
            return "private", f"pinned:{i}"
    for i, term in enumerate(_terms(reader_cfg, "household")):
        if term.lower() in hay:
            return "household", f"household:{i}"
    if source_content_public:
        return "public", "public-source"
    return "private", "default"


def story_privacy(item_labels, has_why: bool = False) -> str:
    """A story is at least as private as its most private item; why_it_matters pins private."""
    if has_why:
        return "private"
    if not item_labels:
        return "private"
    return max(item_labels, key=rank)
