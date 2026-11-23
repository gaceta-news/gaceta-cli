"""Story write-ups: versioning, cache, budget, invalid JSON, model boundary
(reader profile + annotation note only for allow_private), extra_body passthrough."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from gaceta.editor import cluster as run_cluster
from gaceta.editor import writeup
from gaceta.editor.cluster import embed_items
from gaceta.providers.embed import FakeEmbedder
from gaceta.providers.llm import FakeChat, OpenAICompatChat, from_config

from test_cluster import T0, DAY, add_item

NOW = T0 + 10 * DAY
PROFILE_CANARY = "zorblax-profile-canary"
NOTE_CANARY = "zorblax-note-canary"

GOOD = json.dumps({
    "title": "Alpha story", "summary": "Uno. Dos. Tres.",
    "why_it_matters": None, "confidence": 0.8,
})


class FakeWriteup(FakeChat):
    """FakeChat that returns a canned raw reply (write-up JSON or garbage)."""

    def __init__(self, reply, **kw):
        super().__init__(**kw)
        self.reply = reply

    def complete(self, messages, **kw):
        prompt = "\n".join(m["content"] for m in messages)
        self.prompts.append(prompt)
        n = len(prompt.split())
        return self.reply, {"prompt_tokens": n, "completion_tokens": 5,
                            "served_model": "fake:served-1"}


def build_story(env, *items):
    """items: (iid, title, body, verdict, privacy, published)."""
    for it in items:
        add_item(env, *it)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    return run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)


def test_writes_version_and_updates_story(env):
    build_story(
        env,
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma", "noted", "public", T0 + DAY),
    )
    sid = env.conn.execute(
        "SELECT id FROM stories WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchone()[0]
    chat = FakeWriteup(GOOD)
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r["written"] == 1 and r["failed"] == 0 and r["llm_calls"] == 1
    v = env.conn.execute("SELECT * FROM story_versions").fetchone()
    assert v["version"] == 1 and v["members"] == 2 and v["model"] == "fake:served-1"
    s = env.conn.execute("SELECT * FROM stories WHERE id = ?", (sid,)).fetchone()
    assert s["title"] == "Alpha story" and s["summary"] == "Uno. Dos. Tres."
    assert s["confidence"] == 0.8 and s["why_it_matters"] is None
    assert s["privacy"] == "public"  # all items public, no why_it_matters


def test_unchanged_story_is_not_a_candidate_and_cache_is_free(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    chat = FakeWriteup(GOOD)
    r1 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r1["written"] == 1 and r1["llm_calls"] == 1
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r2["window_stories"] == 0 and r2["llm_calls"] == 0  # members unchanged → skip
    # force it back into the queue: same prompt → served from the cache
    env.conn.execute("UPDATE story_versions SET members = NULL")
    env.conn.commit()
    r3 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r3["llm_calls"] == 0 and r3["llm_cache_hits"] == 1 and r3["written"] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()[0] == 2


def test_new_member_triggers_a_new_call(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    chat = FakeWriteup(GOOD)
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    add_item(env, "a2", "A two", "alpha beta gamma", "noted", "public", T0 + DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg)
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r["llm_calls"] == 1 and r["written"] == 1  # new prompt → real call
    assert env.conn.execute("SELECT MAX(version) FROM story_versions").fetchone()[0] == 2


def test_budget_stops_and_reports_pending(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    chat = FakeWriteup(GOOD)
    r = writeup.write_stories(
        env.conn, env.cfg["id"], env.cfg, chat=chat, max_llm_calls=0, now=NOW
    )
    assert r["written"] == 0 and r["pending"] == 1 and r["budget_exhausted"]
    assert env.conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()[0] == 0


def test_invalid_json_keeps_story_and_counts_failure(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    chat = FakeWriteup("sounds like one story to me")  # no JSON anywhere
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r["failed"] == 1 and r["written"] == 0 and r["llm_calls"] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()[0] == 0
    s = env.conn.execute("SELECT title, summary FROM stories").fetchone()
    assert s["title"] == "A one" and s["summary"] is None
    # the raw reply is cached: a rerun fails again for 0 calls
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r2["llm_calls"] == 0 and r2["failed"] == 1


def test_invalid_json_reply_is_cached_not_repaid(env):
    """Step 0: an invalid-JSON write-up reply IS cached in llm_decisions — the
    story is still a candidate next run, but the cached reply is re-read, so
    the model is never re-asked (and never re-paid) for it."""
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    chat = FakeWriteup("sounds like one story to me")
    r1 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r1["failed"] == 1 and r1["llm_calls"] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 1
    assert env.conn.execute("SELECT output FROM llm_decisions").fetchone()[0] == \
        "sounds like one story to me"

    def never(messages, **kw):
        raise AssertionError("invalid-JSON replies are cached; no second call expected")

    chat2 = FakeWriteup("unused")  # same default name → the same cache key
    chat2.complete = never
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat2, now=NOW)
    assert r2["llm_calls"] == 0 and r2["llm_cache_hits"] == 1 and r2["failed"] == 1


def test_priority_told_first(env):
    build_story(
        env,
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),          # 1 item, newer
        ("b1", "B one", "delta epsilon zeta", "told", "public", T0),         # 1 item, told
        ("c1", "C one", "eta theta iota", "noted", "public", T0),
        ("c2", "C two", "eta theta iota", "noted", "public", T0 + DAY),
        ("c3", "C three", "eta theta iota", "noted", "public", T0 + 2 * DAY),
    )
    replies = []
    seen = []

    class Recorder(FakeWriteup):
        def complete(self, messages, **kw):
            seen.append(messages[1]["content"])
            return super().complete(messages, **kw)

    chat = Recorder(GOOD)
    r = writeup.write_stories(
        env.conn, env.cfg["id"], env.cfg, chat=chat, max_llm_calls=2, now=NOW
    )
    assert r["written"] == 2 and r["budget_exhausted"]
    assert "B one" in seen[0]                       # told story first
    assert "C three" in seen[1]                     # then the bigger story


def _with_profile_and_note(env):
    (env.data_dir / "readers" / env.cfg["id"] / "profile.md").write_text(
        f"Alice lives near {PROFILE_CANARY}.\n"
    )
    env.cfg["reader"] = {"profile_path": "profile.md"}
    env.conn.execute(
        "UPDATE annotations SET note = ? WHERE item_id = 'a1'", (NOTE_CANARY,)
    )
    env.conn.commit()


def test_model_boundary_private_text_never_leaves(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    _with_profile_and_note(env)
    chat = FakeWriteup(GOOD, allow_private=False)
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert chat.prompts, "expected at least one call"
    for p in chat.prompts:
        assert PROFILE_CANARY not in p and NOTE_CANARY not in p


def test_public_only_model_does_not_read_private_items(env):
    build_story(env, ("a1", "A one", PROFILE_CANARY, "noted", "private", T0))
    chat = FakeWriteup(GOOD, allow_private=False)
    result = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert result["written"] == result["llm_calls"] == 0
    assert not chat.prompts


def test_model_boundary_private_text_included_when_allowed(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    _with_profile_and_note(env)
    chat = FakeWriteup(GOOD, allow_private=True)
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert PROFILE_CANARY in chat.prompts[0] and NOTE_CANARY in chat.prompts[0]
    assert "Spanish" in chat.prompts[0]  # fixture reader langs = es,en


# ------------------------------------------------------------- extra_body (llm.py)

def test_extra_body_merged_into_request(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "{}"}}],
                "usage": {}, "model": "glm-5.3-flash",
            }).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GACETA_TEST_KEY", "k")
    chat = OpenAICompatChat(
        "https://api.example/v4", "GACETA_TEST_KEY", "glm-5.3-flash",
        extra_body={"thinking": {"type": "disabled"}},
    )
    chat.complete([{"role": "user", "content": "hi"}])
    assert captured["body"]["model"] == "glm-5.3-flash"
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_from_config_passes_extra_body():
    chat = from_config({
        "provider": "openai", "base_url": "https://x", "model": "m",
        "extra_body": {"thinking": {"type": "disabled"}},
    })
    assert chat.extra_body == {"thinking": {"type": "disabled"}}
    with pytest.raises(ValueError):
        from_config({"provider": "openai", "base_url": "https://x", "model": "m",
                     "extra_body": "nope"})


def test_stories_on_the_current_paper_are_written_first(env, tmp_path):
    """A small story on the reader's latest edition beats a bigger off-paper one."""
    build_story(
        env,
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma", "noted", "public", T0 + DAY),
        ("b1", "B one", "omega psi chi", "noted", "public", T0 + DAY),
    )
    rows = env.conn.execute("SELECT si.item_id, si.story_id FROM story_items si").fetchall()
    sid = {r[0]: r[1] for r in rows}
    small = sid["b1"]
    ed = tmp_path / "ed.json"
    ed.write_text(json.dumps({"stories": {small: {}}}))
    env.conn.execute("INSERT INTO editions (reader_id, date, max_privacy, path, built_at) "
                     "VALUES (?, '2026-01-01', 'private', ?, 1)", (env.cfg["id"], str(ed)))
    env.conn.commit()
    cands = writeup._candidates(env.conn, env.cfg["id"], 0, NOW + 1)
    assert cands[0]["id"] == small
    # no edition / unreadable snapshot → old order (bigger story first)
    ed.unlink()
    assert writeup._candidates(env.conn, env.cfg["id"], 0, NOW + 1)[0]["id"] == sid["a1"]


def test_config_fake_chat_writes_valid_cards():
    # offline quickstart: provider "fake" from config answers card prompts with valid JSON
    import json as _json
    from gaceta.providers import llm as _llm
    chat = _llm.from_config({"provider": "fake"})
    out, _ = chat.complete([{"role": "user", "content":
        'Reply with JSON {"title", "summary", "why_it_matters", "confidence"}\nTITLE: Hello\nBODY: World'}])
    card = _json.loads(out)
    assert card["title"] == "Hello" and card["summary"] == "World" and 0 <= card["confidence"] <= 1


ES_SUMMARY = ("La empresa anunció que los paneles solares de los balcones resistieron el invierno "
              "sin fallas graves y que publicará una guía para repararlos.")
EN_SUMMARY = ("The company said the balcony solar panels survived the winter without serious "
              "failures and that it will publish a guide to repair them.")


class WrongLanguageFirst(FakeChat):
    """Answers in English first; in Spanish when asked to fix the language."""

    def complete(self, messages, **kw):
        fix = "Rewrite the same card" in messages[-1]["content"]
        self.prompts.append(messages[-1]["content"])
        return json.dumps({"title": "Lámparas" if fix else "Lamps",
                           "summary": ES_SUMMARY if fix else EN_SUMMARY,
                           "why_it_matters": None, "confidence": 0.7}), {}


def _one_story(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))


def test_wrong_language_card_is_rewritten_once(env):
    _one_story(env)
    cfg = {**env.cfg, "langs": "es"}
    chat = WrongLanguageFirst()
    r = writeup.write_stories(env.conn, cfg["id"], cfg, chat=chat, now=NOW)
    assert r["written"] == 1 and r["relanguaged"] == 1 and r["llm_calls"] == 2
    s = env.conn.execute("SELECT title, summary FROM stories").fetchone()
    assert s["title"] == "Lámparas" and s["summary"] == ES_SUMMARY


def test_right_language_costs_no_extra_call(env):
    _one_story(env)
    cfg = {**env.cfg, "langs": "en"}
    r = writeup.write_stories(env.conn, cfg["id"], cfg, chat=WrongLanguageFirst(), now=NOW)
    assert r["written"] == 1 and r["relanguaged"] == 0 and r["llm_calls"] == 1


def test_language_fix_respects_budget(env):
    _one_story(env)
    cfg = {**env.cfg, "langs": "es"}
    r = writeup.write_stories(env.conn, cfg["id"], cfg, chat=WrongLanguageFirst(),
                              now=NOW, max_llm_calls=1)
    # no budget left for the fix: keep the (valid) card rather than nothing
    assert r["llm_calls"] == 1 and r["written"] == 1 and r["relanguaged"] == 0


# ------------------------------------------------------------ ROADMAP 14: cache policy

class Fake429(FakeWriteup):
    """Every chain member spent: the provider raises a quota refusal."""

    def complete(self, messages, **kw):
        from gaceta.providers.llm import LLMRefused
        self.prompts.append("x")
        raise LLMRefused(429, '{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted"}}')


def test_quota_error_is_not_cached_and_stops_the_run(env):
    build_story(env,
                ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
                ("b1", "B one", "omega psi chi", "noted", "public", T0))
    chat = Fake429("unused")
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r["throttled"] and r["budget_exhausted"] and r["written"] == 0
    assert len(chat.prompts) == 1  # stops at the first 429, never hammers
    assert env.conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 0
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=FakeWriteup(GOOD), now=NOW)
    assert r2["written"] == 2  # next run: both stories get their card


def test_legacy_cached_quota_error_is_asked_again(env):
    """Rows older versions stored for a 429 ('' + usage.refused=429) are not decisions."""
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=FakeWriteup(GOOD), now=NOW)
    env.conn.execute("UPDATE llm_decisions SET output = '', usage = ?",
                     (json.dumps({"refused": 429, "detail": "limit"}),))
    env.conn.execute("DELETE FROM story_versions")
    env.conn.commit()
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=FakeWriteup(GOOD), now=NOW)
    assert r["llm_calls"] == 1 and r["written"] == 1


def test_invalid_reply_is_retried_after_backoff_then_given_up(env):
    from gaceta.editor import llmcache
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    bad = FakeWriteup("sounds like one story to me")
    t = NOW
    for attempt in range(1, llmcache.MAX_ATTEMPTS + 1):
        r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=bad, now=t)
        assert r["llm_calls"] == 1 and r["failed"] == 1, attempt
        usage = json.loads(env.conn.execute("SELECT usage FROM llm_decisions").fetchone()[0])
        assert usage["attempts"] == attempt
        # inside the backoff window: served from the cache, no call
        r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=bad, now=t + 60)
        assert r["llm_calls"] == 0
        t += llmcache.RETRY_AFTER * 2 ** (attempt - 1) + 1
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=bad, now=t + DAY)
    assert r["llm_calls"] == 0 and r["failed"] == 1  # MAX_ATTEMPTS reached: stays as is


def test_retry_that_succeeds_writes_the_card(env):
    from gaceta.editor import llmcache
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=FakeWriteup("nope"), now=NOW)
    r = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=FakeWriteup(GOOD),
                              now=NOW + llmcache.RETRY_AFTER + 1)
    assert r["written"] == 1
    usage = json.loads(env.conn.execute("SELECT usage FROM llm_decisions").fetchone()[0])
    assert "attempts" not in usage


def test_parse_writeup_repairs_inner_quotes_and_cutoffs():
    from gaceta.editor import prompts
    inner = ('{"title": "Solo saludos", "summary": "Notas con saludos ("Gm", "thank you mate") '
             'y nada más.", "why_it_matters": null, "confidence": 0.15}')
    card = prompts.parse_writeup(inner)
    assert card and card["summary"].startswith('Notas con saludos ("Gm", "thank you mate")')
    cut_after_field = ('{"title": "T", "summary": "S uno.", "confidence": 0.4, '
                       '"why_it_matters": "Esto se cort')
    card = prompts.parse_writeup(cut_after_field)
    assert card and card["why_it_matters"] is None and card["confidence"] == 0.4
    cut_before_conf = '{"title": "T", "summary": "S uno.", "why_it_matters": "W", "confidence": 0'
    assert prompts.parse_writeup(cut_before_conf)["confidence"] == 0.0
    assert prompts.parse_writeup('{"title": "T", "summary": "S se cor') is None  # no confidence
    assert prompts.parse_writeup("no json at all") is None
