"""
P3 集成验证脚本 —— GUI bridge API vs CLI 结果一致性校验
=====================================================

验证内容：
1. bridge 层 API 基本可用性（标的管理/数据/信号）
2. GUI 信号结果 vs CLI --signal 输出一致性
3. 多标的切换正确性
"""
import sys
import os

# 确保路径正确
_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
_QUANT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'quant')
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, _QUANT_DIR)

from bridge import ApiBridge


def test_profile_management():
    """测试标的管理 API"""
    print("\n" + "=" * 60)
    print("测试 1: 标的管理 API")
    print("=" * 60)

    api = ApiBridge()

    # list_profiles
    res = api.list_profiles()
    assert res['ok'], f"list_profiles 失败: {res.get('error')}"
    print(f"  ✓ list_profiles: 找到 {len(res['data'])} 个标的")
    for p in res['data']:
        print(f"    - {p['code']} {p['name']} (优化:{p['optimized']}, DB:{p['has_db']})")

    # get_current_profile
    res = api.get_current_profile()
    assert res['ok'], f"get_current_profile 失败: {res.get('error')}"
    print(f"  ✓ get_current_profile: {res['data']['code']} {res['data']['name']}")

    # switch_profile - 589800
    res = api.switch_profile('589800')
    assert res['ok'], f"switch_profile(589800) 失败: {res.get('error')}"
    assert res['data']['code'] == '589800'
    print(f"  ✓ switch_profile(589800): 切换成功 - {res['data']['name']}")

    # switch_profile - 563360
    res = api.switch_profile('563360')
    assert res['ok'], f"switch_profile(563360) 失败: {res.get('error')}"
    assert res['data']['code'] == '563360'
    print(f"  ✓ switch_profile(563360): 切换成功 - {res['data']['name']}")

    print("  ✅ 标的管理 API 全部通过")


def test_data_api():
    """测试数据管理 API"""
    print("\n" + "=" * 60)
    print("测试 2: 数据管理 API")
    print("=" * 60)

    api = ApiBridge()
    api.switch_profile('589800')

    # get_runtime_context
    res = api.get_runtime_context()
    assert res['ok'], f"get_runtime_context 失败: {res.get('error')}"
    print(f"  ✓ get_runtime_context: {res['data']['description']} @ {res['data']['now']}")

    # get_data_overview
    res = api.get_data_overview()
    assert res['ok'], f"get_data_overview 失败: {res.get('error')}"
    d = res['data']
    print(f"  ✓ get_data_overview:")
    print(f"    DB存在: {d['db_exists']}")
    print(f"    数据范围: {d['start_date']} ~ {d['end_date']}")
    print(f"    数据条数: {d['row_count']}")
    print(f"    估算记录: {d['estimated_count']}")
    assert d['db_exists'], "数据库不存在"
    assert d['row_count'] > 100, f"数据条数过少: {d['row_count']}"

    print("  ✅ 数据管理 API 全部通过")


def test_signal_api():
    """测试信号 API"""
    print("\n" + "=" * 60)
    print("测试 3: 信号 API")
    print("=" * 60)

    api = ApiBridge()

    # 测试 589800
    api.switch_profile('589800')
    res = api.get_today_signal()
    assert res['ok'], f"get_today_signal(589800) 失败: {res.get('error')}"
    sig = res['data']
    print(f"  ✓ 589800 信号:")
    print(f"    可用: {sig['available']}")
    if sig['available']:
        print(f"    日期: {sig['last_date']}")
        print(f"    市场状态: {sig['regime']} / {sig['psychology']}")
        print(f"    买分: {sig['buy_score']} / 卖分: {sig['sell_score']} / 净分: {sig['net_score']}")
        print(f"    目标仓位: {sig['target_position']}%")
        print(f"    Evidence买源: {len(sig['evidence_buy'])} 项")
        for ev in sig['evidence_buy']:
            print(f"      - {ev['source']}: {ev['contribution']:+.1f}")
        print(f"    Evidence卖源: {len(sig['evidence_sell'])} 项")
        for ev in sig['evidence_sell']:
            print(f"      - {ev['source']}: {ev['contribution']:+.1f}")
        assert sig['buy_score'] > 0 or sig['sell_score'] > 0, "买分卖分都为0"
        assert sig['data_rows'] > 100, "数据量不足"

    # 测试 563360
    api.switch_profile('563360')
    res = api.get_today_signal()
    assert res['ok'], f"get_today_signal(563360) 失败: {res.get('error')}"
    sig2 = res['data']
    print(f"  ✓ 563360 信号:")
    if sig2['available']:
        print(f"    买分: {sig2['buy_score']} / 卖分: {sig2['sell_score']} / 净分: {sig2['net_score']}")
        print(f"    目标仓位: {sig2['target_position']}%")

    print("  ✅ 信号 API 全部通过")
    return sig, sig2


def test_cli_consistency():
    """验证 GUI 信号与 CLI --signal 输出一致"""
    print("\n" + "=" * 60)
    print("测试 4: GUI vs CLI 一致性校验")
    print("=" * 60)

    # 通过 bridge 获取信号
    api = ApiBridge()
    api.switch_profile('589800')
    res = api.get_today_signal()
    gui_signal = res['data']

    # 通过 CLI 获取信号（用子进程调用 main.py --signal --profile 589800）
    import subprocess
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'main.py')
    result = subprocess.run(
        [sys.executable, main_py, '--profile', '589800', '--signal'],
        capture_output=True, text=True, timeout=60
    )
    cli_output = result.stdout

    # 从CLI输出中提取关键值进行对比
    print(f"  CLI输出长度: {len(cli_output)} 字符")

    # 提取买分/卖分
    import re
    buy_match = re.search(r'买入评分:\s*([\d.]+)', cli_output)
    sell_match = re.search(r'卖出评分:\s*([\d.]+)', cli_output)
    net_match = re.search(r'净评分:\s*([+\-\d.]+)', cli_output)
    regime_match = re.search(r'市场状态:\s*(\w+)', cli_output)
    psych_match = re.search(r'市场情绪:\s*(\w+)', cli_output)

    cli_buy = float(buy_match.group(1)) if buy_match else None
    cli_sell = float(sell_match.group(1)) if sell_match else None
    cli_net = float(net_match.group(1)) if net_match else None
    cli_regime = regime_match.group(1) if regime_match else None
    cli_psych = psych_match.group(1) if psych_match else None

    print(f"  CLI: 买={cli_buy}, 卖={cli_sell}, 净={cli_net}, 状态={cli_regime}, 情绪={cli_psych}")
    print(f"  GUI: 买={gui_signal['buy_score']}, 卖={gui_signal['sell_score']}, 净={gui_signal['net_score']}, 状态={gui_signal['regime']}, 情绪={gui_signal['psychology']}")

    # 验证
    errors = []
    if cli_buy is not None and abs(cli_buy - gui_signal['buy_score']) > 0.1:
        errors.append(f"买分不一致: CLI={cli_buy} vs GUI={gui_signal['buy_score']}")
    if cli_sell is not None and abs(cli_sell - gui_signal['sell_score']) > 0.1:
        errors.append(f"卖分不一致: CLI={cli_sell} vs GUI={gui_signal['sell_score']}")
    if cli_regime and cli_regime != gui_signal['regime']:
        errors.append(f"市场状态不一致: CLI={cli_regime} vs GUI={gui_signal['regime']}")
    if cli_psych and cli_psych != gui_signal['psychology']:
        errors.append(f"市场情绪不一致: CLI={cli_psych} vs GUI={gui_signal['psychology']}")

    if errors:
        print(f"  ❌ 发现 {len(errors)} 处不一致:")
        for e in errors:
            print(f"    - {e}")
        return False
    else:
        print("  ✅ GUI 与 CLI 结果完全一致")
        return True


def main():
    print("V7.0 P3 集成验证")
    print("=" * 60)

    all_passed = True
    try:
        test_profile_management()
    except Exception as e:
        print(f"  ❌ 标的管理测试失败: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False

    try:
        test_data_api()
    except Exception as e:
        print(f"  ❌ 数据API测试失败: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False

    try:
        test_signal_api()
    except Exception as e:
        print(f"  ❌ 信号API测试失败: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False

    try:
        cli_ok = test_cli_consistency()
        if not cli_ok:
            all_passed = False
    except Exception as e:
        print(f"  ❌ CLI一致性测试失败: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("✅ 所有测试通过！P3 验收合格")
    else:
        print("❌ 部分测试失败，请检查")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
