"""腾讯五档盘口 provider。

契约 (docs/plugin-development.md):
    get_depth_batch(symbols) -> {
        "600519.SH": {
            "bid_prices": [买一..买五], "bid_volumes": [...],
            "ask_prices": [卖一..卖五], "ask_volumes": [...],
            "timestamp": 毫秒时间戳,
        }
    }

实测口径 (2026-11-18 M0):
  - 行情串按 ~ 切分, 9-18 = 买一价,量 .. 买五价,量; 19-28 = 卖一价,量 .. 卖五价,量
  - 量单位为手 (沪深北一致; 与字段 35 的"科创板按股"不同)
  - 字段 30 为服务端时间 YYYYMMDDHHMMSS (收盘后仍会更新, 不是最后成交时间)
  - 一次请求上限约 900 只 (URL 长度), 服务层按 100 只/批切片, 本层不再分批
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_URL = "https://qt.gtimg.cn/q="
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
_CST = ZoneInfo("Asia/Shanghai")


def to_tencent_code(symbol: str) -> str | None:
    """600519.SH -> sh600519; 920009.BJ -> bj920009."""
    code, _, exchange = symbol.strip().upper().partition(".")
    if not (code.isdigit() and len(code) == 6):
        return None
    exchange = exchange.lower()
    if exchange not in ("sh", "sz", "bj"):
        # 无后缀时按首位推断: 6 -> 沪, 其余 -> 深
        exchange = "sh" if code.startswith("6") else "sz"
    return f"{exchange}{code}"


def _to_ms(text: str) -> int:
    try:
        dt = datetime.strptime(text.strip(), "%Y%m%d%H%M%S").replace(tzinfo=_CST)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return int(time.time() * 1000)


def _price(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _lot(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_depth(text: str, symbol_by_code: dict[str, str]) -> dict[str, dict]:
    """解析腾讯行情串; symbol_by_code 是 {sh600519: 600519.SH}."""
    out: dict[str, dict] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        code = key[2:].strip().lower()
        symbol = symbol_by_code.get(code)
        if not symbol:
            continue
        fields = raw.strip().strip('"').split("~")
        if len(fields) < 29:
            continue
        bid_prices = [_price(fields[i]) for i in (9, 11, 13, 15, 17)]
        bid_volumes = [_lot(fields[i]) for i in (10, 12, 14, 16, 18)]
        ask_prices = [_price(fields[i]) for i in (19, 21, 23, 25, 27)]
        ask_volumes = [_lot(fields[i]) for i in (20, 22, 24, 26, 28)]
        # 两侧价格全 0: 停牌或无盘口, 不写入 (避免把缺失当成"封单 0")
        if not any(bid_prices) and not any(ask_prices):
            continue
        out[symbol] = {
            "bid_prices": bid_prices,
            "bid_volumes": bid_volumes,
            "ask_prices": ask_prices,
            "ask_volumes": ask_volumes,
            "timestamp": _to_ms(fields[30]) if len(fields) > 30 else int(time.time() * 1000),
        }
    return out


class TencentDepthProvider:
    """只提供 depth5 的免费数据源。"""

    name = "tencent_depth"
    builtin = True

    def __init__(self) -> None:
        self.config = SimpleNamespace(datasets={"depth5": {}})

    def close(self) -> None:  # loader 重建注册表时会调用
        return None

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        symbol_by_code: dict[str, str] = {}
        for symbol in symbols:
            code = to_tencent_code(symbol)
            if code:
                symbol_by_code[code] = symbol
        if not symbol_by_code:
            return {}
        url = _URL + ",".join(symbol_by_code)
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=15.0)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"腾讯五档请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"腾讯五档 HTTP {resp.status_code}")
        data = parse_depth(resp.content.decode("gbk", "replace"), symbol_by_code)
        logger.debug("腾讯五档: 请求 %d 只, 返回 %d 只", len(symbol_by_code), len(data))
        return data

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "depth5":
            return {"provider": self.name, "dataset": dataset, "error": "该源只提供 depth5"}
        rows = self.get_depth_batch(symbols or ["600519.SH"])
        preview = [{"symbol": sym, **values} for sym, values in list(rows.items())[:3]]
        return {
            "provider": self.name,
            "dataset": "depth5",
            "rows": len(rows),
            "columns": ["symbol", "bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "timestamp"],
            "preview": preview,
        }
