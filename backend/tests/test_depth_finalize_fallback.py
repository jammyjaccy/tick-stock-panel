"""depth sealed 定版兜底: 收盘后五档退化时不能丢当天封单。

背景 (2026-11-20 实测): 15:02 定版任务会再拉一次五档, 但腾讯 qt.gtimg.cn
收盘后把五档成交量全部清零 (只剩买一价)。旧实现要么拉到空快照直接放弃,
要么用全 0 快照覆盖缓存后落盘 —— 两种都会让当天封单丢失/变成 0。

背景 (2026-11-23 实测): 15:02 腾讯还会先重置价格字段, 159 只涨跌停只返回
2 只北交所。这 2 只有量, 旧护栏因此放行, 用 2 行覆盖了全天缓存。

规则:
- 新快照没有任何挂单量时, 保持盘中最后一盘, 不用 0 覆盖;
- 新快照覆盖不足当日涨跌停名单一半时同样视为残缺, 保持盘中最后一盘;
- 定版 (persist=True) 时把盘中最后一盘落盘;
- 定版时已有当日有量缓存就直接落缓存, 不再重拉; 拉取为空时回退到缓存落盘。
"""
from __future__ import annotations

import threading
from datetime import date

from app.services import depth_service as depth_mod
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
    svc._repo = None
    svc._last_intraday_snapshot = 0.0
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


def _partial_snapshot(n: int) -> dict:
    return {
        f"{i:06d}.SZ": {"ask_volumes": [0, 0, 0, 0, 0], "bid_volumes": [999, 0, 0, 0, 0],
                        "timestamp": 1_700_000_000_000}
        for i in range(n)
    }


def test_partial_snapshot_keeps_previous_cache_and_persists_it() -> None:
    # 2026-11-23: 159 只涨跌停只要到 2 只, 不能覆盖盘中 157 只的最后一盘
    prev = {
        f"{i:06d}.SZ": {"sealed_up": True, "sealed_down": None, "ask1_vol": 0,
                        "bid1_vol": 50000, "status": "limit_up", "fetched_ts": 1.0}
        for i in range(4)
    }
    svc = _svc(prev)
    expected = [f"{i:06d}.SZ" for i in range(8)]

    svc._seal_locked(_partial_snapshot(2), expected, [], DAY, persist=True)

    assert svc._sealed_cache == prev
    assert svc.persisted == [DAY]


def test_partial_snapshot_without_previous_cache_is_skipped() -> None:
    svc = _svc()
    expected = [f"{i:06d}.SZ" for i in range(8)]

    svc._seal_locked(_partial_snapshot(2), expected, [], DAY, persist=True)

    assert svc._sealed_cache == {}
    assert svc.persisted == []


def test_finalize_prefers_same_day_cache(monkeypatch) -> None:
    monkeypatch.setattr(depth_mod, "cn_today", lambda: DAY)
    svc = _svc(_good_cache())
    svc._has_capability = lambda: True  # type: ignore[method-assign]

    def _boom():
        raise AssertionError("有当日有量缓存时定版不应重拉")

    svc._fetch_depth_snapshot = _boom  # type: ignore[method-assign]

    svc.finalize()

    assert svc.persisted == [DAY]


def test_finalize_fetches_when_cache_is_stale(monkeypatch) -> None:
    monkeypatch.setattr(depth_mod, "cn_today", lambda: DAY)
    svc = _svc(_good_cache(), day=date(2026, 11, 19))
    svc._has_capability = lambda: True  # type: ignore[method-assign]
    called: list[bool] = []
    svc._fetch_and_seal = lambda persist=False: called.append(persist)  # type: ignore[method-assign]

    svc.finalize()

    assert called == [True]
    assert svc.persisted == []


# ---- 断电/重启兜底: 盘中快照落盘 + 定版回退 + 重试 + 完整性检查 ----


class _FakeRepo:
    """只提供 data_dir 的最小 repo 桩。"""

    def __init__(self, data_dir):
        self.store = type("Store", (), {"data_dir": data_dir})()

    def get_enriched_latest(self):  # 默认无 enriched 数据
        import polars as pl

        return pl.DataFrame(), None


def _svc_with_disk(tmp_path, cache=None, day=DAY):
    """带真实持久化的 service (不替换 _persist)。"""
    svc = _svc(cache, day)
    svc._repo = _FakeRepo(tmp_path)
    svc._last_intraday_snapshot = 0.0
    # 还原真实 _persist (覆盖 _svc 里的 lambda)
    svc._persist = DepthService._persist.__get__(svc, DepthService)  # type: ignore[method-assign]
    return svc


def _part_path(tmp_path, day=DAY):
    return tmp_path / "depth5" / f"date={day.isoformat()}" / "part.parquet"


def test_intraday_snapshot_written_and_restored_after_restart(tmp_path, monkeypatch):
    """盘中定期落快照; 模拟断电重启 (内存缓存清空) 后定版仍能落盘。"""
    monkeypatch.setattr(depth_mod, "cn_today", lambda: DAY)

    svc = _svc_with_disk(tmp_path, _good_cache())
    svc._maybe_persist_intraday(DAY)
    snap = svc._snapshot_path(DAY)
    assert snap.exists()

    # 重启: 新实例, 缓存为空, 同一数据目录
    restarted = _svc_with_disk(tmp_path, {})
    restarted._has_capability = lambda: True  # type: ignore[method-assign]
    restarted.finalize()

    assert _part_path(tmp_path).exists()
    import polars as pl

    df = pl.read_parquet(_part_path(tmp_path))
    assert df.height == 1
    assert df["symbol"].to_list() == ["000001.SZ"]
    assert not snap.exists()  # 定版成功后清掉快照


def test_finalize_if_needed_skips_when_today_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(depth_mod, "cn_today", lambda: DAY)
    svc = _svc_with_disk(tmp_path, _good_cache())
    svc._has_capability = lambda: True  # type: ignore[method-assign]
    svc._persist(DAY)

    def _boom():
        raise AssertionError("当天已有定版时重试不应再跑 finalize")

    svc.finalize = _boom  # type: ignore[method-assign]
    svc.finalize_if_needed()  # 不应抛错


def test_close_integrity_check_alerts_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(depth_mod, "cn_today", lambda: DAY)
    svc = _svc_with_disk(tmp_path, {})
    alerted: list[dict] = []
    svc._alert_close_integrity = lambda r: alerted.append(r)  # type: ignore[method-assign]

    result = svc.close_integrity_check()

    assert result["ok"] is False
    assert result["rows"] == 0
    assert alerted == [result]


def test_close_integrity_check_passes_with_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(depth_mod, "cn_today", lambda: DAY)
    svc = _svc_with_disk(tmp_path, _good_cache())
    svc._persist(DAY)
    alerted: list[dict] = []
    svc._alert_close_integrity = lambda r: alerted.append(r)  # type: ignore[method-assign]

    result = svc.close_integrity_check()

    assert result["ok"] is True
    assert result["rows"] == 1
    assert alerted == []
