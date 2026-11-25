"""盘中板块实时采样 — 用实时行情快照逐分钟累积板块涨幅曲线。

背景: 板块切换卡的理想数据源是 ``kline_minute`` 全市场分钟分区, 但该分区只在
盘后同步或 TickFlow 全量分钟能力可用时才存在。只有日线实时快照
(``kline_daily_enriched``) 时, 卡片只能回放上一交易日。这里在实时行情每轮
成功后把全市场快照按板块聚合落盘, 形成当天从服务启动时刻起的分钟级板块曲线,
供 ``sector_rotation`` 在缺少当日分钟数据时使用。

口径:
  - 个股 pct = 实时快照 ``change_pct`` (小数制, 相对昨收), 与分钟路径的
    prev_close 基准一致; 超出 ±30% 物理边界视为脏点剔除 (与 sector_rotation 同口径)
  - 板块 pct = 成分股等权均值; 只统计本轮有行情的成分股 (n_with_bars)
  - 每轮原子改写当前分钟文件, 同一分钟只保留本轮最新快照
  - 只落概念/行业聚合与全市场均值, 不复制个股快照 (约 500 行/分钟)

存储:
  ``data/sector_live/date=YYYY-MM-DD/part-HHMM.parquet``
  读取侧按 glob 汇总当天全部分钟文件; 文件保留不清理 (每天约数百 KB)。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now, cn_today, in_continuous_session
from app.services.fs_utils import atomic_write_parquet
from app.services.rps_rotation import _load_concept_map_df

logger = logging.getLogger(__name__)

_DIRNAME = "sector_live"
_KINDS = ("concept", "industry")
# 全市场均值行的 name 哨兵 (不会与任何板块重名)
_MARKET_NAME = "__market__"
# 与 sector_rotation 完全一致的物理边界: A 股日涨跌停最大 ±30% (北交所)
_PCT_PHYSICAL_BOUND = 0.30
_PCT_BOUND_EPS = 1e-9
# 同一分钟内的最小落盘间隔: 行情 6s 一轮时不必每轮都改写文件
_MIN_WRITE_INTERVAL_S = 10.0

_SCHEMA = {
    "minute": pl.Datetime("us"),
    "kind": pl.Utf8,
    "name": pl.Utf8,
    "pct": pl.Float64,
    "n_with_bars": pl.Int64,
    "amount": pl.Float64,
}

_write_lock = threading.Lock()
_last_write: dict[str, tuple[float, datetime]] = {}

# 行情线程只投递快照, 聚合/落盘放到专用守护线程: 避免在行情回调线程里做
# 成分映射加载与磁盘写 (CONTRIBUTING 5.2)。队列满时丢弃本轮, 不积压。
_queue: queue.Queue = queue.Queue(maxsize=1)
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def submit_snapshot(repo, daily_df: pl.DataFrame | None, quote_extra: pl.DataFrame | None) -> None:
    """异步版 record_snapshot: 供行情轮询线程调用, 立即返回。"""
    if repo is None:
        return
    _ensure_worker()
    try:
        _queue.put_nowait((repo, daily_df, quote_extra))
    except queue.Full:
        return


def _ensure_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_worker, name="sector-live", daemon=True)
        _worker_thread.start()


def _worker() -> None:
    while True:
        repo, daily_df, quote_extra = _queue.get()
        try:
            record_snapshot(repo, daily_df, quote_extra)
        except Exception as exc:  # noqa: BLE001 -- 守护线程不能因单轮失败退出
            logger.warning("sector_live record failed: %s", exc)
        finally:
            _queue.task_done()


def _bare(col: str = "symbol") -> pl.Expr:
    return pl.col(col).cast(pl.String).str.strip_chars().str.split(".").list.first()


def record_snapshot(repo, daily_df: pl.DataFrame | None, quote_extra: pl.DataFrame | None, *, now: datetime | None = None) -> bool:
    """把一轮全市场实时快照聚合后写入当前分钟文件; 不可写时静默返回 False。

    只在连续竞价时段写入。行情线程走 submit_snapshot (守护线程内调用本函数,
    异常被 worker 兜住); 测试可直接调用, 此时异常会向上抛。
    """
    if repo is None or daily_df is None or daily_df.is_empty():
        return False
    now = now or cn_now()
    if now.tzinfo is not None:
        now = now.astimezone(CN_TZ).replace(tzinfo=None)
    if not in_continuous_session(now):
        return False
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is None:
        return False

    minute = now.replace(second=0, microsecond=0)
    queue_key = str(Path(data_dir))
    with _write_lock:
        last_at, last_minute = _last_write.get(queue_key, (0.0, minute))
        if last_minute == minute and time.monotonic() - last_at < _MIN_WRITE_INTERVAL_S:
            return False
        # 先占位: 计算/落盘失败也只退避一个间隔, 不阻塞下一轮
        _last_write[queue_key] = (time.monotonic(), minute)

        quotes, has_amount = _prepare_quotes(daily_df, quote_extra)
        if quotes is None:
            return False
        frames = []
        for kind in _KINDS:
            map_df, _ = _load_concept_map_df(repo, kind)
            if map_df.is_empty():
                continue
            agg = _aggregate_kind(quotes, map_df, kind, has_amount)
            if agg.is_empty():
                continue
            frames.append(
                agg.with_columns(
                    pl.lit(minute).alias("minute"),
                    pl.lit(kind).alias("kind"),
                ).select(list(_SCHEMA))
            )
        if not frames:
            return False
        frames.append(_market_row(quotes, has_amount, minute))
        out = pl.concat(frames, how="vertical")

        path = Path(data_dir) / _DIRNAME / f"date={minute.date().isoformat()}" / f"part-{minute.strftime('%H%M')}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(out, path)
    return True


def load_day(data_dir: Path, kind: str, *, day: str | None = None, bucket_minutes: int = 1) -> dict | None:
    """读取当天已采样的板块曲线; 无数据返回 None。

    bucket_minutes: 与接口的分钟桶粒度一致; 桶内按 ``n_with_bars`` 加权平均
    (每分钟的板块均值权重 = 当分钟有行情的成分股数), 近似"每股每等于 1 票"。
    返回 ``{date, by_sector, market, has_amount, as_of}``:
      - by_sector: ``_bucket/_member/_spct/_n/_amt``, 每桶按涨幅降序
      - market: ``_bucket/_mpct``
    与 minute 路径的中间结构一致, 由 sector_rotation 复用后续计算。
    """
    day = day or cn_today().isoformat()
    root = Path(data_dir) / _DIRNAME / f"date={day}"
    if not root.exists():
        return None
    frames = []
    for path in sorted(root.glob("part-*.parquet")):
        try:
            frames.append(pl.read_parquet(path))
        except Exception as exc:  # noqa: BLE001 -- 单个坏文件不应毁掉当天曲线
            logger.warning("sector_live read failed %s: %s", path, exc)
    if not frames:
        return None
    df = pl.concat(frames, how="vertical")
    if df.is_empty():
        return None
    df = df.with_columns(pl.col("minute").dt.truncate(f"{bucket_minutes}m").alias("_bucket"))

    by_sector = (
        df.filter(pl.col("kind") == kind)
        .group_by(["_bucket", "name"])
        .agg(
            (
                (pl.col("pct") * pl.col("n_with_bars")).sum()
                / pl.col("n_with_bars").sum()
            ).alias("_spct"),
            pl.col("n_with_bars").max().alias("_n"),
            pl.col("amount").sum().alias("_amt"),
        )
        .rename({"name": "_member"})
        .sort(["_bucket", "_spct"], descending=[False, True])
    )
    if by_sector.is_empty():
        return None
    market = (
        df.filter(pl.col("kind") == _MARKET_NAME)
        .group_by("_bucket")
        .agg(pl.col("pct").mean().alias("_mpct"))
        .sort("_bucket")
    )
    if market.is_empty():
        return None
    return {
        "date": day,
        "by_sector": by_sector,
        "market": market,
        "has_amount": bool(by_sector["_amt"].is_not_null().any()),
        "as_of": by_sector["_bucket"].max().strftime("%H:%M"),
    }


def _prepare_quotes(daily_df: pl.DataFrame, quote_extra: pl.DataFrame | None) -> tuple[pl.DataFrame | None, bool]:
    """实时日线 + 补充字段 → 每只有效标的一行 (_bare/_pct/_amt)。"""
    if "symbol" not in daily_df.columns:
        return None, False
    if quote_extra is None or quote_extra.is_empty():
        return None, False
    if "symbol" not in quote_extra.columns or "change_pct" not in quote_extra.columns:
        return None, False

    has_amount = "amount" in daily_df.columns and daily_df["amount"].is_not_null().any()
    amount_expr = (
        pl.col("amount").cast(pl.Float64, strict=False).alias("_amt")
        if "amount" in daily_df.columns
        else pl.lit(None, dtype=pl.Float64).alias("_amt")
    )
    base = daily_df.select([pl.col("symbol").cast(pl.String).alias("symbol"), amount_expr])
    extra = (
        quote_extra.select([
            pl.col("symbol").cast(pl.String).alias("symbol"),
            pl.col("change_pct").cast(pl.Float64, strict=False).alias("_pct"),
        ])
        .drop_nulls(subset=["symbol"])
        .unique(subset=["symbol"], keep="last")
    )
    out = (
        base.join(extra, on="symbol", how="inner")
        .with_columns(_bare().str.to_uppercase().alias("_bare"))
        .drop_nulls(subset=["_bare", "_pct"])
        .filter(
            pl.col("_pct").is_finite()
            & (pl.col("_pct").abs() <= _PCT_PHYSICAL_BOUND + _PCT_BOUND_EPS)
        )
        .unique(subset=["_bare"], keep="last")
    )
    if out.is_empty():
        return None, has_amount
    return out, has_amount


def _aggregate_kind(quotes: pl.DataFrame, map_df: pl.DataFrame, kind: str, has_amount: bool) -> pl.DataFrame:
    """个股快照 join 成分映射 → 每个板块一行 (pct/n_with_bars/amount)。"""
    member_df = map_df.rename({"_sym_up": "_bare", kind: "_member"})
    # 映射表同时含全代码与裸代码行; 快照已按裸代码匹配, 只保留裸代码行避免双计
    member_df = member_df.filter(~pl.col("_bare").str.contains(r"\."))
    joined = quotes.join(member_df, on="_bare", how="inner")
    if joined.is_empty():
        return pl.DataFrame(schema={"name": pl.Utf8, "pct": pl.Float64, "n_with_bars": pl.Int64, "amount": pl.Float64})
    aggs = [
        pl.col("_pct").mean().alias("pct"),
        pl.col("_pct").count().alias("n_with_bars"),
    ]
    aggs.append(pl.col("_amt").sum().alias("amount") if has_amount else pl.lit(None, dtype=pl.Float64).alias("amount"))
    return (
        joined.group_by("_member")
        .agg(aggs)
        .rename({"_member": "name"})
        .with_columns(pl.col("n_with_bars").cast(pl.Int64))
    )


def _market_row(quotes: pl.DataFrame, has_amount: bool, minute: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "minute": [minute],
            "kind": [_MARKET_NAME],
            "name": [_MARKET_NAME],
            "pct": [quotes["_pct"].mean()],
            "n_with_bars": [quotes.height],
            "amount": [quotes["_amt"].sum() if has_amount else None],
        },
        schema=_SCHEMA,
    )
