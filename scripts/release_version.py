"""Synchronize a release group's versions using uv without publishing anything."""

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path

RELEASE_GROUPS = {
    "bedrock-core": (".", "packages/bedrock", "packages/bedrock-cli"),
    "bedrock-fastapi": ("integrations/fastapi",),
    "bedrock-celery": ("integrations/celery",),
}


def main() -> None:
    """Update the selected release group and restore files if synchronization fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", default=os.environ.get("VERSION"))
    parser.add_argument("--package", default=os.environ.get("PACKAGE", "bedrock-core"), choices=RELEASE_GROUPS)
    args = parser.parse_args()
    if not args.version:
        parser.error("provide a version argument or set VERSION (make release-version VERSION=X.Y.Z)")
    if args.package not in RELEASE_GROUPS:
        parser.error(f"unknown release package: {args.package}")

    root = Path(__file__).resolve().parents[1]
    manifests = [root / directory / "pyproject.toml" for directory in RELEASE_GROUPS[args.package]]
    packages = [tomllib.loads(path.read_text())["project"]["name"] for path in manifests]
    command = ["uv", "version", "--frozen"]
    preview = subprocess.check_output(
        [*command, "--package", packages[0], "--dry-run", "--output-format", "json", "--", args.version],
        cwd=root,
        text=True,
    )
    version = json.loads(preview)["version"]
    lockfile = root / "uv.lock"
    originals = {path: path.read_bytes() if path.exists() else None for path in [*manifests, lockfile]}
    try:
        for package in packages:
            subprocess.run([*command, "--package", package, "--", args.version], cwd=root, check=True)
        subprocess.run(["uv", "lock"], cwd=root, check=True)
        for path in manifests:
            if tomllib.loads(path.read_text())["project"]["version"] != version:
                raise ValueError(f"Version mismatch in {path.relative_to(root)}: expected {version}")
    except BaseException:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    print(f"Synchronized {', '.join(packages)} to {version}; updated uv.lock. No commit, tag, or publish performed.")


if __name__ == "__main__":
    main()
