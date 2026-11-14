# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标 API：在进程内跑一个假工人，验证命令下发与屏幕回读。"""

import asyncio
import json
import pathlib

import pytest
import sqless

import besh.beacon as B
from besh.util import DEFAULT_PORT
from tests.fake_worker import FakeShell


class FakeRemote:
    """进程内的假工人：长轮询 + 执行 + 回报。"""

    def __init__(self, worker: str = "w1"):
        self.worker = worker
        self.shell = FakeShell()
        self.task = None
        self.killed = False

    async def loop(self):
        while True:
            ret = await B.poll(self.worker, json.dumps(self.shell.meta()), 2)
            if ret.get("kill"):
                self.killed = True
                return
            for cmd in ret.get("data") or []:
                output, code, status = self.shell.handle(cmd)
                B.report(cmd["id"], output, code, status)


@pytest.fixture()
def bound(tmp_path):
    dbs = sqless.DBS(str(tmp_path / "db"))
    B.STATE.update({"cfg": {}, "dbs": dbs, "store": None, "app": ""})
    B._WAKE.clear()
    B._RESULT.clear()
    B._REQUEUE_AT.clear()
    B._TOUCH_AT.clear()
    B._TOUCH_META.clear()
    B._PRUNE_AT = 0.0
    B.bind({"path_base_db": str(tmp_path / "db")}, dbs)
    return dbs


@pytest.mark.asyncio
async def test_send_keys_then_capture(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        ret = await B.send_keys("w1", json.dumps(["echo hi", "Enter"]), window="main")
        assert ret["suc"] is True
        cap = await B.capture_pane("w1", "main", lines=50, escapes=False, refresh=True)
        assert cap["suc"] is True
        assert "echo hi" in cap["content"]
        assert cap["live"] is True
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_run_returns_output_and_exit_code(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        ret = await B.run("w1", "uname -a", window="main", timeout=10)
        assert ret["suc"] is True, ret
        assert ret["exit_code"] == 0
        assert "ran: uname -a" in ret["output"]
        # 哨兵与提示符都不能留在输出里
        assert "__BESH_" not in ret["output"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_type_text(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await B.type_text("w1", "hello world", window="main")
        cap = await B.capture_pane("w1", "main", refresh=True)
        assert "hello world" in cap["content"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_windows_and_new_window(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        ret = await B.list_windows("w1", refresh=True, timeout=5)
        assert ret["live"] is True
        assert [w["name"] for w in ret["data"]] == ["main"]

        await B.new_window("w1", "gpu")
        ret = await B.list_windows("w1", refresh=True, timeout=5)
        assert "gpu" in [w["name"] for w in ret["data"]]

        await B.kill_window("w1", "gpu")
        ret = await B.list_windows("w1", refresh=True, timeout=5)
        assert "gpu" not in [w["name"] for w in ret["data"]]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_panes_cache_and_offline_fallback(bound):
    B.panes("w1", json.dumps({"main": "$ cached"}))
    ret = await B.capture_pane("w1", "main", refresh=False)
    assert ret["content"] == "$ cached"
    assert ret["live"] is False


@pytest.mark.asyncio
async def test_list_workers_online(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await asyncio.sleep(0.3)
        ret = B.list_workers()
        assert ret["data"][0]["worker"] == "w1"
        assert ret["data"][0]["online"] is True
        assert ret["data"][0]["windows"] == ["main"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_kill_worker_flag(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await asyncio.sleep(0.2)
        B.kill_worker("w1")
        await asyncio.wait_for(task, timeout=5)
        assert remote.killed is True
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_capture_timeout_falls_back(bound):
    # 没有工人时不应该抛异常，而是给出 suc=False
    ret = await B.capture_pane("nobody", "main", refresh=True)
    assert ret["suc"] is False
    assert "没有响应" in ret["msg"]


def test_extract_run():
    content = "\n".join(
        [
            "$ ls",
            "file1",
            '$ echo hi; echo "__BESH_ab12__$?"',
            "hi",
            "__BESH_ab12__0",
            "$ ",
        ]
    )
    code, body = B._extract_run(content, "__BESH_ab12__")
    assert code == 0
    assert body == "hi"


def test_pane_idle_detects_prompt():
    assert B._pane_idle("$ ls\nfile1\n$ ") is True
    assert B._pane_idle("data %") is True
    assert B._pane_idle("PS C:\\Users> ") is True
    assert B._pane_idle("$ python train.py") is False
    assert B._pane_idle("running...") is False
    assert B._pane_idle("") is False
    assert B._pane_idle("\n\n") is False


@pytest.mark.asyncio
async def test_wait_idle_returns_when_prompt_back(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await B.type_text("w1", "hello world", window="main")
        ret = await B.wait_idle("w1", "main", timeout=15)
        assert ret["suc"] is True, ret
        assert "ran: hello world" in ret["content"]
        assert ret["content"].splitlines()[-1].strip() == "$"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_wait_idle_no_worker_fails(bound):
    ret = await B.wait_idle("nobody", "main", timeout=3)
    assert ret["suc"] is False


def test_extract_run_nonzero_and_multiline():
    content = "\n".join(
        [
            '$ make; echo "__BESH_ff__$?"',
            "line1",
            "line2",
            "__BESH_ff__2",
        ]
    )
    code, body = B._extract_run(content, "__BESH_ff__")
    assert code == 2
    assert body == "line1\nline2"


def test_extract_run_missing_marker():
    code, body = B._extract_run("$ ls\nfile\n$ ", "__BESH_none__")
    assert code is None
    assert body == ""


def test_hello(bound):
    ret = B.hello()
    assert ret["suc"] is True
    assert ret["name"] == "besh beacon"


def test_parse_keys():
    assert B._parse_keys('["ls -l", "Enter"]') == ["ls -l", "Enter"]
    assert B._parse_keys("ls -l") == ["ls -l"]
    assert B._parse_keys("") == []





# ---------------- 入口程序即配置文件 ----------------

def test_make_cfg_entry_literals_win(tmp_path, monkeypatch):
    """入口程序传的 host/port 优先于环境变量。"""
    monkeypatch.setenv("besh_host", "10.0.0.1")
    monkeypatch.setenv("besh_port", "19090")
    cfg = B.make_cfg(tmp_path / "besh_beacon.py", host="127.0.0.1", port=18080)
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 18080


def test_make_cfg_env_fallback(tmp_path, monkeypatch):
    """入口程序没写值时用环境变量，再不行用默认值。"""
    monkeypatch.delenv("besh_host", raising=False)
    monkeypatch.delenv("besh_port", raising=False)
    cfg = B.make_cfg(tmp_path / "besh_beacon.py")
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == DEFAULT_PORT
    monkeypatch.setenv("besh_port", "19090")
    assert B.make_cfg(tmp_path / "besh_beacon.py")["port"] == 19090


def test_write_app_bakes_cfg(tmp_path, monkeypatch):
    """生成的入口程序顶部带 CFG_HOST/CFG_PORT，改文件即改配置。"""
    from besh import serve as S

    monkeypatch.delenv("besh_host", raising=False)
    monkeypatch.delenv("besh_port", raising=False)
    path, created = S.write_app(str(tmp_path), host="127.0.0.1", port=18080)
    assert created is True
    text = pathlib.Path(path).read_text(encoding="utf-8")
    assert 'CFG_HOST = "127.0.0.1"' in text
    assert "CFG_PORT = 18080" in text
    assert S.read_cfg(path) == ("127.0.0.1", 18080)

    # 文件已存在时不再生成（保护用户手改的配置）
    pathlib.Path(path).write_text(text.replace("CFG_PORT = 18080", "CFG_PORT = 18888"), encoding="utf-8")
    _, created2 = S.write_app(str(tmp_path), host="127.0.0.1", port=17070)
    assert created2 is False
    assert "CFG_PORT = 18888" in pathlib.Path(path).read_text(encoding="utf-8")


def test_patch_cfg_updates_values(tmp_path, monkeypatch):
    """serve --port 对已存在的入口程序原地更新配置。"""
    from besh import serve as S

    monkeypatch.delenv("besh_host", raising=False)
    monkeypatch.delenv("besh_port", raising=False)
    path, _ = S.write_app(str(tmp_path), host="0.0.0.0", port=12242)
    S.patch_cfg(path, host="127.0.0.1", port=19090)
    assert S.read_cfg(path) == ("127.0.0.1", 19090)
    # 其余内容未被破坏
    assert "beacon.bind(cfg, dbs)" in pathlib.Path(path).read_text(encoding="utf-8")


# ---------------- .env：环境变量集中配置 ----------------

def test_ensure_secret_writes_env(tmp_path, monkeypatch):
    """首次运行生成 .env：只有 besh_secret，host/port/url 不出现在 .env 里。"""
    from besh import serve as S

    monkeypatch.delenv("besh_secret", raising=False)
    value = S.ensure_secret(str(tmp_path))
    assert value  # 返回的是密钥
    text = (pathlib.Path(str(tmp_path)) / ".env").read_text(encoding="utf-8")
    assert "besh_secret=" + value in text
    assert "besh_host" not in text  # host 在 besh_beacon.py 里
    assert "besh_port" not in text
    assert "besh_url" not in text
    assert not (pathlib.Path(str(tmp_path)) / ".besh_secret").exists()  # 不再生成 .besh_secret


def test_worker_accepts_url_positional():
    """besh worker ID URL：地址直接写在命令行，不依赖环境变量。"""
    from besh.cli import build_parser

    args = build_parser().parse_args(["worker", "w1", "http://1.2.3.4:12242?secret=abc"])
    assert args.worker_id == "w1"
    assert args.beacon_url == "http://1.2.3.4:12242?secret=abc"
    args2 = build_parser().parse_args(["worker", "w1"])
    assert args2.beacon_url is None


def test_ensure_secret_reuses_existing_env(tmp_path, monkeypatch):
    """.env 已有 besh_secret 时直接复用，不重新生成。"""
    from besh import serve as S

    monkeypatch.delenv("besh_secret", raising=False)
    env = pathlib.Path(str(tmp_path)) / ".env"
    env.write_text("besh_secret=my-own-secret\n", encoding="utf-8")
    value = S.ensure_secret(str(tmp_path))
    assert value == "my-own-secret"
    assert env.read_text(encoding="utf-8").count("besh_secret=") == 1


def test_ensure_secret_appends_missing_key(tmp_path, monkeypatch):
    """.env 存在但没有 besh_secret 时追加而不是覆盖整个文件。"""
    from besh import serve as S

    monkeypatch.delenv("besh_secret", raising=False)
    env = pathlib.Path(str(tmp_path)) / ".env"
    env.write_text("# 用户自己的注释\nbesh_url=http://x:1?secret=y\n", encoding="utf-8")
    value = S.ensure_secret(str(tmp_path))
    assert value
    text = env.read_text(encoding="utf-8")
    assert "# 用户自己的注释" in text  # 原内容保留
    assert "besh_secret=" in text


@pytest.mark.asyncio
async def test_run_reports_no_response(bound, monkeypatch):
    """工人没响应时给出明确错误，而不是含混的超时。"""
    async def fake_request(*args, **kwargs):
        return None

    monkeypatch.setattr(B, "_request", fake_request)
    ret = await B.run("w1", "ls", window="main", timeout=1)
    assert ret["suc"] is False
    assert "没有响应" in ret["msg"]


# ---------------- MCP inputSchema：标量类型不能被标成 object ----------------

def test_mcp_schema_scalar_types(bound):
    """回归：注解若变成字符串（from __future__ import annotations），
    sqless 会把所有参数标成 object，导致 MCP 客户端类型校验直接拒绝。"""
    tools = {t["name"]: t for t in __import__("sqless").server.tools}

    run_props = tools["run"]["inputSchema"]["properties"]
    assert run_props["worker"]["type"] == "string"
    assert run_props["command"]["type"] == "string"
    assert run_props["timeout"]["type"] == "integer"

    win_props = tools["list_windows"]["inputSchema"]["properties"]
    assert win_props["refresh"]["type"] == "boolean"
    assert win_props["timeout"]["type"] == "integer"

    cap_props = tools["capture_pane"]["inputSchema"]["properties"]
    assert cap_props["lines"]["type"] == "integer"
    assert cap_props["escapes"]["type"] == "boolean"

    # 全局兜底：不应再有参数退化成 object
    bad = [
        f"{name}.{p}"
        for name, tool in tools.items()
        for p, schema in tool["inputSchema"]["properties"].items()
        if schema["type"] == "object"
    ]
    assert not bad, f"这些参数被误标成 object：{bad}"


@pytest.mark.asyncio
async def test_touch_is_throttled(bound, monkeypatch):
    """心跳写库要节流：meta 没变就不写，变了立即写。"""
    calls = []
    real_touch = B.store().touch
    monkeypatch.setattr(B.store(), "touch", lambda w, m=None: calls.append((w, m)) or real_touch(w, m))

    meta = {"host": "h", "windows": [{"name": "main", "active": True}]}
    B._touch("w1", meta)
    B._touch("w1", dict(meta))   # 同内容，节流期内不写
    assert len(calls) == 1

    meta2 = dict(meta, active="gpu")  # meta 变化 → 立即写
    B._touch("w1", meta2)
    assert len(calls) == 2


def test_panes_accepts_compressed_payload(bound):
    """屏幕快照支持压缩负载（省日志和磁盘写入）。"""
    import base64 as _b64
    import zlib as _zlib

    payload = "z:" + _b64.b64encode(
        _zlib.compress(json.dumps({"main": "$ hi"}).encode())
    ).decode()
    ret = B.panes("w1", payload)
    assert ret["suc"] is True
    assert B.store().get_pane("w1", "main")["content"] == "$ hi"

    # 纯 JSON 仍然兼容（老工人）
    assert B.panes("w1", json.dumps({"main": "$ old"}))["suc"] is True
