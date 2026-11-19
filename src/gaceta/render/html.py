"""Static HTML renderer for an edition snapshot (docs/EDITION.md).

One self-contained HTML5 document: inline CSS, system fonts, no external
resources except validated ``media/`` images (relative to the site root, via
``media_prefix``) and https media URLs. Every piece of edition text is escaped;
only http(s) source URLs become links. Rendering refuses any edition containing
a story above ``max_level``. The only script allowed is the tiny inline
YouTube click-to-play loader, emitted only when the edition has YouTube media.
"""
from __future__ import annotations

import html
import re
from contextvars import ContextVar
from datetime import date, datetime
from urllib.parse import urlsplit

from .. import i18n, privacy
from . import icons
from ..media import YOUTUBE_ID_RE

STATUS_KEYS = {"developing": "status.developing", "corrected": "status.corrected"}

# interface language of the document being rendered (set by render / render_guardadas)
_LANG: ContextVar[str] = ContextVar("gaceta_render_lang", default="es")


def _t(key: str, n: int | None = None, **kw) -> str:
    return i18n.t(_LANG.get(), key, n, **kw)

_LOCAL_MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
READER_LINK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_CSS = icons.CSS + """
:root {
  color-scheme: light dark;
  --bg: #fbf9f3;
  --ink: #1c1b18;
  --muted: #6b675e;
  --rule: #d9d3c5;
  --accent: #8a2f2b;
  --card: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #151513;
    --ink: #ece8df;
    --muted: #a7a195;
    --rule: #3a3833;
    --accent: #e0908a;
    --card: #1e1d1a;
  }
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--ink); }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  font-size: 1rem;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { width: min(100% - 2.5rem, 68rem); margin-inline: auto; }
.masthead {
  padding: 1.4rem 0 0.9rem;
  margin-bottom: 1.4rem;
  text-align: center;
  border-bottom: 1px solid var(--ink);
}
.masthead h1 {
  margin: 0;
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-size: clamp(2.2rem, 8vw, 4.2rem);
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.03em;
}
.dateline {
  margin: 0.5rem 0 0;
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.04em;
}
.reader { color: var(--ink); font-weight: 600; }
.folio-row, .dateline-row {
  display: flex;
  gap: 0.55rem;
  align-items: baseline;
  justify-content: space-between;
  margin: 0;
  color: var(--muted);
  font-size: 0.75rem;
  letter-spacing: 0.03em;
}
.folio-row {
  padding: 0.5rem 0 0.45rem;
  margin-bottom: 0.4rem;
  border-bottom: 1px solid var(--rule);
}
.dateline-row { margin-top: 0.55rem; }
.folio-row .slot, .dateline-row .slot {
  flex: 1 1 0;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.folio-row .slot, .dateline-row .slot { text-align: left; }
.folio-row .slot-mid, .dateline-row .slot-mid { flex: 1.6 1 0; text-align: center; }
.folio-row .slot-right, .dateline-row .slot-right { text-align: right; }
.folio-row .short { display: none; }
@media (max-width: 480px) {
  .folio-row .long, .dateline-row .wide-only { display: none; }
  .folio-row .short { display: inline; }
}
.instrument {
  margin: 0 0 0.7rem;
  color: var(--muted);
  font-size: 0.75rem;
  letter-spacing: 0.03em;
}
h1, h2, h3, h4 {
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  line-height: 1.15;
}
.story { padding-top: 1.1rem; border-top: 1px solid var(--rule); }
.story + .story { margin-top: 1.3rem; }
.story::after { content: ""; display: block; clear: both; }
.story-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.35rem 0.85rem;
}
.headline { margin: 0; font-weight: 700; letter-spacing: -0.01em; }
.story h3.headline { font-size: clamp(1.25rem, 3.5vw, 1.55rem); }
.lead h2.headline { font-size: clamp(2rem, 6.5vw, 3.15rem); }
.badge {
  display: inline-block;
  padding: 0.24rem 0.55rem;
  border: 1px solid currentColor;
  border-radius: 999px;
  color: var(--accent);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  line-height: 1;
  text-transform: uppercase;
  white-space: nowrap;
}
.summary {
  max-width: 68ch;
  margin: 0.55rem 0 0;
  font-family: Charter, "Iowan Old Style", "Source Serif 4", Georgia, serif;
  font-size: 1.04rem;
  line-height: 1.58;
}
.lead .summary { font-size: 1.12rem; }
.new-coverage {
  max-width: 68ch;
  margin: 0.55rem 0 0;
  font-size: 0.88rem;
  line-height: 1.45;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.why {
  max-width: 68ch;
  margin: 0.7rem 0 0;
  padding: 0.05rem 0 0.05rem 0.8rem;
  border-left: 2px solid var(--accent);
  color: var(--muted);
  font-size: 0.92rem;
  line-height: 1.5;
}
.why-label {
  margin: 0 0 0.1rem;
  color: var(--accent);
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}
.why p:last-child { margin: 0; }
.corrections {
  max-width: 72ch;
  margin: 1rem 0 0;
  padding: 0.7rem 0.95rem;
  border-left: 3px solid var(--accent);
  background: var(--card);
}
.corrections p { margin: 0.2rem 0; }
.since {
  max-width: 68ch;
  margin: 0.8rem 0 0;
  padding: 0.6rem 0.9rem;
  border-left: 3px solid var(--accent);
  background: var(--card);
  font-family: system-ui, sans-serif;
  font-size: 0.92rem;
}
.since[hidden] { display: none; }
.since-label {
  margin: 0 0 0.25rem;
  color: var(--accent);
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}
.since-steps { margin: 0.2rem 0; padding-left: 1.1rem; }
.since-steps li { margin: 0.15rem 0; }
.since-note { margin: 0.2rem 0; color: var(--muted); }
.since-note:empty { display: none; }
.since-before { margin-top: 0.35rem; color: var(--muted); }
.since-before summary { cursor: pointer; font-size: 0.8rem; }
.since-before p { margin: 0.3rem 0 0; font-style: italic; }
.sources li .src-new {
  margin-left: 0.35rem;
  color: var(--accent);
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.corrections .said { color: var(--muted); font-weight: 600; }
.corrections .now { color: var(--accent); font-weight: 600; }
img, video, iframe { max-width: 100%; }
img { height: auto; }
.sources {
  max-width: 68ch;
  margin-top: 0.8rem;
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.35;
}
.sources-label {
  margin: 0 0 0.3rem;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}
.sources ul {
  display: grid;
  gap: 0.15rem;
  margin: 0;
  padding: 0;
  list-style: none;
}
.sources a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, var(--accent) 55%, transparent);
  text-decoration-thickness: 1px;
  text-underline-offset: 0.18em;
}
.sources a:hover { text-decoration-thickness: 2px; }
.sources li {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  min-height: 2rem;
  white-space: nowrap;
  overflow: hidden;
}
.sources li > a { overflow: hidden; text-overflow: ellipsis; flex: 1 1 auto; min-width: 45%; }
.sources li > .source-meta { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.sources details li { white-space: nowrap; }
.source-thumb {
  width: 1.5rem;
  height: 1.5rem;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--rule);
  flex: none;
}
.sources li, .sources a { min-width: 0; }
.fuente { margin: 0.3rem 0 0; color: var(--muted); font-size: 0.78rem; letter-spacing: 0.02em; }
.fuente a { color: var(--muted); text-decoration: underline; text-decoration-color: var(--rule); }
.headline a { color: inherit; text-decoration: none; }
.headline a:hover { color: var(--accent); }
.media-frame {
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1px solid var(--rule);
  border-radius: 8px;
  background: var(--card);
}
.media-frame img, .yt-lite img, .story-thumb-wrap img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.lead .media-frame, .lead .yt-lite, .lead video { margin-bottom: 1rem; }
.card-media { margin-bottom: 0.75rem; }
.story-thumb-wrap {
  float: right;
  width: 6.25rem;
  aspect-ratio: 1;
  overflow: hidden;
  border-radius: 4px;
  margin: 0.25rem 0 0.4rem 0.85rem;
  background: var(--card);
}
.portada .story:not(.lead) .card-media {
  float: right;
  width: 6.25rem;
  aspect-ratio: 1;
  margin: 0.25rem 0 0.4rem 0.85rem;
  overflow: hidden;
  border-radius: 4px;
}
.portada .story:not(.lead) .card-media > * { width: 100%; height: 100%; margin: 0; border-radius: 0; border: 0; aspect-ratio: auto; }
.portada .story:not(.lead) .card-media img { width: 100%; height: 100%; object-fit: cover; display: block; }
.portada .story:not(.lead) .yt-play { transform: scale(.6); }
.story video {
  width: 100%;
  aspect-ratio: 16 / 9;
  border: 1px solid var(--rule);
  border-radius: 8px;
  background: #000;
  margin-bottom: 0.9rem;
  display: block;
}
.yt-lite {
  position: relative;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1px solid var(--rule);
  border-radius: 8px;
  background: #000;
}
.yt-lite a { display: block; width: 100%; height: 100%; }
.yt-lite button {
  position: relative;
  display: block;
  width: 100%;
  height: 100%;
  padding: 0;
  border: 0;
  background: #000;
  cursor: pointer;
}
.yt-play {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 68px;
  height: 48px;
  margin: -24px 0 0 -34px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.7);
  pointer-events: none;
}
.yt-play::after {
  content: "";
  position: absolute;
  top: 50%;
  left: 50%;
  width: 0;
  height: 0;
  margin: -11px 0 0 -6px;
  border-left: 18px solid #fff;
  border-top: 11px solid transparent;
  border-bottom: 11px solid transparent;
}
.yt-lite iframe { width: 100%; height: 100%; border: 0; display: block; }
@media (min-width: 44rem) {
  .section .story.with-thumb {
    display: grid;
    grid-template-columns: 9.5rem minmax(0, 1fr);
    gap: 1.1rem;
    align-items: start;
  }
  .section .story.with-thumb .story-thumb-wrap { float: none; width: auto; aspect-ratio: 4 / 3; margin: 0.3rem 0 0; }
}
.portada {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1.5rem;
  margin-bottom: 2.5rem;
}
.portada .story { border-top: 0; padding-top: 0; }
.portada .lead { grid-column: 1 / -1; }
.portada .story:not(.lead) { border-top: 1px solid var(--rule); padding-top: 1rem; }
@media (min-width: 44rem) {
  .portada { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.section { margin: 2.4rem 0; }
.section > h2 {
  position: sticky;
  top: 0;
  z-index: 5;
  margin: 0 0 0.4rem;
  padding: 0.55rem 0 0.4rem;
  background: var(--bg);
  border-bottom: 1px solid var(--ink);
  font-size: clamp(1.25rem, 4vw, 1.7rem);
}
.section > h2 + * { border-top: 0; }
body { padding-bottom: 4rem; }
@media (prefers-reduced-motion: reduce) {
  * { scroll-behavior: auto; }
}
.conditions {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  margin: 2rem 0 0;
}
.cond-box {
  flex: 1 1 18rem;
  padding: 0.75rem 0.9rem 0.6rem;
  border: 1px solid var(--rule);
  border-top: 3px double var(--ink);
  background: var(--card);
}
.cond-box h2 {
  margin: 0 0 0.3rem;
  font-size: 0.8rem;
  font-family: inherit;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.cond-lead {
  margin: 0 0 0.45rem;
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-size: 1.55rem;
  line-height: 1.15;
}
.cond-facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(16.5rem, 1fr));
  gap: 0.15rem 1.2rem;
  margin: 0;
  font-size: 0.85rem;
}
.cond-facts div {
  display: flex;
  justify-content: space-between;
  gap: 0.6rem;
  border-bottom: 1px dotted var(--rule);
  padding: 0.12rem 0;
}
.cond-facts dt { color: var(--muted); white-space: nowrap; }
.cond-facts dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
@media (min-width: 30rem) { .cond-facts dd { white-space: nowrap; } }
.cond-lead .wx { width: 1.15em; height: 1.15em; margin-right: 0.3rem; vertical-align: -0.2em; }
.cond-facts dd .wx { margin-right: 0.3rem; font-size: 1.15em; vertical-align: -0.22em; }
.nameplate { position: relative; }
.ear {
  position: absolute;
  right: 0;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.25rem 0 0.25rem 0.7rem;
  border-left: 1px solid var(--rule);
  color: var(--ink);
  text-align: left;
  text-decoration: none;
  line-height: 1.1;
}
.ear .wx { font-size: 2.3rem; }
.ear-temp { display: block; font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-size: 1.5rem; font-weight: 700; }
.ear-note { display: block; color: var(--muted); font-size: 0.7rem; letter-spacing: 0.02em; }
@media (max-width: 52rem) {
  .ear { position: static; transform: none; justify-content: center; border: 0;
    padding: 0.35rem 0 0; }
  .ear .wx { font-size: 1.6rem; }
  .ear-temp { display: inline; font-size: 1.1rem; }
  .ear-note { display: inline; margin-left: 0.35rem; }
}
.cond-stamp { margin: 0.45rem 0 0; color: var(--muted); font-size: 0.72rem; letter-spacing: 0.03em; }
.discovery .src-entry { padding-top: .9rem; border-top: 1px solid var(--rule); }
.discovery .src-entry + .src-entry { margin-top: .9rem; }
.discovery .src-head { margin: 0; overflow-wrap: anywhere; }
.src-name { font-weight: 600; color: var(--ink); }
a.src-name {
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, var(--accent) 55%, transparent);
  text-decoration-thickness: 1px;
  text-underline-offset: .18em;
}
a.src-name:hover { text-decoration-thickness: 2px; }
.src-meta { color: var(--muted); font-size: .8rem; margin-left: .45rem; }
.src-links { margin: .3rem 0 0; color: var(--muted); font-size: .85rem; }
.src-links a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--rule);
  text-underline-offset: .18em;
}
.saved-link {
  color: var(--accent);
  font-weight: 600;
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, var(--accent) 55%, transparent);
  text-underline-offset: .18em;
}
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  margin: 0 0 1.2rem;
}
.filter-bar input[type="search"] {
  flex: 1 1 12rem;
  min-width: 0;
  padding: 0.45rem 0.6rem;
  border: 1px solid var(--rule);
  border-radius: 6px;
  background: var(--card);
  color: var(--ink);
  font: inherit;
  font-size: 0.9rem;
}
.filter-bar select {
  padding: 0.45rem 0.4rem;
  border: 1px solid var(--rule);
  border-radius: 6px;
  background: var(--card);
  color: var(--ink);
  font: inherit;
  font-size: 0.9rem;
}
.save-note {
  max-width: 68ch;
  margin: 0.55rem 0 0;
  padding: 0.05rem 0 0.05rem 0.8rem;
  border-left: 2px solid var(--rule);
  color: var(--muted);
  font-size: 0.9rem;
  font-style: italic;
}
.save-meta {
  margin: 0.4rem 0 0;
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.02em;
}
.save-meta .badge { margin-right: 0.3rem; }
"""


SOURCES_SHOWN = 3


def _esc(value: object) -> str:
    """Escape any edition value for HTML text or a quoted attribute."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _long_date(value: object) -> str:
    """ISO date → long date in the page language (e.g. "martes 17 de noviembre de 2026")."""
    text = str(value or "").strip()
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return text
    return i18n.long_date(_LANG.get(), day)


def _hhmm(value: object) -> str:
    """ISO datetime → local HH:MM (in the offset it carries); '' when unparsable."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%H:%M")
    except ValueError:
        return ""
    except TypeError:
        return ""


def _count(value) -> int | None:
    """Best-effort non-negative int from edition JSON; None when absent/invalid."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _folio_row(folio: dict, edition: dict) -> str:
    """Honest furniture above the masthead: No. code · date · 24 h window.
    Never a headline teaser, never a price. Slots shrink with ellipsis."""
    slots = []
    code = str(folio.get("code") or "").strip()
    if code:
        slots.append(f'<span class="slot">{_esc(_t("folio.no", code=code))}</span>')
    raw_date = str(edition.get("date") or "")
    try:
        day = date.fromisoformat(raw_date[:10])
    except ValueError:
        day = None
    label = (str(folio.get("date_label") or "").strip() if folio.get("lang", "es") == _LANG.get()
             else "") or _long_date(raw_date)
    if label:
        time_attr = f' datetime="{_esc(raw_date)}"' if raw_date else ""
        short = (i18n.short_date(_LANG.get(), day) if day else
                 " ".join(label.split()[:2] + [w[:3] for w in label.split()[3:4]]))
        slots.append(f'<span class="slot slot-mid"><time{time_attr}><span class="long">{_esc(label)}</span>'
                     f'<span class="short">{_esc(short)}</span></time></span>')
    window = str(folio.get("window_end") or "").strip() or _hhmm(edition.get("built_at"))
    if window:
        slots.append(f'<span class="slot slot-right">{_esc(_t("folio.window", time=window))}</span>')
    return f'<p class="folio-row">{"".join(slots)}</p>' if slots else ""


def _dateline_row(folio: dict, edition: dict, reader_name: str) -> str:
    """How big the day was, with denominators: portada/stories · items · voices."""
    stories = edition.get("stories") or {}
    left = (f'<span class="slot"><span class="reader">{_esc(_t("dateline.for", name=reader_name))}</span></span>'
            if reader_name else "")
    portada_n = _count(folio.get("portada"))
    if portada_n is None:
        portada_n = len([s for s in edition.get("portada") or [] if s in stories])
    stories_total = _count(folio.get("stories_total")) or len(stories)
    items_total = _count(folio.get("items_total"))
    mid = _esc(_t("dateline.stories", n=portada_n, total=stories_total))
    if items_total is not None:
        mid += f'<span class="wide-only"> · {_esc(_t("dateline.items", n=items_total))}</span>'
    voices = _count(folio.get("voices"))
    right = (f'<span class="slot slot-right">{_esc(_t("dateline.voices", n=voices))}</span>'
             if voices is not None else "")
    return (f'<p class="dateline-row">{left}'
            f'<span class="slot slot-mid">{mid}</span>{right}</p>')


def _instrument_line(instrument: dict, max_level: str) -> str:
    """Taste transparency: how many portada stories are there because of the
    owner's reactions. Only on the owner's private/household page (never public),
    only when taste actually moved something (k > 0)."""
    k = _count(instrument.get("portada_by_taste"))
    n = _count(instrument.get("portada"))
    if not k or n is None or n <= 0:
        return ""
    if privacy.rank(max_level) <= privacy.rank("public"):
        return ""
    return f'<p class="instrument">{_esc(_t("instrument", k=k, n=n))}</p>'


def _short_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return text


def _safe_url(value: object) -> str | None:
    """Return the URL only when it is an http(s) URL with a host."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return text


def _media_src(value: object, media_prefix: str) -> str | None:
    """Validate a media src: ``media/<safe-name>`` (resolved via media_prefix)
    or a remote https URL. Anything else (javascript:, data:, ../, ...) → None."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("https://"):
        try:
            return text if urlsplit(text).netloc else None
        except ValueError:
            return None
    if text.startswith("media/"):
        name = text[len("media/"):]
        if _LOCAL_MEDIA_NAME_RE.fullmatch(name) and ".." not in name:
            return media_prefix + name
    return None


def _img(m: dict, src: str, cls: str = "") -> str:
    attrs = [f'src="{_esc(src)}"']
    alt = _esc(m.get("alt") or "")
    attrs.append(f'alt="{alt}"')
    attrs.append('loading="lazy"')
    attrs.append('decoding="async"')
    try:
        if int(m.get("width") or 0) > 0 and int(m.get("height") or 0) > 0:
            attrs.append(f'width="{int(m["width"])}" height="{int(m["height"])}"')
    except (TypeError, ValueError):
        pass
    if cls:
        attrs.append(f'class="{_esc(cls)}"')
    return f"<img {' '.join(attrs)}>"


def _youtube_lite(m: dict, media_prefix: str) -> str:
    """Thumbnail + play button; the tiny inline JS swaps in the iframe on click.
    Without JS the button sits inside a link to the watch URL."""
    vid = str(m.get("video_id") or "")
    if not YOUTUBE_ID_RE.fullmatch(vid):
        return ""
    src = _media_src(m.get("src"), media_prefix)
    if not src:
        return ""
    watch = f"https://www.youtube.com/watch?v={vid}"
    return (
        '<div class="yt-lite">'
        f'<a href="{watch}" target="_blank" rel="noopener noreferrer" '
        f'aria-label="{_esc(_t("yt.play"))}">'
        f'<button type="button" data-yt="{vid}">'
        f"{_img(m, src)}"
        '<span class="yt-play" aria-hidden="true"></span>'
        "</button></a></div>"
    )


def _video_html(m: dict, media_prefix: str) -> str:
    src = _media_src(m.get("src"), media_prefix)
    if not src:
        return ""
    return f'<video controls preload="none" src="{_esc(src)}"></video>'


def _lead_media_html(story: dict, media_prefix: str) -> str:
    media = story.get("media") or []
    if not media:
        return ""
    lead = media[0]
    mtype = str(lead.get("type") or "")
    if mtype == "youtube":
        out = _youtube_lite(lead, media_prefix)
    elif mtype == "image":
        src = _media_src(lead.get("src"), media_prefix)
        out = f'<div class="media-frame">{_img(lead, src)}</div>' if src else ""
    elif mtype == "video":
        out = _video_html(lead, media_prefix)
    else:
        out = ""
    for extra in media[1:]:
        if str(extra.get("type") or "") == "video":
            out += _video_html(extra, media_prefix)
    return out


def _thumb_media_html(story: dict, media_prefix: str, wrap_class: str) -> str:
    """First usable image-ish media (image or a youtube thumbnail) as a plain img."""
    for m in story.get("media") or []:
        mtype = str(m.get("type") or "")
        if mtype not in ("image", "youtube"):
            continue
        if mtype == "youtube" and not YOUTUBE_ID_RE.fullmatch(str(m.get("video_id") or "")):
            continue
        src = _media_src(m.get("src"), media_prefix)
        if src:
            return f'<div class="{wrap_class}">{_img(m, src)}</div>'
    return ""


def _story_link(story: dict) -> str | None:
    """The story's origin (or best primary) source URL."""
    sources = story.get("sources") or []
    for source in sources:
        if str(source.get("role") or "") == "origin":
            url = _safe_url(source.get("url"))
            if url:
                return url
    for source in sources:
        url = _safe_url(source.get("url"))
        if url:
            return url
    return None


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _render_source(source: dict, media_prefix: str = "../media/") -> str:
    url = _safe_url(source.get("url"))
    title = _esc(source.get("title") or _t("source"))
    thumb = ""
    thumb_src = _media_src(source.get("thumb"), media_prefix)
    if thumb_src:
        thumb = _img({"alt": ""}, thumb_src, cls="source-thumb")
    if url:
        label = f'<a href="{_esc(url)}" rel="noopener noreferrer">{title}</a>'
    else:
        label = f'<span class="source-title">{title}</span>'

    meta_bits = [
        str(source[key]).strip()
        for key in ("author", "kind")
        if source.get(key) and str(source[key]).strip()
    ]
    published = _short_date(source.get("published"))
    if published:
        meta_bits.append(published)
    meta = _esc(" · ".join(meta_bits))
    added = _count(source.get("added"))
    li = f'<li data-added="{added}">' if added else "<li>"
    if meta:
        return f"{li}{thumb}{label}<span class=\"source-meta\"> · {meta}</span></li>"
    return f"{li}{thumb}{label}</li>"


def _render_since(story: dict) -> str:
    """Hidden 'Desde que la viste' block. Only the page knows when this person
    marked the story seen (/api/state seen_at), so it ships every rewrite with
    its time and react.js shows the ones after that mark, plus the text they
    read back then. Without JS (print, PDF) it stays hidden."""
    history = [h for h in (story.get("history") or []) if isinstance(h, dict)]
    if len(history) < 2:
        return ""
    parts = ['<div class="since" hidden>',
             f'<p class="since-label">{_esc(_t("since.label"))}</p>',
             '<ul class="since-steps">']
    for h in history[1:]:
        at = _count(h.get("at"))
        text = str(h.get("what_changed") or "").strip()
        if at and text:
            parts.append(f'<li data-at="{at}">{_esc(text)}</li>')
    parts.append("</ul>")
    parts.append('<p class="since-note"></p>')
    parts.append(f'<details class="since-before"><summary>{_esc(_t("since.before"))}</summary>')
    for h in history[:-1]:
        at = _count(h.get("at"))
        if at and h.get("summary"):
            parts.append(f'<p data-at="{at}">{_esc(h.get("summary"))}</p>')
    parts.append("</details>")
    parts.append("</div>")
    return "\n".join(parts)


def _render_story(story_id: str, story: dict, *, lead: bool = False,
                  placement: str = "row", media_prefix: str = "../media/") -> str:
    """placement: 'lead' (portada head story, big media), 'card' (portada card,
    image on top) or 'row' (section story, small thumb left on desktop)."""
    classes = "story lead" if lead else "story"
    heading = "h2" if lead else "h3"
    status = str(story.get("status") or "").strip().lower()
    badge = _t(STATUS_KEYS[status]) if status in STATUS_KEYS else ""

    media_html = _lead_media_html(story, media_prefix) if lead else (
        _thumb_media_html(story, media_prefix, "card-media") if placement == "card"
        else _thumb_media_html(story, media_prefix, "story-thumb-wrap")
    )
    if placement == "row" and media_html:
        classes += " with-thumb"

    parts = [f'<article class="{classes}" id="{_esc(story_id)}"'
             f' data-rev="{_esc(story.get("rev") or "")}">']
    if placement == "row" and not lead:
        parts.append(media_html)          # direct child: grid column 1 on desktop
        parts.append('<div class="story-body">')  # grid column 2
    else:
        parts.append(media_html)

    link = _story_link(story)
    headline = _esc(story.get("title"))
    if link:
        headline = f'<a href="{_esc(link)}" rel="noopener noreferrer">{headline}</a>'
    parts.append('<header class="story-head">')
    parts.append(f'<{heading} class="headline">{headline}</{heading}>')
    if badge:
        parts.append(f'<span class="badge">{_esc(badge)}</span>')
    parts.append("</header>")

    if link:
        host = _host(link)
        if host:
            parts.append(f'<p class="fuente">{_esc(_t("story.source"))} <a href="{_esc(link)}" '
                         f'rel="noopener noreferrer">{_esc(host)}</a></p>')

    if story.get("summary"):
        parts.append(f'<p class="summary">{_esc(story.get("summary"))}</p>')

    sources = story.get("sources") or []
    url = _safe_url(story.get("new_coverage"))
    if url:
        title = next((s.get("title") for s in sources if s.get("url") == url and s.get("title")), None)
        if title:
            parts.append(f'<p class="new-coverage"><strong>{_esc(_t("story.coverage"))}</strong> '
                         f'<a href="{_esc(url)}" rel="noopener noreferrer">{_esc(title)}</a></p>')

    since = _render_since(story)
    if since:
        parts.append(since)

    if story.get("why_it_matters"):
        parts.append(
            '<aside class="why">'
            f'<p class="why-label">{_esc(_t("story.why"))}</p>'
            f'<p>{_esc(story.get("why_it_matters"))}</p>'
            "</aside>"
        )

    corrections = story.get("corrections") or []
    if corrections:
        parts.append(f'<section class="corrections" aria-label="{_esc(_t("corrections"))}">')
        for correction in corrections:
            parts.append(
                f'<p><span class="said">{_esc(_t("corrections.said"))}</span> '
                f'{_esc(correction.get("said"))}</p>'
            )
            parts.append(
                f'<p><span class="now">{_esc(_t("corrections.now"))}</span> '
                f'{_esc(correction.get("now"))}</p>'
            )
        parts.append("</section>")

    if sources:
        parts.append('<footer class="sources">')
        parts.append(f'<p class="sources-label">{_esc(_t("sources"))}</p>')
        # newest first; long threads fold behind <details> so a 40-source story stays readable
        ordered = sorted(sources, key=lambda x: str(x.get("published") or ""), reverse=True)
        head, rest = ordered[:SOURCES_SHOWN], ordered[SOURCES_SHOWN:]
        parts.append("<ul>")
        parts.extend(_render_source(source, media_prefix) for source in head)
        parts.append("</ul>")
        if rest:
            parts.append(f"<details><summary>{_esc(_t('sources.more', n=len(rest)))}</summary><ul>")
            parts.extend(_render_source(source, media_prefix) for source in rest)
            parts.append("</ul></details>")
        parts.append("</footer>")

    if placement == "row" and not lead:
        parts.append("</div>")
    parts.append("</article>")
    return "\n".join(parts)


DISCOVERY_KIND_LABELS = {
    "youtube": "YouTube", "nostr": "Nostr", "rss": "RSS", "hn": "HN", "release": "Release",
}
DISCOVERY_KIND_KEYS = {"site": "discovery.kind.site", "other": "discovery.kind.other"}


def _short_label(raw: object, max_label: int = 32) -> str:
    """Hostnames with key-like labels (nsite.lol's 50-char base36 pubkeys)
    read as noise and used to blow the mobile layout: middle-ellipsize any
    dot-separated label longer than max_label."""
    text = str(raw or "")
    if " " in text:
        return text
    labels = text.split(".")
    out = [l if len(l) <= max_label else f"{l[:6]}…{l[-12:]}" for l in labels]
    return ".".join(out)


def _render_discovery_entry(entry: dict, stories: dict, *, trial: bool) -> str:
    """One 'Fuentes nuevas' entry: name, kind + reason meta, story anchors
    (trials) or external link (suggestions), and the .src-verdict placeholder
    the served page fills with Seguir / Quitar / Más tarde (owner only)."""
    key = _esc(entry.get("key"))
    name = _esc(_short_label(entry.get("name") or entry.get("key")))
    kind = str(entry.get("kind") or "")
    kind_label = _esc(_t(DISCOVERY_KIND_KEYS[kind]) if kind in DISCOVERY_KIND_KEYS
                      else DISCOVERY_KIND_LABELS.get(kind, kind))
    if trial:
        try:
            n = int(entry.get("n_items") or 0)
        except (TypeError, ValueError):
            n = 0
        reason = _t("discovery.trial", n)
    else:
        reason = str(entry.get("reason") or "")
    meta = " · ".join(part for part in (kind_label, _esc(reason)) if part)
    url = _safe_url(entry.get("url"))
    if url:
        name_html = (f'<a class="src-name" href="{_esc(url)}" '
                     f'rel="noopener noreferrer">{name}</a>')
    else:
        name_html = f'<span class="src-name">{name}</span>'
    parts = ['<div class="src-entry">',
             f'<p class="src-head">{name_html}',
             (f'<span class="src-meta">{meta}</span></p>' if meta else "</p>")]
    if trial:
        links = []
        for sid in (entry.get("story_ids") or [])[:3]:
            if sid not in stories:
                continue
            title = _esc((stories[sid] or {}).get("title") or sid)
            links.append(f'<a href="#{_esc(sid)}">{title}</a>')
        if links:
            parts.append(f'<p class="src-links">{_esc(_t("discovery.in_issue"))} {" · ".join(links)}</p>')
    parts.append(f'<div class="src-verdict" data-source-key="{key}" '
                 f'data-source-name="{name}"></div>')
    parts.append("</div>")
    return "\n".join(parts)


def _render_discovery(edition: dict, stories: dict) -> str:
    disc = edition.get("discovery") or {}
    trials = disc.get("trials") or []
    sugg = disc.get("suggestions") or []
    if not trials and not sugg:
        return ""
    parts = ['<section class="section discovery" data-section="fuentes-nuevas">',
             f"<h2>{_esc(_t('discovery.title'))}</h2>"]
    parts.extend(_render_discovery_entry(e, stories, trial=True) for e in trials)
    parts.extend(_render_discovery_entry(e, stories, trial=False) for e in sugg)
    parts.append("</section>")
    return "\n".join(parts)


def _cond_anchor(box: dict) -> str:
    return "cond-" + (re.sub(r"[^a-z0-9_-]", "", str(box.get("id") or "").lower()) or "box")


def _visible_conditions(edition: dict, max_level: str) -> list[dict]:
    return [box for box in edition.get("conditions") or []
            if isinstance(box, dict) and box.get("title")
            and privacy.allowed(box.get("privacy"), max_level)]


def _render_ear(edition: dict, max_level: str) -> str:
    """The nameplate's weather ear: the first visible box that offers one."""
    for box in _visible_conditions(edition, max_level):
        ear = str(box.get("ear") or "").strip()
        if not ear:
            continue
        note = str(box.get("ear_note") or "").strip()
        note_html = f'<span class="ear-note">{_esc(note)}</span>' if note else ""
        return (f'<a class="ear" href="#{_cond_anchor(box)}" title="{_esc(box["title"])}">'
                f'{icons.svg(box.get("icon"))}<span><span class="ear-temp">{_esc(ear)}</span>'
                f"{note_html}</span></a>")
    return ""


def _render_conditions(edition: dict, max_level: str) -> str:
    """Condiciones boxes (conditions.py): snapshot numbers, stamped with the
    capture time and the source, never live. Boxes above max_level are dropped
    silently (fail closed: unknown privacy counts as private)."""
    boxes = []
    for box in _visible_conditions(edition, max_level):
        anchor = _cond_anchor(box)
        parts = [f'<div class="cond-box" id="{anchor}">', f'<h2>{_esc(box["title"])}</h2>']
        if box.get("lead"):
            parts.append(f'<p class="cond-lead">{icons.svg(box.get("icon"))}{_esc(box["lead"])}</p>')
        facts = [row for row in box.get("facts") or []
                 if isinstance(row, (list, tuple)) and len(row) in (2, 3)]
        if facts:
            parts.append('<dl class="cond-facts">')
            parts.extend(f"<div><dt>{_esc(row[0])}</dt><dd>"
                         f"{icons.svg(row[2]) if len(row) == 3 else ''}{_esc(row[1])}</dd></div>"
                         for row in facts)
            parts.append("</dl>")
        hhmm = _hhmm(box.get("observed_at")) or _hhmm(edition.get("built_at"))
        stamp = [_t("conditions.stamp", time=hhmm) if hhmm else _t("conditions.not_live")]
        if box.get("source"):
            stamp.append(str(box["source"]))
        parts.append(f'<p class="cond-stamp">{_esc(" · ".join(stamp))}</p>')
        parts.append("</div>")
        boxes.append("\n".join(parts))
    if not boxes:
        return ""
    return (f'<aside class="conditions" aria-label="{_esc(_t("conditions"))}">\n'
            + "\n".join(boxes) + "\n</aside>")


def _render_section(section: dict, stories: dict, media_prefix: str = "../media/") -> str:
    story_ids = [
        story_id
        for story_id in (section.get("stories") or [])
        if story_id in stories
    ]
    if not story_ids:
        return ""

    parts = [f'<section class="section" data-section="{_esc(section.get("id") or "")}">']
    parts.append(f'<h2>{_esc(section.get("title"))}</h2>')
    parts.extend(
        _render_story(story_id, stories[story_id], media_prefix=media_prefix)
        for story_id in story_ids
    )
    parts.append("</section>")
    return "\n".join(parts)


_YT_JS = (
    "document.addEventListener(\"click\",function(e){var t=e.target;"
    "if(!t||!t.closest)return;var b=t.closest(\"button[data-yt]\");if(!b)return;"
    "e.preventDefault();var id=b.getAttribute(\"data-yt\");"
    "if(!/^[A-Za-z0-9_-]{11}$/.test(id))return;"
    "var w=b.closest(\".yt-lite\")||b.parentNode;"
    "var f=document.createElement(\"iframe\");"
    "f.src=\"https://www.youtube-nocookie.com/embed/\"+id+\"?autoplay=1\";"
    "f.allow=\"autoplay; encrypted-media; picture-in-picture\";"
    "f.allowFullscreen=true;f.loading=\"lazy\";f.title=\"YouTube video\";"
    "w.innerHTML=\"\";w.appendChild(f);},false);"
)


def render(edition: dict, max_level: str, media_prefix: str = "../media/") -> str:
    """Render one edition snapshot as a self-contained HTML document.

    ``media_prefix`` resolves edition ``media/<file>`` paths for this output
    location (site/<reader>/index.html uses the default ``../media/``).

    Raises ``ValueError`` if any story's privacy is above ``max_level`` (unknown
    privacy labels count as ``private``).
    """
    reader = edition.get("reader") or {}
    token = _LANG.set(i18n.reader_lang(reader))
    try:
        return _render(edition, max_level, media_prefix)
    finally:
        _LANG.reset(token)


def _render(edition: dict, max_level: str, media_prefix: str) -> str:
    stories = edition.get("stories") or {}

    for story_id, story in stories.items():
        label = (story or {}).get("privacy")
        if not privacy.allowed(label, max_level):
            raise ValueError(
                f"story {story_id!r} has privacy {label!r}, above max level {max_level!r}"
            )

    reader = edition.get("reader") or {}
    reader_name = str(reader.get("name") or "").strip()
    date_text = _long_date(edition.get("date"))
    raw_date = str(edition.get("date") or "")
    folio = edition.get("folio") or {}

    paper = str(reader.get("masthead") or "").strip() or _t("paper.name")
    title_text = paper
    if date_text:
        title_text = f"{paper} — {date_text}"

    reader_id = str(reader.get("id") or "").strip()
    saved_link = ""
    if READER_LINK_RE.match(reader_id):
        # the saved archive is served at /r/<reader>/guardadas; a relative link
        # works for both index.html and dated files under that prefix
        saved_link = f'<a class="saved-link" href="guardadas">{_esc(_t("saved"))}</a>'

    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{_LANG.get()}">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title_text)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        '<header class="masthead">',
    ]
    ear = _render_ear(edition, max_level)
    nameplate = (f'<div class="nameplate"><h1>{_esc(paper)}</h1>{ear}</div>' if ear
                 else f"<h1>{_esc(paper)}</h1>")
    if folio:
        # folio row above the nameplate, dateline row below it (roadmap 11c)
        parts.append(_folio_row(folio, edition))
        parts.append(nameplate)
        parts.append(_dateline_row(folio, edition, reader_name))
    else:
        parts.append(nameplate)
        dateline = []
        if reader_name:
            dateline.append(f'<span class="reader">{_esc(_t("dateline.for", name=reader_name))}</span>')
        if date_text:
            dateline.append(f'<time datetime="{_esc(raw_date)}">{_esc(date_text)}</time>')
        if saved_link:
            dateline.append(saved_link)
        if dateline:
            parts.append(f'<p class="dateline">{" · ".join(dateline)}</p>')
    updated = []
    built_hhmm = _hhmm(edition.get("built_at"))
    if built_hhmm and not folio:
        # with a folio row the window line ("24 h a las HH:MM") already says it
        updated.append(_esc(_t("updated", time=built_hhmm)))
    next_hhmm = _hhmm(edition.get("next_update"))
    if next_hhmm:
        updated.append(_esc(_t("next_update", time=next_hhmm)))
    if folio and saved_link:
        updated.insert(0, saved_link)
    if updated:
        parts.append(f'<p class="dateline updated">{" · ".join(updated)}</p>')
    parts.append("</header>")
    parts.append("<main>")

    portada_ids = [
        story_id
        for story_id in (edition.get("portada") or [])
        if story_id in stories
    ]
    if portada_ids:
        instrument_html = _instrument_line(edition.get("instrument") or {}, max_level)
        if instrument_html:
            parts.append(instrument_html)
        parts.append('<section class="portada" data-section="portada" '
                     f'aria-label="{_esc(_t("portada"))}">')
        parts.extend(
            _render_story(story_id, stories[story_id], lead=(index == 0),
                          placement="lead" if index == 0 else "card",
                          media_prefix=media_prefix)
            for index, story_id in enumerate(portada_ids)
        )
        parts.append("</section>")

    # after the portada, never in the headline area (conditions.py)
    conditions_html = _render_conditions(edition, max_level)
    if conditions_html:
        parts.append(conditions_html)

    for section in edition.get("sections") or []:
        rendered = _render_section(section, stories, media_prefix)
        if rendered:
            parts.append(rendered)

    discovery_html = _render_discovery(edition, stories)
    if discovery_html:
        parts.append(discovery_html)

    parts.extend(["</main>", "</div>"])
    if "data-yt=" in "\n".join(parts):
        parts.append(f"<script>{_YT_JS}</script>")
    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)


# ------------------------------------------------------------ guardadas (v9)

_FILTER_JS = (
    "(function(){var q=document.getElementById(\"gf-q\");if(!q)return;"
    "q.addEventListener(\"input\",function(){var t=q.value.toLowerCase();"
    "document.querySelectorAll(\".g-entry\").forEach(function(el){"
    "el.style.display=el.getAttribute(\"data-text\").indexOf(t)>=0?\"\":\"none\";});});})();"
)


def _saved_date(at: object) -> str:
    try:
        d = datetime.fromtimestamp(int(at)).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return i18n.day_month_year(_LANG.get(), d)


def _render_guardada(entry: dict) -> str:
    """One saved entry: title, summary, note, sources (folded > 3)."""
    saved = _saved_date(entry.get("at"))

    title = _esc(entry.get("title") or _t("saved.untitled"))
    link = _safe_url(entry.get("url"))
    if link:
        title = f'<a href="{_esc(link)}" rel="noopener noreferrer">{title}</a>'
    parts = [
        '<article class="story g-entry" '
        f'data-text="{_esc(((entry.get("title") or "") + " " + (entry.get("summary") or "") + " " + (entry.get("note") or ""))).lower()}">',
        f'<header class="story-head"><h3 class="headline">{title}</h3></header>',
    ]
    if saved:
        parts.append(f'<p class="save-meta">{_esc(_t("saved.on", date=saved))}</p>')
    if entry.get("summary"):
        parts.append(f'<p class="summary">{_esc(entry.get("summary"))}</p>')
    if entry.get("note"):
        parts.append(f'<p class="save-note">“{_esc(entry.get("note"))}”</p>')
    sources = entry.get("sources") or []
    if sources:
        ordered = sorted(sources, key=lambda x: str(x.get("published") or ""), reverse=True)
        head, rest = ordered[:SOURCES_SHOWN], ordered[SOURCES_SHOWN:]
        parts.append(f'<footer class="sources"><p class="sources-label">{_esc(_t("sources"))}</p><ul>')
        parts.extend(_render_source(s, "/media/") for s in head)
        parts.append("</ul>")
        if rest:
            parts.append(f"<details><summary>{_esc(_t('sources.more', n=len(rest)))}</summary><ul>")
            parts.extend(_render_source(s, "/media/") for s in rest)
            parts.append("</ul></details>")
        parts.append("</footer>")
    parts.append("</article>")
    return "\n".join(parts)


def render_guardadas(reader_id: str, by: str, entries: list[dict], lang: str = "es",
                     masthead: str = "") -> str:
    """Saved archive page in ``lang`` (the reader's interface language)."""
    token = _LANG.set(i18n.normalize(lang))
    try:
        return _render_guardadas(reader_id, by, entries, masthead)
    finally:
        _LANG.reset(token)


def _render_guardadas(reader_id: str, by: str, entries: list[dict], masthead: str) -> str:
    """One person's saved archive (GET /r/<reader>/guardadas?by=): edition skeleton.

    ``entries`` (newest first): {title, url, summary, note, at, sources}.
    Every value is escaped here; only http(s) URLs become links.
    """
    dateline = []
    if str(by or "").strip():
        dateline.append(f'<span class="reader">{_esc(_t("saved.of", name=by))}</span>')
    dateline.append(f'<a class="saved-link" href=".">{_esc(_t("saved.back"))}</a>')
    paper = str(masthead or "").strip() or _t("paper.name")
    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{_LANG.get()}">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(paper)} — {_esc(_t('saved'))}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        '<header class="masthead">',
        f"<h1>{_esc(paper)}</h1>",
        f'<p class="dateline">{" · ".join(dateline)}</p>',
        "</header>",
        "<main>",
        '<section class="section" data-section="guardadas">',
        f"<h2>{_esc(_t('saved'))}</h2>",
        '<div class="filter-bar">'
        f'<input type="search" id="gf-q" placeholder="{_esc(_t("saved.filter"))}" '
        f'aria-label="{_esc(_t("saved.filter_aria"))}">'
        "</div>",
    ]
    if not str(by or "").strip():
        parts.append(f'<p class="summary">{_esc(_t("saved.open_hint"))}</p>')
    elif entries:
        parts.extend(_render_guardada(e) for e in entries)
    else:
        parts.append(f'<p class="summary">{_esc(_t("saved.empty"))}</p>')
    parts.extend(["</section>", "</main>", "</div>"])
    parts.append(f"<script>{_FILTER_JS}</script>")
    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)
