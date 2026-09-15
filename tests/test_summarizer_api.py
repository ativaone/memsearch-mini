"""Tests for memsearch_mini.summarizer_api — the stdlib HTTP summarizer.

Every request lands on a throwaway ``http.server`` bound to 127.0.0.1 on an ephemeral port
and torn down with the test, so no vendor endpoint is ever contacted.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from memsearch_mini import summarizer_api

FIRST, SECOND = "- User asked about the hook.\n", "- Claude Code explained it."
BULLETS = FIRST + SECOND
OPENAI_OK = {"choices": [{"message": {"content": BULLETS}}]}
# Both vendors answer in blocks, and both are concatenated: thinking blocks are not text.
ANTHROPIC_OK = {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": BULLETS}]}
GOOGLE_OK = {"candidates": [{"content": {"parts": [{"text": FIRST}, {"text": SECOND}]}}]}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # a handler still sleeping out the timeout test must not hold teardown

    def handle_error(self, request, client_address) -> None:
        """A client that gave up mid-response is the point of one of these tests, not a crash."""


@pytest.fixture
def stub():
    """Start recording HTTP stubs; returns ``(base_url, requests)`` per stub."""
    started: list[tuple[_Server, threading.Thread]] = []

    def start(*, status: int = 200, body: object = OPENAI_OK, delay: float = 0.0):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:  # BaseHTTPRequestHandler dispatches on the method name
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                # Header names are case-insensitive on the wire (urllib capitalizes them).
                headers = {name.lower(): value for name, value in self.headers.items()}
                seen.append({"path": self.path, "headers": headers, "body": json.loads(raw or b"{}")})
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                """Silence the default stderr access log."""

        server = _Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}", seen

    yield start

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(base_url: str, *, provider: str = "openai", model: str = "m-1", timeout: float = 5.0):
    return summarizer_api.summarize_via_api(
        "TRANSCRIPT_BODY",
        prompt="PROMPT_BODY",
        provider=provider,
        model=model,
        api_key="k-1",
        base_url=base_url,
        timeout=timeout,
    )


def _prompt_and_transcript(provider: str, body: dict) -> tuple[str, str]:
    """The system prompt and the user message, wherever that vendor puts them."""
    if provider == "openai":
        assert body["model"] == "m-1"
        return body["messages"][0]["content"], body["messages"][1]["content"]
    if provider == "anthropic":
        assert (body["model"], body["max_tokens"]) == ("m-1", 1024)
        return body["system"], body["messages"][0]["content"]
    return body["systemInstruction"]["parts"][0]["text"], body["contents"][0]["parts"][0]["text"]


# --- the three request shapes ------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "body", "path", "header"),
    [
        ("openai", OPENAI_OK, "/chat/completions", ("authorization", "Bearer k-1")),
        ("anthropic", ANTHROPIC_OK, "/v1/messages", ("x-api-key", "k-1")),
        ("google", GOOGLE_OK, "/v1beta/models/m-1:generateContent", ("x-goog-api-key", "k-1")),
    ],
)
def test_each_provider_posts_its_documented_shape(stub, provider, body, path, header):
    base_url, seen = stub(body=body)

    assert _call(base_url, provider=provider) == (BULLETS, "")
    assert len(seen) == 1
    assert seen[0]["path"] == path
    assert seen[0]["headers"][header[0]] == header[1]
    assert seen[0]["headers"]["content-type"] == "application/json"
    assert seen[0]["headers"]["user-agent"] == "memsearch-mini"
    assert _prompt_and_transcript(provider, seen[0]["body"]) == ("PROMPT_BODY", "Transcript:\nTRANSCRIPT_BODY")


def test_anthropic_sends_the_pinned_api_version(stub):
    base_url, seen = stub(body=ANTHROPIC_OK)

    _call(base_url, provider="anthropic")

    assert seen[0]["headers"]["anthropic-version"] == "2023-06-01"


def test_a_trailing_slash_does_not_double_the_separator(stub):
    base_url, seen = stub()

    assert _call(f"{base_url}/") == (BULLETS, "")
    assert seen[0]["path"] == "/chat/completions"


def test_an_unknown_provider_is_reported_instead_of_raising(stub):
    base_url, seen = stub()

    assert _call(base_url, provider="ollama") == ("", "summarize.provider 'ollama' unknown")
    assert seen == []


# --- failure reasons ---------------------------------------------------------


def test_an_http_error_reports_its_status(stub):
    base_url, _ = stub(status=500, body={"error": "boom"})

    assert _call(base_url) == ("", "summarizer returned HTTP 500")


@pytest.mark.parametrize("body", [b"<html>not json</html>", b"", {"choices": []}, {"unexpected": True}])
def test_a_body_that_is_not_the_documented_shape_is_malformed(stub, body):
    base_url, _ = stub(body=body)

    assert _call(base_url) == ("", "summarizer returned malformed JSON")


def test_empty_content_is_reported(stub):
    base_url, _ = stub(body={"choices": [{"message": {"content": "   \n"}}]})

    assert _call(base_url) == ("", "summarizer returned empty output")


def test_prose_without_bullets_is_never_journaled(stub):
    """Same contract as the CLI path: a refusal or a rate-limit notice is not a summary."""
    base_url, _ = stub(body={"choices": [{"message": {"content": "You've hit your limit. Resets at 11am."}}]})

    assert _call(base_url) == ("", "summarizer returned no bullet points")


def test_a_refused_connection_is_unavailable():
    with socket.socket() as probe:  # bind, read the port, release it: nothing listens there
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert _call(f"http://127.0.0.1:{port}") == ("", "summarizer unavailable")


def test_a_slow_answer_times_out(stub):
    base_url, _ = stub(delay=2.0)

    assert _call(base_url, timeout=0.3) == ("", "summarizer timed out")
