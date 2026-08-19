"""
V6.2.3 Evidence Engine —— 多源置信度融合引擎（稳定化版本）
==========================================================

V6.2.3 核心升级（Fix Before Expand）：

1. Replay Learning 真正参与决策：不再永远返回1.0
   - 多维键 (Regime, Behavior, Psychology)
   - 时间加权样本 + Laplace平滑
   - 动态乘数反馈到最终置信度

2. ML Confidence 真正启用：
   - RandomForest/GradientBoosting 输出 P(up)
   - Probability Calibration 后参与 Evidence Engine
   - ML不可用时权重自动再分配

3. Evidence Explainability：
   - 每笔交易输出完整证据链分解
   - Rule/Replay/ML/Emotion 各证据源贡献可追溯
   - Replay 加分/减分原因明确

4. Time Decay 修复：
   - exp(-days/τ) 替代 (1-rate)^n
   - 输出范围 [0.5, 1.0]

架构：
    Evidence Engine
    ├── Rule Confidence      —— V5已有，人工规则打分
    ├── Replay Confidence    —— V6.2.3升级，在线学习闭环
    ├── ML Confidence        —— V6.2.3启用，机器学习概率输出
    ├── Emotion Bonus        —— V5.5新增，多源情绪双确认加分
    └── Probability Calibration —— V6.2.3验证，校准评估

                    ↓ 加权融合

              Pre-Decay Confidence

                    ↓ Time Decay（exp(-days/τ)）

              Final Confidence (0-100)

设计原则：
- 每个证据来源独立可插拔
- 权重可配置、可自适应调整
- 所有证据贡献在Replay中可追溯
"""

import numpy as np
from behavior_memory import BehaviorMemory, TimeDecay
from feature_builder import FeatureBuilder
from config import *


class EvidenceEngine:
    """
    V6.2.3 多源证据融合引擎（稳定化版本）

    聚合来自规则、历史统计、机器学习、情绪等多个来源的证据，
    输出最终的置信度分数及完整证据分解。
    """

    # 默认证据权重
    DEFAULT_WEIGHTS = {
        'rule': 0.30,       # 人工规则
        'replay': 0.25,     # 行为历史统计
        'ml': 0.35,         # 机器学习
        'emotion': 0.10,    # 情绪修正
    }

    # 自适应权重调整参数
    ADAPTIVE_WINDOW = 30
    ADAPTIVE_MIN_SAMPLES = 10

    def __init__(self, weights=None, use_ml=False, use_replay=True):
        """
        Args:
            weights: dict, 各证据来源的权重
            use_ml: 是否启用ML证据源（V6.2.3默认True）
            use_replay: 是否启用Replay学习
        """
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        self._original_weights = self.weights.copy()
        self.use_ml = use_ml
        self.use_replay = use_replay

        # 子引擎
        self.behavior_memory = BehaviorMemory(
            window_days=REPLAY_WINDOW_DAYS,
            min_samples=REPLAY_MIN_SAMPLES,
            max_age_days=REPLAY_MAX_AGE_DAYS,
            tau_days=REPLAY_TAU_DAYS,
            laplace_alpha=REPLAY_LAPLACE_ALPHA,
        ) if use_replay else None

        self.time_decay = TimeDecay(
            grace_period=TIME_DECAY_GRACE_PERIOD,
            tau=TIME_DECAY_TAU,
            min_multiplier=TIME_DECAY_MIN_MULTIPLIER,
            min_confidence=TIME_DECAY_MIN_CONFIDENCE,
        )

        self.ml_model = None          # ML模型
        self.calibrator = None        # 概率校准器
        self.calibration_enabled = True  # V6.2.3: 校准开关

        # 自适应权重跟踪
        self._evidence_performance = {k: [] for k in self.weights}

        # 最近一次融合的调试信息
        self.last_fusion_debug = {}

        # V6.2.3: 证据历史（用于后续分析）
        self.evidence_history = []
        self.fusion_count = 0

    # ============================================================
    # V6.2.3: 融合逻辑（核心方法）
    # ============================================================

    def compute_final_confidence(self, event, market_context, current_date=None):
        """
        V6.2.3: 计算最终置信度

        Args:
            event: BehaviorEvent 实例
            market_context: dict
            current_date: datetime

        Returns:
            final_confidence: float (0-100)
            evidence_breakdown: dict (完整证据分解，含Explainability)
        """
        # V6.2.3: 每次融合时先重置权重，再归一化防御
        self.weights = self._original_weights.copy()
        self._normalize_weights()

        evidence = {}
        confidence_parts = []

        # --- 1. Rule Confidence ---
        rule_conf = event.confidence
        rule_weight = self.weights['rule']
        evidence['rule'] = {
            'raw': round(rule_conf, 1),
            'weight': rule_weight,
            'contribution': round(rule_conf * rule_weight, 1),
        }
        confidence_parts.append(rule_conf * rule_weight)

        # --- 2. Replay Confidence (V6.2.3升级：真正动态) ---
        if self.use_replay and self.behavior_memory is not None:
            psychology = market_context.get('emotion_state', None)
            # V6.2.3: get_confidence_multiplier 返回 (multiplier, replay_info)
            multiplier, replay_info = self.behavior_memory.get_confidence_multiplier(
                market_context['regime'],
                event.behavior_name,
                current_date,
                psychology=psychology
            )
            replay_conf = rule_conf * multiplier
            replay_weight = self.weights['replay']
            evidence['replay'] = {
                'multiplier': round(multiplier, 3),
                'adjusted_conf': round(replay_conf, 1),
                'weight': replay_weight,
                'contribution': round(replay_conf * replay_weight, 1),
                # V6.2.3: Replay 解释信息
                'replay_info': replay_info,
                'explain': self._explain_replay(multiplier, replay_info, event.behavior_name),
            }
            confidence_parts.append(replay_conf * replay_weight)
        else:
            evidence['replay'] = {'contribution': 0, 'reason': 'disabled', 'explain': 'Replay未启用'}
            self._redistribute_weight('replay', 'rule')

        # --- 3. ML Confidence (V6.2.3：真正启用) ---
        if self.use_ml and self.ml_model is not None and self.ml_model._is_fitted:
            ml_prob = self._compute_ml_confidence(event, market_context)
            if self.calibrator is not None and self.calibration_enabled:
                ml_prob_calibrated = self.calibrator.calibrate(ml_prob)
            else:
                ml_prob_calibrated = ml_prob
            ml_conf = ml_prob_calibrated * 100  # 概率 → 0-100
            ml_weight = self.weights['ml']
            evidence['ml'] = {
                'raw_prob': round(ml_prob, 3),
                'calibrated_prob': round(ml_prob_calibrated, 3) if self.calibrator else None,
                'confidence': round(ml_conf, 1),
                'weight': ml_weight,
                'contribution': round(ml_conf * ml_weight, 1),
                'explain': self._explain_ml(ml_prob_calibrated, event.behavior_name),
            }
            confidence_parts.append(ml_conf * ml_weight)
        else:
            reason = 'no_model' if self.ml_model is None else 'not_fitted'
            evidence['ml'] = {
                'contribution': 0,
                'reason': reason,
                'explain': f'ML未启用({reason})',
            }
            # ML不可用 → 权重转移
            self._redistribute_weight('ml', 'rule', ratio=0.5)
            self._redistribute_weight('ml', 'replay', ratio=0.5)

        # --- 4. Emotion Bonus ---
        emotion_bonus = self._compute_emotion_bonus(
            event.behavior_type,
            market_context.get('emotion_state', 'Hope'),
            market_context.get('emotion_improving', False),
            market_context.get('emotion_magnitude', 0)
        )
        emotion_weight = self.weights['emotion']
        evidence['emotion'] = {
            'bonus': round(emotion_bonus, 1),
            'weight': emotion_weight,
            'contribution': round(emotion_bonus * emotion_weight, 1),
            'explain': self._explain_emotion(emotion_bonus, event.behavior_type,
                                              market_context.get('emotion_state', 'Hope'),
                                              market_context.get('emotion_improving', False)),
        }
        confidence_parts.append(emotion_bonus * emotion_weight)

        # --- 融合 ---
        pre_decay_confidence = sum(confidence_parts)

        # --- 5. Time Decay (V6.2.3修复：exp(-days/τ)) ---
        decay_multiplier = self.time_decay.compute_multiplier(event.age)
        final_confidence = pre_decay_confidence * decay_multiplier

        # 边界裁剪
        final_confidence = max(0, min(100, final_confidence))

        # V6.2.3: 构建完整证据分解（含 Explainability）
        evidence_breakdown = {
            'sources': evidence,
            'pre_decay': round(pre_decay_confidence, 1),
            'decay_multiplier': round(decay_multiplier, 3),
            'final': round(final_confidence, 1),
            'weights_used': {k: round(v, 3) for k, v in self.weights.items()},
            # V6.2.3: 证据总结
            'summary': self._build_evidence_summary(evidence, pre_decay_confidence,
                                                     decay_multiplier, final_confidence),
        }

        self.last_fusion_debug = evidence_breakdown
        self.fusion_count += 1

        # V6.2.3: 记录证据历史
        self.evidence_history.append({
            'behavior': event.behavior_name,
            'date': str(current_date) if current_date else None,
            'regime': market_context.get('regime'),
            'psychology': market_context.get('emotion_state'),
            'final_confidence': round(final_confidence, 1),
            'pre_decay': round(pre_decay_confidence, 1),
            'decay_multiplier': round(decay_multiplier, 3),
        })

        return final_confidence, evidence_breakdown

    # ============================================================
    # V6.2.3: Explainability 方法
    # ============================================================

    def _explain_replay(self, multiplier, replay_info, behavior_name):
        """V6.2.3: 解释 Replay 为什么加分/减分"""
        if multiplier == 1.0:
            reason = replay_info.get('reason', '无历史数据')
            return f"Replay中性({reason})"
        elif multiplier > 1.0:
            samples = replay_info.get('samples', 0)
            rate = replay_info.get('rate', 0)
            return f"Replay+{multiplier-1.0:+.0%}: {behavior_name}在类似环境成功率{rate:.0%}({samples}样本)"
        else:
            samples = replay_info.get('samples', 0)
            rate = replay_info.get('rate', 0)
            return f"Replay{multiplier-1.0:+.0%}: {behavior_name}在类似环境成功率{rate:.0%}({samples}样本)"

    def _explain_ml(self, ml_prob, behavior_name):
        """V6.2.3: 解释 ML 为什么加分/减分"""
        if ml_prob > 0.55:
            return f"ML看涨({ml_prob:.0%})"
        elif ml_prob < 0.45:
            return f"ML看跌({1-ml_prob:.0%})"
        else:
            return f"ML中性({ml_prob:.0%})"

    def _explain_emotion(self, bonus, behavior_type, emotion_state, improving):
        """V6.2.3: 解释 Emotion 为什么加分/减分"""
        if bonus > 0:
            if behavior_type == 'buy':
                return f"情绪+{bonus:.0f}: {emotion_state}情绪改善中，双确认加分"
            else:
                return f"情绪+{bonus:.0f}: {emotion_state}情绪恶化中，卖出确认加分"
        elif bonus < 0:
            return f"情绪{bonus:.0f}: 信号与情绪矛盾，减分"
        else:
            return "情绪中性"

    def _build_evidence_summary(self, evidence, pre_decay, decay_mult, final):
        """V6.2.3: 构建人类可读的证据总结"""
        lines = []
        for src_name in ['rule', 'replay', 'ml', 'emotion']:
            info = evidence.get(src_name, {})
            contrib = info.get('contribution', 0)
            explain = info.get('explain', '')
            if contrib != 0:
                lines.append(f"  {src_name.upper():>8}: {contrib:+.1f}  [{explain}]")
            elif info.get('reason'):
                lines.append(f"  {src_name.upper():>8}: 禁用({info['reason']})")

        lines.append(f"  {'─'*40}")
        lines.append(f"  PreDecay: {pre_decay:.1f} × TimeDecay({decay_mult:.2f}) = Final: {final:.1f}")
        return '\n'.join(lines)

    # ============================================================
    # 证据子组件
    # ============================================================

    def _compute_emotion_bonus(self, behavior_type, emotion_state, emotion_improving, emotion_magnitude):
        """计算情绪修正加分"""
        bonus = 0

        if behavior_type == 'buy':
            if emotion_state == 'Panic':
                bonus += 15
            elif emotion_state == 'Fear':
                bonus += 8
            if emotion_improving:
                bonus += 10 * emotion_magnitude
            if emotion_state in ('Euphoria', 'Exhaustion'):
                bonus -= 15

        elif behavior_type == 'sell':
            if emotion_state in ('Euphoria', 'Exhaustion'):
                bonus += 15
            if not emotion_improving and emotion_magnitude < 0:
                bonus += 8 * abs(emotion_magnitude)
            if emotion_state == 'Panic':
                bonus -= 20

        return np.clip(bonus, -30, 30)

    def _compute_ml_confidence(self, event, market_context):
        """
        V6.2.3: 使用ML模型计算概率（统一 FeatureBuilder）

        Returns:
            probability: float (0-1), P(up)
        """
        if self.ml_model is None:
            return 0.5

        features = FeatureBuilder.build(event, market_context)
        try:
            prob = self.ml_model.predict_proba(features)
            return float(prob)
        except Exception:
            return 0.5

    # ============================================================
    # 权重管理
    # ============================================================

    def _redistribute_weight(self, from_source, to_source, ratio=1.0):
        """将不可用的证据源权重重新分配"""
        if from_source in self.weights and to_source in self.weights:
            transfer = self.weights[from_source] * ratio
            self.weights[to_source] += transfer
            self.weights[from_source] -= transfer
            # V6.2.3: 每次重分配后归一化防御
            self._normalize_weights()

    def _normalize_weights(self):
        """V6.2.3: 归一化权重，确保 sum=1.0"""
        total = sum(self.weights.values())
        if total > 0 and abs(total - 1.0) > 1e-10:
            self.weights = {k: v / total for k, v in self.weights.items()}

    def set_weights(self, weights):
        """手动设置证据权重"""
        total = sum(weights.values())
        if abs(total - 1.0) > 0.01:
            weights = {k: v / total for k, v in weights.items()}
        self.weights = weights
        self._original_weights = weights.copy()

    def update_weights_adaptive(self):
        """自适应调整权重"""
        for source in list(self._evidence_performance.keys()):
            perf = self._evidence_performance[source]
            if len(perf) < self.ADAPTIVE_MIN_SAMPLES:
                continue

            accuracy = sum(1 for p in perf[-self.ADAPTIVE_WINDOW:] if p > 0) / len(perf[-self.ADAPTIVE_WINDOW:])

            if accuracy > 0.65:
                self.weights[source] = min(0.40, self.weights[source] * 1.1)
            elif accuracy < 0.40:
                self.weights[source] = max(0.10, self.weights[source] * 0.9)

        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

    def record_evidence_outcome(self, source, was_correct):
        """记录证据源的预测结果"""
        self._evidence_performance[source].append(1 if was_correct else 0)

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
        """获取行为记忆库"""
        return self.behavior_memory

    def get_fusion_debug(self):
        """获取最近一次融合的调试信息"""
        return self.last_fusion_debug

    def print_fusion_debug(self):
        """V6.2.3: 打印融合调试信息（增强版）"""
        if not self.last_fusion_debug:
            print("无融合记录")
            return

        dbg = self.last_fusion_debug
        print("\n" + "=" * 70)
        print("V6.2.3 Evidence Engine - 置信度融合明细 (Explainability)")
        print("=" * 70)

        for source, info in dbg['sources'].items():
            contrib = info.get('contribution', 0)
            if contrib == 0 and info.get('reason'):
                print(f"  [{source.upper():>8}] 已禁用: {info['reason']}")
            elif source == 'rule':
                print(f"  [{source.upper():>8}] Raw={info['raw']:.1f} × w={info['weight']:.2f} → {contrib:+.1f}")
            elif source == 'replay':
                mult = info.get('multiplier', 1.0)
                explain = info.get('explain', '')
                print(f"  [{source.upper():>8}] Mult={mult:.3f}, AdjConf={info.get('adjusted_conf', 0):.1f} × w={info['weight']:.2f} → {contrib:+.1f}")
                if explain:
                    print(f"           └─ {explain}")
                # 显示 Replay 样本详情
                ri = info.get('replay_info', {})
                if ri:
                    print(f"           └─ 样本: {ri.get('samples', 0)}, "
                          f"成功率: {ri.get('rate', 'N/A')}, "
                          f"原因: {ri.get('reason', 'N/A')}")
            elif source == 'ml':
                explain = info.get('explain', '')
                print(f"  [{source.upper():>8}] Prob={info.get('raw_prob', 0):.3f}, "
                      f"Calib={info.get('calibrated_prob', 'N/A')}, "
                      f"Conf={info.get('confidence', 0):.1f} × w={info['weight']:.2f} → {contrib:+.1f}")
                if explain:
                    print(f"           └─ {explain}")
            elif source == 'emotion':
                explain = info.get('explain', '')
                print(f"  [{source.upper():>8}] Bonus={info.get('bonus', 0):+.1f} × w={info['weight']:.2f} → {contrib:+.1f}")
                if explain:
                    print(f"           └─ {explain}")

        print(f"  {'─'*50}")
        print(f"  Pre-Decay Confidence: {dbg['pre_decay']:.1f}")
        print(f"  Decay Multiplier:     {dbg['decay_multiplier']:.3f}")
        print(f"  {'─'*50}")
        print(f"  FINAL CONFIDENCE:     {dbg['final']:.1f}")
        print("=" * 70)


if __name__ == "__main__":
    class MockEvent:
        def __init__(self):
            self.behavior_name = 'DoubleBottom'
            self.behavior_type = 'buy'
            self.confidence = 55
            self.age = 7
            self.strength = 45

    engine = EvidenceEngine(use_replay=True, use_ml=False)

    from datetime import datetime, timedelta
    bm = engine.get_behavior_memory()
    for i in range(20):
        bm.record_trade('Bull', 'DoubleBottom', i < 16, datetime(2026, 1, 1) + timedelta(days=i), 'Hope')
        bm.record_trade('Bear', 'DoubleBottom', i < 6, datetime(2026, 1, 1) + timedelta(days=i), 'Fear')
        bm.record_trade('Range', 'DoubleBottom', i < 10, datetime(2026, 1, 1) + timedelta(days=i), 'Hope')

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

    final_conf, breakdown = engine.compute_final_confidence(
        event, context, datetime(2026, 1, 20)
    )
    engine.print_fusion_debug()
