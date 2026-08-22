"""
P5 验收脚本 —— GUI 优化 API 与 CLI 一致性 + 小 trial 端到端 + 参数写回
=====================================================================

验证内容（对应 P5 开发计划 §3.7）：
1. list_optimize_modes 返回 4 个模式，与 optimizer_modes.MODES 逐项一致
2. get_diagnostics 仓位统计与 CLI p6_baseline.compute_position_stats 一致；
   相关性 ∈ [-1,1]；超额分解恒等式 position+timing+residual ≈ excess
3. get_param_versions 的 rows 与 gen_profiles.STRATEGY_KEYS 全量一致，
   is_new 标记的 V7 新参数正确
4. 端到端小 trial：start_optimization('589800','light-excess',30,cpu_limit=50)
   跑通 → done → get_optimization_results 返回 top20 非空 → 结果 JSON 落盘
5. apply_optimized_params 备份 profile → 应用 → top1 参数键值写入 → 恢复

约束：不触发 heavy 重算（后台 563360 heavy-excess 运行中）；测试 4 用小 n_jobs；
测试 5 必须备份/恢复 profile，不污染线上参数。
"""
import json
import os
import shutil
import sys
import time

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
_QUANT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'quant')
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, _QUANT_DIR)
sys.path.insert(0, _TOOLS_DIR)

from bridge import ApiBridge

ROOT = os.path.dirname(_TOOLS_DIR)
PROFILES_DIR = os.path.join(ROOT, 'profiles')
CODE = '589800'


def wait_optimization(api, task_id, timeout=600):
    """轮询优化任务直到 done/error，返回 (status, result, error)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = api.get_optimization_status(task_id)
        if not res['ok']:
            return 'api_error', None, res.get('error')
        d = res['data']
        if d['status'] in ('done', 'error'):
            return d['status'], d.get('result'), d.get('error')
        time.sleep(2.0)
    return 'timeout', None, '轮询超时'


def test_list_modes():
    print("\n" + "=" * 60)
    print("测试 1: list_optimize_modes 与 MODES 注册表一致")
    print("=" * 60)

    from optimizer_modes import MODES
    api = ApiBridge()
    res = api.list_optimize_modes()
    assert res['ok'], f"list_optimize_modes 失败: {res.get('error')}"
    modes = res['data']

    assert len(modes) == len(MODES), f"模式数量不一致: GUI={len(modes)} vs MODES={len(MODES)}"
    for m, ref in zip(modes, MODES):
        assert m['key'] == ref.key, f"key 不一致: {m['key']} vs {ref.key}"
        assert m['label'] == ref.label, f"label 不一致: {m['label']} vs {ref.label}"
        assert m['default_trials'] == ref.default_trials, \
            f"default_trials 不一致: {m['default_trials']} vs {ref.default_trials}"
        assert m['default_file'] == ref.default_file, \
            f"default_file 不一致: {m['default_file']} vs {ref.default_file}"

    for m in modes:
        print(f"  ✓ {m['key']:<14} {m['label']:<16} trials={m['default_trials']} file={m['default_file']}")
    print(f"  ✅ 全部 {len(modes)} 个模式与注册表一致")
    return True


def test_diagnostics():
    print("\n" + "=" * 60)
    print("测试 2: get_diagnostics 与 CLI 仓位统计一致 + 分解恒等式")
    print("=" * 60)

    api = ApiBridge()
    res = api.get_diagnostics(CODE)
    assert res['ok'], f"get_diagnostics 失败: {res.get('error')}"
    d = res['data']
    assert d.get('available'), f"诊断不可用: {d.get('reason')}"

    # 与 CLI p6_baseline.compute_position_stats 比对
    import p6_baseline
    _, bt = p6_baseline.run_backtest(CODE, 0.001, 0.0003)
    cli_stats = p6_baseline.compute_position_stats(bt)
    gui_stats = d['position_stats']
    for key in ('min', 'max', 'mean', 'pct_ge_90', 'pct_lt_30', 'pct_lt_70'):
        assert abs(float(gui_stats.get(key, -1)) - float(cli_stats[key])) < 1e-6, \
            f"仓位统计 {key} 不一致: CLI={cli_stats[key]} vs GUI={gui_stats.get(key)}"
    print(f"  ✓ 仓位统计与 CLI 一致: [{cli_stats['min']}, {cli_stats['max']}] "
          f"均值={cli_stats['mean']} <30%占{cli_stats['pct_lt_30']}%")

    # 相关性 ∈ [-1, 1]
    corr = d['correlation']['pearson_r']
    assert -1.0 <= corr <= 1.0, f"相关性越界: {corr}"
    print(f"  ✓ 策略-基准相关性 r={corr} ∈ [-1,1]")

    # 超额分解恒等式
    dec = d['excess_decomp']
    total = dec['position_pp'] + dec['timing_pp'] + dec['residual_pp']
    diff = abs(total - dec['excess_pp'])
    assert diff < 0.05, f"分解不闭合: position+timing+residual={total} vs excess={dec['excess_pp']} (差 {diff})"
    print(f"  ✓ 分解恒等式闭合: 仓位={dec['position_pp']} + 择时={dec['timing_pp']} "
          f"+ 残差={dec['residual_pp']} = {total} ≈ 超额={dec['excess_pp']}")
    print(f"  ✓ 绩效: 策略={d['performance']['strategy_return_pct']}% "
          f"基准={d['performance']['benchmark_return_pct']}% "
          f"回撤={d['performance']['max_drawdown_pct']}% 交易={d['performance']['total_trades']}笔")
    return True


def test_param_versions():
    print("\n" + "=" * 60)
    print("测试 3: get_param_versions 与 STRATEGY_KEYS 全量一致 + is_new 标记")
    print("=" * 60)

    import gen_profiles
    api = ApiBridge()
    res = api.get_param_versions(CODE)
    assert res['ok'], f"get_param_versions 失败: {res.get('error')}"
    d = res['data']
    assert d.get('available'), f"参数对比不可用: {d.get('reason')}"

    # 加载旧 config 与当前 profile，重建期望 rows
    import config
    old_dir = os.path.join(ROOT, '..', 'V6.2.3', '科创')
    old = gen_profiles.load_module('old_config_p5', os.path.join(old_dir, 'config.py'))
    profile = config.load_profile(CODE)

    expected_keys = []
    for k in gen_profiles.STRATEGY_KEYS:
        v6 = getattr(old, k, None)
        v7 = profile.get(k)
        if v6 is None and v7 is None:
            continue
        expected_keys.append(k)

    rows = d['rows']
    got_keys = [r['key'] for r in rows]
    assert sorted(got_keys) == sorted(expected_keys), \
        f"rows 键集合不一致:\n  GUI={sorted(got_keys)}\n  期望={sorted(expected_keys)}"
    print(f"  ✓ rows 共 {len(rows)} 项，与 STRATEGY_KEYS 全量一致")

    # is_new 标记校验：键不在旧 config 即为 V7 新增
    new_keys = [r['key'] for r in rows if r['is_new']]
    expected_new = [k for k in expected_keys if not hasattr(old, k)]
    assert sorted(new_keys) == sorted(expected_new), \
        f"is_new 标记不一致:\n  GUI={sorted(new_keys)}\n  期望={sorted(expected_new)}"
    print(f"  ✓ V7 新增参数 {len(new_keys)} 个: {new_keys}")

    # changed 标记校验
    bad_changed = [r['key'] for r in rows
                   if not r['is_new'] and r['changed'] != (r['v6'] != r['v7'])]
    assert not bad_changed, f"changed 标记错误: {bad_changed}"
    print(f"  ✓ changed 标记正确（变更 {d['summary']['changed']} 项）")
    return True


def test_end_to_end_light():
    print("\n" + "=" * 60)
    print("测试 4: 端到端小 trial（light-excess 30 trials, cpu_limit=50）")
    print("=" * 60)

    api = ApiBridge()
    res = api.start_optimization(CODE, 'light-excess', 30, 50)
    assert res['ok'], f"start_optimization 失败: {res.get('error')}"
    task_id = res['data']['task_id']
    print(f"  → 已提交优化任务 {task_id}，等待完成（30 trials）...")

    status, result, error = wait_optimization(api, task_id, timeout=600)
    assert status == 'done', f"优化任务未成功: status={status}, error={error}"
    print(f"  ✓ 任务完成，结果落盘: {result.get('output_path')}")

    # 结果 JSON 落盘校验
    out_path = result.get('output_path', '')
    assert out_path and os.path.exists(out_path), f"结果文件不存在: {out_path}"
    with open(out_path, encoding='utf-8') as f:
        data = json.load(f)
    assert data.get('top20'), "top20 为空"
    print(f"  ✓ 结果文件存在，top20 共 {len(data['top20'])} 条")

    # get_optimization_results 返回 top20 非空
    res2 = api.get_optimization_results(CODE)
    assert res2['ok'], f"get_optimization_results 失败: {res2.get('error')}"
    files = res2['data']
    assert 'light_excess_results.json' in files, "结果文件未出现在 get_optimization_results"
    assert files['light_excess_results.json']['top20'], "top20 为空"
    top1 = files['light_excess_results.json']['top20'][0]
    print(f"  ✓ GUI 读取 top1: objective={top1['objective']} "
          f"收益={top1['return_pct']}% 夏普={top1['sharpe']} 交易={top1['total_trades']}笔")
    return True


def test_apply_params():
    print("\n" + "=" * 60)
    print("测试 5: apply_optimized_params 写回 + 备份恢复")
    print("=" * 60)

    import gen_profiles
    import config
    api = ApiBridge()

    # 备份 profile
    profile_path = os.path.join(PROFILES_DIR, f'{CODE}.json')
    backup_path = profile_path + '.p5bak'
    shutil.copy2(profile_path, backup_path)
    try:
        # 读取 light_excess_results.json 的 top1 params
        results_path = os.path.join(ROOT, 'runs', CODE, 'light_excess_results.json')
        assert os.path.exists(results_path), f"缺少结果文件: {results_path}"
        with open(results_path, encoding='utf-8') as f:
            results = json.load(f)
        top1_params = results['top20'][0]['params']

        res = api.apply_optimized_params(CODE, results_path)
        assert res['ok'], f"apply_optimized_params 失败: {res.get('error')}"
        assert res['data']['applied'], "apply_optimized 返回未应用"
        print(f"  ✓ 应用成功（code={res['data']['code']}）")

        # 校验 profile 中 top1 参数键值写入
        profile = config.load_profile(CODE)
        applied_count = 0
        for k, v in top1_params.items():
            if k in gen_profiles._REGIME_MAPPING:
                regime, weight = gen_profiles._REGIME_MAPPING[k]
                got = profile.get('REGIME_WEIGHTS', {}).get(regime, {}).get(weight)
                assert got == v, f"REGIME_WEIGHTS 参数 {k} 未写入: {got} vs {v}"
                applied_count += 1
            else:
                assert profile.get(k) == v, f"参数 {k} 未写入: {profile.get(k)} vs {v}"
                applied_count += 1
        print(f"  ✓ 校验通过：{applied_count} 个 top1 参数键值已写入 profile")
    finally:
        # 恢复 profile
        shutil.copy2(backup_path, profile_path)
        os.remove(backup_path)
        print(f"  ✓ 已恢复原 profile（{profile_path}）")
    return True


def main():
    print("V7.0 P5 GUI 优化体系验收")
    print("=" * 60)

    all_passed = True
    for name, fn in [
        ('模式注册表', test_list_modes),
        ('优化前诊断', test_diagnostics),
        ('参数版本对比', test_param_versions),
        ('端到端小trial', test_end_to_end_light),
        ('参数写回', test_apply_params),
    ]:
        try:
            if not fn():
                all_passed = False
        except Exception as e:
            print(f"  ❌ [{name}] 测试异常: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("✅ 所有 P5 测试通过！优化体系 GUI 验收合格")
    else:
        print("❌ 部分 P5 测试失败，请检查")
    print("=" * 60)
    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
