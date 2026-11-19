check:
	python -m compileall -q src
	uv run --with pytest pytest -q

.PHONY: check
