import pytest

from gaceta.print import print_html


def test_text_export_has_source_links_but_no_interactive_or_remote_media():
    edition = {
        "reader": {"id": "reader", "name": "Lector"},
        "portada": ["lead"],
        "stories": {"lead": {
            "privacy": "public", "title": "El titular", "summary": "El resumen",
            "media": [{"type": "youtube", "video_id": "dQw4w9WgXcQ",
                       "src": "https://img.youtube.com/vi/dQw4w9WgXcQ/default.jpg"}],
            "sources": [{"title": "La fuente", "url": "https://example.org/story",
                         "thumb": "media/example.jpg"}],
        }},
    }
    doc = print_html(edition, "public")
    assert "El titular" in doc and "El resumen" in doc and "La fuente" in doc
    assert 'href="https://example.org/story"' in doc
    assert "<img " not in doc and "<script" not in doc and "<button" not in doc
    assert 'href="guardadas"' not in doc and "img.youtube.com" not in doc
    illustrated = print_html(edition, "public", with_images=True, media_prefix="./assets/")
    assert 'src="./assets/example.jpg"' in illustrated
    assert "img.youtube.com" not in illustrated


def test_print_enforces_privacy_cap():
    edition = {"stories": {"secret": {"privacy": "private"}}}
    with pytest.raises(ValueError, match="above max level"):
        print_html(edition, "public")
