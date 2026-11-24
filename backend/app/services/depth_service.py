"""五档盘口 sealed(真假涨停/跌停) 服务 — 独立旁路线。

架构(完全解耦):
  - 只读 enriched(拿涨跌停名单), 不写回 enriched(14列不动)
  - sealed 存独立 parquet(data/depth5/date=xxx/part.parquet)
  - limit_ladder API 查询时 LEFT JOIN(同 ext_columns 机制)
  - signal_limit_up 永远是"价格涨停", sealed 是叠加的真假判定层

数据流:
  盘中轮询线程(交易时段, 独立 sleep, 不绑行情轮询):
    读 enriched 内存缓存(线程安全) → 涨跌停名单 → tf.depth.batch
    → 算 sealed → 更新内存缓存(不落盘) → sealed_ready=True
  盘后定版 job(可配置时间, 默认15:02):
    最后拉一次 → 落盘 depth5 parquet(定版)

三层防护节流("设过大设上限, 设过小设最小值"):
  ① 套餐范围 clamp: Pro 10~120s, Expert 3~120s
  ② 限速安全 clamp: safe = 60/((rpm*0.8)/batches), 涨跌停多就自动放慢
  ③ 系统接管通知: 用户设置会超限时, 推 toast 告知已自动调整
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import date, time as dt_time

from app.market_time import cn_now, cn_today
from pathlib import Path

import polars as pl

from app.tickflow.capabilities import Cap
from app.tickflow.rate_limits import (
    apply_safety_rpm,
    chunked,
    resolve_limit,
    sleep_between_batches,
)

logger = logging.getLogger(__name__)


def _has_volume(cache: dict) -> bool:
    """快照里是否还有真实挂单量。

    收盘后腾讯 qt.gtimg.cn 会把五档成交量清零 (只剩买一价), 这种快照不能
    覆盖盘中数据, 也不能直接定版。
    """
    return any(
        (e.get("bid1_vol") or 0) > 0 or (e.get("ask1_vol") or 0) > 0
        for e in cache.values()
    )


# 套餐 → (轮询间隔下限s, 上限s)
TIER_INTERVAL_RANGE: dict[str, tuple[float, float]] = {
    "pro": (10.0, 120.0),
    "expert": (3.0, 120.0),
}
# 兜底: 其他有 DEPTH5_BATCH 的套餐按 pro 范围
DEFAULT_RANGE = (10.0, 120.0)

# 限速余量: 只用 rpm 的 80%, 给系统其他 depth 调用留空间
# 间隔硬下限/上限(任何套餐)
INTERVAL_HARD_MIN = 10.0
INTERVAL_HARD_MAX = 300.0

# 快照覆盖率下限: 低于当日涨跌停名单的这个比例视为残缺快照。
# 2026-11-23 实测: 15:02 定版对 159 只涨跌停只返回 2 只(腾讯先重置价格字段再恢复),
# 有量但有覆盖不足, 不能用来覆盖盘中缓存或定版。
COVERAGE_MIN_RATIO = 0.5

# 盘中快照落盘间隔(秒): 断电/重启会清空内存缓存, 定版时回退读这份快照。
# 5 分钟一盘, 一整天才 48 次小文件写 (每次不足百行), 代价可忽略。
INTRADAY_SNAPSHOT_INTERVAL_S = 300.0


class DepthService:
    """五档盘口 sealed 服务 — 单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 拉取+定版串行锁 (镜像 quote_service._fetch_lock): _fetch_and_seal 可能同时被
        # 请求线程 (run_once persist=True)、轮询线程、盘后 finalize 触发, 都写同一 parquet,
        # 无锁会交叉写坏文件。_lock 只护内存缓存, 此锁护整段 fetch+seal。
        self._fetch_lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._repo = None              # 延迟注入(KlineRepository)
        self._app_state = None         # 延迟注入(FastAPI app.state)

        # 内存缓存: {symbol: SealedEntry}
        # SealedEntry = {sealed_up, sealed_down, ask1_vol, bid1_vol, status, fetched_ts}
        self._sealed_cache: dict[str, dict] = {}
        self._sealed_ready = False
        self._sealed_date: date | None = None     # sealed 数据对应的交易日(可能是昨天,如休市)
        self._sealed_fetched_ts: float = 0.0   # 上次拉取的 perf_counter
        self._sealed_fetched_at: float = 0.0   # 上次拉取的 wall-clock 时间戳
        self._persisted_date: date | None = None  # 已落盘的日期
        self._last_intraday_snapshot: float = 0.0  # 上次写盘中快照的 perf_counter

        # 系统接管状态(防通知刷屏)
        self._last_taken_over: bool | None = None
        self._last_user_interval: float | None = None

    # ================================================================
    # 注入
    # ================================================================

    def set_repo(self, repo) -> None:
        self._repo = repo

    def set_app_state(self, app_state) -> None:
        self._app_state = app_state

    # ================================================================
    # 生命周期
    # ================================================================

    def boot_check(self) -> None:
        """启动补跑: 当天 depth5 文件不存在则 finalize 一次; 已存在则恢复内存缓存。"""
        if not self._has_capability():
            logger.info("depth sealed: %s 不可用, 跳过启动补跑", self._capability_label())
            return
        today = cn_today()
        if self._persisted_for_date(today):
            # parquet 已存在: 恢复内存缓存(避免重启后每次查询都读 parquet)
            self._restore_from_parquet(today)
            return
        logger.info("depth sealed: 启动补跑今天定版")
        try:
            self.finalize()
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 启动补跑失败: %s", e)

    def _restore_from_parquet(self, d: date) -> None:
        """从 parquet 恢复内存缓存(服务重启后)。"""
        if not self._repo:
            return
        out = self._repo.store.data_dir / "depth5" / f"date={d.isoformat()}" / "part.parquet"
        if not out.exists():
            return
        try:
            df = pl.read_parquet(out)
            cache: dict[str, dict] = {}
            for row in df.to_dicts():
                sym = row.get("symbol")
                if not sym:
                    continue
                cache[sym] = {
                    "sealed_up": row.get("sealed_up"),
                    "sealed_down": row.get("sealed_down"),
                    "ask1_vol": row.get("ask1_vol"),
                    "bid1_vol": row.get("bid1_vol"),
                    "status": row.get("status"),
                    "fetched_ts": row.get("fetched_at"),
                }
            with self._lock:
                self._sealed_cache = cache
                self._sealed_ready = True
                self._sealed_date = d
                self._persisted_date = d
            logger.info("depth sealed: 从 parquet 恢复 %d 只 (日期=%s)", len(cache), d)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 从 parquet 恢复失败: %s", e)

    def start_polling(self) -> None:
        """启动盘中轮询线程(连板梯队监控开启 + 实时行情开启 + 有能力)。

        依赖实时行情开关: 实时行情关闭时 enriched 内存缓存停留在上一交易日,
        轮询会反复拉取陈旧的涨跌停名单(浪费 API 额度且数据无意义)。
        实时行情开关切换时由 settings API 调 stop_polling/start_polling 同步启停。
        """
        if not self._has_capability():
            return
        from app.services import preferences
        if not preferences.get_limit_ladder_monitor_enabled():
            return
        if not preferences.get_realtime_quotes_enabled():
            return
        # check-then-act 加锁: 两个线程同时 start_polling 不会各起一个轮询线程
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("depth sealed 盘中轮询已启动")

    def stop_polling(self) -> None:
        """停止盘中轮询线程。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("depth sealed 盘中轮询已停止")

    def apply_monitor_toggle(self, enabled: bool) -> None:
        """连板梯队监控开关切换时调用: 开启→启动轮询, 关闭→停止轮询。"""
        if enabled:
            self.start_polling()
        else:
            self.stop_polling()

    def run_once(self) -> dict:
        """手动触发一次修正(立即拉取 depth + 更新内存缓存)。

        不受监控开关限制 — 用户可随时手动修正一次。
        返回 {"ok": bool, "count": int, "msg": str}
        """
        if not self._has_capability():
            return {"ok": False, "count": 0, "msg": f"{self._capability_label()} 不可用"}
        try:
            self._fetch_and_seal(persist=True)  # 落盘, 刷新页面不丢
            with self._lock:
                count = len(self._sealed_cache)
            return {"ok": True, "count": count, "msg": f"已修正 {count} 只"}
        except Exception as e:  # noqa: BLE001
            logger.warning("depth run_once 失败: %s", e)
            return {"ok": False, "count": 0, "msg": f"修正失败: {e}"}

    # ================================================================
    # 核心拉取
    # ================================================================

    def _fetch_and_seal(self, persist: bool = False) -> None:
        """拉一次 depth.batch, 算 sealed, 更新内存缓存(可选落盘)。

        persist=True: 盘后定版, 写 depth5 parquet
        persist=False: 盘中轮询, 只更新内存缓存

        网络拉取在 _fetch_lock 外执行 (SDK 批量最坏数秒, 全程持锁会让请求线程
        run_once/轮询/finalize 互相阻塞); 锁内只做 sealed 计算与缓存替换/落盘,
        parquet 写串行化语义不变。
        """
        prepared = self._fetch_depth_snapshot()
        if prepared is None:
            if persist:
                self._finalize_from_cache()
            return
        depth_data, syms_up, syms_down, enriched_date = prepared
        with self._fetch_lock:
            self._seal_locked(depth_data, syms_up, syms_down, enriched_date, persist)

    def _finalize_from_cache(self) -> None:
        """定版拉取为空时的兜底: 用盘中最后一盘缓存落盘。

        收盘后腾讯五档成交量会被清零 (只剩买一价), 15:02 定版可能拉到空快照。
        不能因此丢掉当天封单, 但只能沿用同一天且有量的缓存。
        """
        with self._lock:
            cache = dict(self._sealed_cache)
            day = self._sealed_date
        if day and cache and _has_volume(cache):
            logger.warning(
                "depth sealed: 定版拉取为空, 沿用盘中最后一盘 %d 只 (日期=%s)", len(cache), day,
            )
            self._persist(day)
        elif self._restore_intraday_snapshot(cn_today()):
            logger.warning("depth sealed: 定版拉取为空, 沿用盘中落盘快照 (日期=%s)", cn_today())
            self._persist(cn_today())
        else:
            logger.warning("depth sealed: 定版拉取为空且无可沿用的盘中数据, 今日不落盘")

    def _fetch_depth_snapshot(self):
        """锁外阶段: 读 enriched 涨跌停名单 + 批量拉取 depth (网络调用不进锁)。"""
        if not self._repo:
            return None

        # 只读 enriched 内存缓存(线程安全, 避免和 quote_service 写盘竞态)
        enriched, enriched_date = self._repo.get_enriched_latest()
        if enriched.is_empty():
            return None

        # 筛涨跌停名单(用 fill_null 防止列缺失)
        syms_up: list[str] = []
        syms_down: list[str] = []
        if "signal_limit_up" in enriched.columns:
            syms_up = enriched.filter(
                pl.col("signal_limit_up").fill_null(False)
            )["symbol"].to_list()
        if "signal_limit_down" in enriched.columns:
            syms_down = enriched.filter(
                pl.col("signal_limit_down").fill_null(False)
            )["symbol"].to_list()

        all_syms = list(dict.fromkeys(syms_up + syms_down))  # 去重保序
        if not all_syms:
            logger.debug("depth sealed: 当日无涨跌停股, 跳过")
            return None

        # 拉 depth(涨跌停一次拉, 按 capset batch 切片)
        depth_data = self._call_depth_batch(all_syms)
        if not depth_data:
            logger.warning("depth sealed: depth.batch 返回空")
            return None
        return depth_data, syms_up, syms_down, enriched_date

    def _seal_locked(self, depth_data, syms_up, syms_down, enriched_date, persist) -> None:
        """_fetch_and_seal 的计算/缓存/落盘阶段, 须在持有 _fetch_lock 时调用。"""
        up_set = set(syms_up)
        down_set = set(syms_down)
        now_perf = time.perf_counter()
        now_wall = time.time()

        new_cache: dict[str, dict] = {}
        for sym, d in depth_data.items():
            ask_vols = d.get("ask_volumes") or []
            bid_vols = d.get("bid_volumes") or []
            ask1 = ask_vols[0] if ask_vols else None
            bid1 = bid_vols[0] if bid_vols else None
            # depth 返回的 timestamp(毫秒 epoch), 回退到当前 wall-clock
            depth_ts = d.get("timestamp")
            fetched = (depth_ts / 1000.0) if isinstance(depth_ts, (int, float)) and depth_ts else now_wall
            entry = {
                # 涨停真封: 涨停价上卖一(主动卖压)为 0
                "sealed_up": (ask1 == 0) if sym in up_set and ask1 is not None else None,
                # 跌停真封: 跌停价上买一为 0
                "sealed_down": (bid1 == 0) if sym in down_set and bid1 is not None else None,
                "ask1_vol": ask1,
                "bid1_vol": bid1,
                "status": "limit_down" if sym in down_set and sym not in up_set else "limit_up",
                "fetched_ts": fetched,
            }
            new_cache[sym] = entry

        # 收盘后五档量清零 → 新快照没有可用挂单量; 或收盘后腾讯重置价格字段 →
        # 只返回极少数标的(覆盖残缺)。两种都不能覆盖盘中最后一盘, 否则当天封单
        # 会被改成 0 或只剩零星几只并定版。
        expected = len(up_set | down_set)
        degraded = expected > 0 and len(new_cache) < expected * COVERAGE_MIN_RATIO
        if not _has_volume(new_cache) or degraded:
            with self._lock:
                prev_cache = dict(self._sealed_cache)
                prev_day = self._sealed_date
            reason = (
                "五档成交量已清零"
                if not _has_volume(new_cache)
                else f"快照残缺({len(new_cache)}/{expected} 只)"
            )
            if prev_day == enriched_date and prev_cache and _has_volume(prev_cache):
                logger.warning(
                    "depth sealed: %s, 保持盘中最后一盘 %d 只 (日期=%s)%s",
                    reason, len(prev_cache), prev_day, " → 落盘" if persist else "",
                )
                if persist:
                    self._persist(enriched_date)
            else:
                logger.warning("depth sealed: %s 且无可沿用的盘中缓存, 本轮跳过", reason)
            return

        with self._lock:
            self._sealed_cache = new_cache
            self._sealed_ready = True
            self._sealed_date = enriched_date  # 记录数据对应的交易日(可能是昨天,如休市)
            self._sealed_fetched_ts = now_perf
            self._sealed_fetched_at = now_wall

        logger.info("depth sealed: 拉取 %d 只 (涨停%d/跌停%d) 日期=%s%s",
                    len(new_cache), len(syms_up), len(syms_down),
                    enriched_date, " → 落盘" if persist else "")

        # 缓存已更新: 通知 SSE 推 depth_updated, 触发连板梯队刷新封单数据。
        self._notify_depth_updated(len(new_cache))

        # 盘中每 5 分钟把当前盘口落一份快照, 断电/重启后定版可回退。
        if not persist:
            self._maybe_persist_intraday(enriched_date)

        if persist and enriched_date:
            self._persist(enriched_date)

    def _call_depth_batch(self, symbols: list[str]) -> dict:
        """按独立五档路由取数; 所有 provider 共用分片限速且失败不跨源回退。"""
        from app.services import preferences

        provider_name = preferences.get_depth5_data_provider()
        if provider_name == "tickflow":
            from app.data_providers.registry import get_provider

            provider = get_provider("tickflow")
        else:
            from app.data_providers import custom as custom_sources

            try:
                if not custom_sources.provider_has_dataset(provider_name, "depth5"):
                    logger.warning("depth provider %s 未声明 depth5, 跳过本轮", provider_name)
                    return {}
                provider = custom_sources.get_provider(provider_name)
            except Exception as e:
                logger.warning("depth provider %s 解析失败, 跳过本轮: %s", provider_name, e)
                return {}

        fetch_depth = getattr(provider, "get_depth_batch", None)
        if not callable(fetch_depth):
            logger.warning("depth provider %s 未实现 get_depth_batch, 跳过本轮", provider_name)
            return {}

        capset = self._get_capset()
        limit = resolve_limit(capset, Cap.DEPTH5_BATCH, default_batch=100, default_rpm=30)

        result: dict = {}
        chunks = chunked(symbols, limit.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, limit.rpm, default_interval=2.0)
            try:
                data = fetch_depth(chunk)
                if isinstance(data, dict):
                    result.update(data)
                else:
                    logger.warning(
                        "depth provider %s 第 %d 批返回非 dict, 已跳过",
                        provider_name,
                        i + 1,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "depth provider %s 第 %d 批失败(%d 只): %s",
                    provider_name,
                    i + 1,
                    len(chunk),
                    e,
                )
                # 单批失败不影响其他批
        return result

    def finalize(self) -> None:
        """盘后定版: 有当日盘中有量缓存就直接落盘, 没有才重拉一次。

        连续竞价 15:00 结束, 14:59 的盘中缓存才是收盘真实状态; 15:02 收盘后
        重拉腾讯五档可能拿到残缺快照 (2026-11-23: 159 只只要到 2 只)。因此
        只要缓存是当天的且有挂单量, 就以缓存定版, 不再让重拉结果覆盖它。
        """
        if not self._has_capability():
            return
        with self._lock:
            cache = dict(self._sealed_cache)
            day = self._sealed_date
        today = cn_today()
        if day == today and cache and _has_volume(cache):
            logger.info(
                "depth sealed: 定版使用盘中最后一盘 %d 只 (日期=%s)", len(cache), day,
            )
            self._persist(day)
            return
        # 断电/重启: 内存缓存已丢, 回退到盘中定期落盘的快照。
        if self._restore_intraday_snapshot(today):
            with self._lock:
                n = len(self._sealed_cache)
            logger.info(
                "depth sealed: 定版使用盘中落盘快照 %d 只 (日期=%s)", n, today,
            )
            self._persist(today)
            return
        self._fetch_and_seal(persist=True)

    def finalize_if_needed(self) -> None:
        """定版重试: 当天已有定版文件就跳过, 否则再跑一次 finalize。"""
        if not self._has_capability():
            return
        today = cn_today()
        if self._persisted_for_date(today):
            return
        logger.info("depth sealed: 定版重试 (当天文件仍缺失)")
        self.finalize()

    def close_integrity_check(self) -> dict:
        """收盘完整性检查: 当天 depth5 缺失或覆盖不足当天涨跌停一半 -> 告警。"""
        today = cn_today()
        rows = 0
        expected = 0
        if self._repo:
            out = self._repo.store.data_dir / "depth5" / f"date={today.isoformat()}" / "part.parquet"
            if out.exists():
                try:
                    rows = pl.read_parquet(out).height
                except Exception as e:  # noqa: BLE001
                    logger.warning("depth5 完整性检查读取失败: %s", e)
            try:
                enriched, enriched_date = self._repo.get_enriched_latest()
                if enriched_date == today and not enriched.is_empty():
                    for col in ("signal_limit_up", "signal_limit_down"):
                        if col in enriched.columns:
                            expected += int(enriched.filter(pl.col(col).fill_null(False)).height)
            except Exception as e:  # noqa: BLE001
                logger.warning("depth5 完整性检查取涨跌停名单失败: %s", e)
        ok = rows > 0 and (expected == 0 or rows >= expected * COVERAGE_MIN_RATIO)
        result = {
            "date": today.isoformat(),
            "rows": rows,
            "expected": expected,
            "ok": ok,
        }
        if ok:
            logger.info("depth5 收盘完整性检查通过: %d 行 (当日涨跌停 %d 只)", rows, expected)
        else:
            logger.error("depth5 收盘完整性检查失败: %d 行 (当日涨跌停 %d 只)", rows, expected)
            self._alert_close_integrity(result)
        return result

    def _alert_close_integrity(self, result: dict) -> None:
        """把缺失/残缺写进告警中心并弹系统通知 (失败静默, 不阻断)。"""
        title = "五档收盘数据缺失" if result["rows"] == 0 else "五档收盘覆盖不足"
        body = f"{result['date']} 五档定版 {result['rows']}/{result['expected']} 只"
        if self._repo:
            try:
                from app.services import alert_store

                alert_store.append(self._repo.store.data_dir, {
                    "ts": int(time.time() * 1000),
                    "rule_id": "depth5_close_integrity",
                    "rule_name": "五档收盘完整性",
                    "source": "depth5",
                    "type": title,
                    "symbol": "",
                    "name": "",
                    "message": body,
                    "severity": "error",
                    "signals": [],
                    "conditions": [],
                    "logic": "and",
                })
            except Exception as e:  # noqa: BLE001
                logger.warning("depth5 完整性告警落盘失败: %s", e)
        try:
            from app.services import notify_adapter

            notify_adapter.notify(title, body)
        except Exception:  # noqa: BLE001
            logger.debug("depth5 完整性系统通知失败")

    # ================================================================
    # 落盘
    # ================================================================

    def _cache_frame(self, cache: dict) -> pl.DataFrame:
        rows = []
        for sym, e in cache.items():
            rows.append({
                "symbol": sym,
                "sealed_up": e.get("sealed_up"),
                "sealed_down": e.get("sealed_down"),
                "ask1_vol": e.get("ask1_vol"),
                "bid1_vol": e.get("bid1_vol"),
                "status": e.get("status"),
                "fetched_at": e.get("fetched_ts"),
            })
        # 显式 schema: sealed_up/sealed_down 是 bool 与 None 混合, 不指定 schema
        # polars 会按首行推断类型, 后续遇到不一致 (bool vs null) 报
        # "could not append value: false of type: bool to the builder"。
        return pl.DataFrame(rows, schema={
            "symbol": pl.Utf8,
            "sealed_up": pl.Boolean,
            "sealed_down": pl.Boolean,
            "ask1_vol": pl.Int64,
            "bid1_vol": pl.Int64,
            "status": pl.Utf8,
            "fetched_at": pl.Float64,
        })

    def _write_cache(self, path: Path, cache: dict) -> int | None:
        """原子写缓存到 path; 空缓存返回 None。"""
        if not cache:
            return None
        df = self._cache_frame(cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写: 先写临时文件再 os.replace, 避免读侧读到半写 parquet
        tmp = path.with_name(path.name + ".tmp")
        df.write_parquet(tmp)
        os.replace(tmp, path)
        return df.height

    def _persist(self, today: date) -> None:
        """把内存缓存写 depth5/date=今天/part.parquet。"""
        with self._lock:
            cache = dict(self._sealed_cache)
        out = self._repo.store.data_dir / "depth5" / f"date={today.isoformat()}" / "part.parquet"
        try:
            rows = self._write_cache(out, cache)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 落盘失败: %s", e)
            return
        if rows is None:
            return
        self._persisted_date = today
        logger.info("depth sealed 落盘: %d 行 → %s", rows, out)
        # 定版成功后清掉盘中快照, 避免下次误用旧数据
        try:
            self._snapshot_path(today).unlink(missing_ok=True)
        except OSError:
            pass

    def _snapshot_path(self, d: date) -> Path:
        """盘中快照路径: 放在 depth5 视图之外, 避免被 DuckDB view 重复计入。"""
        return self._repo.store.data_dir / "cache" / "depth5_intraday" / f"date={d.isoformat()}.parquet"

    def _maybe_persist_intraday(self, d: date) -> None:
        """盘中定期把当前盘口快照写到磁盘 (断电/重启后定版可回退)。"""
        now = time.perf_counter()
        if now - self._last_intraday_snapshot < INTRADAY_SNAPSHOT_INTERVAL_S:
            return
        with self._lock:
            cache = dict(self._sealed_cache)
        try:
            n = self._write_cache(self._snapshot_path(d), cache)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 盘中快照写入失败: %s", e)
            return
        if n:
            self._last_intraday_snapshot = now
            logger.debug("depth sealed 盘中快照: %d 行 (日期=%s)", n, d)

    def _restore_intraday_snapshot(self, d: date) -> bool:
        """从盘中定时落盘的快照恢复内存缓存; 无文件/无挂单量返回 False。"""
        if not self._repo:
            return False
        path = self._snapshot_path(d)
        if not path.exists():
            return False
        try:
            df = pl.read_parquet(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 盘中快照读取失败: %s", e)
            return False
        cache: dict[str, dict] = {}
        for row in df.to_dicts():
            sym = row.get("symbol")
            if not sym:
                continue
            cache[sym] = {
                "sealed_up": row.get("sealed_up"),
                "sealed_down": row.get("sealed_down"),
                "ask1_vol": row.get("ask1_vol"),
                "bid1_vol": row.get("bid1_vol"),
                "status": row.get("status"),
                "fetched_ts": row.get("fetched_at"),
            }
        if not cache or not _has_volume(cache):
            return False
        with self._lock:
            self._sealed_cache = cache
            self._sealed_ready = True
            self._sealed_date = d
            self._sealed_fetched_ts = 0.0
        logger.info("depth sealed: 从盘中快照恢复 %d 只 (日期=%s)", len(cache), d)
        return True

    def _persisted_for_date(self, d: date) -> bool:
        """检查某日 depth5 文件是否已存在。"""
        if not self._repo:
            return False
        out = self._repo.store.data_dir / "depth5" / f"date={d.isoformat()}" / "part.parquet"
        return out.exists()

    # ================================================================
    # 查询(供 limit_ladder API 用)
    # ================================================================

    def get_sealed_map(self, target_date: date, is_down: bool) -> dict:
        """返回 {symbol: {sealed, vol, ready, age}} 供 JOIN。

        优先内存缓存(盘中), 回退 parquet(历史/盘后)。
        sealed: bool | None (None=待确认或降级)
        vol: 封单量(int) | None
        ready: sealed 数据是否就绪(False→降级标识)
        age: 距上次拉取秒数(盘后定版为 None)
        """
        # 内存缓存(sealed 数据对应的交易日 = target_date 时才用)
        if self._sealed_date and target_date == self._sealed_date and self._sealed_ready and self._sealed_cache:
            return self._read_from_memory(is_down)
        # parquet(历史或盘后定版)
        return self._read_from_parquet(target_date, is_down)

    def _read_from_memory(self, is_down: bool) -> dict:
        sealed_key = "sealed_down" if is_down else "sealed_up"
        # 封单量: 涨停=买一量(涨停价买单堆积), 跌停=卖一量(跌停价卖单堆积)
        vol_key = "ask1_vol" if is_down else "bid1_vol"
        now = time.perf_counter()
        with self._lock:
            cache = dict(self._sealed_cache)
            fetched_ts = self._sealed_fetched_ts
        age = (now - fetched_ts) if fetched_ts else 0.0
        result = {}
        for sym, e in cache.items():
            result[sym] = {
                "sealed": e.get(sealed_key),
                "vol": e.get(vol_key),
                "ready": True,
                "age": age,
            }
        return result

    def _read_from_parquet(self, target_date: date, is_down: bool) -> dict:
        if not self._repo:
            return {}
        out = self._repo.store.data_dir / "depth5" / f"date={target_date.isoformat()}" / "part.parquet"
        if not out.exists():
            return {}
        try:
            df = pl.read_parquet(out)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth5 parquet 读取失败: %s", e)
            return {}
        sealed_key = "sealed_down" if is_down else "sealed_up"
        # 封单量: 涨停=买一量, 跌停=卖一量
        vol_key = "ask1_vol" if is_down else "bid1_vol"
        result = {}
        for row in df.to_dicts():
            sym = row.get("symbol")
            if not sym:
                continue
            result[sym] = {
                "sealed": row.get(sealed_key),
                "vol": row.get(vol_key),
                "ready": True,
                "age": None,  # 盘后定版, 无 age
            }
        return result

    def is_sealed_ready(self, target_date: date) -> bool:
        """sealed 数据是否就绪(供前端降级判定)。"""
        # 内存缓存对应的数据日 == 查询日 → 看内存就绪状态
        if self._sealed_date and target_date == self._sealed_date:
            return self._sealed_ready
        # 其他日期: 有 parquet 就 ready
        return self._persisted_for_date(target_date)

    def get_sealed_age(self, target_date: date) -> float | None:
        """返回 sealed 数据 age(秒), 盘后定版为 None。"""
        if self._sealed_date and target_date == self._sealed_date and self._sealed_ready and self._sealed_fetched_ts:
            return time.perf_counter() - self._sealed_fetched_ts
        return None

    # ================================================================
    # 盘中轮询线程
    # ================================================================

    def _poll_loop(self) -> None:
        """盘中轮询: 按 capset 自适应间隔拉 depth, 更新内存缓存。"""
        while self._running:
            try:
                if self._is_continuous_trading():
                    self._poll_once()
                else:
                    logger.debug("depth sealed: 非连续竞价时段, 跳过(避免集合竞价盘口覆盖 11:30 定格值)")
            except Exception as e:  # noqa: BLE001
                logger.warning("depth sealed 轮询异常: %s", e)

            # 等待下一轮(用 _running 检查保证能及时退出)
            interval = self._current_sleep_interval()
            waited = 0.0
            while self._running and waited < interval:
                time.sleep(0.5)
                waited += 0.5

    def _poll_once(self) -> None:
        """单次轮询: 算间隔(三层防护) → 拉取 → 检测系统接管通知。"""
        # 数当前涨跌停股
        n = self._count_limit_stocks()
        if n == 0:
            return

        interval, taken_over, user_interval = self._compute_interval(n)

        # 系统接管通知(状态切换时才推, 防刷屏)
        if taken_over and (self._last_taken_over is False or self._last_user_interval != user_interval):
            self._notify_takeover(n, user_interval, interval)
        self._last_taken_over = taken_over
        self._last_user_interval = user_interval

        self._fetch_and_seal(persist=False)

    def _current_sleep_interval(self) -> float:
        """计算当前 sleep 间隔(供 _poll_loop 等待用)。"""
        n = self._count_limit_stocks()
        if n == 0:
            return 30.0  # 无涨跌停, 慢轮询
        interval, _, _ = self._compute_interval(n)
        return interval

    # ================================================================
    # 三层防护节流
    # ================================================================

    def _compute_interval(self, n_symbols: int) -> tuple[float, bool, float]:
        """三层防护计算实际轮询间隔。

        返回 (actual_interval, taken_over, user_interval)
        - actual_interval: 实际使用的间隔(秒)
        - taken_over: 是否被系统接管(用户设置会超限)
        - user_interval: 用户设置(经套餐 clamp 后)的间隔
        """
        from app.services import preferences
        from app.tickflow.policy import tier_label

        capset = self._get_capset()
        lim = capset.limits(__import__("app.tickflow.capabilities", fromlist=["Cap"]).Cap.DEPTH5_BATCH)
        batch_size = (lim.batch if lim and lim.batch else 100)
        rpm = (lim.rpm if lim and lim.rpm else 30)

        # ① 套餐范围 clamp
        tier = tier_label().split()[0].split("+")[0].strip().lower()
        lo, hi = TIER_INTERVAL_RANGE.get(tier, DEFAULT_RANGE)
        raw_user = preferences.get_depth_polling_interval()
        user_interval = max(lo, min(hi, raw_user))

        # ② 限速安全 clamp（与 resolve_limit 共用 SAFETY_RPM_FACTOR，不叠乘）
        batches = max(1, math.ceil(n_symbols / batch_size))
        usable_rpm = apply_safety_rpm(rpm) or 1
        calls_per_min = usable_rpm / batches if batches > 0 else usable_rpm
        safe_interval = 60.0 / calls_per_min if calls_per_min > 0 else INTERVAL_HARD_MAX

        # 实际: 取用户设置和安全的较大值
        actual = max(user_interval, safe_interval)
        # 硬上下限
        actual = max(INTERVAL_HARD_MIN, min(actual, INTERVAL_HARD_MAX))
        taken_over = safe_interval > user_interval

        return actual, taken_over, user_interval

    def _count_limit_stocks(self) -> int:
        """数当前涨跌停股总数(供节流计算)。"""
        if not self._repo:
            return 0
        enriched, _ = self._repo.get_enriched_latest()
        if enriched.is_empty():
            return 0
        n = 0
        if "signal_limit_up" in enriched.columns:
            n += enriched.filter(pl.col("signal_limit_up").fill_null(False)).height
        if "signal_limit_down" in enriched.columns:
            n += enriched.filter(pl.col("signal_limit_down").fill_null(False)).height
        return n

    # ================================================================
    # 通知
    # ================================================================

    def _notify_takeover(self, n_stocks: int, user_interval: float, actual_interval: float) -> None:
        """系统接管通知: 通过 quote_service 广播到所有 SSE 订阅者。"""
        if not self._app_state:
            return
        qs = getattr(self._app_state, "quote_service", None)
        if not qs:
            return
        msg = (f"五档轮询: 当前涨跌停 {n_stocks} 只, 您设置的 {user_interval:.0f} 秒间隔会超限, "
               f"系统已自动调整为 {actual_interval:.0f} 秒")
        alert = {
            "source": "depth",
            "type": "takeover",
            "message": msg,
        }
        try:
            qs.push_alerts([alert])
        except Exception as e:  # noqa: BLE001
            logger.debug("depth 接管通知推送失败: %s", e)

    def _notify_depth_updated(self, count: int) -> None:
        """修正完成通知: set quote_service._depth_update_event, SSE 推 depth_updated 刷新连板梯队。"""
        if not self._app_state:
            return
        qs = getattr(self._app_state, "quote_service", None)
        if not qs:
            return
        try:
            qs.notify_depth_updated()
        except Exception as e:  # noqa: BLE001
            logger.debug("depth 更新通知推送失败: %s", e)

    # ================================================================
    # 工具
    # ================================================================

    def _has_capability(self) -> bool:
        """五档能力是否可用 — 以【生效源】为准, 与取数路径 (_call_depth_batch) 同口径。

        不能只看 TickFlow 档位: 五档路由到声明了 depth5 的插件 (如 tdxquant) 时,
        TickFlow 档位无关; 反之路由到 TickFlow 但档位不足时, 插件声明不该算数。
        本轮修复前此处只查 capset, 导致「五档已切到插件但启动补跑被跳过」。
        """
        from app.data_providers.capabilities import capability_gate
        ok, _ = capability_gate("depth5")
        return ok

    def _capability_label(self) -> str:
        """门禁日志标签: "五档盘口 ← fuyao" / "…(档位不足)", 便于判断该切哪个源。"""
        from app.data_providers.capabilities import capability_gate
        _, label = capability_gate("depth5")
        return label

    def _get_capset(self):
        """获取当前 capset(优先 app.state, 回退 detect)。"""
        if self._app_state:
            cs = getattr(self._app_state, "capabilities", None)
            if cs:
                return cs
        from app.tickflow.policy import detect_capabilities
        return detect_capabilities()

    @staticmethod
    def _is_trading_hours() -> bool:
        # 显式北京时间: 容器/服务器本地时区可能是 UTC, 用 naive now() 会整体错开轮询窗口
        now = cn_now()
        t = now.time()
        morning = dt_time(9, 25) <= t <= dt_time(11, 35)
        afternoon = dt_time(12, 55) <= t <= dt_time(15, 5)
        return now.weekday() < 5 and (morning or afternoon)

    @staticmethod
    def _is_continuous_trading() -> bool:
        """A股连续竞价时段(北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。

        比 _is_trading_hours 严格: 排除午间休市前后(11:30-13:00)。
        depth sealed 轮询用此窗口而非宽窗口, 关键原因:
        12:55-13:00 午后集合竞价准备期, ask1/bid1 盘口语义与连续竞价不同,
        「涨停价上卖一==0」的真封判定在此期间失效, 会用竞价盘口覆盖 11:30
        已定格的正确 sealed 值, 导致涨停股误判为 sealed=False(假涨停)被错误扣减。
        与 quote_service._is_continuous_trading 窗口定义保持一致。
        """
        now = cn_now()
        t = now.time()
        morning = dt_time(9, 30) <= t <= dt_time(11, 30)
        afternoon = dt_time(13, 0) <= t <= dt_time(15, 0)
        return now.weekday() < 5 and (morning or afternoon)
