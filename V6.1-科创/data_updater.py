"""
V5.0 数据更新模块
"""
import pandas as pd
from datetime import datetime, timedelta
import sqlite3
import sys
import os
import akshare as ak
import time


def get_db_path():
    """获取数据库文件路径（V5.0优先 → V4.0 → V2.0 → V1.0 回退）"""
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
    for ver in ['V4.0', 'V2.0', 'V1.0']:
        db = os.path.join(parent_dir, ver, 'stock_data.db')
        if os.path.exists(db):
            return db
    return local_db


def init_database(db_path):
    """初始化数据库表"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_data (
            date TEXT PRIMARY KEY,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL
        )
    ''')
    conn.commit()
    conn.close()


def fetch_stock_data_with_retry(stock_code='589800', start_date=None, end_date=None):
    """持续重试直到成功的数据获取函数（Ctrl+C 可中断）"""
    attempt = 0
    while True:
        try:
            if attempt > 0:
                wait_time = min(3 * (attempt + 1), 60)
                print(f"第{attempt + 1}次重试，等待{wait_time}秒...")
                time.sleep(wait_time)
            else:
                print(f"正在从API获取 {stock_code} 的数据...")

            df = ak.fund_etf_hist_em(
                symbol=stock_code,
                period="daily",
                start_date=start_date if start_date else "20200101",
                end_date=end_date if end_date else datetime.now().strftime("%Y%m%d"),
                adjust=""
            )

            if df.empty:
                print("未获取到数据，稍后重试...")
                attempt += 1
                continue

            df.rename(columns={
                '日期': 'date', '开盘': 'open', '最高': 'high',
                '最低': 'low', '收盘': 'close', '成交量': 'volume'
            }, inplace=True)

            df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

            print(f"成功获取 {len(df)} 条数据")
            return df

        except KeyboardInterrupt:
            print("\n用户中断，退出数据获取")
            raise
        except Exception as e:
            attempt += 1
            print(f"获取数据时出错（第{attempt}次）: {type(e).__name__}: {str(e)[:100]}")


def update_stock_data(stock_code='589800'):
    """增量更新数据"""
    db_path = get_db_path()
    try:
        init_database(db_path)
        conn = sqlite3.connect(db_path)
        existing_data = pd.read_sql_query(
            "SELECT MAX(date) as max_date FROM stock_data", conn)
        last_date = existing_data['max_date'].iloc[0]

        if last_date:
            print(f"数据库中最新日期: {last_date}")
            start_dt = datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
            start_date = start_dt.strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')
            print(f"将获取 {start_date} 至 {end_date} 的数据")
        else:
            print("数据库为空，将获取全部历史数据")
            start_date = None
            end_date = None
        conn.close()

        df_new = fetch_stock_data_with_retry(stock_code, start_date=start_date, end_date=end_date)
        if df_new is None or df_new.empty:
            print("未获取到新数据")
            return False

        conn = sqlite3.connect(db_path)
        df_new.to_sql('stock_data', conn, if_exists='append', index=False)
        total_records = pd.read_sql_query("SELECT COUNT(*) as count FROM stock_data", conn)
        print(f"数据库中共有 {total_records['count'].iloc[0]} 条记录")
        conn.close()
        print("数据更新成功！")
        return True

    except Exception as e:
        print(f"更新数据时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


def full_refresh_data(stock_code='589800'):
    """完全刷新数据"""
    db_path = get_db_path()
    try:
        print("正在完全刷新数据...")
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"已删除旧数据库: {db_path}")
        init_database(db_path)

        df = fetch_stock_data_with_retry(stock_code, start_date=None, end_date=None)
        if df is None or df.empty:
            print("获取数据失败")
            return False

        conn = sqlite3.connect(db_path)
        df.to_sql('stock_data', conn, if_exists='append', index=False)
        total_records = pd.read_sql_query("SELECT COUNT(*) as count FROM stock_data", conn)
        print(f"数据库中共有 {total_records['count'].iloc[0]} 条记录")
        conn.close()
        print("数据完全刷新成功！")
        return True

    except Exception as e:
        print(f"刷新数据时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


def load_data_from_db(db_path=None):
    """从数据库加载数据为DataFrame"""
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume FROM stock_data ORDER BY date", conn)
    conn.close()
    if df.empty:
        return None
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    return df


if __name__ == "__main__":
    print("=" * 60)
    print("V5.0 数据更新工具")
    print("=" * 60)
    print("\n请选择操作:")
    print("1. 增量更新数据（推荐）")
    print("2. 完全刷新数据（耗时较长）")
    choice = input("\n请输入选项 (1/2): ").strip()
    if choice == '2':
        success = full_refresh_data()
    else:
        success = update_stock_data()
    if success:
        print("\n操作完成！")
    else:
        print("\n操作失败！")
