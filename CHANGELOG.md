# CHANGELOG

版本按时间倒序排列。早期版本（V1.0 到 V6.0）代码已归档，条目依据现存代码中的注释和 docstring 整理，细节以当时代码为准。

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
