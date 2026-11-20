"""depth sealed 定版兜底: 收盘后五档成交量清零时不能丢当天封单。

背景 (2026-11-20 实测): 15:02 定版任务会再拉一次五档, 但腾讯 qt.gtimg.cn
收盘后把五档成交量全部清零 (只剩买一价)。旧实现要么拉到空快照直接放弃,
要么用全 0 快照覆盖缓存后落盘 —— 两种都会让当天封单丢失/变成 0。

规则:
- 新快照没有任何挂单量时, 保持盘中最后一盘, 不用 0 覆盖;
- 定版 (persist=True) 时把盘中最后一盘落盘;
- 拉取直接为空时同样回退到缓存落盘。
"""
from __future__ import annotations

import threading
from datetime import date

from app.services.depth_service import DepthService, _has_volume

DAY = date(2026, 11, 20)


def _svc(cache: dict | None = None, day: date = DAY) -> DepthService:
    svc = object.__new__(DepthService)
    svc._lock = threading.Lock()
    svc._fetch_lock = threading.Lock()
    svc._sealed_cache = dict(cache or {})
    svc._sealed_date = day
    svc._sealed_ready = False
    svc._sealed_fetched_ts = 0.0
    svc._sealed_fetched_at = 0.0
    svc._notify_depth_updated = lambda n: None
    svc.persisted = []  # type: ignore[attr-defined]
    svc._persist = lambda d: svc.persisted.append(d)  # type: ignore[method-assign]
    return svc


def _zero_snapshot(sym: str = "000001.SZ") -> dict:
    return {sym: {"ask_volumes": [0, 0, 0, 0, 0], "bid_volumes": [0, 0, 0, 0, 0],
                  "timestamp": 1_700_000_000_000}}


def _good_snapshot(sym: str = "000001.SZ", bid: int = 12345) -> dict:
    return {sym: {"ask_volumes": [0, 0, 0, 0, 0], "bid_volumes": [bid, 0, 0, 0, 0],
                  "timestamp": 1_700_000_000_000}}


def _good_cache() -> dict:
    return {"000001.SZ": {"sealed_up": True, "sealed_down": None, "ask1_vol": 0,
                          "bid1_vol": 50000, "status": "limit_up", "fetched_ts": 1.0}}


def test_has_volume_detects_cleared_snapshot() -> None:
    assert _has_volume(_good_cache()) is True
    assert _has_volume({"x": {"bid1_vol": 0, "ask1_vol": 0}}) is False


def test_zero_snapshot_keeps_previous_cache_and_persists_it() -> None:
    svc = _svc(_good_cache())
    svc._seal_locked(_zero_snapshot(), ["000001.SZ"], [], DAY, persist=True)

    assert svc._sealed_cache["000001.SZ"]["bid1_vol"] == 50000
    assert svc.persisted == [DAY]


def test_zero_snapshot_without_previous_volume_is_skipped() -> None:
    svc = _svc()
    svc._seal_locked(_zero_snapshot(), ["000001.SZ"], [], DAY, persist=True)

    assert svc._sealed_cache == {}
    assert svc.persisted == []


def test_good_snapshot_replaces_cache_normally() -> None:
    svc = _svc(_good_cache())
    svc._seal_locked(_good_snapshot(bid=777), ["000001.SZ"], [], DAY, persist=True)

    assert svc._sealed_cache["000001.SZ"]["bid1_vol"] == 777
    assert svc.persisted == [DAY]


def test_finalize_falls_back_when_fetch_is_empty() -> None:
    svc = _svc(_good_cache())
    svc._fetch_depth_snapshot = lambda: None  # type: ignore[method-assign]

    svc._fetch_and_seal(persist=True)

    assert svc.persisted == [DAY]


def test_finalize_from_cache_skips_when_no_cache() -> None:
    svc = _svc()
    svc._finalize_from_cache()

    assert svc.persisted == []
