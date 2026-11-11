# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""命令行 / 工人访问信标的 HTTP 客户端（只依赖标准库）。"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .util import DEFAULT_PORT, BeshError, getenv, parse_beacon_url


class Beacon:
    """一个信标连接。

    地址可以是 ``http://HOST:12242?secret=PASSWORD``，也可以是纯 ``HOST:12242``，
    密钥会依次从 参数 / 环境变量 ``besh_secret`` 里取。
    """

    def __init__(self, url: Optional[str] = None, secret: Optional[str] = None, timeout: int = 120):
        base, url_secret = parse_beacon_url(url or getenv("besh_url") or f"http://127.0.0.1:{DEFAULT_PORT}")
        self.base = base or f"http://127.0.0.1:{DEFAULT_PORT}"
        self.secret = (secret or url_secret or getenv("besh_secret") or "").strip()
        self.timeout = timeout

    # ------------------------------------------------------------------ 底层

    def _post(self, path: str, payload: dict, timeout: Optional[int] = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.secret}",
            },
            method="POST",
        )
        return self._open(req, timeout)

    def _get(self, path: str, timeout: Optional[int] = None) -> Any:
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self.secret}"},
            method="GET",
        )
        return self._open(req, timeout)

    def _open(self, req: urllib.request.Request, timeout: Optional[int]) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover
                pass
            if exc.code in (401, 403):
                raise BeshError(
                    f"信标拒绝了请求（HTTP {exc.code}）：请检查 besh_secret 是否正确"
                ) from None
            text = body.decode("utf-8", "replace").strip()
            raise BeshError(f"信标返回 HTTP {exc.code}: {text[:200] or exc.reason}") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise BeshError(f"连接不上信标 {self.base}（{reason}）") from None
        except socket.timeout:
            raise BeshError(f"信标 {self.base} 响应超时") from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", "replace")

    # ------------------------------------------------------------------ 接口

    def call(self, func: str, _timeout: Optional[int] = None, **kwargs: Any) -> Any:
        """调用信标上的一个函数（同 MCP 工具）。

        注意：HTTP 超时参数叫 ``_timeout``，避免和 API 函数自己的 ``timeout``
        参数（如 run/list_windows）撞名。
        """
        payload = {"f": func}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return self._post("/api", payload, timeout=_timeout)

    def get(self, func: str, _timeout: Optional[int] = None, **params: Any) -> Any:
        """GET 风格调用，方便浏览器/curl 调试。"""
        clean = {k: ("" if v is None else v) for k, v in params.items()}
        query = urllib.parse.urlencode(clean)
        path = f"/api/{urllib.parse.quote(func)}"
        if query:
            path += f"?{query}"
        return self._get(path, timeout=_timeout)

    def poll(self, worker: str, meta: Optional[dict] = None, wait: int = 20) -> dict:
        payload = {
            "f": "poll",
            "worker": worker,
            "meta": json.dumps(meta or {}, ensure_ascii=False),
            "wait": int(wait),
        }
        ret = self._post("/api", payload, timeout=int(wait) + 30)
        if not isinstance(ret, dict):
            raise BeshError(f"信标返回了非预期内容: {ret!r}")
        return ret

    def mcp_url(self) -> str:
        return f"{self.base}/mcp"

    def __str__(self) -> str:
        return self.base
