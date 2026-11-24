import email.utils
import socket
from io import BytesIO
from types import SimpleNamespace

import pytest

from gaceta.media import MediaError
from gaceta.sources.rss import (
    DEFAULT_MAX_ENTRIES, RssAdapter, _CountingReader, _GuardedRedirect,
    _feed_key, _fetch_entries, _stream_entries, parse_feed, source_identity,
)
from gaceta.transcripts import Transcripts, Transcript, clean_vtt


def feed_of(xml: bytes):
    """Test double for the streaming fetch: pull entries out of fixed bytes."""
    return lambda url, max_entries: _stream_entries(BytesIO(xml), max_entries)

RSS = b'''<rss xmlns:content="urn:content"><channel><item>
  <title>A &amp; B</title><link>https://example.com/story</link><guid>abc</guid>
  <pubDate>Tue, 17 Nov 2026 12:00:00 +0000</pubDate>
  <description>Teaser</description>
  <content:encoded><![CDATA[<p>First.</p><p>More &amp; better.</p>]]></content:encoded>
</item></channel></rss>'''
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="urn:media">
  <entry><id>yt:video:Abcde12345_</id><title>Clickbait</title>
  <link href="https://www.youtube.com/watch?v=Abcde12345_"/>
  <published>2026-11-17T12:00:00Z</published>
  <media:group><media:description>NOT EVIDENCE</media:description></media:group></entry>
</feed>'''
ATOM_XHTML = b'''<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>tag:x,2026:1</id><title>Prose inside XHTML</title>
  <link href="https://example.com/x"/>
  <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Real prose here.</p><p>Second line.</p></div></content>
</entry></feed>'''
NOW = 1794924000


def test_rss_prefers_prose_and_ids_are_stable():
    article, = parse_feed(RSS)
    assert article['body'] == 'First.\n\nMore & better.'
    assert article['title'] == 'A & B'
    adapter = RssAdapter({'feeds': [{'id': 'one', 'url': 'https://example.com/feed'}]},
                         fetch_entries=feed_of(RSS), now=lambda: NOW)
    item, = adapter.items()
    assert item.verdict == 'noted' and item.id == next(adapter.items()).id
    assert list(adapter.items(since=article['published'] + 1)) == []


def test_atom_xhtml_content_keeps_prose():
    article, = parse_feed(ATOM_XHTML)
    assert article['body'] == 'Real prose here.\n\nSecond line.'


# --------------------------------------------------------------- huge feeds

RDF = b'''<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">
<channel><title>Wire</title></channel>
<item><title>One</title><link>https://example.com/1</link><description>first</description></item>
<item><title>Two</title><link>https://example.com/2</link><description>second</description></item>
</rdf:RDF>'''


def test_rss10_items_at_root_level_still_parse():
    # streaming must match the old whole-document walk: entries count at any depth
    assert [e["guid"] for e in parse_feed(RDF)] == ["https://example.com/1", "https://example.com/2"]


def _oversized_feed(items: int, body_kib: int = 9) -> bytes:
    """A synthetic oversized podcast feed: `items` entries of ~body_kib KiB each."""
    body = "Long show notes, paragraph by paragraph. " * (body_kib * 1024 // 42)
    parts = [b'<rss xmlns:content="urn:content"><channel><title>Backlog</title>']
    for i in range(items):
        parts.append(("<item><title>Episode %04d</title>"
                      "<link>https://example.com/ep/%04d</link><guid>ep-%04d</guid>"
                      "<pubDate>%s</pubDate><description>%s</description></item>"
                      % (i, i, i, email.utils.formatdate(NOW - i * 3600, usegmt=True), body)
                      ).encode())
    parts.append(b"</channel></rss>")
    return b"".join(parts)


class _TallyIO(BytesIO):
    """BytesIO that counts what the pull parser actually consumed."""

    def __init__(self, *data):
        super().__init__(*data)
        self.consumed = 0

    def read(self, size: int | None = -1) -> bytes:
        data = super().read(size)
        self.consumed += len(data)
        return data


def test_huge_feed_is_cut_off_not_rejected():
    xml = _oversized_feed(600)
    assert len(xml) > 5_000_000            # the size the old whole-feed cap rejected
    fp = _TallyIO(xml)
    entries = _stream_entries(fp, 200)
    assert len(entries) == 200
    assert entries[0]["guid"] == "ep-0000" and entries[-1]["guid"] == "ep-0199"
    assert fp.consumed < len(xml)          # the read stops with the feed unfinished


def test_adapter_reads_only_the_entry_cap_and_surfaces_the_newest():
    xml = _oversized_feed(600)
    caps = []

    def fetch_entries(url, max_entries):
        caps.append(max_entries)
        return _stream_entries(BytesIO(xml), max_entries)

    adapter = RssAdapter({"feeds": [{"id": "bigcast", "url": "https://example.com/feed"}],
                           "max_entries": 50, "max_items": 10},
                          fetch_entries=fetch_entries, now=lambda: NOW)
    items = list(adapter.items())
    assert caps == [50] and adapter.errors == []
    assert len(items) == 10                          # max_items still bounds the run
    assert items[0].title == "Episode 0000"          # newest first, feed order kept
    assert items[-1].title == "Episode 0009"
    default = RssAdapter({"feeds": [{"id": "b", "url": "https://example.com/feed"}]},
                          fetch_entries=fetch_entries, now=lambda: NOW)
    assert len(list(default.items())) == 20
    assert caps[-1] == DEFAULT_MAX_ENTRIES == 200   # absent key: sensible default


def test_safety_ceiling_still_fails_a_pathological_feed():
    xml = _oversized_feed(3, body_kib=64)  # ~190 KB in three entries: no entry cap hit
    assert len(_stream_entries(BytesIO(xml), 200)) == 3
    with pytest.raises(MediaError, match="safety ceiling"):
        _stream_entries(_CountingReader(BytesIO(xml), 100_000), 200)


def test_ceiling_failure_is_reported_not_fatal():
    huge = _oversized_feed(600)

    def fetch_entries(url, max_entries):
        if url.endswith("/broken"):
            raise MediaError("feed passed the 100000 byte safety ceiling")
        return _stream_entries(BytesIO(huge), max_entries)

    adapter = RssAdapter({"feeds": [{"id": "backlog", "url": "https://example.com/feed"},
                                     {"id": "broken", "url": "https://example.com/broken"}]},
                          fetch_entries=fetch_entries, now=lambda: NOW)
    items = list(adapter.items())
    assert adapter.errors == [("broken", "MediaError")]   # reported, never fatal
    assert len(items) == 20 and items[0].title == "Episode 0000"


def test_source_identity_is_the_feed_url_not_the_slug():
    # items is a shared table: the same slug must never alias two different feeds,
    # and two readers pasting the same feed must land on the same rows.
    a, = RssAdapter({'feeds': [{'id': 'one', 'url': 'https://a.example/feed'}]},
                    fetch_entries=feed_of(RSS), now=lambda: NOW).items()
    b, = RssAdapter({'feeds': [{'id': 'one', 'url': 'https://b.example/feed'}]},
                    fetch_entries=feed_of(RSS), now=lambda: NOW).items()
    c, = RssAdapter({'feeds': [{'id': 'totally-different', 'url': 'https://A.EXAMPLE/feed'}]},
                    fetch_entries=feed_of(RSS), now=lambda: NOW).items()
    assert a.id != b.id and a.source.id != b.source.id
    assert (a.id, a.source.id) == (c.id, c.source.id)
    assert _feed_key('https://A.example:443/feed#frag') == 'https://a.example/feed'


def test_youtube_uses_transcript_or_nothing():
    for url in ('https://www.youtube.com/feeds/videos.xml', 'https://example.com/feed'):
        cfg = {'feeds': [{'id': 'clips', 'url': url}]}
        adapter = RssAdapter(cfg, fetch_entries=feed_of(ATOM), now=lambda: NOW)
        assert list(adapter.items()) == []
        assert adapter.videos_without_transcript == 1
        seen = []

        def transcript(vid):
            seen.append(vid)
            return Transcript('The witness explicitly contradicts the headline.', True)

        item, = RssAdapter(cfg, fetch_entries=feed_of(ATOM), now=lambda: NOW,
                           transcript=transcript).items()
        assert seen == ['Abcde12345_']
        assert item.source.kind == 'youtube' and item.body == transcript(seen[0]).text
        assert item.source.content_public is True and ':u' not in item.source.id
        assert 'NOT EVIDENCE' not in item.body


def test_unproven_transcript_gets_a_private_source_variant():
    def transcript(vid):
        return Transcript('Cookies fetched me a video that may not be public.', False)

    item, = RssAdapter({'feeds': [{'id': 'clips', 'url': 'https://www.youtube.com/feeds/videos.xml'}]},
                       fetch_entries=feed_of(ATOM), now=lambda: NOW, transcript=transcript).items()
    assert item.source.content_public is False and item.source.id.endswith(':u')
    public_again, = RssAdapter({'feeds': [{'id': 'clips', 'url': 'https://www.youtube.com/feeds/videos.xml'}]},
                               fetch_entries=feed_of(ATOM), now=lambda: NOW,
                               transcript=lambda v: Transcript('Refetched anonymously later.', True)).items()
    # Same item row (identity independent of provenance), now on the public source.
    assert public_again.id == item.id and public_again.source.content_public is True


def _resolver(ip):
    def resolve(host, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0))]
    return resolve


def test_fetch_refuses_non_http_schemes(monkeypatch):
    monkeypatch.setattr('gaceta.media.socket.getaddrinfo', _resolver('93.184.216.34'))
    with pytest.raises(MediaError, match='scheme'):
        _fetch_entries('file:///etc/passwd')


def test_fetch_refuses_private_hosts(monkeypatch):
    monkeypatch.setattr('gaceta.media.socket.getaddrinfo', _resolver('10.1.2.3'))
    with pytest.raises(MediaError, match='non-global'):
        _fetch_entries('http://internal.example/feed')


def test_redirect_guard_refuses_file_and_internal_targets(monkeypatch):
    monkeypatch.setattr('gaceta.media.socket.getaddrinfo', _resolver('127.0.0.1'))
    handler = _GuardedRedirect()
    with pytest.raises(MediaError, match='scheme'):
        handler.redirect_request(None, None, 302, 'Found', {}, 'file:///etc/passwd')  # type: ignore[arg-type]
    with pytest.raises(MediaError, match='non-global'):
        handler.redirect_request(None, None, 302, 'Found', {}, 'http://127.0.0.1:8080/feed')  # type: ignore[arg-type]


def test_transcript_cache_and_429_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr('gaceta.transcripts.shutil.which', lambda _: '/usr/bin/yt-dlp')
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stderr='HTTP Error 429')

    other = tmp_path / 'existing'
    other.mkdir()
    (other / 'Abcde12345_.txt').write_text('Usable captions here')
    cfg = {'extra_dirs': [str(other)], 'min_words': 3}
    transcripts = Transcripts(cfg, tmp_path, runner=run, now=lambda: NOW)
    # extra-dir hit: usable text, but NOT proven public (fail closed)
    t = transcripts.get_with_origin('Abcde12345_')
    assert t == Transcript('Usable captions here', False) and not calls
    assert transcripts.get('Abcde12345_') == 'Usable captions here'
    assert transcripts.get_with_origin('vwxyz12345_') is None
    assert Transcripts(cfg, tmp_path, runner=run, now=lambda: NOW).get_with_origin('12345vwxyz_') is None
    assert len(calls) == 1
    assert clean_vtt('WEBVTT\n1\n00:00:00 --> 00:00:03\nHere &amp; now\nHere &amp; now') == 'Here & now'


def test_own_cache_marker_decides_and_extra_dirs_stay_private(tmp_path):
    (tmp_path / 'transcripts').mkdir()
    (tmp_path / 'transcripts' / 'Abcde12345_.txt').write_text('Unmarked text')
    # unmarked own-cache file: fail closed, even with no cookies configured
    assert Transcripts({'min_words': 2}, tmp_path, now=lambda: NOW).get_with_origin('Abcde12345_').public is False
    (tmp_path / 'transcripts' / 'Abcde12345_.prov').write_text('anon')
    assert Transcripts({'min_words': 2}, tmp_path, now=lambda: NOW).get_with_origin('Abcde12345_').public is True
    (tmp_path / 'transcripts' / 'Abcde12345_.prov').write_text('cookies')
    assert Transcripts({'min_words': 2}, tmp_path, now=lambda: NOW).get_with_origin('Abcde12345_').public is False
    # an extra dir is someone else's cache: its sidecar is not ours to trust
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'Bbcde12345_.txt').write_text('Foreign cache text')
    (other / 'Bbcde12345_.prov').write_text('anon')
    assert Transcripts({'min_words': 2, 'extra_dirs': [str(other)]}, tmp_path,
                       now=lambda: NOW).get_with_origin('Bbcde12345_').public is False


def test_feed_key_brackets_ipv6_before_port():
    assert (_feed_key('https://[2606:4700::1111]:8080/feed')
            != _feed_key('https://[2606:4700::1111:8080]/feed'))


def test_unproven_fetch_never_overwrites_public_shared_item(tmp_path):
    import gaceta.db as gdb
    import gaceta.ingest as ingest_mod

    conn = gdb.connect(tmp_path / 'g.db')
    gdb.init(conn)
    feed = {'feeds': [{'id': 'clips', 'url': 'https://www.youtube.com/feeds/videos.xml'}]}
    pub = RssAdapter(feed, fetch_entries=feed_of(ATOM), now=lambda: NOW,
                     transcript=lambda v: Transcript('Anonymous public transcript words.', True))
    unpr = RssAdapter(feed, fetch_entries=feed_of(ATOM), now=lambda: NOW,
                      transcript=lambda v: Transcript('Cookie-fetched secret transcript.', False))
    ingest_mod.ingest(conn, pub, {'id': 'a', 'name': 'A', 'langs': 'es'})
    iid, = [r[0] for r in conn.execute('SELECT id FROM items')]
    ingest_mod.ingest(conn, unpr, {'id': 'b', 'name': 'B', 'langs': 'es'})
    body, src = conn.execute('SELECT body, source_id FROM items WHERE id=?', (iid,)).fetchone()
    assert body == 'Anonymous public transcript words.' and not src.endswith(':u')
    labels = dict(conn.execute(
        "SELECT reader_id, item_privacy FROM annotations WHERE item_id=?", (iid,)))
    assert labels == {'a': 'public', 'b': 'private'}
    # the reverse order upgrades: public evidence may replace unproven text
    conn.execute('DELETE FROM annotations')
    conn.execute('DELETE FROM items')
    conn.execute('DELETE FROM sources')
    ingest_mod.ingest(conn, unpr, {'id': 'b', 'name': 'B', 'langs': 'es'})
    ingest_mod.ingest(conn, pub, {'id': 'a', 'name': 'A', 'langs': 'es'})
    body2, = conn.execute('SELECT body FROM items WHERE id=?', (iid,)).fetchone()
    assert body2 == 'Anonymous public transcript words.'
    labels2 = dict(conn.execute(
        "SELECT reader_id, item_privacy FROM annotations WHERE item_id=?", (iid,)))
    assert labels2 == {'a': 'public', 'b': 'private'}


def test_fresh_fetch_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr('gaceta.transcripts.shutil.which', lambda _: '/usr/bin/yt-dlp')

    def run(cmd, **kwargs):
        out = [a for a in cmd if a.endswith('s.%(ext)s')][0].rsplit('/', 1)[0]
        import pathlib
        pathlib.Path(out, 's.es.vtt').write_text('WEBVTT\n\n00:00:00 --> 00:00:02\nFresh captions line')
        return SimpleNamespace(stderr='')

    tr = Transcripts({'min_words': 3}, tmp_path, runner=run, now=lambda: NOW)
    assert tr.get_with_origin('Abcde12345_') == Transcript('Fresh captions line', True)
    assert (tmp_path / 'transcripts' / 'Abcde12345_.prov').read_text() == 'anon'
    cookied = Transcripts({'min_words': 3, 'cookies_from_browser': 'firefox:/x'}, tmp_path,
                          runner=run, now=lambda: NOW)
    assert cookied.get_with_origin('Bbcde12345_') == Transcript('Fresh captions line', False)
    assert (tmp_path / 'transcripts' / 'Bbcde12345_.prov').read_text() == 'cookies'
