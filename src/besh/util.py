# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""通用工具：配置、URL 解析、标识符校验、时间格式化。"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 12242
DEFAULT_REMOTE = "cf:besh"  # rclone 远端: cf 是 README 中配置的 Cloudflare R2 远端名
DEFAULT_SYNC_INTERVAL = 300  # 秒
MAX_B64 = 24 * 1024 * 1024  # 单次 base64 传输上限（约 32MB 原文）

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_WINDOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class BeshError(Exception):
    """面向用户的错误，CLI 会直接打印消息而不打印堆栈。"""


def getenv(*names: str, default: Optional[str] = None) -> Optional[str]:
    """按顺序读取环境变量，同时兼容大小写（README 使用小写 besh_*）。"""
    for name in names:
        for key in (name, name.upper()):
            val = os.environ.get(key)
            if val not in (None, ""):
                return val
    return default


def getenv_int(*names: str, default: int = 0) -> int:
    val = getenv(*names)
    if val is None:
        return default
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def safe_id(value: Any, what: str = "id") -> str:
    """校验工人 ID / 窗口名，避免注入到 SQL 或 tmux 目标里。"""
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise BeshError(
            f"非法{what}: {value!r}（只允许字母、数字、下划线、点、减号，"
            f"1-64 位，且不能以符号开头）"
        )
    return text


def safe_window(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _WINDOW_RE.match(text):
        raise BeshError(f"非法窗口名: {value!r}")
    return text


def to_bool(value: Any, default: bool = False) -> bool:
    """兼容 GET 请求里被 literal_eval 处理过的各种形态。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def now_iso() -> str:
    """可按字典序排序的时间戳（统一使用秒级精度，便于 SQL 字符串比较）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def today() -> str:
    """对象存储的日期目录 YYYY-MM-DD。"""
    return time.strftime("%Y-%m-%d")


def human_time(iso: str) -> str:
    """把 ISO 时间戳转成 '3s ago' 之类的相对时间。"""
    if not iso:
        return "-"
    try:
        ts = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return str(iso)
    delta = time.time() - ts
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def parse_beacon_url(url: Optional[str]) -> Tuple[str, str]:
    """解析 `besh_url`。

    支持这些写法：
        http://BEACON-HOST:12242?secret=PASSWORD
        https://host/prefix/?secret=PASSWORD
        BEACON-HOST:12242
    """
    raw = (url or "").strip() or f"http://127.0.0.1:{DEFAULT_PORT}"
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise BeshError(f"无法解析 besh_url: {url!r}")
    secret = ""
    if parsed.query:
        secret = (parse_qs(parsed.query).get("secret") or [""])[0]
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return base, secret


def human_size(num: Any) -> str:
    try:
        size = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def shorten(text: str, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
