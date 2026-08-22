"""
P6.6 优化器目标函数重构验证
===========================
验证目标：
1. benchmark_beating_objective 数值正确性（手算对照）
2. OBJECTIVE_REGISTRY / _resolve_objective_fn / _apply_objective 接线正确：
   - 无 env 时回退旧默认（行为与旧版一致）
   - env='benchmark_beating' 时切换到新目标函数
3. EXCESS_SEARCH_SPACE v2 = 31 维且参数均在 config 可注入（setattr 生效）
4. backtest results 含 benchmark_max_drawdown 字段（负值，用于目标函数）
5. smoke：light-excess 小样本跑通（env 注入 + 空间切换 + 目标函数端到端）

用法：
  python tools/p6_6_verify.py [--smoke N]     # N = smoke 试验数，默认 20
退出码：0=全部通过；1=存在失败
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))

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


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_objective_math():
    """验证 1：benchmark_beating_objective 数值手算对照"""
    print("\n[1] benchmark_beating_objective 数值正确性")
    from param_optimizer import benchmark_beating_objective as bb

    # 场景A：正超额 + 回撤刚好等于目标(基准50%)
    # excess=0.10, bench_dd=0.28, strat_dd=0.14, sharpe=1.0, trades=10
    s = bb(excess_return=0.10, benchmark_max_drawdown=-0.28, max_drawdown=-0.14,
           sharpe_ratio=1.0, total_trades=10)
    exp = 0.10 * 150 + ((0.28 - 0.14) / 0.28) * 80 + min(1.0 * 8, 15) + 0
    check("场景A（正超额+回撤达标）", approx(s, exp), f"got={s} exp={exp}")

    # 场景B：负超额 + 回撤超标（DD_TARGET 重罚）
    # excess=-0.02, bench_dd=0.28, strat_dd=0.20 → dd_target=0.14 → 罚 (0.20-0.14)*200
    s = bb(excess_return=-0.02, benchmark_max_drawdown=-0.28, max_drawdown=-0.20,
           sharpe_ratio=0.0, total_trades=10)
    dd_imp = (0.28 - 0.20) / 0.28
    exp = -0.02 * 150 + dd_imp * 80 - (0.20 - 0.14) * 200
    check("场景B（负超额+回撤超标重罚）", approx(s, exp), f"got={s} exp={exp}")

    # 场景C：换手软约束（trades=50 > TRADE_CAP=40）
    s = bb(excess_return=0.05, benchmark_max_drawdown=-0.28, max_drawdown=-0.10,
           sharpe_ratio=0.0, total_trades=50)
    exp = 0.05 * 150 + ((0.28 - 0.10) / 0.28) * 80 - (50 - 40) * 2
    check("场景C（换手超 TRADE_CAP 扣分）", approx(s, exp), f"got={s} exp={exp}")

    # 场景D：基准无回撤的极端情形（bench_dd=0，策略也无回撤 → 满改善）
    s = bb(excess_return=0.0, benchmark_max_drawdown=0.0, max_drawdown=0.0,
           sharpe_ratio=0.0, total_trades=0)
    exp = 0 + 1.0 * 80 + 0 + 0
    check("场景D（基准无回撤→满改善）", approx(s, exp), f"got={s} exp={exp}")


def test_registry_and_helpers():
    """验证 2：注册表 + env 解析 + 默认回退"""
    print("\n[2] OBJECTIVE_REGISTRY / env 解析 / 默认回退")
    import param_optimizer as po

    # 注册表三键齐全
    check("注册表含 3 个目标函数",
          set(po.OBJECTIVE_REGISTRY.keys()) == {
              'composite', 'returns_aggressive', 'benchmark_beating'})

    # 无 env → 解析返回 None（沿用旧默认）
    os.environ.pop(po._OBJECTIVE_ENV_KEY, None)
    check("无 env 时 _resolve_objective_fn 返回 None (回退旧默认)",
          po._resolve_objective_fn() is None)

    # env=benchmark_beating → 返回新目标函数
    os.environ[po._OBJECTIVE_ENV_KEY] = 'benchmark_beating'
    check("env=benchmark_beating 解析到新目标函数",
          po._resolve_objective_fn() is po.benchmark_beating_objective)

    # env=未知 key → 返回 None（安全回退）
    os.environ[po._OBJECTIVE_ENV_KEY] = 'not_exist'
    check("env=未知 key 时安全返回 None",
          po._resolve_objective_fn() is None)
    os.environ.pop(po._OBJECTIVE_ENV_KEY, None)

    # _apply_objective：无 env 时用默认函数；env 注入时用新函数
    r = po.TrialResult(
        params={}, excess_return=0.02, benchmark_max_drawdown=-0.20,
        max_drawdown=-0.10, sharpe_ratio=1.0, total_trades=5,
        strategy_return=0.30, annualized_return=0.25,
    )
    _ = po._apply_objective(r, po.returns_aggressive_objective)
    check("无 env 时 _apply_objective 用默认函数 (returns_aggressive)",
          approx(r.objective, po.returns_aggressive_objective(**po.vars_for_obj(r))))

    os.environ[po._OBJECTIVE_ENV_KEY] = 'benchmark_beating'
    _ = po._apply_objective(r, po.returns_aggressive_objective)
    check("env 注入时 _apply_objective 切到 benchmark_beating",
          approx(r.objective, po.benchmark_beating_objective(**po.vars_for_obj(r))))
    os.environ.pop(po._OBJECTIVE_ENV_KEY, None)


def test_excess_space():
    """验证 3：EXCESS_SEARCH_SPACE v2 维度 + 参数可注入"""
    print("\n[3] EXCESS_SEARCH_SPACE v2 维度与可注入性")
    import config
    import param_optimizer as po

    space = po.EXCESS_SEARCH_SPACE
    check("搜索空间 v2 = 31 维", len(space) == 31, f"got={len(space)}")

    # 新增 11 个 V7 参数齐全
    v7_new = {'CENTER_BULL', 'CENTER_RANGE', 'CENTER_BEAR', 'TARGET_VOL',
              'OVERHEAT_CENTER_MULT', 'BOTTOMFISHING_BOOST', 'STOP_LOSS_PCT',
              'STOP_LOSS_HARD', 'TAKE_PROFIT_T1', 'TAKE_PROFIT_T2',
              'TRAIL_EXIT_DRAWDOWN'}
    check("新增 11 个 V7 参数在空间内", v7_new.issubset(space.keys()),
          f"missing={v7_new - set(space.keys())}")

    # 已移除的参数不在空间内
    removed = {'MAX_POSITION', 'INITIAL_POSITION', 'BULL_SELL_DIV',
               'RANGE_BUY_MULT', 'RANGE_SELL_DIV', 'BEAR_SELL_DIV'}
    check("被移除的 6 个参数不在空间内", removed.isdisjoint(space.keys()),
          f"still-present={removed & set(space.keys())}")

    # 全部参数在 config 中可注入或存在专用 builder
    missing = [k for k in space if not hasattr(config, k) and k not in ('BULL_BUY_MULT', 'BEAR_BUY_MULT')]
    check("空间内参数均可被优化器注入（BULL_BUY_MULT/BEAR_BUY_MULT 由 _build_regime_from_params 处理）", not missing,
          f"missing={missing}")

    # 保留旧参数计数 = 31 - 11 = 20
    old_keys = set(space.keys()) - v7_new
    check("保留旧参数 20 个", len(old_keys) == 20, f"got={len(old_keys)}")


def test_backtest_field():
    """验证 4：backtest results 含 benchmark_max_drawdown"""
    print("\n[4] backtest 输出 benchmark_max_drawdown 字段")
    import config
    config.activate_profile('589800')
    config.STRATEGY_MODE = 'V7'

    from data_updater import load_data_from_db
    from strategy import V6Strategy
    from backtest import V6Backtest

    df = load_data_from_db()
    strategy = V6Strategy(use_ml=True)
    signals = strategy.run(df)
    bt = V6Backtest(df, strategy=strategy)
    results = bt.run(signals)

    has_field = 'benchmark_max_drawdown' in results
    check("results 含 benchmark_max_drawdown", has_field)
    if has_field:
        bdd = results['benchmark_max_drawdown']
        # benchmark 峰谷回撤应为负值
        check("benchmark_max_drawdown 为负值", bdd < 0, f"got={bdd}")
        # 与基准价格序列峰谷回撤自洽（策略回撤上限参考）
        print(f"    基准最大回撤 = {bdd*100:.2f}% | 策略最大回撤 = {results['max_drawdown']*100:.2f}%")


def test_smoke_light_excess(n_trials=20):
    """验证 5：light-excess 小样本端到端跑通"""
    print(f"\n[5] light-excess smoke（{n_trials} trials）")
    import param_optimizer as po

    for code in ['589800', '563360']:
        import config
        config.activate_profile(code)
        print(f"  [{code}] 运行 light-excess, {n_trials} trials ...")
        try:
            results = po.run_light_optimization(
                n_trials=n_trials, n_jobs=4, output_path=None,
                verbose=False, objective='benchmark_beating')
        except Exception as e:
            check(f"{code} light-excess 跑通", False, f"异常: {e}")
            continue

        ok = bool(results)
        check(f"{code} light-excess 返回有效结果", ok)
        if not ok:
            continue
        # 最优参数的键应与 EXCESS_SEARCH_SPACE 一致（证明空间切换生效）
        best = results[0]
        space_keys = set(po.EXCESS_SEARCH_SPACE.keys())
        param_keys = set(best.params.keys())
        check(f"{code} 采样参数与 v2 空间一致", param_keys == space_keys,
              f"diff={param_keys ^ space_keys}")
        print(f"    Best Obj={best.objective:.2f} | "
              f"超额={best.excess_return*100:+.2f}pp | "
              f"回撤={best.max_drawdown*100:.2f}% | 基准回撤={best.benchmark_max_drawdown*100:.2f}%")


def main():
    n_smoke = 20
    if '--smoke' in sys.argv:
        n_smoke = int(sys.argv[sys.argv.index('--smoke') + 1])

    test_objective_math()
    test_registry_and_helpers()
    test_excess_space()
    test_backtest_field()
    test_smoke_light_excess(n_smoke)

    print("\n" + "=" * 60)
    print(f"P6.6 优化器目标函数重构验证: {PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("失败项:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("P6.6 验证全部通过 ✅")
    print("=" * 60)


if __name__ == '__main__':
    main()