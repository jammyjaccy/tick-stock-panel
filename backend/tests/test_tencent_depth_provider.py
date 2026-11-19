"""腾讯五档插件: 字段解析与代码映射 (不联网)。"""
from __future__ import annotations

from app.plugins.tencent_depth import provider as td


def _line(code: str, *, bids=(), asks=(), ts="20261118153331") -> str:
    fields = [""] * 88
    fields[0] = "1"
    fields[1] = "样本"
    fields[2] = code[-6:]
    fields[3] = "10.00"
    # 9-18 买一价,量 ... 买五价,量; 19-28 卖一价,量 ... 卖五价,量
    for i, (price, vol) in enumerate(list(bids) + [(0, 0)] * (5 - len(bids))):
        fields[9 + i * 2] = str(price)
        fields[10 + i * 2] = str(vol)
    for i, (price, vol) in enumerate(list(asks) + [(0, 0)] * (5 - len(asks))):
        fields[19 + i * 2] = str(price)
        fields[20 + i * 2] = str(vol)
    fields[30] = ts
    return f'v_{code}="' + "~".join(fields) + '";'


def test_parse_depth_pairs_and_timestamp():
    text = _line(
        "sh600519",
        bids=[(1442.66, 7), (1442.65, 5), (1442.63, 79)],
        asks=[(1443.00, 1), (1443.39, 10)],
    )
    out = td.parse_depth(text, {"sh600519": "600519.SH"})

    row = out["600519.SH"]
    assert row["bid_prices"][:3] == [1442.66, 1442.65, 1442.63]
    assert row["bid_volumes"][:3] == [7, 5, 79]
    assert row["ask_prices"][:2] == [1443.0, 1443.39]
    assert row["ask_volumes"][:2] == [1, 10]
    # 2026-11-18 15:33:31 CST
    assert row["timestamp"] == 1794987211000


def test_parse_depth_skips_empty_book():
    text = _line("sz000001")  # 买卖两侧全 0
    assert td.parse_depth(text, {"sz000001": "000001.SZ"}) == {}


def test_parse_depth_ignores_unknown_code():
    text = _line("sh600000", bids=[(10.0, 1)])
    assert td.parse_depth(text, {"sz000001": "000001.SZ"}) == {}


def test_to_tencent_code():
    assert td.to_tencent_code("600519.SH") == "sh600519"
    assert td.to_tencent_code("000001.SZ") == "sz000001"
    assert td.to_tencent_code("920009.BJ") == "bj920009"
    assert td.to_tencent_code("600519") == "sh600519"
    assert td.to_tencent_code("bad") is None
