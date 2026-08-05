import tkinter as tk
from tkinter import scrolledtext, messagebox
import sys
import threading
import os
import winreg
import sqlite3
import pandas as pd
from datetime import datetime
from io import StringIO
import contextlib

# 导入 V2.0 策略模块
try:
    from strategy import get_today_signal, calculate_indicators
    from strategy_evaluation import evaluate_strategy as eval_strategy_func
    from data_updater import update_stock_data, full_refresh_data
except ImportError:
    pass


def resource_path(relative_path):
    """获取资源文件的绝对路径，兼容开发环境和PyInstaller打包环境"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    else:
        return os.path.join(os.path.abspath("."), relative_path)


def get_db_path():
    """获取数据库文件路径（基于脚本所在目录，避免CWD依赖）"""
    # 优先检查 exe 同级目录
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        local_db = os.path.join(exe_dir, 'stock_data.db')
        if os.path.exists(local_db):
            return local_db

    # 基于脚本文件位置
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_db = os.path.join(script_dir, 'stock_data.db')
    if os.path.exists(local_db):
        return local_db

    # 回退到同级 V1.0 目录
    parent_dir = os.path.dirname(script_dir)
    v1_db = os.path.join(parent_dir, 'V1.0', 'stock_data.db')
    if os.path.exists(v1_db):
        return v1_db

    return local_db


def capture_output_to_string(func, *args, **kwargs):
    """捕获函数打印输出为字符串"""
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    mystdout = StringIO()
    mystderr = StringIO()

    try:
        sys.stdout = mystdout
        sys.stderr = mystderr
        func(*args, **kwargs)
        output = mystdout.getvalue()
        error = mystderr.getvalue()
        return output + (error if error else "")
    except Exception as e:
        return f"执行出错: {str(e)}\n{mystdout.getvalue()}"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def run_strategy_today():
    """运行今日操作建议"""
    def _run():
        try:
            db_path = get_db_path()

            # 初始化数据库表
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

            # 加载数据
            conn = sqlite3.connect(db_path)
            df = pd.read_sql_query("SELECT date, open, high, low, close, volume FROM stock_data", conn)
            conn.close()

            output_lines = []

            if df.empty:
                output_lines.append(f"从 {db_path} 加载数据...")
                output_lines.append("数据库中没有数据，尝试从API获取最新数据...")
                root.after(0, lambda: display_output("\n".join(output_lines)))

                # 尝试从API获取数据
                try:
                    import akshare as ak

                    stock_code = '563360'
                    start_date = (datetime.now().replace(year=datetime.now().year - 1)).strftime('%Y%m%d')
                    end_date = datetime.now().strftime('%Y%m%d')

                    output_lines.append(f"正在从akshare获取 {stock_code} 的数据...")
                    root.after(0, lambda: display_output("\n".join(output_lines)))

                    df_api = ak.fund_etf_hist_em(symbol=stock_code, period="daily",
                                                  start_date=start_date,
                                                  end_date=end_date)

                    if df_api is not None and not df_api.empty:
                        df_api = df_api.rename(columns={
                            '日期': 'date',
                            '开盘': 'open',
                            '最高': 'high',
                            '最低': 'low',
                            '收盘': 'close',
                            '成交量': 'volume'
                        })
                        df_api = df_api[['date', 'open', 'high', 'low', 'close', 'volume']]
                        df_api['date'] = pd.to_datetime(df_api['date']).dt.strftime('%Y-%m-%d')

                        # 保存到数据库
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()
                        saved_count = 0
                        for _, row in df_api.iterrows():
                            try:
                                cursor.execute('''
                                    INSERT OR REPLACE INTO stock_data
                                    (date, open, high, low, close, volume)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                ''', (row['date'], float(row['open']), float(row['high']),
                                      float(row['low']), float(row['close']), float(row['volume'])))
                                saved_count += 1
                            except Exception as e:
                                pass

                        conn.commit()
                        conn.close()

                        output_lines.append(f"成功获取并保存 {saved_count} 条数据")

                        # 重新加载数据
                        conn = sqlite3.connect(db_path)
                        df = pd.read_sql_query("SELECT date, open, high, low, close, volume FROM stock_data", conn)
                        conn.close()
                    else:
                        output_lines.append("无法从API获取数据")
                        output_lines.append("请安装akshare: pip install akshare")
                        root.after(0, lambda: display_output("\n".join(output_lines)))
                        return

                except ImportError:
                    output_lines.append("未安装akshare库")
                    output_lines.append("请安装: pip install akshare")
                    root.after(0, lambda: display_output("\n".join(output_lines)))
                    return
                except Exception as e:
                    output_lines.append(f"获取数据失败: {str(e)}")
                    root.after(0, lambda: display_output("\n".join(output_lines)))
                    return

            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep='first')]

            # 生成输出
            output_lines.append(f"从 {db_path} 加载数据...")
            output_lines.append(f"成功加载 {len(df)} 条数据")
            output_lines.append(f"数据范围: {df.index.min()} 至 {df.index.max()}")

            today_signal, today_reason = get_today_signal(df)

            output_lines.append(f"\n今日信号 [V2.0]:")
            output_lines.append(f"{datetime.now().strftime('%Y-%m-%d')} | {today_signal} | {today_reason}")

            root.after(0, lambda: display_output("\n".join(output_lines)))

        except Exception as e:
            import traceback
            error_msg = f"执行出错: {str(e)}\n{traceback.format_exc()}"
            root.after(0, lambda: display_output(error_msg))

    threading.Thread(target=_run, daemon=True).start()


def run_backtest():
    """运行回测"""
    def _run():
        try:
            output_text = capture_output_to_string(eval_strategy_func)
            root.after(0, lambda: display_output(output_text))
        except Exception as e:
            import traceback
            error_msg = f"执行出错: {str(e)}\n{traceback.format_exc()}"
            root.after(0, lambda: display_output(error_msg))

    threading.Thread(target=_run, daemon=True).start()


def toggle_startup():
    """切换开机启动状态"""
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "QuantStrategyTool_V2"

        # 检查是否已设置开机启动
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, app_name)
            winreg.CloseKey(key)
            is_set = True
        except FileNotFoundError:
            is_set = False

        if is_set:
            # 取消开机启动
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(key, app_name)
            winreg.CloseKey(key)
            messagebox.showinfo("提示", "已取消开机启动")
            btn_startup.config(text="设置开机启动", bg="#4CAF50")
        else:
            # 设置开机启动
            script_path = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{script_path}"')
            winreg.CloseKey(key)
            messagebox.showinfo("提示", "已设置开机启动")
            btn_startup.config(text="取消开机启动", bg="#f44336")
    except Exception as e:
        messagebox.showerror("错误", f"设置开机启动失败: {str(e)}")


def display_output(text):
    """在文本框中显示输出"""
    output_text.delete(1.0, tk.END)
    output_text.insert(tk.END, text)


def check_startup_status():
    """检查开机启动状态并更新按钮"""
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "QuantStrategyTool_V2"

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, app_name)
        winreg.CloseKey(key)

        # 已设置开机启动
        btn_startup.config(text="取消开机启动", bg="#f44336")
    except FileNotFoundError:
        # 未设置开机启动
        btn_startup.config(text="设置开机启动", bg="#4CAF50")
    except Exception:
        pass


def update_data_incremental():
    """增量更新数据"""
    def _run():
        try:
            output_lines = []
            output_lines.append("开始增量更新数据...")
            root.after(0, lambda: display_output("\n".join(output_lines)))

            update_stock_data()

            output_lines.append("增量更新完成")
            root.after(0, lambda: display_output("\n".join(output_lines)))
        except Exception as e:
            import traceback
            error_msg = f"执行出错: {str(e)}\n{traceback.format_exc()}"
            root.after(0, lambda: display_output(error_msg))

    threading.Thread(target=_run, daemon=True).start()


def refresh_all_data():
    """完全刷新数据"""
    def _run():
        try:
            output_lines = []
            output_lines.append("开始完全刷新数据...")
            root.after(0, lambda: display_output("\n".join(output_lines)))

            full_refresh_data()

            output_lines.append("完全刷新完成")
            root.after(0, lambda: display_output("\n".join(output_lines)))
        except Exception as e:
            import traceback
            error_msg = f"执行出错: {str(e)}\n{traceback.format_exc()}"
            root.after(0, lambda: display_output(error_msg))

    threading.Thread(target=_run, daemon=True).start()


# 创建主窗口
root = tk.Tk()
root.title("量化策略分析工具 V2.0")
root.geometry("900x700")

# 创建按钮框架
button_frame = tk.Frame(root, pady=10)
button_frame.pack(fill=tk.X)

# 更新数据按钮
btn_update = tk.Button(
    button_frame,
    text="更新数据",
    command=update_data_incremental,
    font=("微软雅黑", 12),
    bg="#FF9800",
    fg="white",
    padx=20,
    pady=10
)
btn_update.pack(side=tk.LEFT, padx=10)

# 完全刷新按钮
btn_refresh = tk.Button(
    button_frame,
    text="完全刷新",
    command=refresh_all_data,
    font=("微软雅黑", 12),
    bg="#FF5722",
    fg="white",
    padx=20,
    pady=10
)
btn_refresh.pack(side=tk.LEFT, padx=10)

# 测算今日操作建议按钮
btn_today = tk.Button(
    button_frame,
    text="测算今日操作建议",
    command=run_strategy_today,
    font=("微软雅黑", 12),
    bg="#4CAF50",
    fg="white",
    padx=20,
    pady=10
)
btn_today.pack(side=tk.LEFT, padx=10)

# 回测按钮
btn_backtest = tk.Button(
    button_frame,
    text="回测 [V2.0]",
    command=run_backtest,
    font=("微软雅黑", 12),
    bg="#2196F3",
    fg="white",
    padx=20,
    pady=10
)
btn_backtest.pack(side=tk.LEFT, padx=10)

# 开机启动按钮
btn_startup = tk.Button(
    button_frame,
    text="设置开机启动",
    command=toggle_startup,
    font=("微软雅黑", 12),
    bg="#4CAF50",
    fg="white",
    padx=20,
    pady=10
)
btn_startup.pack(side=tk.LEFT, padx=10)

# 创建输出文本框
output_frame = tk.Frame(root, padx=10, pady=10)
output_frame.pack(fill=tk.BOTH, expand=True)

output_label = tk.Label(output_frame, text="输出结果:", font=("微软雅黑", 10, "bold"))
output_label.pack(anchor=tk.W)

output_text = scrolledtext.ScrolledText(
    output_frame,
    wrap=tk.WORD,
    font=("Consolas", 10),
    bg="#f5f5f5",
    fg="#333"
)
output_text.pack(fill=tk.BOTH, expand=True, pady=5)

# 程序启动时自动执行今日操作建议
root.after(100, run_strategy_today)

# 检查开机启动状态
root.after(200, check_startup_status)

# 运行主循环
if __name__ == "__main__":
    root.mainloop()
