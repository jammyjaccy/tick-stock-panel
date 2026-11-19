"""扩展数据源: 交易所前缀代码 (sh600000) 归一化为 600000.SH。

背景 (T-05 板块资金流): 新浪资金流接口的 symbol 是 "sh600000"/"sz000001"/"bj920418"
形式, 扩展表的 symbol_map 走 normalize_symbol, 旧实现只认 "600000" 与 "600000.SH",
前缀形式会原样落表, 与成分股维表匹配不上, 资金流整列变空。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.services.ext_data import normalize_symbol


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sh600000", "600000.SH"),
        ("sz000001", "000001.SZ"),
        ("bj920418", "920418.BJ"),
        ("SH600519", "600519.SH"),
        ("sh510330", "510330.SH"),
    ],
)
def test_exchange_prefixed_symbol_is_normalized(raw: str, expected: str) -> None:
    out = normalize_symbol(pl.Series([raw]))
    assert out.to_list() == [expected]


def test_already_normalized_symbol_is_untouched() -> None:
    out = normalize_symbol(pl.Series(["600000.SH", "000001.SZ", "920418.BJ"]))
    assert out.to_list() == ["600000.SH", "000001.SZ", "920418.BJ"]


def test_bare_code_still_uses_lookup_first() -> None:
    """纯 6 位代码仍优先查维表 (北交所 8 开头不能用 6 开头规则猜)。"""
    out = normalize_symbol(pl.Series(["920418"]), {"920418": "920418.BJ"})
    assert out.to_list() == ["920418.BJ"]


def test_non_symbol_text_is_untouched() -> None:
    """非代码文本不能被前缀规则误改 (例如 "shanghai" / "sz" 开头的名称)。"""
    out = normalize_symbol(pl.Series(["shanghai", "sz", "60000"]))
    assert out.to_list() == ["shanghai", "sz", "60000"]
