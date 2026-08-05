"""
V4.0 主程序入口 —— CLI 版本

用法：
  python main.py           # 交互式菜单
  python main.py --eval    # 直接运行回测评估
  python main.py --signal  # 仅输出今日信号
  python main.py --update  # 仅更新数据
"""
import sys
import os
import sqlite3
import pandas as pd
from datetime import datetime


def find_data_path():
    """查找数据库文件（优先级：V4.0 > V2.0 > V1.0）"""
    v4_dir = os.path.dirname(os.path.abspath(__file__))
    v4_db = os.path.join(v4_dir, 'stock_data.db')
    if os.path.exists(v4_db):
        return v4_db

    parent_dir = os.path.dirname(v4_dir)
    v2_db = os.path.join(parent_dir, 'V2.0', 'stock_data.db')
    if os.path.exists(v2_db):
        return v2_db

    v1_db = os.path.join(parent_dir, 'V1.0', 'stock_data.db')
    if os.path.exists(v1_db):
        return v1_db

    return v4_db


def main():
    print("=" * 60)
    print("  A500 ETF V4.0 行为识别 + 概率评分系统")
    print("=" * 60)

    # 简单的命令行参数解析
    if '--eval' in sys.argv or '-e' in sys.argv:
        from evaluation import evaluate_strategy
        evaluate_strategy()
        return

    if '--signal' in sys.argv or '-s' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import get_today_signal

        db_path = find_data_path()
        df = load_data_from_db(db_path)
        if df is not None:
            signal, detail = get_today_signal(df)
            print(f"\n今日信号: {signal}")
            print(detail)
        else:
            print("无法加载数据")
        return

    if '--update' in sys.argv or '-u' in sys.argv:
        from data_updater import update_stock_data
        update_stock_data()
        return

    # 交互模式
    print("\n  请选择操作:")
    print("  1. 回测评估（含行为统计+敏感性分析）")
    print("  2. 今日信号")
    print("  3. 更新数据")
    print("  4. 完全刷新数据")
    print("  0. 退出")

    choice = input("\n  请输入选项: ").strip()

    if choice == '1':
        from evaluation import evaluate_strategy
        evaluate_strategy()
    elif choice == '2':
        from data_updater import load_data_from_db
        from strategy import get_today_signal

        db_path = find_data_path()
        df = load_data_from_db(db_path)
        if df is not None:
            signal, detail = get_today_signal(df)
            print(f"\n今日信号: {signal}")
            print(detail)
        else:
            print("无法加载数据，请先更新数据")
    elif choice == '3':
        from data_updater import update_stock_data
        update_stock_data()
    elif choice == '4':
        from data_updater import full_refresh_data
        full_refresh_data()
    elif choice == '0':
        print("再见")
    else:
        print("无效选项")


if __name__ == "__main__":
    main()
