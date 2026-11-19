"""Export a local, read-only edition. Chromium is optional (PDF only)."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .render.html import render

# Only strip markup emitted by our renderer; edition text is HTML-escaped.
_INTERACTIVE = re.compile(
    r'<(?:script|video|button)\b[^>]*>[\s\S]*?</(?:script|video|button)>|'
    r'(?: · )?<a class="saved-link"[^>]*>[^<]*</a>|'
    r'<div class="yt-lite">[\s\S]*?</div>'
)
_IMG = re.compile(r'<img\b[^>]*>')

PRINT_CSS = """
<style id="gaceta-print">
.updated, .src-verdict { display: none; }
.media-frame:empty, .card-media:empty, .story-thumb-wrap:empty { display: none; }
@page { margin: 15mm; }
@media print {
  :root { color-scheme: light; }
  html, body { background: #fff; color: #111; }
  body { padding: 0; }
  .wrap { max-width: none; margin: 0; padding: 0; }
  .portada, .section .story.with-thumb { display: block; }
  h1, h2, h3, .story-head { break-after: avoid; }
  .story { break-inside: avoid; }
  .story h3.headline { font-size: 13pt; }
  .summary, .why { font-size: 11pt; }
  a { color: inherit; }
  img { max-width: 100%; }
}
</style>
"""


def print_html(edition: dict, max_level: str, *, with_images: bool = False,
               media_prefix: str = "../media/") -> str:
    """Render a static edition; cap privacy exactly as the live renderer does."""
    doc = render(edition, max_level, media_prefix=media_prefix)
    doc = _INTERACTIVE.sub("", doc)
    # Keep only cached images in this export; remote media must not load on open.
    doc = _IMG.sub(lambda m: m[0].replace(' loading="lazy"', '')
                   if with_images and 'src="https://' not in m[0] else "", doc)
    policy = ('<meta http-equiv="Content-Security-Policy" '
              'content="default-src \'none\'; style-src \'unsafe-inline\'; img-src file:">')
    return doc.replace("</head>", policy + PRINT_CSS + "</head>", 1)


def render_pdf(html_path: Path, out_path: Path, timeout: int = 90) -> Path:
    """Render the exported file with a fresh, sandboxed browser profile."""
    browser = next((path for name in ("chromium", "chromium-browser", "google-chrome",
                                     "google-chrome-stable") if (path := shutil.which(name))), None)
    if not browser:
        raise RuntimeError("no Chromium or Chrome found: cannot render a PDF")
    with tempfile.TemporaryDirectory(prefix="gaceta-print-") as tmp:
        pdf = Path(tmp) / "edition.pdf"
        cmd = [browser, "--headless", "--disable-gpu", "--disable-background-networking",
               "--no-first-run", f"--user-data-dir={tmp}/profile", "--no-pdf-header-footer",
               f"--print-to-pdf={pdf}", html_path.resolve().as_uri()]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Chromium timed out after {timeout}s") from exc
        if proc.returncode or not pdf.is_file() or not pdf.stat().st_size:
            raise RuntimeError("Chromium did not produce a PDF: " + proc.stderr.strip()[-500:])
        out_path.write_bytes(pdf.read_bytes())
    return out_path
