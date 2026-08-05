"""
V4.0 技术指标计算模块
每个指标都标注了「所解释的行为」，不为了加指标而加指标。
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
    - VolumeZ: 所有成交量相关行为（二次探底、冲高衰竭、恐慌杀跌等）
    - MACD_hist: 动能变化，辅助判断趋势回踩和趋势衰退
    - VWAP: 日内价格有效性（辅助判断假突破）
    - N日涨跌幅: 冲高衰竭、恐慌杀跌
    - N日波动率: 市场状态识别
    """
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    volume = df['volume'].values
    n = len(df)

    # ---- 均线 ----
    df['MA5'] = df['close'].rolling(MA_SHORT).mean()
    df['MA10'] = df['close'].rolling(MA_MID).mean()
    df['MA20'] = df['close'].rolling(MA_LONG).mean()

    # ---- MA20 斜率（5日变化率） ----
    # 用于：市场状态、趋势衰退
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

    # ---- ROC（N日涨跌幅） ----
    # 用于：冲高衰竭、恐慌杀跌
    df['ROC3'] = df['close'].pct_change(3)   # 3日涨跌幅
    df['ROC5'] = df['close'].pct_change(5)   # 5日涨跌幅

    # ---- N日波动率 ----
    # 用于：市场状态识别（波动率扩大/收窄）
    df['Volatility5'] = df['close'].pct_change().rolling(5).std()
    df['Volatility20'] = df['close'].pct_change().rolling(20).std()

    # ---- VWAP 偏离 ----
    # 用于：假突破识别（价格远离VWAP可能是不合理的）
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    df['VWAP'] = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()
    df['VWAP_dev'] = (df['close'] - df['VWAP']) / df['VWAP'].replace(0, np.nan)
    df['VWAP_dev'] = df['VWAP_dev'].fillna(0)

    # ---- 价格位置 ----
    df['price_position'] = (df['close'] - df['BB_lower']) / (df['BB_upper'] - df['BB_lower'] + 1e-10)
    df['price_position'] = df['price_position'].clip(0, 1)

    return df


def get_value(df, index, col, default=0):
    """安全获取指标值"""
    if index < 0 or index >= len(df):
        return default
    val = df.iloc[index][col]
    if pd.isna(val):
        return default
    return val
