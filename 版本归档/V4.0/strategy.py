"""
V4.0 主策略模块（Strategy Pipeline）

完整流程：
  calculate_indicators()
  → detect_market_regime()
  → detect_behaviors()
  → calculate_score()
  → score_to_target_position()
  → position_manager.execute_trade()
"""
import pandas as pd
import numpy as np
from datetime import datetime, time

from indicators import calculate_indicators
from regime_detector import detect_market_regime
from behavior_detector import detect_all_behaviors
from scoring_engine import calculate_score, score_to_target_position
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


def run_strategy(df, start_date=None):
    """
    运行V4.0完整策略流程
    
    Args:
        df: 包含 OHLCV 的 DataFrame
        start_date: 回测起始日期（可选）
    
    Returns:
        signals: list of dict，每个元素包含当日完整信号信息
    """
    # Step 1: 计算指标
    df = calculate_indicators(df)
    
    signals = []
    
    for i in range(len(df)):
        current_date = df.index[i]
        
        # 非交易日跳过
        if not is_trading_day(current_date):
            signals.append({
                'date': current_date,
                'regime': 'NonTrading',
                'buy_score': 0,
                'sell_score': 0,
                'target_position': None,
                'action': 'HOLD',
                'description': '非交易日',
            })
            continue
        
        # 数据不足
        if i < 30:
            signals.append({
                'date': current_date,
                'regime': 'Insufficient',
                'buy_score': 0,
                'sell_score': 0,
                'target_position': None,
                'action': 'HOLD',
                'description': '数据不足',
            })
            continue
        
        # Step 2: 市场状态识别
        regime = detect_market_regime(df, i)
        
        # Step 3: 行为检测
        behavior_result = detect_all_behaviors(df, i, regime)
        
        # Step 4: 评分计算
        buy_score, sell_score, score_detail = calculate_score(behavior_result)
        
        # 当前仓位（从之前信号推算）
        # 这里先返回评分，实际仓位由回测引擎管理
        
        signals.append({
            'date': current_date,
            'regime': regime,
            'buy_score': round(buy_score, 1),
            'sell_score': round(sell_score, 1),
            'buy_behaviors': [b[0] for b in behavior_result['buy_behaviors']],
            'sell_behaviors': [s[0] for s in behavior_result['sell_behaviors']],
            'score_detail': score_detail,
        })
    
    return signals


def get_today_signal(df):
    """
    获取今日信号（用于GUI展示）
    
    Returns:
        (signal_text, detail_text)
    """
    if len(df) < 30:
        return "0%", "数据不足，需要至少30天数据"
    
    df = calculate_indicators(df)
    last_index = len(df) - 1
    
    regime = detect_market_regime(df, last_index)
    behavior_result = detect_all_behaviors(df, last_index, regime)
    buy_score, sell_score, score_detail = calculate_score(behavior_result)
    
    # 构建输出文本
    lines = []
    lines.append(f"市场状态: {regime}")
    lines.append(f"买入评分: {buy_score:.1f} | 卖出评分: {sell_score:.1f}")
    lines.append(f"净评分: {buy_score - sell_score:.1f}")
    
    buy_names = [b[0] for b in behavior_result['buy_behaviors']]
    sell_names = [s[0] for s in behavior_result['sell_behaviors']]
    
    if buy_names:
        lines.append(f"买入行为: {', '.join(buy_names)}")
    if sell_names:
        lines.append(f"卖出行为: {', '.join(sell_names)}")
    if not buy_names and not sell_names:
        lines.append("行为: 无显著行为")
    
    lines.append(f"原始买入分: {score_detail['raw_buy']:.1f} (x{score_detail['buy_mult']})")
    lines.append(f"原始卖出分: {score_detail['raw_sell']:.1f} (x{score_detail['sell_mult']})")
    
    return f"{buy_score - sell_score:+.1f} NetScore", "\n".join(lines)


if __name__ == "__main__":
    from data_updater import load_data_from_db
    
    df = load_data_from_db()
    if df is not None:
        signal, detail = get_today_signal(df)
        print(f"V4.0 今日信号: {signal}")
        print(detail)
