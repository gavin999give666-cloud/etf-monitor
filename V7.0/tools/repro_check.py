"""
V7.0 P1 复现性检验（自动化验收）
=================================
对每个预置标的，分别在【旧版 V6.2.3】与【新版 V7.0】下运行 --signal 与 --eval，
逐项对比关键数值，输出 PASS/FAIL。

验收标准（设计文档 §六 P1）：
  589800 / 563360 两标的 --signal 与回测指标（年化/夏普/回撤）与旧版完全一致。

用法：
  python tools/repro_check.py            # 运行全部标的复现检验
  python tools/repro_check.py --signal   # 仅信号
  python tools/repro_check.py --eval     # 仅回测
退出码：0=全部通过；1=存在不一致（便于 CI 门禁）
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = os.path.join(ROOT, '..', 'V6.2.3')

# 预置标的目标：(代码, 名称, 旧版目录)
INSTRUMENTS = [
    ('589800', '科创综指 ETF', os.path.join(OLD, '科创')),
    ('563360', '中证 A500 ETF', os.path.join(OLD, 'A500')),
]

# --eval 需比对的关键指标（行内中文标签 → 数值）
EVAL_KEYS = {
    '策略收益率': '%', '最大回撤': '%', '年化收益率': '%',
    '夏普比率': 'num', '数据条数': 'int', '总交易次数': 'int',
}

# --signal 需比对的数值字段（正则 → 值）
SIGNAL_PATTERNS = [
    r'市场状态:\s*(\S+)',
    r'市场情绪:\s*(\S+)',
    r'买入评分:\s*([\d.]+).*卖出评分:\s*([\d.]+)',
    r'净评分:\s*([+-][\d.]+)',
    r'Reward:\s*([\d.]+).*Risk:\s*([\d.]+)',
    r'已确认买入事件:\s*(\d+)',
    r'已确认卖出事件:\s*(\d+)',
    r'活跃事件总数:\s*(\d+)',
    r'FINAL:\s*([\d.]+)',
]


def run(cmd, cwd, timeout=300):
    """在指定目录运行命令，捕获 stdout，干净退出"""
    env = dict(os.environ, MPLBACKEND='Agg', PYTHONIOENCODING='utf-8')
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                       text=True, encoding='utf-8', errors='replace',
                       timeout=timeout)
    return p.stdout + '\n' + p.stderr


def parse_eval(text):
    out = {}
    for label, kind in EVAL_KEYS.items():
        m = re.search(rf'{label}.*?[:：]\s*(-?[\d.]+)%?', text)
        if m:
            out[label] = float(m.group(1)) if kind != 'int' else int(m.group(1))
    return out


def parse_signal(text):
    out = {}
    m = re.search(r'今日信号:\s*([+-]?[\d.]+)\s*NetScore', text)
    if m:
        out['NetScore'] = float(m.group(1))
    for pat in SIGNAL_PATTERNS:
        m = re.search(pat, text)
        if m:
            out.setdefault('_', []).append(tuple(m.groups()))
    return out


def compare_signal(code, name, old_text, new_text):
    o, n = parse_signal(old_text), parse_signal(new_text)
    print(f"\n  [{code}] {name}  --signal 复现检验")
    ok = True
    if o.get('NetScore') != n.get('NetScore'):
        ok = False; print(f"    ✗ NetScore: 旧={o.get('NetScore')} 新={n.get('NetScore')}")
    else:
        print(f"    ✓ NetScore {o.get('NetScore')}")
    o_items = o.get('_', []); n_items = n.get('_', [])
    if o_items != n_items:
        ok = False
        print(f"    ✗ 明细字段不一致")
        print(f"      旧: {o_items}")
        print(f"      新: {n_items}")
    else:
        print(f"    ✓ 明细字段（状态/评分/Reward/事件/证据FINAL）完全一致")
    return ok


def compare_eval(code, name, old_text, new_text):
    o, n = parse_eval(old_text), parse_eval(new_text)
    print(f"\n  [{code}] {name}  --eval 回测指标复现检验")
    ok = True
    for label in EVAL_KEYS:
        if o.get(label) is None or n.get(label) is None:
            if o.get(label) != n.get(label):
                ok = False
                print(f"    ✗ {label}: 旧={o.get(label)} 新={n.get(label)} (缺值)")
            continue
        if abs(o[label] - n[label]) > 1e-9:
            ok = False
            print(f"    ✗ {label}: 旧={o[label]} 新={n[label]}")
        else:
            print(f"    ✓ {label}: {o[label]}")
    return ok


def main():
    do_signal = '--signal' not in sys.argv and '--eval' not in sys.argv or '--signal' in sys.argv
    do_eval = '--signal' not in sys.argv and '--eval' not in sys.argv or '--eval' in sys.argv

    all_ok = True
    for code, name, old_dir in INSTRUMENTS:
        print("=" * 70)
        print(f"{code} {name} vs 旧版")
        print("=" * 70)
        if do_signal:
            old_t = run(['python', 'main.py', '--signal'], cwd=old_dir)
            new_t = run(['python', 'main.py', '--profile', code, '--signal'], cwd=ROOT)
            if not compare_signal(code, name, old_t, new_t):
                all_ok = False
        if do_eval:
            old_t = run(['python', 'main.py', '--eval'], cwd=old_dir, timeout=600)
            new_t = run(['python', 'main.py', '--profile', code, '--eval'], cwd=ROOT, timeout=600)
            if not compare_eval(code, name, old_t, new_t):
                all_ok = False

    print("\n" + "=" * 70)
    print(f"复现性检验结论: {'PASS ✅ 全部指标与旧版完全一致' if all_ok else 'FAIL ❌ 存在不一致'}")
    print("=" * 70)
    sys.exit(0 if all_ok else 1)


if __name__ == '__main__':
    main()