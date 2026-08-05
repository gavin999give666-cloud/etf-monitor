"""
V5.0 仓位管理模块
"""
from config import MAX_POSITION, MIN_TRADE_VALUE, SLIPPAGE, COMMISSION


class PositionManager:
    """动态仓位管理器"""

    def __init__(self, initial_cash, initial_position=0.60):
        self.cash = initial_cash
        self.shares = 0
        self.position_pct = 0.0
        self.initial_cash = initial_cash
        self.total_trades = 0

        if initial_position > 0 and initial_cash > 0:
            self.position_pct = initial_position
            self.cash = initial_cash * (1 - initial_position)

    def set_initial_shares(self, price):
        if self.shares == 0 and self.position_pct > 0:
            self.shares = self.initial_cash * self.position_pct / price
            self.cash = self.initial_cash - self.shares * price

    def get_total_value(self, current_price):
        return self.cash + self.shares * current_price

    def execute_trade(self, target_position, current_price):
        current_value = self.get_total_value(current_price)
        target_value = current_value * target_position
        current_position_value = self.shares * current_price
        trade_value = target_value - current_position_value

        if abs(trade_value) < MIN_TRADE_VALUE:
            return None

        if trade_value > 0:
            executed_price = current_price * (1 + SLIPPAGE)
        else:
            executed_price = current_price * (1 - SLIPPAGE)

        shares_traded = trade_value / executed_price
        commission_amount = abs(trade_value) * COMMISSION
        slippage_cost = abs(trade_value) * SLIPPAGE

        old_position = self.position_pct

        self.shares += shares_traded
        self.cash -= trade_value + commission_amount
        self.position_pct = target_position
        self.total_trades += 1

        self.position_pct = max(0.0, min(MAX_POSITION, self.position_pct))

        action = 'BUY' if trade_value > 0 else 'SELL'
        delta = abs(target_position - old_position)

        return {
            'action': action,
            'target_position': target_position,
            'delta': delta,
            'shares_traded': abs(shares_traded),
            'trade_value': abs(trade_value),
            'commission': commission_amount,
            'slippage_cost': slippage_cost,
            'executed_price': executed_price,
            'new_cash': self.cash,
            'new_shares': self.shares,
        }

    def get_snapshot(self, current_price):
        return {
            'cash': self.cash,
            'shares': self.shares,
            'position_pct': self.position_pct,
            'total_value': self.get_total_value(current_price),
        }
