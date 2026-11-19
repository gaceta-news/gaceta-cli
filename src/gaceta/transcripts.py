"""YouTube transcripts: a video enters the paper with its transcript, or not at all.

Titles + descriptions are too clickbait-prone to edit from, so the RSS
adapter asks this module for a transcript and drops the video when there is none (it retries
on the next run, while the video is still inside max_age_days).

Reader config (all optional):

    [sources.rss.transcripts]
    cache_dir = "transcripts"          # relative to the data dir; gaceta's own cache
    extra_dirs = ["/path/other/cache"] # read-only lookups of other caches (<video_id>.txt)
    cookies_from_browser = "firefox:/path/to/profile"
    max_fetch = 12                     # new yt-dlp fetches per run
    min_words = 150                    # shorter = no transcript (music, shorts, silence)

Fetching uses yt-dlp (subtitles only, no media). A 429 sets a 6 h cooldown file so we don't
dig the hole deeper. Residential IP required: YouTube blocks this from datacenter IPs.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

LANGS = "es-orig,en-orig,es,en"
COOLDOWN_S = 6 * 3600
_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_VID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


class Transcript(NamedTuple):
    text: str
    public: bool  # proven-public evidence: fetched without cookies, or cached with 'anon' provenance


def video_id(url: str | None) -> str | None:
    m = _VID.search(url or "")
    return m.group(1) if m else None


def clean_vtt(text: str) -> str:
    lines, seen = [], set()
    for ln in text.splitlines():
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        ln = html.unescape(ln)
        if not ln or ln.isdecimal() or "-->" in ln or ln.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or ln in seen:
            continue
        seen.add(ln)
        lines.append(ln)
    return " ".join(lines)


class Transcripts:
    def __init__(self, cfg: dict | None, data_dir: Path, runner=subprocess.run, now=time.time):
        cfg = cfg or {}
        self.cache = Path(data_dir) / str(cfg.get("cache_dir", "transcripts"))
        self.extra = [Path(p) for p in cfg.get("extra_dirs") or []]
        self.cookies = str(cfg.get("cookies_from_browser") or "")
        self.budget = int(cfg.get("max_fetch", 12))
        self.min_words = int(cfg.get("min_words", 150))
        self.runner = runner
        self.now = now
        self.fetched = self.missing = 0
        self.rate_limited = False

    def _cooldown_file(self) -> Path:
        return self.cache / ".yt_429_until"

    def _cooling(self) -> bool:
        try:
            return self.now() < float(self._cooldown_file().read_text().strip())
        except (OSError, ValueError):
            return False

    def _ok(self, text: str) -> str | None:
        return text if len(text.split()) >= self.min_words else None

    def cached(self, vid: str) -> Transcript | None:
        """Cache hit with provenance. Only our OWN cache and an explicit 'anon'
        marker count as public — extra directories are someone else's cache (their
        sidecars are not ours to trust) and anything unmarked fails closed."""
        for d in [self.cache, *self.extra]:
            p = d / f"{vid}.txt"
            if p.is_file():
                try:
                    text = self._ok(p.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
                if not text:
                    continue
                public = False
                if d == self.cache:
                    try:
                        public = (d / f"{vid}.prov").read_text(encoding="utf-8").strip() == "anon"
                    except OSError:
                        public = False
                return Transcript(text, public)
        return None

    def get_with_origin(self, vid: str) -> Transcript | None:
        """Transcript (text, public provenance), or None (no subs, too short, budget
        spent, or rate-limited). `public` is True only for anonymous fetches and
        cache entries that carry 'anon' provenance — cookies can reach videos that
        are not publicly listed, and those must not ride the public label."""
        if not _ID.fullmatch(vid):
            return None
        hit = self.cached(vid)
        miss = self.cache / f"{vid}.none"
        if hit is not None or (miss.exists() and self.now() - miss.stat().st_mtime < COOLDOWN_S):
            return hit
        if self.rate_limited or self.budget <= 0 or self._cooling() or not shutil.which("yt-dlp"):
            return None
        self.budget -= 1
        self.cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.cache) as tmp:
            cmd = ["yt-dlp", "--ignore-config", "--no-playlist", "--skip-download", "--write-auto-subs", "--write-subs", "--sub-langs", LANGS,
                   "--sub-format", "vtt", "-o", os.path.join(tmp, "s.%(ext)s"),
                   f"https://www.youtube.com/watch?v={vid}"]
            if self.cookies:
                cmd[1:1] = ["--cookies-from-browser", self.cookies]
            try:
                r = self.runner(cmd, capture_output=True, text=True, timeout=180)
                err = r.stderr or ""
            except (subprocess.TimeoutExpired, OSError):
                return None
            vtts = sorted(Path(tmp).glob("*.vtt"), key=lambda p: ("orig" not in p.name, p.name))
            if not vtts:
                if "429" in err:
                    self.rate_limited = True
                    self._cooldown_file().write_text(str(self.now() + COOLDOWN_S))
                else:
                    # Temporary backoff: captions can arrive after upload.
                    miss.touch()
                    self.missing += 1
                return None
            text = clean_vtt(vtts[0].read_text(encoding="utf-8", errors="ignore"))
        # .prov first: a crash between the two writes must leave an unmarked .txt
        # (fail-closed private), never a public text without its marker.
        (self.cache / f"{vid}.prov").write_text("cookies" if self.cookies else "anon", encoding="utf-8")
        (self.cache / f"{vid}.txt").write_text(text, encoding="utf-8")
        self.fetched += 1
        if not self._ok(text):
            miss.touch()
            return None
        return Transcript(text, not self.cookies)

    def get(self, vid: str) -> str | None:
        """Text-only convenience wrapper (provenance lives on get_with_origin)."""
        t = self.get_with_origin(vid)
        return t.text if t else None
