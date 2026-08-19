import time, sys, os, copy
sys.path.insert(0, r'f:\windows10桌面\量化\V6.2.3')
os.chdir(r'f:\windows10桌面\量化\V6.2.3')

import config as cfg
orig = copy.deepcopy(cfg.REGIME_WEIGHTS)
from data_updater import load_data_from_db
from indicators import calculate_indicators

df = load_data_from_db()
df = calculate_indicators(df)
print(f"Data: {len(df)} rows, {len(df.columns)} cols")

from strategy import V6Strategy
from backtest import V6Backtest

# Warm-up (first call might have ML init overhead)
s = V6Strategy()
s.run(df)

# Test 1: Default params
cfg.MIN_HOLD_DAYS = 20
cfg.SCORE_HOLD_ZONE = 20
cfg.TRADE_TARGET_DELTA = 0.03
cfg.MAX_POSITION = 0.90
cfg.CONFIRMATION_THRESHOLD = 70
cfg.REGIME_WEIGHTS = copy.deepcopy(orig)
cfg.REGIME_WEIGHTS['Bull']['sell_div'] = 0.20
cfg.REGIME_WEIGHTS['Bull']['buy_mult'] = 1.50

s = V6Strategy()
t0 = time.time()
sig = s.run(df)
t1 = time.time()
bt = V6Backtest(df, strategy=s)
bt.run(sig)
r = bt._compute_results()
t2 = time.time()

print(f"\nStrategy: {t1-t0:.2f}s")
print(f"Backtest: {t2-t1:.2f}s")
print(f"Total:    {t2-t0:.2f}s")
print(f"Return:   {r['strategy_return']*100:.2f}%")

# Estimate 4860 combos
per_combo = t2 - t0 + 0.5  # +0.5 for purge+reimport
total_min = 4860 * per_combo / 60
print(f"\nPer combo: ~{per_combo:.1f}s")
print(f"4860 combos: ~{total_min:.0f} min")
