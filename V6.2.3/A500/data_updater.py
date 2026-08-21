"""
V6.2.3 数据更新模块（多数据源版）
Sina API(主) -> baostock(备) -> akshare(末备)
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

import config


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
            volume REAL,
            is_estimated INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


def migrate_db(db_path=None):
    """对已有数据库执行迁移（添加新列）"""
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE stock_data ADD COLUMN is_estimated INTEGER DEFAULT 0")
        conn.commit()
        print("数据库迁移完成：添加 is_estimated 列")
    except Exception as e:
        if "duplicate column name" in str(e).lower():
            pass  # 列已存在，忽略
        else:
            raise
    finally:
        conn.close()


# ============================
# 数据源 1: Sina 财经 API（主）
# ============================
def fetch_from_sina(stock_code='563360', start_date=None, end_date=None, scale=240, datalen=2000):
    """从新浪财经获取 K 线数据（scale=240为日线，scale=5为5分钟线）"""
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {
        "symbol": f"sh{stock_code}",
        "scale": str(scale),
        "ma": "no",
        "datalen": str(datalen),
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
            # baostock 要求 YYYY-MM-DD，兼容外部传入的 YYYYMMDD 格式
            if '-' not in start and len(start) == 8:
                start = f"{start[:4]}-{start[4:6]}-{start[6:]}"
            if '-' not in end and len(end) == 8:
                end = f"{end[:4]}-{end[4:6]}-{end[6:]}"

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


def backfill_estimated_data(stock_code=None):
    """用真实日线数据回填估算记录（is_estimated=1），即 T+1 数据回填

    Returns:
        回填的记录数
    """
    if stock_code is None:
        stock_code = config.STOCK_CODE
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM stock_data WHERE is_estimated = 1"
        ).fetchall()
        if not rows:
            print("无估算记录需要回填")
            conn.close()
            return 0

        today_str = datetime.now().strftime('%Y-%m-%d')
        print(f"发现 {len(rows)} 条估算记录，开始 T+1 回填...")
        df_real = fetch_from_sina(stock_code, scale=240, datalen=2000)

        backfilled = 0
        for date, est_open, est_high, est_low, est_close, est_vol in rows:
            if date >= today_str:
                continue  # 当日数据尚未收盘，无法回填
            if df_real is None or df_real.empty:
                print(f"⚠ 获取日线数据失败，无法回填 {date}")
                break
            row = df_real[df_real['date'] == date]
            if row.empty:
                print(f"⚠ 数据源暂无 {date} 的真实数据，保留估算值")
                continue
            r = row.iloc[0]
            conn.execute(
                "UPDATE stock_data SET open=?, high=?, low=?, close=?, volume=?, is_estimated=0 WHERE date=?",
                (float(r['open']), float(r['high']), float(r['low']),
                 float(r['close']), float(r['volume']), date)
            )
            deviation = (float(r['volume']) - est_vol) / est_vol * 100 if est_vol else 0.0
            print(f"✓ 回填 {date}: 估算量 {int(est_vol)} → 实际量 {int(r['volume'])}（偏差 {deviation:+.2f}%）")
            backfilled += 1

        conn.commit()
        conn.close()
        print(f"回填完成，共更新 {backfilled} 条记录")
        return backfilled

    except Exception as e:
        print(f"回填估算数据时出错: {e}")
        return 0


# ============================
# 运行上下文检测（运行时间感知）
# ============================
def get_runtime_context(now=None):
    """检测当前运行上下文：是否交易日、所处时段

    Returns:
        dict:
            now: 当前 datetime
            is_trading_day: bool，是否交易日
            phase: 'non_trading_day' | 'pre_market' | 'intraday' | 'closing' | 'post_market'
            description: 中文描述，如 "交易日 · 盘中(9:25-14:55) · 14:32"
    """
    if now is None:
        now = datetime.now()

    # 复用 strategy.is_trading_day（惰性导入避免循环依赖），失败时降级为本地判断
    try:
        from strategy import is_trading_day
        trading_day = bool(is_trading_day(now.date()))
    except Exception as e:
        print(f"⚠ 交易日检测失败（{type(e).__name__}），降级为“周一至周五视为交易日”的本地判断")
        trading_day = now.weekday() < 5

    hm = now.hour * 60 + now.minute  # 当日分钟数
    t_str = now.strftime('%H:%M')

    if not trading_day:
        phase = 'non_trading_day'
        description = f"非交易日(周末/节假日) · {t_str}"
    elif hm < 9 * 60 + 25:
        phase = 'pre_market'
        description = f"交易日 · 盘前(9:25之前) · {t_str}"
    elif hm < 14 * 60 + 55:
        phase = 'intraday'
        description = f"交易日 · 盘中(9:25-14:55) · {t_str}"
    elif hm < 15 * 60:
        phase = 'closing'
        description = f"交易日 · 收盘竞价(14:55-15:00) · {t_str}"
    else:
        phase = 'post_market'
        description = f"交易日 · 盘后(15:00之后) · {t_str}"

    return {
        'now': now,
        'is_trading_day': trading_day,
        'phase': phase,
        'description': description,
    }


def update_intraday(stock_code=None):
    """盘中估算当日数据并写入数据库（is_estimated=1）

    Returns:
        成功写入/更新返回 True，否则 False
    """
    if stock_code is None:
        stock_code = config.STOCK_CODE

    now = datetime.now()
    cur_time = now.strftime('%H:%M')
    if not ('09:25' <= cur_time <= '14:55'):
        print(f"⚠ 当前时间 {cur_time} 不在盘中估算窗口（09:25-14:55）内，已取消")
        return False

    from strategy import is_trading_day
    if not is_trading_day(now.date()):
        print("今天不是交易日，跳过盘中估算")
        return False

    from volume_estimator import estimate_volume
    est = estimate_volume(stock_code, now=now)
    if est is None:
        print("盘中估算失败，未写入数据")
        return False

    db_path = get_db_path()
    try:
        init_database(db_path)
        migrate_db(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_estimated FROM stock_data WHERE date = ?", (est['date'],)
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO stock_data (date, open, high, low, close, volume, is_estimated) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (est['date'], est['open'], est['high'], est['low'], est['close'], est['volume'])
            )
            action = "新增"
        elif row[0] == 1:
            conn.execute(
                "UPDATE stock_data SET open=?, high=?, low=?, close=?, volume=? WHERE date=?",
                (est['open'], est['high'], est['low'], est['close'], est['volume'], est['date'])
            )
            action = "覆盖估算值"
        else:
            conn.close()
            print("已有真实数据，跳过盘中估算")
            return False

        conn.commit()
        conn.close()
        print("=" * 60)
        print(f"盘中估算完成（{action}）: {est['date']}")
        print(f"  开={est['open']:.3f}  高={est['high']:.3f}  低={est['low']:.3f}  收={est['close']:.3f}")
        print(f"  估算成交量={est['volume']}（is_estimated=1，T+1 更新时自动回填）")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"写入盘中估算数据时出错: {e}")
        return False


def update_stock_data(stock_code='563360'):
    """增量更新数据"""
    db_path = get_db_path()
    try:
        init_database(db_path)
        migrate_db(db_path)
        # 先用真实数据回填此前的估算记录（T+1）
        backfill_estimated_data(stock_code)

        conn = sqlite3.connect(db_path)
        existing_data = pd.read_sql_query(
            "SELECT MAX(date) as max_date FROM stock_data WHERE is_estimated = 0", conn)
        last_date = existing_data['max_date'].iloc[0]
        conn.close()

        if last_date:
            print(f"数据库中最新日期: {last_date}")
            start_dt = datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
            if start_dt.date() > datetime.now().date():
                print(f"数据库已包含今日({last_date})数据，无需获取")
                return True
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

        # 仅在收盘前（盘前/盘中/收盘竞价）过滤当日数据，防止写入不完整的当日K线；
        # 盘后（15:00之后）今日数据已完整，允许写入
        ctx = get_runtime_context()
        if ctx['phase'] in ('pre_market', 'intraday', 'closing'):
            today_str = datetime.now().strftime('%Y-%m-%d')
            df_new = df_new[df_new['date'] != today_str]
            if df_new.empty:
                print("过滤当日数据后无新数据需要写入（收盘后运行 --update 即可获取今日完整数据）")
                return True

        conn = sqlite3.connect(db_path)
        # 使用 INSERT OR REPLACE 避免主键冲突（重复运行不报错）
        records = df_new[['date', 'open', 'high', 'low', 'close', 'volume']].values.tolist()
        conn.executemany(
            "INSERT OR REPLACE INTO stock_data "
            "(date, open, high, low, close, volume, is_estimated) VALUES (?, ?, ?, ?, ?, ?, 0)",
            records
        )
        conn.commit()
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
    """从数据库加载数据为DataFrame（不包含 is_estimated，对下游透明）"""
    if db_path is None:
        db_path = get_db_path()
    if os.path.exists(db_path):
        migrate_db(db_path)
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
