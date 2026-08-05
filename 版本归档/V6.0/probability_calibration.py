"""
V6.0 Probability Calibration —— 概率校准模块
===============================================

解决ML输出的原始概率不可靠问题：
- 模型预测80%上涨，实际可能只有60%
- 校准后确保预测概率与实际频率一致

方法：
- Platt Scaling (sigmoid calibration): 适用于小样本
- Isotonic Regression: 适用于大样本，更灵活
- Temperature Scaling: 简单实用，不改变排序

使用方式：
1. 用训练集训练ML模型
2. 用独立校准集拟合校准器
3. 校准时使用calibrate()包装ML的predict_proba输出
"""

import numpy as np

# sklearn 延迟导入（仅在校准器实际使用时需要）
try:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


class ProbabilityCalibrator:
    """
    概率校准器

    确保 ML 模型输出的概率与实际成功率一致。
    例如：预测"80%上涨"的事件中，实际应有约80%上涨。
    """

    def __init__(self, method='platt'):
        """
        Args:
            method: 校准方法
                - 'platt': Platt Scaling (Sigmoid), 推荐用于小样本
                - 'isotonic': Isotonic Regression, 推荐用于大样本(>1000)
                - 'temperature': Temperature Scaling, 简单实用
        """
        self.method = method
        self._calibrator = None
        self._temperature = 1.0
        self._is_fitted = False

        # 校准前后对比
        self.brier_before = None
        self.brier_after = None

    def fit(self, y_true, y_prob_raw):
        """
        拟合校准器

        Args:
            y_true: 真实标签 (0/1)
            y_prob_raw: ML模型原始预测概率
        """
        if not HAS_SKLEARN:
            print("ProbabilityCalibrator: sklearn not installed, skipping calibration")
            return self

        y_true = np.array(y_true).ravel()
        y_prob_raw = np.array(y_prob_raw).ravel()

        # 评估校准前Brier分数
        self.brier_before = brier_score_loss(y_true, y_prob_raw)

        if self.method == 'platt':
            # Platt Scaling: 用Logistic回归学习sigmoid映射
            # 输入=logit(raw_prob), 输出=calibrated_prob
            eps = 1e-12
            y_prob_clipped = np.clip(y_prob_raw, eps, 1 - eps)
            logits = np.log(y_prob_clipped / (1 - y_prob_clipped))

            self._calibrator = LogisticRegression(
                penalty='l2', C=1.0, solver='lbfgs', max_iter=1000
            )
            self._calibrator.fit(logits.reshape(-1, 1), y_true)

        elif self.method == 'isotonic':
            # Isotonic Regression: 非参数保序回归
            self._calibrator = IsotonicRegression(
                y_min=0.0, y_max=1.0, out_of_bounds='clip'
            )
            self._calibrator.fit(y_prob_raw, y_true)

        elif self.method == 'temperature':
            # Temperature Scaling: 找到最优温度参数
            self._temperature = self._find_optimal_temperature(
                y_true, y_prob_raw
            )

        self._is_fitted = True

        # 评估校准后Brier分数
        calibrated = self.calibrate(y_prob_raw)
        self.brier_after = brier_score_loss(y_true, calibrated)

        return self

    def calibrate(self, y_prob_raw):
        """
        校准概率

        Args:
            y_prob_raw: ML模型原始预测概率

        Returns:
            calibrated_prob: 校准后的概率
        """
        if not self._is_fitted:
            return np.array(y_prob_raw)

        y_prob_raw = np.array(y_prob_raw)
        is_scalar = y_prob_raw.ndim == 0
        if is_scalar:
            y_prob_raw = np.array([y_prob_raw])

        if self.method == 'platt':
            eps = 1e-12
            y_prob_clipped = np.clip(y_prob_raw, eps, 1 - eps)
            logits = np.log(y_prob_clipped / (1 - y_prob_clipped))
            calibrated = self._calibrator.predict_proba(logits.reshape(-1, 1))[:, 1]

        elif self.method == 'isotonic':
            calibrated = self._calibrator.transform(y_prob_raw)

        elif self.method == 'temperature':
            eps = 1e-12
            y_prob_clipped = np.clip(y_prob_raw, eps, 1 - eps)
            logits = np.log(y_prob_clipped / (1 - y_prob_clipped))
            calibrated = 1.0 / (1.0 + np.exp(-logits / self._temperature))

        else:
            calibrated = y_prob_raw

        if is_scalar:
            return float(calibrated[0])
        return calibrated

    def _find_optimal_temperature(self, y_true, y_prob_raw):
        """二分搜索最优温度"""
        eps = 1e-12
        y_prob_clipped = np.clip(y_prob_raw, eps, 1 - eps)
        logits = np.log(y_prob_clipped / (1 - y_prob_clipped))

        best_temp = 1.0
        best_loss = float('inf')

        for temp in np.linspace(0.5, 3.0, 50):
            calibrated = 1.0 / (1.0 + np.exp(-logits / temp))
            loss = brier_score_loss(y_true, calibrated)
            if loss < best_loss:
                best_loss = loss
                best_temp = temp

        return best_temp

    def get_reliability_curve(self, y_true, y_prob_raw, n_bins=10):
        """
        生成可靠性曲线数据

        Returns:
            bins: list of (bin_center, observed_frequency, predicted_avg, count)
        """
        y_true = np.array(y_true).ravel()
        y_prob = self.calibrate(y_prob_raw) if self._is_fitted else np.array(y_prob_raw).ravel()

        bin_edges = np.linspace(0, 1, n_bins + 1)
        curve = []

        for i in range(n_bins):
            mask = (y_prob >= bin_edges[i]) & (y_prob < bin_edges[i + 1])
            count = np.sum(mask)
            if count > 0:
                observed = np.mean(y_true[mask])
                predicted = np.mean(y_prob[mask])
                center = (bin_edges[i] + bin_edges[i + 1]) / 2
                curve.append({
                    'bin_center': round(center, 2),
                    'observed': round(observed, 3),
                    'predicted': round(predicted, 3),
                    'count': int(count),
                })

        return curve

    def print_calibration_report(self):
        """打印校准报告"""
        print("\n" + "=" * 50)
        print("Probability Calibration Report")
        print("=" * 50)
        print(f"  Method: {self.method}")
        print(f"  Fitted: {self._is_fitted}")

        if self._is_fitted:
            print(f"  Brier Score (Before): {self.brier_before:.4f}")
            print(f"  Brier Score (After):  {self.brier_after:.4f}")
            improvement = (self.brier_before - self.brier_after) / max(self.brier_before, 1e-10)
            print(f"  Improvement:          {improvement * 100:.1f}%")
            if self.method == 'temperature':
                print(f"  Temperature:          {self._temperature:.3f}")

        print("=" * 50)

    def get_summary(self):
        """获取校准器摘要"""
        return {
            'method': self.method,
            'is_fitted': self._is_fitted,
            'brier_before': round(self.brier_before, 4) if self.brier_before else None,
            'brier_after': round(self.brier_after, 4) if self.brier_after else None,
            'temperature': self._temperature if self.method == 'temperature' else None,
        }


if __name__ == "__main__":
    # 演示：校准前后的Brier分数对比
    np.random.seed(42)
    n = 1000

    # 模拟"不校准"的ML概率（高估）
    y_true = np.random.binomial(1, 0.5, n)
    # 添加噪声：预测概率普遍偏高
    y_prob_raw = np.clip(y_true * 0.6 + 0.15 + np.random.normal(0, 0.1, n), 0.01, 0.99)

    # Platt校准
    cal = ProbabilityCalibrator(method='platt')
    cal.fit(y_true, y_prob_raw)
    cal.print_calibration_report()

    # 可靠性曲线
    curve = cal.get_reliability_curve(y_true, y_prob_raw)
    print("\nReliability Curve:")
    print(f"  {'Bin':>8} {'Observed':>10} {'Predicted':>10} {'Count':>6}")
    for c in curve:
        print(f"  {c['bin_center']:>8.2f} {c['observed']:>10.3f} {c['predicted']:>10.3f} {c['count']:>6}")
