"""
V5.0 评分引擎（Scoring Engine） —— 核心重构
=============================================

V5.0 新评分公式：

  Final Buy Score = BehaviorScore * W_behavior
                  + Confidence  * W_confidence
                  + Reward      * W_reward
                  - Risk        * W_risk

与 V4.0 的关键区别：
1. 不再直接从行为检测中计算评分
2. 必须经过 Event Engine 的生命周期确认
3. 只有 Confirmed 的事件才参与评分
4. 新增 Reward/Risk 维度
5. 不再使用辅助因子分数（被 Reward/Risk 层替代）
6. 新增 Acceleration 维度影响 Confidence
"""
from config import *


def calculate_final_score(event_engine, reward_score, risk_score, psychology_state, regime):
    """
    V5.0 最终评分计算

    Args:
        event_engine: EventEngine 实例
        reward_score: Reward/Risk 评估的 RewardScore
        risk_score: Reward/Risk 评估的 RiskScore
        psychology_state: 当前市场情绪状态
        regime: 市场状态

    Returns:
        buy_score: 最终买入评分
        sell_score: 最终卖出评分
        score_breakdown: 评分细分
    """
    # 获取已确认的事件
    confirmed_buy_events = event_engine.get_confirmed_buy_events()
    confirmed_sell_events = event_engine.get_confirmed_sell_events()

    # ============================================================
    # 买入评分计算
    # ============================================================
    buy_behavior_score = 0
    buy_confidence = 0
    buy_events_detail = []

    if confirmed_buy_events:
        for event in confirmed_buy_events:
            buy_behavior_score += event.strength
            buy_confidence = max(buy_confidence, event.confidence)  # 取最高置信度
            buy_events_detail.append({
                'name': event.behavior_name,
                'strength': event.strength,
                'confidence': event.confidence,
                'psych_change': event.psych_change,
            })

    # 归一化 BehaviorScore (0-100)
    buy_behavior_norm = min(100, buy_behavior_score * 1.2)

    # 加权
    raw_buy = (
        buy_behavior_norm * SCORE_BEHAVIOR_WEIGHT +
        buy_confidence * SCORE_CONFIDENCE_WEIGHT +
        reward_score * (SCORE_REWARD_WEIGHT * 2)  # RewardScore 0-50, 乘以2适配权重
    )

    # 市场状态调整
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS['Unknown'])
    buy_score = raw_buy * weights['buy_mult']

    # ============================================================
    # 卖出评分计算
    # ============================================================
    sell_behavior_score = 0
    sell_confidence = 0
    sell_events_detail = []

    if confirmed_sell_events:
        for event in confirmed_sell_events:
            sell_behavior_score += event.strength
            sell_confidence = max(sell_confidence, event.confidence)
            sell_events_detail.append({
                'name': event.behavior_name,
                'strength': event.strength,
                'confidence': event.confidence,
                'psych_change': event.psych_change,
            })

    sell_behavior_norm = min(100, sell_behavior_score * 1.2)

    # 卖出评分 = 行为分 + 置信度 + 风险分
    raw_sell = (
        sell_behavior_norm * SCORE_BEHAVIOR_WEIGHT +
        sell_confidence * SCORE_CONFIDENCE_WEIGHT +
        risk_score * (SCORE_RISK_WEIGHT * 5)  # RiskScore 0-50
    )

    sell_score = raw_sell * weights['sell_div']

    # ============================================================
    # 情绪修正
    # ============================================================
    if psychology_state == 'Panic':
        buy_score *= 1.10   # 恐慌中勇敢买入小幅加分
    elif psychology_state == 'Euphoria':
        sell_score *= 1.15  # 狂热中卖出加分
        buy_score *= 0.80   # 狂热中买入打折
    elif psychology_state == 'Exhaustion':
        sell_score *= 1.25  # 衰竭中卖出强烈加分

    # ============================================================
    # 构建评分细分
    # ============================================================
    score_breakdown = {
        'buy': {
            'behavior_score': round(buy_behavior_norm, 1),
            'confidence': round(buy_confidence, 1),
            'reward_score': round(reward_score, 1),
            'raw_score': round(raw_buy, 1),
            'final_score': round(buy_score, 1),
            'events': buy_events_detail,
            'regime_mult': weights['buy_mult'],
        },
        'sell': {
            'behavior_score': round(sell_behavior_norm, 1),
            'confidence': round(sell_confidence, 1),
            'risk_score': round(risk_score, 1),
            'raw_score': round(raw_sell, 1),
            'final_score': round(sell_score, 1),
            'events': sell_events_detail,
            'regime_mult': weights['sell_div'],
        },
        'psychology': psychology_state,
        'regime': regime,
    }

    return round(buy_score, 1), round(sell_score, 1), score_breakdown


def get_center(regime):
    """L1 战略层：Regime 主态 → 仓位中枢 center"""
    import config as _cfg
    return {
        'Bull': _cfg.CENTER_BULL,
        'Range': _cfg.CENTER_RANGE,
        'Bear': _cfg.CENTER_BEAR,
    }.get(regime, _cfg.CENTER_UNKNOWN)


def get_offset(buy_score, sell_score):
    """
    L2 战术层：评分 → 偏移量 offset（有界，不再造成单边失衡）

    净评分在 SCORE_HOLD_ZONE 内 → offset=0（持有区，维持中枢仓位）；
    买入占优 → 正偏移；卖出占优 → 负偏移。
    """
    import config as _cfg

    buy_offset = 0.0
    for threshold, off in _cfg.BUY_OFFSET_THRESHOLDS:
        if buy_score >= threshold:
            buy_offset = off
            break

    sell_offset = 0.0
    for threshold, off in _cfg.SELL_OFFSET_THRESHOLDS:
        if sell_score >= threshold:
            sell_offset = off
            break

    net_score = buy_score - sell_score
    if net_score > _cfg.SCORE_HOLD_ZONE:
        return buy_offset
    elif net_score < -_cfg.SCORE_HOLD_ZONE:
        return sell_offset
    return 0.0


def score_to_target_position(buy_score, sell_score, current_position,
                             regime='Unknown', center=None,
                             offset_boost=0.0, offset_penalty=0.0):
    """
    V7.0 P6 三层架构：target = clamp(center + offset, POSITION_FLOOR, MAX_POSITION)

    - L1 战略层：center 由 Regime 主态决定（可外部传入，P6.5 由 strategy 合成）
    - L2 战术层：offset 由 buy/sell 评分决定（get_offset）
    - 相位通道：offset_boost（BottomFishing 左侧建仓）/ offset_penalty（Overheat 越中枢减仓）

    STRATEGY_MODE='V6' 时走完整旧决策路径（_v6_target_position），行为与 V6.2.3 完全一致。
    """
    import config as _cfg

    if _cfg.STRATEGY_MODE == 'V6':
        return _v6_target_position(buy_score, sell_score, current_position)

    if center is None:
        center = get_center(regime)
    offset = get_offset(buy_score, sell_score) + offset_boost + offset_penalty
    target = center + offset
    return max(_cfg.POSITION_FLOOR, min(_cfg.MAX_POSITION, target))


def _v6_target_position(buy_score, sell_score, current_position):
    """
    V6.2.3 旧版映射（STRATEGY_MODE='V6' 时使用，行为与旧版完全一致）。
    保留：最低仓位 70%、按比例减仓、HOLD 区间作用于绝对仓位。
    """
    import config as _cfg

    buy_target = 0
    for threshold, position in _cfg.BUY_SCORE_THRESHOLDS:
        if buy_score >= threshold:
            buy_target = position
            break

    sell_reduction = 0
    for threshold, reduction in _cfg.SELL_SCORE_THRESHOLDS:
        if sell_score >= threshold:
            sell_reduction = reduction
            break

    net_score = buy_score - sell_score

    if net_score > _cfg.SCORE_HOLD_ZONE:
        target = buy_target
    elif net_score < -_cfg.SCORE_HOLD_ZONE:
        target = max(0.70, current_position * (1 - sell_reduction))
    else:
        target = current_position

    target = max(0.70, min(_cfg.MAX_POSITION, target))
    return target


def get_score_to_action(buy_score, sell_score, current_position):
    """评分 → 交易动作"""
    target = score_to_target_position(buy_score, sell_score, current_position)

    if target > current_position + 0.02:
        action = 'BUY'
        delta = target - current_position
        description = f"买入 +{delta * 100:.0f}% → 目标仓位 {target * 100:.0f}%"
    elif target < current_position - 0.02:
        action = 'SELL'
        delta = current_position - target
        description = f"卖出 -{delta * 100:.0f}% → 目标仓位 {target * 100:.0f}%"
    else:
        action = 'HOLD'
        description = f"维持仓位 {current_position * 100:.0f}%"

    return action, target, description
