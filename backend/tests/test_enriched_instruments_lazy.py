"""信号增删触发 clear_cache 后, enriched 重算必须重新加载维表。

回归: _refresh_enriched_impl 旧实现只在 _instruments_cache 已有值时使用维表;
clear_cache (信号保存/资源导入的失效路径) 会把维表置空, 重算时 instruments
为空 -> compute_enriched_history_window 跳过 compute_limit_signals, 缓存缺
limit_up_price / consecutive_limit_* 等列, 直到下次全量刷新。
"""
from datetime import date, timedelta

import polars as pl

from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 20)


def _enriched_rows() -> pl.DataFrame:
    rows = []
    for i in range(10):
        d = DAY - timedelta(days=9 - i)
        close = 10.0 + i * 0.1
        rows.append({
            "symbol": "600000.SH", "date": d,
            "open": close, "high": close, "low": close, "close": close,
            "volume": 1000.0, "amount": close * 1000,
            "raw_close": close, "raw_high": close, "raw_low": close,
        })
    return pl.DataFrame(rows)


def test_refresh_enriched_reloads_instruments_after_clear_cache(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    try:
        repo.append_enriched(_enriched_rows())
        inst_dir = tmp_path / "instruments"
        inst_dir.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["600000.SH"],
            "name": ["测试股份"],
            "as_of": [DAY],
            "limit_up": [20.0],
            "limit_down": [8.0],
            "listing_date": [date(2010, 1, 1)],
            "float_shares": [1_000_000.0],
            "total_shares": [1_000_000.0],
        }).write_parquet(inst_dir / "part.parquet")
        repo.refresh_view("kline_enriched", repo._enriched_glob)

        repo.clear_cache()
        assert repo._instruments_cache is None

        repo._refresh_enriched()

        cache = repo._enriched_cache
        assert cache is not None and not cache.is_empty()
        for col in ("limit_up_price", "limit_down_price", "consecutive_limit_ups"):
            assert col in cache.columns, f"维表未加载导致缺列: {col}"
    finally:
        repo.db.close()
