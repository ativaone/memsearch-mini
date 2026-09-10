"""Help and version output for every command the plugin and the skill call."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from memsearch_mini.cli import cli

COMMANDS = [
    ["index"],
    ["search"],
    ["expand"],
    ["transcript"],
    ["config"],
    ["config", "get"],
    ["config", "set"],
    ["config", "list"],
    ["stats"],
    ["reset"],
    ["hook"],
    ["hook", "session-start"],
    ["hook", "stop"],
]


@pytest.mark.parametrize(
    ("args", "expected_text"),
    [
        pytest.param(["--help"], "Usage:", id="main-help"),
        pytest.param(["index", "--help"], "--skip-if-locked", id="index-help"),
        pytest.param(["search", "--help"], "--top-k", id="search-help"),
        pytest.param(["expand", "--help"], "--lines", id="expand-help"),
        pytest.param(["transcript", "--help"], "--context", id="transcript-help"),
        pytest.param(["config", "--help"], "Usage:", id="config-help"),
        pytest.param(["config", "get", "--help"], "KEY", id="config-get-help"),
        pytest.param(["config", "set", "--help"], "VALUE", id="config-set-help"),
        pytest.param(["config", "list", "--help"], "--json", id="config-list-help"),
        pytest.param(["stats", "--help"], "Usage:", id="stats-help"),
        pytest.param(["reset", "--help"], "--yes", id="reset-help"),
        pytest.param(["hook", "--help"], "session-start", id="hook-help"),
        pytest.param(["hook", "session-start", "--help"], "--platform", id="hook-session-start-help"),
        pytest.param(["--version"], "0.1.0", id="version"),
    ],
)
def test_cli_help_and_version_commands(args: list[str], expected_text: str) -> None:
    """CLI entrypoints should expose stable help/version output."""
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    assert expected_text in result.stdout


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: "-".join(c))
def test_help_never_mentions_the_removed_backend(command: list[str]) -> None:
    """The fork has one local SQLite index: no server, no collections."""
    result = CliRunner().invoke(cli, [*command, "--help"])

    assert result.exit_code == 0
    lowered = result.stdout.lower()
    assert not [word for word in ("milvus", "collection", "zilliz") if word in lowered]


@pytest.mark.parametrize("command", ["search", "expand", "transcript", "config list"])
def test_json_flag_is_spelled_the_same_everywhere(command: str) -> None:
    """The memory-recall skill passes --json; no command may spell it otherwise."""
    result = CliRunner().invoke(cli, [*command.split(), "--help"])

    assert result.exit_code == 0
    assert "--json" in result.stdout
    assert "--json-output" not in result.stdout
