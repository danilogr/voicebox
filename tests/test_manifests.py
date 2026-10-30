"""The plugin's three declarations of itself must agree, and name voicebox."""

import json
import tomllib
from pathlib import Path

from voicebox.server import VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_json_pyproject_and_the_server_agree_on_1_1_1():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert plugin["name"] == project["name"] == "voicebox"
    assert plugin["version"] == project["version"] == VERSION == "1.1.1"


def test_the_marketplace_lists_voicebox_first_and_walkie_from_github():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "voicebox-marketplace"
    voicebox, walkie = market["plugins"]
    assert (voicebox["name"], voicebox["source"]) == ("voicebox", "./")
    assert walkie["name"] == "walkie"
    assert walkie["source"] == {"source": "github", "repo": "danilogr/walkie"}


def test_the_mcp_server_is_named_voicebox_and_launched_by_its_script():
    servers = json.loads((ROOT / ".mcp.json").read_text())
    assert list(servers) == ["voicebox"]
    assert servers["voicebox"]["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/voicebox-mcp"]
    assert (ROOT / "scripts" / "voicebox-mcp").stat().st_mode & 0o111
