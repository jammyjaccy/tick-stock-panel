"""腾讯 1 分钟 K 线 provider。

契约 (docs/plugin-development.md):
    get_minute(symbols, start_time, end_time, asset_type, on_chunk_done, freq)
    -> [symbol, datetime(北京墙钟 naive), open, high, low, close, volume, amount]

实测口径 (2026-11-19, 个股 000815.SZ / 指数 000001.SH / 399001.SZ):
  - https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=<code>,m1,,320
  - 行格式 [YYYYMMDDHHMM, 开, 收, 高, 低, 量(手), {}, 涨跌幅] — 注意是"开, 收, 高, 低"
  - 一次只返回最近 320 根 (约 1.3 个交易日), 更早的分钟取不到
  - 量单位为手 (与面板内部口径一致, 无需换算); 无成交额 -> amount 置 null, 不伪造
  - 北交所 (bj 前缀) 返回空; 指数支持
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import polars as pl

logger = logging.getLogger(__name__)

_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
# 当日分时: 每行 "HHMM 价 累计量(手) 累计额(元)"; 只有最新交易日, 用来差分出每分钟成交额
_TREND_URL = "https://ifzq.gtimg.cn/appstock/app/minute/query"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
_CST = ZoneInfo("Asia/Shanghai")
_BARS = 320
_INTERVAL_S = 0.12

COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def to_tencent_code(symbol: str) -> str | None:
    """600519.SH -> sh600519; 920009.BJ -> bj920009."""
    code, _, exchange = symbol.strip().upper().partition(".")
    if not (code.isdigit() and len(code) == 6):
        return None
    exchange = exchange.lower()
    if exchange not in ("sh", "sz", "bj"):
        exchange = "sh" if code.startswith("6") else "sz"
    return f"{exchange}{code}"


def _num(text) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_minute(payload: dict, code: str, symbol: str) -> list[dict]:
    """腾讯 mkline 响应 -> 内部分钟行 (北京墙钟 naive, 量手, amount=None)."""
    node = (payload.get("data") or {}).get(code) or {}
    bars = node.get("m1") or []
    out: list[dict] = []
    for bar in bars:
        if not isinstance(bar, (list, tuple)) or len(bar) < 6:
            continue
        try:
            dt = datetime.strptime(str(bar[0]), "%Y%m%d%H%M")
        except ValueError:
            continue
        open_, close, high, low, volume = (_num(bar[i]) for i in range(1, 6))
        if None in (open_, close, high, low):
            continue
        out.append({
            "symbol": symbol,
            "datetime": dt,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": None,  # 源不提供分钟成交额
        })
    return out


def _naive_cn(value: datetime | None) -> datetime | None:
    """带时区的时间 -> 北京墙钟 naive (契约要求 naive 北京时间)."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(_CST).replace(tzinfo=None)
    return value


def parse_cumulative_amount(payload: dict, code: str) -> dict[datetime, float]:
    """腾讯当日分时 (累计量/额) -> {北京时间分钟: 该分钟成交额(元)}.

    行格式 "HHMM 价 累计量 累计额"; 相邻累计额相减得到每分钟成交额,
    首根即其累计值. 只覆盖接口返回的那一个交易日.
    """
    node = (payload.get("data") or {}).get(code) or {}
    data = node.get("data") or {}
    day = str(data.get("date") or "")
    if len(day) != 8 or not data.get("data"):
        return {}
    out: dict[datetime, float] = {}
    prev_cum = 0.0
    for line in data["data"]:
        parts = str(line).split()
        if len(parts) < 4:
            continue
        hhmm, cum_amt = parts[0], _num(parts[3])
        if cum_amt is None or len(hhmm) != 4:
            continue
        try:
            dt = datetime(int(day[:4]), int(day[4:6]), int(day[6:]), int(hhmm[:2]), int(hhmm[2:]))
        except ValueError:
            continue
        out[dt] = cum_amt - prev_cum
        prev_cum = cum_amt
    return out


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "symbol": pl.String, "datetime": pl.Datetime, "open": pl.Float64,
        "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
        "volume": pl.Float64, "amount": pl.Float64,
    })


class TencentMinuteProvider:
    """只提供 minute 的免费数据源。"""

    name = "tencent_minute"
    builtin = True
    # 免费分时只保留约 320 根 (1.3 个交易日) -> 声明为浅源, 个股分时档位随之收窄
    minute_history_days = 2

    def __init__(self) -> None:
        self.config = SimpleNamespace(datasets={"minute": {}})
        self._client = httpx.Client(timeout=10.0, headers=_HEADERS)

    def close(self) -> None:  # loader 重建注册表时会调用
        self._client.close()

    def _fetch(self, code: str) -> dict:
        resp = self._client.get(_URL, params={"param": f"{code},m1,,{_BARS}"})
        resp.raise_for_status()
        return resp.json()

    def _fetch_trend(self, code: str) -> dict:
        resp = self._client.get(_TREND_URL, params={"code": code})
        resp.raise_for_status()
        return resp.json()

    def _rows_for(self, symbol: str, start: datetime | None, end: datetime | None) -> list[dict]:
        code = to_tencent_code(symbol)
        if code is None:
            return []
        try:
            rows = parse_minute(self._fetch(code), code, symbol)
        except Exception as e:  # noqa: BLE001 — 单只失败不阻断其它标的
            logger.warning("腾讯分钟拉取失败 %s: %s", symbol, e)
            return []
        if start is not None:
            rows = [r for r in rows if r["datetime"] >= start]
        if end is not None:
            rows = [r for r in rows if r["datetime"] <= end]
        # mkline 不含成交额; 当日分时接口给累计额, 差分后补齐 (只对最新交易日有效)
        if any(r["datetime"].date() == datetime.now(_CST).date() for r in rows):
            time.sleep(_INTERVAL_S)
            try:
                amounts = parse_cumulative_amount(self._fetch_trend(code), code)
                for r in rows:
                    amount = amounts.get(r["datetime"])
                    if amount is not None:
                        r["amount"] = amount
            except Exception as e:  # noqa: BLE001 — 缺成交额不影响 K 线
                logger.warning("腾讯分时成交额拉取失败 %s: %s", symbol, e)
        return rows

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done=None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        if freq != "1m":
            # 源只有 1 分钟 K; 其它周期不得用 1m 冒充
            return _empty()
        start, end = _naive_cn(start_time), _naive_cn(end_time)
        rows: list[dict] = []
        total = len(symbols)
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            rows.extend(self._rows_for(symbol, start, end))
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        return pl.DataFrame(rows, schema=_empty().schema) if rows else _empty()

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done=None,
    ):
        """逐只产出, 供历史落盘消费; 帧大小与请求一一对应。"""
        if freq != "1m":
            return
        start, end = _naive_cn(start_time), _naive_cn(end_time)
        total = len(symbols)
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            rows = self._rows_for(symbol, start, end)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
            if not rows:
                continue
            frame = pl.DataFrame(rows, schema=_empty().schema)
            if not frame.is_empty():
                yield frame

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "minute":
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "error": "该数据集未声明, 会回退 TickFlow"}
        symbols = symbols or ["600519.SH"]
        df = self.get_minute(symbols, None, None, freq="1m")
        preview = [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
                   for row in df.tail(3).to_dicts()]
        return {"provider": self.name, "dataset": dataset, "rows": df.height,
                "columns": df.columns, "preview": preview}
