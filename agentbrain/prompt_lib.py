"""提示词资产加载器：把可手改的提示词放在 `prompt_assets/*.md`，代码按需读取。

设计要点：
- **不缓存到进程启动时**：按 mtime+size 判断文件是否变化，改了 md 下一次复盘就生效，
  不必重启常驻 broker（提示词调优是高频动作，重启一次就是十几秒）。
- 文件缺失或为空时**明确报错**，不静默回退成空提示词——空提示词会让模型自由发挥写入内容。
"""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR_NAME = "prompt_assets"


class PromptAssetError(RuntimeError):
    """提示词资产缺失或为空。"""


def prompt_dir() -> Path:
    """返回提示词目录（随 agentbrain 包分发）。"""
    return Path(__file__).resolve().parent / PROMPT_DIR_NAME


_CACHE: dict[str, tuple[float, int, str]] = {}
_SOUL_CACHE: dict[str, tuple[float, int, str]] = {}


def _strip_yaml_frontmatter(text: str) -> str:
    """若首行是 ---，剥离 YAML frontmatter，只保留正文。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                return "\n".join(lines[idx + 1 :])
    return text


def load_soul(path: Path | str) -> str:
    """按 mtime+size 热读 SOUL.md 正文（剥离 YAML frontmatter）；缺失或为空明确报错。"""
    soul_path = Path(path)
    try:
        stat = soul_path.stat()
    except OSError as exc:
        raise PromptAssetError(f"SOUL 文件不存在: {soul_path}") from exc

    cache_key = str(soul_path.resolve())
    cached = _SOUL_CACHE.get(cache_key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    raw = soul_path.read_text(encoding="utf-8-sig")
    body = _strip_yaml_frontmatter(raw).strip()
    if not body:
        raise PromptAssetError(f"SOUL 文件为空: {soul_path}")
    _SOUL_CACHE[cache_key] = (stat.st_mtime, stat.st_size, body)
    return body


def load_prompt(name: str) -> str:
    """读取 `prompt_assets/<name>.md` 的正文（按 mtime+size 做热更新缓存）。"""
    clean = str(name or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        raise PromptAssetError(f"非法提示词名: {name!r}")
    path = prompt_dir() / f"{clean}.md"
    try:
        stat = path.stat()
    except OSError as exc:
        raise PromptAssetError(f"提示词文件不存在: {path}") from exc

    cached = _CACHE.get(clean)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise PromptAssetError(f"提示词文件为空: {path}")
    _CACHE[clean] = (stat.st_mtime, stat.st_size, text)
    return text


def list_prompts() -> list[str]:
    """列出可用提示词名（不含扩展名），便于诊断。"""
    directory = prompt_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.md"))
