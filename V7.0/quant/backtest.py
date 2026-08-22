"""
V6.2.3 增强回测引擎
================================

V6.2.3 升级：
1. 集成 Replay Learning（行为成功率统计）
2. 集成 Evidence Engine（多源置信度证据追踪）
3. 情绪双确认统计
4. 完整的绩效指标 + 行为记忆库
"""
import pandas as pd
import numpy as np
import math
from collections import defaultdict

import config as _cfg
from position_manager import PositionManager
from scoring_engine import score_to_target_position
from replay_engine import ReplayEngine
from feature_builder import FeatureBuilder
from risk_guard import RiskGuard


class V6Backtest:
    """V6.2.3 增强回测引擎"""

    def __init__(self, df, start_date=None, initial_cash=None,
                 initial_position=None, strategy=None):
        if start_date is None:
            start_date = _cfg.BACKTEST_START
        if start_date is None:
            start_date = str(df.index[0].date())
        if initial_cash is None:
            initial_cash = _cfg.INITIAL_CASH
        if initial_position is None:
            initial_position = _cfg.INITIAL_POSITION
        self.df_orig = df
        self.initial_cash = initial_cash
        self.initial_position = initial_position
        self.strategy = strategy  # V6策略实例，用于更新行为记忆库
        self._backtest_start = start_date  # 记录实际使用的起始日期

        start_dt = pd.Timestamp(start_date)
        self.df = df[df.index >= start_dt].copy()

        self.pm = PositionManager(initial_cash, initial_position)

        # V7.0 P6.5: L3 风控层实例（跨日状态：止损/止盈/熔断）
        self.risk_guard = RiskGuard()

        self.daily_equity = []
        self.daily_returns = []
        self.trades = []
        self.daily_signals = []

        self.behavior_counts = defaultdict(int)
        self.behavior_trades = defaultdict(list)
        self.event_statistics = defaultdict(int)  # 事件统计
        self.psychology_transitions = []           # 情绪切换记录
        self.score_history = []
        self.current_trade = None

        # V6: Evidence Engine 统计
        self.evidence_history = []
        self.emotion_confirmation_count = 0

        # Replay 记录
        self.replay_records = []

    def run(self, signals):
        """
        运行回测

        Args:
            signals: list of dict，来自 V5Strategy.run()
        """
        signal_map = {s['date']: s for s in signals}

        # V7.0 P6.5: 初始仓位由首日 Regime 的 center 决定（消除"回测起点即满仓"偏差）
        if _cfg.STRATEGY_MODE == 'V7' and signals:
            first_center = signals[0].get('center')
            if first_center is not None:
                init_pos = max(0.0, min(_cfg.MAX_POSITION, first_center))
                self.pm.position_pct = init_pos
                self.pm.cash = self.initial_cash * (1 - init_pos)

        start_price = self.df.iloc[0]['close']
        self.pm.set_initial_shares(start_price)
        initial_value = self.pm.get_total_value(start_price)
        self.daily_equity.append({'date': self.df.index[0], 'equity': initial_value})

        for i, (date, row) in enumerate(self.df.iterrows()):
            current_price = row['close']

            day_signal = signal_map.get(date)
            if day_signal is None:
                day_signal = {
                    'buy_score': 0, 'sell_score': 0,
                    'buy_behaviors': [], 'sell_behaviors': [],
                    'regime': 'Unknown', 'psychology': 'Unknown',
                    'reward_score': 0, 'risk_score': 0,
                    'confirmed_buy_events': 0, 'confirmed_sell_events': 0,
                    'active_events': 0,
                }

            buy_score = day_signal.get('buy_score', 0)
            sell_score = day_signal.get('sell_score', 0)
            buy_behaviors = day_signal.get('buy_behaviors', [])
            sell_behaviors = day_signal.get('sell_behaviors', [])

            # 收集 Replay 记录
            replay = day_signal.get('replay', {})
            if replay:
                self.replay_records.append(replay)

            # 评分历史
            self.score_history.append({
                'date': date,
                'buy_score': buy_score,
                'sell_score': sell_score,
                'regime': day_signal.get('regime', 'Unknown'),
                'psychology': day_signal.get('psychology', 'Unknown'),
                'emotion_score': day_signal.get('emotion_score', 50),
                'reward': day_signal.get('reward_score', 0),
                'risk': day_signal.get('risk_score', 0),
            })

            # V6: 情绪双确认统计
            if day_signal.get('emotion_improving') and buy_behaviors:
                self.emotion_confirmation_count += 1

            # V6: Evidence Engine 追踪
            ev_debug = day_signal.get('evidence_debug', {})
            if ev_debug:
                self.evidence_history.append({
                    'date': date,
                    'buy_evidence': ev_debug.get('buy', []),
                    'sell_evidence': ev_debug.get('sell', []),
                })

            # 行为计数
            for b in buy_behaviors:
                self.behavior_counts[b] += 1
            for s in sell_behaviors:
                self.behavior_counts[s] += 1

            # V5.0: 事件统计
            confirmed_buy = day_signal.get('confirmed_buy_events', 0)
            confirmed_sell = day_signal.get('confirmed_sell_events', 0)
            active = day_signal.get('active_events', 0)
            self.event_statistics['confirmed_buy'] += confirmed_buy
            self.event_statistics['confirmed_sell'] += confirmed_sell
            self.event_statistics['max_active_events'] = max(
                self.event_statistics.get('max_active_events', 0), active
            )

            # 计算目标仓位
            total_value = self.pm.get_total_value(current_price)
            actual_pos = self.pm.shares * current_price / total_value if total_value > 0 else 0
            actual_pos = max(0.0, min(1.0, actual_pos))

            # V7.0 P6.5: L1/L2 合成（center+offset）+ L3 风控覆写（cap）
            risk_actions = []
            if _cfg.STRATEGY_MODE == 'V7':
                target_pos = score_to_target_position(
                    buy_score, sell_score, actual_pos,
                    regime=day_signal.get('regime', 'Unknown'),
                    center=day_signal.get('center'),
                    offset_boost=day_signal.get('offset_boost', 0.0),
                    offset_penalty=day_signal.get('offset_penalty', 0.0),
                )
                cap, risk_actions = self.risk_guard.evaluate(
                    current_price, actual_pos, total_value,
                    ma20=row.get('MA20'), high_60d=row.get('high_60d'), date=date,
                )
                if cap is not None:
                    target_pos = min(target_pos, cap)
            else:
                target_pos = score_to_target_position(buy_score, sell_score, actual_pos)

            prev_target = self.pm.position_pct
            if abs(target_pos - prev_target) > _cfg.TRADE_TARGET_DELTA or ((buy_behaviors or sell_behaviors) and abs(target_pos - actual_pos) > _cfg.TRADE_ACTUAL_DELTA):

                # V6.2.3: 最低持仓天数检查 —— 买入后N天内不允许卖出
                # V7.0 P6.5: L3 风控动作豁免锁仓（止损/止盈/熔断优先级最高，见设计方案 §2.4）
                if target_pos < prev_target and self.current_trade is not None and not risk_actions:
                    min_hold = _cfg.MIN_HOLD_DAYS
                    entry_date = self.current_trade.get('entry_date')
                    if entry_date is not None:
                        hold_days = (date - entry_date).days if hasattr(date, 'days') else (
                            (pd.Timestamp(date) - pd.Timestamp(entry_date)).days)
                        if hold_days < min_hold:
                            trade_info = None  # 未满最低持仓，阻止卖出
                        else:
                            trade_info = self.pm.execute_trade(target_pos, current_price)
                    else:
                        trade_info = self.pm.execute_trade(target_pos, current_price)
                else:
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
                'psychology': day_signal.get('psychology'),
                'reward_score': day_signal.get('reward_score', 0),
                'risk_score': day_signal.get('risk_score', 0),
                'target_position': target_pos,
                'current_position': actual_pos,
                'executed': trade_info is not None,
                # V7.0 P6.5: L3 风控动作 + 相位（记录进 backtest_records）
                'risk_actions': risk_actions,
                'phase': day_signal.get('phase', {}),
                'center': day_signal.get('center'),
            }
            if trade_info:
                signal_record.update({
                    'action': trade_info['action'],
                    'trade_value': trade_info['trade_value'],
                    'executed_price': trade_info['executed_price'],
                })
            self.daily_signals.append(signal_record)

            # 记录交易
            if trade_info:
                if trade_info['action'] == 'BUY':
                    if self.current_trade is None:
                        # V6: 捕获买入时的 Evidence Engine 分解
                        entry_ev = day_signal.get('evidence_debug', {}).get('buy', [])
                        entry_breakdown = day_signal.get('score_breakdown', {}).get('buy', {})
                        self.current_trade = {
                            'entry_date': date,
                            'entry_price': trade_info['executed_price'],
                            'entry_reason': buy_behaviors,
                            'entry_regime': day_signal.get('regime', 'Unknown'),
                            'entry_psychology': day_signal.get('psychology', 'Unknown'),
                            'entry_emotion_score': day_signal.get('emotion_score', 50),
                            'entry_emotion_improving': day_signal.get('emotion_improving', False),
                            'entry_score': buy_score,
                            'entry_reward': day_signal.get('reward_score', 0),
                            'entry_risk': day_signal.get('risk_score', 0),
                            # V6: Evidence Engine 分解
                            'entry_evidence': entry_ev[0] if entry_ev else {},
                            'entry_score_breakdown': {
                                'behavior_score': entry_breakdown.get('behavior_score', 0),
                                'confidence': entry_breakdown.get('confidence', 0),
                                'reward_score': entry_breakdown.get('reward_score', 0),
                                'regime_mult': entry_breakdown.get('regime_mult', 1.0),
                                'final_score': entry_breakdown.get('final_score', 0),
                            },
                        }
                        # V7.0 P6.5: 通知 L3 风控层建仓（记录持仓成本）
                        if _cfg.STRATEGY_MODE == 'V7':
                            self.risk_guard.on_entry(trade_info['executed_price'], date)

                        # V6.2.3: 买入后标记买卖确认事件，防止重复贡献评分
                        if self.strategy:
                            for e in list(self.strategy.event_engine.get_confirmed_buy_events()):
                                e.mark_executed()
                            # 买入方向已确定，清除反方向的卖出确认事件
                            for e in list(self.strategy.event_engine.get_confirmed_sell_events()):
                                e.mark_finished()
                elif trade_info['action'] == 'SELL':
                    if self.current_trade is not None:
                        sell_price = trade_info['executed_price']
                        pnl_pct = (sell_price - self.current_trade['entry_price']) / self.current_trade['entry_price']
                        # V6: 捕获卖出时的 Evidence Engine 分解
                        exit_ev = day_signal.get('evidence_debug', {}).get('sell', [])
                        exit_breakdown = day_signal.get('score_breakdown', {}).get('sell', {})
                        trade_record = {
                            'trade_id': len(self.trades) + 1,
                            'entry_date': str(self.current_trade['entry_date']),
                            'exit_date': str(date),
                            'entry_price': round(self.current_trade['entry_price'], 4),
                            'exit_price': round(sell_price, 4),
                            'pnl_pct': round(pnl_pct * 100, 2),
                            'pnl_label': 'WIN' if pnl_pct > 0 else 'LOSS',
                            # 买入促成因子
                            'entry_behavior': self.current_trade['entry_reason'],
                            'entry_regime': self.current_trade.get('entry_regime', 'Unknown'),
                            'entry_psychology': self.current_trade.get('entry_psychology', 'Unknown'),
                            'entry_emotion_score': round(self.current_trade.get('entry_emotion_score', 50), 1),
                            'entry_emotion_improving': self.current_trade.get('entry_emotion_improving', False),
                            'entry_score': round(self.current_trade['entry_score'], 1),
                            'entry_reward': round(self.current_trade.get('entry_reward', 0), 1),
                            'entry_risk': round(self.current_trade.get('entry_risk', 0), 1),
                            # V6: 买入时 Evidence Engine 促成因子
                            'entry_factors': self._extract_evidence_factors(
                                self.current_trade.get('entry_evidence', {}),
                                self.current_trade.get('entry_score_breakdown', {})
                            ),
                            # 卖出促成因子
                            # 无卖出行为时由净评分下降触发减仓，区别于真正的回测结束平仓
                            'exit_behavior': sell_behaviors if sell_behaviors else ['NoSellSignal'],
                            'exit_psychology': day_signal.get('psychology', 'Unknown'),
                            'exit_score': round(sell_score, 1),
                            'exit_risk': round(day_signal.get('risk_score', 0), 1),
                            # V6: 卖出时 Evidence Engine 促成因子
                            'exit_factors': self._extract_evidence_factors(
                                exit_ev[0] if exit_ev else {},
                                exit_breakdown
                            ),
                        }
                        self.trades.append(trade_record)

                        # V6.2.3: 记录交易结果到行为记忆库 (Replay Learning with Psychology)
                        if self.strategy is not None:
                            regime = self.current_trade.get('entry_regime', 'Unknown')
                            success = pnl_pct > 0
                            psychology = self.current_trade.get('entry_psychology')
                            for b in self.current_trade['entry_reason']:
                                self.strategy.record_trade_result(
                                    regime, b, success, self.current_trade['entry_date'],
                                    psychology=psychology
                                )
                                self.behavior_trades[b].append(pnl_pct)
                            for s in sell_behaviors:
                                self.behavior_trades[s].append(pnl_pct)

                            # V6.2.3: 收集ML训练样本（统一 FeatureBuilder）
                            ml_features = FeatureBuilder.build_from_trade(
                                self.current_trade
                            )
                            if ml_features is not None:
                                self.strategy.add_ml_training_sample(
                                    ml_features, 1 if success else 0
                                )
                        else:
                            for b in self.current_trade['entry_reason']:
                                self.behavior_trades[b].append(pnl_pct)
                            for s in sell_behaviors:
                                self.behavior_trades[s].append(pnl_pct)

                        # V6.2.3: 卖出后标记买卖确认事件，防止重复贡献评分
                        if self.strategy:
                            for e in list(self.strategy.event_engine.get_confirmed_sell_events()):
                                e.mark_executed()
                            # 卖出方向已确定，清除反方向的买入确认事件
                            for e in list(self.strategy.event_engine.get_confirmed_buy_events()):
                                e.mark_finished()

                        # V7.0 P6.5: 通知 L3 风控层平仓（清空持仓状态，保留熔断状态）
                        if _cfg.STRATEGY_MODE == 'V7':
                            self.risk_guard.on_exit()

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
                'trade_id': len(self.trades) + 1,
                'entry_date': str(self.current_trade['entry_date']),
                'exit_date': str(self.df.index[-1]),
                'entry_price': round(self.current_trade['entry_price'], 4),
                'exit_price': round(end_price, 4),
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_label': 'WIN' if pnl_pct > 0 else 'LOSS',
                'entry_behavior': self.current_trade['entry_reason'],
                'entry_regime': self.current_trade.get('entry_regime', 'Unknown'),
                'entry_psychology': self.current_trade.get('entry_psychology', 'Unknown'),
                'entry_emotion_score': round(self.current_trade.get('entry_emotion_score', 50), 1),
                'entry_emotion_improving': self.current_trade.get('entry_emotion_improving', False),
                'entry_score': round(self.current_trade['entry_score'], 1),
                'entry_reward': round(self.current_trade.get('entry_reward', 0), 1),
                'entry_risk': round(self.current_trade.get('entry_risk', 0), 1),
                'entry_factors': self._extract_evidence_factors(
                    self.current_trade.get('entry_evidence', {}),
                    self.current_trade.get('entry_score_breakdown', {})
                ),
                'exit_behavior': ['EndOfBacktest'],
                'exit_psychology': 'End',
                'exit_score': 0,
                'exit_risk': 0,
                'exit_factors': {'reason': '回测结束强制平仓'},
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

        start_price = self.df.iloc[0]['close']
        end_price = self.df.iloc[-1]['close']
        start_equity = equity_df['equity'].iloc[0]
        end_equity = equity_df['equity'].iloc[-1]

        results['benchmark_return'] = (end_price - start_price) / start_price
        results['strategy_return'] = (end_equity - start_equity) / start_equity
        results['final_equity'] = end_equity
        results['start_equity'] = start_equity
        results['excess_return'] = results['strategy_return'] - results['benchmark_return']

        if self.trades:
            trade_pnls = [t['pnl_pct'] for t in self.trades]
            winning = [p for p in trade_pnls if p >= 0]
            losing = [p for p in trade_pnls if p < 0]

            results['total_trades'] = len(self.trades)
            results['winning_trades'] = len(winning)
            results['losing_trades'] = len(losing)
            results['win_rate'] = len(winning) / len(self.trades) if self.trades else 0

            total_profit = sum(winning) if winning else 0
            total_loss = abs(sum(losing)) if losing else 0.0
            # 无亏损交易时盈亏比为无穷大（避免 1e-10 假数值）
            results['profit_factor'] = (total_profit / total_loss) if total_loss > 0 else float('inf')
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

        cummax = equity_df['equity'].cummax()
        drawdown = (equity_df['equity'] - cummax) / cummax
        results['max_drawdown'] = drawdown.min()

        # V7.0 P6.6: 基准（buy & hold）最大回撤 —— 用收盘价序列的峰谷回撤
        # 供 benchmark_beating_objective 计算 dd_improvement 与 DD_TARGET（基准回撤的50%）
        _bclose = self.df['close'].values
        if len(_bclose) > 0:
            _bcummax = np.maximum.accumulate(_bclose)
            _bdd = (_bclose - _bcummax) / np.where(_bcummax > 0, _bcummax, 1)
            results['benchmark_max_drawdown'] = float(_bdd.min())
        else:
            results['benchmark_max_drawdown'] = 0.0

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
        results['trading_days'] = trading_days
        results['trade_frequency'] = results['total_trades'] / trading_days if trading_days > 0 else 0

        # V6.2.3: Sortino Ratio
        if self.daily_returns:
            negative_returns = [r for r in self.daily_returns if r < 0]
            if negative_returns:
                downside_dev = np.std(negative_returns, ddof=0) * math.sqrt(252)
                results['sortino_ratio'] = (results['annualized_return'] - 0.02) / downside_dev if downside_dev > 0 else 0
            else:
                results['sortino_ratio'] = float('inf') if results['annualized_return'] > 0 else 0
        else:
            results['sortino_ratio'] = 0

        # V6.2.3: Kelly Criterion
        if self.trades:
            w = results['win_rate']
            if w >= 1.0:
                # 全部交易盈利：Kelly 最优下注比例 = 100%
                results['kelly'] = 1.0
            elif w <= 0.0:
                # 全部交易亏损：无下注价值
                results['kelly'] = -1.0
            else:
                avg_w = results['avg_profit'] / 100.0 if results.get('avg_profit') else 0
                avg_l = abs(results['avg_loss']) / 100.0 if results.get('avg_loss') else 0.01
                if avg_l > 0:
                    kelly = w - (1 - w) / (avg_w / avg_l) if avg_w > 0 else -1
                    results['kelly'] = kelly
                else:
                    results['kelly'] = 0
        else:
            results['kelly'] = 0

        # V6.2.3: Expectancy (期望收益 per trade)
        if self.trades:
            results['expectancy'] = (results['win_rate'] * results.get('avg_profit', 0) +
                                      (1 - results['win_rate']) * results.get('avg_loss', 0))
        else:
            results['expectancy'] = 0

        # V6.2.3: Average hold days (平均持仓天数)
        if self.trades:
            hold_days = []
            for t in self.trades:
                try:
                    ed = pd.Timestamp(t['entry_date'])
                    xd = pd.Timestamp(t['exit_date'])
                    hold_days.append((xd - ed).days)
                except:
                    pass
            results['avg_hold_days'] = np.mean(hold_days) if hold_days else 0
        else:
            results['avg_hold_days'] = 0

        return results

    def get_behavior_statistics(self):
        """行为统计"""
        stats = {}
        all_names = ['DoubleBottom', 'PanicSell', 'TrendPullback',
                     'BreakoutConfirm', 'MomentumExhaustion',
                     'TrendFailure', 'FalseBreak',
                     'RSI_Overbought', 'MA_DeathCross']  # V6.2.3 新增

        for name in all_names:
            count = self.behavior_counts.get(name, 0)
            trades_list = self.behavior_trades.get(name, [])
            avg_contrib = np.mean(trades_list) if trades_list else 0
            total_contrib = np.sum(trades_list) if trades_list else 0
            stats[name] = {
                'count': count,
                'trades': len(trades_list),
                'avg_contribution': avg_contrib,
                'total_contribution': total_contrib,
            }
        return stats

    def get_event_statistics(self):
        """V5.0: 事件统计"""
        return dict(self.event_statistics)

    def get_score_statistics(self):
        """评分统计"""
        if not self.score_history:
            return {}

        buy_scores = [s['buy_score'] for s in self.score_history]
        sell_scores = [s['sell_score'] for s in self.score_history]
        rewards = [s.get('reward', 0) for s in self.score_history]
        risks = [s.get('risk', 0) for s in self.score_history]

        return {
            'avg_buy_score': np.mean(buy_scores) if buy_scores else 0,
            'avg_sell_score': np.mean(sell_scores) if sell_scores else 0,
            'max_buy_score': max(buy_scores) if buy_scores else 0,
            'max_sell_score': max(sell_scores) if sell_scores else 0,
            'avg_reward': np.mean(rewards) if rewards else 0,
            'avg_risk': np.mean(risks) if risks else 0,
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

    # ============================================================
    # V6: Evidence 因子提取 & 文件输出
    # ============================================================

    @staticmethod
    def _extract_evidence_factors(evidence, score_breakdown):
        """
        从 Evidence Engine 输出中提取可读的促成因子

        Args:
            evidence: Evidence Engine 融合明细 dict
            score_breakdown: 评分子项 dict

        Returns:
            factors: 结构化的促成因子 dict
        """
        if not evidence:
            return {'note': '无Evidence Engine数据'}

        factors = {}

        # 证据源分解
        sources = evidence.get('sources', {})
        for src_name, src_info in sources.items():
            contrib = src_info.get('contribution', 0)
            if src_name == 'rule' and contrib != 0:
                factors['rule_confidence'] = {
                    'label': '人工规则置信度',
                    'raw': round(src_info.get('raw', 0), 1),
                    'weight': round(src_info.get('weight', 0), 2),
                    'contribution': round(contrib, 1),
                }
            elif src_name == 'replay' and contrib != 0:
                factors['replay_confidence'] = {
                    'label': 'Replay Learning 历史成功率修正',
                    'multiplier': round(src_info.get('multiplier', 1.0), 2),
                    'adjusted_confidence': round(src_info.get('adjusted_conf', 0), 1),
                    'weight': round(src_info.get('weight', 0), 2),
                    'contribution': round(contrib, 1),
                }
            elif src_name == 'ml' and contrib != 0:
                factors['ml_confidence'] = {
                    'label': 'ML 概率输出',
                    'raw_prob': round(src_info.get('raw_prob', 0.5), 3),
                    'confidence': round(src_info.get('confidence', 50), 1),
                    'weight': round(src_info.get('weight', 0), 2),
                    'contribution': round(contrib, 1),
                }
            elif src_name == 'emotion' and contrib != 0:
                factors['emotion_bonus'] = {
                    'label': '情绪修正（价格+情绪双确认）',
                    'bonus': round(src_info.get('bonus', 0), 1),
                    'weight': round(src_info.get('weight', 0), 2),
                    'contribution': round(contrib, 1),
                }

        # 总分
        factors['pre_decay'] = round(evidence.get('pre_decay', 0), 1)
        factors['time_decay_multiplier'] = round(evidence.get('decay_multiplier', 1.0), 3)
        factors['final_confidence'] = round(evidence.get('final', 0), 1)

        # 评分分解
        if score_breakdown:
            factors['score_components'] = {
                'behavior_score': round(score_breakdown.get('behavior_score', 0), 1),
                'confidence': round(score_breakdown.get('confidence', 0), 1),
                'reward_score': round(score_breakdown.get('reward_score', 0), 1),
                'regime_multiplier': round(score_breakdown.get('regime_mult', 1.0), 2),
                'final_score': round(score_breakdown.get('final_score', 0), 1),
            }

        # 证据来源摘要
        weights = evidence.get('weights_used', {})
        if weights:
            factors['evidence_weights'] = weights

        return factors

    def export_all(self, filepath=None, results=None):
        """
        导出完整回测记录到独立JSON文件（每次运行覆盖）

        包含：
        - 绩效汇总
        - 每笔交易完整明细（含证据链）
        - 每日信号（buy/sell score, regime, psychology, target_position）
        - 每日净值曲线
        - 评分历史

        Args:
            filepath: 输出文件路径，默认 runs\{code}\backtest_records.json
            results: 回测结果dict（可选，若不提供则自动计算）

        Returns:
            filepath: 输出文件路径，默认 runs\{code}\backtest_records.json
        """
        import json, os
        from datetime import datetime

        if filepath is None:
            filepath = _cfg.runs_path('backtest_records.json')

        if results is None:
            results = self._compute_results()

        # ---- 每日信号（精简关键字段）----
        daily_signals = []
        for s in self.daily_signals:
            daily_signals.append({
                'date': str(s.get('date', '')),
                'buy_score': s.get('buy_score', 0),
                'sell_score': s.get('sell_score', 0),
                'net_score': round(s.get('buy_score', 0) - s.get('sell_score', 0), 1),
                'regime': s.get('regime', 'Unknown'),
                'psychology': s.get('psychology', 'Unknown'),
                'reward_score': s.get('reward_score', 0),
                'risk_score': s.get('risk_score', 0),
                'target_position': round(s.get('target_position', 0), 4),
                'current_position': round(s.get('current_position', 0), 4),
                'buy_behaviors': s.get('buy_behaviors', []),
                'sell_behaviors': s.get('sell_behaviors', []),
                'executed': s.get('executed', False),
                'action': s.get('action', ''),
                'executed_price': s.get('executed_price'),
                'trade_value': s.get('trade_value', 0),
                # V7.0 P6.5: L3 风控动作 + 相位 + 中枢
                'risk_actions': s.get('risk_actions', []),
                'phase': s.get('phase', {}),
                'center': s.get('center'),
            })

        # ---- 每日净值 ----
        daily_equity = []
        for e in self.daily_equity:
            daily_equity.append({
                'date': str(e.get('date', '')),
                'equity': e.get('equity', 0),
            })

        # ---- 完整交易记录 ----
        trades_export = []
        for t in self.trades:
            trades_export.append(t)

        # ---- 评分历史 ----
        score_history_export = []
        for s in self.score_history:
            score_history_export.append({
                'date': str(s.get('date', '')),
                'buy_score': s.get('buy_score', 0),
                'sell_score': s.get('sell_score', 0),
                'regime': s.get('regime', 'Unknown'),
                'psychology': s.get('psychology', 'Unknown'),
                'emotion_score': s.get('emotion_score', 50),
                'reward': s.get('reward', 0),
                'risk': s.get('risk', 0),
            })

        # ---- 构建完整档案 ----
        archive = {
            'meta': {
                'version': 'V6.2.3',
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'backtest_start': str(self.df.index[0]),
                'backtest_end': str(self.df.index[-1]),
                'trading_days': len(daily_equity),
            },
            'performance': {
                'benchmark_return_pct': round(results.get('benchmark_return', 0) * 100, 2),
                'strategy_return_pct': round(results.get('strategy_return', 0) * 100, 2),
                'excess_return_pct': round(results.get('excess_return', 0) * 100, 2),
                'max_drawdown_pct': round(results.get('max_drawdown', 0) * 100, 2),
                'annualized_return_pct': round(results.get('annualized_return', 0) * 100, 2),
                'volatility_pct': round(results.get('volatility', 0) * 100, 2),
                'sharpe_ratio': round(results.get('sharpe_ratio', 0), 3),
                'sortino_ratio': round(results.get('sortino_ratio', 0), 3),
                'calmar_ratio': round(results.get('calmar_ratio', 0), 3),
                'profit_factor': round(results.get('profit_factor', 0), 2),
                'win_rate_pct': round(results.get('win_rate', 0) * 100, 1),
                'kelly': round(results.get('kelly', 0), 3),
                'expectancy_pct': round(results.get('expectancy', 0), 2),
                'total_trades': results.get('total_trades', 0),
                'winning_trades': results.get('winning_trades', 0),
                'losing_trades': results.get('losing_trades', 0),
                'avg_hold_days': round(results.get('avg_hold_days', 0), 1),
                'final_equity': round(results.get('final_equity', 0), 2),
                'max_consecutive_wins': results.get('max_consecutive_wins', 0),
                'max_consecutive_losses': results.get('max_consecutive_losses', 0),
            },
            'trades': trades_export,
            'daily_signals': daily_signals,
            'daily_equity': daily_equity,
            'score_history': score_history_export,
            'config_snapshot': {
                'initial_cash': self.initial_cash,
                'initial_position': self.initial_position,
                'max_position': _cfg.MAX_POSITION,
                'min_trade_value': _cfg.MIN_TRADE_VALUE,
                'min_hold_days': _cfg.MIN_HOLD_DAYS,
            },
        }

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(archive, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n[V6.2.3] 完整回测记录已导出: {filepath}")
        print(f"  {len(trades_export)} 笔交易, {len(daily_signals)} 条每日信号, {len(daily_equity)} 条净值记录")

        return filepath

    def export_trades(self, filepath=None, format='json'):
        """
        导出交易记录（兼容旧接口，内部调用 export_all）

        Args:
            filepath: 输出文件路径
            format: 'json' 或 'csv'
        """
        import json, os
        from datetime import datetime

        if filepath is None:
            filepath = _cfg.runs_path('trades_v6.json')

        # 如果是JSON格式，用完整导出
        if format == 'json':
            return self.export_all(filepath)

        # CSV 格式
        csv_path = filepath.replace('.json', '.csv')
        rows = []
        for t in self.trades:
            ef = t.get('entry_factors', {})
            row = {
                'trade_id': t.get('trade_id'),
                'entry_date': t.get('entry_date'),
                'exit_date': t.get('exit_date'),
                'pnl_pct': t.get('pnl_pct'),
                'pnl_label': t.get('pnl_label'),
                'entry_behavior': ', '.join(t.get('entry_behavior', [])),
                'entry_regime': t.get('entry_regime'),
                'entry_psychology': t.get('entry_psychology'),
                'entry_score': t.get('entry_score'),
                'pre_decay_conf': ef.get('pre_decay', 0),
                'time_decay': ef.get('time_decay_multiplier', 1),
                'final_conf': ef.get('final_confidence', 0),
            }
            rows.append(row)
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"\n[V6.2.3] 交易记录已导出: {csv_path}")

        return csv_path

    def print_trade_factors(self, trade_index=-1):
        """
        打印单笔交易的促成因子明细

        Args:
            trade_index: 交易索引，-1 = 最后一笔
        """
        if not self.trades:
            print("无交易记录")
            return

        trade = self.trades[trade_index]
        print("\n" + "=" * 70)
        print(f"交易 #{trade.get('trade_id', '?')} 促成因子明细")
        print("=" * 70)
        print(f"  日期: {trade.get('entry_date')} → {trade.get('exit_date')}")
        print(f"  价格: {trade.get('entry_price')} → {trade.get('exit_price')}")
        print(f"  收益: {trade.get('pnl_pct'):+.2f}%  [{trade.get('pnl_label')}]")
        print(f"  行为: {', '.join(trade.get('entry_behavior', []))}")
        print(f"  市场: {trade.get('entry_regime')} | 情绪: {trade.get('entry_psychology')} ({trade.get('entry_emotion_score')})")

        print(f"\n  ┌─ 买入促成因子 ─────────────────────────────")
        ef = trade.get('entry_factors', {})
        self._print_factor_group(ef, indent=4)

        xf = trade.get('exit_factors', {})
        if xf and 'reason' not in xf:
            print(f"\n  ┌─ 卖出促成因子 ─────────────────────────────")
            self._print_factor_group(xf, indent=4)

        print(f"  └{'─' * 50}")
        print()

    @staticmethod
    def _print_factor_group(factors, indent=4):
        """打印一组促成因子"""
        prefix = ' ' * indent
        for key, val in factors.items():
            if key == 'rule_confidence':
                print(f"{prefix}人工规则: raw={val['raw']} × w={val['weight']} → {val['contribution']:+.1f}")
            elif key == 'replay_confidence':
                print(f"{prefix}Replay学习: mult={val['multiplier']} adj={val['adjusted_confidence']} × w={val['weight']} → {val['contribution']:+.1f}")
            elif key == 'ml_confidence':
                print(f"{prefix}ML概率: prob={val['raw_prob']} conf={val['confidence']} × w={val['weight']} → {val['contribution']:+.1f}")
            elif key == 'emotion_bonus':
                print(f"{prefix}情绪修正: bonus={val['bonus']:+.1f} × w={val['weight']} → {val['contribution']:+.1f}")
            elif key == 'pre_decay':
                print(f"{prefix}衰减前置信度: {val}")
            elif key == 'time_decay_multiplier':
                print(f"{prefix}时间衰减乘数: {val}")
            elif key == 'final_confidence':
                print(f"{prefix}最终置信度: {val}")
            elif key == 'score_components':
                sc = val
                print(f"{prefix}评分组成: Behavior={sc['behavior_score']} + Confidence={sc['confidence']} + Reward={sc['reward_score']} × RegimeMult={sc['regime_multiplier']} = {sc['final_score']}")
            elif key == 'evidence_weights':
                pass  # 权重信息不单独打印
            elif key == 'note' or key == 'reason':
                print(f"{prefix}{val}")
