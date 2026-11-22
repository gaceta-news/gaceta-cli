"""P4 packaging contracts, tested offline: one version source, rendered
artifacts in parity with their sources, client frontmatter rules, and the
packaged demo end to end against the read-only agent commands.

No wheel build, no network, no credentials, no model calls. The packages
are rendered into a temp dir by importing scripts/build-packages.py (the
same code `make packages` and CI run).
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import zipfile

import pytest

from gaceta import __version__

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_build_packages():
    spec = importlib.util.spec_from_file_location(
        "build_packages", REPO / "scripts" / "build-packages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BP = _load_build_packages()


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("p4-dist") / "dist"
    manifest = BP.build(out, quiet=True)
    return out, manifest


def _iter_files(root):
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _frontmatter(text):
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert match, "no YAML frontmatter"
    return dict(re.findall(r"^([a-z_-]+):\s*(.*)$", match.group(1), re.M))


def _agent_body():
    text = (REPO / "docs" / "agent" / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    assert match
    return text[match.end():].rstrip("\n")


# --- one version source ---------------------------------------------------

def test_version_is_a_pep440_beta():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-.]?(?:a|b|rc)[.-]?\d+)?", __version__)


def test_pyproject_reads_the_version_dynamically():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert not re.search(r'^version\s*=\s*"', pyproject, re.M), \
        "a literal version in [project] would be a second source"
    assert 'path = "src/gaceta/__init__.py"' in pyproject


def test_cli_version_flag_reports_the_single_source(capsys):
    from gaceta import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_wheel_demo_generator_is_byte_identical_to_its_source():
    source = (REPO / "docs" / "agent" / "demo" / "make_demo_export.py").read_bytes()
    wheel_copy = (REPO / "src" / "gaceta" / "assets" / "agent_demo"
                  / "make_demo_export.py").read_bytes()
    assert wheel_copy == source


# --- rendered artifacts ---------------------------------------------------

def test_every_artifact_is_fully_rendered(dist):
    out, _manifest = dist
    leftovers = [
        p.relative_to(out)
        for p in _iter_files(out)
        if p.suffix in {".md", ".json", ".py"}
        and re.search(r"\{\{[A-Z_]+\}\}", p.read_text(encoding="utf-8"))
    ]
    assert not leftovers, f"unresolved placeholders: {leftovers}"


def test_manifest_records_every_file_and_the_version(dist):
    out, manifest = dist
    assert manifest["version"] == __version__
    recorded = set(manifest["files"])
    on_disk = {
        p.relative_to(out).as_posix() for p in _iter_files(out)
        if p.parent.name != "archives" and p != out / "MANIFEST.json"
    }
    assert recorded == on_disk


def test_version_stamps_agree_everywhere(dist):
    out, _manifest = dist
    plugin = json.loads((out / "codex-plugin" / "plugin.json")
                        .read_text(encoding="utf-8"))
    assert plugin["version"] == __version__
    assert plugin["$schema"] == \
        "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    landings = {
        p.name: p.read_text(encoding="utf-8")
        for p in (out / "web").glob("landing.*.md")
    }
    assert f"candidate {__version__}" in landings["landing.en.md"]
    assert f"candidato {__version__}" in landings["landing.es.md"]
    for listing in (out / "listings").glob("*.md"):
        assert f"candidate {__version__}" in listing.read_text(encoding="utf-8")


def test_client_frontmatter_rules(dist):
    out, _manifest = dist
    hermes = _frontmatter(
        (out / "hermes-skill" / "research" / "gaceta" / "SKILL.md")
        .read_text(encoding="utf-8"))
    assert hermes["name"] == "gaceta"
    assert len(hermes["description"]) <= 60
    assert hermes["description"].endswith(".")
    for key in ("version", "author", "license", "platforms"):
        assert key in hermes
    assert hermes["version"] == __version__
    for codex in [out / "codex-skill" / "gaceta" / "SKILL.md",
                  out / "codex-plugin" / "skills" / "gaceta" / "SKILL.md"]:
        fields = _frontmatter(codex.read_text(encoding="utf-8"))
        assert fields["name"] and len(fields["name"]) <= 100
        assert fields["description"] and len(fields["description"]) <= 500


def test_marketplace_entry_resolves_to_the_plugin(dist):
    out, _manifest = dist
    market = json.loads(
        (out / "codex-marketplace" / ".agents" / "plugins"
         / "marketplace.json").read_text(encoding="utf-8"))
    entry = market["plugins"][0]
    assert entry["source"]["source"] == "local"
    plugin_dir = out / "codex-marketplace" / entry["source"]["path"].lstrip("./")
    assert (plugin_dir / "plugin.json").is_file()
    assert (plugin_dir / "skills" / "gaceta" / "SKILL.md").is_file()


def test_skill_body_is_sourced_from_the_canonical_file(dist):
    """The packaged SKILL.md must carry the CURRENT docs/agent/SKILL.md body
    (injected at build time), never a hand-pasted copy that can go stale."""
    out, _manifest = dist
    body = _agent_body()
    assert body
    for skill in [out / "hermes-skill" / "research" / "gaceta" / "SKILL.md",
                  out / "codex-skill" / "gaceta" / "SKILL.md",
                  out / "codex-plugin" / "skills" / "gaceta" / "SKILL.md"]:
        assert body in skill.read_text(encoding="utf-8"), \
            f"{skill.name} lost sync with docs/agent/SKILL.md"


def test_no_prebuilt_export_ships(dist):
    out, _manifest = dist
    stale = [p for p in _iter_files(out) if p.name == "export.json"]
    assert not stale, "pre-built exports go stale in 48 h; ship generators"


def test_archives_are_deterministic_and_layouted(dist):
    out, _manifest = dist
    archives = out / "archives"
    tag = f"v{__version__}"
    hermes = zipfile.ZipFile(archives / f"gaceta-hermes-skill-{tag}.zip")
    assert "research/gaceta/SKILL.md" in hermes.namelist()
    plugin = zipfile.ZipFile(archives / f"gaceta-codex-plugin-{tag}.zip")
    assert "plugin.json" in plugin.namelist()
    assert "skills/gaceta/SKILL.md" in plugin.namelist()
    # rebuilt byte-for-byte (fixed timestamps, sorted entries)
    second = out.parent / "dist-again"
    BP.build(second, quiet=True)
    for name in sorted(p.name for p in archives.glob("*.zip")):
        assert (archives / name).read_bytes() == (second / "archives" / name).read_bytes(), \
            f"{name} is not reproducible"


# --- the packaged demo, end to end ----------------------------------------

def test_packaged_demo_runs_the_readonly_runtime(dist, tmp_path, capsys):
    out, _manifest = dist
    skill = out / "hermes-skill" / "research" / "gaceta"
    work = tmp_path / "work"
    subprocess.run([sys.executable, str(skill / "scripts" / "make_agent_demo.py"),
                    str(work)], check=True, capture_output=True)
    export = work / "export.json"
    assert export.is_file()

    from gaceta import cli

    def run(*argv):
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        assert code == 0, captured.err
        return json.loads(captured.out)

    edition = run("agent", "edition", "--reader", "demo",
                  "--path", str(export))
    assert edition["edition"]["stories_total"] == 3
    assert {"demo-aurora", "demo-trail", "demo-quiet"} <= {
        s["id"] for s in edition["stories"]}

    story = run("agent", "story", "--reader", "demo", "--id", "demo-aurora",
                "--path", str(export))
    assert len(story["story"]["sources"]) == 2
    assert len(story["story"]["corrections"]) == 1

    since = run("agent", "since", "--reader", "demo",
                "--baseline", str(skill / "assets" / "demo-baseline.json"),
                "--path", str(export))
    updates = {u["story_id"]: u for u in since["updates"]}
    trail = updates["demo-trail"]
    assert trail["baseline"]["status"] == "known"
    assert trail["change"]["status"] == "material_change"
    assert len(trail["change"]["proposals"][0]["support"]) == 2
    assert updates["demo-aurora"]["change"]["status"] == "unknown"
    assert since["skipped_editions_note"]

    # first launch, no baseline: still exit 0 with explicit unknowns
    first = run("agent", "since", "--reader", "demo", "--path", str(export))
    assert first["baseline"]["versions"] == {}


# --- the parity gate itself ------------------------------------------------

def test_parity_gate_passes_on_a_fresh_build(tmp_path):
    """scripts/check-parity.py green against a freshly rendered dist."""
    built = tmp_path / "dist"
    BP.build(built, quiet=True)
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check-parity.py"),
         "--dist", str(built)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PARITY OK" in result.stdout
