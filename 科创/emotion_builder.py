"""
V6.0 EmotionBuilder —— 多源数据融合情绪引擎
==============================================

核心升级（vs V5.0 crowd_psychology.py）：
- V5: 纯价格衍生指标 (Z20, RSI, ADX) → 硬编码阈值 → 6种离散状态
- V6: 多源市场数据 → 降维融合算法 → 连续 Emotion Score → 阈值映射离散状态

设计原则：
- 多源数据融合是核心价值，PCA只是工具选择之一
- 架构支持可插拔后端：PCA / AutoEncoder / ICA / 直接加权
- 阈值由历史数据分位数自动确定，而非人工硬编码
- 连续 Emotion Score (0-100) 承载更丰富的情绪信息
- 价格+情绪双确认：Behavior + Emotion Improvement → 提升Confidence

数据源（可扩展）：
- 成交量/成交额
- 融资余额变化
- ETF资金净流入/流出
- 北向资金净流入/流出
- 涨跌停比
- 新高新低比
- 波动率
- Put/Call比率
"""

import numpy as np
from config import *


def _safe_get(df, index, col, default=0):
    """安全获取DataFrame列值，列不存在时返回默认值"""
    if col not in df.columns:
        return default
    try:
        from indicators import get_value
        return get_value(df, index, col, default)
    except (KeyError, IndexError):
        return default


class EmotionBuilder:
    """
    多源数据融合情绪引擎

    支持多种后端算法，默认PCA，可通过 method 切换。
    """

    # 默认特征权重（当无法使用PCA时的回退方案）
    DEFAULT_FEATURE_WEIGHTS = {
        'volume_ratio': 0.15,
        'turnover_ratio': 0.15,
        'margin_balance_chg': 0.15,
        'etf_flow': 0.15,
        'north_bound': 0.20,
        'advance_decline_ratio': 0.10,
        'new_high_low_ratio': 0.10,
    }

    def __init__(self, method='weighted', n_components=0.85):
        """
        Args:
            method: 融合方法
                - 'weighted': 简单加权（默认，不需要历史数据）
                - 'pca': 主成分分析（线性降维，需要sklearn）
                - 'ica': 独立成分分析（信号分离，需要sklearn）
                - 'autoencoder': 自编码器（非线性降维，需安装torch）
            n_components: PCA保留的累积方差比例 (method='pca'时)
        """
        self.method = method
        self.n_components = n_components
        self._scaler = None     # sklearn StandardScaler，延迟导入
        self._model = None
        self._is_fitted = False

        # 分位数阈值（由历史数据确定）
        self.percentile_20 = 30
        self.percentile_35 = 40
        self.percentile_55 = 55
        self.percentile_75 = 65
        self.percentile_90 = 80

        # 情绪状态机（保持与V5兼容）
        self.current_state = 'Hope'
        self.state_history = []
        self.switch_streak = 0
        self.pending_state = None
        self.last_emotion_score = 50

    # ============================================================
    # 特征收集
    # ============================================================

    def collect_features(self, df, index):
        """
        从DataFrame收集多源市场特征

        Args:
            df: 指标DataFrame
            index: 当前索引

        Returns:
            features: dict of {feature_name: value}，缺失值填0
        """
        from indicators import get_value

        features = {}

        # --- 成交量相关 ---
        vol = get_value(df, index, 'volume', 0)
        vol20 = get_value(df, index, 'Vol20', 1)
        features['volume_ratio'] = vol / max(vol20, 1)  # 当前量 vs 20日均量

        # 成交额（如果有，否则用价格×量估算）
        if 'amount' in df.columns:
            amount = get_value(df, index, 'amount', 0)
            amount20 = df['amount'].rolling(20).mean().iloc[index] if index >= 20 else amount
            features['turnover_ratio'] = amount / max(amount20, 1)
        else:
            close = get_value(df, index, 'close', 0)
            features['turnover_ratio'] = (vol * close) / max(vol20 * get_value(df, index, 'MA20', close), 1)

        # --- 价格动量 ---
        close = get_value(df, index, 'close', 0)
        close_5d = get_value(df, max(0, index - 5), 'close', close)
        close_20d = get_value(df, max(0, index - 20), 'close', close)
        features['return_5d'] = (close - close_5d) / max(close_5d, 0.01)
        features['return_20d'] = (close - close_20d) / max(close_20d, 0.01)

        # --- 技术指标 ---
        features['rsi'] = get_value(df, index, 'RSI14', 50) / 100.0  # 归一化到0-1
        features['z_score'] = np.clip(get_value(df, index, 'Z20', 0) / 3.0, -1, 1)  # 裁剪到±1
        features['adx'] = get_value(df, index, 'ADX14', 20) / 50.0  # 归一化

        # --- 波动率 ---
        features['volatility'] = get_value(df, index, 'Volatility20', 0.01) * 10  # 年化波动率近似
        features['volatility_5d'] = get_value(df, index, 'Volatility5', 0.01) * 10

        # --- 价格位置 ---
        features['price_position'] = get_value(df, index, 'price_position', 0.5)
        features['ma20_deviation'] = get_value(df, index, 'ma20_deviation', 0)

        # --- 加速度 ---
        features['acceleration'] = np.clip(get_value(df, index, 'acceleration', 0) * 100, -10, 10)

        # --- 融资余额/北向资金（占位，实际使用时需外部数据源）---
        # 使用安全的列存在性检查
        features['margin_balance_chg'] = _safe_get(df, index, 'margin_balance_chg', 0)
        features['etf_flow'] = _safe_get(df, index, 'etf_flow', 0)
        features['north_bound'] = _safe_get(df, index, 'north_bound', 0)
        features['advance_decline_ratio'] = _safe_get(df, index, 'advance_decline_ratio', 0.5)
        features['new_high_low_ratio'] = _safe_get(df, index, 'new_high_low_ratio', 0.5)

        return features

    # ============================================================
    # 核心方法
    # ============================================================

    def build_emotion_score(self, df, index):
        """
        构建连续情绪得分 (0-100)

        Args:
            df: 指标DataFrame
            index: 当前索引

        Returns:
            emotion_score: float (0-100), 越高越乐观
        """
        features = self.collect_features(df, index)

        if self.method == 'weighted':
            return self._weighted_fusion(features)
        elif self.method == 'pca':
            return self._pca_fusion(features)
        elif self.method == 'ica':
            return self._ica_fusion(features)
        else:
            return self._weighted_fusion(features)

    def _weighted_fusion(self, features):
        """简单加权融合"""
        score = 50.0  # 中性起点

        # Z-Score: 正分多=乐观，负分多=悲观
        score += features.get('z_score', 0) * 30

        # RSI: 高位=乐观
        score += (features.get('rsi', 0.5) - 0.5) * 20

        # 近期收益率
        score += features.get('return_5d', 0) * 15
        score += features.get('return_20d', 0) * 10

        # 成交量：放量上涨=乐观
        if features.get('return_5d', 0) > 0 and features.get('volume_ratio', 0) > 1.1:
            score += 5
        elif features.get('return_5d', 0) < 0 and features.get('volume_ratio', 0) > 1.3:
            score -= 5

        # 价格位置
        score += (features.get('price_position', 0.5) - 0.5) * 15

        # 融资余额变化（如果有数据）
        score += np.clip(features.get('margin_balance_chg', 0) * 50, -5, 5)
        score += np.clip(features.get('etf_flow', 0) * 50, -5, 5)
        score += np.clip(features.get('north_bound', 0) * 50, -5, 5)

        return np.clip(score, 0, 100)

    def _pca_fusion(self, features):
        """PCA降维融合"""
        feature_names = [
            'volume_ratio', 'turnover_ratio', 'return_5d', 'return_20d',
            'rsi', 'z_score', 'adx', 'volatility', 'price_position',
            'ma20_deviation', 'acceleration'
        ]
        X = np.array([[features.get(f, 0) for f in feature_names]])

        if not self._is_fitted:
            # 单样本无法拟合PCA，回退到加权
            return self._weighted_fusion(features)

        try:
            X_scaled = self._scaler.transform(X)
            X_transformed = self._model.transform(X_scaled)
            # 将第一主成分映射到0-100
            pc1 = X_transformed[0, 0]
            score = 50 + pc1 * 20  # 中心50，标准差约20
            return np.clip(score, 0, 100)
        except Exception:
            return self._weighted_fusion(features)

    def _ica_fusion(self, features):
        """ICA独立成分融合"""
        # 与PCA类似但使用ICA
        feature_names = [
            'volume_ratio', 'turnover_ratio', 'return_5d', 'return_20d',
            'rsi', 'z_score', 'adx', 'volatility', 'price_position',
            'ma20_deviation', 'acceleration'
        ]
        X = np.array([[features.get(f, 0) for f in feature_names]])

        if not self._is_fitted:
            return self._weighted_fusion(features)

        try:
            X_scaled = self._scaler.transform(X)
            X_transformed = self._model.transform(X_scaled)
            score = 50 + X_transformed[0, 0] * 20
            return np.clip(score, 0, 100)
        except Exception:
            return self._weighted_fusion(features)

    # ============================================================
    # 模型拟合（历史数据批处理）
    # ============================================================

    def fit(self, df, indices=None):
        """
        使用历史数据拟合降维模型

        Args:
            df: 指标DataFrame
            indices: 用于拟合的索引列表，None则使用全部
        """
        if self.method not in ('pca', 'ica'):
            return

        # 延迟导入 sklearn
        try:
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            print("EmotionBuilder.fit(): sklearn not installed, falling back to weighted method")
            self.method = 'weighted'
            return

        feature_names = [
            'volume_ratio', 'turnover_ratio', 'return_5d', 'return_20d',
            'rsi', 'z_score', 'adx', 'volatility', 'price_position',
            'ma20_deviation', 'acceleration'
        ]

        if indices is None:
            indices = range(60, len(df))

        X_list = []
        for idx in indices:
            features = self.collect_features(df, idx)
            X_list.append([features.get(f, 0) for f in feature_names])

        if len(X_list) < 20:
            return  # 样本不足

        X = np.array(X_list)
        X_scaled = self._scaler.fit_transform(X)

        if self.method == 'pca':
            from sklearn.decomposition import PCA
            self._model = PCA(n_components=self.n_components)
            self._model.fit(X_scaled)
        elif self.method == 'ica':
            from sklearn.decomposition import FastICA
            self._model = FastICA(n_components=1, random_state=42)
            self._model.fit(X_scaled)

        self._is_fitted = True

        # 从训练数据计算分位数阈值
        scores = []
        for idx in indices[-120:]:  # 用最近120天计算分位数
            score = self.build_emotion_score(df, idx)
            scores.append(score)

        if len(scores) >= 30:
            scores = np.array(scores)
            self.percentile_20 = np.percentile(scores, 20)
            self.percentile_35 = np.percentile(scores, 35)
            self.percentile_55 = np.percentile(scores, 55)
            self.percentile_75 = np.percentile(scores, 75)
            self.percentile_90 = np.percentile(scores, 90)

    # ============================================================
    # 状态映射
    # ============================================================

    def map_to_emotion_state(self, score):
        """
        连续 Emotion Score → 离散情绪状态

        使用历史分位数作为阈值（自适应），而非硬编码。

        Args:
            score: 0-100 连续情绪得分

        Returns:
            state: str ('Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion')
        """
        if score < self.percentile_20:
            return 'Panic'
        elif score < self.percentile_35:
            return 'Fear'
        elif score < self.percentile_55:
            return 'Hope'
        elif score < self.percentile_75:
            return 'Optimism'
        elif score < self.percentile_90:
            return 'Euphoria'
        else:
            return 'Exhaustion'

    # ============================================================
    # 日常更新（替代V5 CrowdPsychology.update）
    # ============================================================

    def update(self, df, index, date):
        """
        每日更新情绪状态

        Args:
            df: 指标DataFrame
            index: 当前索引
            date: 当前日期

        Returns:
            (state, state_changed, description)
        """
        if index < 30:
            self.state_history.append((date, self.current_state))
            return self.current_state, False, "数据不足"

        # 计算连续情绪得分
        emotion_score = self.build_emotion_score(df, index)
        self.last_emotion_score = emotion_score

        # 映射到离散状态
        target_state = self.map_to_emotion_state(emotion_score)

        # 处理状态切换（需要确认天数避免噪声）
        state_changed, desc = self._process_transition(target_state)

        self.state_history.append((date, self.current_state))
        return self.current_state, state_changed, desc

    def _process_transition(self, target_state):
        """处理状态切换，需要确认天数"""
        if target_state == self.current_state:
            self.pending_state = None
            self.switch_streak = 0
            return False, f"维持 {self.current_state}"

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
            self.pending_state = target_state
            self.switch_streak = 1
            return False, f"开始偏向 {target_state}"

    def _describe_transition(self, old_state, new_state):
        """描述情绪状态切换的含义"""
        transitions = {
            ('Panic', 'Fear'): "恐慌缓解，市场从极端抛售中恢复",
            ('Panic', 'Hope'): "恐慌结束，出现止跌迹象 → 见底信号",
            ('Fear', 'Panic'): "恐惧升级为恐慌 → 恐慌性抛售",
            ('Fear', 'Hope'): "恐惧消退，市场开始企稳 → 筑底阶段",
            ('Hope', 'Optimism'): "希望转为乐观 → 趋势确认，适合加仓",
            ('Hope', 'Fear'): "希望破灭 → 反弹失败，回归恐惧",
            ('Optimism', 'Euphoria'): "乐观升级为狂热 → FOMO阶段",
            ('Optimism', 'Hope'): "乐观退潮 → 上涨乏力，适当减仓",
            ('Optimism', 'Exhaustion'): "乐观转为衰竭 → 清仓信号",
            ('Euphoria', 'Exhaustion'): "狂热后衰竭 → 立刻减仓",
            ('Euphoria', 'Optimism'): "狂热降温 → 回归理智",
            ('Exhaustion', 'Fear'): "衰竭转恐惧 → 下跌趋势确认",
            ('Exhaustion', 'Hope'): "衰竭后企稳 → 底部形成中",
        }
        return transitions.get((old_state, new_state), f"{old_state} → {new_state}")

    def get_state(self):
        return self.current_state

    def get_state_history(self):
        return self.state_history

    def get_emotion_score(self):
        return self.last_emotion_score

    def is_extreme(self):
        return self.current_state in ('Panic', 'Euphoria', 'Exhaustion')

    def should_fade_extreme(self):
        return self.current_state == 'Panic'

    def should_reduce_on_euphoria(self):
        return self.current_state in ('Euphoria', 'Exhaustion')

    # ============================================================
    # 情绪→行为映射（保持与V5兼容）
    # ============================================================

    @staticmethod
    def behavior_to_psychology_change(behavior_name):
        """每个行为对应的情绪变化说明"""
        behavior_psych_map = {
            'DoubleBottom': {
                'from': 'Fear', 'to': 'Hope',
                'description': '二次探底不破 → 恐惧转向希望'
            },
            'TrendPullback': {
                'from': 'Optimism', 'to': 'Hope',
                'description': '趋势回踩 → 乐观暂歇，等待确认'
            },
            'BreakoutConfirm': {
                'from': 'Hope', 'to': 'Optimism',
                'description': '有效突破确认 → 希望转为乐观'
            },
            'PanicSell': {
                'from': 'Panic', 'to': 'Hope',
                'description': '恐慌杀跌 → 极端恐惧中孕育希望'
            },
            'MomentumExhaustion': {
                'from': 'Optimism', 'to': 'Exhaustion',
                'description': '冲高衰竭 → 乐观转为衰竭，必须减仓'
            },
            'FalseBreak': {
                'from': 'Hope', 'to': 'Fear',
                'description': '假突破 → 希望落空，回归恐惧'
            },
            'TrendFailure': {
                'from': 'Optimism', 'to': 'Fear',
                'description': '趋势瓦解 → 乐观转向恐惧'
            },
        }
        return behavior_psych_map.get(behavior_name, {
            'from': 'Unknown', 'to': 'Unknown',
            'description': '未知情绪变化'
        })

    def get_emotion_improvement(self, days_ago=5):
        """
        检测情绪是否在改善

        用于"价格+情绪双确认"——
        Behavior信号 + Emotion Improvement → 提升Confidence

        Returns:
            improving: bool
            magnitude: float (改善幅度，-1到1)
        """
        if len(self.state_history) < days_ago + 1:
            return False, 0

        state_order = {'Panic': 0, 'Fear': 1, 'Hope': 2, 'Optimism': 3, 'Euphoria': 4, 'Exhaustion': 5}

        old_state = self.state_history[-days_ago - 1][1]
        new_state = self.current_state

        old_rank = state_order.get(old_state, 2)
        new_rank = state_order.get(new_state, 2)

        rank_change = new_rank - old_rank
        magnitude = np.clip(rank_change / 3.0, -1, 1)

        return rank_change > 0, magnitude


if __name__ == "__main__":
    # 演示
    eb = EmotionBuilder(method='weighted')

    # 模拟特征
    mock_features = {
        'volume_ratio': 1.2, 'turnover_ratio': 1.1,
        'return_5d': 0.02, 'return_20d': 0.05,
        'rsi': 0.62, 'z_score': 0.8, 'adx': 0.5,
        'volatility': 0.15, 'price_position': 0.65,
        'ma20_deviation': 0.02, 'acceleration': 0.01,
        'margin_balance_chg': 0.01, 'etf_flow': 0.02,
        'north_bound': 0.03, 'advance_decline_ratio': 0.6,
        'new_high_low_ratio': 0.55,
    }

    score = eb._weighted_fusion(mock_features)
    state = eb.map_to_emotion_state(score)
    print(f"Emotion Score: {score:.1f} → State: {state}")
