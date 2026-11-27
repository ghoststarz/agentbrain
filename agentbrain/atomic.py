"""原子写入与文件锁工具模块（Windows msvcrt 加锁 + tmp + os.replace）。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Windows 上 os.replace 会因目标被短暂占用（git 同步/杀软/Obsidian 编辑器）抛 WinError 5/32，
# 实测一次批量写入因此中途失败。这类占用通常几十毫秒内释放，重试比让整批作废划算。
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF_SECONDS = 0.05


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace，遇到权限类错误时指数退避重试。"""
    last: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            # WinError 5 (拒绝访问) / 32 (共享冲突) 与 errno EACCES/EPERM 都是「被别人短暂持有」
            winerror = getattr(exc, "winerror", None)
            if winerror not in (5, 32) and exc.errno not in (13, 1):  # 13=EACCES, 1=EPERM
                raise
            last = exc
            time.sleep(_REPLACE_BACKOFF_SECONDS * (2**attempt))
    assert last is not None
    raise last


@contextmanager
def _lock_file_descriptor(fd: int) -> Iterator[None]:
    """对已打开的文件描述符加排他锁，并在退出时解锁。"""
    if os.name == "nt":
        # Windows msvcrt 对首字节加锁，确保跨进程写入互斥
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """使用同目录临时文件 + 文件锁 + os.replace 原子写入字节内容。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            with _lock_file_descriptor(fh.fileno()):
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        _replace_with_retry(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    return target


def atomic_write_text(
    path: str | Path,
    content: str,
    encoding: str = "utf-8",
) -> Path:
    """以 UTF-8 无 BOM 与 LF(\\n) 换行原子写入文本文件。"""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return atomic_write_bytes(path, normalized.encode(encoding))


def atomic_write_json(path: str | Path, obj: Any, indent: int = 2) -> Path:
    """以格式化 UTF-8 JSON 原子写入文件。"""
    text = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    return atomic_write_text(path, text)
