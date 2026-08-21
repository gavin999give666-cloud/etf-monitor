"""
V5.0 Reward / Risk Evaluation（赔率评估模块）
==============================================

新增模块：评估交易的赔率（Reward）和风险（Risk）。

Reward 因子：
- 距离60日高点（越远 → 上涨空间越大）
- 距离60日低点（越近 → 安全边际越高）
- MA20偏离度（负偏离 → 有回归动力）
- ATR位置（价格在波动区间的低位 → 上涨空间大）
- 波动率分位数（低波动 → 可能即将突破）
- 趋势强度（ADX高 → 趋势可延续）

Risk 因子：
- 回撤风险（距近期高点越远 → 风险越大）
- 波动率风险（当前波动率高于历史 → 高风险）
- 趋势反转风险（ADX下降 + MA走平 → 趋势可能结束）
- 成交量异常风险（异常放量 → 主力出货）
- 均线偏离风险（偏离MA20越远 → 均值回归风险）

最终生成 RewardScore 和 RiskScore 用于评分引擎。
"""
import numpy as np
from config import *
from indicators import get_value


def evaluate_reward(df, index, regime, psychology_state):
    """
    评估上涨空间（Reward Score）

    返回: reward_score (0-50), reward_detail dict
    """
    if index < 60:
        return 15, {'error': '数据不足'}

    scores = {}
    detail = {}

    # 1. 距离60日高点（越远，空间越大）
    dist_high = get_value(df, index, 'dist_from_60d_high', 0)
    if dist_high < -0.15:
        scores['dist_from_60d_high'] = 10  # 大跌后空间很大
    elif dist_high < -0.10:
        scores['dist_from_60d_high'] = 8
    elif dist_high < -0.05:
        scores['dist_from_60d_high'] = 6
    elif dist_high < -0.02:
        scores['dist_from_60d_high'] = 3
    elif dist_high < 0:
        scores['dist_from_60d_high'] = 1
    else:
        scores['dist_from_60d_high'] = 0  # 创历史新高，无上方空间参考
    detail['dist_from_60d_high'] = float(dist_high)

    # 2. 距离60日低点（越近，安全边际越高）
    dist_low = get_value(df, index, 'dist_from_60d_low', 0)
    if dist_low < 0.03:
        scores['dist_from_60d_low'] = 8   # 接近历史低点，安全边际高
    elif dist_low < 0.06:
        scores['dist_from_60d_low'] = 6
    elif dist_low < 0.10:
        scores['dist_from_60d_low'] = 4
    elif dist_low < 0.15:
        scores['dist_from_60d_low'] = 2
    else:
        scores['dist_from_60d_low'] = 0
    detail['dist_from_60d_low'] = float(dist_low)

    # 3. MA20偏离度（负偏离有回归动力）
    ma20_dev = get_value(df, index, 'ma20_deviation', 0)
    if ma20_dev < -0.05:
        scores['ma20_deviation'] = 10  # 明显低于MA20，回归动力强
    elif ma20_dev < -0.03:
        scores['ma20_deviation'] = 8
    elif ma20_dev < -0.01:
        scores['ma20_deviation'] = 6
    elif ma20_dev < 0.01:
        scores['ma20_deviation'] = 3  # 持平
    elif ma20_dev < 0.03:
        scores['ma20_deviation'] = 1
    else:
        scores['ma20_deviation'] = 0  # 高于MA20太多
    detail['ma20_deviation'] = float(ma20_dev)

    # 4. ATR位置（在波动区间低位 → 上涨空间大）
    atr_pos = get_value(df, index, 'atr_position', 0.5)
    if atr_pos < 0.2:
        scores['atr_position'] = 8   # 低位，反弹空间大
    elif atr_pos < 0.35:
        scores['atr_position'] = 6
    elif atr_pos < 0.5:
        scores['atr_position'] = 4
    elif atr_pos < 0.65:
        scores['atr_position'] = 2
    else:
        scores['atr_position'] = 0  # 高位，无上涨空间
    detail['atr_position'] = float(atr_pos)

    # 5. 波动率分位数（低波动 → 可能即将有行情）
    vol_perc = get_value(df, index, 'volatility_percentile', 0.5)
    if vol_perc < 0.2:
        scores['volatility_percentile'] = 6  # 低波动，可能突破
    elif vol_perc < 0.4:
        scores['volatility_percentile'] = 4
    elif vol_perc < 0.6:
        scores['volatility_percentile'] = 2
    else:
        scores['volatility_percentile'] = 0  # 高波动，风险大
    detail['volatility_percentile'] = float(vol_perc)

    # 6. 趋势强度（ADX越高，趋势可延续）
    adx = get_value(df, index, 'ADX14', 20)
    if adx > 35:
        scores['trend_strength'] = 8
    elif adx > 28:
        scores['trend_strength'] = 6
    elif adx > 22:
        scores['trend_strength'] = 4
    elif adx > 18:
        scores['trend_strength'] = 2
    else:
        scores['trend_strength'] = 0
    detail['trend_strength'] = float(adx)

    # 加权求和
    total_reward = 0
    for factor, score in scores.items():
        weight = REWARD_WEIGHTS.get(factor, 0.15)
        total_reward += score * weight

    # 归一化到 0-50
    reward_score = min(REWARD_SCORE_MAX, total_reward * 5.0)

    # 情绪修正
    if psychology_state == 'Panic':
        reward_score *= 1.3   # 恐慌中赔率更高（逆向买入）
    elif psychology_state == 'Fear':
        reward_score *= 1.15
    elif psychology_state == 'Euphoria':
        reward_score *= 0.7   # 狂热中赔率很低

    reward_score = min(REWARD_SCORE_MAX, max(0, reward_score))

    detail['factor_scores'] = scores
    detail['psychology_modifier'] = psychology_state

    return round(reward_score, 1), detail


def evaluate_risk(df, index, regime, psychology_state):
    """
    评估下行风险（Risk Score）

    返回: risk_score (0-50), risk_detail dict
    """
    if index < 60:
        return 25, {'error': '数据不足'}

    scores = {}
    detail = {}

    # 1. 回撤风险（距20日高点距离）
    dd_risk = get_value(df, index, 'drawdown_risk', 0)
    if dd_risk < -0.10:
        scores['drawdown_risk'] = 10  # 大幅回撤中，风险高
    elif dd_risk < -0.05:
        scores['drawdown_risk'] = 8
    elif dd_risk < -0.03:
        scores['drawdown_risk'] = 5
    elif dd_risk < -0.01:
        scores['drawdown_risk'] = 2
    else:
        scores['drawdown_risk'] = 0   # 接近高点，风险低
    detail['drawdown_risk'] = float(dd_risk)

    # 2. 波动率风险
    vol_risk = get_value(df, index, 'volatility_risk', 1.0)
    if vol_risk > 1.5:
        scores['volatility_risk'] = 10  # 波动率异常放大
    elif vol_risk > 1.3:
        scores['volatility_risk'] = 7
    elif vol_risk > 1.1:
        scores['volatility_risk'] = 4
    elif vol_risk > 0.9:
        scores['volatility_risk'] = 2
    else:
        scores['volatility_risk'] = 0   # 低波动，低风险
    detail['volatility_risk'] = float(vol_risk)

    # 3. 趋势反转风险
    adx_declining = get_value(df, index, 'adx_declining', False)
    ma20_flat = get_value(df, index, 'ma20_flattening', False)
    reversal_score = 0
    if adx_declining:
        reversal_score += 5
    if ma20_flat:
        reversal_score += 5
    # RSI 高位转向
    rsi = get_value(df, index, 'RSI14', 50)
    rsi_prev = get_value(df, index - 1, 'RSI14', 50)
    if rsi > 65 and rsi < rsi_prev:
        reversal_score += 3
    scores['trend_reversal_risk'] = min(10, reversal_score)
    detail['trend_reversal_risk'] = reversal_score

    # 4. 成交量异常风险
    vol_ratio = get_value(df, index, 'vol_ratio_to_mean', 1.0)
    close = get_value(df, index, 'close', 0)
    close_prev = get_value(df, index - 1, 'close', 0)
    if vol_ratio > 2.0 and close < close_prev:
        scores['volume_risk'] = 10   # 放量下跌
    elif vol_ratio > 2.0:
        scores['volume_risk'] = 6    # 放量上涨（警惕出货）
    elif vol_ratio > 1.5:
        scores['volume_risk'] = 3
    elif vol_ratio > 1.2:
        scores['volume_risk'] = 1
    else:
        scores['volume_risk'] = 0
    detail['volume_risk'] = float(vol_ratio)

    # 5. 均线偏离风险
    ma_dev = abs(get_value(df, index, 'ma20_deviation', 0))
    if ma_dev > 0.08:
        scores['ma_deviation_risk'] = 10
    elif ma_dev > 0.05:
        scores['ma_deviation_risk'] = 7
    elif ma_dev > 0.03:
        scores['ma_deviation_risk'] = 4
    elif ma_dev > 0.01:
        scores['ma_deviation_risk'] = 1
    else:
        scores['ma_deviation_risk'] = 0
    detail['ma_deviation_risk'] = float(ma_dev)

    # 加权求和
    total_risk = 0
    for factor, score in scores.items():
        weight = RISK_WEIGHTS.get(factor, 0.15)
        total_risk += score * weight

    # 归一化到 0-50
    risk_score = min(RISK_SCORE_MAX, total_risk * 5.0)

    # 市场状态修正
    if regime == 'Bull':
        risk_score *= 0.8    # 牛市中实际风险较低
    elif regime == 'Bear':
        risk_score *= 1.2    # 熊市中实际风险更高

    risk_score = min(RISK_SCORE_MAX, max(0, risk_score))

    detail['factor_scores'] = scores
    detail['regime_modifier'] = regime

    return round(risk_score, 1), detail


def evaluate_reward_risk(df, index, regime, psychology_state):
    """
    综合评估 Reward/Risk

    返回: (reward_score, risk_score, reward_detail, risk_detail)
    """
    reward_score, reward_detail = evaluate_reward(df, index, regime, psychology_state)
    risk_score, risk_detail = evaluate_risk(df, index, regime, psychology_state)
    return reward_score, risk_score, reward_detail, risk_detail
