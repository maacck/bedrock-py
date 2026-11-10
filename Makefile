PACKAGE ?= bedrock-core
export PACKAGE VERSION

.PHONY: release-version
release-version:
	uv run --no-project --python '>=3.12' scripts/release_version.py
