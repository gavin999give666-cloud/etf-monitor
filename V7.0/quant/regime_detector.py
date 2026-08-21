"""
V5.0 市场状态识别模块
"""
import pandas as pd
import numpy as np
from config import *
from indicators import get_value


def detect_market_regime(df, index):
    if index < 30:
        return 'Unknown'

    adx = get_value(df, index, 'ADX14')
    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')
    ma20_slope = get_value(df, index, 'MA20_slope')
    bb_width = get_value(df, index, 'BB_width')
    adx_prev = get_value(df, index - 1, 'ADX14')
    ma5 = get_value(df, index, 'MA5')
    ma10 = get_value(df, index, 'MA10')

    price_above_ma20 = close > ma20
    ma20_rising = ma20_slope > REGIME_MA_SLOPE_MIN
    ma20_falling = ma20_slope < -REGIME_MA_SLOPE_MIN
    adx_rising = adx > adx_prev
    bb_narrow = bb_width < REGIME_BB_WIDTH_LOW
    bb_wide = bb_width > REGIME_BB_WIDTH_HIGH

    ma_bullish = ma5 > ma10 > ma20
    ma_bearish = ma5 < ma10 < ma20

    if adx < REGIME_ADX_THRESHOLD and bb_narrow:
        return 'Range'
    if adx < REGIME_ADX_THRESHOLD and abs(close / ma20 - 1) < 0.02:
        return 'Range'
    if bb_narrow and abs(close / ma20 - 1) < 0.015:
        return 'Range'

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

    if bull_score >= 6:
        return 'Bull'
    elif bear_score >= 6:
        return 'Bear'
    elif bull_score >= bear_score + 2:
        return 'Bull'
    elif bear_score >= bull_score + 2:
        return 'Bear'
    else:
        if ma20_rising and price_above_ma20:
            return 'Bull' if adx >= REGIME_ADX_THRESHOLD else 'Range'
        elif ma20_falling and not price_above_ma20:
            return 'Bear' if adx >= REGIME_ADX_THRESHOLD else 'Range'
        return 'Range'
