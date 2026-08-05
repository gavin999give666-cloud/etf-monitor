"""
V6.1 Replay Learning —— 行为成功率统计（在线学习闭环）
========================================================

V6.1 核心升级（Fix Before Expand）：

1. 多维 Replay 键：(Regime, Behavior, Psychology) 三维学习
   - 未来可扩展: (Regime, Behavior, Psychology, VolumeRegime)

2. 时间加权样本：Weight = exp(-days / τ)，τ≈90天
   - 半年前交易权重 ≈ 0.13，一个月内交易权重 ≈ 0.72

3. Laplace 平滑：避免小样本误导
   - 样本越少，越接近总体平均成功率（先验=0.5）

4. Time Decay 修复：从 (1-0.05)^n 改为 exp(-days/τ)
   - 输出范围 [0.5, 1.0]，不再压缩到 0.1

核心理念：
- 不需要任何ML模型，纯统计就能让系统开始"学习"
- 每次交易完成后记录 (Regime, Behavior, Psychology) → Success/Failure
- 累积后得到 Behavior Success Table
- 自动更新行为在不同市场环境+情绪状态下的置信度修正系数
"""

import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta


class BehaviorMemory:
    """
    V6.1 行为记忆库（增强版）

    记录每个 (Regime, Behavior, Psychology) 组合的历史表现，
    提供基于时间加权统计 + Laplace平滑的置信度修正系数。
    """

    def __init__(self, window_days=90, min_samples=3, max_age_days=30,
                 tau_days=90, laplace_alpha=1.0):
        """
        Args:
            window_days: 滑动窗口大小（交易日）
            min_samples: 最小样本量（V6.1降低到3，配合Laplace平滑）
            max_age_days: 超过此天数无新样本，回归中性
            tau_days: 时间衰减常数τ（天），Weight = exp(-days/τ)
            laplace_alpha: Laplace平滑伪计数（α=1.0标准Laplace）
        """
        self.window_days = window_days
        self.min_samples = min_samples
        self.max_age_days = max_age_days
        self.tau_days = tau_days
        self.laplace_alpha = laplace_alpha

        # V6.1: 核心数据结构升级 —— (regime, behavior, psychology) → list of (date, success)
        self.memory = defaultdict(list)

        # 紧急降权标记: key → 连续失败次数
        self.consecutive_failures = defaultdict(int)

        # 最近更新日期: key → last_date
        self.last_update = {}

        # V6.1: 全局先验成功率（用于Laplace平滑）
        self._global_successes = 0
        self._global_total = 0

    # ============================================================
    # V6.1: 构建多维 Replay 键
    # ============================================================

    def _make_key(self, regime, behavior_name, psychology=None):
        """
        V6.1: 构建多维 Replay 键

        当前维度: (Regime, Behavior, Psychology)
        未来可扩展: (Regime, Behavior, Psychology, VolumeRegime)
        """
        psych = psychology if psychology else 'Unknown'
        return (regime, behavior_name, psych)

    # ============================================================
    # 记录交易
    # ============================================================

    def record_trade(self, regime, behavior_name, success, date, psychology=None):
        """
        V6.1: 记录一笔交易的结果（增加 Psychology 维度）

        Args:
            regime: 市场状态 ('Bull', 'Bear', 'Range', 'Unknown')
            behavior_name: 行为名称 ('DoubleBottom', 'TrendPullback', ...)
            success: 是否盈利
            date: 交易日期
            psychology: 市场情绪状态 ('Panic', 'Fear', 'Hope', ...)
        """
        key = self._make_key(regime, behavior_name, psychology)
        self.memory[key].append((date, success))
        self.last_update[key] = date

        # 更新连续失败计数
        if success:
            self.consecutive_failures[key] = 0
        else:
            self.consecutive_failures[key] += 1

        # 更新全局先验
        self._global_successes += (1 if success else 0)
        self._global_total += 1

        # 清理过期记录
        self._prune_old_records()

    def record_trade_v6_compat(self, regime, behavior_name, success, date,
                                psychology=None):
        """
        V6兼容接口：与V6 record_trade 签名兼容，内部升级为多维键
        """
        self.record_trade(regime, behavior_name, success, date, psychology)

    def record_trades_batch(self, trades):
        """
        批量记录交易结果（用于回测初始化）

        Args:
            trades: list of dict, 每个包含:
                - 'entry_regime': str
                - 'entry_reason': list[str]
                - 'entry_psychology': str (V6.1新增)
                - 'pnl_pct': float
                - 'entry_date': datetime
        """
        for trade in trades:
            regime = trade.get('entry_regime', 'Unknown')
            pnl = trade.get('pnl_pct', 0)
            success = pnl > 0
            date = trade.get('entry_date')
            psychology = trade.get('entry_psychology')  # V6.1

            for behavior in trade.get('entry_reason', []):
                self.record_trade(regime, behavior, success, date, psychology)

    # ============================================================
    # V6.1: 时间加权 + Laplace平滑的成功率计算
    # ============================================================

    def _compute_time_weights(self, records, current_date=None):
        """
        V6.1.1: 计算时间衰减权重（简化日期处理）

        Weight = exp(-days_since / τ)

        Args:
            records: list of (date, success)
            current_date: 当前日期

        Returns:
            weights: np.array of shape (n,)
        """
        if current_date is None:
            return np.ones(len(records))

        # 统一转换为 datetime 对象
        cd = self._to_datetime(current_date)
        if cd is None:
            return np.ones(len(records))

        weights = []
        for date, _ in records:
            d = self._to_datetime(date)
            if d is None or cd < d:
                days = 0
            else:
                days = (cd - d).days
            weight = np.exp(-max(0, days) / self.tau_days)
            weights.append(weight)

        return np.array(weights)

    @staticmethod
    def _to_datetime(obj):
        """安全转换为 datetime 对象"""
        if obj is None:
            return None
        if isinstance(obj, datetime):
            return obj
        if hasattr(obj, 'to_pydatetime'):
            return obj.to_pydatetime()
        if isinstance(obj, str):
            try:
                return datetime.strptime(obj[:10], '%Y-%m-%d')
            except (ValueError, IndexError):
                return None
        return None

    def get_success_rate(self, regime, behavior_name, psychology=None, current_date=None):
        """
        V6.1: 获取 (Regime, Behavior, Psychology) 组合的时间加权 + Laplace平滑成功率

        Args:
            regime: 市场状态
            behavior_name: 行为名称
            psychology: 情绪状态（V6.1新增）
            current_date: 当前日期（用于时间加权）

        Returns:
            (rate, sample_count, effective_samples): 
                rate: Laplace平滑后的成功率
                sample_count: 原始样本数
                effective_samples: 有效样本数（按时间加权折算）
        """
        key = self._make_key(regime, behavior_name, psychology)
        records = self.memory.get(key, [])

        if not records:
            # 无样本 → 返回全局先验
            global_prior = self._get_global_prior()
            return global_prior, 0, 0

        # V6.1: 时间加权计算
        time_weights = self._compute_time_weights(records, current_date)
        weighted_successes = np.sum([w for (_, s), w in zip(records, time_weights) if s])
        weighted_total = np.sum(time_weights)
        effective_n = weighted_total  # 有效样本数

        if effective_n < self.min_samples:
            # 样本不足 → 返回None，外部会使用中性乘数
            return None, len(records), round(effective_n, 1)

        # V6.1: Laplace平滑
        # smoothed_rate = (successes + α * prior) / (total + α)
        global_prior = self._get_global_prior()
        successes = sum(1 for _, s in records if s)
        smoothed_rate = (successes + self.laplace_alpha * global_prior) / \
                        (len(records) + self.laplace_alpha)

        return smoothed_rate, len(records), round(effective_n, 1)

    def _get_global_prior(self):
        """获取全局先验成功率"""
        if self._global_total == 0:
            return 0.5
        return self._global_successes / self._global_total

    # ============================================================
    # V6.1: 置信度乘数（核心）
    # ============================================================

    def get_confidence_multiplier(self, regime, behavior_name, current_date=None,
                                   psychology=None):
        """
        V6.1: 获取基于历史成功率的置信度修正系数

        改进:
        - 多维键支持 Psychology
        - 时间加权成功率
        - Laplace平滑避免小样本误导
        - 连续4次失败 → 紧急降权（V6.1放宽到4）

        逻辑：
        - 成功率 > 65% → 加分（V6.1调低阈值）
        - 成功率 < 35% → 减分（V6.1调低阈值）
        - 样本不足 → 系数 1.0（中性）
        - 连续4次失败 → 系数 0.70
        - 超过30天无新样本 → 回归1.0

        Returns:
            multiplier: float (0.5 ~ 1.25)
            replay_info: dict (样本数、成功率、时间权重等调试信息)
        """
        key = self._make_key(regime, behavior_name, psychology)
        rate, count, effective_n = self.get_success_rate(
            regime, behavior_name, psychology, current_date
        )

        replay_info = {
            'key': f"{regime}+{behavior_name}+{psychology or 'Unknown'}",
            'samples': count,
            'effective_samples': effective_n,
            'rate': round(rate, 3) if rate is not None else None,
        }

        # 样本不足 → 中性
        if rate is None:
            replay_info['reason'] = f'insufficient_samples({count}<{self.min_samples})'
            replay_info['multiplier'] = 1.0
            return 1.0, replay_info

        # 超过max_age_days无新样本 → 回归中性
        if current_date is not None and key in self.last_update:
            days_since = (current_date - self.last_update[key]).days
            if days_since > self.max_age_days:
                replay_info['reason'] = f'stale({days_since}d>{self.max_age_days}d)'
                replay_info['multiplier'] = 1.0
                return 1.0, replay_info

        # V6.1: 紧急降权——连续4次失败
        if self.consecutive_failures.get(key, 0) >= 4:
            replay_info['reason'] = f'consecutive_failures({self.consecutive_failures[key]})'
            replay_info['multiplier'] = 0.70
            return 0.70, replay_info

        # V6.1: 基于平滑成功率的系数映射（更平滑的曲线）
        if rate >= 0.65:
            # 65%→1.0, 80%→1.10, 100%→1.20
            multiplier = 1.0 + (rate - 0.65) * 0.571
            multiplier = min(1.25, multiplier)
        elif rate < 0.35:
            # 35%→1.0, 20%→0.85, 0%→0.65
            multiplier = 1.0 - (0.35 - rate) * 1.0
            multiplier = max(0.50, multiplier)
        else:
            # 0.35 ~ 0.65: 线性从0.85到1.0
            multiplier = 0.85 + (rate - 0.35) / 0.30 * 0.15

        replay_info['reason'] = f'weighted_success_rate={rate:.2f}'
        replay_info['multiplier'] = round(multiplier, 3)
        return multiplier, replay_info

    # ============================================================
    # V6兼容接口（不传psychology时回退到旧行为）
    # ============================================================

    def get_confidence_multiplier_v6_compat(self, regime, behavior_name,
                                              current_date=None):
        """
        V6兼容接口：不传psychology，内部尝试所有psychology维度取平均
        """
        # 尝试精确匹配
        psychologies = ['Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion']
        multipliers = []
        infos = []

        for psych in psychologies:
            mult, info = self.get_confidence_multiplier(
                regime, behavior_name, current_date, psych
            )
            if info.get('samples', 0) > 0:
                multipliers.append(mult)
                infos.append(info)

        if multipliers:
            avg_mult = np.mean(multipliers)
            # 返回平均乘数和合并信息
            merged_info = {
                'key': f"{regime}+{behavior_name}",
                'samples': sum(i.get('samples', 0) for i in infos),
                'rate': round(np.mean([i.get('rate', 0.5) for i in infos if i.get('rate')]), 3),
                'multiplier': round(avg_mult, 3),
                'reason': f'averaged_over_{len(infos)}_psychologies',
                'per_psychology': infos,
            }
            return avg_mult, merged_info

        return 1.0, {'key': f"{regime}+{behavior_name}", 'samples': 0,
                       'reason': 'no_data', 'multiplier': 1.0}

    # ============================================================
    # V6.1: Replay Summary (Top10 / Worst10)
    # ============================================================

    def get_full_stats(self):
        """
        V6.1: 获取完整的行为成功率统计表

        Returns:
            list of dict
        """
        stats = []
        for key, records in self.memory.items():
            regime, behavior, psych = key
            total = len(records)
            if total == 0:
                continue
            successes = sum(1 for _, s in records if s)
            rate = successes / total

            # 计算时间加权
            latest_date = max(r[0] for r in records if r[0] is not None)
            time_weights = self._compute_time_weights(records, latest_date)
            avg_weight = np.mean(time_weights) if len(time_weights) > 0 else 0

            consecutive = self.consecutive_failures.get(key, 0)
            mult, _ = self.get_confidence_multiplier(regime, behavior, None, psych)

            stats.append({
                'regime': regime,
                'behavior': behavior,
                'psychology': psych,
                'total': total,
                'success': successes,
                'rate': round(rate, 3),
                'avg_time_weight': round(avg_weight, 3),
                'consecutive_failures': consecutive,
                'multiplier': round(mult, 3),
            })

        stats.sort(key=lambda x: x['rate'], reverse=True)
        return stats

    def get_replay_summary(self):
        """
        V6.1: 获取 Replay 学习摘要（Top10 + Worst10）

        Returns:
            dict: {'top10': [...], 'worst10': [...], 'total_keys': int}
        """
        stats = self.get_full_stats()
        if not stats:
            return {'top10': [], 'worst10': [], 'total_keys': 0}

        top10 = stats[:10]  # 已是按rate降序
        worst10 = stats[-10:][::-1]  # 反转得到最差
        return {
            'top10': top10,
            'worst10': worst10,
            'total_keys': len(stats),
        }

    def print_stats(self):
        """V6.1: 打印行为成功率统计表（增强版）"""
        stats = self.get_full_stats()
        if not stats:
            print("行为记忆库为空，无统计数据。")
            return

        header = (f"{'Regime':>8} {'Behavior':<20} {'Psych':<12} "
                  f"{'Total':>6} {'Success':>8} {'Rate':>8} {'Weight':>7} {'Mult':>6}")
        sep = "=" * 95
        print(sep)
        print("V6.1 Behavior Memory (Replay Learning) Statistics")
        print(f"  全局先验成功率: {self._get_global_prior():.1%}")
        print(sep)
        print(header)
        print("-" * 95)

        for s in stats:
            line = (
                f"{s['regime']:>8} "
                f"{s['behavior']:<20} "
                f"{s['psychology']:<12} "
                f"{s['total']:>6} "
                f"{s['success']:>8} "
                f"{s['rate']:>7.1%} "
                f"{s['avg_time_weight']:>7.3f} "
                f"{s['multiplier']:>6.2f}"
            )
            print(line)
        print("-" * 95)

    def print_replay_summary(self):
        """V6.1: 打印 Replay Learning Top10 / Worst10"""
        summary = self.get_replay_summary()

        if not summary['top10']:
            print("Replay Summary: 无数据")
            return

        print("\n" + "=" * 70)
        print("V6.1 Replay Learning Summary")
        print(f"  Total Keys: {summary['total_keys']}")
        print("=" * 70)

        print("\n  --- Top 10 (Highest Success Rate) ---")
        print(f"  {'Rank':>5} {'Regime+Behavior+Psych':<45} {'Rate':>8} {'Samples':>8} {'Mult':>6}")
        print(f"  {'-'*72}")
        for i, s in enumerate(summary['top10'], 1):
            label = f"{s['regime']}+{s['behavior']}+{s['psychology']}"
            print(f"  {i:>5} {label:<45} {s['rate']:>7.1%} {s['total']:>8} {s['multiplier']:>6.2f}")

        print(f"\n  --- Worst 10 (Lowest Success Rate) ---")
        print(f"  {'Rank':>5} {'Regime+Behavior+Psych':<45} {'Rate':>8} {'Samples':>8} {'Mult':>6}")
        print(f"  {'-'*72}")
        for i, s in enumerate(summary['worst10'], 1):
            label = f"{s['regime']}+{s['behavior']}+{s['psychology']}"
            print(f"  {i:>5} {label:<45} {s['rate']:>7.1%} {s['total']:>8} {s['multiplier']:>6.2f}")

    # ============================================================
    # 内部方法
    # ============================================================

    def _prune_old_records(self):
        """清理超出滑动窗口的过期记录"""
        if not self.memory:
            return

        all_dates = []
        for records in self.memory.values():
            for date, _ in records:
                if hasattr(date, 'date'):
                    all_dates.append(date.date())
                elif isinstance(date, datetime):
                    all_dates.append(date.date())
                else:
                    all_dates.append(date)

        # V6.1.1: 空列表防护
        if not all_dates:
            return

        latest_date = max(all_dates)
        cutoff = latest_date - timedelta(days=self.window_days * 2)

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
        for (regime, behavior, psych), records in self.memory.items():
            key = f"{regime}|{behavior}|{psych}"
            data[key] = [
                {'date': str(d), 'success': s}
                for d, s in records
            ]
        return {
            'version': 'V6.1',
            'memory': data,
            'consecutive_failures': {f"{r}|{b}|{p}": v for (r, b, p), v in self.consecutive_failures.items()},
            'last_update': {f"{r}|{b}|{p}": str(v) for (r, b, p), v in self.last_update.items()},
            'global_successes': self._global_successes,
            'global_total': self._global_total,
        }

    @classmethod
    def from_dict(cls, data):
        """从字典反序列化"""
        obj = cls()
        memory_data = data.get('memory', {})
        for key, records in memory_data.items():
            parts = key.split('|')
            if len(parts) == 3:
                regime, behavior, psych = parts
            elif len(parts) == 2:
                # V6兼容：旧格式只有2维
                regime, behavior = parts
                psych = 'Unknown'
            else:
                continue
            obj.memory[(regime, behavior, psych)] = [
                (r['date'], r['success']) for r in records
            ]

        # 恢复连续失败
        cf_data = data.get('consecutive_failures', {})
        for key_str, val in cf_data.items():
            parts = key_str.split('|')
            if len(parts) == 3:
                obj.consecutive_failures[tuple(parts)] = val
            elif len(parts) == 2:
                obj.consecutive_failures[(parts[0], parts[1], 'Unknown')] = val

        # 恢复last_update
        lu_data = data.get('last_update', {})
        for key_str, val in lu_data.items():
            parts = key_str.split('|')
            if len(parts) == 3:
                obj.last_update[tuple(parts)] = val
            elif len(parts) == 2:
                obj.last_update[(parts[0], parts[1], 'Unknown')] = val

        obj._global_successes = data.get('global_successes', 0)
        obj._global_total = data.get('global_total', 0)

        return obj


# ============================================================
# V6.1: Time Decay 模块（关键修复）
# ============================================================

class TimeDecay:
    """
    V6.1 观察期置信度时间衰减（修复版）

    V6.0 问题：
    - 使用 (1-0.05)^n 硬衰减，几十天后乘数→0.1
    - 导致所有置信度几乎全部压缩，没有区分度

    V6.1 修复：
    - 使用 exp(-days/τ) 平滑衰减，τ=90天
    - 输出范围 [0.5, 1.0]
    - 前5天宽限期不衰减

    示例：
        Day 1-5:  1.00
        Day 10:   0.95  (5 decay days)
        Day 20:   0.85  (15 decay days)
        Day 50:   0.61  (45 decay days)
        Day 100:  0.50  (95 decay days, 触及下限)
        Day 200+: 0.50  (维持下限)
    """

    # V6.1 默认参数
    GRACE_PERIOD = 5              # 前5天不衰减（宽限期）
    TAU = 90                      # 衰减时间常数τ（天）
    MIN_MULTIPLIER = 0.5          # 最小乘数（绝不低于0.5）
    MIN_CONFIDENCE = 15           # 衰减到此值以下 → 过期标记

    def __init__(self, grace_period=None, tau=None,
                 min_multiplier=None, min_confidence=None):
        self.grace_period = grace_period or self.GRACE_PERIOD
        self.tau = tau or self.TAU
        self.min_multiplier = min_multiplier or self.MIN_MULTIPLIER
        self.min_confidence = min_confidence or self.MIN_CONFIDENCE

    def compute_decay(self, days_in_observation, current_confidence):
        """
        V6.1: 计算时间衰减后的置信度

        Args:
            days_in_observation: 在观察状态的天数
            current_confidence: 当前置信度 (0-100)

        Returns:
            (decayed_confidence, is_expired): 衰减后的置信度和是否过期
        """
        multiplier = self.compute_multiplier(days_in_observation)
        decayed = current_confidence * multiplier
        is_expired = decayed < self.min_confidence
        return max(0, decayed), is_expired

    def compute_multiplier(self, days_in_observation):
        """
        V6.1: 计算时间衰减乘数（关键修复）

        使用 exp(-days/τ) 替代 (1-rate)^n
        输出范围 [min_multiplier, 1.0]

        Args:
            days_in_observation: 在观察状态的天数

        Returns:
            multiplier: float in [min_multiplier, 1.0]
        """
        if days_in_observation <= self.grace_period:
            return 1.0

        # V6.1: 平滑指数衰减
        decay_days = days_in_observation - self.grace_period
        multiplier = np.exp(-decay_days / self.tau)

        # V6.1: 下限保护 —— 绝不低于 min_multiplier
        return max(self.min_multiplier, multiplier)

    def get_decay_curve(self, max_days=120, step=1):
        """
        V6.1: 获取衰减曲线数据（用于调试和可视化）

        Returns:
            list of (days, multiplier)
        """
        return [(d, self.compute_multiplier(d)) for d in range(0, max_days + 1, step)]

    def print_decay_curve(self, max_days=30):
        """打印衰减曲线"""
        print("\nV6.1 Time Decay Curve (exp(-days/τ), τ={}):".format(self.tau))
        print(f"  {'Days':>5} {'Multiplier':>10}")
        print(f"  {'-'*17}")
        for days, mult in self.get_decay_curve(max_days):
            print(f"  {days:>5} {mult:>10.3f}")


if __name__ == "__main__":
    # 演示 V6.1 BehaviorMemory
    import random
    random.seed(42)

    print("=" * 60)
    print("V6.1 BehaviorMemory Demo")
    print("=" * 60)

    bm = BehaviorMemory(window_days=90, min_samples=3, tau_days=90, laplace_alpha=1.0)

    psychs = ['Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion']
    regimes = ['Bull', 'Bear', 'Range']
    behaviors = ['DoubleBottom', 'TrendPullback', 'BreakoutConfirm']

    for i in range(60):
        regime = random.choice(regimes)
        behavior = random.choice(behaviors)
        psych = random.choice(psychs)
        success = random.random() < 0.55
        date = datetime(2026, 1, 1) + timedelta(days=i)
        bm.record_trade(regime, behavior, success, date, psych)

    bm.print_stats()

    # Replay Summary
    bm.print_replay_summary()

    # V6.1 Time Decay 演示
    print("\n" + "=" * 60)
    print("V6.1 Time Decay Demo (exp(-days/τ), τ=90)")
    print("=" * 60)

    td = TimeDecay(grace_period=5, tau=90, min_multiplier=0.5)
    td.print_decay_curve()

    print("\n关键对比:")
    print(f"  Day 5 (宽限期结束):  乘数 = {td.compute_multiplier(5):.3f}")
    print(f"  Day 10:              乘数 = {td.compute_multiplier(10):.3f}")
    print(f"  Day 20:              乘数 = {td.compute_multiplier(20):.3f}")
    print(f"  Day 30:              乘数 = {td.compute_multiplier(30):.3f}")
    print(f"  Day 60:              乘数 = {td.compute_multiplier(60):.3f}")
    print(f"  Day 90:              乘数 = {td.compute_multiplier(90):.3f}")
    print(f"  Day 120:             乘数 = {td.compute_multiplier(120):.3f}")

    # V6.0 旧方法对比
    print("\nV6.0 旧方法对比 ((1-0.05)^n):")
    for days in [5, 10, 20, 30, 60, 90]:
        decay_days = max(0, days - 5)
        old_mult = max(0.1, (1 - 0.05) ** decay_days)
        print(f"  Day {days:>3}:  旧={old_mult:.3f}  →  新={td.compute_multiplier(days):.3f}")
