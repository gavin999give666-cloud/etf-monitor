"""
V6.0 主程序入口 —— CLI 版本
============================

用法：
  python main.py                    # 交互式菜单
  python main.py --eval             # 运行回测评估
  python main.py --signal           # 输出今日信号
  python main.py --update           # 更新数据
  python main.py --replay           # 打印最近回放记录
  python main.py --grid-search      # 运行参数网格搜索（断点续算）
  python main.py --grid-fresh       # 强制全新网格搜索（忽略断点）
  python main.py --grid-status      # 查看断点续算状态
"""
import sys
import os

# 确保 V6.0 目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def find_data_path():
    """查找数据库文件"""
    v6_dir = os.path.dirname(os.path.abspath(__file__))
    v6_db = os.path.join(v6_dir, 'stock_data.db')
    if os.path.exists(v6_db):
        return v6_db

    parent_dir = os.path.dirname(v6_dir)
    for ver in ['V5.0', 'V4.0', 'V2.0', 'V1.0']:
        db = os.path.join(parent_dir, ver, 'stock_data.db')
        if os.path.exists(db):
            return db

    return v6_db


def main():
    print("=" * 60)
    print("  A500 ETF V6.0 Evidence Engine + Replay Learning")
    print("=" * 60)

    # 命令行参数解析
    if '--eval' in sys.argv or '-e' in sys.argv:
        from evaluation import evaluate_strategy
        evaluate_strategy()
        return

    if '--signal' in sys.argv or '-s' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6

        db_path = find_data_path()
        df = load_data_from_db(db_path)
        if df is not None:
            signal, detail = get_today_signal_v6(df)
            print(f"\nV6.0 今日信号: {signal}")
            print(detail)
        else:
            print("无法加载数据")
        return

    if '--replay' in sys.argv or '-r' in sys.argv:
        from data_updater import load_data_from_db
        from evaluation import evaluate_strategy
        evaluate_strategy()
        return

    if '--grid-search' in sys.argv or '-g' in sys.argv:
        from param_search import run_grid_search_full
        run_grid_search_full(n_jobs=-1, resume=True)
        return

    if '--grid-fresh' in sys.argv:
        from param_search import run_grid_search_full
        run_grid_search_full(n_jobs=-1, resume=False)
        return

    if '--grid-status' in sys.argv:
        from param_search import GridSearch
        from data_updater import load_data_from_db
        df = load_data_from_db()
        if df is not None:
            GridSearch(df).checkpoint_status()
        return

    if '--update' in sys.argv or '-u' in sys.argv:
        from data_updater import update_stock_data
        update_stock_data()
        return

    # V6新增命令行选项
    if '--behavior-memory' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import V6Strategy
        df = load_data_from_db()
        if df is not None:
            strategy = V6Strategy()
            strategy.run(df)
            bm = strategy.get_behavior_memory()
            if bm:
                bm.print_stats()
            else:
                print("行为记忆库未启用")
        return

    if '--evidence-debug' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6
        df = load_data_from_db()
        if df is not None:
            signal, detail = get_today_signal_v6(df)
            print(f"\nV6.0 今日信号: {signal}")
            print(detail)
        return

    # 交互模式
    print("\n  请选择操作:")
    print("  1. V6.0 回测评估（Evidence Engine + Replay Learning）")
    print("  2. V6.0 今日信号（含Evidence分解）")
    print("  3. 更新数据")
    print("  4. 行为记忆库统计（Behavior Memory）")
    print("  5. 参数网格搜索（断点续算）")
    print("  6. 参数网格搜索（忽略断点，全新开始）")
    print("  7. 查看断点续算状态")
    print("  0. 退出")

    choice = input("\n  请输入选项: ").strip()

    if choice == '1':
        from evaluation import evaluate_strategy
        evaluate_strategy()
    elif choice == '2':
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6

        db_path = find_data_path()
        df = load_data_from_db(db_path)
        if df is not None:
            signal, detail = get_today_signal_v6(df)
            print(f"\nV6.0 今日信号: {signal}")
            print(detail)
        else:
            print("无法加载数据，请先更新数据")
    elif choice == '3':
        from data_updater import update_stock_data
        update_stock_data()
    elif choice == '4':
        from data_updater import load_data_from_db
        from strategy import V6Strategy
        df = load_data_from_db()
        if df is not None:
            strategy = V6Strategy()
            strategy.run(df)
            bm = strategy.get_behavior_memory()
            if bm:
                bm.print_stats()
            else:
                print("行为记忆库未启用（需要至少一笔历史交易）")
        else:
            print("无法加载数据")
    elif choice == '5':
        from param_search import run_grid_search_full
        run_grid_search_full(n_jobs=-1, resume=True)
    elif choice == '6':
        from param_search import run_grid_search_full
        run_grid_search_full(n_jobs=-1, resume=False)
    elif choice == '7':
        from param_search import GridSearch
        from data_updater import load_data_from_db
        df = load_data_from_db()
        if df is not None:
            GridSearch(df).checkpoint_status()
    elif choice == '0':
        print("再见")
    else:
        print("无效选项")


if __name__ == "__main__":
    main()
