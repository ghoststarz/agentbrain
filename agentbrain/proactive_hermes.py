"""Hermes 框架后台评审桥接：发现受管解释器、隔离 profile 环境并安全调用桥接脚本。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from agentbrain.config import (
    AppConfig,
    ProactiveConfig,
    expand_portable_path,
    validate_proactive_worker_config,
)
from agentbrain.proactive_redaction import redact_secrets


class HermesBridgeError(RuntimeError):
    """Hermes 运行时发现或子进程桥接调用失败。"""


def get_hermes_review_bridge_path() -> Path:
    """定位随 agentbrain 包发布的 Hermes 后台评审桥接脚本。"""
    path = Path(__file__).resolve().parent / "integrations" / "hermes_review_bridge.py"
    if not path.is_file():
        raise HermesBridgeError(f"未找到打包的 Hermes 桥接脚本: {path}")
    return path


def resolve_hermes_root(cfg: ProactiveConfig) -> Path:
    """解析 Hermes 根目录（优先配置，其次环境变量，最后平台默认目录）。"""
    if cfg.hermes_home.strip():
        p = Path(expand_portable_path(cfg.hermes_home)).resolve()
        return p.parent.parent if p.parent.name == "profiles" else p
    env_root = os.environ.get("HERMES_ROOT", "").strip()
    if env_root:
        return Path(expand_portable_path(env_root)).resolve()
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        p = Path(expand_portable_path(env_home)).resolve()
        return p.parent.parent if p.parent.name == "profiles" else p
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        return (base / "hermes").resolve()
    return (Path.home() / ".hermes").resolve()


def resolve_hermes_profile_home(config: AppConfig) -> tuple[str, Path]:
    """解析隔离的 Hermes profile 名称及其 HERMES_HOME 目录。"""
    cfg = config.memory.proactive
    profile = (cfg.hermes_profile.strip() or config.memory.profile.strip() or "lily")
    if "/" in profile or "\\" in profile or ".." in profile or "\x00" in profile:
        raise HermesBridgeError(f"非法的 Hermes profile 名称: {profile!r}")
    root = resolve_hermes_root(cfg)
    profile_home = root if profile == "default" else (root / "profiles" / profile)
    return profile, profile_home


def resolve_hermes_agent_dir(cfg: ProactiveConfig, hermes_root: Path) -> Path:
    """解析 hermes-agent 源码/安装目录。"""
    if cfg.hermes_agent_dir.strip():
        return Path(expand_portable_path(cfg.hermes_agent_dir)).resolve()
    env_dir = os.environ.get("HERMES_AGENT_DIR", "").strip()
    if env_dir:
        return Path(expand_portable_path(env_dir)).resolve()
    return (hermes_root / "hermes-agent").resolve()


def _python_from_venv_dir(venv_dir: Path) -> Path | None:
    candidates = (
        (venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "python")
        if os.name == "nt"
        else (venv_dir / "bin" / "python3", venv_dir / "bin" / "python")
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _python_from_facts(facts_path: Path) -> Path | None:
    if not facts_path.is_file():
        return None
    try:
        data = json.loads(facts_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    env_dir = ((data.get("packages") or {}).get("venv") or {}).get("environment")
    if not isinstance(env_dir, str) or not env_dir.strip():
        return None
    return _python_from_venv_dir(Path(env_dir.strip()))


def discover_hermes_python(
    cfg: ProactiveConfig,
    hermes_root: Path,
    hermes_agent_dir: Path,
) -> Path:
    """通过显式配置或 Hermes install facts.json 发现受管 Python 解释器。"""
    if cfg.hermes_python.strip():
        explicit = Path(expand_portable_path(cfg.hermes_python)).resolve()
        if explicit.is_file():
            return explicit
        raise HermesBridgeError(f"配置的 hermes_python 不存在: {explicit}")

    installs_dir = hermes_root / "installs"
    if installs_dir.is_dir():
        key = hashlib.sha256(str(hermes_agent_dir.resolve()).encode("utf-8")).hexdigest()[:16]
        py = _python_from_facts(installs_dir / key / "facts.json")
        if py is not None:
            return py
        for facts_file in sorted(installs_dir.glob("*/facts.json")):
            py = _python_from_facts(facts_file)
            if py is not None:
                return py

    for venv_candidate in (hermes_agent_dir / ".venv", hermes_root / "venv"):
        py = _python_from_venv_dir(venv_candidate)
        if py is not None:
            return py

    raise HermesBridgeError(
        f"未找到 Hermes 受管 Python 解释器（已检查 {installs_dir} 与 {hermes_agent_dir / '.venv'}）"
    )


def build_hermes_bridge_env(profile: str, profile_home: Path) -> dict[str, str]:
    """构建隔离的子进程环境变量（剥离宿主 venv 变量并禁用按需安装）。"""
    env = dict(os.environ)
    for var in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(var, None)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["HERMES_HOME"] = str(profile_home)
    env["HERMES_PROFILE"] = profile
    env["AGENTBRAIN_PROACTIVE_INTERNAL"] = "1"
    return env


def _parse_bridge_response(raw_stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in raw_stdout.splitlines() if line.strip()]
    if not lines:
        raise HermesBridgeError("Hermes 桥接进程未返回任何输出")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise HermesBridgeError(f"Hermes 桥接输出非合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HermesBridgeError("Hermes 桥接输出顶层必须为 JSON 对象")
    if not payload.get("ok"):
        err_type = str(payload.get("error_type") or "HermesError")
        where = str(payload.get("where") or "").strip()
        err_msg = redact_secrets(str(payload.get("error") or "unknown error"))
        loc = f" at {where}" if where else ""
        raise HermesBridgeError(f"{err_type}{loc}: {err_msg}")
    content = payload.get("content")
    if not isinstance(content, dict):
        raise HermesBridgeError("Hermes 桥接返回 content 不是 JSON 对象")
    return content


def call_hermes_framework_model(
    config: AppConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """通过 Hermes 运行时受管解释器与桥接脚本执行结构化 JSON 评审。"""
    cfg = config.memory.proactive
    validate_proactive_worker_config(cfg)

    hermes_root = resolve_hermes_root(cfg)
    profile, profile_home = resolve_hermes_profile_home(config)
    hermes_agent_dir = resolve_hermes_agent_dir(cfg, hermes_root)
    bridge_path = get_hermes_review_bridge_path()
    env = build_hermes_bridge_env(profile, profile_home)

    request_payload = {
        "hermes_agent_dir": str(hermes_agent_dir),
        "hermes_home": str(profile_home),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "timeout_seconds": float(cfg.model_timeout_seconds),
        "max_output_tokens": int(cfg.model_max_output_tokens),
        "temperature": 0.1,
    }

    if runner is not None:
        raw_res = runner(
            bridge_path=bridge_path,
            request=request_payload,
            env=env,
            timeout=float(cfg.model_timeout_seconds) + 5.0,
        )
        if not isinstance(raw_res, dict):
            raise HermesBridgeError("fake/custom runner 必须返回 dict")
        if "ok" in raw_res:
            return _parse_bridge_response(json.dumps(raw_res, ensure_ascii=False))
        return raw_res

    if not hermes_agent_dir.is_dir():
        raise HermesBridgeError(f"Hermes 代码目录不存在: {hermes_agent_dir}")
    hermes_python = discover_hermes_python(cfg, hermes_root, hermes_agent_dir)

    proc_timeout = max(5.0, float(cfg.model_timeout_seconds) + 5.0)
    cmd = [str(hermes_python), "-I", str(bridge_path)]
    # 父进程是 pythonw（无控制台）。若子进程是控制台程序又不指定 CREATE_NO_WINDOW，
    # Windows 会给它新开一个控制台窗口——复盘每触发一次就闪一个空白 cmd/终端窗口。
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            cmd,
            input=json.dumps(request_payload, ensure_ascii=False),
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=proc_timeout,
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise HermesBridgeError(
            f"Hermes 桥接调用超时 ({proc_timeout:.1f}s)"
        ) from exc
    except OSError as exc:
        raise HermesBridgeError(f"启动 Hermes 桥接进程失败: {redact_secrets(str(exc))}") from exc

    if completed.stdout.strip():
        return _parse_bridge_response(completed.stdout)

    stderr_msg = redact_secrets(completed.stderr.strip())[:400]
    raise HermesBridgeError(
        f"Hermes 桥接进程异常退出 (code={completed.returncode}): {stderr_msg or '无输出'}"
    )
