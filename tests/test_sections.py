from gaceta.edition.build import _match_section, _rss_aliases
from gaceta.sources.rss import source_identity


def test_section_keywords_match_word_starts_only():
    sec = {"id": "ia", "keywords": ["ia", "vulnerab"]}

    def st(title):
        return {"id": "s", "title": title, "summary": ""}

    assert not _match_section(sec, st("Historia de Colombia"), {})
    assert _match_section(sec, st("La IA en Colombia"), {})
    assert _match_section(sec, st("New vulnerability in X"), {})


def test_rss_alias_is_reader_local_and_matches_video_variants():
    feed = {"id": "city-local", "url": "https://example.com/city.xml"}
    other = {**feed, "url": "https://example.com/other.xml"}
    cfg = {"sources": {"rss": {"feeds": [feed]}}}
    other_cfg = {"sources": {"rss": {"feeds": [other]}}}
    sid, _ = source_identity(feed)
    section = {"source_prefixes": ["rss:city"]}
    story = {"id": "s", "title": "Council votes", "summary": ""}
    for source in (sid, sid + ":u", sid + ":videos:u"):
        items = {"s": [{"source_id": source}]}
        assert _match_section(section, story, items, _rss_aliases(cfg))
        assert not _match_section(section, story, items, _rss_aliases(other_cfg))
    # Existing source-id prefixes still work without an alias.
    assert _match_section(section, story, {"s": [{"source_id": "rss:city-legacy"}]})
