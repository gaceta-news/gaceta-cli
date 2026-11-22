#!/usr/bin/env python3
"""Render every distributable gaceta surface from ONE metadata source.

Inputs (never hand-stamped):
  - packaging/catalog.json       — name, description, links, tested_with, tags
  - src/gaceta/__init__.py       — the only version literal (__version__)
  - docs/agent/SKILL.md          — the read-only runtime discipline (body)
  - docs/agent/demo/make_demo_export.py — the synthetic demo generator
  - packaging/templates/*        — the shape of each artifact

Outputs (all under packaging/dist/ unless --out is given):
  hermes-skill/research/gaceta/     Hermes skill (drop into ~/.hermes/skills/)
  codex-skill/gaceta/               Codex USER-scope skill ($HOME/.agents/skills/)
  codex-plugin/                     Codex skill-only plugin (Agent Plugins schema)
  codex-marketplace/                local marketplace draft + plugin copy
  web/landing.en.md, web/landing.es.md   the canonical public page, two languages
  listings/hermes-hub.md, listings/codex-plugin.md   catalog listing drafts
  archives/*.zip                   version-pinned companion downloads
  MANIFEST.json                    every file + sha256 (deterministic: no clocks)

`--check` re-renders into a temporary directory and compares against the
existing output, so CI can reject drift between the sources and any
hand-edited artifact. Archives are byte-reproducible (fixed timestamps).

Usage:
  python scripts/build-packages.py            # (re)build packaging/dist
  python scripts/build-packages.py --check    # exit 1 on any drift
  python scripts/build-packages.py --out DIR  # build elsewhere (tests)
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "packaging" / "catalog.json"
TEMPLATES = ROOT / "packaging" / "templates"
VERSION_FILE = ROOT / "src" / "gaceta" / "__init__.py"
SKILL_SOURCE = ROOT / "docs" / "agent" / "SKILL.md"
DEMO_GENERATOR = ROOT / "docs" / "agent" / "demo" / "make_demo_export.py"

HERMES_DESC_LIMIT = 60       # stock Hermes skills convention (hardline)
CODEX_NAME_LIMIT = 100
CODEX_DESC_LIMIT = 500

# PEP 440 releases and pre-releases: 0.3.0, 0.3.0b1, 0.3.0-beta.2, 0.3.0rc1.
VERSION_RE = r"\d+\.\d+\.\d+(?:[-.]?(?:a|b|rc)[.-]?\d+)?"

# Per-output constants for the shared packaged-skill README template.
README_CONTEXTS = {
    "hermes": {
        "CLIENT_LABEL": "Hermes",
        "SKILL_HOME_HINT": "`~/.hermes/skills/research/` (unzip the archive there)",
        "UNINSTALL_EXTRA": "",
    },
    "codex": {
        "CLIENT_LABEL": "Codex (USER-scope skill)",
        "SKILL_HOME_HINT": "`$HOME/.agents/skills/` (USER scope) or "
                           "`$CWD/.agents/skills` (repo scope)",
        "UNINSTALL_EXTRA": " (or `codex plugin remove gaceta` if it came "
                           "with the plugin)",
    },
    "plugin": {
        "CLIENT_LABEL": "Codex (inside the gaceta plugin, `skills/gaceta/`)",
        "SKILL_HOME_HINT": "inside the plugin; enabled and removed with the "
                           "plugin itself",
        "UNINSTALL_EXTRA": " with the plugin (`codex plugin remove gaceta`)",
    },
}


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"build-packages: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_version() -> str:
    """The single version literal: gaceta.__version__ (src tree)."""
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        version = value.value
                        if not re.fullmatch(VERSION_RE, version):
                            fail(f"__version__ {version!r} is not a PEP 440 "
                                 f"release or pre-release (X.Y.Z[.][-]aN|bN|rcN)")
                        return version
    fail(f"no string __version__ assignment in {VERSION_FILE}")


def load_catalog() -> dict:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schema") != "gaceta-packaging-catalog/1":
        fail(f"unknown catalog schema: {catalog.get('schema')!r}")
    return catalog


def agent_body() -> str:
    """The read-only runtime discipline, verbatim: docs/agent/SKILL.md body."""
    text = SKILL_SOURCE.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    if not match:
        fail(f"{SKILL_SOURCE} has no YAML frontmatter to strip")
    return text[match.end():].rstrip("\n")


def mapping(catalog: dict, version: str, extra: dict | None = None) -> dict:
    skill, package, plugin, tested, links = (
        catalog["skill"], catalog["package"], catalog["codex_plugin"],
        catalog["tested_with"], catalog["links"],
    )
    values = {
        "VERSION": version,
        "TAG": package["tag"].replace("{VERSION}", version),
        "REPO": package["repo"],
        "ISSUES": package["issues"],
        "INSTALL_CMD": package["install_command"].replace("{VERSION}", version),
        "UNINSTALL_CMD": package["uninstall_command"],
        "PY_FLOOR": package["python"].lstrip(">= "),
        "SKILL_NAME": skill["name"],
        "SKILL_FORMERLY": skill["formerly"],
        "SKILL_DESCRIPTION": skill["description"],
        "SKILL_DESCRIPTION_LONG": skill["codex_description"],
        "SKILL_AUTHOR": skill["author"],
        "SKILL_LICENSE": skill["license"],
        "HERMES_CATEGORY": skill["hermes_category"],
        "HERMES_TAGS": ", ".join(skill["tags"]),
        "PLATFORMS": "[" + ", ".join(skill["platforms"]) + "]",
        "PLUGIN_NAME": plugin["name"],
        "PLUGIN_DESCRIPTION": plugin["description"],
        "PLUGIN_CATEGORY": plugin["category"],
        "MARKETPLACE_NAME": plugin["marketplace_name"],
        "CANDIDATE": tested["candidate"].replace("{VERSION}", version),
        "CODEX_TESTED": tested["codex_cli"],
        "HERMES_TESTED": tested["hermes_cli"],
        "PY_TESTED": tested["python"],
        "DESK_PATH": links["desk_path"],
        "DESK_STATUS": links["desk_status"],
        "RECIPE_PREVIEW_STATUS": links["recipe_preview_status"],
        "TRIAL_STATUS": links["trial_status"],
        "AGENT_BODY": agent_body(),
    }
    if extra:
        values.update(extra)
    return values


def render(template_name: str, values: dict) -> str:
    text = (TEMPLATES / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = re.search(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        fail(f"unresolved placeholder {leftover.group(0)!r} in {template_name}")
    return text


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_files(root: Path):
    """Every file under root, hidden dirs included (os.walk, unlike rglob('*'))."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield Path(dirpath) / name


def validate_frontmatter(hermes: str, codex: str) -> None:
    """Fail the build on client-rule violations (defense for hand-edited catalog)."""
    hermes_fm = re.match(r"\A---\s*\n(.*?)\n---\s*\n", hermes, re.DOTALL)
    codex_fm = re.match(r"\A---\s*\n(.*?)\n---\s*\n", codex, re.DOTALL)
    if not hermes_fm or not codex_fm:
        fail("SKILL.md templates must open with YAML frontmatter")
    fields = dict(
        re.findall(r"^([a-z_-]+):\s*(.*)$", hermes_fm.group(1), re.M)
    )
    desc = fields.get("description", "")
    if len(desc) > HERMES_DESC_LIMIT:
        fail(f"Hermes description is {len(desc)} chars (limit {HERMES_DESC_LIMIT})")
    if not desc.endswith(".") or "\n" in desc:
        fail("Hermes description must be one sentence ending with a period")
    codex_fields = dict(
        re.findall(r"^([a-z_-]+):\s*(.*)$", codex_fm.group(1), re.M)
    )
    name, cdesc = codex_fields.get("name", ""), codex_fields.get("description", "")
    if not name or len(name) > CODEX_NAME_LIMIT:
        fail(f"Codex skill name invalid: {name!r}")
    if not cdesc or len(cdesc) > CODEX_DESC_LIMIT:
        fail(f"Codex description is {len(cdesc)} chars (limit {CODEX_DESC_LIMIT})")


def write_skill_tree(dest: Path, client: str, values: dict) -> None:
    """SKILL.md + README.md + scripts/ + assets/ for one client."""
    key = {"hermes": "hermes", "codex": "codex", "plugin": "codex"}[client]
    skill = dest
    skill.mkdir(parents=True, exist_ok=True)
    rendered = render(f"{key}-SKILL.md.tmpl", values)
    (skill / "SKILL.md").write_text(rendered + "\n", encoding="utf-8")
    readme_values = dict(values)
    readme_values.update(README_CONTEXTS["plugin" if client == "plugin" else key])
    (skill / "README.md").write_text(
        render("packaged-skill-README.md.tmpl", readme_values) + "\n",
        encoding="utf-8",
    )
    scripts = skill / "scripts"
    scripts.mkdir(exist_ok=True)
    wrapper = render("agent-demo-wrapper.py.tmpl", values)
    (scripts / "make_agent_demo.py").write_text(wrapper, encoding="utf-8")
    shutil.copyfile(DEMO_GENERATOR, scripts / "make_demo_export.py")
    assets = skill / "assets"
    assets.mkdir(exist_ok=True)
    shutil.copyfile(TEMPLATES / "demo-baseline.json", assets / "demo-baseline.json")


def deterministic_zip(source: Path, archive: Path, arc_root: str) -> None:
    """Byte-reproducible zip: fixed timestamps, sorted names, deflated."""
    files = sorted(p for p in source.rglob("*") if p.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            rel = Path(arc_root) / path.relative_to(source)
            info = zipfile.ZipInfo(rel.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def build(dest: Path, quiet: bool = False) -> dict:
    version = read_version()
    catalog = load_catalog()
    values = mapping(catalog, version)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # 1. Hermes skill: research/<name>/ (extract target: ~/.hermes/skills/)
    write_skill_tree(dest / "hermes-skill" / values["HERMES_CATEGORY"] / values["SKILL_NAME"],
                     "hermes", values)
    # 2. Codex USER-scope skill: <name>/ (extract target: $HOME/.agents/skills/)
    write_skill_tree(dest / "codex-skill" / values["SKILL_NAME"], "codex", values)
    # 3. Codex plugin (Agent Plugins schema): skills discovered from root skills/.
    plugin = dest / "codex-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        render("codex-plugin.json.tmpl", values) + "\n", encoding="utf-8"
    )
    write_skill_tree(plugin / "skills" / values["SKILL_NAME"], "plugin", values)
    # 4. Local marketplace draft: codex discovers the manifest at
    #    <root>/.agents/plugins/marketplace.json (stock layout, verified
    #    against codex-cli 0.171.0); source.path resolves from the root.
    market = dest / "codex-marketplace"
    (market / ".agents" / "plugins").mkdir(parents=True)
    (market / ".agents" / "plugins" / "marketplace.json").write_text(
        render("codex-marketplace.json.tmpl", values) + "\n", encoding="utf-8"
    )
    shutil.copytree(plugin, market / values["PLUGIN_NAME"])
    # 5. The canonical public page, two languages.
    web = dest / "web"
    web.mkdir()
    (web / "landing.en.md").write_text(
        render("landing.en.md.tmpl", values) + "\n", encoding="utf-8"
    )
    (web / "landing.es.md").write_text(
        render("landing.es.md.tmpl", values) + "\n", encoding="utf-8"
    )
    # 6. Listing drafts.
    listings = dest / "listings"
    listings.mkdir()
    (listings / "hermes-hub.md").write_text(
        render("listing-hermes-hub.md.tmpl", values) + "\n", encoding="utf-8"
    )
    (listings / "codex-plugin.md").write_text(
        render("listing-codex-plugin.md.tmpl", values) + "\n", encoding="utf-8"
    )

    # Frontmatter rules before anything ships.
    validate_frontmatter(
        (dest / "hermes-skill" / values["HERMES_CATEGORY"] / values["SKILL_NAME"]
         / "SKILL.md").read_text(encoding="utf-8"),
        (dest / "codex-skill" / values["SKILL_NAME"] / "SKILL.md").read_text(encoding="utf-8"),
    )

    # 7. Version-pinned companion archives (byte-reproducible).
    archives = dest / "archives"
    archives.mkdir()
    zips = [
        # arc_root "" for all three: each source dir already carries its
        # extract-target layout (hermes: research/<name>/, codex: <name>/).
        (dest / "hermes-skill", archives / f"gaceta-hermes-skill-{values['TAG']}.zip", ""),
        (dest / "codex-skill", archives / f"gaceta-codex-skill-{values['TAG']}.zip", ""),
        (plugin, archives / f"gaceta-codex-plugin-{values['TAG']}.zip", ""),
    ]
    for source, archive, arc_root in zips:
        deterministic_zip(source, archive, arc_root)

    # 8. MANIFEST: every file + sha256, no clocks (reproducible for CI --check).
    manifest = {
        "schema": "gaceta-packaging-manifest/1",
        "generated_by": "scripts/build-packages.py (edit catalog/templates, "
                        "never the files below)",
        "version": version,
        "catalog_sha256": sha256(CATALOG_PATH),
        "files": {
            p.relative_to(dest).as_posix(): sha256(p)
            for p in sorted(iter_files(dest))
            if p.is_file() and p.parent.name != "archives"
        },
        "archives": {
            p.relative_to(dest).as_posix(): sha256(p)
            for p in sorted(archives.glob("*.zip"))
        },
    }
    (dest / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not quiet:
        print(f"packaged gaceta {version} -> {dest}")
        for key in sorted(manifest["files"]):
            print(f"  {key}")
        for key in sorted(manifest["archives"]):
            print(f"  {key}")
    return manifest


def check(dest: Path) -> int:
    """Re-render into a temp dir; any difference from dest is drift."""
    if not dest.exists():
        print(f"build-packages --check: {dest} does not exist; run "
              f"`python scripts/build-packages.py` first", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="gaceta-pkg-check-") as tmp:
        fresh = Path(tmp) / "dist"
        build(fresh, quiet=True)
        drift = []
        expected = {
            p.relative_to(fresh).as_posix(): sha256(p)
            for p in sorted(iter_files(fresh)) if p.is_file()
        }
        current = {
            p.relative_to(dest).as_posix(): sha256(p)
            for p in sorted(iter_files(dest)) if p.is_file()
        }
        for rel in sorted(set(expected) | set(current)):
            if rel not in current:
                drift.append(f"missing: {rel}")
            elif rel not in expected:
                drift.append(f"extra:   {rel}")
            elif expected[rel] != current[rel]:
                drift.append(f"changed: {rel}")
        if drift:
            print("build-packages --check: packaged artifacts drifted from "
                  "catalog/templates/version sources:", file=sys.stderr)
            for line in drift:
                print(f"  {line}", file=sys.stderr)
            print("rebuild with `python scripts/build-packages.py` or fix "
                  "the hand edit", file=sys.stderr)
            return 1
    print(f"build-packages --check: {dest} is in parity with its sources")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "packaging" / "dist"),
                        help="output directory (default packaging/dist)")
    parser.add_argument("--check", action="store_true",
                        help="compare sources against the existing output; "
                             "exit 1 on drift")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    dest = Path(args.out).resolve()
    if args.check:
        return check(dest)
    build(dest, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
