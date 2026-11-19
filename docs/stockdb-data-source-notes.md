# stockdb (free-stockdb) 数据源评估与补数方案

更新时间: 2026-11-17
状态: **评估完成, 未执行任何写入**. 备份与同步均待批准, 面板侧接入未开工.
本文全部数值为当日实测, 非照抄文档.

---

## 一. 这是什么

第三方离线行情库, 装在 `/home/workspace/stockdb`, 与通达信 / TickFlow / 扶摇均无关.

| 项 | 值 |
|---|---|
| 产品 | free-stockdb v0.3.5 more-power (manylinux-x64) |
| 首次安装 | 2026-10-14 (另有 `运维说明.md` 记录安装细节) |
| 服务端口 | 7899 (LGDB, LevelDB + HTTP/TCP), `0.0.0.0` 无鉴权 |
| 数据源 | `sync_url.txt`: `ah.123128.xyz` (once) / `ad.123128.xyz always` |
| 同步器 | `./数据更新` (ELF, 不支持 `--help`, **忽略 SIGTERM**) |
| 服务状态 | **当前已停**, 无 pidfile, 7899 未监听 |

目录分工:

```
data    22 G   677 个 .ldb   日K 为主, once 源, 由 data/disable 冻结
data1  3.0 G    95 个 .ldb   分钟K 为主, always 源, 每次同步以服务端列表为准
mydb    24 K                 私有可写空间, 当前为空
```

该库**不是** tdxquant 积分买来的数据. 证据: 数据文件落盘于 2026-10-14 21:40,
早于面板 tdxquant 分钟同步 (2026-11-17 19:00-19:20); 且覆盖 429 天,
按 0.4 分/只/天折算全市场规模约 90 万积分, 与账户余额 1 万分不符.

---

## 二. 实测能力 (2026-11-17)

| 能力 | 结论 | 证据 |
|---|---|---|
| 日K | 有, 字段丰富 | 2026-10-14 全市场 7,598 行 (A股 5,213: 0\* 1,491 / 3\* 1,407 / 6\* 2,315; 基金债 1\* 1,020 / 5\* 1,057 / 9\* 308) |
| 日K 深度 | 2000 年至今 | 600633 5,452 条 (2000-01-07 起); 000001 6,326 条 |
| 日K 字段 | open/high/low/close/pre_close/volume/amount/turnover/pct_chg/amplitude/is_st/vol_ratio/total_share/float_share/total_mv/float_mv/pe_ttm/pb | `rd.get("日k", "600633", "20261014")` |
| 分钟K | 有, **1 分钟**, 429 天 | 600633 103,408 条, 2025-01-02 ~ 2026-10-14, 每日 242 根 |
| 分钟K 覆盖 | 含 ETF | 510300 95,199 条 / 159915 79,292 条 |
| 复权 | 前/后复权均可算 | 茅台 hfq 收 9,577.17 vs 原始 1,252.90 |
| 指数 | **无** | `日k:399001` / `000300` / `sh000001` 全 0 条 |
| 财务 | **无** | `财务` / `财务报表` 等表名返回空 |
| 实时 / 五档 / 逐笔 | **无** | 纯离线库 |
| 板块概念 | 未确认 | 表名猜测全部返回空 |

静态探测无效: .ldb 块被 snappy 压缩, 直接 grep 表名 0 命中, 必须启服务查.

---

## 三. 与面板口径的差异 (实测)

两边**没有重叠交易日** (面板分钟仅 11-12/13/16/17; stockdb 到 10-14), 故采用
"各自分钟合计 vs 自家日K" 的判据, 并用换手率反推单位.

### 1. 成交量单位: 股 vs 手 (差 100 倍)

```
stockdb 600633 2026-10-14:  amount 529,390,000 / volume 49,116,000 = 10.78  ~ close 10.86
面板    600633 2026-11-13:  amount 237,366,212 / volume    217,543 = 1,091  ~ close 10.82 x 100
```

换手率交叉验证 (决定性):

```
面板 volume 217,543 x 100 = 21,754,300 股
21,754,300 / 流通股 1,268,074,472 = 1.7155%
面板存储 turnover_rate            = 1.7155380...   完全吻合
```

结论: 面板 `volume` = **手**; stockdb (`rd` 与在线 API 一致) = **股**.
接入时 `volume / 100`; `amount` 两边都是元, 不动.

### 2. 分钟时间戳: 起点标签 vs 终点标签

```
本地 .ldb:  242 根/日, 首 09:30, 末 15:00   -> 需 +1 分钟
在线 API:   240 根/日, 首 09:31, 末 15:00   -> 与面板一致, 不用挪
面板:       240 根/日, 首 09:31, 末 15:00
```

### 3. 分钟合计 vs 日K: 本地库略欠

| 标的/日期 | 分钟合计 / 日K | 缺口 |
|---|---|---|
| 600633 10-13 | 17,917,600 / 17,921,600 | 0.022% |
| 600633 10-14 | 49,082,100 / 49,116,000 | 0.069% |
| 000001 10-13 | 99,425,400 / 99,493,700 | 0.069% |
| 000001 10-14 | 96,745,100 / 96,908,200 | **0.168%** |
| 面板 600633 11-13 | 217,544 / 217,543 | +0.0005% |

深市标的缺口明显大于沪市, **推测**为盘后固定价格交易 (15:05-15:30) 计入日K
但不在分钟里. 未验证. 接入时须明确取舍 (以日K量为基准或接受差异).

---

## 四. 补 24 个交易日缺口的方案

缺口: **2026-10-15 ~ 2026-11-17, 共 24 个交易日** (由面板 `kline_daily` 分区数出).

### 云源可达性 (2026-11-17 实测)

```
ah.123128.xyz          TLS 通, HTTP 404 (同步源, once)
ad.123128.xyz          TLS 通, HTTP 404 (同步源, always)
a.123128.xyz           HTTP 200 (网页入口)
mapi_url = 106.75.217.246:12328   TCP 通, HTTP 204
```

`mapi_url` 是 SDK 在线接口要连的地址, 存在本地库中 (不设则报 "API IP地址未配置").

### 路径 A: 跑 `./数据更新` (官方同步, 未执行)

- 拉增量 `.ldb` 到 data1, 断点续传, 自动补齐失败分片
- 首屏打印 "待下载资源数 / 待删除资源数", 可在下载前判断规模
- **不可优雅中止** (忽略 SIGTERM), 只能 `kill -9`
- 未知: 设备鉴权是否仍有效 (上次 10-14 显示 "状态:有效");
  新日期日K是否走 data1 增量 (data1 实测 "日K 命中 0")

### 路径 B: 在线 API 逐只拉 (已验证可用)

实测 `get_bars("000001", count=260, unit="1m", end_dt="2026-11-17 15:00:00")`:

```
日K 到 2026-11-17 (close 12.25), 分钟K 到 2026-11-17 15:00
240 根/日, 首 09:31, 末 15:00  -> 与面板口径一致
字段 date/open/high/low/close/volume/money
volume 为股 (money/volume = 12.23 ~ close)
```

限制: 官方文档明写 "**不得进行大批量分钟行情拉取**", 批量代码请求会返回限制错误.
补 24 天 x 5,213 只需 5,213 次单标的请求, 属明确不鼓励用法.
`count` 语义是 "最近 N 根", 不是日期区间.

---

## 五. 风险与备份方案 (**未执行**)

| # | 风险 | 证据 | 后果 | 可恢复 |
|---|---|---|---|---|
| 1 | 同步器删除本地文件 | `sync.log:13` 首轮打印 "待删除资源数: 1个"; 二进制含 `remove` | 若源端轮转旧文件, 本地对应 .ldb 被删且源端不再提供, 历史永久丢失 | 否 |
| 2 | 只能 kill -9 | 运维文档实测忽略 SIGTERM | 留下半截下载 + 半一致 MANIFEST | 是, 下次同步补齐 |
| 3 | 日K 可能补不到 | data 为 once 且已 disable; data1 日K 命中 0 | 白跑一趟 | 是, 在线 API 可补 |
| 4 | 设备鉴权过期 | 上次 10-14 "状态:有效", 现状未知 | 直接退出 | 是, 零损失 |
| 5 | 增量超出预期 | 未知 | 多花时间 | 是, /home 有 1.5T 可用 |

只有第 1 条不可恢复. 暴露面仅 `data1` (3.0 G):

- `data` 22 G 有 `data/disable` 挡着, 源 0 直接跳过, 不碰 (运维文档 §5 亦警告勿删 disable)
- 面板 `/home/workspace/tickflow-data` 完全不涉及

### 待执行的备份命令 (尚未运行)

```bash
cp -a /home/workspace/stockdb/data1 /home/workspace/backups/stockdb-data1-20261117
```

期望: 数十秒内结束, `du -sh` 为 3.0G, 文件数 101. `cp -a` 只读源.

备份完成后, "最坏结果"从 "丢 429 天历史" 降级为 "白忙 + 从备份还原".

---

## 六. 面板侧接入 (未开工, 待决策)

现状: 扶摇声明 `datasets: [realtime, daily, adj_factor, financial]`, 无 minute;
tdxquant 声明 `[realtime, depth5, daily, adj_factor, minute, full_minute]`, 按积分计费.

**因此分钟K 无法路由到扶摇** — 硬改成 fuyao 会 `provider_has_dataset` 为 False 并自动回退
tickflow, 而 TickFlow 当前档位 none, 结果直接不可用.

两条接入路线:

| 方案 | 做法 | 代价 |
|---|---|---|
| A. 写插件 | 仿 fuyao/tdxquant, `datasets: [minute]`, 直连 127.0.0.1:7899 | 正统, 走 `capabilities.py` 路由; 需吃透 minute 契约 |
| B. 薄适配层 + YAML 自定义源 | 小 HTTP 服务把 stockdb 包成 `?symbols=&start_time=&end_time=` | 不动核心; 多一个常驻进程; 自定义源无 URL 占位符, 查询模型会别扭 |

倾向 A. 选 A 前须先读 `backend/app/data_providers/base.py` 与 `minute_normalizer.py`,
确认 242/240 根、正负 1 分钟偏移、手/股换算能在插件层吸收而不改核心.

另需决定:
- 面板现有 4 天 tdxquant 分钟数据与 stockdb 不同源, 混存如何处理 (重拉 / 标记来源)
- stockdb 服务需 systemd user service 常驻; 当前 `0.0.0.0` 无鉴权, 建议改 `127.0.0.1` + `readonly`

---

## 七. 复现方式

```bash
# 启动 / 停止 (必须在程序目录内, work_dir 与 pidfile 是 ./ 相对路径)
cd /home/workspace/stockdb && ./stockdb -d ./stockdb.conf -s start
cd /home/workspace/stockdb && ./stockdb -d ./stockdb.conf -s stop

# 就绪判断 (加载 25G 约 10 秒)
ss -ltn | grep 7899

# 查询 (需该环境, stockdb 模块经 .pth 暴露)
/home/workspace/envs/free-stockdb/bin/python -c "
import stockdb; rd = stockdb.rd
print(rd.keys('日k', '600633', '2026*'))"
```

坑: `rd.get()` 返回 `QueryResult`, 不是 list/dict. 下标或切片会得到
`'Missing required parameters'`. 取值用 `rd.vals(...)` / `rd.keys(...)` /
`.get("字段,字段")` 投影, 或用 `list(...)` 转换.

在线接口:

```python
from stock_sdk import set_init, get_bars
set_init("106.75.217.246:12328", df=False)
get_bars("000001", count=240, unit="1m",
         fields=["date","open","close","volume","money"],
         end_dt="2026-11-17 15:00:00")
```

注: 本次评估中反复启停 stockdb 导致 LevelDB 重写元数据
(`MANIFEST-005368` -> `MANIFEST-005612`, `LOG`/`LOG.old`), `.ldb` 数据文件未改动.
