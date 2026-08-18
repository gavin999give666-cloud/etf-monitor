import pandas as pd
from datetime import datetime, timedelta
import sqlite3
import sys
import os
import akshare as ak
import time

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

def fetch_stock_data_with_retry(stock_code='563360', start_date=None, end_date=None):
    """带重试机制的数据获取函数"""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait_time = 3 * (attempt + 1)
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
                print("未获取到数据")
                return None
            
            df.rename(columns={
                '日期': 'date',
                '开盘': 'open',
                '最高': 'high',
                '最低': 'low',
                '收盘': 'close',
                '成交量': 'volume'
            }, inplace=True)
            
            df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
            
            print(f"成功获取 {len(df)} 条数据")
            return df
            
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"获取数据时出错: {type(e).__name__}: {str(e)[:100]}")
            else:
                print(f"\n获取数据失败（已重试{max_retries}次）")
                print(f"最后错误: {type(e).__name__}: {e}")
                return None

def update_stock_data(stock_code='563360'):
    """更新股票数据到数据库（增量更新）
    
    Args:
        stock_code: 股票代码
    
    Returns:
        bool: 是否成功更新
    """
    db_path = get_db_path()
    
    try:
        # 初始化数据库
        init_database(db_path)
        
        # 连接数据库
        conn = sqlite3.connect(db_path)
        
        # 获取数据库中最新的日期
        existing_data = pd.read_sql_query(
            "SELECT MAX(date) as max_date FROM stock_data", 
            conn
        )
        conn.close()
        
        last_date = existing_data['max_date'].iloc[0]
        
        # 健壮性检查：处理 None、NaN、NaT、空字符串等情况
        if last_date is None or (isinstance(last_date, float) and pd.isna(last_date)) or str(last_date).strip() == '' or str(last_date).strip() == 'NaT':
            last_date = None
        
        if last_date is not None:
            last_date = str(last_date).strip()
            print(f"数据库中最新日期: {last_date}")
            
            # 检查是否已经是最新（数据库日期 >= 今天）
            today_str = datetime.now().strftime('%Y-%m-%d')
            if last_date >= today_str:
                print(f"数据库已是最新（{last_date}），无需更新")
                # 确认数据库记录数
                conn = sqlite3.connect(db_path)
                total_records = pd.read_sql_query("SELECT COUNT(*) as count FROM stock_data", conn)
                conn.close()
                print(f"数据库中共有 {total_records['count'].iloc[0]} 条记录")
                return True
            
            # 从最后一天的第二天开始获取
            start_dt = datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
            start_date = start_dt.strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')
            print(f"将获取 {start_date} 至 {end_date} 的数据")
        else:
            print("数据库为空，将获取全部历史数据")
            start_date = None
            end_date = None
        
        # 从API获取数据
        df_new = fetch_stock_data_with_retry(stock_code, start_date=start_date, end_date=end_date)
        
        if df_new is None or df_new.empty:
            print("未获取到新数据")
            return False
        
        # 保存到数据库（使用 INSERT OR REPLACE 避免主键冲突）
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        saved_count = 0
        for _, row in df_new.iterrows():
            try:
                cursor.execute('''
                    INSERT OR REPLACE INTO stock_data 
                    (date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (str(row['date']), float(row['open']), float(row['high']), 
                      float(row['low']), float(row['close']), float(row['volume'])))
                saved_count += 1
            except Exception as e:
                print(f"保存数据行 {row['date']} 失败: {e}")
        
        conn.commit()
        print(f"成功保存 {saved_count} 条新记录")
        
        # 验证数据
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

def manual_update_data():
    """手动更新数据 - 当自动更新失败时使用"""
    db_path = get_db_path()
    
    print("=" * 80)
    print("手动数据更新工具")
    print("=" * 80)
    
    try:
        # 获取数据库中最新的日期
        conn = sqlite3.connect(db_path)
        existing_data = pd.read_sql_query(
            "SELECT MAX(date) as max_date FROM stock_data", 
            conn
        )
        conn.close()
        
        last_date = existing_data['max_date'].iloc[0]
        
        if last_date:
            print(f"\n数据库中最新日期: {last_date}")
            start_dt = datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
            start_date = start_dt.strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')
            print(f"需要更新: {start_date} 至 {end_date}")
        else:
            print("\n数据库为空")
            return False
        
        # 尝试获取数据
        print("\n开始获取新数据...")
        df_new = fetch_stock_data_with_retry('563360', start_date=start_date, end_date=end_date)
        
        if df_new is None or df_new.empty:
            print("\n自动获取失败，请使用手动方式:")
            print("1. 在Python交互环境中执行:")
            print(f"   import akshare as ak")
            print(f"   df = ak.fund_etf_hist_em(symbol='563360', period='daily', start_date='{start_date}', end_date='{end_date}')")
            print(f"   df.to_csv('temp_update.csv', index=False)")
            print("\n2. 然后重新运行此脚本选择导入CSV文件")
            return False
        
        # 保存到数据库
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        saved_count = 0
        for _, row in df_new.iterrows():
            try:
                cursor.execute('''
                    INSERT OR REPLACE INTO stock_data 
                    (date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (row['date'], float(row['open']), float(row['high']), 
                      float(row['low']), float(row['close']), float(row['volume'])))
                saved_count += 1
            except Exception as e:
                print(f"保存数据行失败: {e}")
        
        conn.commit()
        
        # 验证数据
        total_records = pd.read_sql_query("SELECT COUNT(*) as count FROM stock_data", conn)
        conn.close()
        
        print(f"\n成功保存 {saved_count} 条新记录")
        print(f"数据库中共有 {total_records['count'].iloc[0]} 条记录")
        
        # 显示最新数据
        conn = sqlite3.connect(db_path)
        latest = pd.read_sql_query("SELECT * FROM stock_data ORDER BY date DESC LIMIT 3", conn)
        conn.close()
        print("\n最新3条数据:")
        print(latest.to_string(index=False))
        
        print("\n数据更新成功！")
        return True
        
    except Exception as e:
        print(f"更新数据时出错: {e}")
        import traceback
        traceback.print_exc()
        return False

def full_refresh_data(stock_code='563360'):
    """完全刷新数据（清空后重新获取）
    
    Args:
        stock_code: 股票代码
    
    Returns:
        bool: 是否成功刷新
    """
    db_path = get_db_path()
    
    try:
        print("正在完全刷新数据...")
        
        # 删除旧数据库
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"已删除旧数据库: {db_path}")
        
        # 初始化数据库
        init_database(db_path)
        
        # 获取全部历史数据（使用带重试机制的函数）
        df = fetch_stock_data_with_retry(stock_code, start_date=None, end_date=None)
        
        if df is None or df.empty:
            print("获取数据失败")
            return False
        
        # 保存到数据库（使用 INSERT OR REPLACE 避免主键冲突）
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        saved_count = 0
        for _, row in df.iterrows():
            try:
                cursor.execute('''
                    INSERT OR REPLACE INTO stock_data 
                    (date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (str(row['date']), float(row['open']), float(row['high']), 
                      float(row['low']), float(row['close']), float(row['volume'])))
                saved_count += 1
            except Exception as e:
                print(f"保存数据行 {row['date']} 失败: {e}")
        
        conn.commit()
        print(f"成功保存 {saved_count} 条记录")
        
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

if __name__ == "__main__":
    print("=" * 80)
    print("股票数据更新工具")
    print("=" * 80)
    
    print("\n请选择操作:")
    print("1. 增量更新数据（推荐）")
    print("2. 完全刷新数据（耗时较长）")
    print("3. 手动更新/故障排除（增强模式）")
    
    choice = input("\n请输入选项 (1/2/3): ").strip()
    
    if choice == '2':
        success = full_refresh_data()
    elif choice == '3':
        success = manual_update_data()
    else:
        success = update_stock_data()
    
    if success:
        print("\n操作完成！")
    else:
        print("\n操作失败！")
