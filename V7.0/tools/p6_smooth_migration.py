"""
P6.3 平滑迁移回归验证
=====================
验证目标：
1. STRATEGY_MODE='V6' 时，回测结果与 baseline_v6.json 完全一致
   （证明 scoring_engine 重写后 V6 分支 = 旧版，未引入手误）
2. V7 分支单元验证：get_center / get_offset / score_to_target_position
   的 center+offset 合成、HOLD 区、相位通道、仓位边界均正确

用法：
  python tools/p6_smooth_migration.py
退出码：0=全部通过；1=存在失败
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))

INSTRUMENTS = ['589800', '563360']

PASS = 0
FAIL = 0
FAILURES = []


def check(desc, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {desc}")
    else:
        FAIL += 1
        FAILURES.append(desc)
        print(f"  ❌ {desc}  {detail}")


def run_backtest_v6(code):
    """STRATEGY_MODE='V6' 下运行回测，返回 results"""
    import config
    config.activate_profile(code)
    config.STRATEGY_MODE = 'V6'

    from data_updater import load_data_from_db
    from strategy import V6Strategy
    from backtest import V6Backtest

    df = load_data_from_db()
    strategy = V6Strategy(use_ml=True)
    signals = strategy.run(df)
    bt = V6Backtest(df, strategy=strategy)
    return bt.run(signals)


def test_v6_regression():
    """验证 1：V6 分支回测 == baseline_v6.json"""
    print("\n[1] V6 分支回归（STRATEGY_MODE='V6' 与 baseline_v6.json 对比）")
    for code in INSTRUMENTS:
        print(f"\n  [{code}]")
        results = run_backtest_v6(code)

        import config
        with open(config.runs_path('baseline_v6.json'), encoding='utf-8') as f:
            baseline = json.load(f)['performance']

        # 对比关键指标（与 export_all 同进位）
        mapping = {
            'strategy_return_pct': results['strategy_return'] * 100,
            'benchmark_return_pct': results['benchmark_return'] * 100,
            'excess_return_pct': results['excess_return'] * 100,
            'max_drawdown_pct': results['max_drawdown'] * 100,
            'annualized_return_pct': results['annualized_return'] * 100,
            'total_trades': results['total_trades'],
            'win_rate_pct': round(results['win_rate'] * 100, 1),  # baseline 存 1 位小数
            'final_equity': results['final_equity'],
        }
        errors = []
        for key, expected in baseline.items():
            if key not in mapping:
                continue
            actual = mapping[key]
            if abs(actual - expected) > 0.01:
                errors.append(f"{key}: baseline={expected} vs V6分支={round(actual, 2)}")
        check(f"{code} V6 分支与基线一致", not errors, '; '.join(errors[:5]))


def test_v7_unit():
    """验证 2：V7 分支单元验证"""
    print("\n[2] V7 分支单元验证（center+offset 合成）")
    import config
    config.activate_profile('589800')
    config.STRATEGY_MODE = 'V7'
    from scoring_engine import get_center, get_offset, score_to_target_position

    # 2.1 center 映射
    check("center: Bull=0.75", abs(get_center('Bull') - 0.75) < 1e-9, f"{get_center('Bull')}")
    check("center: Range=0.45", abs(get_center('Range') - 0.45) < 1e-9, f"{get_center('Range')}")
    check("center: Bear=0.15", abs(get_center('Bear') - 0.15) < 1e-9, f"{get_center('Bear')}")
    check("center: Unknown=0.45", abs(get_center('Unknown') - 0.45) < 1e-9, f"{get_center('Unknown')}")

    # 2.2 offset 映射（净评分占优方向）
    check("offset: buy≥68 → +0.20", abs(get_offset(68, 0) - 0.20) < 1e-9, f"{get_offset(68, 0)}")
    check("offset: buy≥62 → +0.15", abs(get_offset(62, 0) - 0.15) < 1e-9, f"{get_offset(62, 0)}")
    check("offset: buy≥56 → +0.10", abs(get_offset(56, 0) - 0.10) < 1e-9, f"{get_offset(56, 0)}")
    check("offset: buy≥50 → +0.05", abs(get_offset(50, 0) - 0.05) < 1e-9, f"{get_offset(50, 0)}")
    check("offset: buy<50 → 0", abs(get_offset(49, 0) - 0.0) < 1e-9, f"{get_offset(49, 0)}")
    check("offset: sell≥68 → -0.25", abs(get_offset(0, 68) - (-0.25)) < 1e-9, f"{get_offset(0, 68)}")
    check("offset: sell≥62 → -0.18", abs(get_offset(0, 62) - (-0.18)) < 1e-9, f"{get_offset(0, 62)}")
    check("offset: sell≥56 → -0.12", abs(get_offset(0, 56) - (-0.12)) < 1e-9, f"{get_offset(0, 56)}")
    check("offset: sell≥50 → -0.06", abs(get_offset(0, 50) - (-0.06)) < 1e-9, f"{get_offset(0, 50)}")
    check("offset: sell<50 → 0", abs(get_offset(0, 49) - 0.0) < 1e-9, f"{get_offset(0, 49)}")

    # 2.3 HOLD 区：净评分在 ±SCORE_HOLD_ZONE 内 → offset=0
    check("offset: 净评分在HOLD区 → 0", abs(get_offset(60, 50) - 0.0) < 1e-9, f"{get_offset(60, 50)}")
    check("offset: 净评分>HOLD → 用buy档", abs(get_offset(60, 40) - 0.10) < 1e-9, f"{get_offset(60, 40)}")
    check("offset: 净评分<-HOLD → 用sell档", abs(get_offset(40, 60) - (-0.12)) < 1e-9, f"{get_offset(40, 60)}")

    # 2.4 target 合成
    t = score_to_target_position(68, 0, 0.8, regime='Bull')   # center=0.75 + 0.20 = 0.95
    check("target: Bull + buy≥68 → 0.95", abs(t - 0.95) < 1e-9, f"{t}")
    t = score_to_target_position(0, 68, 0.8, regime='Bull')   # center=0.75 - 0.25 = 0.50
    check("target: Bull + sell≥68 → 0.50", abs(t - 0.50) < 1e-9, f"{t}")
    t = score_to_target_position(0, 68, 0.8, regime='Bear')   # center=0.15 - 0.25 < 0 → floor 0
    check("target: Bear + sell≥68 → 0（floor 钳制）", abs(t - 0.0) < 1e-9, f"{t}")
    t = score_to_target_position(68, 0, 0.8, regime='Bear')   # center=0.15 + 0.20 = 0.35
    check("target: Bear + buy≥68 → 0.35", abs(t - 0.35) < 1e-9, f"{t}")
    t = score_to_target_position(68, 0, 0.8, regime='Bull', center=0.9)  # 外部传入 center
    check("target: 外部 center=0.9 + 0.20 → 1.0 → MAX_POSITION 钳制", abs(t - 0.98) < 1e-9, f"{t}")

    # 2.5 相位通道
    t = score_to_target_position(0, 0, 0.8, regime='Bear', offset_boost=0.15)  # BottomFishing
    check("target: BottomFishing boost +0.15 → 0.30", abs(t - 0.30) < 1e-9, f"{t}")
    t = score_to_target_position(68, 0, 0.8, regime='Bull', offset_penalty=-0.30)  # Overheat
    check("target: Overheat penalty -0.30 → 0.65", abs(t - 0.65) < 1e-9, f"{t}")
    t = score_to_target_position(0, 0, 0.8, regime='Range')   # HOLD → center 不变
    check("target: Range HOLD → 0.45", abs(t - 0.45) < 1e-9, f"{t}")

    # 2.6 仓位边界
    t = score_to_target_position(68, 0, 0.8, regime='Bull', offset_boost=0.15)  # 0.75+0.20+0.15=1.10
    check("target: 上界钳制到 MAX_POSITION", abs(t - 0.98) < 1e-9, f"{t}")
    t = score_to_target_position(0, 68, 0.8, regime='Bear', offset_penalty=-0.30)  # 0.15-0.25-0.30<0
    check("target: 下界钳制到 POSITION_FLOOR=0", abs(t - 0.0) < 1e-9, f"{t}")


def main():
    test_v6_regression()
    test_v7_unit()

    print("\n" + "=" * 60)
    print(f"P6.3 平滑迁移验证: {PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("失败项:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("P6.3 平滑迁移回归全部通过 ✅")
    print("=" * 60)


if __name__ == '__main__':
    main()
