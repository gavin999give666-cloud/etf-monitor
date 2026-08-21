"""
V7.0 主程序入口 —— 多标的 CLI
=============================
基于 V6.2.3 引擎的 V7.0 合并版。所有命令通过 --profile {code} 指定标的。

用法：
  python main.py --profile 589800 --signal        # 输出今日信号（含Evidence分解）
  python main.py --profile 589800 --eval          # 运行回测评估
  python main.py --profile 589800 --update        # 更新数据
  python main.py --profile 589800 --heavy         # 全量高算力优化（断点续算）
  python main.py --profile 589800 --heavy-view    # 查看上次全量优化结果
  python main.py --profile 589800 --intraday      # 盘中估算模式
  python main.py --profile 589800 --backfill      # T+1 回填
  python main.py --profiles                       # 列出可用标的
  python main.py --signal                         # 省略 --profile：用默认标的(563360)

设计要点（见 V7.0_合并设计方案.md §3.1）：
- 主进程先 activate_profile(code)（setattr 到 config 全局），再惰性导入引擎模块，
  保证 from config import * / import config 均取到"已激活"的参数值。
- 重计算（优化/回测）在子进程跑，子进程统一 --profile {code} 先激活自己。
"""
import sys
import os

# 引擎模块目录（quant）入 path：flat 命名空间 import config / from strategy import ...
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'quant'))

# Windows 控制台编码保护：GBK 等编码无法输出 ⚠ 等字符时不崩溃（降级替换）
try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass


def _parse_profile():
    """解析 --profile {code}，返回 code 或 None"""
    if '--profile' in sys.argv:
        idx = sys.argv.index('--profile')
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
        print("⚠ --profile 后缺少标代码", file=sys.stderr)
        sys.exit(2)
    return None


def main():
    import config

    # 1) 激活标的（未指定 --profile 时用默认 config.STOCK_CODE）
    profile_code = _parse_profile()
    activated_name = None
    if profile_code:
        config.activate_profile(profile_code)
    else:
        profile_code = config.STOCK_CODE
    activated_name = config.ETF_NAME

    if '--profiles' in sys.argv:
        print("可用标的数据档案:")
        for code in config.available_profiles():
            try:
                p = config.load_profile(code)
                flag = '已优化' if p.get('optimized') else '未优化'
                print(f"  {code:<8} {p.get('name', ''):<20} [{flag}]")
            except Exception as e:
                print(f"  {code:<8} <读取失败: {e}>")
        return

    # 2) 运行时横幅
    print("=" * 60)
    print(f"  {activated_name} ({profile_code}) V7.0 Evidence Engine")
    print("=" * 60)

    from data_updater import get_runtime_context
    ctx = get_runtime_context()
    _PHASE_CN = {
        'non_trading_day': '周末/节假日', 'pre_market': '盘前时段',
        'intraday': '盘中时段', 'closing': '收盘竞价时段', 'post_market': '盘后时段',
    }
    day_cn = '交易日' if ctx['is_trading_day'] else '非交易日'
    print(f"运行环境: {ctx['now'].strftime('%Y-%m-%d %H:%M')} | {day_cn} | {_PHASE_CN[ctx['phase']]}")

    def _warn_data_intraday():
        if ctx['phase'] in ('intraday', 'closing'):
            print("⚠ 警告：当前为盘中/收盘竞价时段，数据库中无今日完整数据。"
                  "如需当日操作建议请使用 --intraday。")

    if '--eval' in sys.argv or '-e' in sys.argv:
        _warn_data_intraday()
        from evaluation import evaluate_strategy
        evaluate_strategy(use_ml=True)
        return

    if '--signal' in sys.argv or '-s' in sys.argv:
        _warn_data_intraday()
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6
        df = load_data_from_db()
        if df is not None:
            signal, detail = get_today_signal_v6(df)
            print(f"\n今日信号: {signal}")
            print(detail)
        else:
            print("无法加载数据，请先 --update")
        return

    if '--update' in sys.argv or '-u' in sys.argv:
        from data_updater import update_stock_data
        update_stock_data()
        return

    if '--heavy' in sys.argv or '--heavy-fresh' in sys.argv:
        resume = '--heavy-fresh' not in sys.argv
        from param_optimizer import run_heavy_optimization
        run_heavy_optimization(resume=resume)
        return

    if '--heavy-view' in sys.argv:
        from param_optimizer import view_saved_results
        view_saved_results()
        return

    if '--intraday' in sys.argv:
        from data_updater import update_intraday, load_data_from_db
        success = update_intraday()
        if success:
            df = load_data_from_db()
            if df is not None:
                from strategy import get_today_signal_v6
                signal, detail = get_today_signal_v6(df)
                print(f"\n今日信号: {signal}")
                print(detail)
                print("\n⚠ 注意：成交量为盘中估算值，仅供参考")
        return

    if '--backfill' in sys.argv:
        from data_updater import backfill_estimated_data
        backfill_estimated_data()
        return

    # 3) 参数检查：无有效命令时提示
    print()
    print("使用方法: python main.py --profile {code} <命令>")
    print("命令: --signal | --eval | --update | --heavy | --heavy-view | --intraday | --backfill | --profiles")
    print("可用标的: " + ", ".join(config.available_profiles()))


if __name__ == "__main__":
    main()