"""
V4.0 增强回测引擎

特性：
1. 逐笔交易跟踪
2. 完整的行为统计
3. 评分统计分析
4. 行为贡献分析
5. 参数敏感性分析
"""
import pandas as pd
import numpy as np
import math
from collections import defaultdict

from config import (BACKTEST_START, INITIAL_CASH, INITIAL_POSITION,
                    MAX_POSITION, MIN_TRADE_VALUE, SLIPPAGE, COMMISSION)
from position_manager import PositionManager
from scoring_engine import score_to_target_position


class V4Backtest:
    """V4.0 增强回测引擎"""

    def __init__(self, df, start_date=BACKTEST_START, initial_cash=INITIAL_CASH,
                 initial_position=INITIAL_POSITION):
        self.df_orig = df
        self.initial_cash = initial_cash
        self.initial_position = initial_position

        # 过滤回测区间
        start_dt = pd.Timestamp(start_date)
        self.df = df[df.index >= start_dt].copy()
        self.df_orig_bt = df[df.index >= start_dt]

        # 初始化
        self.pm = PositionManager(initial_cash, initial_position)

        # 核心数据存储
        self.daily_equity = []         # 每日净值
        self.daily_returns = []        # 每日收益率
        self.trades = []               # 完整交易记录
        self.daily_signals = []        # 每日信号（含行为详情）

        # 行为统计
        self.behavior_counts = defaultdict(int)
        self.behavior_trades = defaultdict(list)  # behavior_name → [(date, pnl), ...]
        self.score_history = []        # [(date, buy_score, sell_score), ...]
        self.current_trade = None

    def run(self, signals):
        """
        运行回测
        
        Args:
            signals: list of dict，来自 strategy.run_strategy() 的输出
        """
        # 建立信号索引
        signal_map = {s['date']: s for s in signals}

        start_price = self.df.iloc[0]['close']
        self.pm.set_initial_shares(start_price)
        initial_value = self.pm.get_total_value(start_price)
        self.daily_equity.append({'date': self.df.index[0], 'equity': initial_value})

        for i, (date, row) in enumerate(self.df.iterrows()):
            current_price = row['close']

            # 获取当日信号
            day_signal = signal_map.get(date)
            if day_signal is None:
                day_signal = {
                    'buy_score': 0, 'sell_score': 0,
                    'buy_behaviors': [], 'sell_behaviors': [],
                    'regime': 'Unknown'
                }

            buy_score = day_signal.get('buy_score', 0)
            sell_score = day_signal.get('sell_score', 0)
            buy_behaviors = day_signal.get('buy_behaviors', [])
            sell_behaviors = day_signal.get('sell_behaviors', [])

            # 评分历史
            self.score_history.append({
                'date': date,
                'buy_score': buy_score,
                'sell_score': sell_score,
                'regime': day_signal.get('regime', 'Unknown'),
            })

            # 行为计数
            for b in buy_behaviors:
                self.behavior_counts[b] += 1
            for s in sell_behaviors:
                self.behavior_counts[s] += 1

            # 计算目标仓位（使用实际仓位，含自然漂移）
            total_value = self.pm.get_total_value(current_price)
            actual_pos = self.pm.shares * current_price / total_value if total_value > 0 else 0
            actual_pos = max(0.0, min(1.0, actual_pos))
            target_pos = score_to_target_position(buy_score, sell_score, actual_pos)

            # 仅当目标与上次目标不同且有行为信号时才执行
            # 避免价格漂移导致的被动再平衡
            prev_target = self.pm.position_pct
            if abs(target_pos - prev_target) > 0.01 or ((buy_behaviors or sell_behaviors) and abs(target_pos - actual_pos) > 0.02):
                trade_info = self.pm.execute_trade(target_pos, current_price)
            else:
                trade_info = None

            # 记录信号
            signal_record = {
                'date': date,
                'buy_score': buy_score,
                'sell_score': sell_score,
                'buy_behaviors': buy_behaviors,
                'sell_behaviors': sell_behaviors,
                'regime': day_signal.get('regime'),
                'target_position': target_pos,
                'current_position': actual_pos,
                'executed': trade_info is not None,
            }
            if trade_info:
                signal_record.update({
                    'action': trade_info['action'],
                    'trade_value': trade_info['trade_value'],
                    'executed_price': trade_info['executed_price'],
                })
            self.daily_signals.append(signal_record)

            # 记录交易（进出对）
            if trade_info:
                if trade_info['action'] == 'BUY':
                    if self.current_trade is None:
                        self.current_trade = {
                            'entry_date': date,
                            'entry_price': trade_info['executed_price'],
                            'entry_reason': buy_behaviors,
                            'entry_score': buy_score,
                        }
                elif trade_info['action'] == 'SELL':
                    if self.current_trade is not None:
                        sell_price = trade_info['executed_price']
                        pnl_pct = (sell_price - self.current_trade['entry_price']) / self.current_trade['entry_price']
                        trade_record = {
                            'entry_date': self.current_trade['entry_date'],
                            'exit_date': date,
                            'entry_price': self.current_trade['entry_price'],
                            'exit_price': sell_price,
                            'pnl_pct': pnl_pct,
                            'entry_reason': self.current_trade['entry_reason'],
                            'entry_score': self.current_trade['entry_score'],
                            'exit_reason': sell_behaviors,
                            'exit_score': sell_score,
                        }
                        self.trades.append(trade_record)

                        # 行为贡献记录
                        for b in self.current_trade['entry_reason']:
                            self.behavior_trades[b].append(pnl_pct)
                        for s in sell_behaviors:
                            self.behavior_trades[s].append(pnl_pct)

                        self.current_trade = None

            # 记录每日净值
            equity = self.pm.get_total_value(current_price)
            self.daily_equity.append({'date': date, 'equity': equity})
            if len(self.daily_equity) >= 2:
                prev_eq = self.daily_equity[-2]['equity']
                if prev_eq > 0:
                    self.daily_returns.append((equity - prev_eq) / prev_eq)

        # 平仓未完成交易
        if self.current_trade is not None:
            end_price = self.df.iloc[-1]['close']
            pnl_pct = (end_price - self.current_trade['entry_price']) / self.current_trade['entry_price']
            trade_record = {
                'entry_date': self.current_trade['entry_date'],
                'exit_date': self.df.index[-1],
                'entry_price': self.current_trade['entry_price'],
                'exit_price': end_price,
                'pnl_pct': pnl_pct,
                'entry_reason': self.current_trade['entry_reason'],
                'entry_score': self.current_trade['entry_score'],
                'exit_reason': ['EndOfBacktest'],
                'exit_score': 0,
            }
            self.trades.append(trade_record)
            for b in self.current_trade['entry_reason']:
                self.behavior_trades[b].append(pnl_pct)
            self.current_trade = None

        return self._compute_results()

    def _compute_results(self):
        """计算所有回测指标"""
        equity_df = pd.DataFrame(self.daily_equity)
        equity_df.set_index('date', inplace=True)

        if equity_df.empty:
            return {}

        results = {}

        # 基础收益
        start_price = self.df.iloc[0]['close']
        end_price = self.df.iloc[-1]['close']
        start_equity = equity_df['equity'].iloc[0]
        end_equity = equity_df['equity'].iloc[-1]

        results['benchmark_return'] = (end_price - start_price) / start_price
        results['strategy_return'] = (end_equity - start_equity) / start_equity
        results['final_equity'] = end_equity
        results['start_equity'] = start_equity
        results['excess_return'] = results['strategy_return'] - results['benchmark_return']

        # 交易统计
        if self.trades:
            trade_pnls = [t['pnl_pct'] for t in self.trades]
            winning = [p for p in trade_pnls if p >= 0]
            losing = [p for p in trade_pnls if p < 0]

            results['total_trades'] = len(self.trades)
            results['winning_trades'] = len(winning)
            results['losing_trades'] = len(losing)
            results['win_rate'] = len(winning) / len(self.trades) if self.trades else 0

            total_profit = sum(winning) if winning else 0
            total_loss = abs(sum(losing)) if losing else 1e-10
            results['profit_factor'] = total_profit / total_loss

            results['avg_profit'] = np.mean(winning) if winning else 0
            results['avg_loss'] = np.mean(losing) if losing else 0
            results['max_profit'] = max(trade_pnls) if trade_pnls else 0
            results['max_loss'] = min(trade_pnls) if trade_pnls else 0
            results['max_consecutive_wins'] = self._max_consecutive(trade_pnls, True)
            results['max_consecutive_losses'] = self._max_consecutive(trade_pnls, False)
        else:
            results.update({
                'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0,
                'win_rate': 0, 'profit_factor': 0, 'avg_profit': 0,
                'avg_loss': 0, 'max_profit': 0, 'max_loss': 0,
                'max_consecutive_wins': 0, 'max_consecutive_losses': 0,
            })

        # 风险指标
        cummax = equity_df['equity'].cummax()
        drawdown = (equity_df['equity'] - cummax) / cummax
        results['max_drawdown'] = drawdown.min()

        trading_days = len(equity_df)
        years = trading_days / 252.0
        results['annualized_return'] = ((end_equity / start_equity) ** (1.0 / years) - 1) if years > 0 else 0

        if self.daily_returns:
            avg_dr = np.mean(self.daily_returns)
            std_dr = np.std(self.daily_returns, ddof=0)
            results['sharpe_ratio'] = (avg_dr / std_dr * math.sqrt(252)) if std_dr > 0 else 0
            results['volatility'] = std_dr * math.sqrt(252)
        else:
            results['sharpe_ratio'] = 0
            results['volatility'] = 0

        results['calmar_ratio'] = (results['annualized_return'] / abs(results['max_drawdown'])) if abs(results['max_drawdown']) > 1e-10 else 0

        # 交易频率
        results['trading_days'] = trading_days
        results['trade_frequency'] = results['total_trades'] / trading_days if trading_days > 0 else 0

        return results

    def get_behavior_statistics(self):
        """行为统计"""
        stats = {}
        all_behavior_names = ['DoubleBottom', 'PanicSell', 'TrendPullback',
                              'BreakoutConfirm', 'MomentumExhaustion',
                              'TrendFailure', 'FalseBreak']

        for name in all_behavior_names:
            count = self.behavior_counts.get(name, 0)
            trades_list = self.behavior_trades.get(name, [])
            if trades_list:
                avg_contrib = np.mean(trades_list)
                total_contrib = np.sum(trades_list)
            else:
                avg_contrib = 0
                total_contrib = 0
            stats[name] = {
                'count': count,
                'trades': len(trades_list),
                'avg_contribution': avg_contrib,
                'total_contribution': total_contrib,
            }
        return stats

    def get_score_statistics(self):
        """评分统计"""
        if not self.score_history:
            return {}

        buy_scores = [s['buy_score'] for s in self.score_history]
        sell_scores = [s['sell_score'] for s in self.score_history]

        # 评分分布
        buy_distribution = {
            '90+': sum(1 for s in buy_scores if s >= 90),
            '80-90': sum(1 for s in buy_scores if 80 <= s < 90),
            '70-80': sum(1 for s in buy_scores if 70 <= s < 80),
            '60-70': sum(1 for s in buy_scores if 60 <= s < 70),
            '50-60': sum(1 for s in buy_scores if 50 <= s < 60),
            '<50': sum(1 for s in buy_scores if s < 50),
        }
        sell_distribution = {
            '85+': sum(1 for s in sell_scores if s >= 85),
            '75-85': sum(1 for s in sell_scores if 75 <= s < 85),
            '65-75': sum(1 for s in sell_scores if 65 <= s < 75),
            '55-65': sum(1 for s in sell_scores if 55 <= s < 65),
            '45-55': sum(1 for s in sell_scores if 45 <= s < 55),
            '<45': sum(1 for s in sell_scores if s < 45),
        }

        return {
            'avg_buy_score': np.mean(buy_scores) if buy_scores else 0,
            'avg_sell_score': np.mean(sell_scores) if sell_scores else 0,
            'max_buy_score': max(buy_scores) if buy_scores else 0,
            'max_sell_score': max(sell_scores) if sell_scores else 0,
            'buy_distribution': buy_distribution,
            'sell_distribution': sell_distribution,
        }

    @staticmethod
    def _max_consecutive(pnls, is_win):
        max_count = 0
        current_count = 0
        for p in pnls:
            if (is_win and p >= 0) or (not is_win and p < 0):
                current_count += 1
                max_count = max(max_count, current_count)
            else:
                current_count = 0
        return max_count
