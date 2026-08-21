"""
V5.0 技术指标计算模块
================================

V5.0 新增：
- Acceleration（加速度）：近N日收益率 - 前N日收益率
- Deceleration（减速度）检测
- Reward/Risk 相关指标：
  - dist_from_60d_high / dist_from_60d_low
  - ATR位置
  - 波动率分位数
  - MA20偏离度

原则：所有技术指标仅用于描述市场状态，不允许直接作为买卖依据。
"""
import pandas as pd
import numpy as np
from config import *


def calculate_indicators(df):
    """
    计算策略所需的全部指标

    指标与行为对应关系：
    - MA5/MA10/MA20: 趋势回踩、假突破、真突破、趋势衰退
    - RSI14: 冲高衰竭、恐慌杀跌、RSI背离
    - ADX14: 市场状态、趋势衰退、趋势强度
    - ATR14: 市场状态、恐慌杀跌、趋势衰退
    - BB_width: 市场状态（布林带收窄/扩张）
    - VolumeZ: 所有成交量相关行为
    - MACD_hist: 动能变化
    - VWAP: 日内价格有效性
    - Acceleration: 上涨/下跌速度（V5.0 新增）
    - Reward/Risk 指标（V5.0 新增）

    V5.2 优化：如果指标已预计算（acceleration 列存在），直接返回。
    所有网格搜索参数均不改变指标计算，此优化零精度影响。
    """
    # V5.2 优化：已预计算则跳过（零精度影响 — 网格搜索参数均为行为检测阈值）
    if 'acceleration' in df.columns and 'dist_from_60d_high' in df.columns:
        return df
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    volume = df['volume'].values
    n = len(df)

    # ---- 均线 ----
    df['MA5'] = df['close'].rolling(MA_SHORT).mean()
    df['MA10'] = df['close'].rolling(MA_MID).mean()
    df['MA20'] = df['close'].rolling(MA_LONG).mean()

    # ---- MA20 斜率（5日变化率）----
    df['MA20_slope'] = df['MA20'].pct_change(5).fillna(0)

    # ---- MA5 斜率 ----
    df['MA5_slope'] = df['MA5'].diff().fillna(0)

    # ---- STD20 / Z20 / 布林带 ----
    df['STD20'] = df['close'].rolling(BB_PERIOD).std(ddof=0)
    df['Z20'] = np.where(df['STD20'] > 0, (close - df['MA20'].values) / df['STD20'].values, 0)
    df['BB_upper'] = df['MA20'] + BB_STD * df['STD20']
    df['BB_lower'] = df['MA20'] - BB_STD * df['STD20']
    df['BB_width'] = (df['BB_upper'] - df['BB_lower']) / df['MA20'].replace(0, np.nan)
    df['BB_width'] = df['BB_width'].fillna(0)

    # ---- RSI14 ----
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = np.zeros(n)
    avg_loss = np.zeros(n)
    if n > RSI_PERIOD:
        avg_gain[RSI_PERIOD] = gain.iloc[1:RSI_PERIOD + 1].mean()
        avg_loss[RSI_PERIOD] = loss.iloc[1:RSI_PERIOD + 1].mean()
        for i in range(RSI_PERIOD + 1, n):
            avg_gain[i] = (avg_gain[i - 1] * (RSI_PERIOD - 1) + gain.iloc[i]) / RSI_PERIOD
            avg_loss[i] = (avg_loss[i - 1] * (RSI_PERIOD - 1) + loss.iloc[i]) / RSI_PERIOD
    rs = avg_gain / (avg_loss + 1e-10)
    df['RSI14'] = 100 - (100 / (1 + rs))

    # ---- ADX14 / ATR14 ----
    tr_arr = np.maximum(
        high - low,
        np.maximum(np.abs(high - np.roll(close, 1)), np.abs(low - np.roll(close, 1)))
    )
    tr_arr[0] = high[0] - low[0]
    df['ATR14'] = pd.Series(tr_arr).rolling(ATR_PERIOD).mean()

    up_move = high - np.roll(high, 1)
    down_move = np.roll(low, 1) - low
    up_move[0] = down_move[0] = 0

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    atr14_vals = df['ATR14'].values
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)

    if n > ADX_PERIOD:
        sum_tr = np.sum(tr_arr[1:ADX_PERIOD + 1])
        if sum_tr > 0:
            plus_di[ADX_PERIOD] = np.sum(plus_dm[1:ADX_PERIOD + 1]) / sum_tr * 100
            minus_di[ADX_PERIOD] = np.sum(minus_dm[1:ADX_PERIOD + 1]) / sum_tr * 100
        for i in range(ADX_PERIOD + 1, n):
            atr_i = atr14_vals[i]
            if atr_i > 0:
                plus_di[i] = (plus_di[i - 1] * (ADX_PERIOD - 1) + plus_dm[i] / atr_i * 100) / ADX_PERIOD
                minus_di[i] = (minus_di[i - 1] * (ADX_PERIOD - 1) + minus_dm[i] / atr_i * 100) / ADX_PERIOD
            else:
                plus_di[i] = plus_di[i - 1]
                minus_di[i] = minus_di[i - 1]

    df['plus_di'] = plus_di
    df['minus_di'] = minus_di
    dx = np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
    adx = np.zeros(n)
    if n > ADX_PERIOD * 2:
        adx[ADX_PERIOD * 2 - 1] = np.mean(dx[ADX_PERIOD:ADX_PERIOD * 2])
        for i in range(ADX_PERIOD * 2, n):
            adx[i] = (adx[i - 1] * (ADX_PERIOD - 1) + dx[i]) / ADX_PERIOD
    df['ADX14'] = adx

    # ---- MACD ----
    ema12 = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']

    # ---- 成交量指标 ----
    df['Vol20'] = df['volume'].rolling(VOL_PERIOD).mean()
    df['Vol20_std'] = df['volume'].rolling(VOL_PERIOD).std(ddof=0)
    df['VolumeZ'] = np.where(
        df['Vol20_std'] > 0,
        (volume - df['Vol20'].values) / df['Vol20_std'].values,
        0
    )

    # ---- ROC ----
    df['ROC3'] = df['close'].pct_change(3)
    df['ROC5'] = df['close'].pct_change(5)

    # ---- 波动率 ----
    df['Volatility5'] = df['close'].pct_change().rolling(5).std()
    df['Volatility20'] = df['close'].pct_change().rolling(20).std()

    # ---- VWAP 偏离 ----
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    df['VWAP'] = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()
    df['VWAP_dev'] = (df['close'] - df['VWAP']) / df['VWAP'].replace(0, np.nan)
    df['VWAP_dev'] = df['VWAP_dev'].fillna(0)

    # ---- 价格位置 ----
    df['price_position'] = (df['close'] - df['BB_lower']) / (df['BB_upper'] - df['BB_lower'] + 1e-10)
    df['price_position'] = df['price_position'].clip(0, 1)

    # ================================================================
    # V5.0 新增指标
    # ================================================================

    # ---- Acceleration（加速度）----
    # Acceleration = 近3日收益率 - 前3日收益率
    # 正值 = 上涨加速，负值 = 上涨减速/下跌加速
    df['return_3d'] = df['close'].pct_change(3)
    df['return_3d_prior'] = df['close'].shift(3).pct_change(3)
    df['acceleration'] = df['return_3d'] - df['return_3d_prior']

    # ---- Deceleration 标记（V5.0：不等RSI，直接检测减速）----
    # 上涨速度开始下降 = Deceleration
    df['accel_prev'] = df['acceleration'].shift(1)
    df['is_decelerating'] = (df['acceleration'] < ACCEL_DECEL_THRESHOLD) & (df['accel_prev'] > df['acceleration'])
    df['is_accelerating'] = (df['acceleration'] > ACCEL_FOMO_THRESHOLD) & (df['acceleration'] > df['accel_prev'])

    # ---- Reward 相关指标 ----
    # 距离60日高点
    df['high_60d'] = df['high'].rolling(60).max()
    df['low_60d'] = df['low'].rolling(60).min()
    df['dist_from_60d_high'] = (df['close'] - df['high_60d']) / df['high_60d'].replace(0, np.nan)
    df['dist_from_60d_low'] = (df['close'] - df['low_60d']) / df['low_60d'].replace(0, np.nan)

    # MA20偏离度
    df['ma20_deviation'] = (df['close'] - df['MA20']) / df['MA20'].replace(0, np.nan)

    # ATR位置（价格在近期波动范围内的位置）
    df['range_20d_high'] = df['high'].rolling(20).max()
    df['range_20d_low'] = df['low'].rolling(20).min()
    df['atr_position'] = np.where(
        (df['range_20d_high'] - df['range_20d_low']) > 0,
        (df['close'] - df['range_20d_low']) / (df['range_20d_high'] - df['range_20d_low']),
        0.5
    )

    # 波动率分位数（当前波动率在60日历史中的位置）
    df['volatility_60d'] = df['close'].pct_change().rolling(60).std()
    df['volatility_percentile'] = df['volatility_60d'].rolling(60).apply(
        lambda x: (x.iloc[-1] >= x).sum() / len(x) if len(x) > 0 else 0.5,
        raw=False
    )

    # ---- Risk 相关指标 ----
    # 回撤风险（距20日高点距离）
    df['high_20d'] = df['high'].rolling(20).max()
    df['drawdown_risk'] = np.where(
        df['high_20d'] > 0,
        (df['close'] - df['high_20d']) / df['high_20d'],
        0
    )

    # 波动率风险（当前波动率 vs 60日均值）
    df['volatility_risk'] = np.where(
        df['volatility_60d'] > 0,
        df['Volatility20'] / df['volatility_60d'],
        1.0
    )

    # 趋势反转风险（ADX下降 + MA走平）
    df['ADX14_prev'] = df['ADX14'].shift(1)
    df['adx_declining'] = df['ADX14'] < df['ADX14_prev']
    df['ma20_flattening'] = abs(df['MA20_slope']) < REGIME_MA_SLOPE_MIN * 0.5

    # 成交量异常风险
    df['vol_ratio_to_mean'] = np.where(
        df['Vol20'] > 0,
        df['volume'] / df['Vol20'],
        1.0
    )

    # 均线偏离风险（偏离MA20越远，回调风险越大）
    df['ma_deviation_risk'] = abs(df['ma20_deviation'])

    return df


def get_value(df, index, col, default=0):
    """安全获取指标值"""
    if index < 0 or index >= len(df):
        return default
    val = df.iloc[index][col]
    if pd.isna(val):
        return default
    return val
