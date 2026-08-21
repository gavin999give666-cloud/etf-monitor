"""
V5.0 市场情绪状态模块（Crowd Psychology）
===========================================

新增模块：识别市场群体心理状态。
6种状态：Panic → Fear → Hope → Optimism → Euphoria → Exhaustion

核心设计思想：
- 情绪状态不是每一天重新计算的独立信号
- 而是连续的状态机，考虑当前状态和切换条件
- 切换需要确认天数，避免噪声
- 每个行为必须说明当前情绪发生了什么变化
"""
import numpy as np
from config import *
from indicators import get_value


class CrowdPsychology:
    """
    市场情绪状态机

    状态切换逻辑：
    Panic    ← Z < -2.2，持续暴跌
    Fear     ← Z < -1.5，悲观蔓延
    Hope     ← -1.5 < Z < -0.5，初见止跌
    Optimism ← 0.5 < Z < 1.5，趋势确认
    Euphoria ← Z > 1.5 + RSI > 70，追涨
    Exhaustion ← 从Euphoria转向，动能衰竭
    """

    def __init__(self):
        self.current_state = 'Hope'       # 初始状态
        self.state_history = []           # [(date, state), ...]
        self.switch_streak = 0            # 连续满足切换条件的天数
        self.pending_state = None         # 待确认的新状态
        self.last_z_score = 0

    def update(self, df, index, date):
        """
        每日更新情绪状态

        返回: (state, state_changed, transition_description)
        """
        if index < 30:
            self.state_history.append((date, self.current_state))
            return self.current_state, False, "数据不足"

        z20 = get_value(df, index, 'Z20')
        rsi = get_value(df, index, 'RSI14')
        accel = get_value(df, index, 'acceleration')
        adx = get_value(df, index, 'ADX14')
        close = get_value(df, index, 'close')
        ma20 = get_value(df, index, 'MA20')
        vol_ratio = get_value(df, index, 'vol_ratio_to_mean', 1.0)

        self.last_z_score = z20

        # 确定目标状态
        target_state = self._determine_target_state(z20, rsi, accel, adx, close, ma20, vol_ratio)

        # 状态切换逻辑
        state_changed, transition_desc = self._process_transition(target_state)

        # 记录历史
        self.state_history.append((date, self.current_state))
        return self.current_state, state_changed, transition_desc

    def _determine_target_state(self, z20, rsi, accel, adx, close, ma20, vol_ratio):
        """根据指标确定目标情绪状态"""
        # Exhaustion 优先判断：从 Euphoria 转向
        if self.current_state == 'Euphoria' and accel < ACCEL_DECEL_THRESHOLD:
            if rsi > 60:
                return 'Exhaustion'

        # Euphoria: Z > 1.5 + RSI 过热
        if z20 > PSYCH_Z_EUPHORIA and rsi > 65:
            if accel > 0:
                return 'Euphoria'

        # Exhaustion: RSI 从高位回落 + 加速度转负
        if accel < -0.015 and rsi > 55:
            return 'Exhaustion'

        # Optimism: Z > 0.5 且 价格在 MA20 上方
        if z20 > PSYCH_Z_OPTIMISM:
            if close > ma20 or adx > 25:
                return 'Optimism'

        # Panic: Z < -2.2
        if z20 < PSYCH_Z_PANIC:
            return 'Panic'

        # Fear: Z < -1.5
        if z20 < PSYCH_Z_FEAR:
            return 'Fear'

        # Hope: Z between -1.5 and -0.5
        if PSYCH_Z_FEAR <= z20 < PSYCH_Z_HOPE:
            return 'Hope'

        # 默认：接近均值
        if -0.5 <= z20 < 0.5:
            return 'Hope'

        # Z > 0.5 但不是 Optimism
        return 'Optimism'

    def _process_transition(self, target_state):
        """处理状态切换，需要确认天数"""
        if target_state == self.current_state:
            self.pending_state = None
            self.switch_streak = 0
            return False, f"维持 {self.current_state}"

        # 正在切换中
        if self.pending_state == target_state:
            self.switch_streak += 1
            if self.switch_streak >= PSYCH_SWITCH_CONFIRM_DAYS:
                old_state = self.current_state
                self.current_state = target_state
                self.pending_state = None
                self.switch_streak = 0
                desc = self._describe_transition(old_state, target_state)
                return True, desc
            return False, f"等待确认 {target_state} ({self.switch_streak}/{PSYCH_SWITCH_CONFIRM_DAYS})"
        else:
            # 新的切换方向
            self.pending_state = target_state
            self.switch_streak = 1
            return False, f"开始偏向 {target_state}"

    def _describe_transition(self, old_state, new_state):
        """描述情绪状态切换的含义"""
        transitions = {
            ('Panic', 'Fear'): "恐慌缓解，市场从极端抛售中恢复 → 仍偏悲观",
            ('Panic', 'Hope'): "恐慌结束，出现止跌迹象 → 见底信号",
            ('Fear', 'Panic'): "恐惧升级为恐慌 → 恐慌性抛售",
            ('Fear', 'Hope'): "恐惧消退，市场开始企稳 → 筑底阶段",
            ('Hope', 'Optimism'): "希望转为乐观 → 趋势确认，适合加仓",
            ('Hope', 'Fear'): "希望破灭 → 反弹失败，回归恐惧",
            ('Optimism', 'Euphoria'): "乐观升级为狂热 → FOMO阶段，警惕追高风险",
            ('Optimism', 'Hope'): "乐观退潮 → 上涨乏力，适当减仓",
            ('Optimism', 'Exhaustion'): "乐观转为衰竭 → 清仓信号！",
            ('Euphoria', 'Exhaustion'): "狂热后衰竭 → 立刻减仓！不等RSI！",
            ('Euphoria', 'Optimism'): "狂热降温 → 回归理智，谨慎持有",
            ('Exhaustion', 'Fear'): "衰竭转恐惧 → 下跌趋势确认",
            ('Exhaustion', 'Hope'): "衰竭后企稳 → 底部形成中",
        }
        return transitions.get((old_state, new_state), f"{old_state} → {new_state}")

    def get_state(self):
        """获取当前情绪状态"""
        return self.current_state

    def get_state_history(self):
        """获取状态历史"""
        return self.state_history

    def is_extreme(self):
        """是否处于极端情绪（需要逆向思考）"""
        return self.current_state in ('Panic', 'Euphoria', 'Exhaustion')

    def should_fade_extreme(self):
        """是否应该逆向操作极端情绪"""
        return self.current_state == 'Panic'  # 恐慌时买入

    def should_reduce_on_euphoria(self):
        """是否应该在狂热中减仓"""
        return self.current_state in ('Euphoria', 'Exhaustion')

    @staticmethod
    def behavior_to_psychology_change(behavior_name):
        """
        每个行为对应的情绪变化说明

        例如：
        RecoveryStart: Fear → Hope
        DoubleBottom: Hope → Optimism
        MomentumExhaustion: Optimism → Euphoria
        """
        behavior_psych_map = {
            'DoubleBottom': {
                'from': 'Fear',
                'to': 'Hope',
                'description': '二次探底不破 → 恐惧转向希望'
            },
            'TrendPullback': {
                'from': 'Optimism',
                'to': 'Hope',
                'description': '趋势回踩 → 乐观暂歇，等待确认'
            },
            'BreakoutConfirm': {
                'from': 'Hope',
                'to': 'Optimism',
                'description': '有效突破确认 → 希望转为乐观'
            },
            'PanicSell': {
                'from': 'Panic',
                'to': 'Hope',
                'description': '恐慌杀跌 → 极端恐惧中孕育希望'
            },
            'MomentumExhaustion': {
                'from': 'Optimism',
                'to': 'Exhaustion',
                'description': '冲高衰竭 → 乐观转为衰竭，必须减仓'
            },
            'FalseBreak': {
                'from': 'Hope',
                'to': 'Fear',
                'description': '假突破 → 希望落空，回归恐惧'
            },
            'TrendFailure': {
                'from': 'Optimism',
                'to': 'Fear',
                'description': '趋势瓦解 → 乐观转向恐惧'
            },
        }
        return behavior_psych_map.get(behavior_name, {
            'from': 'Unknown',
            'to': 'Unknown',
            'description': '未知情绪变化'
        })
