"""Safe subprocess bridge for invoking Hermes framework's auxiliary LLM router.

Executed by the Hermes runtime's own Python interpreter in isolated mode (`-I`)
with `HERMES_DISABLE_LAZY_INSTALLS=1`. Reads a single JSON request from stdin,
calls `agent.auxiliary_client.call_llm(task="background_review", ...)` with
`tools=None` and `stream=False`, and writes a single JSON response to stdout.

Never reads, logs, or prints secrets or API keys.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any


_MODEL_BLOCK_KEYS = ("default", "provider", "base_url", "api_mode")


def _parse_model_block_stub(text: str) -> dict[str, str] | None:
    """无 PyYAML/ruamel 时的最小解析器：只扫顶层 `model:` 块下的四个标量键。

    运行期（Hermes 受管解释器，`-I` 隔离模式）**没有 PyYAML**，只有 ruamel.yaml，
    所以不能假设 `import yaml` 可用；这个兜底保证路由解析在任何宿主里都能工作。
    """
    in_model = False
    found: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            in_model = stripped.startswith("model:")
            if in_model and ":" in stripped:
                inline = stripped.split(":", 1)[1].strip()
                if inline.startswith("{") and inline.endswith("}"):
                    body = inline[1:-1]
                    for pair in body.split(","):
                        if ":" not in pair:
                            continue
                        k, v = pair.split(":", 1)
                        k = k.strip().strip("'\"")
                        v = v.strip().strip("'\"")
                        if k in _MODEL_BLOCK_KEYS and v:
                            found[k] = v
            continue
        if not in_model:
            continue
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip().strip("'\"")
        val = val.strip().strip("'\"").strip()
        if key in _MODEL_BLOCK_KEYS and val:
            found[key] = val
    if not found.get("default") or not found.get("provider"):
        return None
    return found


def _read_profile_default_model(hermes_home: str) -> dict[str, str] | None:
    """从 HERMES_HOME/config.yaml 读取 profile 默认模型配置（绝不读取任何密钥字段）。"""
    home_str = str(hermes_home or "").strip()
    if not home_str:
        return None
    cfg_path = Path(home_str) / "config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        raw_text = cfg_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None

    parsed: Any = None
    try:  # 开发环境（仓库 venv）通常有 PyYAML
        import yaml  # type: ignore[import-untyped]

        parsed = yaml.safe_load(raw_text)
    except Exception:
        try:  # Hermes 受管解释器只带 ruamel.yaml
            from ruamel.yaml import YAML  # type: ignore[import-untyped]

            parsed = YAML(typ="safe").load(raw_text)
        except Exception:
            parsed = None

    model_cfg: Any = None
    if isinstance(parsed, dict):
        model_cfg = parsed.get("model")
    if not isinstance(model_cfg, dict):
        # 两种 YAML 库都不可用时退回最小解析器（只认 model 块里的标量键）。
        model_cfg = _parse_model_block_stub(raw_text)
    if not isinstance(model_cfg, dict):
        return None
    default_model = str(model_cfg.get("default") or "").strip()
    provider = str(model_cfg.get("provider") or "").strip()
    if not default_model or not provider:
        return None
    out: dict[str, str] = {"provider": provider, "model": default_model}
    base_url = str(model_cfg.get("base_url") or "").strip()
    if base_url:
        out["base_url"] = base_url
    api_mode = str(model_cfg.get("api_mode") or "").strip()
    if api_mode:
        out["api_mode"] = api_mode
    return out


def _filter_supported_kwargs(fn: Any, extra: dict[str, Any]) -> dict[str, Any]:
    """仅传入 call_llm 签名支持的可选参数，避免老版本签名报 TypeError。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(extra)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(extra)
    return {k: v for k, v in extra.items() if k in params}


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
        if str(obj.get("op_type") or "").strip():
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
        # 多行 JSONL 先按行抢救（截断只丢最后一行），再退回整体修补。
        salvaged = _extract_jsonl_operations(cleaned)
        if salvaged is not None:
            return salvaged
        try:
            return _repair_truncated_json(cleaned)
        except ValueError:
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


def _normalize_batch(parsed_content: dict[str, Any]) -> dict[str, Any]:
    """把模型输出收敛成下游可校验的批次形状，并丢掉空操作占位。"""
    if not isinstance(parsed_content, dict):
        return parsed_content
    if "outcome" not in parsed_content:
        if str(parsed_content.get("op_type") or "").strip():
            parsed_content = {"outcome": "operations", "operations": [parsed_content]}
        elif isinstance(parsed_content.get("operations"), list):
            parsed_content = {"outcome": "operations", "operations": parsed_content["operations"]}
    if isinstance(parsed_content.get("operations"), list):
        cleaned_ops = [
            o
            for o in parsed_content["operations"]
            if isinstance(o, dict) and str(o.get("op_type") or "").strip()
        ]
        if cleaned_ops:
            parsed_content = {"outcome": "operations", "operations": cleaned_ops}
        elif str(parsed_content.get("outcome") or "").lower() != "no_change":
            parsed_content = {"outcome": "no_change", "operations": []}
    return parsed_content


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

        profile_model = _read_profile_default_model(hermes_home)
        route_source = "profile_default" if profile_model is not None else "framework_fallback"
        route_resolved = profile_model is not None

        explicit_route_kwargs: dict[str, Any] = {}
        if profile_model is not None:
            explicit_route_kwargs["provider"] = profile_model["provider"]
            explicit_route_kwargs["model"] = profile_model["model"]
            if "base_url" in profile_model:
                explicit_route_kwargs["base_url"] = profile_model["base_url"]
            if "api_mode" in profile_model:
                explicit_route_kwargs["api_mode"] = profile_model["api_mode"]
            explicit_route_kwargs = _filter_supported_kwargs(call_llm, explicit_route_kwargs)

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
            **explicit_route_kwargs,
        )
        raw_content = extract_content_or_reasoning(response) or ""
        parsed_content = _normalize_batch(_extract_json_object(str(raw_content)))
        safe_route: dict[str, Any] = _safe_route_metadata(route_info)
        if profile_model is not None:
            safe_route.setdefault("resolved_provider", profile_model["provider"])
            safe_route.setdefault("resolved_model", profile_model["model"])
        safe_route["route_source"] = route_source
        safe_route["route_resolved"] = route_resolved
        result = {
            "ok": True,
            "content": parsed_content,
            "route": safe_route,
            "route_source": route_source,
            "route_resolved": route_resolved,
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
