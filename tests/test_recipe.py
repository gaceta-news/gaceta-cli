"""Safe desk-recipe sharing (recipe.py): export is a private-by-default
allowlist the owner explicitly selects from, previews before consent, and
refuses anything that is not a public web url or that touches a privacy
canary; inspect/import parse with the same fail-closed allowlist parser
(read-only preview), and import creates a new isolated reader — never an
overwrite, independent per recipient. No database is ever opened: the
recipe surface reads and writes files only. Synthetic fixtures, no network."""
from __future__ import annotations

import json

import pytest

import gaceta.config as gconfig
from gaceta import recipe as recipe_mod
from gaceta.cli import main as cli_main
from test_readonly import tree_hashes

READER = "shareable"
TOTAL_FEEDS = 7   # bbc-tech, arxiv-cs, tube, lan-feed, lo-feed, local-feed, doc-feed
TOTAL_LABELS = 3  # tech, secret-den, home

READER_TOML = """# fixture desk: shareable and unshareable parts, offline only
id = "shareable"
name = "Share Desk"
langs = "en"

[privacy]
pinned = ["zorblax"]
household = ["glimmerhouse"]

[[sources.rss.feeds]]
id = "bbc-tech"
name = "BBC Technology"
url = "https://feeds.bbci.co.uk/news/technology/rss.xml"

[[sources.rss.feeds]]
id = "arxiv-cs"
name = "arXiv cs.AI"
url = "https://rss.arxiv.org/rss/cs.AI"

[[sources.rss.feeds]]
id = "tube"
name = "Some Channel"
url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCxyz"
kind = "youtube"

[[sources.rss.feeds]]
id = "lan-feed"
name = "LAN Feed"
url = "http://192.168.1.10:8080/feed.xml"

[[sources.rss.feeds]]
id = "lo-feed"
name = "Loopback Feed"
url = "http://localhost:8090/feed.xml"

[[sources.rss.feeds]]
id = "local-feed"
name = "Local file feed"
file = "sample-feed.xml"

[[sources.rss.feeds]]
id = "doc-feed"
name = "Zorblax docs"
url = "https://docs.example.com/feed.xml"

[[sources.nostr_wot]]
pubkey = "aa11bb22cc33dd44ee55ff66"
relays = ["wss://relay.example.com"]

[sources.sqlite]
path = "/abs/path/items.db"

[[sections]]
id = "tech"
title = "Technology"
kinds = ["rss"]
keywords = ["chip", "solar"]

[[sections]]
id = "secret-den"
title = "Zorblax den"
keywords = ["zorblax"]

[[sections]]
id = "home"
title = "Glimmerhouse board"
keywords = ["glimmerhouse"]

[embed]
provider = "ollama"
model = "bge-m3"
base_url = "http://localhost:11434"

[llm.editor]
provider = "openai"
model = "gpt-4o-mini"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[cluster]
threshold = 0.70
window_days = 21
"""


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A reader config full of things that must and must not be shareable."""
    data = tmp_path / "data"
    reader_dir = data / "readers" / READER
    reader_dir.mkdir(parents=True)
    (reader_dir / "reader.toml").write_text(READER_TOML, encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(data))
    return data


def _export(capsys, *extra):
    argv = ["recipe", "export", "--reader", READER, *extra]
    rc = cli_main(argv)
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


def _cli(capsys, *argv):
    rc = cli_main(list(argv))
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


# ------------------------------------------------------------- export: allowlist

def test_export_is_private_by_default_and_requires_a_selection(desk, capsys):
    rc, out, err = _export(capsys)
    assert rc == 2 and "private by default" in err and "--feed" in err
    assert out == ""  # fail closed: no data on stdout
    assert not (desk / "recipes").exists()  # and nothing written anywhere


def test_export_carries_exactly_the_explicit_selection(desk, capsys, tmp_path):
    out_path = tmp_path / "r.json"
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--feed", "tube",
                           "--label", "tech", "--title", "Tech desk",
                           "--out", str(out_path), "--consent")
    assert rc == 0, err
    assert "recipe export reader=shareable" in out
    recipe = json.loads(out_path.read_text(encoding="utf-8"))
    # the allowlist, both ways: exact keys at every level
    assert set(recipe) == {"schema_version", "kind", "recipe", "privacy"}
    assert recipe["kind"] == "gaceta.desk-recipe"
    assert recipe["privacy"] == recipe_mod.PRIVACY_BLOCK
    assert set(recipe["recipe"]) == {"title", "feeds", "labels"}
    assert [f["id"] for f in recipe["recipe"]["feeds"]] == ["bbc-tech", "tube"]
    assert set(recipe["recipe"]["feeds"][0]) == {"id", "name", "url"}
    assert set(recipe["recipe"]["feeds"][1]) == {"id", "name", "url", "kind"}
    assert recipe["recipe"]["feeds"][1]["kind"] == "youtube"
    assert [l["id"] for l in recipe["recipe"]["labels"]] == ["tech"]
    assert set(recipe["recipe"]["labels"][0]) == {"id", "title", "kinds", "keywords"}
    assert recipe["recipe"]["labels"][0]["keywords"] == ["chip", "solar"]
    # nothing unselected, secret, local or executable rides along
    text = out_path.read_text(encoding="utf-8")
    for absent in ("arxiv-cs", "lan-feed", "lo-feed", "local-feed", "doc-feed",
                   "sample-feed.xml", "zorblax", "glimmerhouse", "secret-den",
                   "home", "Glimmerhouse", "OPENAI_API_KEY", "gpt-4o-mini",
                   "11434", "nostr", "pubkey", "relay", "/abs/path", "items.db",
                   '"provider"', '"api_key_env"', '"base_url"', '"path"',
                   '"file"', "sqlite"):
        assert absent not in text, absent
    # the recipe surface never opens a database
    assert not (desk / "gaceta.db").exists()


def test_export_previews_before_consent_and_writes_nothing(desk, capsys, tmp_path):
    out_path = tmp_path / "r.json"
    before = tree_hashes(desk)
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--label", "tech",
                           "--out", str(out_path))
    assert rc == 0, err
    assert "preview only" in out and "nothing written" in out
    assert "feeds (1)" in out and "bbc-tech" in out
    assert "https://feeds.bbci.co.uk" in out
    assert "labels (1)" in out and "tech" in out
    assert "privacy: public-only" in out
    # what stayed private is counted, never named
    assert f"not shared: {TOTAL_FEEDS - 1} other feed(s)" in out
    assert f"{TOTAL_LABELS - 1} other label(s)" in out
    assert "arxiv-cs" not in out and "secret-den" not in out
    assert not out_path.exists()
    assert not (desk / "recipes").exists()
    assert tree_hashes(desk) == before


@pytest.mark.parametrize("feed,needle", [
    ("local-feed", "local file"),
    ("lan-feed", "non-public"),
    ("lo-feed", "local host"),
    ("nope-feed", "no [[sources.rss.feeds]] id"),
])
def test_export_refuses_unshareable_or_unknown_feeds(desk, capsys, feed, needle):
    rc, out, err = _export(capsys, "--feed", feed, "--consent")
    assert rc == 2 and needle in err
    assert out == ""
    assert not (desk / "recipes").exists()


def test_export_refuses_unknown_label(desk, capsys):
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--label", "nope", "--consent")
    assert rc == 2 and "no [[sections]] id" in err


def test_export_needs_at_least_one_feed(desk, capsys):
    rc, out, err = _export(capsys, "--label", "tech", "--consent")
    assert rc == 2 and "at least one --feed" in err


# ------------------------------------------------------------ export: canaries

@pytest.mark.parametrize("args,scope", [
    (["--feed", "bbc-tech", "--label", "secret-den"], "pinned"),
    (["--feed", "bbc-tech", "--label", "home"], "household"),
    (["--feed", "doc-feed"], "pinned"),
])
def test_export_refuses_any_selection_touching_a_canary(desk, capsys, args, scope):
    rc, out, err = _export(capsys, *args, "--consent")
    assert rc == 2 and "canary" in err and scope in err
    # the canary term itself is never printed (privacy.py discipline)
    assert "zorblax" not in err and "glimmerhouse" not in err
    assert "Zorblax" not in err and "Glimmerhouse" not in err
    assert out == ""
    assert not (desk / "recipes").exists()


# --------------------------------------------------------- export: renderer link

def test_export_link_is_explicit_and_public_only(desk, capsys, tmp_path):
    out_path = tmp_path / "r.json"
    rc, out, err = _export(capsys, "--feed", "bbc-tech",
                           "--link", "https://paper.example/ana/",
                           "--out", str(out_path), "--consent")
    assert rc == 0, err
    recipe = json.loads(out_path.read_text(encoding="utf-8"))
    assert recipe["link"] == {"url": "https://paper.example/ana/",
                              "privacy": "public", "about": recipe_mod.LINK_ABOUT}
    assert "public-only renderer link" in out
    # and it is optional: without --link the recipe has no link key at all
    out_path2 = tmp_path / "r2.json"
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--out", str(out_path2),
                           "--consent")
    assert rc == 0 and "link" not in json.loads(out_path2.read_text())


@pytest.mark.parametrize("link,needle", [
    ("http://paper.example/", "must be https"),
    ("https://localhost/paper", "local host"),
    ("https://10.0.0.9/paper", "non-public"),
    ("https://user:pw@paper.example/", "credentials"),
    ("https://paper/", "not a public web hostname"),
])
def test_export_refuses_links_that_are_not_public_web(desk, capsys, link, needle):
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--link", link, "--consent")
    assert rc == 2 and needle in err
    assert out == ""


# ------------------------------------------------------------- export: plumbing

def test_export_out_must_be_json_and_round_trips_the_safe_parser(desk, capsys, tmp_path):
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--out", str(tmp_path / "x.txt"))
    assert rc == 2 and ".json" in err
    assert not (tmp_path / "x.txt").exists()
    # a written export re-loads through the very parser import uses
    out_path = tmp_path / "r.json"
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--feed", "tube",
                           "--label", "tech", "--langs", "es,en",
                           "--title", "Ana's tech desk", "--out", str(out_path),
                           "--consent")
    assert rc == 0, err
    recipe = recipe_mod.load_recipe(out_path)
    assert recipe["recipe"]["langs"] == "es,en"
    assert len(recipe["recipe"]["feeds"]) == 2


def test_export_default_path_and_idempotent_refresh(desk, capsys):
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--consent")
    assert rc == 0, err
    default = desk / "recipes" / READER / "desk-recipe.json"
    assert default.is_file()
    first = default.read_text(encoding="utf-8")
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--consent")
    assert rc == 0, err
    assert recipe_mod.load_recipe(default)  # refreshed artifact, still valid
    assert default.read_text(encoding="utf-8") == first


def test_export_refuses_a_missing_reader(desk, capsys):
    rc, out, err = _cli(capsys, "recipe", "export", "--reader", "ghost",
                        "--feed", "x", "--consent")
    assert rc == 2 and out == ""


def test_export_langs_must_be_language_codes(desk, capsys):
    rc, out, err = _export(capsys, "--feed", "bbc-tech", "--langs", "not,lang!",
                           "--consent")
    assert rc == 2 and "language codes" in err


# -------------------------------------------------------------------- inspect

def _valid_recipe() -> dict:
    return {
        "schema_version": 1,
        "kind": "gaceta.desk-recipe",
        "recipe": {
            "title": "A desk",
            "langs": "es,en",
            "feeds": [{"id": "bbc-tech", "name": "BBC Technology",
                       "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"}],
            "labels": [{"id": "tech", "title": "Technology", "kinds": ["rss"],
                        "source_prefixes": [], "keywords": ["chip"]}],
        },
        # a copy, so tamper tests can't touch the module constant
        "privacy": dict(recipe_mod.PRIVACY_BLOCK),
    }


def _recipe_file(tmp_path, mutate=None, name="recipe.json"):
    obj = _valid_recipe()
    if mutate:
        mutate(obj)
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def test_inspect_is_read_only_and_needs_no_data_dir(desk, tmp_path, monkeypatch, capsys):
    p = _recipe_file(tmp_path)
    before = tree_hashes(desk)
    # a data dir that does not even exist: inspect reads ONE file, no db,
    # no config, no writes
    monkeypatch.setenv("GACETA_DATA", str(tmp_path / "nowhere"))
    rc, out, err = _cli(capsys, "recipe", "inspect", str(p))
    assert rc == 0, err
    assert 'desk recipe "A desk"' in out
    assert "feeds (1)" in out and "bbc-tech" in out
    assert "labels (1)" in out
    assert "public-only" in out
    assert tree_hashes(desk) == before


def test_inspect_shows_the_link_as_public_only(tmp_path, capsys):
    def with_link(o):
        o["link"] = {"url": "https://paper.example/ana/", "privacy": "public",
                     "about": recipe_mod.LINK_ABOUT}

    p = _recipe_file(tmp_path, mutate=with_link)
    rc, out, err = _cli(capsys, "recipe", "inspect", str(p))
    assert rc == 0 and "https://paper.example/ana/" in out
    assert "public-only renderer link" in out


# ---------------------------------------------- inspect: the parser fails closed

def _add_embed(o):
    o["embed"] = {"provider": "openai", "api_key_env": "OPENAI_API_KEY",
                  "base_url": "https://api.openai.com/v1"}


def _add_secret(o):
    o["recipe"]["feeds"][0]["api_key_env"] = "OPENAI_API_KEY"


def _local_file(o):
    o["recipe"]["feeds"][0]["file"] = "sample-feed.xml"


def _file_url(o):
    o["recipe"]["feeds"][0]["url"] = "file:///etc/passwd"


def _host_path(o):
    o["recipe"]["feeds"][0]["url"] = "/etc/passwd"


def _localhost(o):
    o["recipe"]["feeds"][0]["url"] = "http://localhost/feed.xml"


def _private_ip(o):
    o["recipe"]["feeds"][0]["url"] = "https://192.168.0.4/feed"


def _loopback(o):
    o["recipe"]["feeds"][0]["url"] = "http://127.0.0.1/feed"


def _v6_loopback(o):
    o["recipe"]["feeds"][0]["url"] = "http://[::1]/feed"


def _creds(o):
    o["recipe"]["feeds"][0]["url"] = "https://user:pass@feeds.example.com/rss"


def _single_label_host(o):
    o["recipe"]["feeds"][0]["url"] = "http://intranet/feed"


def _unknown_kind(o):
    o["recipe"]["feeds"][0]["kind"] = "sqlite"


def _dup_feed_ids(o):
    o["recipe"]["feeds"].append(dict(o["recipe"]["feeds"][0]))


def _no_feeds(o):
    o["recipe"]["feeds"] = []


def _label_unknown_key(o):
    o["recipe"]["labels"][0]["script"] = "rm -rf ~"


def _label_bad_id(o):
    o["recipe"]["labels"][0]["id"] = "Tecnología"


def _label_dup_ids(o):
    o["recipe"]["labels"].append(dict(o["recipe"]["labels"][0]))


def _control_char(o):
    o["recipe"]["feeds"][0]["name"] = "Bad\nName"


def _kind_mismatch(o):
    o["kind"] = "gaceta.agent-export"


def _schema_bump(o):
    o["schema_version"] = 2


def _schema_bool(o):
    o["schema_version"] = True


def _privacy_edit(o):
    o["privacy"]["max_level"] = "household"


def _privacy_gone(o):
    o.pop("privacy")


def _link_private(o):
    o["link"] = {"url": "https://paper.example/", "privacy": "household"}


def _link_http(o):
    o["link"] = {"url": "http://paper.example/", "privacy": "public"}


def _link_local(o):
    o["link"] = {"url": "https://localhost/x", "privacy": "public"}


def _executable_llm(o):
    o["recipe"]["llm"] = {"provider": "openai",
                          "base_url": "http://localhost:11434/v1",
                          "api_key_env": "X"}


def _plugin_table(o):
    o["recipe"]["plugins"] = ["my_package.sources:MyAdapter"]


def _arbitrary_settings(o):
    o["recipe"]["settings"] = {"cluster": {"threshold": 0.1}}


def _nostr_key(o):
    o["recipe"]["sources"] = {"nostr_wot": {"pubkey": "aa11"}}


def _huge_title(o):
    o["recipe"]["title"] = "x" * 81


def _langs_junk(o):
    o["recipe"]["langs"] = "not,a,lang!!"


def _too_many_feeds(o):
    o["recipe"]["feeds"] = [{"id": f"f{i}", "url": "https://f.example/x"}
                            for i in range(recipe_mod.MAX_FEEDS + 1)]


@pytest.mark.parametrize("mutate,needle", [
    (_add_embed, "never emits"),
    (_add_secret, "never emits"),
    (_local_file, "never emits"),
    (_file_url, "web urls only"),
    (_host_path, "web urls only"),
    (_localhost, "local host"),
    (_private_ip, "non-public"),
    (_loopback, "non-public"),
    (_v6_loopback, "non-public"),
    (_creds, "credentials"),
    (_single_label_host, "not a public web hostname"),
    (_unknown_kind, "not one of"),
    (_dup_feed_ids, "twice"),
    (_no_feeds, "at least one feed"),
    (_label_unknown_key, "never emits"),
    (_label_bad_id, "bad id"),
    (_label_dup_ids, "twice"),
    (_control_char, "control characters"),
    (_kind_mismatch, "not a gaceta desk recipe"),
    (_schema_bump, "schema version"),
    (_schema_bool, "schema version"),
    (_privacy_edit, "privacy contract"),
    (_privacy_gone, "privacy contract"),
    (_link_private, "explicitly public-only"),
    (_link_http, "must be https"),
    (_link_local, "local host"),
    (_executable_llm, "never emits"),
    (_plugin_table, "never emits"),
    (_arbitrary_settings, "never emits"),
    (_nostr_key, "never emits"),
    (_huge_title, "longer than"),
    (_langs_junk, "language codes"),
    (_too_many_feeds, "more than"),
])
def test_inspect_refuses_anything_this_surface_never_writes(tmp_path, capsys,
                                                             mutate, needle):
    p = _recipe_file(tmp_path, mutate=mutate)
    rc, out, err = _cli(capsys, "recipe", "inspect", str(p))
    assert rc == 2, (rc, out, err)
    assert needle in err
    assert out == ""  # never a partial answer on stdout


def test_inspect_refuses_oversized_broken_and_missing_files(tmp_path, capsys):
    big = tmp_path / "big.json"
    big.write_text("[" + "0," * (recipe_mod.MAX_RECIPE_BYTES // 2 + 8) + "0]",
                   encoding="utf-8")
    rc, out, err = _cli(capsys, "recipe", "inspect", str(big))
    assert rc == 2 and "larger than" in err
    junk = tmp_path / "junk.json"
    junk.write_text("]not json[", encoding="utf-8")
    rc, out, err = _cli(capsys, "recipe", "inspect", str(junk))
    assert rc == 2 and "not valid JSON" in err
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    rc, out, err = _cli(capsys, "recipe", "inspect", str(arr))
    assert rc == 2 and "JSON object" in err
    rc, out, err = _cli(capsys, "recipe", "inspect", str(tmp_path / "nope.json"))
    assert rc == 2 and "no desk recipe" in err
    assert out == ""


# -------------------------------------------------------------------- import

def test_import_previews_before_consent_and_writes_nothing(desk, tmp_path, capsys):
    p = _recipe_file(tmp_path)
    before = tree_hashes(desk)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "friend")
    assert rc == 0, err
    assert "preview only" in out and "nothing written" in out
    assert "import --as friend" in out
    assert "new isolated reader" in out
    assert not (desk / "readers" / "friend").exists()
    assert not (desk / "gaceta.db").exists()
    assert tree_hashes(desk) == before


def test_import_creates_a_new_isolated_reader(desk, tmp_path, capsys):
    p = _recipe_file(tmp_path)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "friend",
                        "--consent")
    assert rc == 0, err
    assert "recipe import --as friend" in out
    assert "next: gaceta ingest rss --reader friend" in out
    target = desk / "readers" / "friend" / "reader.toml"
    assert target.is_file()
    cfg = gconfig.load_reader("friend", desk)
    # only the allowlisted values flowed in
    assert cfg["id"] == "friend" and cfg["name"] == "Friend"
    assert cfg["langs"] == "es,en"
    feeds = cfg["sources"]["rss"]["feeds"]
    assert [f["id"] for f in feeds] == ["bbc-tech"]
    assert feeds[0]["url"] == "https://feeds.bbci.co.uk/news/technology/rss.xml"
    assert "file" not in feeds[0] and "path" not in feeds[0]
    assert [s["id"] for s in cfg["sections"]] == ["tech"]
    assert cfg["sections"][0]["keywords"] == ["chip"]
    # the recipient starts from safe defaults, never from recipe settings
    assert cfg["embed"]["provider"] == "fake"
    assert cfg["llm"]["editor"]["provider"] == "fake"
    assert cfg["privacy"]["pinned"] == [] and cfg["privacy"]["household"] == []
    text = target.read_text(encoding="utf-8")
    for absent in ("zorblax", "glimmerhouse", "OPENAI", "11434", "nostr",
                   "pubkey", "/abs/path", "items.db", str(p), "shareable"):
        assert absent not in text, absent
    # a file write only: no database was ever created or opened
    assert not (desk / "gaceta.db").exists()


@pytest.mark.parametrize("pre", ["toml", "junk"])
def test_import_never_overwrites_an_existing_reader(desk, tmp_path, capsys, pre):
    existing = desk / "readers" / "friend"
    existing.mkdir(parents=True)
    if pre == "toml":
        (existing / "reader.toml").write_text('id = "friend"\nname = "Mine"\n',
                                              encoding="utf-8")
    else:
        (existing / "leftover.txt").write_text("untouched", encoding="utf-8")
    before = tree_hashes(desk)
    p = _recipe_file(tmp_path)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "friend",
                        "--consent")
    assert rc == 2 and "never overwrites" in err
    assert out == ""
    assert tree_hashes(desk) == before


def test_import_refuses_a_bad_new_reader_id(desk, tmp_path, capsys):
    p = _recipe_file(tmp_path)
    for bad in ("Bad Id", "../escape", ""):
        rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", bad,
                            "--consent")
        assert rc == 2 and "bad new reader id" in err, bad


def test_recipients_are_independent(desk, tmp_path, capsys):
    p = _recipe_file(tmp_path)
    texts = {}
    for rid in ("a-desk", "b-desk"):
        rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", rid,
                            "--consent")
        assert rc == 0, err
        texts[rid] = (desk / "readers" / rid / "reader.toml").read_text(
            encoding="utf-8")
    # two independent readers from one recipe: each parses, each is its own
    # config, and neither references the other, the recipe file or the sender
    assert gconfig.load_reader("a-desk", desk)["id"] == "a-desk"
    assert gconfig.load_reader("b-desk", desk)["id"] == "b-desk"
    for rid, text in texts.items():
        assert str(p) not in text and "shareable" not in text
        for other in texts:
            if other != rid:
                assert other not in text
    # a third import onto an existing id still refuses (no overwrite)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "a-desk",
                        "--consent")
    assert rc == 2 and "never overwrites" in err


def test_import_escapes_hostile_but_allowed_strings_into_valid_toml(
        desk, tmp_path, capsys):
    def evil_name(o):
        o["recipe"]["feeds"][0]["name"] = 'Weird "quote" #comment \\back'

    def unicode_name(o):
        o["recipe"]["labels"][0]["title"] = "Tecnología y café"

    p = _recipe_file(tmp_path, mutate=evil_name)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "esc",
                        "--consent")
    assert rc == 0, err
    cfg = gconfig.load_reader("esc", desk)
    assert cfg["sources"]["rss"]["feeds"][0]["name"] == \
        'Weird "quote" #comment \\back'
    p2 = _recipe_file(tmp_path, mutate=unicode_name, name="recipe2.json")
    rc, out, err = _cli(capsys, "recipe", "import", str(p2), "--as", "uni",
                        "--consent")
    assert rc == 0, err
    assert gconfig.load_reader("uni", desk)["sections"][0]["title"] == \
        "Tecnología y café"


def test_import_keeps_the_link_as_an_explicit_public_only_comment(
        desk, tmp_path, capsys):
    def with_link(o):
        o["link"] = {"url": "https://paper.example/ana/", "privacy": "public",
                     "about": recipe_mod.LINK_ABOUT}

    p = _recipe_file(tmp_path, mutate=with_link)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "linked",
                        "--consent")
    assert rc == 0, err
    text = (desk / "readers" / "linked" / "reader.toml").read_text(encoding="utf-8")
    assert "https://paper.example/ana/" in text
    assert "public-only rendered paper" in text
    # the link is a comment (a place to visit), never a setting gaceta reads
    cfg = gconfig.load_reader("linked", desk)
    assert "link" not in cfg and "recipe" not in cfg


def test_import_refuses_a_recipe_smuggling_settings(desk, tmp_path, capsys):
    p = _recipe_file(tmp_path, mutate=_add_embed)
    rc, out, err = _cli(capsys, "recipe", "import", str(p), "--as", "smuggled",
                        "--consent")
    assert rc == 2 and "never emits" in err
    assert out == ""
    assert not (desk / "readers" / "smuggled").exists()


# ---------------------------------------------------------------- round trip

def test_export_inspect_import_round_trip_offline(desk, tmp_path, capsys):
    out_path = tmp_path / "desk.json"
    rc, out, err = _cli(capsys, "recipe", "export", "--reader", READER,
                        "--feed", "bbc-tech", "--feed", "tube", "--label", "tech",
                        "--title", "Ana's tech desk", "--langs", "en,es",
                        "--link", "https://paper.example/ana/",
                        "--out", str(out_path), "--consent")
    assert rc == 0, err
    rc, out, err = _cli(capsys, "recipe", "inspect", str(out_path))
    assert rc == 0 and "feeds (2)" in out
    assert "labels (1)" in out and "public-only renderer link" in out
    rc, out, err = _cli(capsys, "recipe", "import", str(out_path),
                        "--as", "roundtrip", "--consent")
    assert rc == 0, err
    cfg = gconfig.load_reader("roundtrip", desk)
    assert [f["id"] for f in cfg["sources"]["rss"]["feeds"]] == ["bbc-tech", "tube"]
    assert cfg["sources"]["rss"]["feeds"][1].get("kind") == "youtube"
    assert cfg["sections"][0]["id"] == "tech"
    assert cfg["sections"][0]["kinds"] == ["rss"]
    assert cfg["sections"][0]["keywords"] == ["chip", "solar"]
    assert cfg["langs"] == "en,es"
    text = (desk / "readers" / "roundtrip" / "reader.toml").read_text(encoding="utf-8")
    assert "https://paper.example/ana/" in text
    assert not (desk / "gaceta.db").exists()
