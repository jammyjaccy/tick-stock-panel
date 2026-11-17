# 交接说明 (HANDOFF)

给在 `/home/workspace/tickflow-stock-panel` 新开会话的 agent 或人.
先读本文件, 再读 `docs/decisions.md`.

更新时间: 2026-11-17 晚 (部署完成当天)

## 一. 一句话现状

面板已部署并跑通, 局域网访问 `http://192.168.1.9:3018`.
日K / 除权因子 / 指标 / 指数 / 市场环境 / 主线 / 财务 全部可用且不花积分.
分钟K 批量同步因通达信积分耗尽被关闭, 只保留已落盘的 4 天数据.

## 二. 部署形态 (改动容器前必读)

- 容器名: `tsp`
- 镜像: `ghcr.io/shy3130/tick-stock-panel:latest`
  digest `sha256:d1230fb1ea0e4d533d2e3b09fdbdc1563acf41950cb75bff1508aeadb4b50f83`
- 数据目录: `/home/workspace/tickflow-data` (容器内 `/app/data`, 落盘文件属主 root)
- 启动时间: 2026-11-17 17:11

### 完整启动命令 (重建容器必须一字不差)

```bash
docker run -d --name tsp \
  -p 0.0.0.0:3018:3018 \
  -e DATA_DIR=/app/data \
  -v /home/workspace/tickflow-data:/app/data \
  -v /home/workspace/tickflow-stock-panel/backend/app/plugins/fuyao/client.py:/app/app/plugins/fuyao/client.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/app/plugins/fuyao/provider.py:/app/app/plugins/fuyao/provider.py:ro \
  -v /home/workspace/tickflow-stock-panel/backend/tests:/app/tests:ro \
  --restart unless-stopped \
  ghcr.io/shy3130/tick-stock-panel:latest
```

**三个 `-v` 是补丁挂载, 少一个补丁就失效** (回到会撞 429 的旧代码, 见第五节).
`backend/tests` 挂载只是为了能在容器里跑测试, 不影响运行; 可以省, 但省了就没法 `uv run --with pytest`.

长期方案: 从自己的 fork 构建镜像, 把补丁烤进镜像, 摆脱挂载. 未做.

## 三. 本次改的代码 (未提交)

| 文件 | 改动 |
|---|---|
| `backend/app/plugins/fuyao/client.py` | `_get()` 增加 429/5xx 退避重试 (默认 3 次, 等 10/20/30 秒, 尊重 `Retry-After`, 上限 60 秒); 实时快照两处调用传 `retries=0` 不重试 |
| `backend/app/plugins/fuyao/provider.py` | `_HIST_INTERVAL_S` 0.12 -> 0.3 (单标的节流) |
| `backend/tests/test_fuyao_client_retry.py` | 新增 5 条测试 (429 重试成功 / 耗尽仍抛错 / Retry-After 优先 / 实时不重试 / 5xx 也重试) |

验证 (实测):

```
5 passed   (test_fuyao_client_retry.py)
141 passed (test_fuyao_provider.py + test_fuyao_historical_batch.py + test_fuyao_financial.py)
```

在容器内执行: `docker exec tsp sh -lc "cd /app && uv run --with pytest pytest tests/ -q"`
(镜像里无 pytest, 靠 `--with pytest` 临时装; 需要容器内有网络)

## 四. 当前数据 (2026-11-17 19:53 实测)

```
标的池     5,893 只       交易日 243 天 (2025-11-17 ~ 2026-11-17)
日K        25,169 分区    197 MB
指标       243 天, 15 列  65 MB   (close 前复权 / raw_close 原始价 / 换手率 / 连板天数)
除权因子   3,778 只有事件  4,951 行
指数       599 只 × 243 天 145,270 行
市场环境   243 天           (冰点/启动/主升/高潮/退潮/修复)
市场主线   7,806 行
分钟K      4 天 × ~5,150 只 (2026-11-12/13/16/17), 51 MB, 缺 11-11
总占用     233.87 MB
掉队标的   7 只 (今日无数据, 软告警)
```

### 能力路由 (设置页可改)

```
日K        fuyao
除权因子   fuyao
实时行情   fuyao      <- 故意不切 TdxQuant, 6 秒轮询切过去就是每 6 秒烧分
财务       fuyao
分钟K      tdxquant
五档盘口   tdxquant
全量分钟   tdxquant
TickFlow 档位: none (未付费, 无 Key)
```

### 关键 preferences

```
pipeline_regime_enabled  = true    (市场环境/主线随管道计算)
minute_sync_enabled      = false   (关闭, 见第五节积分问题)
minute_sync_days         = 5
```

## 五. 踩过的坑 (别再踩一遍)

### 1. fuyao HTTP 429 导致整轮同步归零

现象: 首次全市场日K同步在 41% 处报 `扶摇日K补查失败 xxx: HTTP 429`,
`daily_files = 0`, 重跑仍在同一位置失败, 永远跑不完.

根因链:
- `provider.py` 批量接口没覆盖到的标的逐个回落**单标的接口**, 节流仅 0.12 秒
- 实测峰值 111 请求/分钟, 撞 fuyao 分钟配额
- `client.py` 对非 200 直接抛错, **无重试**
- 同步层收到错误后**丢弃整轮暂存帧** (设计如此, 见 `docs/plugin-development.md:400`)

修法: 见第三节. 实测降速到 0.3 秒后整轮 23 分钟跑完, 一次 429 都没撞.

### 2. TdxQuant 积分: 分钟批量非常贵

实测账单 (通达信商城「使用流水」):

```
今日起始余额  9,999
今日结束余额  1,641
今日总消耗    8,358
其中分钟批量  8,355  (19:00/19:05/19:10/19:15/19:20 五笔, 每笔约 1,670)
其他零星      3      (Key 探测, 每次 1 分)
```

- 单价约 **0.4 分/只/天**, 全市场 5 天一轮约 **8,350 分**
- 失败原因: 19:20 扣完剩 1,642, 下批要 1,670 -> 服务端 `TokenKey Insufficient Or Key Error`
  (错误文案二义: "额度不足"或"Key 错误". 凭流水可判定是**额度不足**, Key 权限正常)
- 单只按需拉 (分时图) 约 1-2 分/只, **便宜可用**; 批量才贵
- 一次性拉分钟数据会**按段落盘**, 不像日K整轮丢弃 (4 天数据在失败后仍在)

结论: 分钟批量保持关闭. 除非充值, 否则不要开 `minute_sync_enabled`.

### 3. `/api/data/status` 的 `minute_files` 恒为 0

不要拿它判断分钟数据有没有落地. 实测该字段一直是 0, 而磁盘
`/app/data/kline_minute/` 有 51 MB 真实数据. 判断一律看磁盘:

```bash
docker exec tsp sh -lc "du -sh /app/data/kline_minute; ls /app/data/kline_minute"
```

### 4. 本机 curl 必须加 `--noproxy '*'`

否则被 `http_proxy` 截走, 返回 `HTTP 000` (会误判成服务没起来).

### 5. 启动日志里的两条"错误"是正常的

- `读 /frontend/package.json 失败, 回退 _FALLBACK_VERSION=0.3.37` -- 镜像内路径小瑕疵, 回退值正确
- `FinancialScheduler skipped: 档位不足` / `已配置鉴权但未找到凭据 fuyao_api_key`
  (后者在配上 fuyao key 后消失)

## 六. 用户目标与对应功能 (Jimmy 明确的三件事)

1. **盘中找机会** -> 异动监控页 (竞价/盘中/偏移) + 监控中心 + 市场环境页. 基本零成本.
   连板梯队封单走五档 (TdxQuant), 会花积分.
2. **做T (已有持仓)** -> **批次页** (`frontend/src/custom/lots/Page.tsx`,
   域名逻辑 `backend/app/strategy/lots.py`): 每笔买入登记 成本价/数量/止盈%/止损%/到期日,
   自动派生两条监控规则 (到价提醒 + 到期提醒). 零积分.
   分时图单只按需拉, 1-2 分/只.
   **不要**开分钟级 `csgi_*` 自动信号: 每分钟为每个标的拉一次数据,
   监控 10 只票一天就能烧光余额.
3. **选股** -> 策略 / 因子 / 回测. 数据齐全, 零成本. 这是面板最成熟的部分.

建议路径: 先做「批次登记」验证提醒链路, 再写策略回测, 最后配异动监控.

## 七. 未完成 / 待决策

- 分钟数据: 缺 2026-11-11 一天, 覆盖 5,144/5,893 只. 补齐需 8,350 分, 暂不做.
- 访问密码: **未设**. 当前是"未设密码 + 内网放行"(`backend/app/main.py:669`),
  局域网内任何设备可直接访问. Jimmy 已明确局域网无他人, 决定不设.
- AI 未接. 支持 `openai_compat` / `openai` / `codex_cli`; Claude 需经 LiteLLM, 要批准 (H2).
- 补丁未提交, 未自建镜像 (长期应做).
- 多市场改造: 仍未立项 (见 decisions.md 第三节).
- 分钟同步失败的"整段丢弃"修改: 属改上游代码, 未做.

## 八. 关键证据 (复现时用)

```bash
# 容器状态
docker ps --filter name=tsp

# 数据落盘
docker exec tsp sh -lc "du -sh /app/data/* | sort -h | tail"

# 管道任务
curl -s --noproxy '*' http://127.0.0.1:3018/api/pipeline/jobs?limit=5

# 能力路由
curl -s --noproxy '*' http://127.0.0.1:3018/api/settings/capability-matrix

# 数据状态
curl -s --noproxy '*' http://127.0.0.1:3018/api/data/status
```

- 能力路由权威定义: `backend/app/data_providers/capabilities.py`
- fuyao 日K补查与回退: `backend/app/plugins/fuyao/provider.py:691` (`_historical_batch_rows`)
- fuyao 退避补丁: `backend/app/plugins/fuyao/client.py` (`_retry_wait` / `_get`)
- 管道阶段顺序: `backend/app/jobs/daily_pipeline.py` (Step 0 维表 -> 1 日K -> 1.5 除权
  -> 2 指标 -> 2.3 指数 -> 2.5 分钟 -> 2.6 环境 -> 2.7 主线 -> 2.8 模拟盘)
- 分时信号取数: `backend/app/services/quote_service.py:1723` (`_inject_intraday_signals`)
- TickFlow 免费档 (none) 只提供历史日K; 实时/除权/分钟/五档/财务均需付费档或第三方源
  (`backend/app/data_providers/capabilities.py` 的 `tf_tier` 字段)
