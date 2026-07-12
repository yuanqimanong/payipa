"""Release metadata must move as one versioned unit."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import payipa
import pyp_agent
import pyp_server
from payipa.db.revisions import script_head
from payipa_contracts import CONTRACT_VERSION, MIN_SUPPORTED_CONTRACT_VERSION
from pyp_server.settings import ServerSettings

ROOT = Path(__file__).parents[1]


def test_release_manifest_matches_runtime_versions() -> None:
    manifest = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    release = manifest["release"]
    package_files = [
        ROOT / "apps/server/pyproject.toml",
        ROOT / "packages/payipa-contracts/pyproject.toml",
        ROOT / "packages/payipa-core/pyproject.toml",
        ROOT / "packages/pyp-agent/pyproject.toml",
    ]
    assert {tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"] for path in package_files} == {
        release
    }
    assert {payipa.__version__, pyp_agent.__version__, pyp_server.__version__, ServerSettings().version} == {release}
    assert manifest["contract_version"] == CONTRACT_VERSION
    assert manifest["minimum_contract_version"] == MIN_SUPPORTED_CONTRACT_VERSION
    assert manifest["schema_head"] == script_head()


def test_release_support_files_exist() -> None:
    for name in ("LICENSE", "SECURITY.md", "CHANGELOG.md"):
        assert (ROOT / name).is_file()
