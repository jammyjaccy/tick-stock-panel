"""腾讯 1 分钟 K 插件: 解析口径 / 窗口过滤 / 边界 (不联网)。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.plugins.tencent_minute import provider as tm

CST = ZoneInfo("Asia/Shanghai")


def _payload(bars: list[list], code: str = "sh600519") -> dict:
    return {"code": 0, "data": {code: {"m1": bars}}}


def _bar(text: str, o: str, c: str, h: str, l: str, v: str) -> list:
    # 腾讯行序: 时间, 开, 收, 高, 低, 量(手), {}, 涨跌幅
    return [text, o, c, h, l, v, {}, "0.5"]


def test_parse_minute_ohlc_order_and_units():
    payload = _payload([
        _bar("202611190930", "14.74", "14.74", "14.80", "14.70", "27234.00"),
        _bar("202611190931", "14.74", "14.70", "14.75", "14.68", "1234.00"),
    ])
    rows = tm.parse_minute(payload, "sh600519", "600519.SH")

    assert len(rows) == 2
    first = rows[0]
    assert first["symbol"] == "600519.SH"
    assert first["datetime"] == datetime(2026, 11, 19, 9, 30)
    assert (first["open"], first["close"], first["high"], first["low"]) == (14.74, 14.74, 14.80, 14.70)
    # 量单位为手, 原样透传; 无成交额 -> None (不伪造)
    assert first["volume"] == 27234.0
    assert first["amount"] is None


def test_parse_minute_skips_bad_rows():
    payload = _payload([
        ["bad-time", "1", "1", "1", "1", "1", {}, "0"],
        _bar("202611190932", "", "14.7", "14.8", "14.6", "10"),
        _bar("202611190933", "14.7", "14.7", "14.8", "14.6", "10"),
    ])
    rows = tm.parse_minute(payload, "sh600519", "600519.SH")
    assert [r["datetime"].minute for r in rows] == [33]


def test_get_minute_filters_window_and_normalizes_timezone(monkeypatch):
    bars = [_bar("202611190930", "1", "1", "1", "1", "1"),
            _bar("202611191130", "2", "2", "2", "2", "2")]
    provider = tm.TencentMinuteProvider()
    monkeypatch.setattr(tm.time, "sleep", lambda *_: None)
    monkeypatch.setattr(provider, "_fetch", lambda code: _payload(bars))

    start = datetime(2026, 11, 19, 10, 0, tzinfo=CST)
    df = provider.get_minute(["600519.SH"], start_time=start, end_time=None)

    # 带时区的入参必须换成北京墙钟再比较: 09:30 那根被排除, 11:30 保留
    assert df.height == 1
    assert df["datetime"][0] == datetime(2026, 11, 19, 11, 30)


def test_get_minute_rejects_non_1m_freq(monkeypatch):
    provider = tm.TencentMinuteProvider()
    called: list[str] = []
    monkeypatch.setattr(provider, "_fetch", lambda code: called.append(code) or {})
    df = provider.get_minute(["600519.SH"], freq="5m")
    assert df.is_empty() and "datetime" in df.columns
    assert called == []  # 不能用 1m 冒充其它周期


def test_get_minute_skips_unknown_symbol_and_survives_failure(monkeypatch):
    provider = tm.TencentMinuteProvider()
    monkeypatch.setattr(tm.time, "sleep", lambda *_: None)

    def fake_fetch(code: str) -> dict:
        if code == "sh600519":
            raise RuntimeError("网络失败")
        return _payload([_bar("202611190930", "1", "1", "1", "1", "1")], code)

    monkeypatch.setattr(provider, "_fetch", fake_fetch)
    monkeypatch.setattr(provider, "_fetch_trend", lambda code: {"data": {}})
    df = provider.get_minute(["600519.SH", "510300.SH", "bad"], None, None)

    assert df["symbol"].to_list() == ["510300.SH"]
    assert df["amount"].to_list() == [None]


def test_to_tencent_code():
    assert tm.to_tencent_code("600519.SH") == "sh600519"
    assert tm.to_tencent_code("000001.SZ") == "sz000001"
    assert tm.to_tencent_code("920009.BJ") == "bj920009"
    assert tm.to_tencent_code("600519") == "sh600519"
    assert tm.to_tencent_code("bad") is None


def test_parse_cumulative_amount_diffs_to_per_minute():
    payload = {"data": {"sh600519": {"data": {
        "date": "20261119",
        "data": [
            "0930 14.74 27234 4010000.00",
            "0931 14.70 30000 4420000.00",
            "0932 14.72 31000 4560000.00",
        ],
    }}}}
    out = tm.parse_cumulative_amount(payload, "sh600519")

    assert out[datetime(2026, 11, 19, 9, 30)] == 4_010_000.0
    assert out[datetime(2026, 11, 19, 9, 31)] == 410_000.0
    assert out[datetime(2026, 11, 19, 9, 32)] == 140_000.0


def test_parse_cumulative_amount_empty_or_bad_date():
    assert tm.parse_cumulative_amount({"data": {}}, "sh600519") == {}
    assert tm.parse_cumulative_amount({"data": {"sh600519": {"data": {}}}}, "sh600519") == {}


def test_get_minute_fills_amount_only_for_today(monkeypatch):
    from datetime import date

    today = date.today()
    bars = [["202611190930", "1", "1", "1", "1", "1", {}, "0.5"]]
    trend = {"data": {"sh600519": {"data": {"date": "20261119",
                                             "data": ["0930 1 100 500.00"]}}}}
    provider = tm.TencentMinuteProvider()
    monkeypatch.setattr(tm.time, "sleep", lambda *_: None)
    monkeypatch.setattr(provider, "_fetch", lambda code: _payload(bars))
    monkeypatch.setattr(provider, "_fetch_trend", lambda code: trend)

    df = provider.get_minute(["600519.SH"], None, None)

    if today.isoformat() == "2026-11-19":  # 固定日期样本只有在这一天才走成交额分支
        assert df["amount"].to_list() == [500.0]
    else:
        assert df["amount"].to_list() == [None]
