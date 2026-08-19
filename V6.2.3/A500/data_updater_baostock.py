"""
V6.2.3 数据更新模块
多数据源: Sina API(主) -> baostock(备) -> akshare(末备)
最稳定免费的 A 股/ETF 日线数据获取方案
"""
import pandas as pd
import requests
import json
from datetime import datetime, timedelta
import sqlite3
import sys
import os
import time


def get_db_path():
    """获取数据库文件路径"""
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


# ============================
# 数据源 1: Sina 财经 API（主）
# ============================
def fetch_from_sina(stock_code='563360', start_date=None, end_date=None):
    """从新浪财经获取日线 K 线数据"""
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {
        "symbol": f"sh{stock_code}",
        "scale": "240",
        "ma": "no",
        "datalen": "2000",
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for attempt in range(5):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"Sina API 状态码 {r.status_code}，重试...")
                time.sleep(2)
                continue

            data = json.loads(r.text)
            if not data:
                print("Sina API 返回空数据")
                time.sleep(2)
                continue

            df = pd.DataFrame(data)
            df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']

            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            df = df.dropna(subset=['close'])
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
            df = df.sort_values('date').reset_index(drop=True)

            if start_date:
                s = start_date.replace('-', '')[:8]
                df = df[df['date'] >= f"{s[:4]}-{s[4:6]}-{s[6:]}"]
            if end_date:
                e = end_date.replace('-', '')[:8]
                df = df[df['date'] <= f"{e[:4]}-{e[4:6]}-{e[6:]}"]

            if df.empty:
                print("Sina API: 日期范围内无数据")
                return None

            print(f"成功获取 {len(df)} 条数据（Sina）")
            return df

        except json.JSONDecodeError:
            print("Sina API 返回非 JSON 格式，重试...")
            time.sleep(2)
        except Exception as e:
            print(f"Sina API 出错: {type(e).__name__}: {str(e)[:80]}，重试...")
            time.sleep(2)

    return None


# ============================
# 数据源 2: baostock（备）
# ============================
def fetch_from_baostock(stock_code='563360', start_date=None, end_date=None):
    """从 baostock 获取日线数据"""
    import baostock as bs

    code = str(stock_code).strip()
    if code.startswith('6') or code.startswith('58'):
        bs_code = f'sh.{code}'
    elif code.startswith(('0', '3')):
        bs_code = f'sz.{code}'
    else:
        bs_code = f'sh.{code}'

    for attempt in range(5):
        try:
            lg = bs.login()
            if lg.error_code != '0':
                print(f"baostock 登录失败: {lg.error_msg}")
                time.sleep(3)
                continue

            start = (start_date or "2020-01-01").replace('_', '-').replace('/', '-')
            end = (end_date or datetime.now().strftime("%Y-%m-%d")).replace('_', '-').replace('/', '-')

            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume",
                start_date=start, end_date=end,
                frequency='d', adjustflag='3'
            )

            if rs is None:
                bs.logout()
                time.sleep(3)
                continue

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
            bs.logout()

            if not data_list:
                print("baostock 无数据")
                return None

            df = pd.DataFrame(data_list, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna(subset=['close'])
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

            print(f"成功获取 {len(df)} 条数据（baostock）")
            return df

        except Exception as e:
            print(f"baostock 出错: {type(e).__name__}: {str(e)[:80]}")
            time.sleep(3)

    return None


# ============================
# 数据源 3: akshare（末备）
# ============================
def fetch_from_akshare(stock_code='563360', start_date=None, end_date=None):
    """从 akshare（东方财富）获取日线数据"""
    try:
        import akshare as ak
    except ImportError:
        return None

    for attempt in range(5):
        try:
            start = (start_date or "20200101").replace('-', '')
            end = (end_date or datetime.now().strftime("%Y%m%d")).replace('-', '')

            df = ak.fund_etf_hist_em(
                symbol=stock_code, period="daily",
                start_date=start, end_date=end, adjust=""
            )

            if df.empty:
                time.sleep(3)
                continue

            df.rename(columns={
                '日期': 'date', '开盘': 'open', '最高': 'high',
                '最低': 'low', '收盘': 'close', '成交量': 'volume'
            }, inplace=True)
            df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

            print(f"成功获取 {len(df)} 条数据（akshare）")
            return df

        except Exception as e:
            print(f"akshare 出错: {type(e).__name__}: {str(e)[:80]}，重试...")
            time.sleep(3)

    return None


# ============================
# 统一获取入口
# ============================
def fetch_stock_data(stock_code='563360', start_date=None, end_date=None):
    """多数据源自动切换获取日线数据"""
    df = fetch_from_sina(stock_code, start_date, end_date)
    if df is not None and not df.empty:
        return df

    print("Sina 不可用，尝试 baostock...")
    df = fetch_from_baostock(stock_code, start_date, end_date)
    if df is not None and not df.empty:
        return df

    print("baostock 不可用，尝试 akshare...")
    df = fetch_from_akshare(stock_code, start_date, end_date)
    if df is not None and not df.empty:
        return df

    print("所有数据源均失败")
    return None


# ============================
# 业务函数
# ============================
def update_stock_data(stock_code='563360'):
    """增量更新数据"""
    db_path = get_db_path()
    try:
        init_database(db_path)
        conn = sqlite3.connect(db_path)
        existing_data = pd.read_sql_query(
            "SELECT MAX(date) as max_date FROM stock_data", conn)
        last_date = existing_data['max_date'].iloc[0]
        conn.close()

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

        df_new = fetch_stock_data(stock_code, start_date=start_date, end_date=end_date)
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


def full_refresh_data(stock_code='563360'):
    """完全刷新数据"""
    db_path = get_db_path()
    try:
        print("正在完全刷新数据...")
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"已删除旧数据库: {db_path}")
        init_database(db_path)

        df = fetch_stock_data(stock_code, start_date=None, end_date=None)
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
    print("V6.2.3 数据更新工具（Sina + baostock + akshare）")
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
