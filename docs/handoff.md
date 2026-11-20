# 交接说明 (HANDOFF)

给在 `/home/workspace/tickflow-stock-panel` 新开会话的 agent 或人.
先读本文件, 再做任何改动. 详细数据口径与验收记录在:
- `docs/data-inventory.md` — 数据资产总清单 (单一事实来源)
- `docs/data-tickets.md` — 工单 T-01 ~ T-15 与验收证据

更新时间: 2026-11-20 17:40

---

## 一. 一句话现状

面板已部署, 局域网 `http://192.168.1.9:3018`.
**日K / 除权因子 / 指标 / 指数日K / 市场环境 / 主线 覆盖 2016-11 至今**;
**财务核心指标 8 期**; **分钟 K 从 2026-11-19 起每日自动落盘**;
五档封单与分钟都走腾讯免费源; **板块资金流走新浪, 盘中每 10 分钟一盘** (T-05).
剩下的是: 复盘页日期选择 (卡前端构建), 财务公告日 (候补), 板块轮动盘中分钟聚合 (另议).

## 二. 部署形态 (改动容器前必读)

- 容器名: `tsp`
- 镜像: `ghcr.io/shy3130/tick-stock-panel@sha256:d1230fb1ea0e4d533d2e3b09fdbdc1563acf41950cb75bff1508aeadb4b50f83`
- 数据目录: `/home/workspace/tickflow-data` (容器内 `/app/data`, 属主 root)
- 镜像内是 v0.3.37; 当前用挂载补丁叠加本仓库改动, 尚未自建镜像

### 完整启动命令 (重建容器必须一字不差)

```bash
docker run -d --name tsp \
  -p 0.0.0.0:3018:3018 \
  -e DATA_DIR=/app/data \
  --dns 223.5.5.5 --dns 119.29.29.29 \
  -v /home/workspace/tickflow-data:/app/data \
  -v /home/workspace/tickflow-stock-panel/backend/app/plugins/fuyao/client.py:/app/app/plugins/fuyao/client.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/plugins/fuyao/provider.py:/app/app/plugins/fuyao/provider.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/services/instrument_sync.py:/app/app/services/instrument_sync.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/services/ext_data.py:/app/app/services/ext_data.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/services/sector_rotation.py:/app/app/services/sector_rotation.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/services/depth_service.py:/app/app/services/depth_service.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/plugins/tencent_depth:/app/app/plugins/tencent_depth:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/plugins/tencent_minute:/app/app/plugins/tencent_minute:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/api/settings.py:/app/app/api/settings.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/api/indices.py:/app/app/api/indices.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/tests:/app/tests:ro \
  --restart unless-stopped \
  ghcr.io/shy3130/tick-stock-panel@sha256:d1230fb1ea0e4d533d2e3b09fdbdc1563acf41950cb75bff1508aeadb4b50f83
```

**十一个挂载, 少一个就失效**:

| 挂载 | 作用 |
|---|---|
| fuyao/client.py | 429/5xx 退避重试, 实时快照不重试 |
| fuyao/provider.py | 单标的节流 0.3 秒; 指数历史逐只拉 (0.6 秒), 股->手 |
| instrument_sync.py | 单市场 503 时沿用历史维表, 不再抹掉北交所 |
| services/ext_data.py | 扩展源认 `sh600000` 前缀代码 (新浪资金流用) |
| services/sector_rotation.py | 残缺分钟分区回退到完整交易日, 防止板块轮动塌成一根线 |
| services/depth_service.py | 收盘后五档量清零时, 定版沿用盘中最后一盘 |
| tencent_depth/ | 腾讯五档插件 (depth5) |
| tencent_minute/ | 腾讯分钟插件 (minute, 个股/指数/成交额) |
| api/settings.py | 手动修正等门禁按"生效源"判断能力 |
| api/indices.py | 指数日K同步门禁改为 capability_gate("daily") |
| backend/tests | 容器内跑 pytest 用, 可省 |

**两个必须记住的坑**:
1. `--dns` 必须带. 2026-11-19 14:37 容器 DNS 跟着宿主网络漂移失效, 全站外网请求挂掉,
   15:02 五档定版与 15:35 管道静默失败.
2. 单文件挂载跟 inode: 宿主上"写临时文件再 rename"的编辑器改完, 容器里还是旧内容,
   必须 `docker rm -f` + `docker run` 重建; 目录挂载不受影响.

## 三. 代码改动 (已提交到 main, 未 push)

| 提交 | 内容 |
|---|---|
| `59c9446c` | fuyao 429 退避重试 + 单标的节流 0.3 秒 |
| `0296c0f1` | 部署结果与数据源结论文档 |
| `235fa964` | 腾讯五档 depth5 插件 (+4 测试) |
| `e3730a51` | 腾讯分钟插件 (OHLCV + 当日累计额差分成交额, +9 测试) |
| `70762532` | 单市场 503 时沿用历史维表 (+1 测试) |
| `712d871f` | 指数历史日K 逐只拉取 + 股转手 (+3 测试) |
| `8a2f35dd` | 手动修正/指数同步门禁按生效源判断 |
| `621e8b58` | 数据资产清单 / 工单 / 交接文档 |
| `ad7b2d01` | 扩展源认 `sh600000` 前缀代码 (+8 测试) |
| `9c662087` | 残缺分钟分区回退到完整交易日 (+6 测试) |
| `10fa40f5` | 收盘后五档量清零时定版沿用盘中最后一盘 (+6 测试) |

测试 (容器内, 实测):

```bash
docker exec tsp sh -lc "cd /app && uv run --quiet --with pytest \
  python -m pytest tests/test_ext_*.py -q"
# 430 passed, 6 skipped
```

一次性容器跑单文件 (不重启服务):

```bash
docker run --rm \
  -v .../backend/app/services/<file>.py:/app/app/services/<file>.py:ro \
  -v .../backend/tests:/app/tests:ro \
  <image> sh -lc "cd /app && uv run --quiet --with pytest --with pytest-asyncio \
    python -m pytest tests/<test>.py -q"
```

## 四. 当前数据 (2026-11-19 22:20 实测)

```
标的维表     5,893 只 (沪 2,469 / 深 3,068 / 北 356)
日K          2,432 天 / 约 10,418,000 行, 2016-11-16 ~ 2026-11-20
指标 enriched 2,432 天 / 10,418,493 行, 15 列
除权因子     35,462+ 行, 2016-11-18 ~ 2026-11-20
指数日K      599 只 / 2,648 天 / 1,409,690+ 行, 2016-01-04 起 (569 只有历史)
指数指标     2,648 天 / 12 列
市场环境     2,432 天 (2016-11-16 起)
市场主线     64,084 行, 到 2026-11-20
财务三表     5,580 只 x 8 期; 核心指标 43,456 行 / 8 期 (2024-12-31 ~ 2026-09-30); 股本 0
分钟K        6 天 (11-12/13/16/17/19/20); 11-20 = 1,256,624 行 / 5,216 只 / 09:30~15:00
板块资金流   36 盘 / 232,344 行 (2026-11-20); 每盘 6,454 条, 覆盖维表 99.42%
五档封单     11-18: 91 行, 11-19: 111 行; **11-20 缺失** (定版 bug, 已修, 11-23 复验)
龙虎榜       最近 60 个交易日 (2026-08-19 ~ 11-18 回补 + 按需)
扩展表       概念 5,584 行 / 行业 5,580 行 (每日快照, 无历史)
```

注意: `kline_daily` 里有 242 个日期用的是 `part-stream-*.parquet` 分片 (不是 `part.parquet`),
写统计脚本要 glob `date=*/*.parquet`, 只找 `part.parquet` 会误判成缺数据.

### 能力路由 (设置页可改)

```
日K        fuyao          可用
除权因子   fuyao          可用
实时行情   fuyao          可用 (6 秒轮询)
财务       fuyao          可用 (指标单股单期)
五档盘口   tencent_depth  可用 (免费)
分钟K      tencent_minute 可用 (最近约 320 根 + 当日成交额)
全量分钟   tickflow       不可用
板块资金流 ext_fund_flow_sina 可用 (新浪扩展表, 盘中每 10 分钟一盘)
TickFlow 档位: none (未付费)
```

### 关键 preferences

```
pipeline_regime_enabled   = true
minute_sync_enabled       = true
minute_sync_days          = 1
minute_data_provider      = tencent_minute
depth5_data_provider      = tencent_depth
limit_ladder_monitor_enabled = false   (11-19 盘中临时开过, 收盘已关)
depth_polling_interval    = 10.0
```

## 五. 工单状态

| 票 | 内容 | 状态 |
|---|---|---|
| T-01 | 日K 历史导入 | 已验证 (2,431 天) |
| T-02 | 除权因子历史 | 已验证 (35,462 行) |
| T-03 | 指标/环境/主线重算 | 已验证 |
| T-04 | 五档封单 (腾讯) | 盘中已验证 (11-20 全天 5 个采样点, age 3~7 秒); **定版 bug 已修, 11-23 复验落盘** |
| T-05 | 板块资金流 | **已验证** (新浪扩展表; 36 盘 / 232,344 行; flow_available=true) |
| T-06 | 龙虎榜回补 | 数据完成; 页面入口待 T-12 |
| T-07 | 指数分时 | 已验证 (页面确认) |
| T-08 | 指数K线历史 | 已验证 (2,647 天, 30 只扶摇不支持) |
| T-09 | 财务指标历史 | 已验证 (8 期 / 43,456 行) |
| T-10 | 概念/行业历史归属 | 未做, 可能关票 |
| T-11 | 分钟历史 | 已被 T-15 替代 |
| T-12 | 复盘页龙虎榜日期选择 | 卡在 pnpm/前端构建 |
| T-13 | 财务 announce_date 假公告日 | 候补 |
| T-14 | 腾讯分钟源 | 已验证 (页面确认) |
| T-15 | 分钟历史落盘 | 已验证 (125.7 万行/241 根/额 100%) |

## 六. 用户目标与对应功能

1. **盘中找机会** -> 异动监控 / 监控中心 / 市场环境 / 连板梯队 + 五档封单 (已可用).
2. **做T** -> 批次页 (`frontend/src/custom/lots/Page.tsx`, 逻辑 `backend/app/strategy/lots.py`),
   分时图现在有真实数据 (腾讯).
3. **选股/回测** -> 策略 / 因子 / 回测; 10 年日K + 8 期财务 + 每日分钟累积.

## 七. 踩过的坑 (别再踩一遍)

### 1. fuyao 429 导致整轮同步归零
回落接口节流原是 0.12 秒, 实测 111 请求/分钟撞配额; client 当时无重试.
现为 0.3 秒 + 退避重试 (10/20/30 秒, 尊重 Retry-After).

### 2. TdxQuant 没有积分
分钟批量约 0.4 分/只/天, 账户已耗尽. **不要再尝试用 TdxQuant 批量拉分钟/五档**.

### 3. 读日K 会触发自动补数 (会写盘)
`GET /api/kline/daily` 在请求区间本地完全无数据时, 会走 `daily_bootstrap.ensure_history`
从扶摇拉取并**落盘**. 随便点一下历史日期也会产生写入.

### 4. 数据页的统计会滞后 30~60 秒
`/api/data/status` 有内存缓存 + 后台刷新; 看 `stats_pending` 或直接查磁盘.

### 5. 东财接口约 9~35 次请求后断开, 短期不恢复
2026-11-19 实测: 100 次 paced 请求 (0.35 秒间隔) 只成功 **14** 次, 第 9 次起
`RemoteDisconnected`. **不要再用东财做全市场批量**.

### 6. 前端目前无法构建
本机有 Node 26, 但**没有 pnpm / corepack / npm**, `frontend/node_modules` 不存在.
任何前端改动 (T-12 日期选择) 需要先装包管理器并构建 (安装位置需 Jimmy 决定, H1).

### 7. 本机 curl 必须加 `--noproxy '*'`
否则被 http_proxy 截走, 返回 HTTP 000 (会误判成服务没起来).

### 8. 指数成交量单位 (股 vs 手)
扶摇 `historical` 的指数 volume 是**股**, 面板内部统一**手**; 但快照接口的指数 volume
已经是手. T-08 已把历史统一 `floor(股/100)`, 并修掉存量 2026-11-17/18 的 8 只核心指数.
判断口径: `volume / amount` 约 0.0007 手/元 (约 13 元/股) 才对.

### 9. 指数日K同步会跑 599 次请求
`iter_daily(asset_type="index")` 逐只拉, 0.6 秒/只; 每日管道指数阶段约 10 分钟. 属预期成本.

### 10. 腾讯分钟: 320 根 + 成交额要另一个接口
`mkline` 只回最近约 1.3 个交易日, 无成交额; 成交额来自 `minute/query` 的当日累计额差分.
**必须用 `ifzq.gtimg.cn`, 不要用 `web.ifzq` 子域名 (会返回 501)**. 北交所无分钟.

### 11. 财务表 announce_date 是假公告日
扶摇三表的 `report_date_ms` = "该季度例行披露的月日 + 当前年份":
600519 的 2025-09-30 期显示 2026-10-31. 影响财务页展示与按时点回测. 候补 T-13, 未修.

### 12. 容器 DNS 跟着宿主网络漂移
见第二节. 症状: `Temporary failure in name resolution`, 实时/五档/分钟全挂, 管道静默失败.
修法: 重建容器带 `--dns 223.5.5.5 --dns 119.29.29.29`; 重建后启动自检会补当日日K/指标/指数/环境/主线,
五档定版手动补一次.

### 13. 单文件挂载跟 inode
编辑器写临时文件再 rename 后, 容器内仍是旧内容. 改完必须重建容器.

### 14. `repair_date` 模式只补缺失记录
`sync_and_persist_minute(repair_date=...)` 不会重写已存在的 (symbol, datetime);
中断后重跑只能补缺口, 已有的旧版本行要保持覆盖需单独处理 (T-15 就踩过, 用 coalesce 补额).

### 15. 统计脚本要 glob 分片
`kline_daily` 242 个日期是 `part-stream-*.parquet`, 只 glob `part.parquet` 会误判丢数据.

### 16. 启动日志的两条"错误"是正常的
- `读 /frontend/package.json 失败, 回退 _FALLBACK_VERSION=0.3.37`
- `FinancialScheduler skipped: 档位不足`

### 17. 收盘后腾讯五档成交量清零
2026-11-20 15:06 实测: `qt.gtimg.cn` 五档只剩买一价, 买一~卖五量全为 0.
15:02 定版再拉一次会拿到空快照或全 0 量, 旧实现会直接丢当天封单.
已修: 无成交量时保持盘中最后一盘, 定版落该缓存 (`10fa40f5`).

### 18. 盘中 live 分时会污染当天分钟分区
打开个股分时 (`live=1`) 会把那一只票写进 `kline_minute/date=今天`.
板块轮动按当天分钟聚合, 只有 1 只票时整张卡塌成一根线.
已修: 分区标的数不足近 5 盘最大值的 50% 时回退到完整交易日 (`9c662087`).

### 19. 分钟同步 2 天要 1 小时 47 分
2026-11-20 管道: 15:35 启动, 17:22 结束. `sync_minute` 阶段 5,893 只 x 2 天,
每只 1~2 次腾讯请求 + 0.12 秒节流, 约占 80 分钟. 属预期, 不是卡死.
看进度用 `/api/pipeline/jobs/{id}` 的 `stage` / `progress` / `log` 尾部.

### 20. 扩展源日志的 "every 1440 min" 是显示 bug
`PullScheduler: scheduled ... (every %d min)` 只打印 `schedule_minutes`,
实际间隔由 `_interval_seconds()` 决定, `schedule_seconds` 优先.
资金流配的是 600 秒, 日志却写 1440, 别被误导.

### 21. 板块轮动盘中会混日期
涨幅矩阵来自 `kline_minute` (只 15:35 全量落盘), 资金流来自扩展表 (实时).
盘中看卡片时: 资金流是今天的, 涨幅可能是昨天的 (或今天半量的).
不要跨列做横向比较; 收盘管道跑完才对齐.

## 八. 未完成 / 待决策

- **T-04**: 盘中 11-20 已复验 (10:30/11:25/13:05/14:35/14:55 五个采样点).
  当天定版因收盘后五档量清零而缺失, 修复已上线, **11-23 15:02 复验落盘**.
- **T-05 板块资金流**: 已完成 (新浪个股资金流扩展表). 东财方案已废弃.
  已知限制: 盘中分钟未全量落盘时, 涨幅与资金流不同日 (见坑 21).
- **T-12 复盘页日期选择**: 需要装 pnpm + 构建前端 + 部署静态产物.
- **T-10 概念/行业历史**: 大概率关票 (无历史时点成分源).
- **T-13 假公告日**: 需要一个真实公告日来源 (巨潮/交易所披露表), 未开始.
- **北交所历史**: 2024 前是 43/83/87 老代码, 需映射; 另案.
- **分钟历史**: 只能从 2026-11-19 往前攒; 11-18 及更早补不了 (腾讯 1.3 天窗口).
- 访问密码: 未设 (内网放行, Jimmy 已确认). AI 未接. 补丁未提交, 未自建镜像.

## 九. 关键证据命令

```bash
# 容器
docker ps --filter name=tsp
docker inspect tsp --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
docker exec tsp python -c "import socket;print(socket.gethostbyname('qt.gtimg.cn'))"

# 数据
docker exec tsp sh -lc "du -sh /app/data/* | sort -h | tail"
curl -s --noproxy '*' http://127.0.0.1:3018/api/data/status

# 能力路由
curl -s --noproxy '*' http://127.0.0.1:3018/api/settings/capability-matrix
curl -s --noproxy '*' http://127.0.0.1:3018/api/capabilities

# 任务
curl -s --noproxy '*' 'http://127.0.0.1:3018/api/pipeline/jobs?limit=5'

# 分时 (本地/实时)
curl -s --noproxy '*' "http://127.0.0.1:3018/api/kline/minute?symbol=000815.SZ&date=2026-11-19"
curl -s --noproxy '*' "http://127.0.0.1:3018/api/index/minute?symbol=000001.SH&date=2026-11-19"

# 指数历史
curl -s --noproxy '*' "http://127.0.0.1:3018/api/index/daily?symbol=000001.SH&start_date=2016-01-01&end_date=2016-12-30"

# 财务多期
curl -s --noproxy '*' "http://127.0.0.1:3018/api/financials/metrics?symbol=600519.SH"

# 连板梯队 / 手动修正五档
curl -s --noproxy '*' "http://127.0.0.1:3018/api/screener/limit-ladder?direction=up"
curl -s --noproxy '*' -X POST http://127.0.0.1:3018/api/settings/preferences/limit-ladder-monitor/run

# 手动分钟落盘 (触发管道任务)
curl -s --noproxy '*' -X POST http://127.0.0.1:3018/api/kline/sync_minute -H 'Content-Type: application/json' -d '{"days":1}'
```

## 十. 文档索引

- `docs/data-inventory.md` — 数据资产总清单 (单一事实来源)
- `docs/data-tickets.md` — 工单 T-01 ~ T-15 与验收记录
- `docs/astock-supply-catalog.md` — a-stock-data 供给清单
- `docs/astock-m0-realtime-probe.md` — 腾讯 M0 实测 (五档字段/批量上限)
- `docs/astock-daily-history-backfill-plan.md` — 日K 补数方案 (已执行)
- `docs/astock-data-verification.md` / `docs/stockdb-data-source-notes.md` — 数据源评测
- `docs/decisions.md` — 历史决策
- `CONTRIBUTING.md` / `docs/secondary-development.md` — 改代码前必读
