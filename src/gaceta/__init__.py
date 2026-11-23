"""gaceta: a personal newspaper edited for one reader.

The package version lives here and nowhere else: pyproject.toml reads it
(hatch dynamic version), the built wheel records it, and the packaged skill /
plugin / listing artifacts are stamped from it at build time by
scripts/build-packages.py.

0.3.0b1 is the P4 beta candidate: the published 0.2.x tags predate the
agent-demo wheel assets, `gaceta --version` and the packaged skills, so an
install pinned at 0.2.0 would point readers at missing features. The beta
tag v0.3.0b1 is cut at release review; parity CI rejects tag/wheel/docs
drift from this value.
"""

__version__ = "0.3.0b2"
