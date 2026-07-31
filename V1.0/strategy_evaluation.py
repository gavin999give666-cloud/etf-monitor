import pandas as pd
from datetime import datetime
import sqlite3
import sys
import os
from strategy import run_signal_generator, calculate_indicators, get_today_signal, parse_signal

stock_code = '563360'

def get_db_path():
    """获取数据库文件路径"""
    # 优先检查 exe 同级目录
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        local_db = os.path.join(exe_dir, 'stock_data.db')
        if os.path.exists(local_db):
            return local_db
    
    # 开发环境当前目录
    return os.path.join(os.path.abspath("."), 'stock_data.db')

db_path = get_db_path()

def load_db_data():
    """从SQLite数据库加载数据"""
    try:
        print(f"从 {db_path} 加载数据...")

        conn = sqlite3.connect(db_path)
        df = pd.read_sql_query("SELECT date, open, high, low, close, volume FROM stock_data", conn)
        conn.close()

        if df.empty:
            print("未提取到有效数据")
            return None

        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep='first')]

        print(f"成功加载 {len(df)} 条数据")
        print(f"数据时间范围: {df.index[0].strftime('%Y-%m-%d')} 到 {df.index[-1].strftime('%Y-%m-%d')}")

        return df
    except Exception as e:
        print(f"加载数据库时出错: {e}")
        import traceback
        traceback.print_exc()
        return None

def backtest_strategy(df):
    """回测策略"""
    if len(df) < 20:
        print("数据不足，无法回测")
        return []

    signals = run_signal_generator(df)

    return signals

def calculate_returns(df, signals):
    """计算收益率和胜率
    
    基准收益：一次性买入1万元并持有到结束
    策略收益：初始买入6000元（60%仓位），根据信号调仓，最大仓位不超过100%
    """
    start_date = pd.Timestamp('2025-09-01')
    
    df_backtest = df[df.index >= start_date]
    signals_backtest = [s for s in signals if s['date'] >= start_date]
    
    if len(df_backtest) == 0 or len(signals_backtest) == 0:
        print("2025-09-01及以后无数据")
        return 0.0, 0.0, 0, 0, 0.0, 0.0
    
    start_price = df_backtest.iloc[0]['close']
    end_price = df_backtest.iloc[-1]['close']
    
    # ---- 基准收益：一次性买入1万元，持有到结束 ----
    benchmark_investment = 10000.0
    benchmark_shares = benchmark_investment / start_price
    benchmark_final = benchmark_shares * end_price
    benchmark_return = (benchmark_final - benchmark_investment) / benchmark_investment
    
    # ---- 策略收益：初始买入6000元，然后根据信号调仓 ----
    initial_cash = 10000.0
    initial_invest = 6000.0
    cash = initial_cash - initial_invest      # 剩余现金 4000
    shares = initial_invest / start_price     # 初始持仓股数
    position_pct = initial_invest / initial_cash  # 初始仓位 60%
    
    trade_count = 0
    winning_trades = 0
    
    for i, signal in enumerate(signals_backtest):
        date = signal['date']
        if date not in df_backtest.index:
            continue
        
        current_price = df_backtest.loc[date]['close']
        signal_str = signal['signal']
        
        if signal_str == '0%':
            continue
        
        delta = parse_signal(signal_str)
        # 目标仓位限制在 [0, 1] 之间
        target_pct = max(0.0, min(1.0, position_pct + delta))
        
        current_value = shares * current_price
        total_asset = cash + current_value
        
        target_value = total_asset * target_pct
        trade_value = target_value - current_value
        
        if abs(trade_value) > 100:  # 交易价值大于100元才执行
            trade_count += 1
            
            shares += trade_value / current_price
            cash -= trade_value
            
            # 判断该笔交易是否盈利（基于下一交易日价格）
            if i < len(signals_backtest) - 1:
                next_date = signals_backtest[i+1]['date']
                if next_date in df_backtest.index:
                    next_price = df_backtest.loc[next_date]['close']
                    if delta > 0:        # 买入后上涨为盈利
                        if next_price > current_price:
                            winning_trades += 1
                    else:                # 卖出后下跌为盈利
                        if next_price < current_price:
                            winning_trades += 1
        
        position_pct = target_pct
    
    final_value = cash + shares * end_price
    strategy_return = (final_value - initial_cash) / initial_cash
    
    win_rate = winning_trades / trade_count if trade_count > 0 else 0.0
    
    return strategy_return, win_rate, trade_count, winning_trades, benchmark_return, final_value

def evaluate_strategy():
    """评估策略"""
    print(f"策略评估开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    df = load_db_data()
    if df is None:
        print("无法获取历史数据，无法评估策略")
        return

    print(f"获取到 {len(df)} 天的历史数据")
    print(f"数据时间范围: {df.index[0].strftime('%Y-%m-%d')} 到 {df.index[-1].strftime('%Y-%m-%d')}")
    signals = backtest_strategy(df)

    if signals:
        print("\n" + "=" * 80)
        print("交易信号:")
        print("-" * 80)
        print("Date       | Signal | Reason")
        print("-" * 80)

        signal_count = {'+50%': 0, '+20%': 0, '-80%': 0, '-50%': 0, '0%': 0}

        for signal in signals:
            date_str = signal['date'].strftime('%Y-%m-%d')
            sig = signal['signal']
            reason = signal['reason']
            print(f"{date_str} | {sig:6} | {reason}")
            signal_count[sig] = signal_count.get(sig, 0) + 1

        print("-" * 80)
        print(f"共生成 {len(signals)} 条交易记录")
        print(f"信号统计: +50%={signal_count['+50%']}, +20%={signal_count['+20%']}, -80%={signal_count['-80%']}, -50%={signal_count['-50%']}, 0%={signal_count['0%']}")
        print("-" * 80)

        # 计算收益率和胜率
        strategy_return, win_rate, trade_count, winning_trades, benchmark_return, final_value = calculate_returns(df, signals)
        
        print("\n" + "=" * 80)
        print("回测结果（从2025-09-01开始，初始资金1万元，策略初始仓位60%）:")
        print("-" * 80)
        print(f"策略收益率: {strategy_return * 100:.2f}%")
        print(f"基准收益率（买入持有）: {benchmark_return * 100:.2f}%")
        print(f"总交易次数: {trade_count}")
        print(f"盈利交易次数: {winning_trades}")
        print(f"胜率: {win_rate * 100:.2f}%")
        print(f"最终资产: {final_value:.2f} 元")
        print("-" * 80)

    print("=" * 80)
    print("策略评估完成！")

if __name__ == "__main__":
    evaluate_strategy()