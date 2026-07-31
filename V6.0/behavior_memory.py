"""
V6.0 Replay Learning —— 行为成功率统计（Replay Confidence）
============================================================

核心理念：
- 不需要任何ML模型，纯统计就能让系统开始"学习"
- 每次交易完成后记录 (Regime, BehaviorType) → Success/Failure
- 累积后得到 Behavior Success Table
- 自动更新行为在不同市场环境下的置信度修正系数

示例：
    Bull + DoubleBottom: 12次交易，11次成功 → 成功率91.7% → Confidence × 1.15
    Bear + DoubleBottom: 13次交易，5次成功  → 成功率38.5% → Confidence × 0.80

设计原则：
- 最小样本量 = 5（冷启动用Rule Confidence兜底）
- 滑动窗口 = 最近90个交易日
- 连续3次失败 → 紧急降权
- 30日无新样本 → 回归中性
"""

import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta


class BehaviorMemory:
    """
    行为记忆库

    记录每个 (Regime, BehaviorType) 组合的历史表现，
    提供基于统计的置信度修正系数。
    """

    def __init__(self, window_days=90, min_samples=5, max_age_days=30):
        """
        Args:
            window_days: 滑动窗口大小（交易日）
            min_samples: 最小样本量，低于此值使用中性系数
            max_age_days: 超过此天数无新样本，回归中性
        """
        self.window_days = window_days
        self.min_samples = min_samples
        self.max_age_days = max_age_days

        # 核心数据结构: (regime, behavior) → list of (date, success_bool)
        self.memory = defaultdict(list)

        # 紧急降权标记: (regime, behavior) → 连续失败次数
        self.consecutive_failures = defaultdict(int)

        # 最近更新日期: (regime, behavior) → last_date
        self.last_update = {}

    def record_trade(self, regime, behavior_name, success, date):
        """
        记录一笔交易的结果

        Args:
            regime: 市场状态 ('Bull', 'Bear', 'Range', 'Unknown')
            behavior_name: 行为名称 ('DoubleBottom', 'TrendPullback', ...)
            success: 是否盈利
            date: 交易日期
        """
        key = (regime, behavior_name)
        self.memory[key].append((date, success))
        self.last_update[key] = date

        # 更新连续失败计数
        if success:
            self.consecutive_failures[key] = 0
        else:
            self.consecutive_failures[key] += 1

        # 清理过期记录
        self._prune_old_records()

    def record_trades_batch(self, trades):
        """
        批量记录交易结果（用于回测初始化）

        Args:
            trades: list of dict, 每个包含:
                - 'entry_regime': str
                - 'entry_reason': list[str] (行为名称列表)
                - 'pnl_pct': float
                - 'entry_date': datetime
        """
        for trade in trades:
            regime = trade.get('entry_regime', 'Unknown')
            pnl = trade.get('pnl_pct', 0)
            success = pnl > 0
            date = trade.get('entry_date')

            for behavior in trade.get('entry_reason', []):
                self.record_trade(regime, behavior, success, date)

    def get_success_rate(self, regime, behavior_name):
        """
        获取指定 (Regime, Behavior) 组合的历史成功率

        Returns:
            (rate, sample_count): 成功率 (0-1) 和样本量
        """
        key = (regime, behavior_name)
        records = self.memory.get(key, [])

        if len(records) < self.min_samples:
            return None, len(records)  # 样本不足，返回None

        successes = sum(1 for _, s in records if s)
        rate = successes / len(records)
        return rate, len(records)

    def get_confidence_multiplier(self, regime, behavior_name, current_date=None):
        """
        获取基于历史成功率的置信度修正系数

        逻辑：
        - 成功率 > 70% → 系数 1.15
        - 成功率 < 40% → 系数 0.80
        - 40% ≤ 成功率 ≤ 70% → 线性插值
        - 样本不足 → 系数 1.0（中性）
        - 连续3次失败 → 系数 0.70（紧急降权）
        - 超过30天无新样本 → 回归1.0

        Returns:
            multiplier: float (0.5 ~ 1.5)
        """
        key = (regime, behavior_name)
        rate, count = self.get_success_rate(regime, behavior_name)

        # 样本不足 → 中性
        if rate is None:
            return 1.0

        # 超过max_age_days无新样本 → 回归中性
        if current_date is not None and key in self.last_update:
            days_since = (current_date - self.last_update[key]).days
            if days_since > self.max_age_days:
                return 1.0

        # 紧急降权：连续3次失败
        if self.consecutive_failures.get(key, 0) >= 3:
            return 0.70

        # 基于成功率的系数映射
        if rate >= 0.70:
            multiplier = 1.0 + (rate - 0.70) * 0.5  # 0.70→1.0, 0.90→1.10, 1.0→1.15
            return min(1.20, multiplier)
        elif rate < 0.40:
            multiplier = 1.0 - (0.40 - rate) * 0.5  # 0.40→1.0, 0.30→0.95, 0.20→0.90
            return max(0.50, multiplier)
        else:
            # 0.40 ~ 0.70: 线性从0.85到1.0
            multiplier = 0.85 + (rate - 0.40) / 0.30 * 0.15
            return multiplier

    def get_full_stats(self):
        """
        获取完整的行为成功率统计表

        Returns:
            list of dict: [{'regime': ..., 'behavior': ..., 'total': ..., 'success': ..., 'rate': ...}, ...]
        """
        stats = []
        for (regime, behavior), records in self.memory.items():
            total = len(records)
            if total == 0:
                continue
            success = sum(1 for _, s in records if s)
            rate = success / total
            consecutive = self.consecutive_failures.get((regime, behavior), 0)
            stats.append({
                'regime': regime,
                'behavior': behavior,
                'total': total,
                'success': success,
                'rate': round(rate, 3),
                'consecutive_failures': consecutive,
                'multiplier': round(self.get_confidence_multiplier(regime, behavior), 3),
            })
        # 按成功率降序排列
        stats.sort(key=lambda x: x['rate'], reverse=True)
        return stats

    def print_stats(self):
        """打印行为成功率统计表"""
        stats = self.get_full_stats()
        if not stats:
            print("行为记忆库为空，无统计数据。")
            return

        header = f"{'Regime':>8} {'Behavior':<20} {'Total':>6} {'Success':>8} {'Rate':>8} {'Mult':>6}"
        print("=" * len(header))
        print("Behavior Success Rate Statistics (Replay Learning)")
        print("=" * len(header))
        print(header)
        print("-" * len(header))

        for s in stats:
            line = (
                f"{s['regime']:>8} "
                f"{s['behavior']:<20} "
                f"{s['total']:>6} "
                f"{s['success']:>8} "
                f"{s['rate']:>7.1%} "
                f"{s['multiplier']:>6.2f}"
            )
            print(line)
        print("-" * len(header))

    def _prune_old_records(self):
        """清理超出滑动窗口的过期记录"""
        if not self.memory:
            return

        # 找到最近的日期
        all_dates = []
        for records in self.memory.values():
            for date, _ in records:
                if hasattr(date, 'date'):
                    all_dates.append(date.date())
                elif isinstance(date, datetime):
                    all_dates.append(date.date())
                else:
                    all_dates.append(date)

        if not all_dates:
            return

        latest_date = max(all_dates)
        cutoff = latest_date - timedelta(days=self.window_days * 2)  # 日历日≈2倍交易日

        for key in list(self.memory.keys()):
            self.memory[key] = [
                (d, s) for d, s in self.memory[key]
                if (d.date() if hasattr(d, 'date') else d) >= cutoff
            ]
            if not self.memory[key]:
                del self.memory[key]
                if key in self.last_update:
                    del self.last_update[key]
                if key in self.consecutive_failures:
                    del self.consecutive_failures[key]

    def to_dict(self):
        """序列化为可保存的字典"""
        data = {}
        for (regime, behavior), records in self.memory.items():
            key = f"{regime}|{behavior}"
            data[key] = [
                {'date': str(d), 'success': s}
                for d, s in records
            ]
        return {
            'memory': data,
            'consecutive_failures': dict(self.consecutive_failures),
            'last_update': {k: str(v) for k, v in self.last_update.items()},
        }

    @classmethod
    def from_dict(cls, data):
        """从字典反序列化"""
        obj = cls()
        memory_data = data.get('memory', {})
        for key, records in memory_data.items():
            regime, behavior = key.split('|')
            obj.memory[(regime, behavior)] = [
                (r['date'], r['success']) for r in records
            ]
        obj.consecutive_failures = defaultdict(
            int, data.get('consecutive_failures', {})
        )
        obj.last_update = data.get('last_update', {})
        return obj


# ============================================================
# Time Decay 模块
# ============================================================

class TimeDecay:
    """
    观察期置信度时间衰减

    解决V5问题：Candidate在Observation状态超过一定天数后，
    置信度不再变化，但市场环境已经改变。

    衰减规则：
    - 前N天（GRACE_PERIOD）不衰减
    - 之后每天按比例衰减
    - 衰减到阈值以下 → 标记为过期
    """

    # 默认参数
    GRACE_PERIOD = 5          # 前5天不衰减（宽限期）
    DECAY_RATE = 0.05         # 每天衰减5%的当前置信度
    MIN_CONFIDENCE = 25       # 衰减到此值以下 → 过期

    def __init__(self, grace_period=None, decay_rate=None, min_confidence=None):
        self.grace_period = grace_period or self.GRACE_PERIOD
        self.decay_rate = decay_rate or self.DECAY_RATE
        self.min_confidence = min_confidence or self.MIN_CONFIDENCE

    def compute_decay(self, days_in_observation, current_confidence):
        """
        计算时间衰减后的置信度

        Args:
            days_in_observation: 在观察状态的天数
            current_confidence: 当前置信度

        Returns:
            (decayed_confidence, is_expired): 衰减后的置信度和是否过期
        """
        if days_in_observation <= self.grace_period:
            return current_confidence, False

        # 超出宽限期，逐日衰减
        decay_days = days_in_observation - self.grace_period
        decayed = current_confidence

        for _ in range(decay_days):
            decayed -= decayed * self.decay_rate

        # 检查是否过期
        is_expired = decayed < self.min_confidence
        return max(0, decayed), is_expired

    def compute_multiplier(self, days_in_observation):
        """
        计算时间衰减乘数（用于Evidence Engine融合）

        返回: 0.0 ~ 1.0，表示置信度保留比例
        """
        if days_in_observation <= self.grace_period:
            return 1.0

        decay_days = days_in_observation - self.grace_period
        multiplier = (1 - self.decay_rate) ** decay_days
        return max(0.1, multiplier)


if __name__ == "__main__":
    # 演示
    bm = BehaviorMemory(window_days=90, min_samples=3)

    # 模拟一些交易
    import random
    random.seed(42)

    for i in range(30):
        regime = random.choice(['Bull', 'Bear', 'Range'])
        behavior = random.choice(['DoubleBottom', 'TrendPullback', 'BreakoutConfirm'])
        success = random.random() < 0.55
        date = datetime(2026, 1, 1) + timedelta(days=i)
        bm.record_trade(regime, behavior, success, date)

    bm.print_stats()

    # Time Decay 演示
    td = TimeDecay()
    print("\nTime Decay 演示:")
    for days in [1, 3, 5, 7, 10, 15, 20]:
        conf, expired = td.compute_decay(days, 70)
        mult = td.compute_multiplier(days)
        print(f"  观察{days:>2}天: 置信度 {conf:.0f}, 乘数 {mult:.2f}, {'已过期' if expired else '有效'}")
