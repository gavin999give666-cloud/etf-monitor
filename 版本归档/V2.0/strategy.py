import pandas as pd
from datetime import datetime, time
import numpy as np

# 策略名称：A500 ETF 波段交易策略 V2.0（优化版）
# 优化重点：提高胜率、减少无效交易、避免频繁卖出后追回、增强趋势识别

# 主要节假日列表（2026年）
MAJOR_HOLIDAYS_2026 = [
    '2026-01-01',
    '2026-02-01', '2026-02-02', '2026-02-03', '2026-02-04', '2026-02-05',
    '2026-04-04', '2026-04-05',
    '2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05',
    '2026-06-25', '2026-06-26',
    '2026-09-27',
    '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07',
]


def is_trading_day(date):
    """判断日期是否为交易日"""
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y-%m-%d').date()
    elif isinstance(date, datetime):
        date = date.date()
    if date.weekday() >= 5:
        return False
    if date.strftime('%Y-%m-%d') in MAJOR_HOLIDAYS_2026:
        return False
    return True


def is_trading_hour(current_time):
    """判断当前时间是否在交易时段内"""
    if isinstance(current_time, datetime):
        current_time = current_time.time()
    morning_start = time(9, 30)
    morning_end = time(11, 30)
    afternoon_start = time(13, 0)
    afternoon_end = time(15, 0)
    return (morning_start <= current_time <= morning_end) or (afternoon_start <= current_time <= afternoon_end)


def parse_signal(signal):
    """将 '+20%' 转为 +0.2，'-50%' 转为 -0.5"""
    if signal == '0%' or signal is None:
        return 0.0
    return float(signal.replace('%', '')) / 100


def calculate_indicators(df):
    """
    计算策略所需的全部指标
    V2.0 新增：ADX, ATR, MA20斜率, 布林带宽(BBWidth), 成交量ZScore, MACD直方图
    """
    # ========== 原有均线指标 ==========
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA10'] = df['close'].rolling(window=10).mean()
    df['MA20'] = df['close'].rolling(window=20).mean()

    # ========== 原有标准差和ZScore ==========
    df['STD20'] = df['close'].rolling(window=20).std(ddof=0)
    df['Z20'] = (df['close'] - df['MA20']) / df['STD20']
    df.loc[df['STD20'] == 0, 'Z20'] = 0

    # ========== 原有RSI14 ==========
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta).where(delta < 0, 0)
    avg_gain = np.zeros(len(df))
    avg_loss = np.zeros(len(df))
    if len(df) >= 15:
        avg_gain[14] = gain.iloc[1:15].mean()
        avg_loss[14] = loss.iloc[1:15].mean()
        for i in range(15, len(df)):
            avg_gain[i] = (avg_gain[i - 1] * 13 + gain.iloc[i]) / 14
            avg_loss[i] = (avg_loss[i - 1] * 13 + loss.iloc[i]) / 14
    else:
        for i in range(1, len(df)):
            if i == 1:
                avg_gain[i] = gain.iloc[i]
                avg_loss[i] = loss.iloc[i]
            else:
                avg_gain[i] = (avg_gain[i - 1] * 13 + gain.iloc[i]) / 14
                avg_loss[i] = (avg_loss[i - 1] * 13 + loss.iloc[i]) / 14
    rs = avg_gain / (avg_loss + 1e-10)
    df['RSI14'] = 100 - (100 / (1 + rs))

    # ========== 原有ROC5和Vol20 ==========
    df['ROC5'] = df['close'].pct_change(periods=5)
    df['Vol20'] = df['volume'].rolling(window=20).mean()

    # ========== V2.0 新增：ADX(14) 趋势强度指标 ==========
    # ADX用于判断市场是趋势市还是震荡市
    high = df['high']
    low = df['low']
    close = df['close']

    tr = pd.DataFrame({
        'hl': high - low,
        'hc': abs(high - close.shift(1)),
        'lc': abs(low - close.shift(1))
    }).max(axis=1)
    df['ATR14'] = tr.rolling(window=14).mean()  # ATR(14)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    atr14 = df['ATR14'].values
    plus_di = np.zeros(len(df))
    minus_di = np.zeros(len(df))

    # 使用Wilder平滑计算+DI和-DI
    if len(df) >= 15:
        plus_di[14] = np.sum(plus_dm[1:15]) / atr14[14] * 100 if atr14[14] > 0 else 0
        minus_di[14] = np.sum(minus_dm[1:15]) / atr14[14] * 100 if atr14[14] > 0 else 0
        for i in range(15, len(df)):
            atr_val = atr14[i]
            if atr_val > 0:
                plus_di[i] = (plus_di[i - 1] * 13 + (plus_dm[i] / atr_val * 100)) / 14
                minus_di[i] = (minus_di[i - 1] * 13 + (minus_dm[i] / atr_val * 100)) / 14
            else:
                plus_di[i] = plus_di[i - 1]
                minus_di[i] = minus_di[i - 1]

    df['plus_di'] = plus_di
    df['minus_di'] = minus_di

    dx = np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
    adx = np.zeros(len(df))
    if len(df) >= 29:
        adx[28] = np.mean(dx[14:29])
        for i in range(29, len(df)):
            adx[i] = (adx[i - 1] * 13 + dx[i]) / 14
    df['ADX14'] = adx

    # ========== V2.0 新增：MA20斜率（用于判断趋势方向） ==========
    # MA20_slope = (MA20_today - MA20_5days_ago) / MA20_5days_ago
    df['MA20_5d_ago'] = df['MA20'].shift(5)
    df['MA20_slope'] = (df['MA20'] - df['MA20_5d_ago']) / (df['MA20_5d_ago'].replace(0, np.nan))
    df['MA20_slope'] = df['MA20_slope'].fillna(0)

    # ========== V2.0 新增：MA5斜率（用于RecoveryStart判断动量恢复） ==========
    df['MA5_slope'] = df['MA5'] - df['MA5'].shift(1)

    # ========== V2.0 新增：布林带宽 BBWidth ==========
    df['BB_upper'] = df['MA20'] + 2 * df['STD20']
    df['BB_lower'] = df['MA20'] - 2 * df['STD20']
    df['BB_width'] = (df['BB_upper'] - df['BB_lower']) / df['MA20']

    # ========== V2.0 新增：成交量ZScore ==========
    df['Vol20_std'] = df['volume'].rolling(window=20).std(ddof=0)
    df['VolumeZ'] = (df['volume'] - df['Vol20']) / (df['Vol20_std'].replace(0, np.nan))
    df['VolumeZ'] = df['VolumeZ'].fillna(0)

    # ========== V2.0 新增：MACD直方图（用于判断动量变化） ==========
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']  # MACD柱状图

    return df


def get_market_regime(df, index):
    """
    V2.0 新增：市场状态识别
    返回 'trending_up', 'trending_down', 'ranging'
    - ADX < 20: 震荡市
    - ADX >= 20 且 close > MA20: 上涨趋势市
    - ADX >= 20 且 close <= MA20: 下跌趋势市
    """
    if index < 29:
        return 'unknown'

    adx = df.iloc[index]['ADX14']
    close = df.iloc[index]['close']
    ma20 = df.iloc[index]['MA20']
    ma20_slope = df.iloc[index]['MA20_slope']

    if adx < 20:
        return 'ranging'
    else:
        if close > ma20 and ma20_slope > 0:
            return 'trending_up'
        elif close < ma20 and ma20_slope < 0:
            return 'trending_down'
        else:
            # ADX高但方向不明确
            if close > ma20:
                return 'trending_up'
            else:
                return 'trending_down'


def check_buy_conditions(df, index):
    """检查买入条件（V2.0 优化版）"""
    if index < 29:  # 需要ADX数据，提高到29
        return None, None

    latest = df.iloc[index]
    prev = df.iloc[index - 1]

    # ============================================================
    # 1. DeepReversal 深超卖大幅买入（最高优先级）
    # V2.0 优化：首次进入机制 —— 昨天Z > -1.8 且 今天Z <= -1.8
    # 避免连续多天重复触发
    # ============================================================
    if (
        latest['Z20'] <= -1.8 and
        latest['RSI14'] <= 45 and
        latest['ROC5'] <= -0.03 and
        latest['close'] < latest['MA20'] and
        latest['volume'] >= latest['Vol20'] and
        prev['Z20'] > -1.8  # V2.0：首次进入超卖区，昨天还不算深超卖
    ):
        return '+50%', 'DeepReversal'

    # ============================================================
    # 2. RecoveryStart 修复启动买入
    # V2.0 优化：增加动量恢复确认，而非仅仅位置恢复
    # 新增条件：MA5开始拐头向上 或 今日收盘>昨日收盘
    # V2.0 优化：首次进入机制，避免连续多天重复触发
    # ============================================================
    momentum_recovering = (
        latest['MA5_slope'] > 0 or           # MA5开始拐头向上
        latest['close'] > prev['close']       # 今日收阳
    )
    recovery_conditions_met = (
        latest['close'] < latest['MA20'] and
        latest['close'] >= latest['MA10'] * 0.985 and
        -1.6 <= latest['Z20'] <= -0.6 and
        45 <= latest['RSI14'] <= 58 and
        latest['ROC5'] > -0.03 and
        latest['ROC5'] <= 0.02 and
        momentum_recovering  # V2.0 新增：动量恢复确认
    )
    if recovery_conditions_met:
        # V2.0 首次进入检查：昨天是否已触发RecoveryStart
        if index >= 30:
            prev_momentum = (
                prev['MA5_slope'] > 0 or
                prev['close'] > df.iloc[index - 2]['close']
            )
            prev_recovery = (
                prev['close'] < prev['MA20'] and
                prev['close'] >= prev['MA10'] * 0.985 and
                -1.6 <= prev['Z20'] <= -0.6 and
                45 <= prev['RSI14'] <= 58 and
                prev['ROC5'] > -0.03 and
                prev['ROC5'] <= 0.02 and
                prev_momentum
            )
            if not prev_recovery:
                return '+20%', 'RecoveryStart'

    # ============================================================
    # 3. Pullback 上升趋势中的回踩买入
    # V2.0 优化：增加趋势过滤 —— MA20不下降、MA5 > MA20
    # 确保Pullback发生在上涨趋势中，而不是下跌中的反弹
    # ============================================================
    uptrend_confirmed = (
        latest['MA20'] >= df.iloc[index - 1]['MA20'] and  # MA20不继续下降
        latest['MA5'] > latest['MA20']                      # MA5在MA20上方
    )
    if (
        abs(latest['close'] - latest['MA10']) / latest['MA10'] <= 0.015 and
        -1.1 <= latest['Z20'] <= -0.4 and
        40 <= latest['RSI14'] <= 50 and
        latest['ROC5'] <= 0.01 and
        latest['volume'] <= 1.10 * latest['Vol20'] and
        uptrend_confirmed  # V2.0 新增：趋势过滤
    ):
        # 首次触发检查
        if index >= 30:
            prev_cond = (
                abs(prev['close'] - prev['MA10']) / prev['MA10'] <= 0.015 and
                -1.1 <= prev['Z20'] <= -0.4 and
                40 <= prev['RSI14'] <= 50 and
                prev['ROC5'] <= 0.01 and
                prev['volume'] <= 1.10 * prev['Vol20'] and
                prev['MA20'] >= df.iloc[index - 2]['MA20'] and
                prev['MA5'] > prev['MA20']
            )
            if not prev_cond:
                return '+20%', 'Pullback'

    # ============================================================
    # 4. ReversalConfirm 反转确认加仓
    # V2.0 优化：增加确认条件 —— 成交量放大 + 涨幅>1%
    # 减少假突破导致的追买
    # ============================================================
    if index >= 30:
        reversal_confirmed = (
            latest['close'] > latest['MA20'] and
            prev['close'] <= prev['MA20'] and            # 刚刚站上MA20
            latest['RSI14'] >= 45 and
            latest['ROC5'] > 0 and
            # V2.0 新增确认条件：
            latest['volume'] > latest['Vol20'] and        # 放量突破
            (latest['close'] - prev['close']) / prev['close'] > 0.01  # 涨幅>1%，真突破
        )
        if reversal_confirmed:
            return '+20%', 'ReversalConfirm'

    return None, None


def check_sell_conditions(df, index):
    """检查卖出条件（V2.0 优化版）"""
    if index < 29:
        return None, None

    latest = df.iloc[index]
    prev = df.iloc[index - 1]

    # ============================================================
    # 1. Overheat 过热区大幅卖出
    # V2.0 优化：增加成交量确认 —— 成交量明显萎缩才执行
    # 若成交量持续放大说明资金仍在流入，不急于卖出
    # ============================================================
    # V2.0 优化：成交量环比下降，而非绝对值低于均量
    # 若成交量持续放大说明资金仍在流入，不急于卖出
    # 若今日成交量 < 昨日成交量，说明上涨动能开始减弱
    volume_weakening = (
        latest['volume'] < prev['volume']  # V2.0：成交量环比下降 = 动能减弱
    )
    if (
        latest['close'] > latest['MA20'] and prev['close'] > prev['MA20'] and
        latest['RSI14'] >= 67 and
        latest['Z20'] >= 1.6 and
        latest['ROC5'] >= 0.03 and
        volume_weakening  # V2.0 新增：成交量确认顶部
    ):
        # 首次触发检查
        if index >= 30:
            prev_vol_weakening = prev['volume'] < df.iloc[index - 2]['volume']
            prev_cond = (
                prev['close'] > prev['MA20'] and df.iloc[index - 2]['close'] > df.iloc[index - 2]['MA20'] and
                prev['RSI14'] >= 67 and
                prev['Z20'] >= 1.6 and
                prev['ROC5'] >= 0.03 and
                prev_vol_weakening
            )
            if not prev_cond:
                return '-80%', 'Overheat'

    # ============================================================
    # 2. StandardExit 标准卖出
    # V2.0 优化：检测"开始转弱"而非"已经很热"
    # 新逻辑：RSI >= 60 + Z >= 0.6 + 在MA10上方 + 出现转弱信号
    # 转弱信号包括：RSI开始下降、今日收阴、ROC下降
    # ============================================================
    weakening_signal = (
        latest['RSI14'] < prev['RSI14'] or           # RSI开始下降
        latest['close'] < prev['close'] or            # 今日收阴
        latest['ROC5'] < prev['ROC5']                 # ROC开始下降
    )
    if (
        latest['close'] >= latest['MA10'] and
        latest['Z20'] >= 0.6 and
        latest['RSI14'] >= 60 and
        weakening_signal  # V2.0 优化：必须出现转弱信号才卖出
    ):
        # 首次触发检查
        if index >= 30:
            prev_weakening = (
                prev['RSI14'] < df.iloc[index - 2]['RSI14'] or
                prev['close'] < df.iloc[index - 2]['close'] or
                prev['ROC5'] < df.iloc[index - 2]['ROC5']
            )
            prev_cond = (
                prev['close'] >= prev['MA10'] and
                prev['Z20'] >= 0.6 and
                prev['RSI14'] >= 60 and
                prev_weakening
            )
            if not prev_cond:
                return '-50%', 'StandardExit'

    # ============================================================
    # 3. TrendBroken 趋势破坏强制卖出
    # V2.0 重大优化：方案C + (方案A 或 方案B) 综合判断
    # 旧逻辑：连续2天跌破MA20 → 直接-80%（过于敏感）
    # 新逻辑：
    #   (方案C) 连续3天收盘价低于MA20（而非2天）
    #   AND
    #   (方案A) MA20开始下弯  OR  (方案B) MA5 < MA10短线死叉
    #   同时非极端超卖状态
    # ============================================================
    if index >= 31:
        prev2 = df.iloc[index - 2]

        # 方案C: 连续3天收盘价低于MA20
        three_days_below_ma20 = (
            latest['close'] < latest['MA20'] and
            prev['close'] < prev['MA20'] and
            prev2['close'] < prev2['MA20']
        )

        # 方案A: MA20开始下弯
        ma20_turning_down = latest['MA20'] < prev['MA20']

        # 方案B: MA5 < MA10，短线死叉
        ma5_below_ma10 = latest['MA5'] < latest['MA10']

        # 综合判断：3天跌破MA20 + (MA20下弯 或 MA5死叉MA10)
        trend_broken = (
            three_days_below_ma20 and
            (ma20_turning_down or ma5_below_ma10)
        )

        # 非极端超卖时才触发
        not_extreme_oversold = not (latest['Z20'] <= -1.8 and latest['RSI14'] <= 45)

        if trend_broken and not_extreme_oversold:
            # 首次触发检查
            prev_trend_broken = (
                prev['close'] < prev['MA20'] and
                prev2['close'] < prev2['MA20'] and
                df.iloc[index - 3]['close'] < df.iloc[index - 3]['MA20'] and
                (prev['MA20'] < prev2['MA20'] or prev['MA5'] < prev['MA10'])
            )
            if not prev_trend_broken:
                return '-80%', 'TrendBroken'

    return None, None


def generate_signal(df, index):
    """
    生成交易信号（V2.0 优化版）
    V2.0 新增：市场状态感知，在趋势市和震荡市采用不同策略权重
    """
    regime = get_market_regime(df, index)

    # 1. 深超卖买入（最高优先级）—— 在任何市场状态下都有效
    buy_signal, buy_reason = check_buy_conditions(df, index)
    if buy_signal == '+50%':
        return buy_signal, buy_reason

    # 2. 过热区卖出
    sell_signal, sell_reason = check_sell_conditions(df, index)
    if sell_signal == '-80%' and sell_reason == 'Overheat':
        return sell_signal, sell_reason

    # 3. 趋势破坏卖出 —— 任何市场状态
    if sell_signal == '-80%' and sell_reason == 'TrendBroken':
        return sell_signal, sell_reason

    # 4. 标准卖出
    # V2.0 优化：在上涨趋势市中，标准卖出条件更严格（减少卖飞）
    if sell_signal == '-50%':
        if regime == 'trending_up':
            # 上涨趋势中，RSI需要更高且转弱信号足够明显才卖
            latest = df.iloc[index]
            prev = df.iloc[index - 1]
            strong_weakening = (
                latest['RSI14'] >= 63 and                     # RSI较高且有转弱信号
                latest['close'] < prev['close'] and           # 确认收阴
                latest['RSI14'] < prev['RSI14']               # RSI确实下降
            )
            if strong_weakening:
                return sell_signal, sell_reason
            else:
                # 趋势市中不轻易卖出
                return '0%', 'NoSetup_HoldTrend'
        else:
            return sell_signal, sell_reason

    # 5. 修复启动买入
    if buy_signal == '+20%' and buy_reason == 'RecoveryStart':
        # V2.0：在下跌趋势市中，RecoveryStart需要更谨慎
        if regime == 'trending_down':
            # 下跌趋势中，RecoveryStart可能是下跌中继，增加确认
            latest = df.iloc[index]
            prev = df.iloc[index - 1]
            # 需要MACD柱状图也在改善
            if latest['MACD_hist'] > prev['MACD_hist']:
                return buy_signal, buy_reason
            else:
                return '0%', 'NoSetup_RecoveryWeakInDowntrend'
        return buy_signal, buy_reason

    # 6. 回踩买入
    if buy_signal == '+20%' and buy_reason == 'Pullback':
        # Pullback已有趋势过滤，直接通过
        return buy_signal, buy_reason

    # 7. 反转确认加仓
    if buy_signal == '+20%' and buy_reason == 'ReversalConfirm':
        return buy_signal, buy_reason

    return '0%', 'NoSetup'


def run_signal_generator(df):
    """运行策略，生成交易信号"""
    df = calculate_indicators(df)

    signals = []

    for i in range(len(df)):
        current_date = df.index[i]

        if not is_trading_day(current_date):
            signals.append({
                'date': current_date,
                'signal': '0%',
                'reason': 'NonTradingDay'
            })
            continue

        signal, reason = generate_signal(df, i)
        signals.append({
            'date': current_date,
            'signal': signal,
            'reason': reason
        })

    return signals


def get_today_signal(df):
    """获取今日信号（基于最后一个交易日）"""
    if len(df) < 29:
        return "0%", "InsufficientData"

    df = calculate_indicators(df)

    last_index = len(df) - 1
    signal, reason = generate_signal(df, last_index)

    return signal, reason


if __name__ == "__main__":
    import sqlite3
    import sys
    import os

    def get_db_path():
        """获取数据库文件路径（基于脚本所在目录）"""
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            local_db = os.path.join(exe_dir, 'stock_data.db')
            if os.path.exists(local_db):
                return local_db
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_db = os.path.join(script_dir, 'stock_data.db')
        if os.path.exists(local_db):
            return local_db
        parent_dir = os.path.dirname(script_dir)
        v1_db = os.path.join(parent_dir, 'V1.0', 'stock_data.db')
        if os.path.exists(v1_db):
            return v1_db
        return local_db

    db_path = get_db_path()

    try:
        print(f"从 {db_path} 加载数据...")

        conn = sqlite3.connect(db_path)
        df = pd.read_sql_query("SELECT date, open, high, low, close, volume FROM stock_data", conn)
        conn.close()

        if df.empty:
            print("未提取到有效数据")
        else:
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep='first')]

            print(f"成功加载 {len(df)} 条数据")
            print(f"数据范围: {df.index.min()} 至 {df.index.max()}")

            today_signal, today_reason = get_today_signal(df)

            print(f"\n今日信号:")
            print(f"{datetime.now().strftime('%Y-%m-%d')} | {today_signal} | {today_reason}")
    except Exception as e:
        print(f"获取今日信号时出错: {e}")
        import traceback
        traceback.print_exc()
