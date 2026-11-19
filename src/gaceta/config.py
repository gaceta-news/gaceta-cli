"""Reader configuration. Everything personal comes from config under data/, never from src/.

Layout: <data_dir>/readers/<id>/reader.toml   (data_dir default ./data, env GACETA_DATA)
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("GACETA_DATA", "./data"))


def reader_config_path(reader_id: str, base: Path | None = None) -> Path:
    return (base or data_dir()) / "readers" / reader_id / "reader.toml"


def load_reader(reader_id: str, base: Path | None = None) -> dict:
    """Load and sanity-check a reader's TOML config."""
    path = reader_config_path(reader_id, base)
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("id", reader_id)
    cfg.setdefault("name", cfg["id"])
    cfg.setdefault("langs", "en")
    cfg["_reader_dir"] = str(path.parent)  # where profile_path etc. resolve from
    cfg.setdefault("privacy", {})
    cfg["privacy"].setdefault("pinned", [])
    cfg["privacy"].setdefault("household", [])
    cfg.setdefault("sources", {})
    return cfg
