"""Shared fixture: synthetic collector db + initialised gaceta db + reader config."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import make_fixture_db as fwmod
import gaceta.config as gconfig
import gaceta.db as gdb


class Env:
    def __init__(self, conn, cfg, info, src_root, data_dir):
        self.conn = conn
        self.cfg = cfg
        self.info = info
        self.src_root = src_root
        self.data_dir = data_dir

    def annotation(self, item_id):
        return self.conn.execute(
            "SELECT * FROM annotations WHERE reader_id = ? AND item_id = ?",
            (self.cfg["id"], item_id),
        ).fetchone()


@pytest.fixture
def env(tmp_path):
    src_root = tmp_path / "sourcedb"
    info = fwmod.build(src_root)

    data_dir = tmp_path / "data"
    reader_dir = data_dir / "readers" / "testreader"
    reader_dir.mkdir(parents=True)
    toml_text = (Path(fwmod.__file__).parent / "reader.toml").read_text()
    toml_text, n = re.subn(r'(?m)^path\s*=\s*".*"', f'path = "{src_root}"', toml_text)
    assert n == 1, "reader.toml fixture must have exactly one path line"
    (reader_dir / "reader.toml").write_text(toml_text)

    cfg = gconfig.load_reader("testreader", data_dir)
    conn = gdb.connect(data_dir / "gaceta.db")
    gdb.init(conn)
    return Env(conn, cfg, info, src_root, data_dir)
