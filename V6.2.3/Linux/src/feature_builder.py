"""
V6.2.3 Feature Builder —— 统一特征构建（Hotfix）

设计原则：
- 整个工程仅此一个特征构建器
- 训练和推理共用同一个 build() 方法
- 新增或修改特征必须在此文件完成

用法：
    from feature_builder import FeatureBuilder
    features = FeatureBuilder.build(event=event, market_context=context)
"""

# 行为类型列表（与 behavior_detector 保持一致）
BEHAVIOR_TYPES = [
    'DoubleBottom', 'MomentumExhaustion', 'TrendPullback',
    'FalseBreak', 'BreakoutConfirm', 'TrendFailure', 'PanicSell'
]

# 市场状态类型
REGIME_TYPES = ['Bull', 'Bear', 'Range', 'Unknown']

# 情绪状态类型
EMOTION_TYPES = ['Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion']


class FeatureBuilder:
    """
    V6.2.3 统一特征构建器

    所有 ML 特征构建必须通过此类的 build() 方法，
    禁止在 evidence_engine、backtest、ml_confidence 中各自实现。
    """

    @staticmethod
    def build(event, market_context):
        """
        构建 ML 模型输入特征向量

        Args:
            event: BehaviorEvent 实例（或兼容 dict/object），包含:
                - behavior_name: str
                - confidence: float (0-100)
                - strength: float (0-100)
            market_context: dict，包含:
                - regime: str
                - emotion_state: str
                - emotion_score: float (0-100)
                - emotion_improving: bool
                - emotion_magnitude: float
                - reward_score: float (0-50)
                - risk_score: float (0-50)

        Returns:
            features: list of float，长度 = 7 + 4 + 6 + 7 = 24
        """
        # 获取 behavior_name
        if hasattr(event, 'behavior_name'):
            behavior_name = event.behavior_name
        elif isinstance(event, dict):
            behavior_name = event.get('behavior_name', 'Unknown')
        else:
            behavior_name = 'Unknown'

        # 获取 confidence / strength
        if hasattr(event, 'confidence'):
            confidence = event.confidence
        elif isinstance(event, dict):
            confidence = event.get('confidence', 50)
        else:
            confidence = 50

        if hasattr(event, 'strength'):
            strength = event.strength
        elif isinstance(event, dict):
            strength = event.get('strength', 50)
        else:
            strength = 50

        # One-hot 编码
        behavior_onehot = [1.0 if behavior_name == b else 0.0 for b in BEHAVIOR_TYPES]
        regime_onehot = [1.0 if market_context.get('regime') == r else 0.0 for r in REGIME_TYPES]
        emotion_onehot = [1.0 if market_context.get('emotion_state') == e else 0.0 for e in EMOTION_TYPES]

        # 数值特征
        numerical = [
            confidence / 100.0,
            strength / 100.0,
            market_context.get('emotion_score', 50) / 100.0,
            market_context.get('reward_score', 25) / 50.0,
            market_context.get('risk_score', 25) / 50.0,
            float(market_context.get('emotion_improving', False)),
            market_context.get('emotion_magnitude', 0),
        ]

        return behavior_onehot + regime_onehot + emotion_onehot + numerical

    @staticmethod
    def build_from_trade(trade):
        """
        从交易记录构建 ML 训练特征向量

        与 build() 输出 dim 完全一致，仅输入来源不同。

        Args:
            trade: dict，包含:
                - entry_reason: list[str]
                - entry_regime: str
                - entry_psychology: str
                - entry_score: float
                - entry_reward: float
                - entry_risk: float
                - entry_emotion_score: float
                - entry_emotion_improving: bool

        Returns:
            features: list of float，长度 = 24
        """
        entry_reasons = trade.get('entry_reason', [])
        main_behavior = entry_reasons[0] if entry_reasons else 'Unknown'

        behavior_onehot = [1.0 if main_behavior == b else 0.0 for b in BEHAVIOR_TYPES]
        regime_onehot = [1.0 if trade.get('entry_regime') == r else 0.0 for r in REGIME_TYPES]
        emotion_onehot = [1.0 if trade.get('entry_psychology') == e else 0.0 for e in EMOTION_TYPES]

        numerical = [
            trade.get('entry_score', 50) / 100.0,
            trade.get('entry_score', 50) / 100.0,   # strength proxy: 用 entry_score 替代
            trade.get('entry_emotion_score', 50) / 100.0,
            trade.get('entry_reward', 25) / 50.0,
            trade.get('entry_risk', 25) / 50.0,
            float(trade.get('entry_emotion_improving', False)),
            0.3,  # emotion_magnitude default
        ]

        return behavior_onehot + regime_onehot + emotion_onehot + numerical

    @staticmethod
    def get_feature_names():
        """返回特征名称列表（共 24 个）"""
        names = []
        names.extend([f'behavior_{b}' for b in BEHAVIOR_TYPES])
        names.extend([f'regime_{r}' for r in REGIME_TYPES])
        names.extend([f'emotion_{e}' for e in EMOTION_TYPES])
        names.extend([
            'confidence',
            'strength',
            'emotion_score',
            'reward_score',
            'risk_score',
            'emotion_improving',
            'emotion_magnitude',
        ])
        return names

    @staticmethod
    def get_feature_count():
        """返回特征维度"""
        return len(BEHAVIOR_TYPES) + len(REGIME_TYPES) + len(EMOTION_TYPES) + 7
