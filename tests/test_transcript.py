from __future__ import annotations

import json
from pathlib import Path

import pytest

from memsearch import transcript as tr


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_claude_format_extracts_tool_command_and_output(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "c.jsonl",
        [
            {"type": "user", "uuid": "u1", "message": {"content": "run the tests"}},
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "content": [
                        {"type": "text", "text": "Running them"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -x --ff tests/"}},
                    ]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "5 passed"}]},
            },
        ],
    )
    assert tr.detect_format(tr._load_jsonl(p)) == "claude"
    turns = tr.parse_transcript(p)
    assert [t.role for t in turns] == ["user", "assistant"]
    tc = turns[1].tools[0]
    assert tc.command == "pytest -x --ff tests/"  # full command, not truncated
    assert "5 passed" in tc.output


def test_codex_rollout_extracts_function_call(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "r.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "deploy to staging"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "c1",
                    "arguments": json.dumps({"command": ["bash", "-lc", "make build-fast && kubectl apply -f k8s/"]}),
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "c1", "output": "deployed"},
            },
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}},
        ],
    )
    assert tr.detect_format(tr._load_jsonl(p)) == "codex"
    turns = tr.parse_transcript(p)
    tools = [tc for t in turns for tc in t.tools]
    assert "make build-fast && kubectl apply -f k8s/" in tools[0].command  # exact command recovered
    assert tools[0].output == "deployed"


def test_openclaw_transcripts_are_no_longer_recognised(tmp_path: Path) -> None:
    """The fork serves Claude Code and Codex only; OpenClaw JSONL is unknown now."""
    p = _write(
        tmp_path / "o.jsonl",
        [
            {"type": "message", "id": "m1", "message": {"role": "user", "content": [{"type": "text", "text": "lint"}]}},
            {
                "type": "message",
                "id": "m2",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            },
        ],
    )
    with pytest.raises(tr.UnknownTranscriptFormat):
        tr.parse_transcript(p)
    assert set(tr._PARSERS) == {"claude", "codex"}
    assert not hasattr(tr, "_parse_openclaw")


def test_unknown_format_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "x.jsonl", [{"foo": "bar"}, {"baz": 1}])
    with pytest.raises(tr.UnknownTranscriptFormat):
        tr.parse_transcript(p)


def test_format_turns_includes_command(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "c.jsonl",
        [
            {"type": "user", "uuid": "u1", "message": {"content": "go"}},
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "uv run pytest"}}]
                },
            },
        ],
    )
    rendered = tr.format_turns(tr.parse_transcript(p))
    assert "uv run pytest" in rendered


def test_select_turns_by_id(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "c.jsonl",
        [
            {"type": "user", "uuid": "aaaa1111", "message": {"content": "one"}},
            {"type": "user", "uuid": "bbbb2222", "message": {"content": "two"}},
            {"type": "user", "uuid": "cccc3333", "message": {"content": "three"}},
        ],
    )
    turns = tr.parse_transcript(p)
    sel = tr.select_turns(turns, "bbbb2222", context=0)
    assert [t.text for t in sel] == ["two"]


def test_tool_output_is_clipped(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "c.jsonl",
        [
            {"type": "user", "uuid": "u1", "message": {"content": "go"}},
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Z" * 5000}]},
            },
        ],
    )
    turns = tr.parse_transcript(p)
    assert len(turns[1].tools[0].output) <= tr.MAX_OUTPUT_CHARS + 20  # clipped, not the full 5000


def test_render_is_capped(tmp_path: Path) -> None:
    rows = [{"type": "user", "uuid": f"u{i}", "message": {"content": "x" * 60}} for i in range(2000)]
    out = tr.format_turns(tr.parse_transcript(_write(tmp_path / "big.jsonl", rows)))
    assert len(out) <= tr.MAX_RENDER_CHARS + 80
    assert "truncated" in out


# --- harness-injected blocks (upstream #227) ---------------------------------


def _user(text: str, uuid: str = "u") -> dict:
    return {"type": "user", "uuid": uuid, "message": {"content": text}}


def test_slash_command_wrapper_alone_yields_no_user_turn(tmp_path: Path) -> None:
    wrapper = (
        "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n"
        "<command-args></command-args>\n<local-command-stdout>Cancelled</local-command-stdout>"
    )
    p = _write(tmp_path / "c.jsonl", [_user(wrapper), _user("real question", "u2")])
    turns = tr.parse_transcript(p)
    assert [(t.uuid, t.text) for t in turns] == [("u2", "real question")]


def test_system_reminder_is_stripped_from_user_text(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.jsonl", [_user("<system-reminder>ignore me</system-reminder>\nreal question")])
    assert tr.parse_transcript(p)[0].text == "real question"


def test_large_command_stdout_does_not_eat_the_render_budget(tmp_path: Path) -> None:
    noise = "<local-command-stdout>" + "x" * 5000 + "</local-command-stdout>"
    p = _write(tmp_path / "c.jsonl", [_user(noise + "\nthe one sentence that matters")])
    rendered = tr.format_turns(tr.parse_transcript(p))
    assert "the one sentence that matters" in rendered
    assert "xxxx" not in rendered
    assert len(rendered) < 200


def test_user_authored_tags_are_kept(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.jsonl", [_user("<demanda>keep this</demanda> please")])
    assert tr.parse_transcript(p)[0].text == "<demanda>keep this</demanda> please"


def test_assistant_text_is_never_stripped(tmp_path: Path) -> None:
    rows = [
        _user("q"),
        {
            "type": "assistant",
            "uuid": "a",
            "message": {"content": [{"type": "text", "text": "see <system-reminder>quoted</system-reminder>"}]},
        },
    ]
    p = _write(tmp_path / "c.jsonl", rows)
    assert tr.parse_transcript(p)[1].text == "see <system-reminder>quoted</system-reminder>"
