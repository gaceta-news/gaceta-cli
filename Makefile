check:
	python -m compileall -q src
	uv run --with pytest pytest -q
	python scripts/check-parity.py

release-dry:
	bash scripts/release.sh $(V) --dry-run

release:
	bash scripts/release.sh $(V)

packages:
	python scripts/build-packages.py

packages-check:
	python scripts/build-packages.py --check

.PHONY: check packages packages-check release release-dry
