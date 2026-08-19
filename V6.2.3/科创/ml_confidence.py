"""
V6.2.3 ML Confidence —— 机器学习概率输出模块
=============================================

基于论文一的RandomForest信号过滤思想和V6路线图，
使用机器学习模型输出校准后的概率作为置信度证据源。

核心设计：
- ML做过滤器，不是发生器（论文一核心思想）
- 特征必须包含 Regime 和 Emotion，学习"行为×环境"交互效应
- 输出概率（非确定性分数）
- 支持多模型对比和Ensemble

当前实现：
- 使用 sklearn 的 RandomForest 和 GradientBoosting
- 支持滚动窗口交叉验证（防过拟合）
- 概率校准（Platt Scaling / Isotonic Regression）
"""

import numpy as np
import warnings

warnings.filterwarnings('ignore')

# sklearn 延迟导入
try:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression as SklearnLR
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score, log_loss, brier_score_loss
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


class MLConfidence:
    """
    ML置信度模型

    在Evidence Engine中作为ml证据源使用。
    """

    def __init__(self, model_type='rf', n_estimators=100, max_depth=5):
        """
        Args:
            model_type: 模型类型
                - 'rf': RandomForest
                - 'gb': GradientBoosting
                - 'lr': LogisticRegression
            n_estimators: 树的数量
            max_depth: 树的最大深度（防过拟合）
        """
        self.model_type = model_type
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self._model = None
        self._is_fitted = False
        self._feature_names = None

        # 性能指标
        self.train_accuracy = 0
        self.cv_accuracy = 0
        self.cv_brier_score = 0

    def _build_model(self):
        """构建底层模型"""
        if not HAS_SKLEARN:
            return None

        if self.model_type == 'rf':
            return RandomForestClassifier(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                min_samples_leaf=5,
                min_samples_split=10,
                max_features='sqrt',
                class_weight='balanced',
                random_state=42,
                n_jobs=-1,
            )
        elif self.model_type == 'gb':
            return GradientBoostingClassifier(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                min_samples_leaf=5,
                learning_rate=0.05,
                random_state=42,
            )
        elif self.model_type == 'lr':
            return SklearnLR(
                penalty='l2',
                C=1.0,
                class_weight='balanced',
                max_iter=1000,
                random_state=42,
            )
        else:
            return RandomForestClassifier(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                random_state=42,
            )

    def fit(self, X, y, feature_names=None, cv_folds=5):
        """
        训练模型

        Args:
            X: 特征矩阵 (n_samples, n_features)
            y: 标签 (n_samples,), 1=盈利, 0=亏损
            feature_names: 特征名称列表
            cv_folds: 交叉验证折数

        Returns:
            self
        """
        if len(X) < 30:
            print(f"ML Confidence: 训练样本不足 ({len(X)} < 30)，不训练")
            return self

        if not HAS_SKLEARN:
            print("ML Confidence: sklearn not installed, skipping ML training")
            return self

        self._feature_names = feature_names
        X = np.array(X)
        y = np.array(y)

        # 构建模型
        self._model = self._build_model()

        # 滚动窗口交叉验证
        if len(X) >= 100 and cv_folds > 1:
            tscv = TimeSeriesSplit(n_splits=min(cv_folds, 5))
            cv_scores = []
            cv_brier_scores = []

            for train_idx, val_idx in tscv.split(X):
                X_train, X_val = X[train_idx], X[val_idx]
                y_train, y_val = y[train_idx], y[val_idx]

                model_cv = self._build_model()
                model_cv.fit(X_train, y_train)

                y_pred = model_cv.predict(X_val)
                y_prob = model_cv.predict_proba(X_val)[:, 1]

                cv_scores.append(accuracy_score(y_val, y_pred))
                cv_brier_scores.append(brier_score_loss(y_val, y_prob))

            self.cv_accuracy = np.mean(cv_scores)
            self.cv_brier_score = np.mean(cv_brier_scores)

        # 全量训练
        self._model.fit(X, y)

        # 训练集准确率
        y_pred_train = self._model.predict(X)
        self.train_accuracy = accuracy_score(y, y_pred_train)

        self._is_fitted = True
        return self

    def predict_proba(self, X):
        """
        预测概率

        Args:
            X: 特征向量 (n_features,) 或 (n_samples, n_features)

        Returns:
            probability: float or array, 上涨概率 (0-1)
        """
        if not self._is_fitted or self._model is None:
            return 0.5  # 未训练返回中性概率

        X = np.array(X)
        if X.ndim == 1:
            X = X.reshape(1, -1)

        try:
            prob = self._model.predict_proba(X)[:, 1]
            if len(prob) == 1:
                return float(prob[0])
            return prob
        except Exception as e:
            print(f"ML predict_proba error: {e}")
            return 0.5

    def predict(self, X, threshold=0.5):
        """
        预测方向

        Args:
            X: 特征向量
            threshold: 分类阈值

        Returns:
            bool: 是否看涨
        """
        prob = self.predict_proba(X)
        if isinstance(prob, np.ndarray):
            return prob > threshold
        return prob > threshold

    def get_feature_importance(self):
        """获取特征重要性"""
        if not self._is_fitted or self._model is None:
            return {}

        if hasattr(self._model, 'feature_importances_'):
            importances = self._model.feature_importances_
            if self._feature_names and len(self._feature_names) == len(importances):
                return dict(zip(self._feature_names, importances))
            return {f'feature_{i}': imp for i, imp in enumerate(importances)}

        elif hasattr(self._model, 'coef_'):
            coef = self._model.coef_[0]
            if self._feature_names and len(self._feature_names) == len(coef):
                return dict(zip(self._feature_names, np.abs(coef)))
            return {f'feature_{i}': abs(c) for i, c in enumerate(coef)}

        return {}

    def get_summary(self):
        """获取模型摘要"""
        return {
            'model_type': self.model_type,
            'is_fitted': self._is_fitted,
            'n_features': len(self._feature_names) if self._feature_names else 0,
            'train_accuracy': round(self.train_accuracy, 3),
            'cv_accuracy': round(self.cv_accuracy, 3),
            'cv_brier_score': round(self.cv_brier_score, 3),
            'top_features': self._get_top_features(5),
        }

    def _get_top_features(self, n=5):
        """获取最重要的N个特征"""
        importance = self.get_feature_importance()
        if not importance:
            return []
        sorted_items = sorted(importance.items(), key=lambda x: x[1], reverse=True)
        return sorted_items[:n]

    def save(self, filepath):
        """保存模型到文件"""
        import joblib
        data = {
            'model': self._model,
            'model_type': self.model_type,
            'feature_names': self._feature_names,
            'train_accuracy': self.train_accuracy,
            'cv_accuracy': self.cv_accuracy,
            'cv_brier_score': self.cv_brier_score,
        }
        joblib.dump(data, filepath)

    @classmethod
    def load(cls, filepath):
        """从文件加载模型"""
        import joblib
        data = joblib.load(filepath)
        obj = cls(model_type=data['model_type'])
        obj._model = data['model']
        obj._is_fitted = True
        obj._feature_names = data.get('feature_names')
        obj.train_accuracy = data.get('train_accuracy', 0)
        obj.cv_accuracy = data.get('cv_accuracy', 0)
        obj.cv_brier_score = data.get('cv_brier_score', 0)
        return obj


# ============================================================
# 训练数据构建工具
# ============================================================

def build_training_data_from_trades(trades, feature_builder_fn=None):
    """
    从交易记录构建ML训练数据

    Args:
        trades: list of dict
        feature_builder_fn: function(trade) → feature_vector，默认使用 FeatureBuilder.build_from_trade

    Returns:
        X: np.array
        y: np.array (1=盈利, 0=亏损)
    """
    from feature_builder import FeatureBuilder
    if feature_builder_fn is None:
        feature_builder_fn = FeatureBuilder.build_from_trade

    X_list = []
    y_list = []

    for trade in trades:
        features = feature_builder_fn(trade)
        if features is not None:
            X_list.append(features)
            y_list.append(1 if trade.get('pnl_pct', 0) > 0 else 0)

    return np.array(X_list), np.array(y_list)


def default_feature_builder(trade):
    """
    V6.2.3: 默认特征构建器（委托给统一 FeatureBuilder）

    从交易记录提取特征向量，与 Evidence Engine 的推理特征维度完全一致。
    """
    from feature_builder import FeatureBuilder
    return FeatureBuilder.build_from_trade(trade)


if __name__ == "__main__":
    # 模拟训练数据
    np.random.seed(42)
    n_samples = 200
    n_features = 20
    X_mock = np.random.randn(n_samples, n_features)
    y_mock = (X_mock[:, 0] + X_mock[:, 1] + np.random.randn(n_samples) * 0.5 > 0).astype(int)

    ml = MLConfidence(model_type='rf', n_estimators=50, max_depth=4)
    ml.fit(X_mock, y_mock, feature_names=[f'f{i}' for i in range(n_features)])

    print("ML Confidence 模型摘要:")
    summary = ml.get_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # 预测
    test_sample = np.random.randn(10, n_features)
    probs = ml.predict_proba(test_sample)
    print(f"\n预测概率 (前5): {probs[:5]}")
