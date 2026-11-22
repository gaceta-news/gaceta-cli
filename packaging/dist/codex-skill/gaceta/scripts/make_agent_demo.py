#!/usr/bin/env python3
"""Generate the gaceta agent demo export into a working directory you own.

Bundled with the gaceta skill (candidate 0.3.0b1). Stdlib only, no
network, no model calls. Writes exactly one file, WORKDIR/export.json — the
synthetic, public-only demo export (invented stories, *.example hosts, no
real data). The edition clock is read at run time, so the export is never
stale. This script never touches a gaceta database or any other path.

Usage: python make_agent_demo.py WORKDIR
"""
import pathlib
import runpy
import sys

HERE = pathlib.Path(__file__).resolve().parent


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    workdir = pathlib.Path(argv[1])
    workdir.mkdir(parents=True, exist_ok=True)
    module = runpy.run_path(str(HERE / "make_demo_export.py"))
    path = module["build"](workdir)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
