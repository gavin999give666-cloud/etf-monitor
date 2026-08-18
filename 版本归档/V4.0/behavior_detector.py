"""
V4.0 行为识别模块（Behavior Recognition）  —— 核心模块

第二层：识别市场正在发生什么行为，而不是直接看指标数值。
每个行为独立检测，返回 (detected, score, evidence_dict)
指标只是行为的「证据」，不是交易依据。

新增指标说明：
- VWAP_dev: 辅助假突破识别（价格过度偏离日内公允价不合理）
- ROC3: 辅助冲高衰竭识别（短期急速上涨）
- Volatility5: 辅助市场状态（短期波动率变化）
"""
import numpy as np
from config import *
from indicators import get_value


# ============================================================
# Behavior 1: Double Bottom（二次探底）
# 目标：第一次止跌不买，等待二次确认后才买
# 逻辑：第一次低点 → 反弹 → 第二次低点 → 未创新低 → 成交量缩小 → 确认底部
# ============================================================
def detect_double_bottom(df, index):
    """
    检测二次探底行为
    
    关键条件：
    1. 回溯30日内存在两个明显的低点
    2. 第二个低点不破第一个低点（或创新低但不明显）
    3. 第二个低点成交量明显缩小
    4. 当前价格开始从第二个低点反弹
    """
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

    # 找两个最低点（至少间隔5天）
    min_threshold = len(segment_low) // 3
    if min_threshold < 5:
        return False, 0, {}

    # 找到局部极小值
    local_mins = []
    for i in range(2, len(segment_low) - 2):
        if segment_low[i] <= segment_low[i-1] and segment_low[i] <= segment_low[i-2] and \
           segment_low[i] <= segment_low[i+1] and segment_low[i] <= segment_low[i+2]:
            local_mins.append((start + i, segment_low[i], segment_vol[i]))

    if len(local_mins) < 2:
        return False, 0, {}

    # 取最近的两个低点
    local_mins.sort(key=lambda x: x[0], reverse=True)
    second_low = local_mins[0]  # 最近的
    first_low = local_mins[1]   # 较早的

    # 检查时间间隔（至少5天）
    if second_low[0] - first_low[0] < 5:
        return False, 0, {}

    # 检查：第二个低点不创新低（容忍2%误差）
    if second_low[1] < first_low[1] * DOUBLE_BOTTOM_SECOND_LOW_MAX:
        return False, 0, {}

    # 检查：中间有反弹（反弹幅度>2%）
    mid_segment = close_arr[first_low[0]:second_low[0] + 1]
    if len(mid_segment) < 3:
        return False, 0, {}
    mid_max = np.max(mid_segment)
    if mid_max / first_low[1] - 1 < DOUBLE_BOTTOM_REBOUND_MIN:
        return False, 0, {}

    # 检查：第二个低点成交量缩小
    vol_ratio = second_low[2] / (first_low[2] + 1e-10)
    if vol_ratio > DOUBLE_BOTTOM_VOL_SHRINK:
        return False, 0, {}

    # 当前价格开始反弹
    current_close = close_arr[end]
    current_vol = vol_arr[end]
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
# A股最重要的顶部行为：急速上涨后动能衰竭
# 关键：上涨速度急剧升高后开始衰减 → 立即减仓，不等RSI>70
# ============================================================
def detect_momentum_exhaustion(df, index):
    """
    检测冲高衰竭行为
    
    逻辑：
    1. 近5日累计涨幅 > 6%
    2. RSI快速升高（对比5日前上升>15点）
    3. 成交量放大
    4. 上涨速度开始衰减（最近1-2日涨幅 < 前两日涨幅）
    """
    if index < MOMO_EXH_LOOKBACK + 2:
        return False, 0, {}

    close_arr = df['close'].values

    # 近5日累计涨幅
    cum_return = close_arr[index] / close_arr[index - MOMO_EXH_LOOKBACK] - 1
    if cum_return < MOMO_EXH_RETURN_THRESHOLD:
        return False, 0, {}

    # RSI快速升高
    rsi_now = get_value(df, index, 'RSI14')
    rsi_5d_ago = get_value(df, index - MOMO_EXH_LOOKBACK, 'RSI14')
    rsi_rise = rsi_now - rsi_5d_ago
    if rsi_rise < MOMO_EXH_RSI_RISE_MIN:
        return False, 0, {}

    # 成交量放大（当前成交量 vs 20日均量）
    vol_now = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')
    vol_ratio = vol_now / (vol20 + 1e-10)
    if vol_ratio < MOMO_EXH_VOL_EXPAND:
        return False, 0, {}

    # 上涨速度衰减
    recent_return = close_arr[index] / close_arr[index - 2] - 1
    prior_return = close_arr[index - 2] / close_arr[index - MOMO_EXH_LOOKBACK] - 1
    if prior_return <= 0:
        return False, 0, {}
    accel_ratio = recent_return / prior_return
    if accel_ratio > MOMO_EXH_ACCEL_DECLINE:
        return False, 0, {}

    # 加分：今日收阴或高开低走
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
# 上涨趋势中回踩MA10 + 成交量萎缩 → 买入机会
# 不是任何回踩都可以买，必须在上涨趋势中
# ============================================================
def detect_trend_pullback(df, index, regime):
    """
    检测趋势回踩行为
    
    要求：
    1. 上涨趋势确认（must be Bull）
    2. 价格回踩接近MA10（误差<1.5%）
    3. 成交量萎缩（相对于20日均量）
    4. MA10仍然向上
    """
    if index < 25:
        return False, 0, {}

    if PULLBACK_REQUIRE_BULL and regime != 'Bull':
        return False, 0, {}

    close = get_value(df, index, 'close')
    ma10 = get_value(df, index, 'MA10')
    ma5 = get_value(df, index, 'MA5')
    ma10_prev = get_value(df, index - 1, 'MA10')

    # 价格接近MA10
    ma10_dist = abs(close - ma10) / ma10
    if ma10_dist > PULLBACK_MA_DIST:
        return False, 0, {}

    # MA10仍然向上
    if ma10 < ma10_prev:
        return False, 0, {}

    # 成交量萎缩
    vol = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')
    vol_ratio = vol / (vol20 + 1e-10)
    if vol_ratio > PULLBACK_VOL_SHRINK:
        return False, 0, {}

    # RSI不在极端高位
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
# 突破MA20但成交量不足且迅速跌回 → 卖出信号
# 不追！这是陷阱
# ============================================================
def detect_false_break(df, index):
    """
    检测假突破行为
    
    逻辑：
    1. 前一两日突破了MA20（从下方）
    2. 突破时成交量不足（<均量80%）
    3. 当前跌回MA20下方
    4. 突破幅度很小（<0.5%）→ 非有效突破
    """
    if index < FALSE_BREAK_LOOKBACK + 1:
        return False, 0, {}

    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')
    close_prev = get_value(df, index - 1, 'close')
    ma20_prev = get_value(df, index - 1, 'MA20')

    # 需要前一日或前两日突破了MA20
    # 检查前一日
    broke_prev = close_prev > ma20_prev * (1 + FALSE_BREAK_BREAK_DIST) and \
                 get_value(df, index - 2, 'close') <= get_value(df, index - 2, 'MA20')

    # 检查前两日
    broke_prev2 = get_value(df, index - 2, 'close') > get_value(df, index - 2, 'MA20') * (1 + FALSE_BREAK_BREAK_DIST) and \
                  get_value(df, index - 3, 'close') <= get_value(df, index - 3, 'MA20')

    if not broke_prev and not broke_prev2:
        return False, 0, {}

    break_day = index - 1 if broke_prev else index - 2

    # 突破日成交量不足
    break_vol = df['volume'].values[break_day]
    break_vol20 = get_value(df, break_day, 'Vol20')
    vol_ratio = break_vol / (break_vol20 + 1e-10)
    if vol_ratio > FALSE_BREAK_VOL_RATIO:
        return False, 0, {}

    # 当前跌回MA20下方
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
# 放量突破MA20 + 连续站稳 + 成交量持续放大 → 买入确认
# ============================================================
def detect_breakout_confirm(df, index, regime):
    """
    检测真突破行为
    
    要求：
    1. 连续站稳MA20上方（至少BREAKOUT_CONFIRM_DAYS天）
    2. 成交量较前期放大30%以上
    3. 突破了前期阻力（从下方穿越）
    4. 突破时涨幅>1%（说明是真金白银推动）
    """
    if index < BREAKOUT_CONFIRM_DAYS + 2:
        return False, 0, {}

    close_arr = df['close'].values
    ma20_arr = df['MA20'].values

    # 连续站稳MA20
    for d in range(BREAKOUT_CONFIRM_DAYS):
        if index - d < 0 or close_arr[index - d] <= ma20_arr[index - d]:
            return False, 0, {}

    # 突破前在MA20下方
    if close_arr[index - BREAKOUT_CONFIRM_DAYS - 1] > ma20_arr[index - BREAKOUT_CONFIRM_DAYS - 1]:
        return False, 0, {}

    # 成交量放大（突破期间的日均量 vs 前20日均量）
    break_vols = df['volume'].values[index - BREAKOUT_CONFIRM_DAYS + 1:index + 1]
    avg_break_vol = np.mean(break_vols)
    vol20 = get_value(df, index, 'Vol20')
    vol_ratio = avg_break_vol / (vol20 + 1e-10)
    if vol_ratio < BREAKOUT_VOL_INCREASE:
        return False, 0, {}

    # 突破日涨幅>1%
    break_return = close_arr[index - BREAKOUT_CONFIRM_DAYS + 1] / close_arr[index - BREAKOUT_CONFIRM_DAYS] - 1
    if break_return < BREAKOUT_PRICE_RISE:
        return False, 0, {}

    # RSI不能已经过热
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
# 不是简单跌破MA20，而是多指标综合判断趋势正在瓦解
# MA20下降 + MA5<MA10 + ADX下降 + ATR扩大 → 趋势衰退
# ============================================================
def detect_trend_failure(df, index, regime):
    """
    检测趋势衰退行为
    
    综合条件：
    1. MA20开始下降（斜率转负）
    2. MA5跌破MA10（短线死叉）
    3. ADX在下降（趋势强度减弱）
    4. ATR扩大（波动加大，通常不利）
    """
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

    # 条件累积
    failure_signals = 0
    evidence = {}

    # C1: MA20开始下降
    if ma20_slope < TREND_FAIL_MA_SLOPE_NEG:
        failure_signals += 1
        evidence['ma20_declining'] = float(ma20_slope)

    # C2: MA5 < MA10
    if ma5 < ma10:
        failure_signals += 1
        evidence['ma5_below_ma10'] = True

    # C3: ADX下降至少3个点
    adx_change = adx_5d_ago - adx
    if adx_change >= TREND_FAIL_ADX_DECLINE:
        failure_signals += 1
        evidence['adx_decline'] = float(adx_change)

    # C4: ATR扩大
    if atr_10d_ago > 0:
        atr_expansion = atr / atr_10d_ago
        if atr_expansion >= TREND_FAIL_ATR_EXPAND:
            failure_signals += 1
            evidence['atr_expansion'] = float(atr_expansion)

    # C5: 价格在MA20下方
    if close < ma20:
        failure_signals += 1
        evidence['below_ma20'] = True

    # 至少满足4个条件才触发（严格过滤假阳性）
    if failure_signals < 4:
        return False, 0, evidence

    evidence['failure_count'] = failure_signals
    return True, TREND_FAIL_SCORE, evidence


# ============================================================
# Behavior 7: Panic Sell（恐慌杀跌）
# 连续暴跌 + ATR扩大 + 成交量爆炸 + Z<-2 → 不要卖！反而买入！
# 这是恐慌性抛售，逆向买入的机会
# ============================================================
def detect_panic_sell(df, index):
    """
    检测恐慌杀跌行为
    
    逻辑：
    1. 近3日累计跌幅>6%
    2. ATR显著扩大
    3. 成交量爆炸式放大
    4. Z-score < -2（极端超卖）
    5. RSI进入超卖区域
    """
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

    # C1: 连续暴跌
    cum_drop = close / close_3d - 1
    if cum_drop > PANIC_SELL_DROP_THRESHOLD:
        return False, 0, {}

    # C2: ATR扩大
    if atr_10d > 0:
        atr_expansion = atr / atr_10d
        if atr_expansion < PANIC_SELL_ATR_EXPAND:
            return False, 0, {}
    else:
        return False, 0, {}

    # C3: 成交量爆炸
    vol_ratio = vol / (vol20 + 1e-10)
    if vol_ratio < PANIC_SELL_VOL_EXPLODE:
        return False, 0, {}

    # C4: Z-score极端
    if z20 > PANIC_SELL_Z_THRESHOLD:
        return False, 0, {}

    # C5: RSI超卖
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
# 辅助评分因子检测
# ============================================================

def detect_aux_factors(df, index, regime):
    """
    检测辅助评分因子（不属于7大行为，但提供额外评分）
    返回 (buy_aux_score, sell_aux_score, evidence)
    """
    buy_score = 0
    sell_score = 0
    evidence = {}

    close = get_value(df, index, 'close')
    ma20 = get_value(df, index, 'MA20')
    ma5 = get_value(df, index, 'MA5')
    ma10 = get_value(df, index, 'MA10')
    rsi = get_value(df, index, 'RSI14')
    rsi_prev = get_value(df, index - 1, 'RSI14')
    adx = get_value(df, index, 'ADX14')
    vol = df['volume'].values[index]
    vol20 = get_value(df, index, 'Vol20')
    z20 = get_value(df, index, 'Z20')
    macd_hist = get_value(df, index, 'MACD_hist')
    macd_hist_prev = get_value(df, index - 1, 'MACD_hist')

    # ---- 买入辅助因子 ----

    # MA20开始上拐
    ma20_slope = get_value(df, index, 'MA20_slope')
    ma20_slope_prev = get_value(df, index - 1, 'MA20_slope')
    if ma20_slope > REGIME_MA_SLOPE_MIN and ma20_slope_prev <= 0:
        buy_score += AUX_MA20_TURNING_UP
        evidence['ma20_turning_up'] = True

    # ADX支持上涨趋势（ADX>25且+DI>-DI）
    plus_di = get_value(df, index, 'plus_di')
    minus_di = get_value(df, index, 'minus_di')
    if adx > 25 and plus_di > minus_di:
        buy_score += AUX_ADX_BULL_SUPPORT
        evidence['adx_bull'] = True

    # 成交量温和放大支持
    if 1.1 < vol / vol20 < 1.8 and close > get_value(df, index - 1, 'close'):
        buy_score += AUX_VOLUME_SUPPORT
        evidence['vol_support'] = True

    # RSI超卖反弹：RSI从<30开始回升
    if rsi_prev < 32 and rsi > rsi_prev and rsi < 50:
        buy_score += AUX_RSI_OVERSOLD_REBOUND
        evidence['rsi_oversold_rebound'] = True

    # 底背离：价格新低但RSI未创新低
    close_10d_low = df['close'].rolling(10).min().values[index]
    rsi_10d_low = df['RSI14'].rolling(10).min().values[index]
    if close <= close_10d_low * 1.01 and rsi > rsi_10d_low * 1.05:
        buy_score += AUX_DIVERGENCE_BULL
        evidence['bull_divergence'] = True

    # ---- 卖出辅助因子 ----

    # RSI顶背离：价格新高但RSI未创新高
    close_10d_high = df['close'].rolling(10).max().values[index]
    rsi_10d_high = df['RSI14'].rolling(10).max().values[index]
    if close >= close_10d_high * 0.99 and rsi < rsi_10d_high * 0.95:
        sell_score += AUX_RSI_BEAR_DIVERGENCE
        evidence['bear_divergence'] = True

    # 成交量持续衰退
    vol_3d_avg = np.mean(df['volume'].values[max(0, index - 3):index + 1])
    vol_10d_avg = np.mean(df['volume'].values[max(0, index - 10):index + 1])
    if vol_10d_avg > 0 and vol_3d_avg / vol_10d_avg < 0.7:
        sell_score += AUX_VOLUME_DECLINE
        evidence['vol_decline'] = True

    # 短线死叉确认
    if ma5 < ma10 and get_value(df, index - 1, 'MA5') > get_value(df, index - 1, 'MA10'):
        sell_score += AUX_MA5_MA10_DEAD_CROSS
        evidence['dead_cross'] = True

    # Z-score极端超买
    if z20 > 2.0:
        sell_score += AUX_ZSCORE_EXTREME
        evidence['z_extreme'] = float(z20)

    return buy_score, sell_score, evidence


# ============================================================
# 行为检测汇总（主入口）
# ============================================================

def detect_all_behaviors(df, index, regime):
    """
    检测所有行为，返回结构化结果
    
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

    # Behavior 1: Double Bottom → 买入
    detected, score, evidence = detect_double_bottom(df, index)
    if detected:
        buy_behaviors.append(('DoubleBottom', score, evidence))

    # Behavior 2: Momentum Exhaustion → 卖出
    detected, score, evidence = detect_momentum_exhaustion(df, index)
    if detected:
        sell_behaviors.append(('MomentumExhaustion', score, evidence))

    # Behavior 3: Trend Pullback → 买入
    detected, score, evidence = detect_trend_pullback(df, index, regime)
    if detected:
        buy_behaviors.append(('TrendPullback', score, evidence))

    # Behavior 4: False Break → 卖出
    detected, score, evidence = detect_false_break(df, index)
    if detected:
        sell_behaviors.append(('FalseBreak', score, evidence))

    # Behavior 5: Breakout Confirmation → 买入
    detected, score, evidence = detect_breakout_confirm(df, index, regime)
    if detected:
        buy_behaviors.append(('BreakoutConfirm', score, evidence))

    # Behavior 6: Trend Failure → 卖出
    detected, score, evidence = detect_trend_failure(df, index, regime)
    if detected:
        sell_behaviors.append(('TrendFailure', score, evidence))

    # Behavior 7: Panic Sell → 买入（逆向）
    detected, score, evidence = detect_panic_sell(df, index)
    if detected:
        buy_behaviors.append(('PanicSell', score, evidence))

    # 辅助因子
    aux_buy, aux_sell, aux_evidence = detect_aux_factors(df, index, regime)

    return {
        'buy_behaviors': buy_behaviors,
        'sell_behaviors': sell_behaviors,
        'aux_buy_score': aux_buy,
        'aux_sell_score': aux_sell,
        'aux_evidence': aux_evidence,
        'regime': regime,
    }
