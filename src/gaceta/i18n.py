"""Interface languages: one JSON catalog per language in ``gaceta/locales``.

The paper's interface language is the reader's ``ui_lang`` or, when unset, the
first of ``langs`` ("es,en" → es). Unsupported languages fall back to English;
missing keys fall back to English, then to the key itself. Adding a language is
one file: copy ``locales/en.json``, translate the values, keep the keys and the
``{placeholders}``.

No locale module, no machine-locale dependency: the same reader config renders the
same paper on any build machine.
"""
from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from importlib import resources

DEFAULT = "en"


@lru_cache(maxsize=None)
def _catalog(lang: str) -> dict:
    try:
        raw = (resources.files("gaceta") / "locales" / f"{lang}.json").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    return json.loads(raw)


@lru_cache(maxsize=1)
def supported() -> tuple[str, ...]:
    names = [p.name[:-5] for p in (resources.files("gaceta") / "locales").iterdir()
             if p.name.endswith(".json")]
    return tuple(sorted(names))


def normalize(lang: object) -> str:
    """'pt-BR' → 'pt'; unknown or empty → DEFAULT."""
    base = str(lang or "").strip().lower().replace("_", "-").split("-")[0]
    return base if base in supported() else DEFAULT


def langs_list(langs: object) -> list[str]:
    if isinstance(langs, (list, tuple)):
        items = [str(x) for x in langs]
    else:
        items = str(langs or "").split(",")
    return [x.strip() for x in items if x.strip()]


def reader_lang(cfg: dict | None) -> str:
    """Interface language for a reader config (or an edition's ``reader`` dict)."""
    cfg = cfg or {}
    explicit = cfg.get("ui_lang")
    if explicit:
        return normalize(explicit)
    first = (langs_list(cfg.get("langs")) or [DEFAULT])[0]
    return normalize(first)


def t(lang: str, key: str, n: int | None = None, **kw) -> str:
    """Translate ``key``; with ``n`` pick ``key_one`` / ``key_other`` when present."""
    lang = normalize(lang)
    keys = [key]
    if n is not None:
        keys = [f"{key}_{plural(lang, n)}", key]
        kw.setdefault("n", n)
    for cat in (_catalog(lang), _catalog(DEFAULT)):
        strings = cat.get("strings") or {}
        for k in keys:
            if k in strings:
                text = strings[k]
                return text.format(**kw) if kw else text
    return key


def plural(lang: str, n: int) -> str:
    if normalize(lang) == "fr":
        return "one" if abs(n) < 2 else "other"
    return "one" if n == 1 else "other"


def _dates(lang: str) -> dict:
    return _catalog(normalize(lang)).get("dates") or _catalog(DEFAULT)["dates"]


def _fmt_date(lang: str, day: date, pattern_key: str) -> str:
    d = _dates(lang)
    return d[pattern_key].format(
        weekday=d["weekdays"][day.weekday()],
        wd=d["weekdays_short"][day.weekday()],
        day=day.day,
        month=d["months"][day.month - 1],
        mon=d["months_short"][day.month - 1],
        year=day.year,
    )


def long_date(lang: str, day: date) -> str:
    """'miércoles 18 de noviembre de 2026' / 'Wednesday, November 18, 2026'."""
    return _fmt_date(lang, day, "long")


def short_date(lang: str, day: date) -> str:
    """Folio on small screens: 'miércoles 18 nov' / 'Wed, Nov 18'."""
    return _fmt_date(lang, day, "short")


def day_month_year(lang: str, day: date) -> str:
    """'18 de noviembre de 2026' / 'November 18, 2026'."""
    return _fmt_date(lang, day, "dmy")


def js_catalog(lang: str) -> dict:
    """Strings for the served page script (react.js), English-filled."""
    lang = normalize(lang)
    out = dict(_catalog(DEFAULT).get("js") or {})
    out.update(_catalog(lang).get("js") or {})
    out["_lang"] = lang
    out["_locale"] = (_catalog(lang).get("locale") or lang)
    return out


def js_script(lang: str) -> str:
    """Inline <script> defining window.GACETA_L10N (safe inside HTML)."""
    payload = json.dumps(js_catalog(lang), ensure_ascii=False).replace("</", "<\\/")
    return f"<script>window.GACETA_L10N={payload};</script>"


_HTML_LANG_RE = re.compile(r'<html lang="([A-Za-z-]{2,16})"')


def html_lang(html: str) -> str:
    m = _HTML_LANG_RE.search(html[:500])
    return normalize(m.group(1) if m else DEFAULT)


def language_name(code: str) -> str:
    """English name of a language code for LLM prompts ('es' → 'Spanish')."""
    base = str(code or "").strip().lower().split("-")[0]
    return LANGUAGE_NAMES.get(base, code)


LANGUAGE_NAMES = {
    "es": "Spanish", "en": "English", "pt": "Portuguese", "fr": "French", "de": "German",
    "it": "Italian", "nl": "Dutch", "ca": "Catalan", "gl": "Galician", "eu": "Basque",
    "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "nb": "Norwegian",
    "fi": "Finnish", "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "el": "Greek",
    "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "he": "Hebrew",
    "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "id": "Indonesian",
    "vi": "Vietnamese", "th": "Thai",
}

# ---------------------------------------------------------------- detection

# Function words only: frequent, short, rarely shared across these languages.
_STOP = {
    "es": "el la los las del que y en un una por con para se no es su al lo como más pero sus le ya o fue este ha sí porque esta son entre cuando muy sin sobre también hasta hay donde quien desde todo nos durante todos uno les ni contra otros ese eso ante ellos e esto mí antes algunos qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos cual poco ella estar estas algunas algo nosotros",
    "en": "the of and to in is that for it as was with be by on not he this are or his from at which but have an they you were her she there been their one all would will has more if its who than what when about into can only other new some could these after also so most over such many those then them may because should while where",
    "pt": "o a os as de do da dos das que e em um uma para com não por se na no mais ao como mas foi pela pelo ele das tem à seu sua ou ser quando muito nos já está também só pelos isso ela entre depois sem mesmo aos seus quem nas me esse eles você essa num nem suas meu às minha numa pelas havia seja qual será nós tenho lhe deles essas esses",
    "fr": "le la les de des du et en un une que qui pour dans ce il pas ne sur au se plus par avec est son sont mais comme ou leur elle aux été cette ses nous vous lui ont fait aussi être bien sans peut tout tous même deux entre très après donc",
    "de": "der die das und in den von zu mit sich des auf für ist im dem nicht ein eine als auch es an werden aus er hat dass sie nach wird bei einer um am sind noch wie einem über einen so zum war haben nur oder aber vor zur bis mehr durch man sein wurde",
    "it": "il di che la e a per un in è con non una sono le si da del della i al lo gli più ma come anche nel alla dei delle questo ha ci se loro tra suo sua essere quando molto stato fra già dopo senza cui degli nella ancora",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}
_WORD_RE = re.compile(r"[a-zà-öø-ÿ']+", re.IGNORECASE)


def detect(text: str, min_words: int = 8) -> str | None:
    """Best-guess language among the detectable ones, or None when unsure.

    Conservative on purpose: it only answers when the text is long enough and one
    language clearly wins, so a false 'wrong language' never costs an LLM call."""
    words = [w.lower().strip("'") for w in _WORD_RE.findall(text or "")]
    if len(words) < min_words:
        return None
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPSETS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, s1), (_, s2) = ranked[0], ranked[1]
    if s1 < 3 or s1 < 2 * s2 or s1 / len(words) < 0.12:
        return None
    return best


def mismatch(text: str, want: str) -> str | None:
    """Detected language when ``text`` is confidently NOT in ``want``; else None.
    Only judges languages the detector knows."""
    want = str(want or "").strip().lower().split("-")[0]
    if want not in _STOPSETS:
        return None
    got = detect(text)
    return got if got and got != want else None
