"""
V4.0 策略配置文件 —— 所有可调参数集中管理
（已校准：行为分数和阈值匹配实际市场条件）
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
REGIME_ADX_THRESHOLD = 20       # ADX < 20 为震荡
REGIME_MA_SLOPE_MIN = 0.001     # MA20斜率阈值
REGIME_BB_WIDTH_LOW = 0.02      # 布林带收窄阈值
REGIME_BB_WIDTH_HIGH = 0.06     # 布林带宽扩张阈值
REGIME_VOLATILITY_LOOKBACK = 20

# ============================================================
# 行为识别参数（已校准）
# ============================================================

# Behavior 1: Double Bottom（二次探底）
DOUBLE_BOTTOM_LOOKBACK = 30
DOUBLE_BOTTOM_REBOUND_MIN = 0.015  # 第一次低点后反弹至少1.5%（放松）
DOUBLE_BOTTOM_SECOND_LOW_MAX = 0.985  # 第二个低点不低于第一个的98.5%
DOUBLE_BOTTOM_VOL_SHRINK = 0.75      # 第二个低点成交量缩至前低的75%以下
DOUBLE_BOTTOM_SCORE = 45             # 30→45

# Behavior 2: Momentum Exhaustion（冲高衰竭）
MOMO_EXH_LOOKBACK = 5
MOMO_EXH_RETURN_THRESHOLD = 0.04  # 累计涨幅>4%（放松：6%→4%）
MOMO_EXH_RSI_RISE_MIN = 10       # RSI快速升高至少10点（放松：15→10）
MOMO_EXH_VOL_EXPAND = 1.3        # 成交量放大1.3倍（放松：1.5→1.3）
MOMO_EXH_ACCEL_DECLINE = 0.7     # 上涨速度衰减>30%（放松：0.5→0.7）
MOMO_EXH_SCORE = 55              # 40→55

# Behavior 3: Trend Pullback（趋势回踩）
PULLBACK_MA_DIST = 0.02          # 回踩MA10误差<2%（放松：1.5%→2%）
PULLBACK_VOL_SHRINK = 0.9        # 成交量萎缩至均量90%以下（放松：80%→90%）
PULLBACK_REQUIRE_BULL = False    # 不强制要求Bull（允许Range中回踩）
PULLBACK_SCORE = 30              # 20→30

# Behavior 4: False Break（假突破）
FALSE_BREAK_LOOKBACK = 3
FALSE_BREAK_BREAK_DIST = 0.01    # 突破MA20距离<1%（放松：0.5%→1%）
FALSE_BREAK_VOL_RATIO = 1.0      # 突破时成交量<均量100%（放松：0.8→1.0）
FALSE_BREAK_FALLBACK = 0.003     # 次日跌回MA20下方至少0.3%
FALSE_BREAK_SCORE = 30           # 20→30

# Behavior 5: Breakout Confirmation（真突破）
BREAKOUT_CONFIRM_DAYS = 2
BREAKOUT_VOL_INCREASE = 1.2      # 成交量较前日均量放大20%（放松：30%→20%）
BREAKOUT_PRICE_RISE = 0.008      # 突破日涨幅>0.8%（放松：1%→0.8%）
BREAKOUT_SCORE = 35              # 25→35

# Behavior 6: Trend Failure（趋势衰退）
TREND_FAIL_MA_SLOPE_NEG = -0.001
TREND_FAIL_MA5_BELOW_MA10 = True
TREND_FAIL_ADX_DECLINE = 2       # ADX下降至少2个点
TREND_FAIL_ATR_EXPAND = 1.3      # ATR扩大30%（提高门槛）
TREND_FAIL_SCORE = 45            # 50→45（降低评分以避免过度反应）            # 35→50

# Behavior 7: Panic Sell（恐慌杀跌）
PANIC_SELL_LOOKBACK = 3
PANIC_SELL_DROP_THRESHOLD = -0.04  # 连续暴跌>4%（放松：6%→4%）
PANIC_SELL_ATR_EXPAND = 1.3        # ATR扩大30%（放松：50%→30%）
PANIC_SELL_VOL_EXPLODE = 1.5       # 成交量爆炸1.5倍（放松：2.0→1.5）
PANIC_SELL_Z_THRESHOLD = -1.8      # Z-score < -1.8（放松：-2.0→-1.8）
PANIC_SELL_SCORE = 45              # 30→45

# ============================================================
# 辅助评分因子
# ============================================================
# 买入方向辅助因子
AUX_MA20_TURNING_UP = 12       # MA20开始上拐
AUX_ADX_BULL_SUPPORT = 12      # ADX支持上涨趋势
AUX_VOLUME_SUPPORT = 12        # 成交量支持（温和放量）
AUX_RSI_OVERSOLD_REBOUND = 15  # RSI超卖区域反弹
AUX_DIVERGENCE_BULL = 18       # 价格新低RSI未新低（底背离）

# 卖出方向辅助因子
AUX_RSI_BEAR_DIVERGENCE = 22   # RSI顶背离
AUX_VOLUME_DECLINE = 18        # 成交量衰退
AUX_MA5_MA10_DEAD_CROSS = 12   # 短线死叉
AUX_ZSCORE_EXTREME = 18        # Z-score极端值

# ============================================================
# 评分 → 仓位变化量（Delta 映射）
# V4.0核心创新：分数映射到「仓位变化量」而非「绝对目标」
# ============================================================
# BuyScore → 仓位增加量（+delta）
# 基于敏感性分析优化：更低的触发门槛 + 更大的加仓幅度
BUY_DELTA_MAP = [
    (80, 0.55),   # >= 80: 加仓55%
    (70, 0.42),   # >= 70: 加仓42%
    (60, 0.30),   # >= 60: 加仓30%
    (50, 0.20),   # >= 50: 加仓20%
    (45, 0.14),   # >= 45: 加仓14%
    (40, 0.10),   # >= 40: 加仓10%
    (36, 0.07),   # >= 36: 加仓7%
    (33, 0.04),   # >= 33: 加仓4%
]

# SellScore → 仓位减少量（-delta）
# 卖出比买入快：同样的分数，卖出幅度更大
SELL_DELTA_MAP = [
    (85, 0.50),   # >= 85: 减仓50%（几乎清仓）
    (75, 0.35),   # >= 75: 减仓35%
    (65, 0.25),   # >= 65: 减仓25%
    (55, 0.15),   # >= 55: 减仓15%
    (48, 0.10),   # >= 48: 减仓10%
    (42, 0.06),   # >= 42: 减仓6%
    (38, 0.04),   # >= 38: 减仓4%
    (34, 0.02),   # >= 34: 减仓2%
]

# 旧版兼容（保留但不再使用）
BUY_SCORE_POSITION_MAP = []
SELL_SCORE_POSITION_MAP = []

# ============================================================
# 市场状态权重系数
# ============================================================
REGIME_WEIGHTS = {
    'Bull': {
        'buy_mult': 1.15,
        'sell_div': 0.80,
    },
    'Bear': {
        'buy_mult': 0.85,       # 熊市买入打85折（不要太低，留出加仓空间）
        'sell_div': 1.15,       # 熊市卖出加分15%（不要太高，避免过于敏感）
    },
    'Range': {
        'buy_mult': 1.0,
        'sell_div': 1.0,
    },
    'Unknown': {
        'buy_mult': 0.8,
        'sell_div': 1.0,
    },
}

# ============================================================
# 交易执行参数
# ============================================================
MAX_POSITION = 0.60
MIN_TRADE_VALUE = 50             # 降低最小交易金额
SLIPPAGE = 0.001
COMMISSION = 0.0003

# ============================================================
# 回测参数
# ============================================================
BACKTEST_START = '2025-09-01'
INITIAL_CASH = 10000.0
INITIAL_POSITION = 0.60

# ============================================================
# V5 可扩展接口预留
# ============================================================
FUTURE_FACTORS = {
    'news_sentiment': False,
    'etf_flow': False,
    'northbound_flow': False,
    'margin_balance': False,
    'macro_liquidity': False,
    'multi_etf_rotation': False,
    'ml_model': False,
}
