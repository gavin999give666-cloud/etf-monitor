"""
V4.0 市场状态识别模块（Market Regime Detection）

第一层：每天首先判断当前属于 上涨趋势/震荡/下跌趋势
综合：MA20斜率、ADX、ATR变化、布林带宽、波动率变化
"""
import pandas as pd
import numpy as np
from config import *
from indicators import get_value


def detect_market_regime(df, index):
    """
    市场状态识别
    
    综合判断逻辑：
    1. ADX < 20 → 倾向于震荡（Range）
    2. ADX >= 20 且 close > MA20 且 MA20_slope > 0 → 上涨趋势（Bull）
    3. ADX >= 20 且 close < MA20 且 MA20_slope < 0 → 下跌趋势（Bear）
    4. 布林带宽辅助：收窄→震荡，扩张+方向→趋势
    5. 波动率辅助：低波动+ADX低→确认震荡
    
    Returns:
        'Bull' / 'Range' / 'Bear' / 'Unknown'
    """
    if index < 30:
        return 'Unknown'

    adx = get_value(df, index, 'ADX14')
    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')
    ma20_slope = get_value(df, index, 'MA20_slope')
    bb_width = get_value(df, index, 'BB_width')
    adx_prev = get_value(df, index - 1, 'ADX14')
    vol20 = get_value(df, index, 'Volatility20')
    ma5 = get_value(df, index, 'MA5')
    ma10 = get_value(df, index, 'MA10')

    # 辅助判断变量
    price_above_ma20 = close > ma20
    ma20_rising = ma20_slope > REGIME_MA_SLOPE_MIN
    ma20_falling = ma20_slope < -REGIME_MA_SLOPE_MIN
    adx_rising = adx > adx_prev
    bb_narrow = bb_width < REGIME_BB_WIDTH_LOW
    bb_wide = bb_width > REGIME_BB_WIDTH_HIGH
    
    # 均线排列
    ma_bullish = ma5 > ma10 > ma20
    ma_bearish = ma5 < ma10 < ma20

    # ---- 震荡市判断 ----
    # ADX低 + 布林带收窄（经典震荡特征）
    if adx < REGIME_ADX_THRESHOLD and bb_narrow:
        return 'Range'
    
    # ADX低 + 价格在MA20附近纠缠
    if adx < REGIME_ADX_THRESHOLD and abs(close / ma20 - 1) < 0.02:
        return 'Range'
    
    # 价格在布林带内窄幅波动
    if bb_narrow and abs(close / ma20 - 1) < 0.015:
        return 'Range'

    # ---- 上涨趋势判断 ----
    bull_score = 0
    if price_above_ma20:
        bull_score += 2
    if ma20_rising:
        bull_score += 2
    if adx >= REGIME_ADX_THRESHOLD:
        bull_score += 1
    if adx_rising:
        bull_score += 1
    if ma_bullish:
        bull_score += 2
    if bb_wide and price_above_ma20:
        bull_score += 1

    # ---- 下跌趋势判断 ----
    bear_score = 0
    if not price_above_ma20:
        bear_score += 2
    if ma20_falling:
        bear_score += 2
    if adx >= REGIME_ADX_THRESHOLD:
        bear_score += 1
    if not adx_rising:
        bear_score += 1
    if ma_bearish:
        bear_score += 2
    if bb_wide and not price_above_ma20:
        bear_score += 1

    # 综合裁决
    if bull_score >= 6:
        return 'Bull'
    elif bear_score >= 6:
        return 'Bear'
    elif bull_score >= bear_score + 2:
        return 'Bull'
    elif bear_score >= bull_score + 2:
        return 'Bear'
    else:
        # 均线方向辅助
        if ma20_rising and price_above_ma20:
            return 'Bull' if adx >= REGIME_ADX_THRESHOLD else 'Range'
        elif ma20_falling and not price_above_ma20:
            return 'Bear' if adx >= REGIME_ADX_THRESHOLD else 'Range'
        return 'Range'


def get_regime_stats(df, start_index=30):
    """统计回测期间各市场状态出现频率"""
    regimes = []
    for i in range(start_index, len(df)):
        regimes.append(detect_market_regime(df, i))
    
    total = len(regimes)
    stats = {
        'Bull': regimes.count('Bull') / total * 100 if total > 0 else 0,
        'Range': regimes.count('Range') / total * 100 if total > 0 else 0,
        'Bear': regimes.count('Bear') / total * 100 if total > 0 else 0,
        'Unknown': regimes.count('Unknown') / total * 100 if total > 0 else 0,
    }
    return stats
