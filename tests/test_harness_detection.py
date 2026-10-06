"""spec 2's three-tier harness detection: MCP `client_info` when present, else
environment signals (covered in tests/telemetry/test_wiring.py), else "unknown".
This file covers the tier this repo can only reach from inside a live request:
`server.client_info` and the enum it normalises into.
"""

import pytest
from mcp.server.mcpserver import Context
from speak_core.telemetry.wiring import normalize_harness

from speak.server import client_info


class _FakeParams:
    def __init__(self, name: str, version: str) -> None:
        class _Info:
            pass

        info = _Info()
        info.name = name  # type: ignore[attr-defined]
        info.version = version  # type: ignore[attr-defined]
        self.client_info = info


class _FakeSession:
    def __init__(self, client_params: object) -> None:
        self.client_params = client_params


class _FakeContext:
    def __init__(self, client_params: object) -> None:
        self.session = _FakeSession(client_params)


def test_client_info_reads_the_name_and_version_when_present() -> None:
    ctx = _FakeContext(_FakeParams("Gemini CLI", "1.2.3"))
    assert client_info(ctx) == ("Gemini CLI", "1.2.3")  # type: ignore[arg-type]


def test_client_info_is_none_when_the_host_declared_none() -> None:
    """`client_params` is `None` when a host declares no client info at all -
    optional in the MCP spec since 2026-07-28, a real runtime case, not padding."""
    ctx = _FakeContext(None)
    assert client_info(ctx) is None  # type: ignore[arg-type]


def test_client_info_is_none_outside_of_a_live_request() -> None:
    """The exact object every test calling `server.call_tool(...)` directly is
    handed: a real `Context` with no request in flight. `ctx.session` raises;
    `client_info` must absorb that rather than propagate it into the tool."""
    ctx = Context(mcp_server=None, subscriptions=None)
    assert client_info(ctx) is None


@pytest.mark.parametrize("name", ["claude-code", "Claude Code", "claude-code-sdk"])
def test_normalize_harness_recognises_claude_code(name) -> None:
    assert normalize_harness(name) == "claude-code"


@pytest.mark.parametrize("name", ["Claude Desktop", "claude-ai", "my-claude-client"])
def test_a_name_that_only_mentions_claude_is_another_host(name) -> None:
    """Counting every client with "claude" in its name as Claude Code would inflate
    the one number this column exists to measure."""
    assert normalize_harness(name) == "other"


def test_normalize_harness_recognises_gemini_cli() -> None:
    assert normalize_harness("gemini-cli") == "gemini-cli"


@pytest.mark.parametrize("name", ["antigravity-client", "Antigravity"])
def test_normalize_harness_recognises_antigravity(name) -> None:
    """Antigravity's MCP client declares itself as "antigravity-client"."""
    assert normalize_harness(name) == "antigravity"


def test_normalize_harness_recognises_codex() -> None:
    assert normalize_harness("Codex CLI") == "codex"


def test_normalize_harness_is_other_for_a_declared_but_unrecognised_client() -> None:
    """Distinct from "unknown": the client said who it is, just not one of the
    hosts this schema names."""
    assert normalize_harness("SomeOtherAgent") == "other"
