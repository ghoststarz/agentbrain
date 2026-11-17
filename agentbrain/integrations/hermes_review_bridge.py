"""Safe subprocess bridge for invoking Hermes framework's auxiliary LLM router.

Executed by the Hermes runtime's own Python interpreter in isolated mode (`-I`)
with `HERMES_DISABLE_LAZY_INSTALLS=1`. Reads a single JSON request from stdin,
calls `agent.auxiliary_client.call_llm(task="background_review", ...)` with
`tools=None` and `stream=False`, and writes a single JSON response to stdout.

Never reads, logs, or prints secrets or API keys.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(raw_text)
    obj = json.loads(cleaned)
    if not isinstance(obj, dict):
        raise ValueError("Model response JSON is not an object")
    return obj


def _safe_route_metadata(route_info: dict[str, Any]) -> dict[str, str]:
    allowed = ("provider", "model", "api_mode", "resolved_provider", "resolved_model")
    out: dict[str, str] = {}
    for key in allowed:
        val = route_info.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def main() -> int:
    real_stdout = sys.stdout
    # Redirect stdout -> stderr so any stray prints from Hermes imports/plugins
    # cannot corrupt the structured JSON response written to real_stdout.
    sys.stdout = sys.stderr
    try:
        raw_in = sys.stdin.read()
        req = json.loads(raw_in) if raw_in.strip() else {}
        if not isinstance(req, dict):
            raise ValueError("Bridge stdin payload must be a JSON object")

        hermes_agent_dir = str(req.get("hermes_agent_dir") or "").strip()
        hermes_home = str(req.get("hermes_home") or "").strip()
        system_prompt = str(req.get("system_prompt") or "")
        user_prompt = str(req.get("user_prompt") or "")
        timeout_seconds = float(req.get("timeout_seconds") or 30.0)
        max_output_tokens = int(req.get("max_output_tokens") or 2000)
        temperature = float(req.get("temperature") if req.get("temperature") is not None else 0.1)

        if not hermes_agent_dir:
            raise ValueError("hermes_agent_dir is required")
        agent_path = Path(hermes_agent_dir)
        if not agent_path.is_dir():
            raise FileNotFoundError(f"hermes_agent_dir not found: {hermes_agent_dir}")

        os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        if hermes_home:
            os.environ["HERMES_HOME"] = hermes_home

        agent_str = str(agent_path)
        if agent_str not in sys.path:
            sys.path.insert(0, agent_str)

        from agent.auxiliary_client import call_llm, extract_content_or_reasoning

        route_info: dict[str, Any] = {}
        response = call_llm(
            task="background_review",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_output_tokens,
            tools=None,
            timeout=timeout_seconds,
            extra_body={"response_format": {"type": "json_object"}},
            stream=False,
            route_info=route_info,
        )
        raw_content = extract_content_or_reasoning(response) or ""
        parsed_content = _extract_json_object(str(raw_content))
        result = {
            "ok": True,
            "content": parsed_content,
            "route": _safe_route_metadata(route_info),
        }
        real_stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        real_stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001
        err_payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        real_stdout.write(json.dumps(err_payload, ensure_ascii=False) + "\n")
        real_stdout.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
