"""
V7.0 L3 风控层（risk_guard.py）—— 纯函数风控模块
================================================
优先级高于一切评分信号，且不受 MIN_HOLD_DAYS / SCORE_HOLD_ZONE 限制。

机制（默认值进搜索空间，见 config）：
  1. 硬止损：浮亏 ≤ STOP_LOSS_PCT(-8%) → 仓位减半；≤ STOP_LOSS_HARD(-12%) → 清仓。
     触发后进入 STOP_LOSS_COOLDOWN_DAYS(180) 冷静期，期内不重复触发。
  2. 阶梯止盈：浮盈 ≥ TAKE_PROFIT_T1(+15%) → 减 1/3；≥ TAKE_PROFIT_T2(+25%) → 再减 1/3；
     距 60 日高点回撤 ≥ TRAIL_EXIT_DRAWDOWN(4%) → 清仓锁利（高位及时止盈主力机制）。
  3. 回撤熔断：策略净值自峰值回撤 ≥ DRAWDOWN_CIRCUIT(12%) → 仓位上限强制 CIRCUIT_BREAKER_CAP(20%)；
     解除条件：收盘站上 MA20 且连续 CIRCUIT_RELEASE_DAYS(5) 日确认。

接口：
  guard = RiskGuard()
  guard.on_entry(entry_price, entry_date)      # 建仓时调用
  guard.on_exit()                              # 平仓时调用（保留熔断状态）
  cap, actions = guard.evaluate(current_price, current_position, equity,
                                ma20, high_60d, date)
  # cap: float|None —— 仓位上限；None 表示不限制
  # actions: list[dict] —— 当日触发的风控动作（含类型/级别/描述）
"""
import pandas as pd

import config as _cfg


class RiskGuard:
    """L3 风控层：跨日状态 + 纯逻辑评估"""

    def __init__(self):
        self.entry_price = None          # 当前持仓成本
        self.entry_date = None           # 建仓日期
        self.peak_equity = None          # 策略净值峰值（熔断依据）
        self.circuit_breaker = False     # 是否处于熔断状态
        self.circuit_confirm_days = 0    # 熔断解除确认天数
        self.stop_loss_count = 0         # 止损触发累计次数
        self.stop_loss_last_date = None  # 最近一次止损触发日期（冷静期）
        self.last_actions = []           # 最近一次评估的动作

    # ---- 持仓生命周期 ----
    def on_entry(self, entry_price, entry_date=None):
        """建仓时记录持仓成本（清空止损/止盈状态，保留熔断状态）"""
        self.entry_price = entry_price
        self.entry_date = entry_date

    def on_exit(self):
        """平仓时清空持仓状态（保留熔断状态与净值峰值）"""
        self.entry_price = None
        self.entry_date = None

    # ---- 主评估 ----
    def evaluate(self, current_price, current_position, equity,
                 ma20=None, high_60d=None, date=None):
        """
        评估当日风控，返回 (cap, actions)

        Args:
            current_price: 当日收盘价
            current_position: 当前实际仓位（0~1）
            equity: 当日策略总资产（熔断依据）
            ma20: 当日 MA20（熔断解除条件）
            high_60d: 近 60 日最高价（高位回撤锁利依据）
            date: 当日日期（冷静期计算；None 则跳过冷静期检查）

        Returns:
            cap: float|None —— 仓位上限；None 表示不限制
            actions: list[dict] —— 触发的风控动作
        """
        cap = None
        actions = []

        # 更新净值峰值
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

        # ---- 1. 硬止损（仅持仓时）----
        if self.entry_price and current_price is not None and self.entry_price > 0:
            pnl_pct = (current_price - self.entry_price) / self.entry_price

            in_cooldown = False
            if self.stop_loss_last_date is not None and date is not None:
                days = (pd.Timestamp(date) - pd.Timestamp(self.stop_loss_last_date)).days
                in_cooldown = days < _cfg.STOP_LOSS_COOLDOWN_DAYS

            if not in_cooldown:
                if pnl_pct <= _cfg.STOP_LOSS_HARD:
                    cap = 0.0
                    self.stop_loss_count += 1
                    self.stop_loss_last_date = date
                    actions.append({
                        'type': 'stop_loss_hard', 'level': 'hard',
                        'pnl_pct': round(pnl_pct * 100, 2), 'cap': 0.0,
                        'desc': f'硬止损清仓（浮亏 {pnl_pct*100:.1f}% ≤ {_cfg.STOP_LOSS_HARD*100:.0f}%）',
                    })
                elif pnl_pct <= _cfg.STOP_LOSS_PCT:
                    half_cap = current_position * 0.5
                    cap = half_cap
                    self.stop_loss_count += 1
                    self.stop_loss_last_date = date
                    actions.append({
                        'type': 'stop_loss_half', 'level': 'soft',
                        'pnl_pct': round(pnl_pct * 100, 2), 'cap': round(half_cap, 4),
                        'desc': f'止损减半（浮亏 {pnl_pct*100:.1f}% ≤ {_cfg.STOP_LOSS_PCT*100:.0f}%）',
                    })

        # ---- 2. 阶梯止盈（仅持仓时）----
        if self.entry_price and current_price is not None and self.entry_price > 0:
            pnl_pct = (current_price - self.entry_price) / self.entry_price

            if pnl_pct >= _cfg.TAKE_PROFIT_T2:
                tp_cap = current_position * (1.0 / 3.0)   # 累计减 2/3
                cap = min(cap, tp_cap) if cap is not None else tp_cap
                actions.append({
                    'type': 'take_profit_t2', 'level': 't2',
                    'pnl_pct': round(pnl_pct * 100, 2), 'cap': round(tp_cap, 4),
                    'desc': f'阶梯止盈T2（浮盈 {pnl_pct*100:.1f}% ≥ {_cfg.TAKE_PROFIT_T2*100:.0f}%，再减1/3）',
                })
            elif pnl_pct >= _cfg.TAKE_PROFIT_T1:
                tp_cap = current_position * (2.0 / 3.0)   # 减 1/3
                cap = min(cap, tp_cap) if cap is not None else tp_cap
                actions.append({
                    'type': 'take_profit_t1', 'level': 't1',
                    'pnl_pct': round(pnl_pct * 100, 2), 'cap': round(tp_cap, 4),
                    'desc': f'阶梯止盈T1（浮盈 {pnl_pct*100:.1f}% ≥ {_cfg.TAKE_PROFIT_T1*100:.0f}%，减1/3）',
                })

            # 距 60 日高点回撤 → 清仓锁利
            if high_60d and high_60d > 0:
                dist_from_high = (high_60d - current_price) / high_60d
                if dist_from_high >= _cfg.TRAIL_EXIT_DRAWDOWN:
                    cap = 0.0
                    actions.append({
                        'type': 'trail_exit', 'level': 'trail',
                        'dist_from_high': round(dist_from_high * 100, 2), 'cap': 0.0,
                        'desc': f'高位回撤锁利（距60日高点 {dist_from_high*100:.1f}% ≥ {_cfg.TRAIL_EXIT_DRAWDOWN*100:.0f}%，清仓）',
                    })

        # ---- 3. 回撤熔断 ----
        if self.peak_equity and self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity
            if dd >= _cfg.DRAWDOWN_CIRCUIT:
                if not self.circuit_breaker:
                    self.circuit_breaker = True
                    self.circuit_confirm_days = 0
                    actions.append({
                        'type': 'circuit_breaker', 'level': 'circuit',
                        'drawdown': round(dd * 100, 2), 'cap': _cfg.CIRCUIT_BREAKER_CAP,
                        'desc': f'回撤熔断（净值回撤 {dd*100:.1f}% ≥ {_cfg.DRAWDOWN_CIRCUIT*100:.0f}%，仓位上限 {_cfg.CIRCUIT_BREAKER_CAP*100:.0f}%）',
                    })
                cap = min(cap, _cfg.CIRCUIT_BREAKER_CAP) if cap is not None else _cfg.CIRCUIT_BREAKER_CAP
            elif self.circuit_breaker:
                # 熔断解除：收盘站上 MA20 且连续 N 日确认
                above_ma20 = ma20 is not None and current_price > ma20
                if above_ma20:
                    self.circuit_confirm_days += 1
                    if self.circuit_confirm_days >= _cfg.CIRCUIT_RELEASE_DAYS:
                        self.circuit_breaker = False
                        self.circuit_confirm_days = 0
                        actions.append({
                            'type': 'circuit_release', 'level': 'release',
                            'desc': f'熔断解除（收盘站上MA20连续{_cfg.CIRCUIT_RELEASE_DAYS}日）',
                        })
                else:
                    self.circuit_confirm_days = 0
            else:
                self.circuit_confirm_days = 0

        self.last_actions = actions
        return cap, actions
