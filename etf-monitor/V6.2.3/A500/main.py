"""
V6.2.1 主程序入口 —— CLI 版本（稳定化版本）
==========================================

用法：
  python main.py                    # 交互式菜单
  python main.py --eval             # 运行回测评估（V6.2.1）
  python main.py --signal           # 输出今日信号（含交易建议/仓位指引/评分解读）
  python main.py --signal --pos 0.5 --capital 100000   # 指定当前持仓与总资金换算买卖数量
  python main.py --update           # 更新数据
  python main.py --replay           # 打印最近回放记录
  python main.py --behavior-memory  # 行为记忆库统计（V6.2.1增强版）
  python main.py --evidence-debug   # Evidence Engine调试输出
  python main.py --replay-summary   # Replay Learning Top10/Worst10
  python main.py --grid-search      # 运行参数网格搜索（断点续算）
  python main.py --grid-fresh       # 强制全新网格搜索（忽略断点）
  python main.py --grid-status      # 查看断点续算状态
  python main.py --heavy            # 全量高算力优化（50+参数，断点续算）
  python main.py --heavy-fresh      # 全量优化（忽略断点，全新开始）
  python main.py --heavy-view       # 查看上次全量优化结果
  python main.py --intraday         # 盘中估算模式（14:40估算当日数据并出信号）
  python main.py --backfill         # T+1回填（用真实数据回填估算记录）
"""
import sys
import os

# 确保 V6.2.1 目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台编码保护：GBK 等编码无法输出 ⚠ 等字符时不崩溃（降级替换）
try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass


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


# 时段中文名称映射
_PHASE_CN = {
    'non_trading_day': '周末/节假日',
    'pre_market': '盘前时段',
    'intraday': '盘中时段',
    'closing': '收盘竞价时段',
    'post_market': '盘后时段',
}


def parse_signal_options():
    """解析信号相关可选参数：--pos <当前持仓0-1>，--capital <总资金元>（用于换算买卖数量）"""
    pos, capital = None, None
    try:
        if '--pos' in sys.argv:
            pos = float(sys.argv[sys.argv.index('--pos') + 1])
    except (ValueError, IndexError):
        pass
    try:
        if '--capital' in sys.argv:
            capital = float(sys.argv[sys.argv.index('--capital') + 1])
    except (ValueError, IndexError):
        pass
    return pos, capital


def print_runtime_banner(ctx):
    """打印运行环境横幅"""
    day_cn = '交易日' if ctx['is_trading_day'] else '非交易日'
    line = "═" * 58
    print(line)
    print(f"运行环境: {ctx['now'].strftime('%Y-%m-%d %H:%M')} | {day_cn} | {_PHASE_CN[ctx['phase']]}")
    print(line)


def check_intraday_allowed(ctx):
    """检查当前时间是否允许盘中估算，不允许时打印清晰的拒绝原因"""
    phase = ctx['phase']
    if phase == 'intraday':
        return True
    if phase == 'non_trading_day':
        print("⚠ 当前为非交易日，无法进行盘中估算")
    elif phase == 'pre_market':
        print(f"⚠ 当前为盘前（{ctx['now'].strftime('%H:%M')}，9:25 之前），盘中估算窗口尚未开始")
    elif phase == 'closing':
        print("⚠ 当前为收盘竞价时段（14:55-15:00），盘中估算窗口已结束，今日数据不完整，请谨慎")
    else:  # post_market
        print("⚠ 已收盘（15:00 之后），无需盘中估算，可直接使用 --update 获取今日完整数据")
    return False


def warn_intraday_data(ctx):
    """盘中/收盘竞价时段运行 --signal / --eval 时的数据完整性警告"""
    if ctx['phase'] == 'intraday':
        print("⚠ 警告：当前为盘中时段，数据库中无今日完整数据。"
              "如需当日操作建议，请使用 --intraday；当前输出基于截至上一交易日的数据")
    elif ctx['phase'] == 'closing':
        print("⚠ 警告：当前为收盘竞价时段，今日数据不完整。"
              "如需当日操作建议，请使用 --intraday；当前输出基于截至上一交易日的数据")


def main():
    print("=" * 60)
    print("  A500 ETF V6.2.1 Evidence Engine 稳定化版本")
    print("=" * 60)

    # 运行上下文检测（运行时间感知）
    from data_updater import get_runtime_context
    ctx = get_runtime_context()
    print_runtime_banner(ctx)
    if ctx['phase'] == 'non_trading_day':
        print("ℹ 今日为非交易日，使用最近交易日数据")
    elif ctx['phase'] == 'post_market':
        print("ℹ 已收盘，可直接 --update 获取今日完整数据")

    # 命令行参数解析
    if '--eval' in sys.argv or '-e' in sys.argv:
        warn_intraday_data(ctx)
        from evaluation import evaluate_strategy
        pos, capital = parse_signal_options()
        evaluate_strategy(use_ml=True, reference_position=pos)
        return

    if '--signal' in sys.argv or '-s' in sys.argv:
        warn_intraday_data(ctx)
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6

        db_path = find_data_path()
        df = load_data_from_db(db_path)
        if df is not None:
            pos, capital = parse_signal_options()
            signal, detail = get_today_signal_v6(df, current_position=pos, capital=capital)
            print(f"\nV6.2.1 今日信号: {signal}")
            print(detail)
        else:
            print("无法加载数据")
        return

    if '--replay' in sys.argv or '-r' in sys.argv:
        from data_updater import load_data_from_db
        from evaluation import evaluate_strategy
        evaluate_strategy(use_ml=True)
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
        if ctx['phase'] == 'intraday':
            print("ℹ 提示：盘中更新只会补充历史数据，当日数据请使用 --intraday 估算写入")
        from data_updater import update_stock_data
        update_stock_data()
        return

    # V6.1 新增命令行选项
    if '--behavior-memory' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import V6Strategy
        df = load_data_from_db()
        if df is not None:
            strategy = V6Strategy(use_ml=True)
            strategy.run(df)
            bm = strategy.get_behavior_memory()
            if bm:
                bm.print_stats()
            else:
                print("行为记忆库未启用")
        return

    if '--replay-summary' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import V6Strategy
        df = load_data_from_db()
        if df is not None:
            strategy = V6Strategy(use_ml=True)
            strategy.run(df)
            bm = strategy.get_behavior_memory()
            if bm:
                bm.print_replay_summary()
            else:
                print("行为记忆库未启用")
        return

    if '--evidence-debug' in sys.argv:
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6
        df = load_data_from_db()
        if df is not None:
            pos, capital = parse_signal_options()
            signal, detail = get_today_signal_v6(df, current_position=pos, capital=capital)
            print(f"\nV6.2.1 今日信号: {signal}")
            print(detail)
        return

    if '--heavy' in sys.argv:
        from param_optimizer import run_heavy_optimization
        run_heavy_optimization(resume=True)
        return

    if '--heavy-fresh' in sys.argv:
        from param_optimizer import run_heavy_optimization
        run_heavy_optimization(resume=False)
        return

    if '--heavy-view' in sys.argv:
        from param_optimizer import view_saved_results
        view_saved_results()
        return

    if '--intraday' in sys.argv:
        print("=" * 60)
        print("盘中估算模式")
        print("=" * 60)
        if not check_intraday_allowed(ctx):
            return
        from data_updater import update_intraday, load_data_from_db
        success = update_intraday()
        if success:
            df = load_data_from_db()
            if df is not None:
                from strategy import get_today_signal_v6
                pos, capital = parse_signal_options()
                signal, detail = get_today_signal_v6(df, current_position=pos, capital=capital)
                print(f"\nV6.2.1 今日信号: {signal}")
                print(detail)
                print("\n⚠ 注意：成交量为盘中估算值，仅供参考")
            else:
                print("无法加载数据")
        return

    if '--backfill' in sys.argv:
        from data_updater import backfill_estimated_data
        backfill_estimated_data()
        return

    # 交互模式
    print(f"\n  当前运行环境: {ctx['description']}")
    print("\n  请选择操作:")
    print("  1. V6.2.1 回测评估（Evidence Engine 稳定化版本）")
    print("  2. V6.2.1 今日信号（含Evidence分解）")
    print("  3. 更新数据")
    print("  4. 行为记忆库统计（Replay Learning）")
    print("  5. Replay Summary (Top10/Worst10)")
    print("  6. 参数网格搜索（断点续算）")
    print("  7. 参数网格搜索（忽略断点，全新开始）")
    print("  8. 查看断点续算状态")
    print("  9. 全量高算力优化（50+参数，断点续算）")
    print(" 10. 全量优化（忽略断点，全新开始）")
    print("  11. 查看上次全量优化结果")
    print("  12. 盘中估算（估算当日数据并出信号）")
    print("  13. T+1回填（用真实数据回填估算记录）")
    print("  0. 退出")

    choice = input("\n  请输入选项: ").strip()

    if choice == '1':
        warn_intraday_data(ctx)
        from evaluation import evaluate_strategy
        evaluate_strategy(use_ml=True)
    elif choice == '2':
        warn_intraday_data(ctx)
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6

        db_path = find_data_path()
        df = load_data_from_db(db_path)
        if df is not None:
            print("\n  （可选）输入你的实际持仓与资金，将用于计算具体买卖数量，回车使用默认：")
            pos_in = input("  当前持仓比例（%，回车按策略参考持仓）: ").strip()
            cap_in = input("  总资金（元，回车默认 100000）: ").strip()
            pos = float(pos_in) / 100.0 if pos_in else None
            capital = float(cap_in) if cap_in else None
            signal, detail = get_today_signal_v6(df, current_position=pos, capital=capital)
            print(f"\nV6.2.1 今日信号: {signal}")
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
            strategy = V6Strategy(use_ml=True)
            strategy.run(df)
            bm = strategy.get_behavior_memory()
            if bm:
                bm.print_stats()
            else:
                print("行为记忆库未启用（需要至少一笔历史交易）")
        else:
            print("无法加载数据")
    elif choice == '5':
        from data_updater import load_data_from_db
        from strategy import V6Strategy
        df = load_data_from_db()
        if df is not None:
            strategy = V6Strategy(use_ml=True)
            strategy.run(df)
            bm = strategy.get_behavior_memory()
            if bm:
                bm.print_replay_summary()
            else:
                print("行为记忆库未启用")
        else:
            print("无法加载数据")
    elif choice == '6':
        from param_search import run_grid_search_full
        run_grid_search_full(n_jobs=-1, resume=True)
    elif choice == '7':
        from param_search import run_grid_search_full
        run_grid_search_full(n_jobs=-1, resume=False)
    elif choice == '8':
        from param_search import GridSearch
        from data_updater import load_data_from_db
        df = load_data_from_db()
        if df is not None:
            GridSearch(df).checkpoint_status()
    elif choice == '9':
        from param_optimizer import run_heavy_optimization
        run_heavy_optimization(resume=True)
    elif choice == '10':
        from param_optimizer import run_heavy_optimization
        run_heavy_optimization(resume=False)
    elif choice == '11':
        from param_optimizer import view_saved_results
        view_saved_results()
    elif choice == '12':
        print("=" * 60)
        print("盘中估算模式")
        print("=" * 60)
        if not check_intraday_allowed(ctx):
            return
        from data_updater import update_intraday, load_data_from_db
        success = update_intraday()
        if success:
            df = load_data_from_db()
            if df is not None:
                from strategy import get_today_signal_v6
                pos, capital = parse_signal_options()
                signal, detail = get_today_signal_v6(df, current_position=pos, capital=capital)
                print(f"\nV6.2.1 今日信号: {signal}")
                print(detail)
                print("\n⚠ 注意：成交量为盘中估算值，仅供参考")
            else:
                print("无法加载数据")
    elif choice == '13':
        from data_updater import backfill_estimated_data
        backfill_estimated_data()
    elif choice == '0':
        print("再见")
    else:
        print("无效选项")


if __name__ == "__main__":
    main()
