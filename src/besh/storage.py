# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""对象存储（Cloudflare R2 / 任意 rclone 远端）访问。

信标带宽贵、网速慢，所以工人的工作目录不落在信标磁盘上，而是交给 rclone
直接写进对象存储。信标只做"下指令"和"签链接"。

远端布局（README 约定）：``cf:besh/YYYY-MM-DD/WORKER_ID/*``
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from .util import DEFAULT_REMOTE, MAX_B64, BeshError, getenv, today

RCLONE_TIMEOUT = 900


def remote_base() -> str:
    """远端前缀，默认 ``cf:besh``，可用 ``besh_remote`` 覆盖（如 ``s3:mybucket/besh``）。"""
    return (getenv("besh_remote", "besh_remote_path", default=DEFAULT_REMOTE) or DEFAULT_REMOTE).rstrip("/")


def rclone_path() -> str:
    return getenv("besh_rclone", default="rclone") or "rclone"


def available() -> bool:
    return shutil.which(rclone_path()) is not None


def safe_rel_path(path: Optional[str]) -> str:
    """校验对象存储里的相对路径，杜绝 ``..`` 穿越与绝对路径。"""
    text = str(path or "").replace("\\", "/").strip()
    text = text.lstrip("/")
    if not text:
        return ""
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise BeshError(f"非法路径: {path!r}")
        parts.append(part)
    return "/".join(parts)


def remote_path(worker: str, path: str = "", date: str = "") -> str:
    """拼出 ``cf:besh/日期/工人/相对路径``。"""
    date = (date or today()).strip()
    rel = safe_rel_path(path)
    base = f"{remote_base()}/{date}/{worker}"
    return f"{base}/{rel}" if rel else base


async def rclone(args: List[str], timeout: int = RCLONE_TIMEOUT) -> Dict[str, Any]:
    """异步执行 rclone，返回 ``{ok, code, out, err}``。"""
    if not available():
        return {
            "ok": False,
            "code": 127,
            "out": "",
            "err": "未安装 rclone：请参考 README 安装并配置 RCLONE_CONFIG_CF_* 环境变量",
        }
    proc = await asyncio.create_subprocess_exec(
        rclone_path(),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:  # pragma: no cover - 网络异常
        proc.kill()
        return {"ok": False, "code": -1, "out": "", "err": f"rclone 超时（{timeout}s）"}
    text = (out or b"").decode("utf-8", "replace")
    error = (err or b"").decode("utf-8", "replace")
    return {"ok": proc.returncode == 0, "code": proc.returncode, "out": text, "err": error}


async def lsjson(remote: str) -> Dict[str, Any]:
    ret = await rclone(["lsjson", remote, "--files-only", "--no-mimetype"], timeout=120)
    if not ret["ok"]:
        lowered = (ret["err"] or "").lower()
        # 目录还不存在（工人还没备份过）不算错误
        if "directory not found" in lowered or "object not found" in lowered:
            return {"suc": True, "data": [], "empty": True}
        return {"suc": False, "data": [], "msg": (ret["err"] or ret["out"]).strip()[:400]}
    import json

    try:
        items = json.loads(ret["out"] or "[]")
    except ValueError:
        return {"suc": False, "data": [], "msg": "rclone 输出无法解析"}
    items = [
        {
            "path": i.get("Path", ""),
            "size": i.get("Size", 0),
            "mtime": (i.get("ModTime") or "")[:19].replace("T", " "),
        }
        for i in items
    ]
    items.sort(key=lambda i: i["path"])
    return {"suc": True, "data": items}


async def link(remote: str) -> Dict[str, Any]:
    """生成预签名下载链接（R2/S3 均支持），让浏览器直连对象存储、不走信标带宽。"""
    ret = await rclone(["link", remote], timeout=60)
    url = ""
    if ret["ok"]:
        # 兼容带日志的输出：取最后一行 http(s) 链接
        for line in reversed((ret["out"] or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                url = line
                break
    if not url:
        return {"suc": False, "msg": (ret["err"] or "无法生成签名链接").strip()[:300]}
    return {"suc": True, "url": url}


async def put_bytes(remote: str, data: bytes) -> Dict[str, Any]:
    if len(data) > MAX_B64:
        return {"suc": False, "msg": f"文件过大（>{MAX_B64 // 1024 // 1024}MB），请直接在工人上用 rclone"}
    suffix = os.path.splitext(remote)[1] or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(data)
        tmp.close()
        ret = await rclone(["copyto", tmp.name, remote], timeout=600)
        if not ret["ok"]:
            return {"suc": False, "msg": (ret["err"] or ret["out"]).strip()[:400]}
        return {"suc": True}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


async def cat_bytes(remote: str) -> Dict[str, Any]:
    """按字节读取远端文件（不能走 rclone()，否则二进制会被文本解码破坏）。"""
    if not available():
        return {"suc": False, "msg": "未安装 rclone"}
    proc = await asyncio.create_subprocess_exec(
        rclone_path(),
        "cat",
        remote,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:  # pragma: no cover
        proc.kill()
        return {"suc": False, "msg": "读取超时"}
    if proc.returncode != 0:
        msg = (err or b"").decode("utf-8", "replace").strip()[:400]
        return {"suc": False, "msg": msg or "读取失败"}
    if len(out or b"") > MAX_B64:
        return {"suc": False, "msg": "文件过大，请用 storage_link 直链下载"}
    return {"suc": True, "data": out or b""}


async def get_bytes(remote: str) -> Dict[str, Any]:
    return await cat_bytes(remote)


async def delete(remote: str) -> Dict[str, Any]:
    ret = await rclone(["deletefile", remote], timeout=120)
    if not ret["ok"]:
        return {"suc": False, "msg": (ret["err"] or ret["out"]).strip()[:300]}
    return {"suc": True}


async def size(remote: str) -> Dict[str, Any]:
    ret = await rclone(["size", remote, "--json"], timeout=120)
    if not ret["ok"]:
        return {"suc": False, "bytes": 0, "count": 0}
    import json

    try:
        data = json.loads(ret["out"] or "{}")
    except ValueError:
        return {"suc": False, "bytes": 0, "count": 0}
    return {"suc": True, "bytes": data.get("bytes", 0), "count": data.get("count", 0)}
