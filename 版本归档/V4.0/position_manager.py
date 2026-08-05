"""
V4.0 动态仓位管理模块（Position Manager）

第四层：根据评分动态调整仓位
- 不再固定 +20% / +50%
- 而是根据评分映射到目标仓位
- 考虑交易成本、滑点、最大仓位限制
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
        
        # 初始建仓（回测中使用）
        if initial_position > 0 and initial_cash > 0:
            self.position_pct = initial_position
            self.cash = initial_cash * (1 - initial_position)
    
    def set_initial_shares(self, price):
        """设置初始持仓份额"""
        if self.shares == 0 and self.position_pct > 0:
            self.shares = self.initial_cash * self.position_pct / price
            self.cash = self.initial_cash - self.shares * price
    
    def get_total_value(self, current_price):
        """获取当前总资产"""
        return self.cash + self.shares * current_price
    
    def execute_trade(self, target_position, current_price):
        """
        执行仓位调整
        
        Args:
            target_position: 目标仓位比例 (0.0 ~ MAX_POSITION)
            current_price: 当前价格
        
        Returns:
            trade_info: {
                'action': 'BUY' / 'SELL' / 'HOLD',
                'delta': 仓位变化比例,
                'shares_traded': 交易股数,
                'trade_value': 交易金额,
                'commission': 手续费,
                'slippage_cost': 滑点成本,
                'executed_price': 实际成交价,
            } 或 None（无交易）
        """
        current_value = self.get_total_value(current_price)
        target_value = current_value * target_position
        current_position_value = self.shares * current_price
        trade_value = target_value - current_position_value

        # 最小交易金额检查
        if abs(trade_value) < MIN_TRADE_VALUE:
            return None

        # 计算滑点后的成交价
        if trade_value > 0:  # 买入
            executed_price = current_price * (1 + SLIPPAGE)
        else:  # 卖出
            executed_price = current_price * (1 - SLIPPAGE)

        # 计算股数
        shares_traded = trade_value / executed_price
        
        # 佣金
        commission_amount = abs(trade_value) * COMMISSION
        slippage_cost = abs(trade_value) * SLIPPAGE

        old_position = self.position_pct
        old_shares = self.shares
        old_cash = self.cash

        # 更新持仓
        self.shares += shares_traded
        self.cash -= trade_value + commission_amount
        self.position_pct = target_position
        self.total_trades += 1

        # 修正浮点误差
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
        """获取当前持仓快照"""
        return {
            'cash': self.cash,
            'shares': self.shares,
            'position_pct': self.position_pct,
            'total_value': self.get_total_value(current_price),
        }
