from gaceta.agent import _verified_seen_version, story_rev


def test_repeated_writeup_cannot_identify_the_read_version():
    versions = [{"version": i, "summary": s} for i, s in enumerate(("A", "B", "A"), 1)]
    assert _verified_seen_version(versions, story_rev([], "A")) is None
    assert _verified_seen_version(versions, story_rev([], "B")) == 2
