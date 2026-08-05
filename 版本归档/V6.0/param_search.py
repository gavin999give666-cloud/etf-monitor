"""
V5.0 参数网格搜索（Parameter Grid Search）
===========================================

自动参数搜索，输出：
- 收益率 / 夏普比率 / 最大回撤 / Calmar Ratio / 交易次数 / 胜率

寻找 Pareto 最优组合（不只追求收益率）

V5.2 升级：断点续算（Checkpoint / Resume）
- 自动保存进度到 .grid_search_checkpoint.json
- 中断后重新运行自动从断点继续
- 指纹校验：检测参数/数据变更，自动失效旧缓存
- resume=True 默认开启；resume=False 强制重新开始
- 不影响精度（网格搜索是穷举枚举，非迭代收敛）

V5.1 升级：多核并行优化
- ProcessPoolExecutor 实现真正的多进程并行
- 自动检测 CPU 核心数
- n_jobs=-1 使用全部核心
"""
import itertools
import json
import hashlib
import multiprocessing
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import numpy as np

import config
from data_updater import load_data_from_db

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# 断点文件路径
CHECKPOINT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '.grid_search_checkpoint.json')


# ============================================================
# Worker 函数（在子进程中运行）
# ============================================================

def _worker_evaluate(params_data):
    """
    独立 worker 函数 —— 在子进程中运行单次策略+回测

    每个子进程有独立的 config 模块副本，不会相互干扰。
    """
    params, df_json, start_date = params_data

    try:
        for param_name, value in params.items():
            setattr(config, param_name, value)

        df = pd.DataFrame(df_json)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

        from strategy import V6Strategy
        from backtest import V6Backtest

        strategy = V6Strategy()
        signals = strategy.run(df)
        bt = V6Backtest(df, start_date=start_date)
        bt_results = bt.run(signals)

        if not bt_results:
            return params.get('_combo_id', 0), None

        result = {
            **params,
            'strategy_return': bt_results.get('strategy_return', 0),
            'benchmark_return': bt_results.get('benchmark_return', 0),
            'excess_return': bt_results.get('excess_return', 0),
            'sharpe_ratio': bt_results.get('sharpe_ratio', 0),
            'max_drawdown': bt_results.get('max_drawdown', 0),
            'calmar_ratio': bt_results.get('calmar_ratio', 0),
            'annualized_return': bt_results.get('annualized_return', 0),
            'win_rate': bt_results.get('win_rate', 0),
            'total_trades': bt_results.get('total_trades', 0),
            'profit_factor': bt_results.get('profit_factor', 0),
        }
        return params.get('_combo_id', 0), result

    except Exception:
        return params.get('_combo_id', 0), None


def _get_cpu_count():
    """获取可用 CPU 核心数"""
    cpu_count = os.cpu_count() or 4
    return max(1, min(cpu_count, 12))


# ============================================================
# GridSearch 类（断点续算版）
# ============================================================

class GridSearch:
    """
    参数网格搜索器（多核并行 + 断点续算）

    使用方式：
        # 首次运行（自动开启断点保存）
        searcher = GridSearch(df)
        results = searcher.run(n_jobs=-1)

        # 中断后重新运行，自动续算
        results = searcher.run(n_jobs=-1)         # 默认 resume=True

        # 强制重新开始
        results = searcher.run(n_jobs=-1, resume=False)

        # 查看续算状态
        searcher.checkpoint_status()
    """

    def __init__(self, df, start_date='2025-09-01', checkpoint_path=None):
        self.df = df
        self.start_date = start_date
        self.results = []
        self._original_config = {}
        self._df_json = None
        self.checkpoint_path = checkpoint_path or CHECKPOINT_FILE
        self._current_grid = None         # 当前使用的 param_grid，用于指纹

    # ================================================================
    # 配置管理
    # ================================================================

    def _save_config(self):
        for param_name in config.GRID_SEARCH_PARAMS:
            self._original_config[param_name] = getattr(config, param_name, None)

    def _reset_config(self):
        for param_name, value in self._original_config.items():
            if value is not None:
                setattr(config, param_name, value)

    # ================================================================
    # DataFrame 序列化
    # ================================================================

    def _prepare_df_json(self):
        """准备 DataFrame 的 JSON 序列化版本（含预计算指标，一次算好全复用）"""
        if self._df_json is not None:
            return self._df_json

        # V5.2 优化：主进程预计算一次指标，所有 worker 直接复用
        # 网格搜索参数均为行为检测阈值，不影响指标计算 → 零精度影响
        from indicators import calculate_indicators
        df_with_indicators = calculate_indicators(self.df.copy())

        df_copy = df_with_indicators.reset_index()
        if 'date' in df_copy.columns:
            df_copy['date'] = df_copy['date'].astype(str)

        # NaN 替换为 None 以确保 JSON 序列化（worker 端会还原为 NaN）
        df_copy = df_copy.where(df_copy.notna(), None)

        self._df_json = df_copy.to_dict(orient='list')
        return self._df_json

    # ================================================================
    # 断点续算核心
    # ================================================================

    def _compute_fingerprint(self, param_grid):
        """
        计算当前搜索上下文的指纹

        指纹 = hash(param_grid 的 key + value 的 str + 数据行数 + 首尾日期 + start_date)
        三个要素任一变化 → 指纹不同 → 旧断点失效
        """
        hasher = hashlib.sha256()

        # 1. 参数网格
        grid_str = json.dumps(
            {k: [str(v) for v in vals] for k, vals in param_grid.items()},
            sort_keys=True
        )
        hasher.update(grid_str.encode('utf-8'))

        # 2. 数据形状
        df_info = f"rows={len(self.df)}"
        if len(self.df) > 0:
            first_date = str(self.df.index[0])
            last_date = str(self.df.index[-1])
            df_info += f"_first={first_date}_last={last_date}"
        hasher.update(df_info.encode('utf-8'))

        # 3. 回测起始日期
        hasher.update(str(self.start_date).encode('utf-8'))

        return hasher.hexdigest()[:16]

    def _load_checkpoint(self, fingerprint):
        """
        加载断点文件，验证指纹

        Returns:
            (results_list, completed_ids_set) 如果有效
            (None, None) 如果无效或不存在
        """
        if not os.path.exists(self.checkpoint_path):
            return None, None

        try:
            with open(self.checkpoint_path, 'r', encoding='utf-8') as f:
                ckpt = json.load(f)

            # 指纹验证
            stored_fingerprint = ckpt.get('fingerprint', '')
            if stored_fingerprint != fingerprint:
                return None, None  # 指纹不匹配，旧断点失效

            results = ckpt.get('results', [])
            completed_ids = set(ckpt.get('completed_ids', []))
            return results, completed_ids

        except (json.JSONDecodeError, KeyError, IOError):
            return None, None

    @staticmethod
    def _json_serializable(obj):
        """递归转换 numpy 类型为 Python 原生类型，确保 JSON 可序列化"""
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, dict):
            return {k: GridSearch._json_serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [GridSearch._json_serializable(v) for v in obj]
        return obj

    def _save_checkpoint(self, results, completed_ids, fingerprint, param_grid):
        """
        保存断点到磁盘（原子写入：先写临时文件再 rename）
        """
        # 转换 numpy 类型 → Python 原生类型
        clean_results = self._json_serializable(results)
        clean_ids = [int(i) for i in completed_ids]

        ckpt = {
            'version': '5.2',
            'fingerprint': fingerprint,
            'param_grid_keys': list(param_grid.keys()),
            'param_grid_values': {k: [str(v) for v in vals] for k, vals in param_grid.items()},
            'start_date': str(self.start_date),
            'total_combos': sum(1 for _ in itertools.product(*param_grid.values())),
            'results': clean_results,
            'completed_ids': sorted(clean_ids),
            'completed_count': len(clean_ids),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        # 原子写入：先写 .tmp 再 rename，避免写入中途崩溃产生损坏文件
        tmp_path = self.checkpoint_path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(ckpt, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.checkpoint_path)
        except (IOError, TypeError, ValueError) as e:
            # 写入失败不中止搜索，下次周期性保存会重试
            if hasattr(self, '_checkpoint_errors'):
                self._checkpoint_errors += 1
            else:
                self._checkpoint_errors = 1

    def _cleanup_checkpoint(self):
        """搜索完成后清理断点文件"""
        try:
            if os.path.exists(self.checkpoint_path):
                os.remove(self.checkpoint_path)
        except IOError:
            pass

    def checkpoint_status(self, param_grid=None):
        """
        查看当前断点状态（不执行搜索）

        Returns:
            dict 或 None
        """
        if param_grid is None:
            param_grid = config.GRID_SEARCH_PARAMS

        fingerprint = self._compute_fingerprint(param_grid)
        results, completed_ids = self._load_checkpoint(fingerprint)

        if results is None:
            print("无有效断点，需要全新运行")
            return None

        total = sum(1 for _ in itertools.product(*param_grid.values()))
        print(f"\n断点状态:")
        print(f"  已完成: {len(completed_ids)} / {total} ({len(completed_ids)/total*100:.1f}%)")
        print(f"  上次更新: {self._load_checkpoint_meta().get('last_updated', 'unknown')}")
        print(f"  断点文件: {self.checkpoint_path}")
        return {'results': results, 'completed_ids': completed_ids, 'total': total}

    def _load_checkpoint_meta(self):
        """仅加载断点元信息（不验证指纹）"""
        try:
            if os.path.exists(self.checkpoint_path):
                with open(self.checkpoint_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
        return {}

    # ================================================================
    # 主执行入口
    # ================================================================

    def run(self, param_grid=None, n_jobs=-1, resume=True, verbose=True,
            checkpoint_interval=10):
        """
        运行网格搜索（多核并行 + 断点续算）

        Args:
            param_grid: 自定义参数网格，None 则使用 config 默认
            n_jobs: 并行进程数（-1=全部核心, 1=单核, N=指定N核）
            resume: 是否启用断点续算（默认 True）
            verbose: 是否打印进度
            checkpoint_interval: 每完成 N 个组合保存一次断点

        Returns:
            results: list of dict
        """
        if param_grid is None:
            param_grid = config.GRID_SEARCH_PARAMS
        self._current_grid = param_grid

        self._save_config()

        # 初始化断点错误计数器
        self._checkpoint_errors = 0

        # ---- 生成所有参数组合 ----
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        total_combos = 1
        for vals in param_values:
            total_combos *= len(vals)

        all_combos = list(itertools.product(*param_values))
        combo_id_to_params = {}
        for idx, combo in enumerate(all_combos, 1):
            params = dict(zip(param_names, combo))
            params['_combo_id'] = idx
            combo_id_to_params[idx] = params

        # ---- 断点续算 ----
        fingerprint = self._compute_fingerprint(param_grid)
        existing_results = []
        completed_ids = set()
        resumed = False

        if resume:
            loaded_results, loaded_ids = self._load_checkpoint(fingerprint)
            if loaded_results is not None:
                existing_results = loaded_results
                completed_ids = loaded_ids
                resumed = True
            else:
                # 检查是否有旧断点（指纹不匹配）
                old_ckpt = self._load_checkpoint_meta()
                if old_ckpt:
                    print("⚠ 检测到旧断点，但参数网格/数据/起始日期已变更，旧断点失效")
                    print("  将重新开始。如需保留旧结果，请备份断点文件后手动处理。")
                    if verbose:
                        answer = input("  继续重新开始？[Y/n]: ").strip().lower()
                        if answer == 'n':
                            print("已取消")
                            self._reset_config()
                            return existing_results if existing_results else []

        # ---- 构建待执行任务列表 ----
        df_json = self._prepare_df_json()
        pending_tasks = []

        for combo_id in range(1, total_combos + 1):
            if combo_id in completed_ids:
                continue
            params = combo_id_to_params[combo_id]
            pending_tasks.append((params, df_json, self.start_date))

        pending_count = len(pending_tasks)

        # ---- 打印信息 ----
        if verbose:
            print(f"\n{'='*60}")
            print(f"V5.2 参数网格搜索（多核 + 断点续算）")
            print(f"{'='*60}")
            print(f"参数数量: {len(param_names)}")
            print(f"参数: {param_names}")
            print(f"总组合数: {total_combos}")

            if resumed:
                print(f"断点续算: 已完成 {len(completed_ids)}, 剩余 {pending_count} "
                      f"({pending_count/total_combos*100:.1f}% 待执行)")
            else:
                print(f"全新开始: {total_combos} 个组合待执行")

            if n_jobs == -1:
                actual_jobs = _get_cpu_count()
            elif n_jobs <= 0:
                actual_jobs = 1
            else:
                actual_jobs = min(n_jobs, max(1, pending_count))

            print(f"并行进程: {actual_jobs} (CPU: {os.cpu_count()}核)")
            print(f"{'='*60}\n")

        if pending_count == 0:
            if verbose:
                print("所有组合已完成，无需计算。")
            self._reset_config()
            self.results = existing_results
            return existing_results

        # ---- 执行 ----
        self.results = list(existing_results)  # 从已有结果开始

        if n_jobs == 1 or pending_count <= 1:
            new_results = self._run_sequential(
                pending_tasks, completed_ids, total_combos,
                fingerprint, param_grid, resume, verbose, checkpoint_interval
            )
        else:
            new_results = self._run_parallel(
                pending_tasks, completed_ids, total_combos, n_jobs,
                fingerprint, param_grid, resume, verbose, checkpoint_interval
            )

        # 合并结果
        new_ids = {r.get('combo_id', r.get('_combo_id', 0)) for r in new_results}
        for r in new_results:
            self.results.append(r)
        completed_ids.update(new_ids)

        # 最后保存一次
        self._save_checkpoint(self.results, completed_ids, fingerprint, param_grid)

        # 恢复原始配置
        self._reset_config()

        if verbose:
            print(f"\n搜索完成！共运行 {len(self.results)} 个有效组合")
            print(f"断点已保存: {self.checkpoint_path}")
            if self._checkpoint_errors > 0:
                print(f"⚠ 断点保存出现 {self._checkpoint_errors} 次错误，请检查磁盘空间")
            print()

        return self.results

    # ================================================================
    # 串行执行
    # ================================================================

    def _run_sequential(self, tasks, completed_ids, total_combos, fingerprint,
                         param_grid, resumed, verbose, checkpoint_interval):
        """单核串行执行（含断点保存）"""
        results = []
        base_count = len(completed_ids)

        iterator = enumerate(tasks, 1)
        desc = "Grid Search (resume)" if resumed else "Grid Search"
        if HAS_TQDM and verbose:
            iterator = tqdm(iterator, total=len(tasks), desc=desc, unit="combo")

        for idx, task in iterator:
            combo_id, result = _worker_evaluate(task)
            if result is not None:
                results.append(result)
                completed_ids.add(combo_id)

            # 定期保存断点
            current_total = base_count + idx
            if idx % checkpoint_interval == 0 and results:
                self._save_checkpoint(
                    list(self.results) + results,
                    completed_ids,
                    fingerprint,
                    param_grid
                )

            if verbose and not HAS_TQDM:
                if idx % max(1, len(tasks) // 10) == 0:
                    print(f"进度: {current_total}/{total_combos} "
                          f"({current_total/total_combos*100:.0f}%)")

        return results

    # ================================================================
    # 并行执行
    # ================================================================

    def _run_parallel(self, tasks, completed_ids, total_combos, n_jobs,
                       fingerprint, param_grid, resumed, verbose, checkpoint_interval):
        """多核并行执行（含断点保存。主进程单写，无竞态。）"""
        if n_jobs == -1:
            n_jobs = _get_cpu_count()
        n_jobs = min(n_jobs, len(tasks))

        results = []
        new_completed = 0
        base_count = len(completed_ids)

        ctx = multiprocessing.get_context('spawn')

        with ProcessPoolExecutor(max_workers=n_jobs, mp_context=ctx) as executor:
            future_to_task = {
                executor.submit(_worker_evaluate, task): task
                for task in tasks
            }

            desc = "Grid Search (resume)" if resumed else "Grid Search"
            if HAS_TQDM and verbose:
                pbar = tqdm(total=len(tasks), desc=desc, unit="combo")

            for future in as_completed(future_to_task):
                try:
                    combo_id, result = future.result()
                    if result is not None:
                        results.append(result)
                        completed_ids.add(combo_id)
                except Exception:
                    pass

                new_completed += 1

                # 定期保存断点（主进程写入，天然线程安全）
                if new_completed % checkpoint_interval == 0 and results:
                    self._save_checkpoint(
                        list(self.results) + results,
                        completed_ids,
                        fingerprint,
                        param_grid
                    )

                if HAS_TQDM and verbose:
                    pbar.update(1)
                elif verbose and new_completed % max(1, len(tasks) // 10) == 0:
                    current = base_count + new_completed
                    print(f"进度: {current}/{total_combos} "
                          f"({current/total_combos*100:.0f}%)")

            if HAS_TQDM and verbose:
                pbar.close()

        return results

    # ================================================================
    # Pareto 分析
    # ================================================================

    def find_pareto_frontier(self, results=None):
        """寻找 Pareto 最优面"""
        if results is None:
            results = self.results
        if not results:
            return []

        objectives = config.PARETO_OBJECTIVES

        vectors = []
        for r in results:
            vec = [
                r.get('strategy_return', 0) * objectives.get('strategy_return', 1.0),
                r.get('sharpe_ratio', 0) * objectives.get('sharpe_ratio', 1.0),
                -abs(r.get('max_drawdown', 0)) * objectives.get('max_drawdown', 1.0),
                r.get('calmar_ratio', 0) * objectives.get('calmar_ratio', 1.0),
                r.get('win_rate', 0) * objectives.get('win_rate', 0.5),
                -abs(r.get('total_trades', 0) - 20) * objectives.get('total_trades', 0.3),
            ]
            vectors.append(vec)

        vectors = np.array(vectors)
        pareto_indices = self._non_dominated_sort(vectors)
        pareto_results = [results[i] for i in pareto_indices]
        pareto_results.sort(key=lambda x: x.get('strategy_return', 0), reverse=True)
        return pareto_results

    def _non_dominated_sort(self, vectors):
        n = len(vectors)
        pareto = list(range(n))
        i = 0
        while i < len(pareto):
            dominated = False
            j = 0
            while j < len(pareto):
                if i == j:
                    j += 1
                    continue
                if self._dominates(vectors[pareto[j]], vectors[pareto[i]]):
                    pareto.pop(i)
                    dominated = True
                    break
                j += 1
            if not dominated:
                i += 1
        return pareto

    def _dominates(self, a, b):
        at_least_one_better = False
        for ai, bi in zip(a, b):
            if ai < bi:
                return False
            if ai > bi:
                at_least_one_better = True
        return at_least_one_better

    # ================================================================
    # 输出
    # ================================================================

    def print_results(self, results=None, top_n=10):
        if results is None:
            results = self.results
        if not results:
            print("无搜索结果")
            return

        sorted_results = sorted(results, key=lambda x: x.get('strategy_return', 0), reverse=True)[:top_n]

        print(f"\n{'='*100}")
        print(f"TOP {top_n} 参数组合（按收益率排序）")
        print(f"{'='*100}")

        header = (f"{'#':>3} {'策略收益':>10} {'超额':>8} {'夏普':>7} {'最大回撤':>8} "
                  f"{'Calmar':>7} {'胜率':>7} {'交易':>5} | 参数")
        print(header)
        print("-" * 100)

        for i, r in enumerate(sorted_results):
            param_str = ', '.join(
                f"{k}={v}" for k, v in r.items()
                if k in config.GRID_SEARCH_PARAMS
            )
            line = (f"{i+1:>3} {r['strategy_return']*100:>9.2f}% "
                    f"{r['excess_return']*100:>7.2f}% "
                    f"{r['sharpe_ratio']:>7.3f} "
                    f"{r['max_drawdown']*100:>7.2f}% "
                    f"{r['calmar_ratio']:>7.3f} "
                    f"{r['win_rate']*100:>6.1f}% "
                    f"{r['total_trades']:>5} | {param_str}")
            print(line)

        returns = [r['strategy_return'] for r in results]
        sharpes = [r['sharpe_ratio'] for r in results]
        print(f"\n--- 网格搜索统计 ---")
        print(f"总组合数: {len(results)}")
        print(f"收益率范围: {min(returns)*100:.2f}% ~ {max(returns)*100:.2f}%")
        print(f"收益率均值: {np.mean(returns)*100:.2f}%")
        print(f"夏普范围: {min(sharpes):.3f} ~ {max(sharpes):.3f}")

    def print_pareto_frontier(self, pareto_results=None):
        if pareto_results is None:
            pareto_results = self.find_pareto_frontier()
        if not pareto_results:
            print("无 Pareto 最优组合")
            return

        print(f"\n{'='*100}")
        print(f"Pareto 最优面 ({len(pareto_results)} 个组合)")
        print(f"{'='*100}")

        header = (f"{'#':>3} {'策略收益':>10} {'超额':>8} {'夏普':>7} {'最大回撤':>8} "
                  f"{'Calmar':>7} {'胜率':>7} {'交易':>5} | 参数")
        print(header)
        print("-" * 100)

        for i, r in enumerate(pareto_results[:20]):
            param_str = ', '.join(
                f"{k}={v}" for k, v in r.items()
                if k in config.GRID_SEARCH_PARAMS
            )
            line = (f"{i+1:>3} {r['strategy_return']*100:>9.2f}% "
                    f"{r['excess_return']*100:>7.2f}% "
                    f"{r['sharpe_ratio']:>7.3f} "
                    f"{r['max_drawdown']*100:>7.2f}% "
                    f"{r['calmar_ratio']:>7.3f} "
                    f"{r['win_rate']*100:>6.1f}% "
                    f"{r['total_trades']:>5} | {param_str}")
            print(line)

    def get_best_params(self, metric='calmar_ratio'):
        if not self.results:
            return None
        best = max(self.results, key=lambda x: x.get(metric, 0))
        return {k: v for k, v in best.items() if k in config.GRID_SEARCH_PARAMS}


# ============================================================
# 便捷函数
# ============================================================

def run_grid_search_sample():
    """运行示例网格搜索（少量组合，用于演示）"""
    df = load_data_from_db()
    if df is None:
        print("无法加载数据")
        return

    sample_grid = {
        'OBSERVATION_WINDOW_MAX': [3, 5],
        'CONFIRMATION_THRESHOLD': [65, 75],
        'CONFIDENCE_INCREMENT': [8, 10],
    }

    searcher = GridSearch(df, start_date='2025-09-01')
    results = searcher.run(param_grid=sample_grid, n_jobs=-1, verbose=True)
    searcher.print_results(top_n=min(8, len(results)))

    pareto = searcher.find_pareto_frontier()
    searcher.print_pareto_frontier(pareto)


def run_grid_search_full(n_jobs=-1, resume=True):
    """运行完整网格搜索"""
    df = load_data_from_db()
    if df is None:
        print("无法加载数据")
        return

    searcher = GridSearch(df, start_date='2025-09-01')
    results = searcher.run(n_jobs=n_jobs, resume=resume, verbose=True)
    searcher.print_results(top_n=15)

    pareto = searcher.find_pareto_frontier()
    searcher.print_pareto_frontier(pareto)

    # 输出最优参数
    print("\n--- 各指标最优参数 ---")
    for metric in ['strategy_return', 'sharpe_ratio', 'calmar_ratio']:
        best = searcher.get_best_params(metric)
        print(f"  {metric}: {best}")


if __name__ == "__main__":
    if '--full' in sys.argv:
        n_jobs = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else -1
        resume = '--fresh' not in sys.argv
        run_grid_search_full(n_jobs=n_jobs, resume=resume)
    elif '--status' in sys.argv:
        df = load_data_from_db()
        if df is not None:
            GridSearch(df).checkpoint_status()
    else:
        run_grid_search_sample()
