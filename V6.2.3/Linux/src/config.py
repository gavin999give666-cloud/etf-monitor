"""
V6.2.3 策略配置文件 —— Evidence Engine 稳定化版本
=================================================

V6.2.3 核心升级（Fix Before Expand）：
- Time Decay 修复：从指数硬衰减改为平滑衰减，输出范围[0.5, 1.0]
- Replay Learning 在线学习闭环：多维Replay键 + 时间加权样本 + Laplace平滑
- ML Confidence 真正启用：RandomForest/GradientBoosting 参与 Evidence Engine
- Probability Calibration 验证：Brier/LogLoss评估，校准变差自动禁用
- Evidence Explainability：每笔交易输出完整证据链分解
- Behavior Replay Granularity：(Regime, Behavior, Psychology) 三维学习

设计原则：
- 行为 != 信号
- 情绪 != 拐点
- 候选 != 执行
- Confidence来源多元化（Rule/Replay/ML）
- 指标用于解释市场，不是触发交易
- Fix Before Expand：先让已有模块真正工作，再考虑新增
"""

# ============================================================
# 基础配置
# ============================================================
STOCK_CODE = '563360'
DB_FILENAME = 'stock_data.db'

# ============================================================
# 指标参数
# ============================================================
MA_SHORT = 5
MA_MID = 10
MA_LONG = 20
RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2
VOL_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VWAP_PERIOD = 1

# ============================================================
# 市场状态识别参数
# ============================================================
REGIME_ADX_THRESHOLD = 20
REGIME_MA_SLOPE_MIN = 0.001
REGIME_BB_WIDTH_LOW = 0.02
REGIME_BB_WIDTH_HIGH = 0.06
REGIME_VOLATILITY_LOOKBACK = 20

# ============================================================
# 行为识别参数
# ============================================================

# Behavior 1: Double Bottom（二次探底）
DOUBLE_BOTTOM_LOOKBACK = 30
DOUBLE_BOTTOM_REBOUND_MIN = 0.01
DOUBLE_BOTTOM_SECOND_LOW_MAX = 0.985
DOUBLE_BOTTOM_VOL_SHRINK = 0.75
DOUBLE_BOTTOM_SCORE = 45

# Behavior 2: Momentum Exhaustion（冲高衰竭）
MOMO_EXH_LOOKBACK = 5
MOMO_EXH_RETURN_THRESHOLD = 0.025
MOMO_EXH_RSI_RISE_MIN = 5
MOMO_EXH_VOL_EXPAND = 1.1
MOMO_EXH_ACCEL_DECLINE = 0.6
MOMO_EXH_SCORE = 50

# Behavior 3: Trend Pullback（趋势回踩）
PULLBACK_MA_DIST = 0.015
PULLBACK_VOL_SHRINK = 0.9
PULLBACK_REQUIRE_BULL = False
PULLBACK_SCORE = 30

# Behavior 4: False Break（假突破）—— V6.2.3 阈值放宽
FALSE_BREAK_LOOKBACK = 3
FALSE_BREAK_BREAK_DIST = 0.008       # 突破距离>0.8%（原1%）
FALSE_BREAK_VOL_RATIO = 1.2           # 突破日量比<1.2（原1.0，更宽松）
FALSE_BREAK_FALLBACK = 0.003
FALSE_BREAK_SCORE = 35                # 提高评分（原30）

# Behavior 5: Breakout Confirmation（真突破）
BREAKOUT_CONFIRM_DAYS = 2
BREAKOUT_VOL_INCREASE = 1.2
BREAKOUT_PRICE_RISE = 0.008
BREAKOUT_SCORE = 35

# Behavior 6: Trend Failure（趋势衰退）—— V6.2.3 阈值降低
TREND_FAIL_MA_SLOPE_NEG = -0.0005     # MA20斜率<-0.0005（原-0.001，更敏感）
TREND_FAIL_MA5_BELOW_MA10 = True
TREND_FAIL_ADX_DECLINE = 1             # ADX下降>1（原2）
TREND_FAIL_ATR_EXPAND = 1.1            # ATR扩张>1.1倍（原1.3）
TREND_FAIL_SCORE = 50                  # 提高评分（原45）

# Behavior 7: Panic Sell（恐慌杀跌）
PANIC_SELL_LOOKBACK = 3
PANIC_SELL_DROP_THRESHOLD = -0.04
PANIC_SELL_ATR_EXPAND = 1.3
PANIC_SELL_VOL_EXPLODE = 1.5
PANIC_SELL_Z_THRESHOLD = -1.8
PANIC_SELL_SCORE = 45

# V6.2.3 新增卖出行为
# Behavior 8: RSI Overbought（RSI超买）—— 降低评分避免牛市过早卖出
RSI_OVERBOUGHT_THRESHOLD = 68         # RSI > 68 视为超买
RSI_OVERBOUGHT_SCORE = 25             # 基础评分（降低，避免过早卖出）

# Behavior 9: MA Death Cross（均线死叉）
MA_DEATH_CROSS_SCORE = 40

# ============================================================
# 情绪修正系数（可在高算力优化中调优）
# ============================================================
# 恐慌中勇敢买入加分系数
PSYCH_PANIC_BUY_BOOST = 1.10
# 狂热中卖出加分系数
PSYCH_EUPHORIA_SELL_BOOST = 1.20
# 狂热中买入打折系数
PSYCH_EUPHORIA_BUY_CUT = 0.50
# 衰竭中卖出强烈加分系数
PSYCH_EXHAUSTION_SELL_BOOST = 1.25

# ============================================================
# 辅助评分因子
# ============================================================
AUX_MA20_TURNING_UP = 12
AUX_ADX_BULL_SUPPORT = 12
AUX_VOLUME_SUPPORT = 12
AUX_RSI_OVERSOLD_REBOUND = 15
AUX_DIVERGENCE_BULL = 18
AUX_RSI_BEAR_DIVERGENCE = 22
AUX_VOLUME_DECLINE = 18
AUX_MA5_MA10_DEAD_CROSS = 12
AUX_ZSCORE_EXTREME = 18

# ============================================================
# V5.0 新增：行为生命周期参数
# ============================================================

# 生命周期状态
LIFECYCLE_STATES = [
    'Candidate',      # 候选：行为首次被检测到
    'Observation',    # 观察：进入观察窗口，跟踪证据变化
    'Confirmed',      # 确认：置信度达到阈值，准备执行
    'Executed',       # 执行：已触发交易
    'Finished',       # 结束：交易完成或事件过期
]

# 观察窗口配置
OBSERVATION_WINDOW_MIN = 2   # 最少观察2个交易日
OBSERVATION_WINDOW_MAX = 3# 最多观察5个交易日
CONFIRMATION_THRESHOLD = 65 # 置信度 >= 65 才能确认执行
EXPIRY_THRESHOLD = 15# 置信度 < 25 则过期取消
CONFIDENCE_BASE = 50         # 新候选事件的初始置信度

# 置信度调整参数
CONFIDENCE_INCREMENT = 5# 每满足一个确认条件，增加5分
CONFIDENCE_DECREMENT = 10    # 每出现一个不利证据，减少10分
CONFIDENCE_MAX = 100         # 置信度上限
CONFIDENCE_MIN = 0           # 置信度下限

# ============================================================
# V5.0 新增：Crowd Psychology 参数
# ============================================================

# 市场情绪状态（按强度递增）
PSYCHOLOGY_STATES = [
    'Panic',       # 恐慌：极端下跌，散户割肉
    'Fear',        # 恐惧：悲观蔓延，观望为主
    'Hope',        # 希望：初见止跌，抄底犹豫
    'Optimism',    # 乐观：趋势确认，积极买入
    'Euphoria',    # 狂热：追涨杀跌，FOMO阶段
    'Exhaustion',  # 衰竭：动能耗尽，见顶信号
]

# 情绪状态切换阈值
PSYCH_Z_PANIC = -2.2        # Z-score < -2.2 → Panic
PSYCH_Z_FEAR = -1.5         # Z-score < -1.5 → Fear
PSYCH_Z_HOPE = -0.5         # Z-score between -1.5 and -0.5 → Hope
PSYCH_Z_OPTIMISM = 0.8      # Z-score between 0.5 and 1.5 → Optimism
PSYCH_Z_EUPHORIA = 1.5      # Z-score > 1.5 + RSI > 70 → Euphoria

# 情绪切换需要确认天数（避免噪声）
PSYCH_SWITCH_CONFIRM_DAYS = 2

# ============================================================
# V5.0 新增：Reward / Risk Evaluation 参数
# ============================================================

# Reward 因子权重
REWARD_WEIGHTS = {
    'dist_from_60d_high': 0.25,    # 距离60日高点越远，上涨空间越大
    'dist_from_60d_low': 0.15,     # 距离60日低点越近，安全边际越高
    'ma20_deviation': 0.20,        # 偏离MA20的程度
    'atr_position': 0.15,          # ATR位置（价格在近期波动范围内的位置）
    'volatility_percentile': 0.10, # 波动率分位数
    'trend_strength': 0.15,        # 趋势强度（ADX）
}

# Risk 因子权重
RISK_WEIGHTS = {
    'drawdown_risk': 0.30,          # 回撤风险（距近期高点距离）
    'volatility_risk': 0.20,         # 波动率风险（当前波动率 vs 历史）
    'trend_reversal_risk': 0.25,     # 趋势反转风险（ADX下降+MA走平）
    'volume_risk': 0.15,             # 成交量异常风险
    'ma_deviation_risk': 0.10,       # 均线偏离风险
}

# Reward/Risk 评分范围
REWARD_SCORE_MAX = 50     # Reward最高50分
RISK_SCORE_MAX = 50       # Risk最高50分

# ============================================================
# V5.0 新增：Acceleration / Deceleration 参数
# ============================================================

# 加速度计算
ACCEL_WINDOW_RECENT = 3    # 近期收益率窗口
ACCEL_WINDOW_PRIOR = 3     # 前期收益率窗口
ACCEL_FOMO_THRESHOLD = 0.02  # 加速度 > 2% → FOMO
ACCEL_DECEL_THRESHOLD = -0.01  # 减速度 < -1% → 动能衰减

# ============================================================
# V5.0 新增：Scoring Engine（新评分公式）
# ============================================================

# Final Buy Score = BehaviorScore + Confidence + Reward - Risk
# 各项权重（V6.2.3 最终版：最大化赔率）
SCORE_BEHAVIOR_WEIGHT = 0.30     # 行为分数的权重
SCORE_CONFIDENCE_WEIGHT = 0.15   # 置信度的权重
SCORE_REWARD_WEIGHT = 0.40       # 赔率的权重（提升到0.40）
SCORE_RISK_WEIGHT = 0.15         # 风险的权重

# 评分 → 买入仓位映射（基准仓位90%）
BUY_SCORE_THRESHOLDS = [
    (68, 0.95),   # >= 68: 加仓到95%（满仓）
    (62, 0.90),   # >= 62: 维持90%（基准）
    (56, 0.85),   # >= 56: 微减至85%
    (50, 0.75),   # >= 50: 减至75%
]

# 评分 → 卖出减仓映射
SELL_SCORE_THRESHOLDS = [
    (68, 0.50),   # >= 68: 减仓50%
    (62, 0.35),   # >= 62: 减仓35%
    (56, 0.20),   # >= 56: 减仓20%
    (50, 0.10),   # >= 50: 减仓10%
]

# ============================================================
# 市场状态权重系数
# ============================================================
REGIME_WEIGHTS = {
    'Bull': {
        'buy_mult': 1.80,    # 牛市中买入信号大幅加强
        'sell_div': 0.12,    # 牛市中卖出大幅削弱
    },
    'Bear': {
        'buy_mult': 0.90,    # 熊市中买入信号适度削弱
        'sell_div': 1.05,    # 熊市中卖出适度加强
    },
    'Range': {
        'buy_mult': 0.55,    # V6.2.3: 震荡市中大幅减少买入（交易往往不正确）
        'sell_div': 0.85,    # V6.2.3: 震荡市中卖出适度加强（及时止盈）
    },
    'Unknown': {
        'buy_mult': 0.75,
        'sell_div': 0.95,
    },
}

# ============================================================
# V6.2.3 新增：牛市止盈后重新入场参数
# ============================================================
# 在牛市中，止盈后不应空仓观望，应大胆重新买入
BULL_REENTRY_ENABLED = True       # 是否启用牛市直入机制
BULL_REENTRY_WINDOW = 5           # 止盈后N个交易日内可直入
BULL_REENTRY_BUY_BOOST = 1.50     # 直入窗口内买入评分乘数（Boost）
BULL_REENTRY_SCORE_DROP = 12      # 买入评分单日下降超过此值 → 视为止盈信号
BULL_REENTRY_SCORE_LOW = 55       # 买入评分低于此值 → 确认策略在减仓

# ============================================================
# 交易执行参数
# ============================================================
MAX_POSITION = 0.95           # 最大仓位95%（原85%），牛市中允许更高仓位
MIN_TRADE_VALUE = 50
SLIPPAGE = 0.001
COMMISSION = 0.0003

# ============================================================
# 回测参数
# ============================================================
BACKTEST_START = None  # None = 使用数据库最早日期
INITIAL_CASH = 10000.0
INITIAL_POSITION = 0.95   # 初始仓位95%，几乎满仓

# V6.2.3: 最低持仓天数
MIN_HOLD_DAYS = 10        # 买入后至少10天才能卖出

# V6.2.3: 交易执行阈值
TRADE_TARGET_DELTA = 0.02  # 目标仓位变化超过2%才执行
TRADE_ACTUAL_DELTA = 0.02  # 实际仓位偏离目标超过2%才执行
SCORE_HOLD_ZONE = 15       # HOLD区间

# ============================================================
# V5.0 新增：Grid Search 参数
# ============================================================
GRID_SEARCH_PARAMS = {
    'OBSERVATION_WINDOW_MAX': [3, 4, 5, 6],
    'CONFIRMATION_THRESHOLD': [60, 65, 70, 75, 80],
    'CONFIDENCE_INCREMENT': [5, 8, 10, 12],
    'EXPIRY_THRESHOLD': [15, 20, 25, 30],
    'DOUBLE_BOTTOM_REBOUND_MIN': [0.01, 0.015, 0.02],
    'MOMO_EXH_ACCEL_DECLINE': [0.5, 0.6, 0.7, 0.8],
    'PULLBACK_MA_DIST': [0.015, 0.02, 0.025],
    'PANIC_SELL_DROP_THRESHOLD': [-0.03, -0.04, -0.05],
}

# Pareto 优化目标权重
PARETO_OBJECTIVES = {
    'strategy_return': 1.0,    # 收益率权重
    'sharpe_ratio': 1.0,       # 夏普比率权重
    'max_drawdown': 1.0,       # 最大回撤权重（越小越好）
    'calmar_ratio': 1.0,       # Calmar比率权重
    'win_rate': 0.5,           # 胜率权重
    'total_trades': 0.3,       # 交易次数权重（适中的交易次数更好）
}

# ============================================================
# V6.2.3 新增：Evidence Engine 参数
# ============================================================

# 证据来源权重（Rule / Replay / ML / Emotion）
EVIDENCE_WEIGHTS = {
    'rule': 0.30,       # 人工规则置信度
    'replay': 0.25,     # 行为历史成功率统计
    'ml': 0.35,         # 机器学习概率输出
    'emotion': 0.10,    # 情绪修正
}

# 是否启用各证据源
EVIDENCE_ENABLE_REPLAY = True     # 启用Replay Learning
EVIDENCE_ENABLE_ML = True         # V6.2.3: ML默认启用
EVIDENCE_ENABLE_EMOTION_BONUS = True  # 情绪双确认

# ============================================================
# V6.2.3 升级：Replay Learning (Behavior Memory) 参数
# ============================================================
REPLAY_WINDOW_DAYS = 90           # 滑动窗口大小（交易日）
REPLAY_MIN_SAMPLES = 3            # 最小样本量（V6.2.3: 降低到3，配合Laplace平滑）
REPLAY_MAX_AGE_DAYS = 30          # 超过此天数无新样本 → 回归中性

# V6.2.3: 时间衰减参数（历史样本加权）
REPLAY_TAU_DAYS = 90              # 时间衰减常数τ（天），Weight = exp(-days/τ)
REPLAY_LAPLACE_ALPHA = 1.0        # Laplace平滑伪计数（α=1.0标准Laplace）

# V6.2.3: Replay键维度
# 当前使用: (Regime, Behavior, Psychology)
# 未来可扩展: (Regime, Behavior, Psychology, VolumeRegime)
REPLAY_KEY_DIMENSIONS = ['regime', 'behavior', 'psychology']

# 成功率 → 置信度乘数映射
REPLAY_HIGH_SUCCESS = 0.65        # 成功率高于此值 → 加分（V6.2.3调低到65%）
REPLAY_LOW_SUCCESS = 0.35         # 成功率低于此值 → 减分（V6.2.3调低到35%）
REPLAY_CONSECUTIVE_FAIL = 4       # 连续N次失败 → 紧急降权（V6.2.3放宽到4）

# ============================================================
# V6.2.3 升级：Time Decay 参数（关键修复）
# ============================================================
# V6.2.3问题: 衰减过于激进，所有置信度几乎全部压缩到0.1
# V6.2.3修复: 使用exp(-days/τ)平滑衰减，输出范围[0.5, 1.0]
TIME_DECAY_GRACE_PERIOD = 5       # 前N天不衰减（宽限期）
TIME_DECAY_TAU = 90               # V6.2.3: 衰减时间常数τ（天），越大衰减越慢
TIME_DECAY_MIN_MULTIPLIER = 0.7   # V6.2.3: 最小衰减乘数（提升到0.7，保留更多置信度）
TIME_DECAY_MIN_CONFIDENCE = 15    # 衰减到此值以下 → 过期标记（V6.2.3调低到15）

# ============================================================
# V6.2.3 新增：EmotionBuilder 参数
# ============================================================
EMOTION_METHOD = 'weighted'       # 融合方法: 'weighted' | 'pca' | 'ica'
EMOTION_PCA_COMPONENTS = 0.85     # PCA保留的累积方差比例

# 情绪改善检测窗口
EMOTION_IMPROVEMENT_WINDOW = 5    # 检测过去N天情绪是否改善

# ============================================================
# V6.2.3 升级：ML Confidence 参数
# ============================================================
ML_MODEL_TYPE = 'rf'              # 模型类型: 'rf' | 'gb' | 'lr'
ML_N_ESTIMATORS = 100             # 树的数量
ML_MAX_DEPTH = 5                  # 最大深度（防过拟合）
ML_MIN_TRAINING_SAMPLES = 30      # V6.2.3: 降低到30（更早开始学习）
ML_CV_FOLDS = 5                   # 交叉验证折数
ML_RETRAIN_FREQUENCY = 10         # V6.2.3: 每N笔交易后重新训练ML

# ============================================================
# V6.2.3 升级：Probability Calibration 参数
# ============================================================
CALIBRATION_METHOD = 'platt'      # 校准方法: 'platt' | 'isotonic' | 'temperature'
CALIBRATION_MIN_SAMPLES = 30      # V6.2.3: 降低校准样本门槛
CALIBRATION_AUTO_DISABLE = True   # V6.2.3: 若校准变差则自动禁用

# ============================================================
# V6.2.3 新增：Evidence Explainability 参数
# ============================================================
EXPLAIN_ENABLED = True            # 启用证据解释
EXPLAIN_DETAIL_LEVEL = 'full'     # 解释详细程度: 'full' | 'summary'
EXPLAIN_PRINT_PER_TRADE = True    # 每笔交易打印证据分解

# ============================================================
# V6.2.3 扩展接口
# ============================================================
FUTURE_FACTORS = {
    'news_sentiment': False,
    'etf_flow': False,
    'northbound_flow': False,
    'margin_balance': False,
    'macro_liquidity': False,
    'multi_etf_rotation': False,
    'ml_model': True,             # V6已启用
    'llm_sentiment': False,       # V6.5预留
}

# ==================== 盘中估算配置 ====================
INTRADAY_RUN_TIME = '14:40'              # 盘中运行时间
INTRADAY_VOLUME_RATIO_DEFAULT = 0.90     # 退化用固定成交量比例（当5分钟线数据不足时）
VOLUME_PROFILE_LOOKBACK_DAYS = 5         # 5分钟线回看天数（用于自动标定成交量比例）
