"""Safe desk-recipe sharing: export (owner) / inspect (anyone) / import (recipient).

A **desk recipe** is an inert JSON description of how a desk is set up: the
public web feed urls and the section labels the owner explicitly selected,
plus optional furniture (--title, --langs) and an explicit public-only
renderer link. It is how one gaceta reader hands another person the *shape*
of a paper — never the paper itself.

Contract (loosening any of it = full-stop, every clause is pinned by tests):

- **Private by default.** Export selects from an allowlist, not a
  denylist: NOTHING rides along unless the owner names it on the command
  line (`--feed <id>`, `--label <id>`). Unselected feeds and labels do not
  travel, not even as counts inside the file; the preview mentions only
  how many stayed private, never their names.
- **Public web urls only.** Every carried url (feeds and the renderer link)
  must be an http(s) url with a public-looking host: a dotted ascii
  hostname, no embedded credentials, no localhost/.local/lan names, and no
  loopback/private/link-local ip literal. The check is *syntactic* — this
  surface never does DNS and never fetches — and ingest re-runs its own
  SSRF guard at fetch time anyway.
- **No executable config, no local paths, no secrets, no arbitrary
  settings.** A recipe is data, never code: parsed with a key allowlist
  (every level allows exactly the keys this module writes, refuses
  everything else), hard caps on every count and length, nothing
  evaluated, merged or imported verbatim. Provider tables, api key env
  names, adapter paths, nostr keys, plugin/entry-point tables cannot
  appear anywhere — there is no key for them. The importer regenerates a
  fresh reader.toml from gaceta's own template using only the allowlisted
  values, so recipe bytes never reach a config file as-is.
- **Canaries never ride.** The owner's privacy terms ([privacy]
  pinned/household) are checked against every string the recipe would
  carry; a selection that touches one refuses, and the refusal never
  names the term (same discipline as privacy.label_item).
- **Preview before consent.** `export` and `import` print the full preview
  (exactly what would be written, and where) and write nothing unless
  `--consent` is passed. Without it both are read-only. `inspect` is
  always read-only: one file, no data dir, no database, no config.
- **New, isolated, independent readers.** `import --as <id>` creates a
  brand-new reader and refuses if anything already exists at that path —
  never an overwrite, never a merge. The recipe is not bound to any
  reader (unlike the agent export), carries no data-directory paths and
  no database content — no items, stories, write-ups, marks, reactions or
  taste — so every recipient is independent, and importing the same
  recipe twice yields two readers that share nothing.
- **The renderer link is explicit and public-only.** `--link` is opt-in,
  must be https, and is recorded as privacy "public" in the recipe — the
  parser demands that exact marking, refusing any link that is not
  explicitly public. `inspect` shows it as a public-only renderer link;
  `import` keeps it in the generated config as a comment (a place to
  visit in a browser), never as a setting gaceta reads.

No database is opened anywhere on this surface: export reads the reader's
TOML only, inspect/import read one recipe file, and the only writes are
`export --consent` (one JSON file) and `import --consent` (one new
reader.toml, exclusive-create).
"""
from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

RECIPE_KIND = "gaceta.desk-recipe"
RECIPE_SCHEMA_VERSION = 1

MAX_RECIPE_BYTES = 512 * 1024
MAX_FEEDS = 64
MAX_LABELS = 32
MAX_LIST_ITEMS = 8
MAX_TITLE = 80
MAX_NAME = 120
MAX_TOKEN = 64
MAX_ABOUT = 200
MAX_URL = 512
MAX_LANGS = 8

DEFAULT_TITLE = "Desk recipe"
FEED_KINDS = ("rss", "youtube")

# same slug rule as serve.READER_RE / agent.READER_RE (kills path tricks too)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")

# The allowlist contract both ways: the builder emits exactly these keys,
# the parser refuses anything else — a field that must never ride along
# (provider tables, api key env names, adapter paths, plugin entry points,
# arbitrary settings) has no key to sneak in through.
RECIPE_KEYS = frozenset({"schema_version", "kind", "recipe", "link", "privacy"})
RECIPE_BLOCK_KEYS = frozenset({"title", "langs", "feeds", "labels"})
FEED_KEYS = frozenset({"id", "name", "url", "kind"})
LABEL_KEYS = frozenset({"id", "title", "kinds", "source_prefixes", "keywords"})
LINK_KEYS = frozenset({"url", "privacy", "about"})

PRIVACY_CARRIES = (
    "a desk recipe carries configuration only: the public web feed urls and "
    "section labels the owner explicitly selected. no items, stories, "
    "write-ups, marks, reactions, taste, provider settings, api keys or "
    "local paths ride along.")
PRIVACY_BLOCK = {"max_level": "public", "carries": PRIVACY_CARRIES}

LINK_ABOUT = ("the sender's public-only rendered paper "
              "(built with gaceta render --max-privacy public)")


class RecipeError(Exception):
    """Any reason this surface refuses to build, parse or import (fail closed)."""


def validate_slug(value: object, what: str = "id") -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise RecipeError(f"bad {what} {value!r} (lowercase letters, digits, - or _)")
    return value


# ----------------------------------------------------------------- url checks

def _public_web_url(url: object, *, https_only: bool = False, what: str = "url") -> str:
    """A syntactic public-web-url check. No DNS, no fetching: scheme, a
    dotted ascii hostname (or a global ip literal), no credentials, no
    local names. Ingest re-runs its own SSRF guard at fetch time."""
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL or any(ch.isspace() for ch in text):
        raise RecipeError(f"{what} is not a web url: {text!r}")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise RecipeError(f"{what} is not a parseable url: {text!r}") from None
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme.lower() not in allowed:
        raise RecipeError(
            f"{what} must be {' or '.join(allowed)}: {text!r}; public web urls only")
    if parts.username or parts.password:
        raise RecipeError(f"{what} carries embedded credentials; refusing (fail closed)")
    host = parts.hostname or ""
    if not host:
        raise RecipeError(f"{what} has no host: {text!r}")
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        raise RecipeError(f"{what} points at a local host; refusing (fail closed)")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if (not ip.is_global or ip.is_private or ip.is_reserved
                or ip.is_loopback or ip.is_link_local or ip.is_multicast):
            raise RecipeError(
                f"{what} points at a non-public address; refusing (fail closed)")
    elif not HOST_RE.fullmatch(host):
        raise RecipeError(
            f"{what} host {host!r} is not a public web hostname "
            "(dotted, letters/digits/hyphens); refusing (fail closed)")
    return text


def _check_text(value: object, what: str, cap: int, *, allow_empty: bool = False) -> str:
    """One carried string: a real string, no control characters, capped."""
    if not isinstance(value, str):
        raise RecipeError(f"{what} must be a string; refusing (fail closed)")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RecipeError(f"{what} contains control characters; refusing (fail closed)")
    if len(value) > cap:
        raise RecipeError(f"{what} is longer than {cap} chars; refusing (fail closed)")
    if not allow_empty and not value.strip():
        raise RecipeError(f"{what} must not be empty; refusing (fail closed)")
    return value


# ------------------------------------------------------------- the safe parser

def validate_recipe(obj: object) -> dict:
    """The safe parser: allowlist keys, strict types, hard caps, fail closed.

    Accepts exactly what `build_recipe` writes and nothing else. Returns
    the validated recipe (normalized: title defaulted, urls stripped)."""
    if not isinstance(obj, dict):
        raise RecipeError("a desk recipe must be a JSON object; refusing (fail closed)")
    if obj.get("kind") != RECIPE_KIND:
        raise RecipeError(
            f"not a gaceta desk recipe (kind {obj.get('kind')!r}); refusing")
    v = obj.get("schema_version")
    if isinstance(v, bool) or not isinstance(v, int) or v != RECIPE_SCHEMA_VERSION:
        raise RecipeError(
            f"unknown desk-recipe schema version {v!r}; this tool supports "
            f"{RECIPE_SCHEMA_VERSION} — refusing (fail closed)")
    unknown = set(obj) - RECIPE_KEYS
    if unknown:
        raise RecipeError(
            f"desk recipe carries keys this surface never emits "
            f"({', '.join(sorted(unknown))}); refusing (fail closed)")
    if obj.get("privacy") != PRIVACY_BLOCK:
        raise RecipeError(
            "desk recipe lacks its exact public-only privacy contract; "
            "refusing (fail closed)")
    block = obj.get("recipe")
    if not isinstance(block, dict):
        raise RecipeError("desk recipe has no 'recipe' object; refusing (fail closed)")
    unknown = set(block) - RECIPE_BLOCK_KEYS
    if unknown:
        raise RecipeError(
            f"recipe carries keys this surface never emits "
            f"({', '.join(sorted(unknown))}); refusing (fail closed)")

    title = block.get("title")
    if title is None:
        block["title"] = DEFAULT_TITLE
    else:
        _check_text(title, "recipe title", MAX_TITLE)
    langs = block.get("langs")
    if langs is not None:
        langs = _check_text(langs, "recipe langs", 40)
        tokens = [t.strip() for t in langs.split(",")]
        if (not tokens or len(tokens) > MAX_LANGS
                or any(not LANG_RE.fullmatch(t) for t in tokens)):
            raise RecipeError(
                f"recipe langs must be comma-separated language codes, "
                f"e.g. 'es,en': {langs!r}; refusing (fail closed)")

    feeds = block.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise RecipeError(
            "desk recipe must carry at least one feed (a list of public web "
            "urls); refusing (fail closed)")
    if len(feeds) > MAX_FEEDS:
        raise RecipeError(
            f"desk recipe carries {len(feeds)} feeds; this surface refuses "
            f"more than {MAX_FEEDS}")
    seen: set[str] = set()
    for i, f in enumerate(feeds):
        what = f"recipe feed[{i}]"
        if not isinstance(f, dict):
            raise RecipeError(f"{what} is malformed; refusing (fail closed)")
        unknown = set(f) - FEED_KEYS
        if unknown:
            raise RecipeError(
                f"{what} carries keys this surface never emits "
                f"({', '.join(sorted(unknown))}); refusing (fail closed)")
        fid = f.get("id")
        if not isinstance(fid, str) or not SLUG_RE.fullmatch(fid):
            raise RecipeError(
                f"{what} has a bad id {fid!r} (lowercase letters, digits, "
                "- or _); refusing (fail closed)")
        if fid in seen:
            raise RecipeError(
                f"desk recipe carries feed id {fid!r} twice; refusing (fail closed)")
        seen.add(fid)
        if f.get("name") is not None:
            _check_text(f["name"], f"feed {fid!r} name", MAX_NAME)
        f["url"] = _public_web_url(f.get("url"), what=f"feed {fid!r} url")
        kind = f.get("kind")
        if kind is not None and kind not in FEED_KINDS:
            raise RecipeError(
                f"feed {fid!r} kind {kind!r} is not one of {FEED_KINDS}; "
                "refusing (fail closed)")

    labels = block.get("labels")
    if labels is None:
        labels = []
        block["labels"] = labels
    if not isinstance(labels, list):
        raise RecipeError("recipe labels must be a list; refusing (fail closed)")
    if len(labels) > MAX_LABELS:
        raise RecipeError(
            f"desk recipe carries {len(labels)} labels; this surface "
            f"refuses more than {MAX_LABELS}")
    seen = set()
    for i, lab in enumerate(labels):
        what = f"recipe label[{i}]"
        if not isinstance(lab, dict):
            raise RecipeError(f"{what} is malformed; refusing (fail closed)")
        unknown = set(lab) - LABEL_KEYS
        if unknown:
            raise RecipeError(
                f"{what} carries keys this surface never emits "
                f"({', '.join(sorted(unknown))}); refusing (fail closed)")
        lid = lab.get("id")
        if not isinstance(lid, str) or not SLUG_RE.fullmatch(lid):
            raise RecipeError(
                f"{what} has a bad id {lid!r} (lowercase letters, digits, "
                "- or _); refusing (fail closed)")
        if lid in seen:
            raise RecipeError(
                f"desk recipe carries label id {lid!r} twice; refusing (fail closed)")
        seen.add(lid)
        if lab.get("title") is not None:
            _check_text(lab["title"], f"label {lid!r} title", MAX_NAME)
        for key in ("kinds", "source_prefixes", "keywords"):
            vals = lab.get(key)
            if vals is None:
                continue
            if not isinstance(vals, list) or len(vals) > MAX_LIST_ITEMS:
                raise RecipeError(
                    f"label {lid!r} {key} must be a list of at most "
                    f"{MAX_LIST_ITEMS} strings; refusing (fail closed)")
            clean = []
            for t in vals:
                t = _check_text(t, f"label {lid!r} {key} token", MAX_TOKEN)
                if key == "kinds" and not KIND_RE.fullmatch(t):
                    raise RecipeError(
                        f"label {lid!r} kinds token {t!r} is not a source "
                        "kind slug; refusing (fail closed)")
                clean.append(t)
            lab[key] = clean

    link = obj.get("link")
    if link is not None:
        if not isinstance(link, dict):
            raise RecipeError(
                "recipe link must be an object {url, privacy, about}; "
                "refusing (fail closed)")
        unknown = set(link) - LINK_KEYS
        if unknown:
            raise RecipeError(
                f"recipe link carries keys this surface never emits "
                f"({', '.join(sorted(unknown))}); refusing (fail closed)")
        _public_web_url(link.get("url"), https_only=True, what="the renderer link")
        if link.get("privacy") != "public":
            raise RecipeError(
                "the renderer link must be explicitly public-only "
                "(privacy 'public'); refusing (fail closed)")
        if link.get("about") is not None:
            _check_text(link["about"], "renderer link about", MAX_ABOUT)
    return obj


def load_recipe(path: object) -> dict:
    """Read and fully validate ONE recipe file. Never writes. The safe
    parser is the whole read: anything this surface never emits refuses."""
    p = Path(path)
    if not p.is_file():
        raise RecipeError(f"no desk recipe at {p}")
    if p.stat().st_size > MAX_RECIPE_BYTES:
        raise RecipeError(
            f"desk recipe {p} is larger than the {MAX_RECIPE_BYTES // 1024} KB "
            "cap; refusing (fail closed)")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RecipeError(f"unreadable desk recipe {p}: {e}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise RecipeError(f"desk recipe {p} is not valid JSON: {e}") from None
    return validate_recipe(obj)


# ------------------------------------------------------------- owner: export

def _canary_check(cfg: dict, carried: Sequence[tuple[str, str]]) -> None:
    """Refuse if any pinned/household canary appears in what the recipe
    would carry. The term itself is never named in the refusal (same
    discipline as privacy.label_item: logs don't leak it)."""
    terms = cfg.get("privacy") or {}
    for scope in ("pinned", "household"):
        for i, raw in enumerate(terms.get(scope) or []):
            term = str(raw).strip().lower()
            if not term:
                continue
            for what, text in carried:
                if term in str(text).lower():
                    raise RecipeError(
                        f"refusing: a {scope} privacy canary (#{i + 1}) appears "
                        f"in {what}; canaries never ride along and are never "
                        "printed; nothing was written")


def build_recipe(cfg: dict, feeds: Sequence[str], labels: Sequence[str],
                 title: str | None = None, langs: str | None = None,
                 link: str | None = None) -> dict:
    """OWNER side: reader config + explicit selections → a desk recipe.

    Private by default: only the feeds and labels named in ``feeds`` /
    ``labels`` ride along, and only if they are shareable (a public web
    feed url, a slug label id). A selection that touches a privacy canary
    refuses. The built recipe is validated with the very parser the
    recipient side uses — the builder must satisfy its own contract."""
    reader = str(cfg.get("id") or "")
    requested_feeds = list(dict.fromkeys(str(f) for f in feeds))
    requested_labels = list(dict.fromkeys(str(l) for l in labels))
    if not requested_feeds and not requested_labels:
        raise RecipeError(
            "nothing selected: a desk recipe is private by default — pass "
            "--feed <id> (and --label <id>) for each feed/label to share")
    if not requested_feeds:
        raise RecipeError(
            "a desk recipe needs at least one --feed (labels ride along "
            "with feeds)")

    rss = (cfg.get("sources") or {}).get("rss") or {}
    table = {str(f["id"]): f for f in rss.get("feeds") or [] if f.get("id")}
    carried: list[tuple[str, str]] = []
    if title is not None:
        carried.append(("the --title",
                        _check_text(title, "the --title", MAX_TITLE)))

    feed_entries: list[dict] = []
    for slug in requested_feeds:
        f = table.get(slug)
        if f is None:
            raise RecipeError(
                f"reader {reader!r} has no [[sources.rss.feeds]] id {slug!r}; "
                "a desk recipe selects among that reader's rss feeds")
        if f.get("file"):
            raise RecipeError(
                f"feed {slug!r} reads a local file; a desk recipe carries "
                "public web urls only — share nothing instead of a path")
        url = _public_web_url(f.get("url"), what=f"feed {slug!r} url")
        kind = f.get("kind")
        if kind is not None and str(kind) not in FEED_KINDS:
            raise RecipeError(
                f"feed {slug!r} has kind {kind!r}; a desk recipe carries "
                f"rss/youtube feeds only")
        name = str(f.get("name") or slug)
        entry = {"id": slug, "name": name, "url": url}
        if kind is not None:
            entry["kind"] = str(kind)
        feed_entries.append(entry)
        carried += [(f"feed {slug!r} id", slug), (f"feed {slug!r} name", name),
                    (f"feed {slug!r} url", url)]

    by_id: dict[str, dict] = {}
    for sec in cfg.get("sections") or []:
        if sec.get("id") is not None:
            by_id.setdefault(str(sec["id"]), sec)
    label_entries: list[dict] = []
    for lid in requested_labels:
        sec = by_id.get(lid)
        if sec is None:
            raise RecipeError(
                f"reader {reader!r} has no [[sections]] id {lid!r}")
        sid = str(sec.get("id") or "")
        if not SLUG_RE.fullmatch(sid):
            raise RecipeError(
                f"label {lid!r}: label ids must be url-safe slugs "
                "(lowercase letters, digits, - or _); refusing (fail closed)")
        entry: dict = {"id": sid}
        carried.append((f"label {sid!r} id", sid))
        if sec.get("title") is not None:
            entry["title"] = _check_text(str(sec["title"]),
                                         f"label {sid!r} title", MAX_NAME)
            carried.append((f"label {sid!r} title", entry["title"]))
        for key in ("kinds", "source_prefixes", "keywords"):
            vals = sec.get(key)
            if vals is None:
                continue
            if not isinstance(vals, list) or len(vals) > MAX_LIST_ITEMS:
                raise RecipeError(
                    f"label {sid!r} {key} must be a list of at most "
                    f"{MAX_LIST_ITEMS} items; refusing (fail closed)")
            clean = []
            for t in vals:
                t = _check_text(str(t), f"label {sid!r} {key} token", MAX_TOKEN)
                if key == "kinds" and not KIND_RE.fullmatch(t):
                    raise RecipeError(
                        f"label {sid!r} kinds token {t!r} is not a source "
                        "kind slug; refusing (fail closed)")
                clean.append(t)
                carried.append((f"label {sid!r} {key}", t))
            entry[key] = clean
        label_entries.append(entry)

    recipe: dict = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "kind": RECIPE_KIND,
        "recipe": {
            "title": str(title or DEFAULT_TITLE),
            "feeds": feed_entries,
            "labels": label_entries,
        },
        "privacy": dict(PRIVACY_BLOCK),
    }
    if langs is not None:
        recipe["recipe"]["langs"] = langs
    if link is not None:
        url = _public_web_url(link, https_only=True,
                              what="the --link renderer link")
        recipe["link"] = {"url": url, "privacy": "public", "about": LINK_ABOUT}
        carried.append(("the renderer link", url))
    _canary_check(cfg, carried)
    return validate_recipe(recipe)


def default_export_path(data_dir: Path, reader: str, recipe: dict) -> Path:
    """<data>/recipes/<reader>/<title-slug>.json — gaceta's own default."""
    title = str((recipe.get("recipe") or {}).get("title") or DEFAULT_TITLE)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "recipe"
    return Path(data_dir) / "recipes" / reader / f"{slug}.json"


def write_recipe(recipe: dict, out: object) -> Path:
    """Write one recipe file atomically (export side; overwriting gaceta's
    own export artifact with a fresh one is fine — it is not a reader)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(out)
    return out


# ----------------------------------------------------------------- the preview

def preview_recipe(recipe: dict) -> str:
    """Human preview of EXACTLY what the recipe carries — the same text
    before consent on export, from `inspect`, and before consent on
    import. Read-only by construction (pure function on the dict)."""
    block = recipe["recipe"]
    lines = [
        f'desk recipe "{block["title"]}" ({RECIPE_KIND} v{RECIPE_SCHEMA_VERSION})',
        f"privacy: public-only — {PRIVACY_CARRIES}",
    ]
    if block.get("langs"):
        lines.append(f"langs: {block['langs']} (write-up languages carried)")
    feeds = block.get("feeds") or []
    lines.append(f"feeds ({len(feeds)}):")
    for f in feeds:
        kind = f"  [{f['kind']}]" if f.get("kind") else ""
        lines.append(f"  - {f['id']}  {f.get('name') or f['id']}  {f['url']}{kind}")
    labels = block.get("labels") or []
    if labels:
        lines.append(f"labels ({len(labels)}):")
    else:
        lines.append("labels: none")
    for lab in labels:
        head = f"  - {lab['id']}"
        if lab.get("title"):
            head += f'  "{lab["title"]}"'
        rules = []
        if lab.get("kinds"):
            rules.append("kinds=" + ",".join(lab["kinds"]))
        if lab.get("source_prefixes"):
            rules.append("prefixes=" + ",".join(lab["source_prefixes"]))
        if lab.get("keywords"):
            rules.append("keywords=" + ",".join(lab["keywords"]))
        lines.append(head + (f"  ({' '.join(rules)})" if rules else ""))
    if "link" in recipe:
        lines.append(f"sender's public paper: {recipe['link']['url']} "
                     "(explicit public-only renderer link, privacy=public)")
    return "\n".join(lines)


# ---------------------------------------------------------- recipient: import

def render_reader_toml(new_reader: str, recipe: dict, day: str) -> str:
    """A fresh reader.toml from gaceta's own template. Only allowlisted
    recipe values are interpolated, strings through JSON escaping (valid
    TOML basic strings) — recipe bytes never reach the config verbatim, and
    the recipe cannot choose providers, keys or any other setting."""
    block = recipe["recipe"]

    def s(value: str) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    lines = [
        "# gaceta reader configuration — created by `gaceta recipe import`.",
        f"# recipe: {block['title']} — the public feed urls and section labels",
        "# the sender explicitly selected; no content, settings, keys or",
        "# local paths ride along (the recipe is inert data, never config).",
    ]
    if "link" in recipe:
        lines += [
            "#",
            f"# sender's public-only rendered paper: {recipe['link']['url']}",
            "# (an explicit public-only renderer link: open it in a browser;",
            "#  gaceta never reads it.)",
        ]
    lines += [
        f"# imported {day}; a new, isolated reader: nothing is shared with the",
        "# sender or with other recipients of the same recipe.",
        "",
        f"id = {s(new_reader)}",
        f"name = {s(new_reader.replace('-', ' ').title())}",
        f"langs = {s(block.get('langs') or 'en')}",
        "",
        "[privacy]",
        "pinned = []           # canaries are yours alone; the recipe carried none",
        "household = []",
        "",
    ]
    for f in block.get("feeds") or []:
        lines += ["[[sources.rss.feeds]]", f"id = {s(f['id'])}",
                  f"name = {s(f.get('name') or f['id'])}", f"url = {s(f['url'])}"]
        if f.get("kind"):
            lines.append(f"kind = {s(f['kind'])}")
        lines.append("")
    for lab in block.get("labels") or []:
        lines += ["[[sections]]", f"id = {s(lab['id'])}"]
        if lab.get("title"):
            lines.append(f"title = {s(lab['title'])}")
        for key in ("kinds", "source_prefixes", "keywords"):
            if lab.get(key):
                lines.append(f"{key} = {json.dumps(lab[key], ensure_ascii=False)}")
        lines.append("")
    lines += [
        "# point these at real services when ready (see README); a recipe",
        "# cannot choose providers for you — it never carries settings.",
        "[embed]",
        'provider = "fake"',
        "",
        "[llm.editor]",
        'provider = "fake"',
        "",
        "[cluster]",
        "threshold = 0.70",
        "window_days = 21",
        "",
    ]
    return "\n".join(lines)


def plan_import(recipe: dict, data_dir: Path, new_reader: object) -> tuple[Path, str]:
    """Everything but the write: validate the new reader id, refuse any
    existing path (no overwrite, no merge), render the fresh config."""
    validate_slug(new_reader, what="new reader id")
    day = datetime.now().date().isoformat()
    target = Path(data_dir) / "readers" / str(new_reader)
    if target.exists():
        raise RecipeError(
            f"{target} already exists; import creates a new isolated reader "
            "and never overwrites — pass a fresh --as")
    return target / "reader.toml", render_reader_toml(str(new_reader), recipe, day)


def commit_import(target: Path, toml_text: str) -> Path:
    """The only write on the recipient side: exclusive-create of one new
    reader.toml. If anything appears at that path first, refuse."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(toml_text)
    except FileExistsError:
        raise RecipeError(
            f"{target} already exists; import never overwrites "
            "(fail closed)") from None
    return target
