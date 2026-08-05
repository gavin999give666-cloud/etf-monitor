"""
V4.0 评分引擎（Scoring Engine）  —— 核心模块

第三层：将行为检测结果转化为具体的 BuyScore 和 SellScore

V4.0 重构：评分映射为仓位变化量（delta），而非绝对目标
- BuyScore → 仓位增加量
- SellScore → 仓位减少量
- 两者共同作用决定最终目标仓位
"""
from config import REGIME_WEIGHTS


def calculate_score(behavior_result):
    """
    计算买卖评分
    
    逻辑：
    1. 汇总所有买入行为的分数 → raw_buy_score
    2. 汇总所有卖出行为的分数 → raw_sell_score
    3. 加上辅助因子评分
    4. 应用市场状态权重系数
    5. 返回最终 (buy_score, sell_score, score_detail)
    """
    regime = behavior_result['regime']
    buy_behaviors = behavior_result['buy_behaviors']
    sell_behaviors = behavior_result['sell_behaviors']
    aux_buy = behavior_result['aux_buy_score']
    aux_sell = behavior_result['aux_sell_score']

    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS['Unknown'])
    buy_mult = weights['buy_mult']
    sell_mult = weights['sell_div']

    raw_buy = sum(b[1] for b in buy_behaviors)
    raw_sell = sum(s[1] for s in sell_behaviors)

    raw_buy += aux_buy
    raw_sell += aux_sell

    # 行为共存时的处理：弱势方折半
    has_buy_behavior = len(buy_behaviors) > 0
    has_sell_behavior = len(sell_behaviors) > 0
    if has_buy_behavior and has_sell_behavior:
        if raw_buy >= raw_sell:
            raw_sell *= 0.5
        else:
            raw_buy *= 0.5

    buy_score = raw_buy * buy_mult
    sell_score = raw_sell * sell_mult

    detail = {
        'regime': regime,
        'raw_buy': raw_buy,
        'raw_sell': raw_sell,
        'buy_mult': buy_mult,
        'sell_mult': sell_mult,
        'buy_behaviors': [{'name': b[0], 'score': b[1]} for b in buy_behaviors],
        'sell_behaviors': [{'name': s[0], 'score': s[1]} for s in sell_behaviors],
        'aux_buy': aux_buy,
        'aux_sell': aux_sell,
    }

    return buy_score, sell_score, detail


def score_to_delta(score, delta_map):
    """将分数映射为仓位变化量"""
    for threshold, delta in delta_map:
        if score >= threshold:
            return delta
    return 0.0


def score_to_target_position(buy_score, sell_score, current_position):
    """
    将评分映射为目标仓位（差值法）

    BuyScore → 仓位增量
    SellScore → 仓位减量
    Net = current + buy_delta - sell_delta
    """
    from config import BUY_DELTA_MAP, SELL_DELTA_MAP

    buy_delta = score_to_delta(buy_score, BUY_DELTA_MAP)
    sell_delta = score_to_delta(sell_score, SELL_DELTA_MAP)

    # 两者都有信号时，取主导方向（避免同时加减仓）
    target = current_position
    if buy_delta > 0 and sell_delta > 0:
        # 净效应：谁大听谁的
        if buy_delta >= sell_delta:
            target = current_position + buy_delta * 0.7
        else:
            target = current_position - sell_delta * 0.7
    elif buy_delta > 0:
        target = current_position + buy_delta
    elif sell_delta > 0:
        target = current_position - sell_delta
    else:
        target = current_position

    # 安全边界（允许买入时的自然漂移，最高80%）
    target = max(0.03, min(0.80, target))

    return target


def score_to_action(buy_score, sell_score, current_position):
    """
    将评分转化为明确的交易动作描述
    """
    target_pos = score_to_target_position(buy_score, sell_score, current_position)

    if target_pos > current_position + 0.02:
        action = 'BUY'
        delta = target_pos - current_position
        description = f"买入 {delta * 100:.0f}% -> 目标仓位 {target_pos * 100:.0f}%"
    elif target_pos < current_position - 0.02:
        action = 'SELL'
        delta = current_position - target_pos
        description = f"卖出 {delta * 100:.0f}% -> 目标仓位 {target_pos * 100:.0f}%"
    else:
        action = 'HOLD'
        description = f"维持仓位 {current_position * 100:.0f}%"

    return action, target_pos, description
