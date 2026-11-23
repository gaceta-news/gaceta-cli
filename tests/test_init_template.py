"""`gaceta init` writes the example reader.toml: it must format and parse.

Regression: a literal `${uuid}` / inline table in the commented example broke
str.format, so every fresh install failed at `gaceta init` (CI quickstart red
on main, 2026-11-23).
"""
import tomllib

from gaceta.cli import EXAMPLE_READER_TOML


def test_example_reader_toml_formats_and_parses():
    text = EXAMPLE_READER_TOML.format(reader="main", name="Main")
    cfg = tomllib.loads(text)
    assert cfg["id"] == "main"
    assert cfg["name"] == "Main"
    assert "${uuid}" in text  # the documented placeholder survives escaping
