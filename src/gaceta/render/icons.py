"""Weather pictograms for condiciones boxes: small inline SVG line icons in the
spirit of a printed weather ear (ink lines, a touch of colour for sun, water and
lightning). Names follow the common weather-condition vocabulary (Home Assistant /
met.no style). No external files, no fonts; unknown names render nothing."""
from __future__ import annotations

_CLOUD = ('<path class="wx-cloud" d="M7.5 18h9.6a3.6 3.6 0 0 0 .5-7.17 5.2 5.2 0 0 0-10.05-.6'
          'A3.9 3.9 0 0 0 7.5 18z"/>')
_CLOUD_HI = ('<path class="wx-cloud" d="M7.5 14.5h9.6a3.4 3.4 0 0 0 .5-6.77 5 5 0 0 0-9.7-.6'
             'A3.7 3.7 0 0 0 7.5 14.5z"/>')
_SUN_RAYS = "".join(
    f'<line class="wx-sun" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>'
    for x1, y1, x2, y2 in ((12, 2.5, 12, 4.5), (12, 19.5, 12, 21.5), (2.5, 12, 4.5, 12),
                           (19.5, 12, 21.5, 12), (5.3, 5.3, 6.7, 6.7), (17.3, 17.3, 18.7, 18.7),
                           (5.3, 18.7, 6.7, 17.3), (17.3, 6.7, 18.7, 5.3)))
_SUN = f'<circle class="wx-sun wx-sun-disc" cx="12" cy="12" r="4.2"/>{_SUN_RAYS}'
_SMALL_SUN = ('<g transform="translate(1 .5) scale(.6)">'
              f'<circle class="wx-sun wx-sun-disc" cx="12" cy="12" r="4.2"/>{_SUN_RAYS}</g>')


def _drops(n: int) -> str:
    xs = {2: (9.5, 14.5), 3: (8, 12, 16), 5: (6.5, 9.5, 12.5, 15.5, 18.5)}[n]
    return "".join(f'<line class="wx-water" x1="{x}" y1="17" x2="{x - 1.2}" y2="20.5"/>' for x in xs)


_BOLT = '<path class="wx-bolt" d="M12.8 14.8l-2.3 3.4h3l-2.1 3.6"/>'

ICONS = {
    "sunny": _SUN,
    "clear-night": '<path class="wx-moon" d="M19 14.6A7.5 7.5 0 1 1 9.4 5a6 6 0 0 0 9.6 9.6z"/>',
    "partlycloudy": _SMALL_SUN + '<path class="wx-cloud wx-fill" d="M9 19.5h8.8a3.3 3.3 0 0 0 .45-6.57'
                    ' 4.8 4.8 0 0 0-9.25-.55A3.6 3.6 0 0 0 9 19.5z"/>',
    "cloudy": _CLOUD,
    "fog": _CLOUD_HI + "".join(f'<line class="wx-fog" x1="{a}" y1="{y}" x2="{b}" y2="{y}"/>'
                               for a, b, y in ((4, 20, 17.5), (6, 18, 20.5))),
    "rainy": _CLOUD_HI + _drops(3),
    "pouring": _CLOUD_HI + _drops(5),
    "lightning": _CLOUD_HI + _BOLT,
    "lightning-rainy": _CLOUD_HI + _BOLT + '<line class="wx-water" x1="8" y1="17" x2="6.8" y2="20.5"/>'
                       '<line class="wx-water" x1="17" y1="17" x2="15.8" y2="20.5"/>',
    "hail": _CLOUD_HI + "".join(f'<circle class="wx-water" cx="{x}" cy="{y}" r="1"/>'
                                for x, y in ((8, 18), (12, 20), (16, 18))),
    "snowy": _CLOUD_HI + "".join(f'<circle class="wx-snow" cx="{x}" cy="{y}" r=".9"/>'
                                 for x, y in ((8, 18), (12, 20.3), (16, 18))),
    "snowy-rainy": _CLOUD_HI + '<circle class="wx-snow" cx="8.5" cy="18.5" r=".9"/>'
                   '<line class="wx-water" x1="12.5" y1="17" x2="11.3" y2="20.5"/>'
                   '<circle class="wx-snow" cx="16" cy="18.5" r=".9"/>',
    "windy": '<path class="wx-wind" d="M3 9h11a2.5 2.5 0 1 0-2.5-2.5M3 13h15a2.5 2.5 0 1 1-2.5 2.5'
             'M3 17h7"/>',
    "windy-variant": '<path class="wx-cloud" d="M8 12h8.5a3 3 0 0 0 .4-6 4.4 4.4 0 0 0-8.5-.5A3.2 3.2 0 0 0 8 12z"/>'
                     '<path class="wx-wind" d="M3 15.5h12a2.2 2.2 0 1 1-2.2 2.2M3 19h8"/>',
    "exceptional": '<path class="wx-bolt" d="M12 4l9 16H3z"/><line class="wx-cloud" x1="12" y1="10" x2="12" y2="14"/>'
                   '<circle class="wx-cloud" cx="12" cy="17" r=".5"/>',
}

CSS = """
.wx { width: 1em; height: 1em; vertical-align: -0.14em; fill: none; stroke: currentColor;
  stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.wx .wx-sun { stroke: #d08a12; }
.wx .wx-sun-disc { fill: #f4c34d; fill-opacity: .35; }
.wx .wx-water { stroke: #3f86c2; }
.wx .wx-snow { stroke: #7fa9c9; fill: #7fa9c9; }
.wx .wx-bolt { stroke: #d08a12; }
.wx .wx-fill { fill: var(--card, #fff); }
.wx .wx-fog, .wx .wx-wind { stroke: var(--muted, currentColor); }
"""


def svg(name: object, label: str = "") -> str:
    """Inline SVG for a condition name, or '' when unknown. ``label`` becomes the
    accessible name (already-escaped text expected); without it the icon is decorative."""
    body = ICONS.get(str(name or "").strip().lower())
    if not body:
        return ""
    a11y = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    return f'<svg class="wx" viewBox="0 0 24 24" {a11y}>{body}</svg>'
