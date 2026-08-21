"""
V6.2.3 事件引擎（Event Engine）+ 行为生命周期（Behavior Lifecycle）
================================================================

V6.2.3 升级：
- Time Decay 参数更新为 exp(-days/τ)，min_multiplier=0.5
- Confidence 来源扩展为 Rule + Replay + ML + Emotion

核心理念：
- 不每天重新计算独立信号
- 所有行为必须形成 Event
- 每个 Event 经历完整生命周期：
  Candidate → Observation → Confirmed → Executed → Finished

Event 属性：
- event_id：唯一标识
- behavior_name：行为名称
- start_date：首次检测日期
- age：存在天数
- confidence：置信度（动态更新）
- strength：信号强度
- current_state：当前生命周期状态
- history：每日证据记录
- psych_change：对应的情绪变化
"""
import uuid
from config import *
from behavior_memory import TimeDecay

# V6.2.3: 全局 TimeDecay 实例（使用新参数）
_time_decay = TimeDecay(
    grace_period=TIME_DECAY_GRACE_PERIOD,
    tau=TIME_DECAY_TAU,
    min_multiplier=TIME_DECAY_MIN_MULTIPLIER,
    min_confidence=TIME_DECAY_MIN_CONFIDENCE,
)


class BehaviorEvent:
    """
    单个行为事件
    
    跟踪一个行为从检测到执行（或过期）的完整生命周期
    """

    def __init__(self, behavior_name, behavior_type, start_date, base_score, evidence, psych_change):
        self.event_id = str(uuid.uuid4())[:8]
        self.behavior_name = behavior_name   # 如 'DoubleBottom', 'MomentumExhaustion'
        self.behavior_type = behavior_type   # 'buy' 或 'sell'
        self.start_date = start_date
        self.age = 1                         # 存在天数
        self.confidence = CONFIDENCE_BASE    # 初始置信度
        self.base_score = base_score         # 基础行为分数
        self.strength = base_score           # 信号强度（base_score normalized）
        self.current_state = 'Candidate'     # 生命周期状态
        self.psych_change = psych_change     # 情绪变化描述

        # 历史记录
        self.history = [{
            'date': start_date,
            'state': 'Candidate',
            'confidence': self.confidence,
            'evidence': evidence,
            'change_reason': '首次检测'
        }]

    def update(self, date, new_evidence, regime, psychology_state, df_index_data):
        """
        每日更新事件状态

        根据新证据调整置信度，推动生命周期状态演变。

        Args:
            date: 当前日期
            new_evidence: 当日新证据 dict
            regime: 市场状态
            psychology_state: 市场情绪状态
            df_index_data: 当前行的 DataFrame 数据

        Returns:
            state_changed: bool (状态是否变化)
            description: 变化描述
        """
        self.age += 1
        state_before = self.current_state
        change_reason = ""

        if self.current_state == 'Candidate':
            # 候选 → 观察
            self.current_state = 'Observation'
            self.confidence = max(CONFIDENCE_MIN, self.confidence + 5)
            change_reason = "进入观察窗口"

        elif self.current_state == 'Observation':
            # 观察窗口：根据新证据动态调整置信度
            evidence_score = self._evaluate_evidence(new_evidence)
            confidence_delta = evidence_score * CONFIDENCE_INCREMENT

            # 市场状态修正
            if self.behavior_type == 'buy':
                if regime == 'Bull':
                    confidence_delta += 5
                elif regime == 'Bear':
                    confidence_delta -= 5

            # 情绪状态修正
            if self.behavior_type == 'buy':
                if psychology_state in ('Panic', 'Fear'):
                    confidence_delta += 3  # 恐惧中买入是好时机
                elif psychology_state == 'Euphoria':
                    confidence_delta -= 5  # 狂热中买入需谨慎

            if self.behavior_type == 'sell':
                if psychology_state == 'Euphoria':
                    confidence_delta += 5  # 狂热中卖出是好时机
                elif psychology_state == 'Exhaustion':
                    confidence_delta += 8  # 衰竭中必须卖出

            self.confidence = self._clamp_confidence(self.confidence + confidence_delta)
            change_reason = f"证据评分: {evidence_score:+.1f}, 置信度调整: {confidence_delta:+.0f}"

            # V6.2.3: Time Decay —— 观察期过长，置信度逐渐衰减
            decay_applied = _time_decay.compute_decay(self.age, self.confidence)
            self.confidence = decay_applied[0]
            if decay_applied[1]:
                # 衰减到阈值以下 → 过期
                self.current_state = 'Finished'
                change_reason += f" | 时间衰减至 {self.confidence:.0f}，过期"
                self.history.append({
                    'date': date,
                    'state': self.current_state,
                    'confidence': self.confidence,
                    'evidence': new_evidence,
                    'change_reason': change_reason,
                })
                return True, change_reason

            # V6.2.3: 状态机重构 —— 更清晰的生命周期判断
            if self.age < OBSERVATION_WINDOW_MIN:
                # 观察窗口初期：不判断，继续观察
                change_reason += " → 继续观察（观察期初）"
            elif self.age <= OBSERVATION_WINDOW_MAX:
                # 观察窗口内：高置信度可提前确认，低置信度可提前过期
                if self.confidence >= CONFIRMATION_THRESHOLD + 10:
                    self.current_state = 'Confirmed'
                    change_reason += " → 置信度极高，提前确认"
                elif self.confidence < EXPIRY_THRESHOLD:
                    self.current_state = 'Finished'
                    change_reason += " → 置信度跌至阈值以下，提前过期"
                else:
                    change_reason += " → 继续观察（窗口内）"
            else:
                # 观察窗口外：必须做出最终判断
                if self.confidence >= CONFIRMATION_THRESHOLD:
                    self.current_state = 'Confirmed'
                    change_reason += " → 观察期满，置信度达标，确认执行"
                elif self.confidence < EXPIRY_THRESHOLD:
                    self.current_state = 'Finished'
                    change_reason += " → 观察期满，置信度过低，过期取消"
                else:
                    # 置信度在中间地带：延长观察
                    change_reason += " → 观察期延长（置信度居中）"

        elif self.current_state == 'Confirmed':
            change_reason = "等待执行"

        elif self.current_state == 'Executed':
            change_reason = "已执行"

        elif self.current_state == 'Finished':
            change_reason = "已结束"

        # 记录历史
        self.history.append({
            'date': date,
            'state': self.current_state,
            'confidence': self.confidence,
            'evidence': new_evidence,
            'change_reason': change_reason,
        })

        state_changed = (self.current_state != state_before)
        return state_changed, change_reason

    def _evaluate_evidence(self, evidence):
        """
        评估新证据对置信度的影响

        返回: -2 到 +2 的分数
        - 正分 = 有利证据
        - 负分 = 不利证据
        """
        if evidence is None:
            return -1  # 无证据 = 轻度不利

        score = 0

        if self.behavior_name == 'DoubleBottom':
            # 有利：MA5开始拐头 +1，成交量恢复 +1，价格不创新低 +1
            if evidence.get('ma5_turning_up'):
                score += 1
            if evidence.get('vol_recovering'):
                score += 1
            if evidence.get('not_new_low'):
                score += 1
            # 不利：继续创新低 -2
            if evidence.get('new_low'):
                score -= 2

        elif self.behavior_name == 'MomentumExhaustion':
            # 有利：减速继续 -1 (卖出信号确认)
            if evidence.get('decelerating'):
                score += 1
            if evidence.get('rsi_declining'):
                score += 1
            # 不利：再度加速上涨
            if evidence.get('re_accelerating'):
                score -= 1

        elif self.behavior_name == 'TrendPullback':
            # 有利：企稳反弹，成交量谷底回升
            if evidence.get('bouncing'):
                score += 1
            if evidence.get('vol_bottoming'):
                score += 1
            # 不利：跌破MA20
            if evidence.get('broke_below_ma20'):
                score -= 2

        elif self.behavior_name == 'FalseBreak':
            if evidence.get('confirmed_fall'):
                score += 1
            if evidence.get('volume_surge'):
                score += 1

        elif self.behavior_name == 'BreakoutConfirm':
            if evidence.get('still_above_ma20'):
                score += 1
            if evidence.get('vol_sustained'):
                score += 1
            if evidence.get('fall_back'):
                score -= 2

        elif self.behavior_name == 'TrendFailure':
            if evidence.get('more_signals'):
                score += 1
            if evidence.get('close_below_ma20'):
                score += 1  # 卖出确认

        elif self.behavior_name == 'PanicSell':
            # 恐慌杀跌后：止跌 +1，成交量回归正常 +1
            if evidence.get('stop_falling'):
                score += 1
            if evidence.get('vol_normalizing'):
                score += 1
            # 继续暴跌：不利（虽然我们想逆向，但需要等止跌）
            if evidence.get('continued_crash'):
                score -= 1

        return score

    def _clamp_confidence(self, value):
        return max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, value))

    def mark_executed(self):
        """标记事件已执行"""
        self.current_state = 'Executed'
        self.history.append({
            'date': self.history[-1]['date'],
            'state': 'Executed',
            'confidence': self.confidence,
            'evidence': {},
            'change_reason': '触发交易',
        })

    def mark_finished(self):
        """标记事件结束"""
        self.current_state = 'Finished'

    def is_active(self):
        """事件是否仍在活跃状态（Candidate/Observation/Confirmed）"""
        return self.current_state in ('Candidate', 'Observation', 'Confirmed')

    def is_confirmed(self):
        """事件是否已确认"""
        return self.current_state == 'Confirmed'

    def is_executable(self):
        """事件是否可执行"""
        return self.current_state == 'Confirmed'

    def get_summary(self):
        """获取事件摘要"""
        return {
            'event_id': self.event_id,
            'behavior_name': self.behavior_name,
            'behavior_type': self.behavior_type,
            'start_date': self.start_date,
            'age': self.age,
            'confidence': self.confidence,
            'strength': self.strength,
            'state': self.current_state,
            'psych_change': self.psych_change,
            'history_length': len(self.history),
        }


class EventEngine:
    """
    事件引擎：管理所有活跃事件的完整生命周期

    - 维护活跃事件列表
    - 每日更新所有事件状态
    - 检测新的候选行为并创建事件
    - 清理过期/已完成事件
    """

    def __init__(self):
        self.active_events = {}   # event_id → BehaviorEvent
        self.completed_events = []  # 已完成的事件列表
        self.all_events = []        # 所有事件（包含已完成的）

    def process_daily(self, date, behavior_result, regime, psychology_state, df, index):
        """
        每日处理流程：

        1. 更新所有活跃事件
        2. 扫描新行为，创建候选事件
        3. 收集确认事件供执行

        Args:
            date: 当前日期
            behavior_result: 行为检测结果
            regime: 市场状态
            psychology_state: 市场情绪
            df: 完整的指标DataFrame
            index: 当前索引

        Returns:
            daily_summary: {
                'new_candidates': [...],
                'confirmed_events': [...],
                'expired_events': [...],
                'executed_events': [],
                'active_count': int
            }
        """
        daily_summary = {
            'new_candidates': [],
            'confirmed_events': [],
            'expired_events': [],
            'executed_events': [],
            'active_count': 0,
        }

        # Step 1: 更新现有活跃事件
        evidence = self._collect_daily_evidence(df, index)
        events_to_remove = []

        for event_id, event in self.active_events.items():
            if event.is_active():
                state_changed, desc = event.update(
                    date, evidence, regime, psychology_state,
                    df.iloc[index] if index < len(df) else None
                )

                if event.current_state == 'Confirmed':
                    daily_summary['confirmed_events'].append(event)
                elif event.current_state == 'Finished':
                    daily_summary['expired_events'].append(event)
                    events_to_remove.append(event_id)
                    self.completed_events.append(event)

        # 清理已过期的事件
        for eid in events_to_remove:
            del self.active_events[eid]

        # Step 2: 扫描新的行为候选
        buy_behaviors = behavior_result.get('buy_behaviors', [])
        sell_behaviors = behavior_result.get('sell_behaviors', [])

        for b_type, behaviors in [('buy', buy_behaviors), ('sell', sell_behaviors)]:
            for b in behaviors:
                name, score, b_evidence = b[0], b[1], b[2] if len(b) > 2 else {}

                # 检查是否已有同名活跃事件
                has_existing = False
                for event in self.active_events.values():
                    if event.behavior_name == name and event.is_active():
                        has_existing = True
                        break

                if not has_existing:
                    # 创建新事件
                    from crowd_psychology import CrowdPsychology as CP
                    psych_change = CP.behavior_to_psychology_change(name)
                    new_event = BehaviorEvent(
                        behavior_name=name,
                        behavior_type=b_type,
                        start_date=date,
                        base_score=score,
                        evidence=b_evidence,
                        psych_change=psych_change
                    )
                    self.active_events[new_event.event_id] = new_event
                    self.all_events.append(new_event)
                    daily_summary['new_candidates'].append(new_event)

        daily_summary['active_count'] = len(self.active_events)
        return daily_summary

    def _collect_daily_evidence(self, df, index):
        """收集当日的市场证据"""
        if index < 10:
            return {}

        from indicators import get_value as gv

        evidence = {
            # MA 相关
            'ma5_turning_up': gv(df, index, 'MA5_slope') > 0 and gv(df, index - 1, 'MA5_slope') <= 0,
            'broke_below_ma20': gv(df, index, 'close') < gv(df, index, 'MA20'),

            # 价格相关
            'not_new_low': True,
            'new_low': False,
            'bouncing': gv(df, index, 'close') > gv(df, index - 1, 'close'),
            'still_above_ma20': all(
                gv(df, index - d, 'close') > gv(df, index - d, 'MA20')
                for d in range(3) if index - d >= 0
            ),
            'fall_back': gv(df, index, 'close') < gv(df, index, 'MA20') and gv(df, index - 1, 'close') > gv(df, index - 1, 'MA20'),

            # 成交量相关
            'vol_recovering': gv(df, index, 'volume') > gv(df, index - 1, 'volume'),
            'vol_bottoming': abs(gv(df, index, 'volume') - gv(df, index, 'Vol20')) < gv(df, index, 'Vol20_std') * 0.5 if index >= 20 else False,
            'vol_sustained': gv(df, index, 'volume') / (gv(df, index, 'Vol20') + 1e-10) > 1.1,
            'vol_normalizing': gv(df, index, 'volume') / (gv(df, index, 'Vol20') + 1e-10) < 1.3,

            # 动量相关
            'decelerating': gv(df, index, 'is_decelerating', False),
            're_accelerating': gv(df, index, 'is_accelerating', False),
            'rsi_declining': gv(df, index, 'RSI14') < gv(df, index - 1, 'RSI14'),
            'confirmed_fall': gv(df, index, 'close') < gv(df, index, 'MA20'),

            # 趋势相关
            'more_signals': True,
            'close_below_ma20': gv(df, index, 'close') < gv(df, index, 'MA20'),
            'stop_falling': gv(df, index, 'close') >= gv(df, index - 1, 'close'),
            'continued_crash': gv(df, index, 'close') < gv(df, index - 1, 'close') * 0.98,
        }

        # 检查是否创新低
        if index >= 5:
            recent_low = min(gv(df, index - d, 'low') for d in range(5))
            evidence['new_low'] = gv(df, index, 'low') < recent_low
            evidence['not_new_low'] = not evidence['new_low']

        return evidence

    def mark_event_executed(self, event_id):
        """标记事件已被执行"""
        if event_id in self.active_events:
            self.active_events[event_id].mark_executed()

    def get_confirmed_buy_events(self):
        """获取所有已确认的买入事件"""
        events = []
        for eid, event in self.active_events.items():
            if event.is_confirmed() and event.behavior_type == 'buy':
                events.append(event)
        return events

    def get_confirmed_sell_events(self):
        """获取所有已确认的卖出事件"""
        events = []
        for eid, event in self.active_events.items():
            if event.is_confirmed() and event.behavior_type == 'sell':
                events.append(event)
        return events

    def get_active_summary(self):
        """获取所有活跃事件摘要"""
        return [event.get_summary() for event in self.active_events.values()]

    def get_event_by_id(self, event_id):
        """根据ID获取事件"""
        return self.active_events.get(event_id)


# CrowdPsychology 从 crowd_psychology 模块导入（使用时 import）
