"""
V5.0 策略流水线（Strategy Pipeline）
=====================================

完整的7层架构：

  数据预处理（indicators.py）
        ↓
  Market Regime（regime_detector.py）
        ↓
  Behavior Detection（behavior_detector.py）
        ↓
  Crowd Psychology（crowd_psychology.py）
        ↓
  Event Engine + Lifecycle（event_engine.py）
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
from crowd_psychology import CrowdPsychology
from event_engine import EventEngine
from reward_risk import evaluate_reward_risk
from scoring_engine import calculate_final_score, score_to_target_position
from position_manager import PositionManager

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


class V5Strategy:
    """
    V5.0 策略流水线

    维护全策略状态（Event Engine, Crowd Psychology 等跨日状态）
    """

    def __init__(self):
        self.event_engine = EventEngine()
        self.psychology_engine = CrowdPsychology()
        self.signals = []           # 每日信号
        self.replay_records = []    # 回放记录

    def run(self, df, start_date=None):
        """
        运行 V5.0 完整策略流水线

        Args:
            df: 包含 OHLCV 的 DataFrame（已计算指标）
            start_date: 回测起始日期

        Returns:
            signals: list of dict
        """
        # Step 0: 确保指标已计算
        df = calculate_indicators(df)

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
                    'psychology': self.psychology_engine.get_state(),
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

            # ---- Layer 3: Crowd Psychology Update ----
            psych_state, psych_changed, psych_desc = self.psychology_engine.update(df, i, current_date)

            # ---- Layer 4: Event Engine (Lifecycle) ----
            daily_summary = self.event_engine.process_daily(
                current_date, behavior_result, regime, psych_state, df, i
            )

            # ---- Layer 5: Reward / Risk Evaluation ----
            reward_score, risk_score, reward_detail, risk_detail = evaluate_reward_risk(
                df, i, regime, psych_state
            )

            # ---- Layer 6: Scoring Engine ----
            buy_score, sell_score, score_breakdown = calculate_final_score(
                self.event_engine, reward_score, risk_score, psych_state, regime
            )

            # ---- Layer 7: Position (handled by backtest engine) ----
            # 这里只返回评分，实际仓位由回测引擎处理

            # ---- 构建 Replay 记录 ----
            replay = self._build_replay_record(
                current_date, regime, psych_state, psych_changed, psych_desc,
                daily_summary, buy_score, sell_score, score_breakdown,
                reward_score, risk_score
            )
            self.replay_records.append(replay)

            # ---- 构建信号 ----
            buy_behaviors = [b[0] for b in behavior_result.get('buy_behaviors', [])]
            sell_behaviors = [s[0] for s in behavior_result.get('sell_behaviors', [])]

            signal = {
                'date': current_date,
                'regime': regime,
                'psychology': psych_state,
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
                'replay': replay,
            }
            self.signals.append(signal)

        return self.signals

    def _build_replay_record(self, date, regime, psych_state, psych_changed, psych_desc,
                              daily_summary, buy_score, sell_score, score_breakdown,
                              reward_score, risk_score):
        """构建完整的交易解释记录"""
        record = {
            'Date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'Regime': regime,
            'Psychology': psych_state,
            'Psychology_Change': psych_desc if psych_changed else '维持不变',
            'BuyScore': round(buy_score, 1),
            'SellScore': round(sell_score, 1),
            'RewardScore': round(reward_score, 1),
            'RiskScore': round(risk_score, 1),
        }

        # 买入事件详情
        buy_events = score_breakdown['buy']['events']
        if buy_events:
            record['Buy_Behavior'] = buy_events[0]['name']
            record['Buy_Confidence'] = buy_events[0]['confidence']
            record['Buy_PsychChange'] = buy_events[0].get('psych_change', {}).get('description', '')
        else:
            record['Buy_Behavior'] = 'None'
            record['Buy_Confidence'] = 0
            record['Buy_PsychChange'] = ''

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

        return record

    def get_replay_records(self):
        """获取所有回放记录"""
        return self.replay_records


def run_strategy_v5(df, start_date=None):
    """便捷函数：运行 V5.0 策略"""
    strategy = V5Strategy()
    return strategy.run(df, start_date)


def get_today_signal_v5(df):
    """
    获取今日信号（V5.0版本）

    Returns:
        (signal_text, detail_text)
    """
    if len(df) < 60:
        return "0%", "数据不足，需要至少60天数据（V5.0）"

    df = calculate_indicators(df)
    strategy = V5Strategy()
    signals = strategy.run(df)

    if not signals:
        return "N/A", "无信号"

    last = signals[-1]
    lines = []

    lines.append("=" * 60)
    lines.append("A500 ETF V5.0 行为生命周期策略")
    lines.append("=" * 60)
    lines.append(f"市场状态: {last['regime']}")
    lines.append(f"市场情绪: {last['psychology']}")
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

    net = last['buy_score'] - last['sell_score']
    return f"{net:+.1f} NetScore", "\n".join(lines)


if __name__ == "__main__":
    from data_updater import load_data_from_db

    df = load_data_from_db()
    if df is not None:
        signal, detail = get_today_signal_v5(df)
        print(f"V5.0 今日信号: {signal}")
        print(detail)
