import pandas as pd
from datetime import datetime, time
import numpy as np

# 策略名称：科创100ETF波段优化策略（纯信号发生器版）

# 主要节假日列表（2026年）
MAJOR_HOLIDAYS_2026 = [
    '2026-01-01',
    '2026-02-01',
    '2026-02-02',
    '2026-02-03',
    '2026-02-04',
    '2026-02-05',
    '2026-04-04',
    '2026-04-05',
    '2026-05-01',
    '2026-05-02',
    '2026-05-03',
    '2026-05-04',
    '2026-05-05',
    '2026-06-25',
    '2026-06-26',
    '2026-09-27',
    '2026-10-01',
    '2026-10-02',
    '2026-10-03',
    '2026-10-04',
    '2026-10-05',
    '2026-10-06',
    '2026-10-07',
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
    """计算策略所需的全部指标"""
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA10'] = df['close'].rolling(window=10).mean()
    df['MA20'] = df['close'].rolling(window=20).mean()

    df['STD20'] = df['close'].rolling(window=20).std(ddof=0)

    df['Z20'] = (df['close'] - df['MA20']) / df['STD20']
    df.loc[df['STD20'] == 0, 'Z20'] = 0

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta).where(delta < 0, 0)

    avg_gain = np.zeros(len(df))
    avg_loss = np.zeros(len(df))

    if len(df) >= 15:
        avg_gain[14] = gain.iloc[1:15].mean()
        avg_loss[14] = loss.iloc[1:15].mean()

        for i in range(15, len(df)):
            avg_gain[i] = (avg_gain[i-1] * 13 + gain.iloc[i]) / 14
            avg_loss[i] = (avg_loss[i-1] * 13 + loss.iloc[i]) / 14
    else:
        for i in range(1, len(df)):
            if i == 1:
                avg_gain[i] = gain.iloc[i]
                avg_loss[i] = loss.iloc[i]
            else:
                avg_gain[i] = (avg_gain[i-1] * 13 + gain.iloc[i]) / 14
                avg_loss[i] = (avg_loss[i-1] * 13 + loss.iloc[i]) / 14

    rs = avg_gain / (avg_loss + 1e-10)
    df['RSI14'] = 100 - (100 / (1 + rs))

    df['ROC5'] = df['close'].pct_change(periods=5)

    df['Vol20'] = df['volume'].rolling(window=20).mean()

    return df

def check_buy_conditions(df, index):
    """检查买入条件（纯市场状态，不考虑仓位）"""
    if index < 20:
        return None, None

    latest = df.iloc[index]
    prev = df.iloc[index-1]

    # 深超卖大幅买入（最高优先级）
    if (
        latest['Z20'] <= -1.8 and
        latest['RSI14'] <= 45 and
        latest['ROC5'] <= -0.03 and
        latest['close'] < latest['MA20'] and
        latest['volume'] >= latest['Vol20']
    ):
        return '+50%', 'DeepReversal'

    # 上升趋势中的回踩买入
    if (
        abs(latest['close'] - latest['MA10']) / latest['MA10'] <= 0.015 and
        -1.1 <= latest['Z20'] <= -0.4 and
        40 <= latest['RSI14'] <= 50 and
        latest['ROC5'] <= 0.01 and
        latest['volume'] <= 1.10 * latest['Vol20']
    ):
        # 首次触发检查
        if index >= 21:
            prev_cond = (
                abs(prev['close'] - prev['MA10']) / prev['MA10'] <= 0.015 and
                -1.1 <= prev['Z20'] <= -0.4 and
                40 <= prev['RSI14'] <= 50 and
                prev['ROC5'] <= 0.01 and
                prev['volume'] <= 1.10 * prev['Vol20']
            )
            if not prev_cond:
                return '+20%', 'Pullback'

    # 修复启动买入
    if (
        latest['close'] < latest['MA20'] and
        latest['close'] >= latest['MA10'] * 0.985 and
        -1.6 <= latest['Z20'] <= -0.6 and
        45 <= latest['RSI14'] <= 58 and
        latest['ROC5'] > -0.03 and
        latest['ROC5'] <= 0.02
    ):
        return '+20%', 'RecoveryStart'

    # 反转确认加仓（收紧条件）
    if index >= 21:
        prev_prev = df.iloc[index-2]
        reversal_confirm = (
            latest['close'] > latest['MA20'] and
            prev['close'] <= prev['MA20'] and  # 刚刚站上MA20
            latest['RSI14'] >= 45 and
            latest['ROC5'] > 0
        )
        if reversal_confirm:
            return '+20%', 'ReversalConfirm'

    return None, None

def check_sell_conditions(df, index):
    """检查卖出条件（纯市场状态，不考虑仓位）"""
    if index < 20:
        return None, None

    latest = df.iloc[index]
    prev = df.iloc[index-1]

    # 过热区大幅卖出
    if (
        latest['close'] > latest['MA20'] and prev['close'] > prev['MA20'] and
        latest['RSI14'] >= 67 and
        latest['Z20'] >= 1.6 and
        latest['ROC5'] >= 0.03
    ):
        # 首次触发检查
        if index >= 21:
            prev_cond = (
                prev['close'] > prev['MA20'] and df.iloc[index-2]['close'] > df.iloc[index-2]['MA20'] and
                prev['RSI14'] >= 67 and
                prev['Z20'] >= 1.6 and
                prev['ROC5'] >= 0.03
            )
            if not prev_cond:
                return '-80%', 'Overheat'

    # 标准卖出
    if (
        latest['close'] >= latest['MA10'] and
        latest['Z20'] >= 0.6 and
        latest['RSI14'] >= 60
    ):
        # 首次触发检查
        if index >= 21:
            prev_cond = (
                prev['close'] >= prev['MA10'] and
                prev['Z20'] >= 0.6 and
                prev['RSI14'] >= 60
            )
            if not prev_cond:
                return '-50%', 'StandardExit'

    # 趋势破坏强制卖出（只有在非极端超卖时才生效）
    if (
        latest['close'] < latest['MA20'] and
        prev['close'] < prev['MA20']
    ):
        # 非极端超卖时才触发
        if not (latest['Z20'] <= -1.8 and latest['RSI14'] <= 45):
            # 首次触发检查
            if index >= 21:
                prev_cond = (
                    prev['close'] < prev['MA20'] and
                    df.iloc[index-2]['close'] < df.iloc[index-2]['MA20']
                )
                if not prev_cond:
                    return '-80%', 'TrendBroken'

    return None, None

def generate_signal(df, index):
    """生成交易信号（新优先级顺序）"""
    # 1. 深超卖买入（最高优先级）
    buy_signal, buy_reason = check_buy_conditions(df, index)
    if buy_signal == '+50%':
        return buy_signal, buy_reason

    # 2. 过热区卖出
    sell_signal, sell_reason = check_sell_conditions(df, index)
    if sell_signal == '-80%' and sell_reason == 'Overheat':
        return sell_signal, sell_reason

    # 3. 标准卖出
    if sell_signal == '-50%':
        return sell_signal, sell_reason

    # 4. 趋势破坏卖出
    if sell_signal == '-80%' and sell_reason == 'TrendBroken':
        return sell_signal, sell_reason

    # 5. 修复启动买入
    if buy_signal == '+20%' and buy_reason == 'RecoveryStart':
        return buy_signal, buy_reason

    # 6. 回踩买入
    if buy_signal == '+20%' and buy_reason == 'Pullback':
        return buy_signal, buy_reason

    # 7. 反转确认加仓
    if buy_signal == '+20%' and buy_reason == 'ReversalConfirm':
        return buy_signal, buy_reason

    return '0%', 'NoSetup'

def run_signal_generator(df):
    """运行策略，生成交易信号（纯信号发生器模式）"""
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
    if len(df) < 20:
        return "0%", "InsufficientData"

    df = calculate_indicators(df)

    last_index = len(df) - 1
    signal, reason = generate_signal(df, last_index)

    return signal, reason

if __name__ == "__main__":
    import sqlite3
    from datetime import datetime
    import sys
    import os
    
    def get_db_path():
        """获取数据库文件路径"""
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            local_db = os.path.join(exe_dir, 'stock_data.db')
            if os.path.exists(local_db):
                return local_db
        return os.path.join(os.path.abspath("."), 'stock_data.db')
    
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