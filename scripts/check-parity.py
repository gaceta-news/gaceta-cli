#!/usr/bin/env python3
"""The P4 parity gate: one version, one metadata source, no drift.

Offline, no network, no model calls. Run by `make check` and CI; the
release job adds the tag/wheel record. Checks, in order:

 1. Version single source: `__version__` in src/gaceta/__init__.py is the
    only version literal; pyproject declares it dynamic (hatch path), and
    no stray version literal hides in [project].
 2. The wheel-shipped demo generator (src/gaceta/assets/agent_demo/) is
    byte-identical to its source (docs/agent/demo/make_demo_export.py).
 3. packaging/dist is exactly what catalog + templates + version render
    (delegates to `scripts/build-packages.py --check`).
 4. Client rules on the rendered artifacts: Hermes frontmatter fields and
    the 60-char description limit; Codex name/description limits; plugin
    and marketplace manifests resolve.
 5. Every version stamp in every artifact agrees with __version__.
 6. Freshness rule: no pre-built export.json ships anywhere (they go stale
    in 48 h; packages ship the generator instead).
 7. Tag parity: when this tree is a git checkout with tags, the newest
    vX.Y.Z tag must equal __version__; otherwise the check reports SKIPPED
    (the release job enforces it for real, from the public revision).

Usage: python scripts/check-parity.py [--dist DIR] [--root DIR]
Exit: 0 in parity, 1 on any failure.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def iter_files(root: Path):
    """Every file under root, hidden dirs included (os.walk, unlike rglob)."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield Path(dirpath) / name

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "gaceta" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
DEMO_SOURCE = ROOT / "docs" / "agent" / "demo" / "make_demo_export.py"
DEMO_WHEEL_COPY = ROOT / "src" / "gaceta" / "assets" / "agent_demo" / "make_demo_export.py"

failures: list[str] = []
notes: list[str] = []


def ok(message: str) -> None:
    print(f"  ok    {message}")


def bad(message: str) -> None:
    failures.append(message)
    print(f"  FAIL  {message}")


def note(message: str) -> None:
    notes.append(message)
    print(f"  --    {message}")


def read_version(root: Path) -> str:
    tree = ast.parse((root / "src/gaceta/__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return node.value.value
    raise AssertionError("__version__ not found in src/gaceta/__init__.py")


def frontmatter_fields(text: str) -> dict[str, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        raise AssertionError("no YAML frontmatter")
    return dict(re.findall(r"^([a-z_-]+):\s*(.*)$", match.group(1), re.M))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", action="store_true", help="require clean HEAD at the exact version tag")
    parser.add_argument("--dist", default=str(ROOT / "packaging" / "dist"))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    dist = Path(args.dist).resolve()
    version = read_version(root)

    print(f"gaceta parity check — candidate {version}")

    print("[1] version single source")
    version_re = r"\d+\.\d+\.\d+(?:[-.]?(?:a|b|rc)[.-]?\d+)?"
    if not re.fullmatch(version_re, version):
        bad(f"__version__ {version!r} is not a PEP 440 release or pre-release")
    else:
        ok(f"src/gaceta/__init__.py __version__ = {version}")
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    if 'dynamic = ["version"]' not in pyproject:
        bad("pyproject [project] must declare dynamic = [\"version\"]")
    else:
        ok("pyproject declares the version dynamic (single source)")
    if re.search(r"^version\s*=\s*\"", pyproject, re.M):
        bad("pyproject carries a literal version (drift risk); remove it")
    if '[tool.hatch.version]' not in pyproject or (
            'path = "src/gaceta/__init__.py"' not in pyproject):
        bad("pyproject [tool.hatch.version] must point at src/gaceta/__init__.py")
    else:
        ok("hatch version path = src/gaceta/__init__.py")

    print("[2] wheel-shipped demo generator == its source")
    if (DEMO_WHEEL_COPY.exists() and DEMO_SOURCE.exists()
            and DEMO_WHEEL_COPY.read_bytes() == DEMO_SOURCE.read_bytes()):
        ok("src/gaceta/assets/agent_demo/make_demo_export.py is byte-identical")
    else:
        bad("src/gaceta/assets/agent_demo/make_demo_export.py differs from "
            "docs/agent/demo/make_demo_export.py (recopy the byte-identical file)")

    print("[3] packaged artifacts == catalog + templates + version")
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "build-packages.py"),
         "--check", "--out", str(dist)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok(result.stdout.strip().splitlines()[-1])
    else:
        bad("packaging/dist drifted from its sources:")
        for line in result.stderr.strip().splitlines()[:20]:
            print(f"        {line}")

    print("[4] client rules on the rendered artifacts")
    hermes_skill = dist / "hermes-skill"
    codex_skill = dist / "codex-skill"
    plugin = dist / "codex-plugin"
    hermes_files = sorted(hermes_skill.rglob("SKILL.md"))
    codex_files = sorted(codex_skill.rglob("SKILL.md")) + sorted(
        plugin.rglob("SKILL.md"))
    if len(hermes_files) != 1 or len(codex_files) != 2:
        bad(f"expected 1 Hermes and 2 Codex SKILL.md, found "
            f"{len(hermes_files)}/{len(codex_files)}")
    for path in hermes_files:
        fields = frontmatter_fields(path.read_text(encoding="utf-8"))
        missing = [k for k in ("name", "description", "version", "author",
                               "license", "platforms") if k not in fields]
        desc = fields.get("description", "")
        if missing:
            bad(f"{path.name} ({path.parent.name}): missing frontmatter {missing}")
        elif len(desc) > 60:
            bad(f"Hermes description is {len(desc)} chars (limit 60)")
        elif not desc.endswith("."):
            bad("Hermes description must end with a period")
        elif not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fields.get("name", "")):
            bad(f"Hermes skill name invalid: {fields.get('name')!r}")
        else:
            ok(f"{path.parent.parent.name}/{path.parent.name}: Hermes frontmatter "
               f"valid ({len(desc)}-char description)")
    for path in codex_files:
        fields = frontmatter_fields(path.read_text(encoding="utf-8"))
        name, desc = fields.get("name", ""), fields.get("description", "")
        if not name or len(name) > 100:
            bad(f"Codex skill name invalid: {name!r}")
        elif not desc or len(desc) > 500:
            bad(f"Codex description is {len(desc)} chars (limit 500)")
        else:
            ok(f"{path.parent.parent.name}/…/{path.parent.name}: Codex "
               f"frontmatter valid ({len(desc)}-char description)")
    plugin_json = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    if (plugin_json.get("$schema")
            != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"):
        bad("plugin.json must declare the Agent Plugins 1.0.0 schema")
    elif plugin_json.get("version") != version:
        bad(f"plugin.json version {plugin_json.get('version')!r} != {version}")
    else:
        ok(f"plugin.json: Agent Plugins schema, version {version}")
    market = json.loads(
        (dist / "codex-marketplace" / ".agents" / "plugins" / "marketplace.json")
        .read_text(encoding="utf-8"))
    entries = market.get("plugins", [])
    entry = entries[0] if entries else {}
    source_path = entry.get("source", {}).get("path", "")
    resolved = (dist / "codex-marketplace" / source_path.lstrip("./"))
    if (not entries or not (resolved / "plugin.json").exists()):
        bad(f"marketplace.json source.path {source_path!r} does not resolve "
            f"to the plugin")
    else:
        ok(f"marketplace.json: {len(entries)} plugin(s), source.path resolves")

    print("[5] every version stamp agrees with __version__")
    stamps = {
        "MANIFEST.json": (dist / "MANIFEST.json", lambda d: d.get("version")),
        "plugin.json": (dist / "codex-plugin" / "plugin.json",
                       lambda d: d.get("version")),
    }
    manifest = json.loads((dist / "MANIFEST.json").read_text(encoding="utf-8"))
    for label, (path, getter) in stamps.items():
        data = json.loads(path.read_text(encoding="utf-8"))
        if getter(data) == version:
            ok(f"{label} version = {version}")
        else:
            bad(f"{label} version {getter(data)!r} != {version}")
    if manifest.get("catalog_sha256") == hashlib.sha256(
            (root / "packaging" / "catalog.json").read_bytes()).hexdigest():
        ok("MANIFEST catalog digest matches packaging/catalog.json")
    else:
        bad("MANIFEST catalog_sha256 does not match packaging/catalog.json")
    skill_bodies = hermes_files + codex_files
    source_body = re.sub(
        r"\A---\s*\n.*?\n---\s*\n", "",
        (root / "docs" / "agent" / "SKILL.md").read_text(encoding="utf-8"),
        flags=re.DOTALL,
    ).rstrip("\n")
    for path in skill_bodies:
        rendered = path.read_text(encoding="utf-8")
        if source_body and source_body in rendered:
            ok(f"{path.parent.parent.name}/…/{path.parent.name}/SKILL.md body "
               f"== current docs/agent/SKILL.md (sourced at build time)")
        else:
            bad(f"{path} does not contain the current docs/agent/SKILL.md "
                f"body — rebuild (`python scripts/build-packages.py`); the "
                f"canonical body must never be a stale copy")
    landing_hits = []
    # The canonical page states its candidate in each rendering's language.
    candidate_phrase = {"landing.en.md": f"candidate {version}",
                         "landing.es.md": f"candidato {version}"}
    for landing in sorted((dist / "web").glob("landing.*.md")):
        text = landing.read_text(encoding="utf-8")
        phrase = candidate_phrase.get(landing.name, f"candidate {version}")
        if phrase not in text:
            bad(f"{landing.name} does not state {phrase!r}")
        else:
            landing_hits.append(landing.name)
    if len(landing_hits) == 2:
        ok("both landing renderings state the candidate version "
           "(EN 'candidate', ES 'candidato')")
    for listing in sorted((dist / "listings").glob("*.md")):
        text = listing.read_text(encoding="utf-8")
        if f"candidate {version}" not in text:
            bad(f"{listing.name} does not state candidate {version}")
        else:
            ok(f"{listing.name} states candidate {version}")

    print("[6] freshness rule: no pre-built exports ship")
    stale = [p for p in iter_files(dist) if p.name == "export.json"]
    if stale:
        bad(f"pre-built export(s) shipped (48 h staleness): "
            f"{[str(p) for p in stale]}")
    else:
        ok("no export.json in any package (generators ship instead)")

    print("[7] tag parity (release record)")
    git_dir = root / ".git"
    if not args.release:
        note("development check; pass --release at a clean, tagged revision")
    elif git_dir.exists():
        dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True)
        if dirty.strip():
            bad("release tree contains uncommitted changes")
        tags = subprocess.run(
            ["git", "-C", str(root), "tag", "--points-at", "HEAD", "--list", "v*"],
            capture_output=True, text=True,
        ).stdout.split()
        if f"v{version}" not in tags:
            bad(f"release HEAD lacks exact tag v{version}")
        else:
            ok(f"release HEAD tagged v{version}")
    else:
        bad("release validation requires a git checkout")

    print()
    for line in notes:
        print(f"note: {line}")
    if failures:
        print(f"PARITY FAIL — {len(failures)} problem(s)")
        return 1
    print(f"PARITY OK — candidate {version}: one version source, one "
          f"metadata source, artifacts in parity, client rules hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
