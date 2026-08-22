"""Guards against the hand-maintained RTK release pin silently going stale."""

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.rtk import (
    RTK_RELEASE_BASE_URL,
    RTK_VERSION,
    parse_rtk_version,
)

_RTK_RELEASES_API = "https://api.github.com/repos/rtk-ai/rtk/releases/latest"
_SOURCE = Path(rtk_config.__file__).read_text(encoding="utf-8")


def test_pinned_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", RTK_VERSION)


def test_release_base_url_embeds_the_pinned_version():
    assert RTK_RELEASE_BASE_URL.endswith(f"/v{RTK_VERSION}")


def test_every_supported_platform_has_a_distinct_sha256():
    digests = [digest for _asset, digest in rtk_config._RELEASES.values()]
    assert len(digests) == 5
    assert len(set(digests)) == len(digests)
    for digest in digests:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_asset_names_are_unique_and_archive_shaped():
    assets = [asset for asset, _digest in rtk_config._RELEASES.values()]
    assert len(set(assets)) == len(assets)
    for asset in assets:
        assert asset.startswith("rtk-")
        assert asset.endswith((".tar.gz", ".zip"))


def test_windows_release_is_the_only_zip():
    zips = {
        key
        for key, (asset, _d) in rtk_config._RELEASES.items()
        if asset.endswith(".zip")
    }
    assert zips == {("win32", "x86_64")}


def test_pinned_digests_are_not_duplicated_across_versions_in_source():
    """Each digest literal must appear exactly once in the module."""

    for _asset, digest in rtk_config._RELEASES.values():
        assert _SOURCE.count(digest) == 1


@pytest.mark.skipif(
    os.environ.get("MCC_NETWORK_TESTS") != "1",
    reason="network-gated: set MCC_NETWORK_TESTS=1 to check the RTK pin upstream",
)
def test_pin_matches_latest_upstream_release():
    """Network-gated drift check against the real GitHub releases API.

    Skipped by default so offline CI stays green; run it deliberately to learn
    that upstream has moved past the pin.
    """

    with urllib.request.urlopen(_RTK_RELEASES_API, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    latest = parse_rtk_version(release["tag_name"])
    assets = {asset["name"]: asset for asset in release["assets"]}

    assert latest == RTK_VERSION, (
        f"RTK pin is stale: pinned {RTK_VERSION}, latest upstream {latest}. "
        "Refresh RTK_VERSION and all five sha256 digests together."
    )
    for asset_name, digest in rtk_config._RELEASES.values():
        upstream = assets[asset_name]["digest"]
        assert upstream == f"sha256:{digest}", asset_name
