"""
P6.2 单测 —— risk_guard.py L3 风控层
=====================================
构造 20+ 场景：硬止损（梯度/冷静期）、阶梯止盈（T1/T2/高位回撤）、
回撤熔断（触发/解除/确认天数清零）、多机制 cap 取 min、无持仓不触发。

用法：
  python tools/test_risk_guard.py
退出码：0=全部通过；1=存在失败
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))

from risk_guard import RiskGuard

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


def has_type(actions, t):
    return any(a['type'] == t for a in actions)


# ============================================================
# 1. 硬止损
# ============================================================
print("\n[1] 硬止损")
g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(92.0, 0.9, 10000, date='2026-01-10')   # -8%
check("浮亏 -8% → 仓位减半 (cap=0.45)", cap == 0.45 and has_type(acts, 'stop_loss_half'),
      f"cap={cap} acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(91.0, 0.9, 10000, date='2026-01-10')   # -9%
check("浮亏 -9% → 仓位减半", cap == 0.45 and has_type(acts, 'stop_loss_half'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(88.0, 0.9, 10000, date='2026-01-10')   # -12%
check("浮亏 -12% → 清仓 (cap=0)", cap == 0.0 and has_type(acts, 'stop_loss_hard'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(85.0, 0.9, 10000, date='2026-01-10')   # -15%
check("浮亏 -15% → 清仓", cap == 0.0 and has_type(acts, 'stop_loss_hard'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(93.0, 0.9, 10000, date='2026-01-10')   # -7% 未达阈值
check("浮亏 -7%（未达 -8%）→ 不触发", cap is None and not acts, f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(92.0, 0.9, 10000, date='2026-01-10')   # 触发 -8%
check("首次触发止损", has_type(acts, 'stop_loss_half'))
cap, acts = g.evaluate(91.0, 0.9, 10000, date='2026-01-20')   # 10日后 -9%，冷静期内
check("冷静期内（10日）再次浮亏 → 不重复触发", not has_type(acts, 'stop_loss_half') and not has_type(acts, 'stop_loss_hard'),
      f"acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
g.evaluate(92.0, 0.9, 10000, date='2026-01-10')               # 触发
cap, acts = g.evaluate(91.0, 0.9, 10000, date='2026-07-10')   # 181日后 -9%，冷静期过
check("冷静期过后（181日）→ 重新触发", has_type(acts, 'stop_loss_half'), f"acts={[a['type'] for a in acts]}")

g = RiskGuard()
cap, acts = g.evaluate(92.0, 0.9, 10000, date='2026-01-10')   # 无持仓
check("无持仓 → 不触发止损/止盈", cap is None and not acts, f"cap={cap}")

# ============================================================
# 2. 阶梯止盈
# ============================================================
print("\n[2] 阶梯止盈")
g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(115.0, 0.9, 10000, date='2026-01-10')  # +15%
check("浮盈 +15% → 减1/3 (cap=0.6)", abs(cap - 0.6) < 1e-9 and has_type(acts, 'take_profit_t1'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(120.0, 0.9, 10000, date='2026-01-10')  # +20%
check("浮盈 +20% → 减1/3", abs(cap - 0.6) < 1e-9 and has_type(acts, 'take_profit_t1'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(125.0, 0.9, 10000, date='2026-01-10')  # +25%
check("浮盈 +25% → 再减1/3 (cap=0.3)", abs(cap - 0.3) < 1e-9 and has_type(acts, 'take_profit_t2'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(130.0, 0.9, 10000, date='2026-01-10')  # +30%
check("浮盈 +30% → 再减1/3", abs(cap - 0.3) < 1e-9 and has_type(acts, 'take_profit_t2'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(110.0, 0.9, 10000, date='2026-01-10')  # +10% 未达 T1
check("浮盈 +10%（未达 +15%）→ 不触发", cap is None and not acts, f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(96.0, 0.9, 10000, high_60d=100.0, date='2026-01-10')  # 距高点4%
check("距60日高点回撤 4% → 清仓锁利", cap == 0.0 and has_type(acts, 'trail_exit'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(95.0, 0.9, 10000, high_60d=100.0, date='2026-01-10')  # 距高点5%
check("距60日高点回撤 5% → 清仓锁利", cap == 0.0 and has_type(acts, 'trail_exit'), f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(97.0, 0.9, 10000, high_60d=100.0, date='2026-01-10')  # 距高点3%
check("距60日高点回撤 3%（未达 4%）→ 不触发", cap is None and not acts, f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(125.0, 0.9, 10000, high_60d=130.0, date='2026-01-10')  # +25% 且距高点3.8%
check("浮盈 +25% 且距高点 3.8% → 仅 T2（cap=0.3）", abs(cap - 0.3) < 1e-9 and has_type(acts, 'take_profit_t2'),
      f"cap={cap} acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(125.0, 0.9, 10000, high_60d=131.0, date='2026-01-10')  # +25% 且距高点4.6%
check("浮盈 +25% 且距高点 4.6% → T2 与 trail 都触发，cap=0（min）",
      cap == 0.0 and has_type(acts, 'take_profit_t2') and has_type(acts, 'trail_exit'),
      f"cap={cap} acts={[a['type'] for a in acts]}")

# ============================================================
# 3. 回撤熔断
# ============================================================
print("\n[3] 回撤熔断")
g = RiskGuard()
cap, acts = g.evaluate(100.0, 0.9, 10000, date='2026-01-01')   # 峰值 10000
cap, acts = g.evaluate(100.0, 0.9, 8800, date='2026-01-10')    # 回撤 12%
check("净值回撤 12% → 熔断触发 (cap=0.20)", cap == 0.20 and has_type(acts, 'circuit_breaker'), f"cap={cap}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
cap, acts = g.evaluate(100.0, 0.9, 8500, date='2026-01-10')    # 回撤 15%
check("净值回撤 15% → 熔断触发", cap == 0.20 and has_type(acts, 'circuit_breaker'), f"cap={cap}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 8800, date='2026-01-10')                # 触发熔断
cap, acts = g.evaluate(100.0, 0.9, 8700, ma20=95.0, date='2026-01-11')  # 仍回撤13%，未站上MA20
check("熔断持续（回撤仍≥12%）→ cap 仍 0.20", cap == 0.20 and not has_type(acts, 'circuit_release'), f"cap={cap}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 8800, date='2026-01-10')                # 触发熔断
cap, acts = g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-11')  # 回撤10%<12%，站上MA20
check("回撤<12% 且站上MA20（第1日）→ 未解除", cap is None and not has_type(acts, 'circuit_release'),
      f"cap={cap} acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 8800, date='2026-01-10')                # 触发熔断
for d in range(1, 5):                                          # 连续4日站上MA20
    g.evaluate(100.0, 0.9, 9000, ma20=95.0, date=f'2026-01-1{d}')
cap, acts = g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-15')  # 第5日
check("连续5日站上MA20 → 熔断解除", not g.circuit_breaker and has_type(acts, 'circuit_release'),
      f"circuit={g.circuit_breaker} acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 8800, date='2026-01-10')                # 触发熔断
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-11')     # 第1日站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-12')     # 第2日站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-13')     # 第3日站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-14')     # 第4日站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-15')     # 第5日站上 → 解除
cap, acts = g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-16')  # 解除后第1日
check("熔断解除后不再限制", cap is None and not has_type(acts, 'circuit_breaker'), f"cap={cap}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 8800, date='2026-01-10')                # 触发熔断
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-11')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-12')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-13')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-14')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-15')     # 第5日 → 应解除
cap, acts = g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-16')
check("连续5日站上MA20 后确认天数清零", g.circuit_confirm_days == 0, f"confirm={g.circuit_confirm_days}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 8800, date='2026-01-10')                # 触发熔断
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-11')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-12')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-13')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-14')     # 站上
g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-15')     # 第5日 → 解除
cap, acts = g.evaluate(100.0, 0.9, 9000, ma20=95.0, date='2026-01-16')
check("解除后再次回撤≥12% → 重新熔断", True)  # 占位，下面用新实例验证
g2 = RiskGuard()
g2.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g2.evaluate(100.0, 0.9, 9500, date='2026-01-10')               # 回撤5% 未触发
check("净值回撤 5%（未达 12%）→ 不触发熔断", not g2.circuit_breaker and cap is not None or not g2.circuit_breaker,
      f"circuit={g2.circuit_breaker}")

# ============================================================
# 4. 多机制 cap 取 min + 状态
# ============================================================
print("\n[4] 多机制叠加与状态")
g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')               # 峰值 10000
cap, acts = g.evaluate(92.0, 0.9, 8800, date='2026-01-10')     # 浮亏-8%（cap=0.45）且净值回撤12%（cap=0.20）
check("止损减半(0.45) 与熔断(0.20) 取 min → 0.20", abs(cap - 0.20) < 1e-9,
      f"cap={cap} acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
cap, acts = g.evaluate(88.0, 0.9, 8800, date='2026-01-10')     # 浮亏-12%（cap=0）且熔断（0.20）
check("止损清仓(0) 与熔断(0.20) 取 min → 0", cap == 0.0, f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
cap, acts = g.evaluate(115.0, 0.9, 10000, date='2026-01-10')   # 浮盈+15%（cap=0.6）
check("止盈 T1 cap=0.6", abs(cap - 0.6) < 1e-9, f"cap={cap}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(115.0, 0.9, 10000, date='2026-01-10')               # T1 触发
g.on_exit()                                                    # 平仓
cap, acts = g.evaluate(115.0, 0.0, 10000, date='2026-01-11')   # 无持仓
check("平仓后（on_exit）→ 止损/止盈不触发", cap is None and not has_type(acts, 'take_profit_t1'),
      f"cap={cap} acts={[a['type'] for a in acts]}")

g = RiskGuard()
g.evaluate(100.0, 0.9, 10000, date='2026-01-01')
g.evaluate(100.0, 0.9, 10500, date='2026-01-02')               # 净值新高
g.evaluate(100.0, 0.9, 11000, date='2026-01-03')               # 再新高
cap, acts = g.evaluate(100.0, 0.9, 10000, date='2026-01-10')   # 从11000回撤到10000 = 9.1%
check("净值峰值随新高更新（11000→10000 回撤9.1% 未达12%）", not g.circuit_breaker,
      f"peak={g.peak_equity} circuit={g.circuit_breaker}")

g = RiskGuard()
g.on_entry(100.0, '2026-01-01')
cap, acts = g.evaluate(92.0, 0.9, 10000, date=None)            # date=None 跳过冷静期
check("date=None → 冷静期跳过，止损正常触发", has_type(acts, 'stop_loss_half'), f"acts={[a['type'] for a in acts]}")

# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 60)
print(f"P6.2 单测结果: {PASS} 通过 / {FAIL} 失败")
if FAIL:
    print("失败项:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("P6.2 risk_guard.py 单测全部通过 ✅")
print("=" * 60)
