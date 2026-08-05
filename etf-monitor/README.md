# ETF 多因子择时监控系统

基于多因子与投资者情绪的 ETF 量化择时系统。策略先把市场切成不同状态（牛市、熊市、震荡），再识别 9 种典型买卖行为，最后用一个证据引擎把人工规则、历史回放统计、机器学习概率和情绪修正四路证据融合成买卖评分，映射成仓位。

项目分两条主线，共享同一套架构，参数配置互相独立：

- `V6.1/`：面向 A500 ETF（563360）的主策略
- `V6.1-科创/`：面向科创综指 ETF（589800）的同源分支，附带更大规模的参数优化工具

策略流水线从数据到仓位一共经过 10 个模块：指标计算 → 市场状态识别 → 行为检测 → 情绪融合 → 事件生命周期 → 赔率/风险评估 → Evidence Engine 融合（Rule + Replay + ML + Emotion）→ 评分 → 仓位映射 → 回测执行。

## 目录结构

```
etf-monitor/
├── V6.1/                        # A500 ETF 策略（主）
│   ├── main.py                  # CLI 入口（回测 / 信号 / 数据更新 / 网格搜索）
│   ├── config.py                # 全部策略参数
│   ├── strategy.py              # 策略流水线（V6Strategy）
│   ├── backtest.py              # 回测引擎（V6Backtest）
│   ├── evaluation.py            # 评估模块（绩效 + 证据链分解输出）
│   ├── param_search.py          # 参数网格搜索（多进程 + 断点续算）
│   ├── evidence_engine.py       # 多源证据融合（Rule/Replay/ML/Emotion）
│   ├── behavior_memory.py       # Replay Learning 行为记忆库
│   ├── ml_confidence.py         # ML 置信度（RandomForest）
│   ├── probability_calibration.py  # 概率校准（Platt / Isotonic）
│   ├── emotion_builder.py       # 多源情绪融合引擎
│   ├── behavior_detector.py     # 行为检测（9 种买卖行为）
│   ├── event_engine.py          # 事件引擎 + 行为生命周期
│   ├── reward_risk.py           # 赔率/风险评估
│   ├── scoring_engine.py        # 评分 → 仓位映射
│   ├── position_manager.py      # 仓位管理
│   ├── regime_detector.py       # 市场状态识别
│   ├── indicators.py            # 技术指标计算
│   ├── feature_builder.py       # 统一特征构建（训练/推理共用）
│   ├── data_updater.py          # 数据更新（Sina 主 / baostock 备 / akshare 末备）
│   ├── llm_sentiment.py         # LLM 情绪分析接口（预留）
│   └── requirements.txt
├── V6.1-科创/                   # 科创综指 ETF 策略（589800）
│   ├── main.py                  # 额外提供 --heavy 全量参数优化命令
│   ├── param_optimizer.py       # 三层参数优化（贝叶斯 / 遗传 / 逐级网格）
│   └── ...                      # 其余模块与 V6.1 同构
├── 板块监控/                    # 板块监控系统设计文档（V2.2.2 / V2.2.3）
└── 理论支撑/                    # 三篇参考论文及可学之处分析（与 V5.0 对比）
```

行情数据存在各目录下的 `stock_data.db`（SQLite），不入库，需要先更新再跑回测。

## V6.1 快速开始

依赖 Python 3.9+，安装依赖：

```bash
pip install -r V6.1/requirements.txt
```

更新行情数据（增量写入 `stock_data.db`）：

```bash
cd V6.1
python main.py --update
```

运行回测评估（ML 置信度默认启用）：

```bash
python main.py --eval
```

输出今日信号（含 Evidence Engine 证据链分解）：

```bash
python main.py --signal
```

其他常用命令：

```bash
python main.py                    # 交互式菜单
python main.py --replay           # 打印最近回放记录
python main.py --behavior-memory  # 行为记忆库统计
python main.py --replay-summary   # Replay Learning Top10/Worst10
python main.py --evidence-debug   # Evidence Engine 调试输出
python main.py --grid-search      # 参数网格搜索（断点续算）
python main.py --grid-fresh       # 强制全新网格搜索（忽略断点）
python main.py --grid-status      # 查看断点续算状态
```

科创主线（V6.1-科创）的命令基本相同，额外有全量参数优化：

```bash
cd ../V6.1-科创
python main.py --heavy        # 全量高算力优化（50+ 参数，断点续算）
python main.py --heavy-view   # 查看上次优化结果
```

## 版本说明

- `V1.0` ~ `V6.0`：早期版本，已归档，不再维护
- `V6.1`：A500 主线稳定版。修掉了 Time Decay 衰减过激的问题，让 Replay Learning、ML 置信度、概率校准真正参与决策，每笔交易输出完整证据链
- `V6.1-科创`：科创板主线的同源分支，参数优化升级为贝叶斯 + 遗传 + 逐级网格三层优化

版本演进细节见 [CHANGELOG.md](CHANGELOG.md)。
