check:
	python -m compileall -q src
	uv run --with pytest pytest -q
	python scripts/check-parity.py

packages:
	python scripts/build-packages.py

packages-check:
	python scripts/build-packages.py --check

.PHONY: check packages packages-check
