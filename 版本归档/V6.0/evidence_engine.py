"""
V6.0 Evidence Engine —— 多源置信度融合引擎
=============================================

核心架构：
    Evidence Engine
    ├── Rule Confidence      —— V5已有，人工规则打分
    ├── Replay Confidence    —— V5.1新增，行为历史成功率统计
    ├── ML Confidence        —— V6新增，机器学习概率输出
    ├── Emotion Bonus        —— V5.5新增，多源情绪双确认加分
    └── Probability Calibration —— V6新增，概率校准

                    ↓ 加权融合

              Pre-Decay Confidence

                    ↓ Time Decay（时间惩罚项）

              Final Confidence (0-100)

设计原则：
- 每个证据来源独立可插拔（如ML不可用则自动回退）
- 权重可配置、可自适应调整
- 所有证据贡献在Replay中可追溯
- 命名统一为 Evidence，不再叫"Confidence"
"""

import numpy as np
from behavior_memory import BehaviorMemory, TimeDecay


class EvidenceEngine:
    """
    多源证据融合引擎

    聚合来自规则、历史统计、机器学习、情绪等多个来源的证据，
    输出最终的置信度分数。
    """

    # 默认证据权重
    DEFAULT_WEIGHTS = {
        'rule': 0.30,       # 人工规则
        'replay': 0.25,     # 行为历史统计
        'ml': 0.35,         # 机器学习
        'emotion': 0.10,    # 情绪修正
    }

    # 自适应权重调整参数
    ADAPTIVE_WINDOW = 30    # 最近30笔交易用于评估各证据源表现
    ADAPTIVE_MIN_SAMPLES = 10

    def __init__(self, weights=None, use_ml=False, use_replay=True):
        """
        Args:
            weights: dict, 各证据来源的权重
            use_ml: 是否启用ML证据源
            use_replay: 是否启用Replay学习
        """
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        self._original_weights = self.weights.copy()  # 保存原始权重用于每次调用时重置
        self.use_ml = use_ml
        self.use_replay = use_replay

        # 子引擎
        self.behavior_memory = BehaviorMemory() if use_replay else None
        self.time_decay = TimeDecay()
        self.ml_model = None          # ML模型（延迟加载）
        self.calibrator = None        # 概率校准器（延迟加载）

        # 自适应权重跟踪
        self._evidence_performance = {k: [] for k in self.weights}

        # 最近一次融合的调试信息
        self.last_fusion_debug = {}

    # ============================================================
    # 融合逻辑
    # ============================================================

    def compute_final_confidence(self, event, market_context, current_date=None):
        """
        计算最终置信度（V6核心方法）

        Args:
            event: BehaviorEvent 实例，包含:
                - behavior_name: str
                - behavior_type: str ('buy'/'sell')
                - confidence: float (Rule Confidence)
                - age: int (days_in_observation)
                - strength: float
            market_context: dict, 包含:
                - regime: str
                - emotion_state: str
                - emotion_score: float (0-100)
                - emotion_improving: bool
                - emotion_magnitude: float
            current_date: datetime，用于Replay的时效性检查

        Returns:
            final_confidence: float (0-100)
            evidence_breakdown: dict (各证据来源的贡献明细)
        """
        # 每次调用时重置权重（防止多次调用累积修改）
        self.weights = self._original_weights.copy()

        evidence = {}
        confidence_parts = []

        # --- 1. Rule Confidence (V5原有，直接使用event.confidence) ---
        rule_conf = event.confidence
        rule_weight = self.weights['rule']
        evidence['rule'] = {
            'raw': rule_conf,
            'weight': rule_weight,
            'contribution': rule_conf * rule_weight,
        }
        confidence_parts.append(rule_conf * rule_weight)

        # --- 2. Replay Confidence (V5.1新增) ---
        if self.use_replay and self.behavior_memory is not None:
            multiplier = self.behavior_memory.get_confidence_multiplier(
                market_context['regime'],
                event.behavior_name,
                current_date
            )
            replay_conf = rule_conf * multiplier  # 基于Rule Conf修正
            replay_weight = self.weights['replay']
            evidence['replay'] = {
                'multiplier': multiplier,
                'adjusted_conf': replay_conf,
                'weight': replay_weight,
                'contribution': replay_conf * replay_weight,
            }
            confidence_parts.append(replay_conf * replay_weight)
        else:
            # Replay不可用 → 权重转移给Rule
            evidence['replay'] = {'contribution': 0, 'reason': 'disabled'}
            self._redistribute_weight('replay', 'rule')

        # --- 3. ML Confidence (V6新增，可选) ---
        if self.use_ml and self.ml_model is not None:
            ml_prob = self._compute_ml_confidence(event, market_context)
            if self.calibrator is not None:
                ml_prob = self.calibrator.calibrate(ml_prob)
            ml_conf = ml_prob * 100  # 概率 → 0-100分数
            ml_weight = self.weights['ml']
            evidence['ml'] = {
                'raw_prob': ml_prob,
                'calibrated': ml_prob if self.calibrator else None,
                'confidence': ml_conf,
                'weight': ml_weight,
                'contribution': ml_conf * ml_weight,
            }
            confidence_parts.append(ml_conf * ml_weight)
        else:
            evidence['ml'] = {'contribution': 0, 'reason': 'disabled'}
            # ML不可用 → 权重转移给Rule和Replay
            self._redistribute_weight('ml', 'rule', ratio=0.5)
            self._redistribute_weight('ml', 'replay', ratio=0.5)

        # --- 4. Emotion Bonus (V5.5新增，价格+情绪双确认) ---
        emotion_bonus = self._compute_emotion_bonus(
            event.behavior_type,
            market_context.get('emotion_state', 'Hope'),
            market_context.get('emotion_improving', False),
            market_context.get('emotion_magnitude', 0)
        )
        emotion_weight = self.weights['emotion']
        evidence['emotion'] = {
            'bonus': emotion_bonus,
            'weight': emotion_weight,
            'contribution': emotion_bonus * emotion_weight,
        }
        confidence_parts.append(emotion_bonus * emotion_weight)

        # --- 融合 ---
        pre_decay_confidence = sum(confidence_parts)

        # --- 5. Time Decay (在所有证据之后施加) ---
        decay_multiplier = self.time_decay.compute_multiplier(event.age)
        final_confidence = pre_decay_confidence * decay_multiplier

        # 边界裁剪
        final_confidence = max(0, min(100, final_confidence))

        # 构建分解
        evidence_breakdown = {
            'sources': evidence,
            'pre_decay': round(pre_decay_confidence, 1),
            'decay_multiplier': round(decay_multiplier, 3),
            'final': round(final_confidence, 1),
            'weights_used': self.weights.copy(),
        }

        self.last_fusion_debug = evidence_breakdown
        return final_confidence, evidence_breakdown

    # ============================================================
    # 证据子组件
    # ============================================================

    def _compute_emotion_bonus(self, behavior_type, emotion_state, emotion_improving, emotion_magnitude):
        """
        计算情绪修正加分

        对应论文二的"价格+情绪双确认"机制：
        - 买入行为 + 情绪改善 → 加分
        - 卖出行为 + 情绪恶化 → 加分
        - 买入行为 + 狂热情绪 → 减分
        - 卖出行为 + 恐慌情绪 → 减分（不该恐慌卖出）
        """
        bonus = 0

        if behavior_type == 'buy':
            # 恐慌中逆向买入 → 加分
            if emotion_state == 'Panic':
                bonus += 15
            elif emotion_state == 'Fear':
                bonus += 8
            # 情绪改善 → 加分（双确认）
            if emotion_improving:
                bonus += 10 * emotion_magnitude
            # 狂热中买入 → 减分
            if emotion_state in ('Euphoria', 'Exhaustion'):
                bonus -= 15

        elif behavior_type == 'sell':
            # 狂热中卖出 → 加分
            if emotion_state in ('Euphoria', 'Exhaustion'):
                bonus += 15
            # 情绪恶化 → 卖出加分
            if not emotion_improving and emotion_magnitude < 0:
                bonus += 8 * abs(emotion_magnitude)
            # 恐慌中卖出 → 减分（不该恐慌卖）
            if emotion_state == 'Panic':
                bonus -= 20

        return np.clip(bonus, -30, 30)

    def _compute_ml_confidence(self, event, market_context):
        """
        使用ML模型计算概率（V6新增）

        当前为桩实现，实际需要训练好的模型。

        Returns:
            probability: float (0-1), 未来N日上涨的概率
        """
        if self.ml_model is None:
            return 0.5  # 无模型时返回中性概率

        # 构建特征向量
        features = self._build_ml_features(event, market_context)
        try:
            prob = self.ml_model.predict_proba([features])[0][1]  # 上涨概率
            return float(prob)
        except Exception:
            return 0.5

    def _build_ml_features(self, event, market_context):
        """
        构建ML模型输入特征向量

        V6要求：特征必须包含 Regime 和 Emotion，让ML学习行为在不同环境下的条件概率
        """
        # 行为类型编码
        behavior_types = [
            'DoubleBottom', 'MomentumExhaustion', 'TrendPullback',
            'FalseBreak', 'BreakoutConfirm', 'TrendFailure', 'PanicSell'
        ]
        behavior_onehot = [1 if event.behavior_name == b else 0 for b in behavior_types]

        # 市场状态编码
        regime_types = ['Bull', 'Bear', 'Range', 'Unknown']
        regime_onehot = [1 if market_context.get('regime') == r else 0 for r in regime_types]

        # 情绪状态编码
        emotion_types = ['Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion']
        emotion_onehot = [1 if market_context.get('emotion_state') == e else 0 for e in emotion_types]

        features = (
            behavior_onehot +
            regime_onehot +
            emotion_onehot +
            [
                event.confidence / 100.0,
                event.strength / 100.0,
                market_context.get('emotion_score', 50) / 100.0,
                market_context.get('reward_score', 25) / 50.0,
                market_context.get('risk_score', 25) / 50.0,
                float(market_context.get('emotion_improving', False)),
                market_context.get('emotion_magnitude', 0),
            ]
        )
        return features

    # ============================================================
    # 权重管理
    # ============================================================

    def _redistribute_weight(self, from_source, to_source, ratio=1.0):
        """将不可用的证据源权重重新分配"""
        if from_source in self.weights and to_source in self.weights:
            transfer = self.weights[from_source] * ratio
            self.weights[to_source] += transfer
            self.weights[from_source] -= transfer

    def set_weights(self, weights):
        """手动设置证据权重"""
        total = sum(weights.values())
        if abs(total - 1.0) > 0.01:
            # 归一化
            weights = {k: v / total for k, v in weights.items()}
        self.weights = weights

    def update_weights_adaptive(self):
        """
        自适应调整权重

        基于最近N笔交易中每个证据源的预测表现，
        自动调整权重分配。
        需要足够样本量才执行。
        """
        for source in list(self._evidence_performance.keys()):
            perf = self._evidence_performance[source]
            if len(perf) < self.ADAPTIVE_MIN_SAMPLES:
                continue

            # 计算该证据源的近期准确率
            accuracy = sum(1 for p in perf[-self.ADAPTIVE_WINDOW:] if p > 0) / len(perf[-self.ADAPTIVE_WINDOW:])

            # 基于准确率调整权重
            if accuracy > 0.65:
                self.weights[source] = min(0.40, self.weights[source] * 1.1)
            elif accuracy < 0.40:
                self.weights[source] = max(0.10, self.weights[source] * 0.9)

        # 重新归一化
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

    def record_evidence_outcome(self, source, was_correct):
        """记录证据源的预测结果（用于自适应权重）"""
        self._evidence_performance[source].append(1 if was_correct else 0)

        # 保持滑动窗口
        for src in self._evidence_performance:
            if len(self._evidence_performance[src]) > self.ADAPTIVE_WINDOW * 2:
                self._evidence_performance[src] = self._evidence_performance[src][-self.ADAPTIVE_WINDOW * 2:]

    # ============================================================
    # 集成接口
    # ============================================================

    def set_ml_model(self, model, calibrator=None):
        """设置ML模型和校准器"""
        self.ml_model = model
        self.use_ml = True
        if calibrator:
            self.calibrator = calibrator

    def get_behavior_memory(self):
        """获取行为记忆库（供外部更新）"""
        return self.behavior_memory

    def get_fusion_debug(self):
        """获取最近一次融合的调试信息"""
        return self.last_fusion_debug

    def print_fusion_debug(self):
        """打印融合调试信息"""
        if not self.last_fusion_debug:
            print("无融合记录")
            return

        dbg = self.last_fusion_debug
        print("\n" + "=" * 60)
        print("Evidence Engine - 置信度融合明细")
        print("=" * 60)

        for source, info in dbg['sources'].items():
            contrib = info.get('contribution', 0)
            reason = info.get('reason', '')
            if contrib == 0 and reason:
                print(f"  [{source.upper():>8}] 已禁用: {reason}")
            elif source == 'rule':
                print(f"  [{source.upper():>8}] Raw={info['raw']:.1f} × w={info['weight']:.2f} → {contrib:.1f}")
            elif source == 'replay':
                print(f"  [{source.upper():>8}] Mult={info.get('multiplier', 0):.2f}, AdjConf={info.get('adjusted_conf', 0):.1f} × w={info['weight']:.2f} → {contrib:.1f}")
            elif source == 'ml':
                prob = info.get('raw_prob', 0)
                print(f"  [{source.upper():>8}] Prob={prob:.3f}, Conf={info.get('confidence', 0):.1f} × w={info['weight']:.2f} → {contrib:.1f}")
            elif source == 'emotion':
                print(f"  [{source.upper():>8}] Bonus={info.get('bonus', 0):.1f} × w={info['weight']:.2f} → {contrib:.1f}")

        print(f"  {'─' * 40}")
        print(f"  Pre-Decay Confidence: {dbg['pre_decay']:.1f}")
        print(f"  Decay Multiplier:     {dbg['decay_multiplier']:.3f}")
        print(f"  {'─' * 40}")
        print(f"  FINAL CONFIDENCE:     {dbg['final']:.1f}")
        print("=" * 60)


if __name__ == "__main__":
    # 建造一个模拟的BehaviorEvent
    class MockEvent:
        def __init__(self):
            self.behavior_name = 'DoubleBottom'
            self.behavior_type = 'buy'
            self.confidence = 55
            self.age = 7
            self.strength = 45

    engine = EvidenceEngine(use_replay=True, use_ml=False)

    # 向行为记忆库添加一些历史数据
    from datetime import datetime, timedelta
    bm = engine.get_behavior_memory()
    for i in range(15):
        bm.record_trade('Bull', 'DoubleBottom', i < 12, datetime(2026, 1, 1) + timedelta(days=i))
        bm.record_trade('Bear', 'DoubleBottom', i < 5, datetime(2026, 1, 1) + timedelta(days=i))

    event = MockEvent()
    context = {
        'regime': 'Bull',
        'emotion_state': 'Hope',
        'emotion_score': 55,
        'emotion_improving': True,
        'emotion_magnitude': 0.3,
        'reward_score': 30,
        'risk_score': 20,
    }

    final_conf, breakdown = engine.compute_final_confidence(event, context)
    engine.print_fusion_debug()
