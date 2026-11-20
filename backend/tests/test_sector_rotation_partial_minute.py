"""板块轮动: 盘中残缺分钟分区不能当全市场行情用。

背景 (2026-11-20 实测): 盘中分钟不自动落盘, 只有在面板上打开个股分时
(live=1) 才把那一只股票写进当天分区。当天分区因此可能只有 1 只股票,
板块轮动聚合后只剩它所属的 1 个板块有数据 —— 卡片从多根线变成一根线。

规则: 最新分区标的数不足前一盘的 50% 时视为残缺, 回退到前一盘;
所有候选都残缺时退回最新 (fail-open, 与旧行为一致), 不返回 None。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from app.services.sector_rotation import _latest_minute_partition


def _write_partition(minute_dir: Path, day: str, n_symbols: int) -> None:
    rows = [
        {"symbol": f"{i:06d}.SZ", "datetime": datetime.fromisoformat(f"{day}T09:35:00"),
         "close": 10.0, "amount": 1.0e6}
        for i in range(n_symbols)
    ]
    out = minute_dir / f"date={day}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(out)


def test_partial_latest_falls_back_to_previous_partition(tmp_path) -> None:
    """今天只有 1 只票 (用户看了一眼分时), 昨天是全市场 → 用昨天。"""
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, "2026-11-19", 500)
    _write_partition(minute_dir, "2026-11-20", 1)

    assert _latest_minute_partition(minute_dir) == "2026-11-19"


def test_complete_latest_partition_is_used(tmp_path) -> None:
    """今天全量落盘后正常切到今天。"""
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, "2026-11-19", 500)
    _write_partition(minute_dir, "2026-11-20", 480)

    assert _latest_minute_partition(minute_dir) == "2026-11-20"


def test_all_partitions_partial_returns_latest(tmp_path) -> None:
    """全是小分区 (例如只有部分标的的装法) 时退回最新, 不返回 None。"""
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, "2026-11-19", 1)
    _write_partition(minute_dir, "2026-11-20", 1)

    assert _latest_minute_partition(minute_dir) == "2026-11-20"


def test_single_partition_is_used(tmp_path) -> None:
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, "2026-11-20", 3)

    assert _latest_minute_partition(minute_dir) == "2026-11-20"


def test_missing_dir_returns_none(tmp_path) -> None:
    assert _latest_minute_partition(tmp_path / "nope") is None


def test_lookback_skips_multiple_partial_days(tmp_path) -> None:
    """连续两天残缺 → 继续往前找到完整的那天。"""
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, "2026-11-18", 500)
    _write_partition(minute_dir, "2026-11-19", 2)
    _write_partition(minute_dir, "2026-11-20", 1)

    assert _latest_minute_partition(minute_dir) == "2026-11-18"
