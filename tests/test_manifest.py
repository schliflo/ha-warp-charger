"""Packaging consistency checks."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_lib_pin_matches_manifest() -> None:
    """manifest.json and the dev dependency pin the same warp-charger version."""
    manifest = json.loads((ROOT / "custom_components/tinkerforge_warp/manifest.json").read_text())
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    pins = [d for d in dev if re.match(r"warp-charger\b", d)]
    assert pins == [r for r in manifest["requirements"] if r.startswith("warp-charger")]
