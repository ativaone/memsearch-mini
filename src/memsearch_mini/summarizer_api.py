"""The ``[summarize] mode = "api"`` summarizer: one HTTPS request, written with the stdlib.

No SDK and no extra to install, so switching to it never triggers a runtime resync. The three
request shapes are the vendors' documented chat endpoints; everything that can go wrong comes
back as a failure reason worded exactly like the CLI path's, because the journal renders both
through ``capture.unavailable_line``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .capture import output_problem

USER_AGENT = "memsearch-mini"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 1024


def _openai(model: str, prompt: str, user: str, api_key: str, root: str) -> tuple[str, dict, dict]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
    }
    return f"{root}/chat/completions", {"Authorization": f"Bearer {api_key}"}, body


def _openai_text(data: dict) -> str:
    return str(data["choices"][0]["message"]["content"] or "")


def _anthropic(model: str, prompt: str, user: str, api_key: str, root: str) -> tuple[str, dict, dict]:
    body = {
        "model": model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "system": prompt,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    return f"{root}/v1/messages", headers, body


def _anthropic_text(data: dict) -> str:
    blocks = data["content"]
    return "".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")


def _google(model: str, prompt: str, user: str, api_key: str, root: str) -> tuple[str, dict, dict]:
    body = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
    }
    return f"{root}/v1beta/models/{model}:generateContent", {"x-goog-api-key": api_key}, body


def _google_text(data: dict) -> str:
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(str(part.get("text") or "") for part in parts)


_PROVIDERS = {
    "openai": (_openai, _openai_text),
    "anthropic": (_anthropic, _anthropic_text),
    "google": (_google, _google_text),
}


def summarize_via_api(
    text: str,
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
) -> tuple[str, str]:
    """Return ``(summary, failure_reason)``; exactly one is non-empty. Never raises.

    The key travels in a header and is never logged, printed or echoed into a reason.
    """
    build, extract = _PROVIDERS.get(provider, (None, None))
    if build is None or extract is None:
        return "", f"summarize.provider {provider!r} unknown"
    url, auth, body = build(model, prompt, f"Transcript:\n{text}", api_key, base_url.removesuffix("/"))
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **auth},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        return "", f"summarizer returned HTTP {exc.code}"
    except TimeoutError:  # a read that outlived the budget: not wrapped by urllib
        return "", "summarizer timed out"
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        return "", "summarizer timed out" if timed_out else "summarizer unavailable"
    except Exception:  # refused, DNS, TLS, a socket that died mid-read
        return "", "summarizer unavailable"
    try:
        summary = extract(json.loads(raw.decode("utf-8", errors="replace"))).strip()
    except Exception:  # not JSON, or JSON the vendor documented differently than it answered
        return "", "summarizer returned malformed JSON"
    problem = output_problem(summary)
    return ("", problem) if problem else (summary, "")
