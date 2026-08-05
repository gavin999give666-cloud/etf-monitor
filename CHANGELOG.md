# CHANGELOG

版本按时间倒序排列。早期版本（V1.0 到 V6.0）代码已归档，条目依据现存代码中的注释和 docstring 整理，细节以当时代码为准。

## V6.2 / V6.2-科创（2026-08-05）

A500 主线与科创综指主线同步发布 V6.2。这一版修复了一个会永久污染历史数据的重大问题，并补齐了盘中估算与 T+1 回填能力。

- 修复重大数据污染问题：盘中运行时 Sina API 会返回当日不完整K线，此前被直接写入数据库且永久污染历史数据。三层修复（`data_updater.py`）：新增当日数据过滤（盘中不完整K线不入库）、写入改为 `INSERT OR REPLACE`（同日记录可被真实收盘数据覆盖）、增量更新的 `MAX(date)` 判断只统计真实数据（跳过估算/不完整记录）
- 新增 14:40 盘中成交量估算（`volume_estimator.py`）：基于盘中已产生的成交量估算当日总量，主用 Sina 5分钟线自动标定比例法，拿不到 5分钟线时退化为固定比例 0.90；新增 CLI 命令 `--intraday`（盘中估算当日数据并出信号，附估算值免责提示）；数据库新增 `is_estimated` 标记列区分估算记录与真实数据
- 新增 T+1 自动回填（`backfill_estimated_data()`）：`--update` 时自动用真实收盘数据回填此前的估算记录，也可手动 `--backfill` 触发
- 新增运行时间感知（`get_runtime_context()`）：启动横幅显示当前交易日/时段（盘前/盘中/收盘竞价/盘后/非交易日），各模式配套智能提示与防护——非盘中时段拒绝 `--intraday` 并给出原因，盘中运行 `--signal`/`--eval` 警告数据不完整并引导使用 `--intraday`，盘中 `--update` 提示只补历史
- 修复 Windows GBK 控制台输出 ⚠ 等特殊字符时的 UnicodeEncodeError 崩溃（stdout/stderr 统一 `errors='replace'` 降级）

## V6.1-科创（2026-08）

- 二次全量优化（2026-08-02 22:20，12167 次评估）：Optuna 阶段最优 objective 46.77（记录收益 41.51%），因 `_heavy_eval_worker` 中 strategy 在 `setattr(config, ...)` 之前已 `from config import *` 捕获旧值，**策略级参数（评分权重、REGIME 乘数、确认阈值等）在优化过程中从未真正生效**，仅 backtest/scoring_engine 运行时读取的执行参数生效
- 复核后仅将**真正生效的执行参数**写入 `config.py`：`TRADE_TARGET_DELTA=0.091`、`TRADE_ACTUAL_DELTA=0.019`、`MIN_HOLD_DAYS=34`、`MAX_POSITION=0.98`、`INITIAL_POSITION=0.89`、`SCORE_HOLD_ZONE=17`、买入/卖出仓位曲线重写（T1-T4）
- 执行参数复核回测：收益 40.76%、夏普 0.976、回撤 -27.51%、胜率 66.7%、6 笔交易（与原 config 的 25.63% 相比收益显著提升，但回撤同步加深，属激进化配置）
- 修复 `param_optimizer.py` worker 参数注入 bug：`_heavy_eval_worker` / `_eval_single_*_on_df` 在 `setattr(config, ...)` 后新增 `_reload_config_capture_modules()` 强制重载 strategy/backtest 及所有导入时 `from config import *` 的依赖模块，使策略级参数（评分权重、REGIME 乘数、确认阈值等）真正生效；`_build_regime_from_params` 改用固定默认结构，避免 worker 常驻时跨 trial 的 config 污染。修复后 top1 参数回测 24.80%（与直接写入 config.py 的 25.07% 一致，策略级参数生效），彻底消除"记录收益 41.51% 但实际无法复现"的失真问题
- `--heavy` 优化默认训练量调整为 10000（`n_trials`，含 `main.py --heavy` 与 CLI `--trials` 默认值），默认并行线程数调整为 16（`n_jobs`/`--jobs`）

## V6.1-科创（2026-07）

科创综指 ETF（589800）主线的独立分支，代码从 V6.1 复制后按科创板特征调整。

- 新增 `param_optimizer.py`，三层参数优化：Optuna TPE 贝叶斯、遗传算法、逐级精细网格按顺序跑，搜索空间覆盖 50+ 参数（评分权重、市场状态乘数、仓位映射等），比 `param_search.py` 原来的 8 参数网格大一个量级
- 多进程并行：`ProcessPoolExecutor` + spawn 上下文，指标在父进程预计算，数据先落成 pickle 缓存再给子进程复用，避免每轮重复算
- 断点续算换成 Optuna JournalStorage，日志文件支持多进程写入，中断后接着跑
- 全量优化结果落盘 `heavy_results.json`：一次跑 7250 次评估、耗时约 288 分钟，optuna 阶段最优 objective 47.81（收益 39.11%，夏普 1.047，回撤 -23.41%）
- 调研了科创板融资融券余额的数据来源（`_test_margin_balance.py`）：886033 板块两融指数拿不到公开 API，改用 akshare `stock_margin_detail_sse` 汇总 688 开头个股代替

## V6.1（2026-07）

A500 主线的稳定化版本。这一版的思路是 Fix Before Expand，先让已有模块真正工作，再考虑加新东西。

- Time Decay 修复：指数硬衰减改成 exp(-days/τ) 平滑衰减，输出范围 [0.5, 1.0]，之前衰减过激会把所有置信度压缩到 0.1
- Replay Learning 落地：行为记忆库按 (Regime, Behavior, Psychology) 三维键统计历史成功率，样本按时间加权，加 Laplace 平滑，样本太少时回归中性
- ML 置信度默认启用：RandomForest 每 10 笔交易在线重训练，配 Platt 概率校准，用 Brier/LogLoss 评估，校准变差自动禁用
- Evidence Explainability：每笔交易输出完整证据链分解，rule / replay / ml / emotion 四个证据源逐项列出
- 行为阈值整体放宽，新增 RSI 超买、均线死叉两个卖出行为，最低持仓天数设为 10 天；`feature_builder.py` 做了 V6.1.1 hotfix，训练和推理统一走一个特征构建器
- 网格搜索稳定化：断点续算用 SHA256 指纹校验（参数、数据、起始日期任一变化旧断点自动失效），原子写入防崩溃，完整搜索 34,560 组组合

调参记录（来自 2026-07-26 的诊断报告和后续搜索）：问题集中在卖出过早，24 笔交易全部提前离场，累计错失约 17.8%。只调整 6 个参数后策略收益率从 15.30% 提到 20.19%，胜率从 58.3% 到 73.3%，交易次数从 24 降到 15，平均持仓从 9.8 天拉长到 29.3 天。

## V6.0

- Evidence Engine 上线：rule / replay / ml / emotion 四路证据源按权重融合出置信度，替代之前完全人工规则的评分方式
- EmotionBuilder 替换 Crowd Psychology：多源市场数据做 PCA / ICA / 加权融合，输出连续情绪分，再映射到离散状态，不再依赖单一价格指标的硬编码阈值
- 回测引擎增强（`backtest.py`）：集成 Replay Learning、证据追踪、情绪双确认统计，补全绩效指标
- ML 置信度模块（`ml_confidence.py`）和 LLM 情绪分析接口（`llm_sentiment.py`）在这个版本出现，LLM 部分只定义了抽象接口，计划 V6.5 接入 Qwen / DeepSeek
- `FUTURE_FACTORS` 扩展接口：预留新闻情绪、ETF 资金流、北向资金、融资融券、宏观流动性、多 ETF 轮动等因子位

## V5.0

- 行为生命周期：行为检测到之后不再直接触发交易，先交给事件引擎走 Candidate → Observation → Confirmed → Executed → Finished 五阶段，加观察窗口和置信度门槛
- Crowd Psychology 情绪状态机：Panic / Fear / Hope / Optimism / Euphoria / Exhaustion 六种状态，切换需要连续确认，避免噪声误判
- Reward / Risk 评估：六因子赔率打分（距 60 日高低点、MA20 偏离、ATR 位置等）和五因子风险打分（回撤、波动率、趋势反转、量能等）进入最终评分
- 评分公式改为行为分 + 置信度 + Reward - Risk 加权，评分到仓位用阈值表映射
- 参数网格搜索上线（`param_search.py`，内部 V5.2 加了断点续算），配 Pareto 多目标分析
- 数据更新改用 akshare 单源（`data_updater.py`），指标模块加了加速度 / 减速度计算

## V4.0

早期版本，代码已归档。现存 `behavior_detector.py` 的注释里保留了与这一版的对比：V4.0 是"检测到行为 → 立即评分 → 触发交易"，没有事件管理和生命周期这一层，信号噪声直接进了交易决策。

## V1.0 / V2.0

最早的可运行版本，目录已归档。`main.py` 的数据路径查找至今保留了对这两个版本目录的回退引用（V5.0 → V4.0 → V2.0 → V1.0），说明数据库文件格式从那时延续到了现在，期间跳过了 V3.0。这一时期的细节已经不在现存代码里，不再补述。

## 未来计划

- 把 V6.1 和 V6.1-科创 合并成整合版，标的选择参数化，一次维护一套代码
- 板块监控系统（`板块监控/`，V2.2.3 设计文档）的信号通过 JSON 接口接入 Evidence Engine，作为板块层面的新证据源
