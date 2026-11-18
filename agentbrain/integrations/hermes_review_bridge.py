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


def _repair_truncated_json(text: str) -> dict[str, Any]:
    """尽力从被截断的 JSON 中抢救出完整前缀（模型输出超长时常见）。

    只接受"顶层对象已闭合"或"截到最后一个完整成员后补齐括号"的结果；
    任何修补失败都抛 ValueError，让上层明确失败而不是静默写入半截内容。
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object start")
    body = text[start:]
    # 1) 直接解析
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2) 逐个裁剪到最后一个候选边界，补齐未闭合的括号
    for cut in range(len(body), 0, -1):
        if body[cut - 1] not in "}],\"":
            continue
        candidate = body[:cut]
        depth_sq = candidate.count("[") - candidate.count("]")
        depth_br = candidate.count("{") - candidate.count("}")
        in_str = candidate.count('"') % 2 == 1
        if depth_sq < 0 or depth_br < 0:
            continue
        suffix = ""
        if in_str:
            suffix += '"'
        suffix += "]" * depth_sq + "}" * depth_br
        try:
            obj = json.loads(candidate + suffix)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("unrepairable truncated JSON")


def _extract_jsonl_operations(text: str) -> dict[str, Any] | None:
    """从 JSONL（每行一个操作对象）里抢救出所有完整行。

    单行被截断只损失最后一条，前面已完整的操作仍然可用——这比整批失败安全得多。
    """
    ops: list[dict[str, Any]] = []
    saw_no_change = False
    for raw_line in text.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if str(obj.get("outcome") or "").strip().lower() == "no_change":
            saw_no_change = True
        if "op_type" in obj:
            ops.append(obj)
        elif isinstance(obj.get("operations"), list):
            ops.extend([o for o in obj["operations"] if isinstance(o, dict)])
    if ops:
        return {"outcome": "operations", "operations": ops}
    if saw_no_change:
        return {"outcome": "no_change", "operations": []}
    return None


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(raw_text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            return _repair_truncated_json(cleaned)
        except ValueError:
            salvaged = _extract_jsonl_operations(cleaned)
            if salvaged is not None:
                return salvaged
            raise
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


def _write_result(stream, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:  # pragma: no cover - buffer 一定存在，仅作兜底
        stream.write(data.decode("utf-8"))
        stream.flush()


def main() -> int:
    real_stdout = sys.stdout
    # Redirect stdout -> stderr so any stray prints from Hermes imports/plugins
    # cannot corrupt the structured JSON response written to real_stdout.
    sys.stdout = sys.stderr
    try:
        # `-I` 会忽略 PYTHONIOENCODING 等环境变量，文本模式下 stdin 会按系统 ANSI 代码页
        # （本机 cp936）解码 UTF-8 中文载荷，在第一个多字节字符处就产生非法 JSON。
        # 必须读原始字节后显式按 UTF-8 解码。
        raw_bytes = sys.stdin.buffer.read()
        raw_in = raw_bytes.decode("utf-8", errors="replace")
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
            # 不请求 response_format=json_object：框架的结构化输出校验会在模型输出被截断时
            # 直接抛 JSONDecodeError（连我们的容错解析都轮不到）。我们自己在下面做容错解析。
            extra_body=None,
            stream=False,
            route_info=route_info,
        )
        raw_content = extract_content_or_reasoning(response) or ""
        parsed_content = _extract_json_object(str(raw_content))
        # 模型常直接吐单个操作对象（JSONL 风格），这里统一收敛成带 outcome 的批次形状，
        # 否则下游整批校验会判为「缺少 outcome」而白白丢弃一条真实操作。
        if isinstance(parsed_content, dict) and "outcome" not in parsed_content:
            if "op_type" in parsed_content:
                parsed_content = {"outcome": "operations", "operations": [parsed_content]}
            elif isinstance(parsed_content.get("operations"), list):
                parsed_content = {"outcome": "operations", "operations": parsed_content["operations"]}
        result = {
            "ok": True,
            "content": parsed_content,
            "route": _safe_route_metadata(route_info),
        }
        # ensure_ascii=True：模型偶尔返回孤立代理字符，直接写 UTF-8 会抛 UnicodeEncodeError
        _write_result(real_stdout, result)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)

        where = " <- ".join(f"{fr.filename}:{fr.lineno}" for fr in frames[-5:]) if frames else ""
        err_payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "where": where,
        }
        _write_result(real_stdout, err_payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
