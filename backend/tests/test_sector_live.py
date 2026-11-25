"""盘中板块实时采样 (sector_live) 契约测试。

覆盖: 快照 → 板块等权聚合 → 板块切换响应; 非交易时段不落盘;
脏点 (±30% 物理边界) 剔除; 当日分钟分区存在时优先于实时快照。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import rps_rotation, sector_live, sector_rotation
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

DAY = "2026-09-11"
PREV = "2026-09-10"
SYMS_A = ["000001.SZ", "000002.SZ"]
SYMS_B = ["000003.SZ", "000004.SZ"]


def _reset_caches() -> None:
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    sector_rotation.invalidate_cache()
    sector_live._last_write.clear()


def _write_concept_ext(data_dir: Path) -> None:
    config = ExtConfig(
        id="ext_gn",
        label="测试概念",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("所属概念", "string", "所属概念")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": SYMS_A + SYMS_B,
            "所属概念": ["A题材", "A题材", "B题材", "B题材"],
        }),
        config,
        data_dir,
    )


def _snapshot(pcts: list[float]) -> tuple[pl.DataFrame, pl.DataFrame]:
    daily = pl.DataFrame({
        "symbol": SYMS_A + SYMS_B,
        "amount": [1e8, 2e8, 3e8, 4e8],
    })
    extra = pl.DataFrame({"symbol": SYMS_A + SYMS_B, "change_pct": pcts})
    return daily, extra


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch):
    _reset_caches()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_concept_ext(data_dir)
    monkeypatch.setattr(sector_live, "cn_today", lambda: date.fromisoformat(DAY))
    monkeypatch.setattr(sector_rotation, "cn_today", lambda: date.fromisoformat(DAY))
    yield SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    _reset_caches()


def test_snapshot_aggregates_and_builds_today_curve(repo):
    """实时快照按板块等权聚合, 板块切换接口直接返回当天盘中曲线。"""
    daily, extra = _snapshot([0.02, -0.01, 0.05, 0.03])
    assert sector_live.record_snapshot(repo, daily, extra, now=datetime(2026, 9, 11, 10, 30)) is True

    live = sector_live.load_day(repo.store.data_dir, "concept")
    assert live is not None
    assert live["date"] == DAY
    assert live["as_of"] == "10:30"
    by_member = {row["_member"]: row for row in live["by_sector"].iter_rows(named=True)}
    assert by_member["A题材"]["_spct"] == pytest.approx(0.005, abs=1e-9)  # (0.02 - 0.01) / 2
    assert by_member["B题材"]["_spct"] == pytest.approx(0.04, abs=1e-9)   # (0.05 + 0.03) / 2
    assert by_member["A题材"]["_n"] == 2
    assert by_member["A题材"]["_amt"] == pytest.approx(3e8)

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    assert result["date"] == DAY
    assert result["basis"] == "prev_close"
    assert result["as_of"] == "10:30"
    sectors = {item["name"]: item for item in result["sectors"]}
    assert sectors["A题材"]["pct_now"] == pytest.approx(0.005, abs=1e-9)
    assert sectors["B题材"]["pct_now"] == pytest.approx(0.04, abs=1e-9)
    assert result["series"]["buckets"] == ["10:30"]
    universe = {item["name"]: item for item in result["universe"]}
    assert universe["B题材"]["activity"] == pytest.approx(7e8)


def test_snapshot_minutes_roll_into_requested_bucket(repo):
    """多分钟采样按接口的桶粒度聚合: 5 分钟桶内按成分股数加权平均。"""
    daily, extra = _snapshot([0.02, 0.02, 0.02, 0.02])
    assert sector_live.record_snapshot(repo, daily, extra, now=datetime(2026, 9, 11, 10, 30)) is True
    daily, extra = _snapshot([0.04, 0.04, 0.04, 0.04])
    assert sector_live.record_snapshot(repo, daily, extra, now=datetime(2026, 9, 11, 10, 31)) is True

    live = sector_live.load_day(repo.store.data_dir, "concept", bucket_minutes=5)
    assert live["by_sector"].height == 2  # A/B 两个板块各一行
    by_member = {row["_member"]: row for row in live["by_sector"].iter_rows(named=True)}
    assert by_member["A题材"]["_spct"] == pytest.approx(0.03, abs=1e-9)
    assert by_member["A题材"]["_bucket"] == datetime(2026, 9, 11, 10, 30)

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["series"]["buckets"] == ["10:30"]


def test_snapshot_ignored_outside_session(repo):
    """午休/盘后不采样: 没有当天曲线时仍回退到分钟分区 (此处无分区 → no_data)。"""
    daily, extra = _snapshot([0.02, -0.01, 0.05, 0.03])
    assert sector_live.record_snapshot(repo, daily, extra, now=datetime(2026, 9, 11, 12, 0)) is False
    assert sector_live.load_day(repo.store.data_dir, "concept") is None
    result = sector_rotation.build_sector_rotation(repo, kind="concept")
    assert result["status"] == "no_data"
    assert result["reason"] == "minute_missing"


def test_snapshot_drops_out_of_bound_pct(repo):
    """+50% 超出 A 股物理边界, 视为脏点剔除, 不把板块均值带飞。"""
    daily, extra = _snapshot([0.50, 0.10, 0.01, 0.02])
    assert sector_live.record_snapshot(repo, daily, extra, now=datetime(2026, 9, 11, 10, 30)) is True
    live = sector_live.load_day(repo.store.data_dir, "concept")
    by_member = {row["_member"]: row for row in live["by_sector"].iter_rows(named=True)}
    assert by_member["A题材"]["_spct"] == pytest.approx(0.10, abs=1e-9)
    assert by_member["A题材"]["_n"] == 1


def test_today_minute_partition_takes_precedence(repo):
    """当日分钟分区存在时用分钟路径 (从 9:30 起完整), 不被实时快照覆盖。"""
    daily, extra = _snapshot([0.02, 0.02, 0.02, 0.02])
    assert sector_live.record_snapshot(repo, daily, extra, now=datetime(2026, 9, 11, 10, 30)) is True

    out = repo.store.data_dir / "kline_daily" / f"date={PREV}"
    out.mkdir(parents=True)
    pl.DataFrame({"symbol": SYMS_A + SYMS_B, "close": [100.0] * 4}).write_parquet(out / "part.parquet")
    rows = []
    for ts in (datetime(2026, 9, 11, 9, 35), datetime(2026, 9, 11, 10, 30)):
        for sym in SYMS_A:
            rows.append({"symbol": sym, "datetime": ts, "close": 110.0})  # +10%
        for sym in SYMS_B:
            rows.append({"symbol": sym, "datetime": ts, "close": 100.0})
    minute_out = repo.store.data_dir / "kline_minute" / f"date={DAY}"
    minute_out.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(minute_out / "part.parquet")

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    sectors = {item["name"]: item for item in result["sectors"]}
    assert sectors["A题材"]["pct_now"] == pytest.approx(0.10, abs=1e-6)
    assert result["series"]["buckets"] == ["09:35", "10:30"]
