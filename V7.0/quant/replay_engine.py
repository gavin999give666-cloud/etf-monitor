"""
V5.0 交易回放引擎（Replay Engine）
==================================

核心原则：
- 每一次交易必须输出完整解释
- 没有解释的交易视为 Bug

Replay 输出格式：
  Date | Behavior | Market | Psychology | Confidence | Reward | Risk | Score | Signal
"""
import pandas as pd
from config import *


class ReplayEngine:
    """
    交易回放引擎

    负责：
    1. 格式化输出每日策略决策
    2. 输出交易执行记录
    3. 验证每笔交易是否有完整解释
    """

    def __init__(self, replay_records, trades):
        self.records = replay_records
        self.trades = trades
        self.errors = []  # 无解释的交易

    def validate(self):
        """验证所有交易是否有完整解释"""
        self.errors = []

        for trade in self.trades:
            entry_date = trade.get('entry_date')
            exit_date = trade.get('exit_date')

            # 查找对应日期的 replay 记录
            entry_record = self._find_record(entry_date)
            exit_record = self._find_record(exit_date)

            if entry_record is None or entry_record.get('Buy_Behavior') == 'None':
                if entry_record is None or entry_record.get('Sell_Behavior') == 'None':
                    self.errors.append({
                        'type': 'MISSING_EXPLANATION',
                        'trade': trade,
                        'message': f"交易 {trade.get('entry_date')} → {trade.get('exit_date')} 缺少完整解释",
                    })

        return len(self.errors) == 0, self.errors

    def _find_record(self, date):
        """查找指定日期的 replay 记录"""
        date_str = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)
        for r in self.records:
            if r.get('Date') == date_str:
                return r
        return None

    def generate_report(self, max_rows=50):
        """
        生成完整回放报告

        Returns:
            report_df: DataFrame
            summary: dict
        """
        if not self.records:
            return pd.DataFrame(), {}

        df = pd.DataFrame(self.records)
        df = df.tail(max_rows)

        # 统计
        summary = {
            'total_days': len(self.records),
            'days_with_buy_signal': sum(1 for r in self.records if r.get('Buy_Behavior', 'None') != 'None'),
            'days_with_sell_signal': sum(1 for r in self.records if r.get('Sell_Behavior', 'None') != 'None'),
            'total_trades': len(self.trades),
        }

        # 行为统计
        behavior_counts = {}
        for r in self.records:
            buy = r.get('Buy_Behavior', 'None')
            sell = r.get('Sell_Behavior', 'None')
            if buy != 'None':
                behavior_counts[buy] = behavior_counts.get(buy, 0) + 1
            if sell != 'None':
                behavior_counts[sell] = behavior_counts.get(sell, 0) + 1
        summary['behavior_counts'] = behavior_counts

        return df, summary

    def print_replay(self, num_days=20):
        """
        打印最近 N 天的回放记录

        Args:
            num_days: 输出天数
        """
        records = self.records[-num_days:] if self.records else []

        if not records:
            print("无回放记录")
            return

        # 表头
        header = f"{'Date':>12} {'Regime':>8} {'Psych':>10} {'Buy Beh':<18} {'Conf':>6} {'Reward':>7} {'Risk':>6} {'Score':>7}"
        print("=" * len(header))
        print(header)
        print("-" * len(header))

        for r in records:
            line = (
                f"{r['Date']:>12} "
                f"{r['Regime']:>8} "
                f"{r['Psychology']:>10} "
                f"{r.get('Buy_Behavior', 'None'):<18} "
                f"{r.get('Buy_Confidence', 0):>6.0f} "
                f"{r.get('RewardScore', 0):>7.1f} "
                f"{r.get('RiskScore', 0):>6.1f} "
                f"{r.get('BuyScore', 0):>7.1f}"
            )
            print(line)

    def print_trade_explanation(self, trade_index=None):
        """
        打印单笔交易的完整解释

        Args:
            trade_index: 交易索引，None 则打印最后一笔
        """
        if not self.trades:
            print("无交易记录")
            return

        if trade_index is None:
            trade = self.trades[-1]
        elif 0 <= trade_index < len(self.trades):
            trade = self.trades[trade_index]
        else:
            print(f"无效索引: {trade_index}")
            return

        entry_record = self._find_record(trade.get('entry_date'))
        exit_record = self._find_record(trade.get('exit_date'))

        print("\n" + "=" * 70)
        print("交易解释（Trade Explanation）")
        print("=" * 70)

        print(f"\n买入日期: {trade.get('entry_date')}")
        print(f"卖出日期: {trade.get('exit_date')}")
        print(f"买入价格: {trade.get('entry_price', 'N/A')}")
        print(f"卖出价格: {trade.get('exit_price', 'N/A')}")
        print(f"收益率: {trade.get('pnl_pct', 0) * 100:.2f}%")

        if entry_record:
            print(f"\n买入理由:")
            print(f"  行为: {entry_record.get('Buy_Behavior', 'Unknown')}")
            print(f"  市场状态: {entry_record.get('Regime', 'Unknown')}")
            print(f"  市场情绪: {entry_record.get('Psychology', 'Unknown')}")
            print(f"  情绪变化: {entry_record.get('Buy_PsychChange', 'N/A')}")
            print(f"  置信度: {entry_record.get('Buy_Confidence', 'N/A')}")
            print(f"  Reward: {entry_record.get('RewardScore', 'N/A')}")
            print(f"  Risk: {entry_record.get('RiskScore', 'N/A')}")
            print(f"  评分: {entry_record.get('BuyScore', 'N/A')}")

        if exit_record:
            print(f"\n卖出理由:")
            print(f"  行为: {exit_record.get('Sell_Behavior', 'Unknown')}")
            print(f"  市场情绪: {exit_record.get('Psychology', 'Unknown')}")
            print(f"  情绪变化: {exit_record.get('Sell_PsychChange', 'N/A')}")
            print(f"  置信度: {exit_record.get('Sell_Confidence', 'N/A')}")
            print(f"  Risk: {exit_record.get('RiskScore', 'N/A')}")
            print(f"  评分: {exit_record.get('SellScore', 'N/A')}")

        print("-" * 70)
