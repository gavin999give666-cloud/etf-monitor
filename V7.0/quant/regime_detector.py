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


def detect_phases(df, index, psychology_state, panic_events_confirmed=False):
    """
    V7.0 P6 相位检测：Overheat（高位过热）/ BottomFishing（熊末恐慌）

    相位不进 Replay 三维键，只作 L1 中枢乘数（见设计文档 §2.2）。

    Args:
        df: 含指标的 DataFrame
        index: 当前索引
        psychology_state: 当前情绪状态（'Panic'/'Euphoria'/'Exhaustion' 等）
        panic_events_confirmed: 是否有 PanicSell 或 DoubleBottom 事件已确认（由 strategy 传入）

    Returns:
        (overheat, bottom_fishing, detail)
        overheat: bool —— 高位过热相位
        bottom_fishing: bool —— 熊末恐慌相位
        detail: dict —— 触发明细（供信号输出/人工抽查）
    """
    overheat = False
    bottom_fishing = False
    detail = {'overheat': {}, 'bottom_fishing': {}}

    # ---- Overheat 高位过热 ----
    if index >= 60:
        close = get_value(df, index, 'close')
        high_60d = get_value(df, index, 'high_60d')
        if high_60d and high_60d > 0:
            dist_from_high = (close - high_60d) / high_60d
            if dist_from_high > -OVERHEAT_DIST_HIGH and psychology_state in ('Euphoria', 'Exhaustion'):
                # 条件1: MACD_hist 顶背离（近3日递减）
                macd_hist = get_value(df, index, 'MACD_hist')
                macd_hist_p1 = get_value(df, index - 1, 'MACD_hist')
                macd_hist_p2 = get_value(df, index - 2, 'MACD_hist')
                macd_divergence = macd_hist < macd_hist_p1 < macd_hist_p2

                # 条件2: 放量滞涨（vol_ratio>1.3 且 ROC5 衰减）
                vol_ratio = get_value(df, index, 'vol_ratio_to_mean')
                roc5 = get_value(df, index, 'ROC5')
                roc5_prev = get_value(df, index - 1, 'ROC5')
                vol_stagnation = vol_ratio > OVERHEAT_VOL_RATIO and roc5 < roc5_prev

                # 条件3: VWAP_dev > 2σ 持续 3 日
                vwap_dev = get_value(df, index, 'VWAP_dev')
                vwap_dev_p1 = get_value(df, index - 1, 'VWAP_dev')
                vwap_dev_p2 = get_value(df, index - 2, 'VWAP_dev')
                vwap_extreme = (vwap_dev > VWAP_DEV_EXTREME
                                and vwap_dev_p1 > VWAP_DEV_EXTREME
                                and vwap_dev_p2 > VWAP_DEV_EXTREME)

                if macd_divergence or vol_stagnation or vwap_extreme:
                    overheat = True
                    detail['overheat'] = {
                        'dist_from_high': round(dist_from_high * 100, 2),
                        'macd_divergence': bool(macd_divergence),
                        'vol_stagnation': bool(vol_stagnation),
                        'vwap_extreme': bool(vwap_extreme),
                    }

    # ---- BottomFishing 熊末恐慌 ----
    if index >= 60:
        vol_percentile = get_value(df, index, 'volatility_percentile')
        # volatility_percentile 为 0-1 小数，BOTTOMFISHING_VOL_PERCENTILE 为百分数（80）
        if (psychology_state == 'Panic' and panic_events_confirmed
                and vol_percentile * 100 > BOTTOMFISHING_VOL_PERCENTILE):
            bottom_fishing = True
            detail['bottom_fishing'] = {
                'volatility_percentile': round(vol_percentile * 100, 2),
                'panic_events_confirmed': True,
            }

    return overheat, bottom_fishing, detail


def volatility_target_multiplier(df, index):
    """
    V7.0 P6 波动率目标：center *= min(1.0, TARGET_VOL / realized_vol_20d)

    高波动期自动降中枢（替代形同虚设的 volatility_risk 评分项）。
    """
    realized_vol = get_value(df, index, 'Volatility20')
    if realized_vol and realized_vol > 0:
        return min(1.0, TARGET_VOL / realized_vol)
    return 1.0
