"""fuyao client 429/5xx 退避重试契约。

背景: 首次全市场日K同步在 41% 处因单标的接口一次 HTTP 429 触发
ProviderRequestError, 同步层按设计丢弃整轮暂存帧, 导致 daily 分区归零;
每次重跑都在同一位置再撞, 永远跑不完。本测试锁定三点:
  1. 429 会按次数重试, 恢复后请求成功;
  2. Retry-After 合法时按其秒数退避;
  3. 实时快照路径 (retries=0) 不重试, 保持"本轮空数据优先于卡住轮询"。
"""

from __future__ import annotations

import pytest

from app.plugins.fuyao import client as fc


def _resp(status_code: int, payload: dict | None = None, headers: dict | None = None):
    class _R:
        pass

    r = _R()
    r.status_code = status_code
    r.headers = headers or {}
    r.json = lambda: payload or {"code": 0, "message": "OK", "data": {"item": []}}
    return r


def _patch_http(monkeypatch, responses: list):
    """按顺序返回预设响应; 记录每次调用。"""
    calls: list[str] = []

    def _get(self, path, params=None):
        calls.append(path)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    sleeps: list[float] = []
    monkeypatch.setattr(fc.httpx.Client, "get", _get)
    monkeypatch.setattr(fc.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_429_retries_then_succeeds(monkeypatch):
    calls, sleeps = _patch_http(
        monkeypatch,
        [_resp(429), _resp(429), _resp(200, {"code": 0, "data": {"item": [{"thscode": "000001.SZ"}]}})],
    )
    c = fc.FuyaoClient(api_key="k")
    data = c._get("/api/a-share/prices/historical", {})
    assert len(calls) == 3
    assert sleeps == [fc._RETRY_WAIT_S[0], fc._RETRY_WAIT_S[1]]
    assert data["item"][0]["thscode"] == "000001.SZ"


def test_429_exhausted_still_raises(monkeypatch):
    calls, _ = _patch_http(monkeypatch, [_resp(429)])
    c = fc.FuyaoClient(api_key="k")
    with pytest.raises(fc.FuyaoError, match="429"):
        c._get("/api/a-share/prices/historical", {})
    assert len(calls) == fc._DEFAULT_RETRIES + 1


def test_retry_after_header_wins(monkeypatch):
    _, sleeps = _patch_http(monkeypatch, [_resp(429, headers={"retry-after": "p3"}), _resp(429, headers={"retry-after": "7"}), _resp(200)])
    c = fc.FuyaoClient(api_key="k")
    c._get("/api/a-share/prices/historical", {})
    # 非数字 Retry-After 回退默认序列; 数字 7 直接采用
    assert sleeps == [fc._RETRY_WAIT_S[0], 7.0]


def test_realtime_snapshot_does_not_retry(monkeypatch):
    calls, sleeps = _patch_http(monkeypatch, [_resp(429)])
    c = fc.FuyaoClient(api_key="k")
    with pytest.raises(fc.FuyaoError, match="429"):
        c.snapshot_page()
    assert len(calls) == 1 and sleeps == []


def test_server_error_retries_too(monkeypatch):
    calls, _ = _patch_http(monkeypatch, [_resp(503), _resp(200)])
    c = fc.FuyaoClient(api_key="k")
    c._get("/api/a-share/prices/historical", {})
    assert len(calls) == 2
