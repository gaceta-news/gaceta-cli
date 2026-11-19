"""Privacy contract tests (WORKFLOW § 3: canaries are never deleted, never skipped)."""
from gaceta.privacy import LEVELS, allowed, label_item, rank, story_privacy

CFG = {
    "id": "t",
    "privacy": {"pinned": ["zorblax", "qq-secret-farm"], "household": ["glimmerhouse"]},
}


def test_ordering():
    assert LEVELS == ("public", "household", "private")
    assert rank("public") < rank("household") < rank("private")


def test_allowed_matrix():
    assert allowed("public", "public")
    assert allowed("public", "household")
    assert allowed("household", "household")
    assert allowed("household", "private")
    assert allowed("private", "private")
    assert not allowed("household", "public")
    assert not allowed("private", "public")
    assert not allowed("private", "household")


def test_unknown_label_fails_closed():
    assert rank("who-knows") == rank("private")
    assert rank(None) == rank("private")
    assert rank("") == rank("private")
    assert not allowed("who-knows", "household")
    assert allowed("who-knows", "private")  # unknown ranks as private, allowed at private


def test_pinned_beats_household_beats_public():
    label, reason = label_item(CFG, True, "Zorblax quarterly report", None, None, None)
    assert (label, reason) == ("private", "pinned:0")
    label, reason = label_item(CFG, True, "normal title", "visit glimmerhouse farm", None, None)
    assert (label, reason) == ("household", "household:0")
    label, reason = label_item(CFG, True, "normal title", None, None, None)
    assert (label, reason) == ("public", "public-source")
    label, reason = label_item(CFG, False, "normal title", None, None, None)
    assert (label, reason) == ("private", "default")


def test_pinned_beats_household_when_both_match():
    label, reason = label_item(CFG, True, "glimmerhouse", "zorblax", None, None)
    assert (label, reason) == ("private", "pinned:0")


def test_match_fields_and_case():
    for title, body, url, author in [
        ("ZORBLAX", None, None, None),
        (None, "the qq-secret-farm leaks", None, None),
        (None, None, "https://ex.com/zorblax", None),
        (None, None, None, "Zorblax Channel"),
    ]:
        assert label_item(CFG, True, title, body, url, author)[0] == "private"


def test_reason_holds_index_never_term():
    for term_i, term in enumerate(CFG["privacy"]["pinned"]):
        label, reason = label_item(CFG, True, f"x {term} y", None, None, None)
        assert label == "private"
        assert term.lower() not in reason.lower()
        assert reason == f"pinned:{term_i}"
    label, reason = label_item(CFG, True, "glimmerhouse", None, None, None)
    assert reason == "household:0"
    assert "glimmerhouse" not in reason.lower()


def test_story_privacy():
    assert story_privacy(["public", "public"]) == "public"
    assert story_privacy(["public", "household"]) == "household"
    assert story_privacy(["public", "private"]) == "private"
    assert story_privacy([]) == "private"
    assert story_privacy(["public"], has_why=True) == "private"


def test_url_canary_pins_private():
    cfg = {"privacy": {"pinned": ["zorblax"], "household": []}}
    assert label_item(cfg, True, "t", "b", "https://example.com/ZORBLAX/x", "a")[0] == "private"


def test_public_view_has_no_reader_signals():
    import inspect
    from gaceta import views
    src = inspect.getsource(views.public_items)
    for col in ("a.note", "a.verdict", "a.library_path"):
        assert col not in src
