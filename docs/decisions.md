# tick-stock-panel 立项摘要

更新时间: 2026-11-17
状态: 已部署运行中 (2026-11-17), 未做多市场改造

本文件只记录结论, 不记录推导过程. 推导证据见 `docs/handoff.md` 与各仓库历史.

## 一. 已定

1. 上游仓库: `shy3130/tick-stock-panel`, 最新 v0.3.37 (f9cb383c, 2026-11-12).
   - 上游只做 A 股, 无 `markets.py`, 无港美股能力.
   - 仓库曾改名: 旧 `tickflow-stock-panel` -> 现 `tick-stock-panel`, 搜索只认新名.

2. 我方 fork: `jammyjaccy/tick-stock-panel`, 与上游状态 identical, 共享完整 Git 历史.

3. 旧 fork `jammyjaccy/tickflow-stock-panel` 已删除 (2026-11-17).
   - 它只是 `hzy1522/tickflow-stock-panel` 的静态快照, 无自有提交, 代码未丢失.
   - `hzy1522` 那份历史被重写过, 与上游无共同祖先, 不可直接 merge.

4. 本地克隆: `/home/workspace/tickflow-stock-panel`.
   - remote 拆分已完成 (2026-11-17): `origin` = `jammyjaccy/tick-stock-panel`,
     `upstream` = `shy3130/tick-stock-panel`.
   - `main` 跟踪 `origin/main`, 与 `upstream/main` 同为 `f9cb383c`.
   - fork 上遗留分支 `codex/settings-and-pipeline-fixes` 无独有提交 (已在 v0.3.37 祖先链内),
     可删可不删, 暂未处理.

5. 部署方式 (方案 A, **已执行 2026-11-17**):
   - 预构建镜像 `ghcr.io/shy3130/tick-stock-panel:latest`, revision f9cb383c.
   - 容器名 `tsp`, 数据目录 `/home/workspace/tickflow-data`, 局域网 `0.0.0.0:3018`.
   - 首次全市场同步 (fuyao 日K, 5,893 只 x 1 年) 耗时 23 分钟, 数据见 handoff 第四节.
   - AI: 初始关闭, 后续在设置页随时开.

6. AI 接入结论:
   - 支持的 provider: `openai_compat`, `openai`, `codex_cli`.
   - Claude Code 不能直连 (不是 chat completion API, 无 adapter).
   - Claude 模型可经 LiteLLM 转 OpenAI 兼容后使用, 需 Jimmy 批准模型与 key (H2).
   - Hermes 9119 端口是带登录的 Web 控制台, 当前不可直连.

7. 数据源策略:
   - 面板按能力分源路由: daily / realtime / minute / depth5 / financial / adj_factor.
   - 原计划日K走 TickFlow 免费档, 实际落地改为 fuyao (见第 9 条), TickFlow 未使用.
   - 自定义 realtime 契约: 必须是全市场快照, 字段含 symbol, last_price, prev_close, open, high, low, volume.
   - 不做破解; 官方替代为 TdxQuant 插件或 YAML 自定义源.

8. Fork 维护策略:
   - `origin` = 自己的 fork, `upstream` = shy3130.
   - 同步用 merge 到 `sync/upstream-YYYYMMDD` 分支, 解冲突并测试后再合回 main.
   - 个性化改动优先走扩展点: `frontend/src/custom/`, `backend/app/data_providers/`, `extensions/`, YAML 数据源.
   - 避免修改上游高频文件: `frontend/src/lib/api.ts`, `backend/app/api/screener.py`,
     `backend/app/services/kline_sync.py`, `backend/app/services/quote_service.py`.

9. 数据源落地 (2026-11-17): Jimmy 已自配 fuyao 与 TdxQuant 两把 Key (未入库, 不记录).
   - 路由: 日K / 除权因子 / 实时行情 / 财务 -> fuyao; 分钟 / 五档 / 全量分钟 -> tdxquant.
   - 实时故意留在 fuyao: 6 秒轮询切 TdxQuant 等于每 6 秒烧积分.
   - TickFlow 无 Key, 档位 none.

10. TdxQuant 计费实测 (2026-11-17, 来自通达信商城使用流水):
   - 单价约 0.4 分/只/天; 全市场 5 天一轮约 8,350 分.
   - 当日余额 9,999 -> 1,641, 消耗 8,358, 其中分钟批量 8,355.
   - 失败原因判定为**额度不足** (非 Key 权限): 扣完剩 1,642, 下一批需 1,670.
   - 结论: 分钟批量关闭; 单只按需拉 (分时图) 1-2 分/只, 可放心用.

11. 代码补丁 (未提交, 以 bind mount 方式生效, 详见 handoff 第二/三节):
   - fuyao 429/5xx 退避重试 + 单标的节流 0.12 -> 0.3 秒, 修复首轮同步必崩问题.
   - 测试 146 条通过. 仓库里是改动, 容器里是挂载, 两者必须一致.

## 二. 待定

1. 访问密码: **不设**. 未设密码时内网放行, 公网 403, 局域网无他人, Jimmy 2026-11-17 决定.
2. AI 最终接哪个: DeepSeek / LiteLLM / Codex CLI / 暂不接.
3. 分钟数据是否补齐: 缺 2026-11-11 一天, 覆盖 5,144/5,893 只, 补齐需约 8,350 分.
4. 是否自建镜像 (从 fork 构建), 把补丁烤进镜像, 摆脱 bind mount.
5. 多市场改造是否立项, 以及走"追随上游"还是"独立分叉".

## 三. 多市场改造 (未启动, 仅存档结论)

以 `hzy1522` 的 fork 为参照, 在新基线 v0.3.37 上重做的评估:

- 改动规模: 71 文件 (新增 13 / 删除 1 / 修改 57), +5240 / -538 行.
- 13 个新增文件可原样落地, 其中 `markets.py` 零依赖.
- 36 个文件有文本冲突; 14 个可干净合并; 5 个页面被上游搬到 `frontend/src/custom/`.
- 三处语义地雷:
  1. `StrategyDataContext.market` 在上游指行情矩阵, 与市场标识同名不同义.
  2. 新端点须进 `api_gateway.required_scope` 规则表, 并同步 `test_openapi_contract.py` 快照.
  3. 存储已是 asset_type 三维度 (stock/etf/index), 多市场会变成 (market x asset_type), 需重设计.
- 预估工作量: 6-11 人天 (推测, 未实测).

## 四. 环境事实 (2026-11-17 实测)

- Docker 29.1.3 可用; uv 0.13.5; pnpm 缺失; 系统 Python 3.14.
- CPU i5-12490F, 支持 avx2/fma, 12 核; 内存 31 GB (可用 18 GB).
- `/home` 剩余 1.6 TB; 端口 3018/3011 空闲.
- 无同名容器.
