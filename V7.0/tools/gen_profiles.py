"""
V7.0 Profile 生成器
=====================
从 V6.2.3 旧版 config.py 程序化导出标的策略决策参数到 profiles/{code}.json。

设计原则：
- 严禁手抄参数 → 直接 import 旧版 config 模块，读者值程序化序列化，保证字节级一致（复现性根因）。
- Profile 键名为 config 常量名（一一对应），activate_profile(code) 通过 setattr 应用到 config 模块。

用法：
  python tools/gen_profiles.py            # 重新生成 profiles（589800 / 563360 / _default）
  python tools/gen_profiles.py --dump   # 仅打印对比，不写文件
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_DIR = os.path.join(ROOT, '..', 'V6.2.3')
PROFILES_DIR = os.path.join(ROOT, 'profiles')

# ---------------------------------------------------------------------------
# Profile 决策参数 schema（键名 == config 模块常量名，A500 基线为默认值来源）
# ---------------------------------------------------------------------------
STRATEGY_KEYS = [
    # A组：评分公式权重
    'SCORE_BEHAVIOR_WEIGHT', 'SCORE_CONFIDENCE_WEIGHT',
    'SCORE_REWARD_WEIGHT', 'SCORE_RISK_WEIGHT',
    # 市场状态权重（嵌套 dict，config 原生结构）
    'REGIME_WEIGHTS',
    # 买卖评分 → 仓位映射
    'BUY_SCORE_THRESHOLDS', 'SELL_SCORE_THRESHOLDS',
    # 交易执行
    'MAX_POSITION', 'INITIAL_POSITION', 'MIN_HOLD_DAYS',
    'TRADE_TARGET_DELTA', 'TRADE_ACTUAL_DELTA', 'SCORE_HOLD_ZONE',
    'MIN_TRADE_VALUE', 'SLIPPAGE', 'COMMISSION',
    # 观察窗口 / 置信度
    'OBSERVATION_WINDOW_MAX', 'CONFIRMATION_THRESHOLD',
    'CONFIDENCE_INCREMENT', 'EXPIRY_THRESHOLD',
    # 行为检测阈值（D 组）
    'DOUBLE_BOTTOM_REBOUND_MIN', 'DOUBLE_BOTTOM_SCORE',
    'FALSE_BREAK_BREAK_DIST', 'FALSE_BREAK_SCORE',
    'MOMO_EXH_RETURN_THRESHOLD', 'MOMO_EXH_ACCEL_DECLINE', 'MOMO_EXH_SCORE',
    'PULLBACK_MA_DIST', 'PULLBACK_SCORE',
    'PANIC_SELL_DROP_THRESHOLD', 'PANIC_SELL_SCORE',
    'TREND_FAIL_MA_SLOPE_NEG', 'TREND_FAIL_SCORE',
    'BREAKOUT_CONFIRM_DAYS', 'BREAKOUT_SCORE',
    'RSI_OVERBOUGHT_THRESHOLD', 'RSI_OVERBOUGHT_SCORE',
    # 情绪修正系数
    'PSYCH_PANIC_BUY_BOOST', 'PSYCH_EUPHORIA_SELL_BOOST',
    'PSYCH_EUPHORIA_BUY_CUT', 'PSYCH_EXHAUSTION_SELL_BOOST',
    # 辅助评分因子
    'AUX_MA20_TURNING_UP', 'AUX_ADX_BULL_SUPPORT', 'AUX_VOLUME_SUPPORT',
    'AUX_RSI_OVERSOLD_REBOUND', 'AUX_DIVERGENCE_BULL',
    'AUX_RSI_BEAR_DIVERGENCE', 'AUX_VOLUME_DECLINE',
    'AUX_MA5_MA10_DEAD_CROSS', 'AUX_ZSCORE_EXTREME',
    # 牛市止盈后重新入场
    'BULL_REENTRY_ENABLED', 'BULL_REENTRY_WINDOW', 'BULL_REENTRY_BUY_BOOST',
    'BULL_REENTRY_SCORE_DROP', 'BULL_REENTRY_SCORE_LOW',
    # Evidence Engine
    'EVIDENCE_WEIGHTS', 'EVIDENCE_ENABLE_REPLAY', 'EVIDENCE_ENABLE_ML',
    'EVIDENCE_ENABLE_EMOTION_BONUS',
    # Replay Learning
    'REPLAY_WINDOW_DAYS', 'REPLAY_MIN_SAMPLES', 'REPLAY_MAX_AGE_DAYS',
    'REPLAY_TAU_DAYS', 'REPLAY_LAPLACE_ALPHA', 'REPLAY_HIGH_SUCCESS',
    'REPLAY_LOW_SUCCESS', 'REPLAY_CONSECUTIVE_FAIL',
    # Time Decay
    'TIME_DECAY_GRACE_PERIOD', 'TIME_DECAY_TAU',
    'TIME_DECAY_MIN_MULTIPLIER', 'TIME_DECAY_MIN_CONFIDENCE',
    # ML Confidence
    'ML_MODEL_TYPE', 'ML_N_ESTIMATORS', 'ML_MAX_DEPTH',
    'ML_MIN_TRAINING_SAMPLES', 'ML_CV_FOLDS', 'ML_RETRAIN_FREQUENCY',
    # Probability Calibration
    'CALIBRATION_METHOD', 'CALIBRATION_MIN_SAMPLES', 'CALIBRATION_AUTO_DISABLE',
]

# 标的信息（代码 → (名称, 市场前缀, 是否已优化)）
INSTRUMENTS = {
    '589800': ('科创综指 ETF', 'sh', True),
    '563360': ('中证 A500 ETF', 'sh', True),
}


def load_module(name, path):
    """从文件路径加载 python 模块（不依赖 sys.path）"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def collect_params(mod):
    """收集 STRATEGY_KEYS 中在模块里存在的参数值（JSON 可序列化）"""
    out = {}
    for k in STRATEGY_KEYS:
        if hasattr(mod, k):
            out[k] = getattr(mod, k)
    return out


def dump(profile_589, profile_563):
    """打印两标的参数对比（仅差异行）"""
    print("=" * 70)
    print(f"{'参数':<34}{'589800(科创)':>22}{'563360(A500)':>22}")
    print("=" * 70)
    all_keys = set(profile_589) | set(profile_563)
    for k in sorted(all_keys):
        v1 = profile_589.get(k)
        v2 = profile_563.get(k)
        if v1 == v2:
            continue
        print(f"{k:<34}{str(v1):>22}{str(v2):>22}")
    print(f"\n共 {len(all_keys)} 个策略参数；其中差异 {sum(1 for k in all_keys if profile_589.get(k) != profile_563.get(k))} 个")
    print(f"（以上差异应与 V6.2.3 两版 config.py 的 diff 一致：BUY/SELL阈值、MAX_POSITION、"
          f"INITIAL_POSITION、MIN_HOLD_DAYS、TRADE_TARGET/ACTUAL_DELTA、SCORE_HOLD_ZONE）")


def build_identity(code, name, market, optimized):
    return {
        'code': code,
        'name': name,
        'market': market,
        'optimized': optimized,
    }


def main():
    kc_config = load_module('kc_config', os.path.join(OLD_DIR, '科创', 'config.py'))
    a5_config = load_module('a5_config', os.path.join(OLD_DIR, 'A500', 'config.py'))

    n589, m589, o589 = INSTRUMENTS['589800']
    n563, m563, o563 = INSTRUMENTS['563360']
    p589 = {**build_identity('589800', n589, m589, o589), **collect_params(kc_config)}
    p563 = {**build_identity('563360', n563, m563, o563), **collect_params(a5_config)}

    if '--dump' in sys.argv:
        dump(p589, p563)
        return

    os.makedirs(PROFILES_DIR, exist_ok=True)
    for code, prof in (('589800', p589), ('563360', p563)):
        path = os.path.join(PROFILES_DIR, f'{code}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
        print(f"已生成 {path}  ({len(prof)} 键)")

    # 冷启动默认 = 563360(A500) 基线策略参数 + DEFAULT 身份（GUI 标记“未优化”）
    p_default = {
        'code': 'DEFAULT',
        'name': '未优化默认参数',
        'market': '',
        'optimized': False,
    }
    for k in STRATEGY_KEYS:
        if k in p563:
            p_default[k] = p563[k]
    default_path = os.path.join(PROFILES_DIR, '_default.json')
    with open(default_path, 'w', encoding='utf-8') as f:
        json.dump(p_default, f, ensure_ascii=False, indent=2)
    print(f"已生成 {default_path}  ({len(p_default)} 键)")


if __name__ == '__main__':
    main()