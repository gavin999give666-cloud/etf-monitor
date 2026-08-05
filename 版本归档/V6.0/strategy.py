"""
V6.0 策略流水线（Strategy Pipeline）
=====================================

完整的 V6.0 架构：

  数据预处理（indicators.py）
        ↓
  Market Regime（regime_detector.py）
        ↓
  Behavior Detection（behavior_detector.py）
        ↓
  EmotionBuilder（emotion_builder.py）—— V5.5 多源数据融合情绪引擎
        ↓
  Event Engine + Lifecycle（event_engine.py）—— V5.1 Time Decay
        ↓
  Evidence Engine（evidence_engine.py）—— 多源置信度融合
        ↓
  Reward / Risk Evaluation（reward_risk.py）
        ↓
  Scoring Engine（scoring_engine.py）
        ↓
  Position Manager（position_manager.py）
"""
import pandas as pd
import numpy as np
from datetime import datetime, time

from indicators import calculate_indicators
from regime_detector import detect_market_regime
from behavior_detector import detect_all_behaviors
from emotion_builder import EmotionBuilder
from event_engine import EventEngine
from evidence_engine import EvidenceEngine
from reward_risk import evaluate_reward_risk
from scoring_engine import calculate_final_score, score_to_target_position
from position_manager import PositionManager
from config import *

# 主要节假日列表（2026年）
MAJOR_HOLIDAYS_2026 = [
    '2026-01-01',
    '2026-02-01', '2026-02-02', '2026-02-03', '2026-02-04', '2026-02-05',
    '2026-04-04', '2026-04-05',
    '2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05',
    '2026-06-25', '2026-06-26',
    '2026-09-27',
    '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07',
]


def is_trading_day(date):
    """判断是否为交易日"""
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y-%m-%d').date()
    elif isinstance(date, datetime):
        date = date.date()
    if date.weekday() >= 5:
        return False
    if date.strftime('%Y-%m-%d') in MAJOR_HOLIDAYS_2026:
        return False
    return True


class V6Strategy:
    """
    V6.0 策略流水线

    新增：
    - EmotionBuilder（替代 CrowdPsychology）
    - Evidence Engine（多源置信度融合）
    - Replay Learning（行为成功率统计）
    - Time Decay（观察期衰减）
    """

    def __init__(self, use_ml=False, emotion_method='weighted'):
        """
        Args:
            use_ml: 是否启用ML证据源
            emotion_method: 情绪融合方法
        """
        self.event_engine = EventEngine()
        self.emotion_builder = EmotionBuilder(method=emotion_method)
        self.evidence_engine = EvidenceEngine(
            weights=EVIDENCE_WEIGHTS.copy(),
            use_ml=use_ml,
            use_replay=EVIDENCE_ENABLE_REPLAY,
        )
        self.signals = []
        self.replay_records = []

        # V6: 价格+情绪双确认记录
        self.emotion_confirmation_log = []

    def fit_emotion_builder(self, df):
        """使用历史数据拟合EmotionBuilder（PCA/ICA方法）"""
        if self.emotion_builder.method in ('pca', 'ica'):
            self.emotion_builder.fit(df)

    def run(self, df, start_date=None):
        """
        运行 V6.0 完整策略流水线

        Args:
            df: 包含 OHLCV 的 DataFrame（已计算指标）
            start_date: 回测起始日期

        Returns:
            signals: list of dict
        """
        # Step 0: 确保指标已计算
        df = calculate_indicators(df)

        # V6: 拟合EmotionBuilder（如果需要）
        self.fit_emotion_builder(df)

        self.signals = []
        self.replay_records = []

        for i in range(len(df)):
            current_date = df.index[i]
            date_str = current_date.strftime('%Y-%m-%d') if hasattr(current_date, 'strftime') else str(current_date)

            # 非交易日跳过
            if not is_trading_day(current_date):
                self.signals.append({
                    'date': current_date,
                    'regime': 'NonTrading',
                    'psychology': self.emotion_builder.get_state(),
                    'buy_score': 0,
                    'sell_score': 0,
                    'target_position': None,
                    'action': 'HOLD',
                    'description': '非交易日',
                })
                continue

            # 数据不足
            if i < 60:
                self.signals.append({
                    'date': current_date,
                    'regime': 'Insufficient',
                    'psychology': 'Unknown',
                    'buy_score': 0,
                    'sell_score': 0,
                    'target_position': None,
                    'action': 'HOLD',
                    'description': '数据不足',
                })
                continue

            # ---- Layer 1: Market Regime ----
            regime = detect_market_regime(df, i)

            # ---- Layer 2: Behavior Detection ----
            behavior_result = detect_all_behaviors(df, i, regime)

            # ---- Layer 3: EmotionBuilder Update (替代 CrowdPsychology) ----
            psych_state, psych_changed, psych_desc = self.emotion_builder.update(df, i, current_date)
            emotion_score = self.emotion_builder.get_emotion_score()
            emotion_improving, emotion_magnitude = self.emotion_builder.get_emotion_improvement(
                EMOTION_IMPROVEMENT_WINDOW
            )

            # V6: 价格+情绪双确认记录
            if emotion_improving and behavior_result['buy_behaviors']:
                self.emotion_confirmation_log.append({
                    'date': current_date,
                    'behavior': behavior_result['buy_behaviors'][0][0],
                    'emotion_from': self.emotion_builder.state_history[-EMOTION_IMPROVEMENT_WINDOW][1] if len(self.emotion_builder.state_history) > EMOTION_IMPROVEMENT_WINDOW else 'N/A',
                    'emotion_to': psych_state,
                    'magnitude': round(emotion_magnitude, 2),
                })

            # ---- Layer 4: Event Engine (Lifecycle) with Time Decay ----
            daily_summary = self.event_engine.process_daily(
                current_date, behavior_result, regime, psych_state, df, i
            )

            # ---- Layer 5: Reward / Risk Evaluation ----
            reward_score, risk_score, reward_detail, risk_detail = evaluate_reward_risk(
                df, i, regime, psych_state
            )

            # ---- Layer 6: V6 Evidence Engine ----
            buy_score, sell_score, score_breakdown, evidence_debug = self._compute_v6_score(
                reward_score, risk_score, psych_state, regime,
                emotion_score, emotion_improving, emotion_magnitude,
                current_date
            )

            # ---- Layer 7: Position (handled by backtest engine) ----

            # ---- 构建 Replay 记录 ----
            replay = self._build_replay_record(
                current_date, regime, psych_state, psych_changed, psych_desc,
                daily_summary, buy_score, sell_score, score_breakdown,
                reward_score, risk_score, evidence_debug
            )
            self.replay_records.append(replay)

            # ---- 构建信号 ----
            buy_behaviors = [b[0] for b in behavior_result.get('buy_behaviors', [])]
            sell_behaviors = [s[0] for s in behavior_result.get('sell_behaviors', [])]

            signal = {
                'date': current_date,
                'regime': regime,
                'psychology': psych_state,
                'emotion_score': emotion_score,
                'emotion_improving': emotion_improving,
                'buy_score': buy_score,
                'sell_score': sell_score,
                'reward_score': reward_score,
                'risk_score': risk_score,
                'buy_behaviors': buy_behaviors,
                'sell_behaviors': sell_behaviors,
                'confirmed_buy_events': len(score_breakdown['buy']['events']),
                'confirmed_sell_events': len(score_breakdown['sell']['events']),
                'active_events': daily_summary['active_count'],
                'score_breakdown': score_breakdown,
                'evidence_debug': evidence_debug,
                'replay': replay,
            }
            self.signals.append(signal)

        return self.signals

    def _compute_v6_score(self, reward_score, risk_score, psych_state, regime,
                           emotion_score, emotion_improving, emotion_magnitude,
                           current_date):
        """
        V6.0 评分计算 —— 使用 Evidence Engine 替代纯 Rule Confidence

        Returns:
            buy_score, sell_score, score_breakdown, evidence_debug
        """
        confirmed_buy_events = self.event_engine.get_confirmed_buy_events()
        confirmed_sell_events = self.event_engine.get_confirmed_sell_events()

        # 市场上下文
        market_context = {
            'regime': regime,
            'emotion_state': psych_state,
            'emotion_score': emotion_score,
            'emotion_improving': emotion_improving,
            'emotion_magnitude': emotion_magnitude,
            'reward_score': reward_score,
            'risk_score': risk_score,
        }

        # --- 买入评分 ---
        buy_events_detail = []
        buy_final_conf = 0
        buy_behavior_score = 0
        evidence_debug_buy = []

        for event in confirmed_buy_events:
            buy_behavior_score += event.strength

            # V6: 通过 Evidence Engine 计算最终置信度
            final_conf, ev_breakdown = self.evidence_engine.compute_final_confidence(
                event, market_context, current_date
            )
            buy_final_conf = max(buy_final_conf, final_conf)

            buy_events_detail.append({
                'name': event.behavior_name,
                'strength': event.strength,
                'confidence': round(final_conf, 1),
                'evidence_breakdown': ev_breakdown,
                'psych_change': event.psych_change,
            })
            evidence_debug_buy.append(ev_breakdown)

        buy_behavior_norm = min(100, buy_behavior_score * 1.2)
        weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS['Unknown'])

        raw_buy = (
            buy_behavior_norm * SCORE_BEHAVIOR_WEIGHT +
            buy_final_conf * SCORE_CONFIDENCE_WEIGHT +
            reward_score * (SCORE_REWARD_WEIGHT * 2)
        )
        buy_score = raw_buy * weights['buy_mult']

        # --- 卖出评分 ---
        sell_events_detail = []
        sell_final_conf = 0
        sell_behavior_score = 0
        evidence_debug_sell = []

        for event in confirmed_sell_events:
            sell_behavior_score += event.strength

            final_conf, ev_breakdown = self.evidence_engine.compute_final_confidence(
                event, market_context, current_date
            )
            sell_final_conf = max(sell_final_conf, final_conf)

            sell_events_detail.append({
                'name': event.behavior_name,
                'strength': event.strength,
                'confidence': round(final_conf, 1),
                'evidence_breakdown': ev_breakdown,
                'psych_change': event.psych_change,
            })
            evidence_debug_sell.append(ev_breakdown)

        sell_behavior_norm = min(100, sell_behavior_score * 1.2)
        raw_sell = (
            sell_behavior_norm * SCORE_BEHAVIOR_WEIGHT +
            sell_final_conf * SCORE_CONFIDENCE_WEIGHT +
            risk_score * (SCORE_RISK_WEIGHT * 5)
        )
        sell_score = raw_sell * weights['sell_div']

        # --- 情绪修正 ---
        if psych_state == 'Panic':
            buy_score *= 1.10
        elif psych_state == 'Euphoria':
            sell_score *= 1.15
            buy_score *= 0.80
        elif psych_state == 'Exhaustion':
            sell_score *= 1.25

        # --- 构建分解 ---
        score_breakdown = {
            'buy': {
                'behavior_score': round(buy_behavior_norm, 1),
                'confidence': round(buy_final_conf, 1),
                'reward_score': round(reward_score, 1),
                'raw_score': round(raw_buy, 1),
                'final_score': round(buy_score, 1),
                'events': buy_events_detail,
                'regime_mult': weights['buy_mult'],
            },
            'sell': {
                'behavior_score': round(sell_behavior_norm, 1),
                'confidence': round(sell_final_conf, 1),
                'risk_score': round(risk_score, 1),
                'raw_score': round(raw_sell, 1),
                'final_score': round(sell_score, 1),
                'events': sell_events_detail,
                'regime_mult': weights['sell_div'],
            },
            'psychology': psych_state,
            'regime': regime,
        }

        evidence_debug = {
            'buy': evidence_debug_buy,
            'sell': evidence_debug_sell,
        }

        return round(buy_score, 1), round(sell_score, 1), score_breakdown, evidence_debug

    def _build_replay_record(self, date, regime, psych_state, psych_changed, psych_desc,
                              daily_summary, buy_score, sell_score, score_breakdown,
                              reward_score, risk_score, evidence_debug):
        """构建完整的交易解释记录（V6增强版）"""
        record = {
            'Date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'Regime': regime,
            'Psychology': psych_state,
            'EmotionScore': round(self.emotion_builder.get_emotion_score(), 1),
            'Psychology_Change': psych_desc if psych_changed else '维持不变',
            'BuyScore': round(buy_score, 1),
            'SellScore': round(sell_score, 1),
            'RewardScore': round(reward_score, 1),
            'RiskScore': round(risk_score, 1),
        }

        # 买入事件详情（含Evidence Engine分解）
        buy_events = score_breakdown['buy']['events']
        if buy_events:
            record['Buy_Behavior'] = buy_events[0]['name']
            record['Buy_Confidence'] = buy_events[0]['confidence']
            record['Buy_PsychChange'] = buy_events[0].get('psych_change', {}).get('description', '')

            # V6: Evidence Engine 分解
            ev = buy_events[0].get('evidence_breakdown', {})
            if ev:
                record['Buy_Evidence_PreDecay'] = ev.get('pre_decay', 0)
                record['Buy_Evidence_TimeDecay'] = ev.get('decay_multiplier', 1.0)
                record['Buy_Evidence_Final'] = ev.get('final', 0)
        else:
            record['Buy_Behavior'] = 'None'
            record['Buy_Confidence'] = 0
            record['Buy_PsychChange'] = ''
            record['Buy_Evidence_PreDecay'] = 0
            record['Buy_Evidence_TimeDecay'] = 1.0
            record['Buy_Evidence_Final'] = 0

        # 卖出事件详情
        sell_events = score_breakdown['sell']['events']
        if sell_events:
            record['Sell_Behavior'] = sell_events[0]['name']
            record['Sell_Confidence'] = sell_events[0]['confidence']
            record['Sell_PsychChange'] = sell_events[0].get('psych_change', {}).get('description', '')
        else:
            record['Sell_Behavior'] = 'None'
            record['Sell_Confidence'] = 0
            record['Sell_PsychChange'] = ''

        # 新建候选事件
        new_candidates = daily_summary.get('new_candidates', [])
        record['New_Candidates'] = ', '.join([e.behavior_name for e in new_candidates]) if new_candidates else 'None'

        # 过期事件
        expired = daily_summary.get('expired_events', [])
        record['Expired_Events'] = ', '.join([e.behavior_name for e in expired]) if expired else 'None'

        # V6: 情绪双确认
        record['EmotionConfirmation'] = 'Yes' if self.emotion_confirmation_log and \
            str(self.emotion_confirmation_log[-1]['date']) == record['Date'] else 'No'

        return record

    def get_replay_records(self):
        """获取所有回放记录"""
        return self.replay_records

    def get_behavior_memory(self):
        """获取行为记忆库"""
        return self.evidence_engine.get_behavior_memory()

    def get_emotion_confirmation_log(self):
        """获取情绪双确认日志"""
        return self.emotion_confirmation_log

    def record_trade_result(self, regime, behavior_name, success, date):
        """
        记录交易结果到行为记忆库

        由回测引擎在每笔交易完成后调用。
        """
        if self.evidence_engine.behavior_memory:
            self.evidence_engine.behavior_memory.record_trade(regime, behavior_name, success, date)


def run_strategy_v6(df, start_date=None, use_ml=False, emotion_method='weighted'):
    """便捷函数：运行 V6.0 策略"""
    strategy = V6Strategy(use_ml=use_ml, emotion_method=emotion_method)
    return strategy.run(df, start_date)


def get_today_signal_v6(df):
    """
    获取今日信号（V6.0版本）

    Returns:
        (signal_text, detail_text)
    """
    if len(df) < 60:
        return "0%", "数据不足，需要至少60天数据（V6.0）"

    df = calculate_indicators(df)
    strategy = V6Strategy()
    signals = strategy.run(df)

    if not signals:
        return "N/A", "无信号"

    last = signals[-1]
    lines = []

    lines.append("=" * 60)
    lines.append("A500 ETF V6.0 Evidence Engine 策略")
    lines.append("=" * 60)
    lines.append(f"市场状态: {last['regime']}")
    lines.append(f"市场情绪: {last['psychology']} (Score: {last.get('emotion_score', 'N/A')})")
    lines.append(f"情绪改善: {'是' if last.get('emotion_improving') else '否'}")
    lines.append(f"买入评分: {last['buy_score']:.1f} | 卖出评分: {last['sell_score']:.1f}")
    lines.append(f"净评分: {last['buy_score'] - last['sell_score']:+.1f}")
    lines.append(f"Reward: {last.get('reward_score', 0):.1f} | Risk: {last.get('risk_score', 0):.1f}")
    lines.append("")

    buy_behaviors = last.get('buy_behaviors', [])
    sell_behaviors = last.get('sell_behaviors', [])
    if buy_behaviors:
        lines.append(f"候选人行为: {', '.join(buy_behaviors)}")
    if sell_behaviors:
        lines.append(f"候选卖出行为: {', '.join(sell_behaviors)}")
    lines.append(f"已确认买入事件: {last.get('confirmed_buy_events', 0)}")
    lines.append(f"已确认卖出事件: {last.get('confirmed_sell_events', 0)}")
    lines.append(f"活跃事件总数: {last.get('active_events', 0)}")

    # V6: Evidence Engine 分解
    ev_debug = last.get('evidence_debug', {}).get('buy', [])
    if ev_debug and ev_debug[0]:
        ev = ev_debug[0]
        lines.append("")
        lines.append("--- Evidence Engine 融合明细 ---")
        for src, info in ev.get('sources', {}).items():
            contrib = info.get('contribution', 0)
            if contrib != 0:
                lines.append(f"  {src.upper():>8}: {contrib:+.1f}")
        lines.append(f"  Pre-Decay: {ev.get('pre_decay', 0):.1f}")
        lines.append(f"  TimeDecay: {ev.get('decay_multiplier', 1.0):.3f}")
        lines.append(f"  FINAL:     {ev.get('final', 0):.1f}")

    net = last['buy_score'] - last['sell_score']
    return f"{net:+.1f} NetScore", "\n".join(lines)


if __name__ == "__main__":
    from data_updater import load_data_from_db

    df = load_data_from_db()
    if df is not None:
        signal, detail = get_today_signal_v6(df)
        print(f"V6.0 今日信号: {signal}")
        print(detail)
