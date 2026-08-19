"""
V5.0 行为检测模块（Behavior Detector）
=======================================

与 V4.0 核心区别：
- V4.0: 检测到行为 → 立即评分 → 触发交易
- V5.0: 检测到行为 → 作为"候选事件"提交给 EventEngine
        → EventEngine 管理生命周期 → 确认后才执行

行为检测仍然使用相同的技术指标和阈值，
但检测结果不再直接转化为交易信号。
"""
import numpy as np
from config import *
from indicators import get_value


# ============================================================
# Behavior 1: Double Bottom（二次探底）
# ============================================================
def detect_double_bottom(df, index):
    if index < DOUBLE_BOTTOM_LOOKBACK:
        return False, 0, {}

    close_arr = df['close'].values
    low_arr = df['low'].values
    vol_arr = df['volume'].values

    lookback = DOUBLE_BOTTOM_LOOKBACK
    end = index
    start = max(0, end - lookback)

    segment_low = low_arr[start:end + 1]
    segment_close = close_arr[start:end + 1]
    segment_vol = vol_arr[start:end + 1]

    min_threshold = len(segment_low) // 3
    if min_threshold < 5:
        return False, 0, {}

    local_mins = []
    for i in range(2, len(segment_low) - 2):
        if (segment_low[i] <= segment_low[i-1] and segment_low[i] <= segment_low[i-2] and
                segment_low[i] <= segment_low[i+1] and segment_low[i] <= segment_low[i+2]):
            local_mins.append((start + i, segment_low[i], segment_vol[i]))

    if len(local_mins) < 2:
        return False, 0, {}

    local_mins.sort(key=lambda x: x[0], reverse=True)
    second_low = local_mins[0]
    first_low = local_mins[1]

    if second_low[0] - first_low[0] < 5:
        return False, 0, {}

    if second_low[1] < first_low[1] * DOUBLE_BOTTOM_SECOND_LOW_MAX:
        return False, 0, {}

    mid_segment = close_arr[first_low[0]:second_low[0] + 1]
    if len(mid_segment) < 3:
        return False, 0, {}
    mid_max = np.max(mid_segment)
    if mid_max / first_low[1] - 1 < DOUBLE_BOTTOM_REBOUND_MIN:
        return False, 0, {}

    vol_ratio = second_low[2] / (first_low[2] + 1e-10)
    if vol_ratio > DOUBLE_BOTTOM_VOL_SHRINK:
        return False, 0, {}

    current_close = close_arr[end]
    if current_close < second_low[1] * 1.005:
        return False, 0, {}

    evidence = {
        'first_low': float(first_low[1]),
        'second_low': float(second_low[1]),
        'vol_shrink_ratio': float(vol_ratio),
        'rebound_pct': float(current_close / second_low[1] - 1),
    }
    return True, DOUBLE_BOTTOM_SCORE, evidence


# ============================================================
# Behavior 2: Momentum Exhaustion（冲高衰竭）
# ============================================================
def detect_momentum_exhaustion(df, index):
    if index < MOMO_EXH_LOOKBACK + 2:
        return False, 0, {}

    close_arr = df['close'].values

    cum_return = close_arr[index] / close_arr[index - MOMO_EXH_LOOKBACK] - 1
    if cum_return < MOMO_EXH_RETURN_THRESHOLD:
        return False, 0, {}

    rsi_now = get_value(df, index, 'RSI14')
    rsi_5d_ago = get_value(df, index - MOMO_EXH_LOOKBACK, 'RSI14')
    rsi_rise = rsi_now - rsi_5d_ago
    if rsi_rise < MOMO_EXH_RSI_RISE_MIN:
        return False, 0, {}

    vol_now = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')
    vol_ratio = vol_now / (vol20 + 1e-10)
    if vol_ratio < MOMO_EXH_VOL_EXPAND:
        return False, 0, {}

    recent_return = close_arr[index] / close_arr[index - 2] - 1
    prior_return = close_arr[index - 2] / close_arr[index - MOMO_EXH_LOOKBACK] - 1
    if prior_return <= 0:
        return False, 0, {}
    accel_ratio = recent_return / prior_return
    if accel_ratio > MOMO_EXH_ACCEL_DECLINE:
        return False, 0, {}

    open_today = df['open'].values[index]
    weak_close = close_arr[index] < open_today

    evidence = {
        'cum_return_5d': float(cum_return),
        'rsi_rise': float(rsi_rise),
        'vol_ratio': float(vol_ratio),
        'accel_ratio': float(accel_ratio),
        'weak_close': weak_close,
    }

    bonus = 5 if weak_close else 0
    return True, MOMO_EXH_SCORE + bonus, evidence


# ============================================================
# Behavior 3: Trend Pullback（趋势回踩）
# ============================================================
def detect_trend_pullback(df, index, regime):
    if index < 25:
        return False, 0, {}

    if PULLBACK_REQUIRE_BULL and regime != 'Bull':
        return False, 0, {}

    close = get_value(df, index, 'close')
    ma10 = get_value(df, index, 'MA10')
    ma10_prev = get_value(df, index - 1, 'MA10')

    ma10_dist = abs(close - ma10) / ma10
    if ma10_dist > PULLBACK_MA_DIST:
        return False, 0, {}

    if ma10 < ma10_prev:
        return False, 0, {}

    vol = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')
    vol_ratio = vol / (vol20 + 1e-10)
    if vol_ratio > PULLBACK_VOL_SHRINK:
        return False, 0, {}

    rsi = get_value(df, index, 'RSI14')
    if rsi > 65:
        return False, 0, {}

    evidence = {
        'ma10_dist': float(ma10_dist),
        'vol_ratio': float(vol_ratio),
        'rsi': float(rsi),
    }
    return True, PULLBACK_SCORE, evidence


# ============================================================
# Behavior 4: False Break（假突破）
# ============================================================
def detect_false_break(df, index):
    if index < FALSE_BREAK_LOOKBACK + 1:
        return False, 0, {}

    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')
    close_prev = get_value(df, index - 1, 'close')
    ma20_prev = get_value(df, index - 1, 'MA20')

    broke_prev = close_prev > ma20_prev * (1 + FALSE_BREAK_BREAK_DIST) and \
        get_value(df, index - 2, 'close') <= get_value(df, index - 2, 'MA20')

    broke_prev2 = get_value(df, index - 2, 'close') > get_value(df, index - 2, 'MA20') * (1 + FALSE_BREAK_BREAK_DIST) and \
        get_value(df, index - 3, 'close') <= get_value(df, index - 3, 'MA20')

    if not broke_prev and not broke_prev2:
        return False, 0, {}

    break_day = index - 1 if broke_prev else index - 2

    break_vol = df['volume'].values[break_day]
    break_vol20 = get_value(df, break_day, 'Vol20')
    vol_ratio = break_vol / (break_vol20 + 1e-10)
    if vol_ratio > FALSE_BREAK_VOL_RATIO:
        return False, 0, {}

    if close > ma20:
        return False, 0, {}

    evidence = {
        'break_day': break_day,
        'break_vol_ratio': float(vol_ratio),
        'fallback_dist': float(close / ma20 - 1),
    }
    return True, FALSE_BREAK_SCORE, evidence


# ============================================================
# Behavior 5: Breakout Confirmation（真突破）
# ============================================================
def detect_breakout_confirm(df, index, regime):
    if index < BREAKOUT_CONFIRM_DAYS + 2:
        return False, 0, {}

    close_arr = df['close'].values
    ma20_arr = df['MA20'].values

    for d in range(BREAKOUT_CONFIRM_DAYS):
        if index - d < 0 or close_arr[index - d] <= ma20_arr[index - d]:
            return False, 0, {}

    if close_arr[index - BREAKOUT_CONFIRM_DAYS - 1] > ma20_arr[index - BREAKOUT_CONFIRM_DAYS - 1]:
        return False, 0, {}

    break_vols = df['volume'].values[index - BREAKOUT_CONFIRM_DAYS + 1:index + 1]
    avg_break_vol = np.mean(break_vols)
    vol20 = get_value(df, index, 'Vol20')
    vol_ratio = avg_break_vol / (vol20 + 1e-10)
    if vol_ratio < BREAKOUT_VOL_INCREASE:
        return False, 0, {}

    break_return = close_arr[index - BREAKOUT_CONFIRM_DAYS + 1] / close_arr[index - BREAKOUT_CONFIRM_DAYS] - 1
    if break_return < BREAKOUT_PRICE_RISE:
        return False, 0, {}

    rsi = get_value(df, index, 'RSI14')
    if rsi > 75:
        return False, 0, {}

    evidence = {
        'break_return': float(break_return),
        'vol_ratio': float(vol_ratio),
        'rsi': float(rsi),
    }
    return True, BREAKOUT_SCORE, evidence


# ============================================================
# Behavior 6: Trend Failure（趋势衰退）
# ============================================================
def detect_trend_failure(df, index, regime):
    if index < 30:
        return False, 0, {}

    ma5 = get_value(df, index, 'MA5')
    ma10 = get_value(df, index, 'MA10')
    ma20_slope = get_value(df, index, 'MA20_slope')
    adx = get_value(df, index, 'ADX14')
    adx_5d_ago = get_value(df, index - 5, 'ADX14')
    atr = get_value(df, index, 'ATR14')
    atr_10d_ago = get_value(df, index - 10, 'ATR14')
    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')

    failure_signals = 0
    evidence = {}

    if ma20_slope < TREND_FAIL_MA_SLOPE_NEG:
        failure_signals += 1
        evidence['ma20_declining'] = float(ma20_slope)

    if ma5 < ma10:
        failure_signals += 1
        evidence['ma5_below_ma10'] = True

    adx_change = adx_5d_ago - adx
    if adx_change >= TREND_FAIL_ADX_DECLINE:
        failure_signals += 1
        evidence['adx_decline'] = float(adx_change)

    if atr_10d_ago > 0:
        atr_expansion = atr / atr_10d_ago
        if atr_expansion >= TREND_FAIL_ATR_EXPAND:
            failure_signals += 1
            evidence['atr_expansion'] = float(atr_expansion)

    if close < ma20:
        failure_signals += 1
        evidence['below_ma20'] = True

    if failure_signals < 3:                    # V6.2.3: 降为3个信号即可触发（原4）
        return False, 0, evidence

    evidence['failure_count'] = failure_signals
    return True, TREND_FAIL_SCORE, evidence


# ============================================================
# Behavior 7: Panic Sell（恐慌杀跌）
# ============================================================
def detect_panic_sell(df, index):
    if index < PANIC_SELL_LOOKBACK + 5:
        return False, 0, {}

    close = get_value(df, index, 'close')
    close_3d = get_value(df, index - PANIC_SELL_LOOKBACK, 'close')
    atr = get_value(df, index, 'ATR14')
    atr_10d = get_value(df, index - 10, 'ATR14')
    z20 = get_value(df, index, 'Z20')
    rsi = get_value(df, index, 'RSI14')
    vol = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')

    cum_drop = close / close_3d - 1
    if cum_drop > PANIC_SELL_DROP_THRESHOLD:
        return False, 0, {}

    if atr_10d > 0:
        atr_expansion = atr / atr_10d
        if atr_expansion < PANIC_SELL_ATR_EXPAND:
            return False, 0, {}
    else:
        return False, 0, {}

    vol_ratio = vol / (vol20 + 1e-10)
    if vol_ratio < PANIC_SELL_VOL_EXPLODE:
        return False, 0, {}

    if z20 > PANIC_SELL_Z_THRESHOLD:
        return False, 0, {}

    if rsi > 35:
        return False, 0, {}

    evidence = {
        'cum_drop': float(cum_drop),
        'atr_expansion': float(atr_expansion),
        'vol_ratio': float(vol_ratio),
        'z20': float(z20),
        'rsi': float(rsi),
    }
    return True, PANIC_SELL_SCORE, evidence


# ============================================================
# V6.2.3 新增：Behavior 8: RSI Overbought（RSI超买卖出信号）
# ============================================================
def detect_rsi_overbought(df, index):
    """
    检测RSI超买状态。

    条件：
    - RSI > RSI_OVERBOUGHT_THRESHOLD (68)
    - 价格高于MA20（处于上升趋势中）
    - RSI开始回落（比前一天低）
    """
    if index < 20:
        return False, 0, {}

    rsi = get_value(df, index, 'RSI14')
    rsi_prev = get_value(df, index - 1, 'RSI14')
    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')

    if rsi < RSI_OVERBOUGHT_THRESHOLD:
        return False, 0, {}

    if close < ma20:
        return False, 0, {}

    # RSI正在回落（从超买区下降）→ 卖出信号更强
    bonus = 10 if rsi < rsi_prev else 0

    evidence = {
        'rsi': float(rsi),
        'rsi_declining': rsi < rsi_prev,
        'close_above_ma20': close > ma20,
    }

    return True, RSI_OVERBOUGHT_SCORE + bonus, evidence


# ============================================================
# V6.2.3 新增：Behavior 9: MA Death Cross（均线死叉卖出信号）
# ============================================================
def detect_ma_death_cross(df, index):
    """
    检测MA5下穿MA10死叉。

    条件：
    - 今天 MA5 < MA10（死叉已形成）
    - 昨天 MA5 > MA10（今天刚形成死叉）
    - 价格低于MA20（趋势转弱）
    """
    if index < 20:
        return False, 0, {}

    ma5 = get_value(df, index, 'MA5')
    ma10 = get_value(df, index, 'MA10')
    ma5_prev = get_value(df, index - 1, 'MA5')
    ma10_prev = get_value(df, index - 1, 'MA10')
    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')

    # 今天刚形成死叉
    if not (ma5_prev > ma10_prev and ma5 < ma10):
        return False, 0, {}

    # 价格在MA20下方 → 确认弱势
    weak_close = close < ma20

    evidence = {
        'ma5': float(ma5),
        'ma10': float(ma10),
        'close_below_ma20': weak_close,
    }

    bonus = 15 if weak_close else 0
    return True, MA_DEATH_CROSS_SCORE + bonus, evidence


# ============================================================
# 辅助评分因子检测
# ============================================================
def detect_aux_factors(df, index, regime):
    buy_score = 0
    sell_score = 0
    evidence = {}

    ma20_slope = get_value(df, index, 'MA20_slope')
    ma20_slope_prev = get_value(df, index - 1, 'MA20_slope')
    if ma20_slope > REGIME_MA_SLOPE_MIN and ma20_slope_prev <= 0:
        buy_score += AUX_MA20_TURNING_UP
        evidence['ma20_turning_up'] = True

    adx = get_value(df, index, 'ADX14')
    plus_di = get_value(df, index, 'plus_di')
    minus_di = get_value(df, index, 'minus_di')
    if adx > 25 and plus_di > minus_di:
        buy_score += AUX_ADX_BULL_SUPPORT
        evidence['adx_bull'] = True

    vol = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')
    close = get_value(df, index, 'close')
    if 1.1 < vol / vol20 < 1.8 and close > get_value(df, index - 1, 'close'):
        buy_score += AUX_VOLUME_SUPPORT
        evidence['vol_support'] = True

    rsi = get_value(df, index, 'RSI14')
    rsi_prev = get_value(df, index - 1, 'RSI14')
    if rsi_prev < 32 and rsi > rsi_prev and rsi < 50:
        buy_score += AUX_RSI_OVERSOLD_REBOUND
        evidence['rsi_oversold_rebound'] = True

    close_10d_low = df['close'].rolling(10).min().values[index]
    rsi_10d_low = df['RSI14'].rolling(10).min().values[index]
    if close <= close_10d_low * 1.01 and rsi > rsi_10d_low * 1.05:
        buy_score += AUX_DIVERGENCE_BULL
        evidence['bull_divergence'] = True

    close_10d_high = df['close'].rolling(10).max().values[index]
    rsi_10d_high = df['RSI14'].rolling(10).max().values[index]
    if close >= close_10d_high * 0.99 and rsi < rsi_10d_high * 0.95:
        sell_score += AUX_RSI_BEAR_DIVERGENCE
        evidence['bear_divergence'] = True

    vol_3d_avg = np.mean(df['volume'].values[max(0, index - 3):index + 1])
    vol_10d_avg = np.mean(df['volume'].values[max(0, index - 10):index + 1])
    if vol_10d_avg > 0 and vol_3d_avg / vol_10d_avg < 0.7:
        sell_score += AUX_VOLUME_DECLINE
        evidence['vol_decline'] = True

    ma5 = get_value(df, index, 'MA5')
    ma10 = get_value(df, index, 'MA10')
    if ma5 < ma10 and get_value(df, index - 1, 'MA5') > get_value(df, index - 1, 'MA10'):
        sell_score += AUX_MA5_MA10_DEAD_CROSS
        evidence['dead_cross'] = True

    z20 = get_value(df, index, 'Z20')
    if z20 > 2.0:
        sell_score += AUX_ZSCORE_EXTREME
        evidence['z_extreme'] = float(z20)

    return buy_score, sell_score, evidence


# ============================================================
# 行为检测汇总
# ============================================================
def detect_all_behaviors(df, index, regime):
    """
    检测所有行为（V5.0版本：仅为Event Engine提供候选事件）

    Returns:
        {
            'buy_behaviors': [(name, score, evidence), ...],
            'sell_behaviors': [(name, score, evidence), ...],
            'aux_buy_score': float,
            'aux_sell_score': float,
            'aux_evidence': {},
            'regime': str,
        }
    """
    buy_behaviors = []
    sell_behaviors = []

    detected, score, evidence = detect_double_bottom(df, index)
    if detected:
        buy_behaviors.append(('DoubleBottom', score, evidence))

    detected, score, evidence = detect_momentum_exhaustion(df, index)
    if detected:
        sell_behaviors.append(('MomentumExhaustion', score, evidence))

    detected, score, evidence = detect_trend_pullback(df, index, regime)
    if detected:
        buy_behaviors.append(('TrendPullback', score, evidence))

    detected, score, evidence = detect_false_break(df, index)
    if detected:
        sell_behaviors.append(('FalseBreak', score, evidence))

    detected, score, evidence = detect_breakout_confirm(df, index, regime)
    if detected:
        buy_behaviors.append(('BreakoutConfirm', score, evidence))

    detected, score, evidence = detect_trend_failure(df, index, regime)
    if detected:
        sell_behaviors.append(('TrendFailure', score, evidence))

    detected, score, evidence = detect_panic_sell(df, index)
    if detected:
        buy_behaviors.append(('PanicSell', score, evidence))

    # V6.2.3 新增卖出行为
    detected, score, evidence = detect_rsi_overbought(df, index)
    if detected:
        sell_behaviors.append(('RSI_Overbought', score, evidence))

    detected, score, evidence = detect_ma_death_cross(df, index)
    if detected:
        sell_behaviors.append(('MA_DeathCross', score, evidence))

    aux_buy, aux_sell, aux_evidence = detect_aux_factors(df, index, regime)

    return {
        'buy_behaviors': buy_behaviors,
        'sell_behaviors': sell_behaviors,
        'aux_buy_score': aux_buy,
        'aux_sell_score': aux_sell,
        'aux_evidence': aux_evidence,
        'regime': regime,
    }
