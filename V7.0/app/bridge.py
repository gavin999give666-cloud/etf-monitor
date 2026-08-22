"""
V7.0 GUI Bridge —— pywebview js_api 薄封装层
=============================================

前端通过 window.pywebview.api.xxx() 调用本类的公开方法。
快操作（查询/信号计算）线程池直接执行；长任务（数据更新）通过 tasks.py 管理。

设计原则：
1. 所有返回值必须是 JSON 可序列化的 dict/list/str/int/float/bool/None
2. 异常必须捕获并以 {ok:false, error:'...'} 格式返回，避免前端崩溃
3. 主进程同一时刻只激活一个标的；切换 = 重新 activate + 标记缓存失效
4. 长任务不阻塞 GUI，返回 task_id 由前端轮询
"""
import os
import sys
import json
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor

# V7.0 根目录加入 path，使 import config / from strategy import ... 可用
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_V7_ROOT = os.path.dirname(_APP_DIR)
_QUANT_DIR = os.path.join(_V7_ROOT, 'quant')
if _QUANT_DIR not in sys.path:
    sys.path.insert(0, _QUANT_DIR)


class ApiBridge:
    """pywebview js_api 暴露类。方法名即前端调用名（camelCase由前端映射）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='gui_worker')
        self._tasks = {}           # task_id -> {status, result, error, progress}
        self._task_counter = 0
        self._strategy_cache = None    # V6Strategy 实例（按标的缓存，切换时失效）
        self._data_cache = None        # DataFrame 缓存
        self._cached_profile = None    # 缓存对应的标的代码

    # ============================================================
    # 内部工具
    # ============================================================

    def _safe(self, fn):
        """捕获异常，统一返回 {ok, data/error} 格式"""
        try:
            result = fn()
            return {'ok': True, 'data': result}
        except Exception as e:
            traceback.print_exc()
            return {'ok': False, 'error': str(e)}

    def _ensure_profile(self, code=None):
        """确保指定标的已激活；未指定则用当前激活的。
        返回 (config_module, is_switched)
        """
        import config
        if code and code != config.CURRENT_PROFILE_CODE:
            config.activate_profile(code)
            # 切换标的 → 失效缓存
            self._strategy_cache = None
            self._data_cache = None
            self._cached_profile = code
            return config, True
        if config.CURRENT_PROFILE_CODE is None:
            # 未激活过，用默认标的
            config.activate_profile(config.STOCK_CODE)
            self._cached_profile = config.STOCK_CODE
        return config, False

    def _load_data(self):
        """加载当前标的数据（带缓存）"""
        import config
        if self._data_cache is not None and self._cached_profile == config.CURRENT_PROFILE_CODE:
            return self._data_cache
        from data_updater import load_data_from_db
        df = load_data_from_db()
        self._data_cache = df
        self._cached_profile = config.CURRENT_PROFILE_CODE
        return df

    def _get_strategy(self):
        """获取当前标的策略实例（带缓存）"""
        import config
        if self._strategy_cache is not None and self._cached_profile == config.CURRENT_PROFILE_CODE:
            return self._strategy_cache
        from strategy import V6Strategy
        strategy = V6Strategy(use_ml=True)
        self._strategy_cache = strategy
        self._cached_profile = config.CURRENT_PROFILE_CODE
        return strategy

    def _new_task_id(self):
        self._task_counter += 1
        return f'task_{self._task_counter}'

    # ============================================================
    # 1. 标的管理
    # ============================================================

    def list_profiles(self):
        """列出所有可用标的
        Returns: {ok, data: [{code, name, optimized, has_db}]}
        """
        def _do():
            import config
            profiles = []
            for code in config.available_profiles():
                try:
                    p = config.load_profile(code)
                    db_path = os.path.join(config.DATA_DIR, f'{code}.db')
                    profiles.append({
                        'code': code,
                        'name': p.get('name', code),
                        'optimized': bool(p.get('optimized', False)),
                        'has_db': os.path.exists(db_path),
                    })
                except Exception as e:
                    profiles.append({
                        'code': code,
                        'name': f'<读取失败: {e}>',
                        'optimized': False,
                        'has_db': False,
                    })
            return profiles
        return self._safe(_do)

    def get_current_profile(self):
        """获取当前激活标的信息
        Returns: {ok, data: {code, name, optimized, market}}
        """
        def _do():
            import config
            self._ensure_profile()  # 确保至少有默认激活
            return {
                'code': config.CURRENT_PROFILE_CODE or config.STOCK_CODE,
                'name': config.ETF_NAME,
                'optimized': config.OPTIMIZED,
                'market': config.MARKET,
            }
        return self._safe(_do)

    def switch_profile(self, code):
        """切换标的
        Args:
            code: 标的代码，如 '589800'
        Returns: {ok, data: {code, name, optimized}}
        """
        def _do():
            import config
            if code not in config.available_profiles():
                raise ValueError(f'标的 {code} 不存在，可用: {config.available_profiles()}')
            config.activate_profile(code)
            self._strategy_cache = None
            self._data_cache = None
            self._cached_profile = code
            return {
                'code': code,
                'name': config.ETF_NAME,
                'optimized': config.OPTIMIZED,
            }
        return self._safe(_do)

    def add_profile(self, code, name, market='sh'):
        """添加新标的（从 _default.json 复制参数）
        Args:
            code: 标的代码，如 '588000'
            name: 标的名称，如 '科创50 ETF'
            market: 'sh' 或 'sz'
        Returns: {ok, data: {code, name}}
        """
        def _do():
            import config
            import json
            if not code or not code.isdigit():
                raise ValueError('标的代码必须是数字字符串')
            if not name:
                raise ValueError('标的名称不能为空')
            target_path = os.path.join(config.PROFILES_DIR, f'{code}.json')
            if os.path.exists(target_path):
                raise ValueError(f'标的 {code} 已存在')

            # 从 _default.json 复制
            default_path = os.path.join(config.PROFILES_DIR, '_default.json')
            if not os.path.exists(default_path):
                raise ValueError('_default.json 不存在，无法创建新标的')
            with open(default_path, encoding='utf-8') as f:
                profile = json.load(f)
            profile['code'] = code
            profile['name'] = name
            profile['market'] = market
            profile['optimized'] = False
            with open(target_path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)

            # 确保 data/runs 目录存在
            os.makedirs(os.path.join(config.DATA_DIR), exist_ok=True)
            os.makedirs(os.path.join(config.RUNS_ROOT, code), exist_ok=True)
            return {'code': code, 'name': name}
        return self._safe(_do)

    # ============================================================
    # 2. 数据管理
    # ============================================================

    def get_data_overview(self, code=None):
        """获取数据概览
        Returns: {ok, data: {start_date, end_date, row_count, has_estimated, db_exists}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            db_path = os.path.join(config.DATA_DIR, f'{config.CURRENT_PROFILE_CODE}.db')
            if not os.path.exists(db_path):
                return {
                    'db_exists': False,
                    'start_date': None,
                    'end_date': None,
                    'row_count': 0,
                    'has_estimated': False,
                }
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT MIN(date), MAX(date), COUNT(*) FROM stock_data"
                ).fetchone()
                est_count = conn.execute(
                    "SELECT COUNT(*) FROM stock_data WHERE is_estimated = 1"
                ).fetchone()[0]
            finally:
                conn.close()
            return {
                'db_exists': True,
                'start_date': row[0],
                'end_date': row[1],
                'row_count': row[2],
                'has_estimated': est_count > 0,
                'estimated_count': est_count,
            }
        return self._safe(_do)

    def get_runtime_context(self):
        """获取运行时环境（交易日/时段）
        Returns: {ok, data: {now, is_trading_day, phase, description}}
        """
        def _do():
            self._ensure_profile()
            from data_updater import get_runtime_context
            ctx = get_runtime_context()
            _PHASE_CN = {
                'non_trading_day': '周末/节假日',
                'pre_market': '盘前时段',
                'intraday': '盘中时段',
                'closing': '收盘竞价时段',
                'post_market': '盘后时段',
            }
            day_cn = '交易日' if ctx['is_trading_day'] else '非交易日'
            return {
                'now': ctx['now'].strftime('%Y-%m-%d %H:%M:%S'),
                'is_trading_day': ctx['is_trading_day'],
                'phase': ctx['phase'],
                'description': f"{day_cn} · {_PHASE_CN[ctx['phase']]}",
            }
        return self._safe(_do)

    def update_data(self, code=None):
        """触发数据更新（异步，返回 task_id）
        Returns: {ok, data: {task_id}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            target_code = config.CURRENT_PROFILE_CODE
            task_id = self._new_task_id()
            self._tasks[task_id] = {'status': 'running', 'progress': 0, 'result': None, 'error': None}

            def _worker():
                try:
                    # 只在激活标的时加锁（config是模块全局，需保护）
                    with self._lock:
                        import config as cfg
                        cfg.activate_profile(target_code)
                    # 数据更新不在锁内执行，避免长时间阻塞
                    from data_updater import update_stock_data
                    import io
                    from contextlib import redirect_stdout, redirect_stderr
                    buf = io.StringIO()
                    with redirect_stdout(buf), redirect_stderr(buf):
                        update_stock_data()
                    output = buf.getvalue()
                    # 数据更新后失效缓存
                    self._data_cache = None
                    self._strategy_cache = None
                    self._tasks[task_id]['status'] = 'done'
                    self._tasks[task_id]['result'] = {'output': output[-2000:]}  # 最后2000字
                except Exception as e:
                    traceback.print_exc()
                    self._tasks[task_id]['status'] = 'error'
                    self._tasks[task_id]['error'] = str(e)

            self._thread_pool.submit(_worker)
            return {'task_id': task_id}
        return self._safe(_do)

    def update_intraday(self, code=None):
        """触发盘中估算（异步）
        Returns: {ok, data: {task_id}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            target_code = config.CURRENT_PROFILE_CODE
            task_id = self._new_task_id()
            self._tasks[task_id] = {'status': 'running', 'progress': 0, 'result': None, 'error': None}

            def _worker():
                try:
                    with self._lock:
                        import config as cfg
                        cfg.activate_profile(target_code)
                    from data_updater import update_intraday
                    import io
                    from contextlib import redirect_stdout, redirect_stderr
                    buf = io.StringIO()
                    with redirect_stdout(buf), redirect_stderr(buf):
                        success = update_intraday()
                    output = buf.getvalue()
                    self._data_cache = None
                    self._strategy_cache = None
                    self._tasks[task_id]['status'] = 'done'
                    self._tasks[task_id]['result'] = {'success': success, 'output': output[-2000:]}
                except Exception as e:
                    traceback.print_exc()
                    self._tasks[task_id]['status'] = 'error'
                    self._tasks[task_id]['error'] = str(e)

            self._thread_pool.submit(_worker)
            return {'task_id': task_id}
        return self._safe(_do)

    def backfill_data(self, code=None):
        """触发T+1回填（异步）
        Returns: {ok, data: {task_id}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            target_code = config.CURRENT_PROFILE_CODE
            task_id = self._new_task_id()
            self._tasks[task_id] = {'status': 'running', 'progress': 0, 'result': None, 'error': None}

            def _worker():
                try:
                    with self._lock:
                        import config as cfg
                        cfg.activate_profile(target_code)
                    from data_updater import backfill_estimated_data
                    import io
                    from contextlib import redirect_stdout, redirect_stderr
                    buf = io.StringIO()
                    with redirect_stdout(buf), redirect_stderr(buf):
                        count = backfill_estimated_data()
                    output = buf.getvalue()
                    self._data_cache = None
                    self._strategy_cache = None
                    self._tasks[task_id]['status'] = 'done'
                    self._tasks[task_id]['result'] = {'backfilled_count': count or 0, 'output': output[-2000:]}
                except Exception as e:
                    traceback.print_exc()
                    self._tasks[task_id]['status'] = 'error'
                    self._tasks[task_id]['error'] = str(e)

            self._thread_pool.submit(_worker)
            return {'task_id': task_id}
        return self._safe(_do)

    def get_task_status(self, task_id):
        """查询任务状态
        Returns: {ok, data: {status, result, error}}
        """
        def _do():
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f'任务不存在: {task_id}')
            return {
                'status': task['status'],
                'result': task['result'],
                'error': task['error'],
            }
        return self._safe(_do)

    # ============================================================
    # 3. 信号
    # ============================================================

    def get_today_signal(self, code=None):
        """获取今日信号（结构化数据，供前端渲染）
        Returns: {ok, data: {regime, psychology, buy_score, sell_score, net_score,
                             reward_score, risk_score, buy_behaviors, sell_behaviors,
                             confirmed_buy_events, confirmed_sell_events, active_events,
                             evidence_buy, evidence_sell, target_position, signal_text,
                             last_date, data_rows}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            df = self._load_data()
            if df is None or len(df) < 60:
                return {
                    'available': False,
                    'reason': '数据不足，需要至少60天数据，请先更新数据',
                }
            from indicators import calculate_indicators
            df_ind = calculate_indicators(df)
            strategy = self._get_strategy()
            signals = strategy.run(df_ind)
            if not signals:
                return {'available': False, 'reason': '无信号'}

            last = signals[-1]
            # Evidence Engine 分解
            ev_buy = []
            ev_sell = []
            ev_debug_buy = last.get('evidence_debug', {}).get('buy', [])
            ev_debug_sell = last.get('evidence_debug', {}).get('sell', [])
            if ev_debug_buy and ev_debug_buy[0]:
                ev = ev_debug_buy[0]
                for src, info in ev.get('sources', {}).items():
                    contrib = info.get('contribution', 0)
                    if contrib != 0:
                        ev_buy.append({'source': src, 'contribution': round(contrib, 1)})
            if ev_debug_sell and ev_debug_sell[0]:
                ev = ev_debug_sell[0]
                for src, info in ev.get('sources', {}).items():
                    contrib = info.get('contribution', 0)
                    if contrib != 0:
                        ev_sell.append({'source': src, 'contribution': round(contrib, 1)})

            # 目标仓位（以初始仓位为基准计算目标）
            import config
            from scoring_engine import score_to_target_position
            target_pos = score_to_target_position(
                last['buy_score'], last['sell_score'],
                config.INITIAL_POSITION
            )

            return {
                'available': True,
                'last_date': last['date'].strftime('%Y-%m-%d') if hasattr(last['date'], 'strftime') else str(last['date']),
                'regime': last.get('regime', 'N/A'),
                'psychology': last.get('psychology', 'N/A'),
                'emotion_score': round(last.get('emotion_score', 0), 2) if last.get('emotion_score') is not None else 'N/A',
                'emotion_improving': last.get('emotion_improving', False),
                'buy_score': round(last.get('buy_score', 0), 1),
                'sell_score': round(last.get('sell_score', 0), 1),
                'net_score': round(last.get('buy_score', 0) - last.get('sell_score', 0), 1),
                'reward_score': round(last.get('reward_score', 0), 1),
                'risk_score': round(last.get('risk_score', 0), 1),
                'buy_behaviors': last.get('buy_behaviors', []),
                'sell_behaviors': last.get('sell_behaviors', []),
                'confirmed_buy_events': last.get('confirmed_buy_events', 0),
                'confirmed_sell_events': last.get('confirmed_sell_events', 0),
                'active_events': last.get('active_events', 0),
                'evidence_buy': ev_buy,
                'evidence_sell': ev_sell,
                'target_position': round(target_pos * 100, 1),
                'data_rows': len(df),
            }
        return self._safe(_do)

    def get_recent_prices(self, code=None, days=60):
        """获取近期K线数据（用于迷你图）
        Returns: {ok, data: [{date, open, high, low, close, volume}]}
        """
        def _do():
            import config
            self._ensure_profile(code)
            df = self._load_data()
            if df is None:
                return []
            recent = df.tail(days).reset_index()
            result = []
            for _, row in recent.iterrows():
                result.append({
                    'date': row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date']),
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row['volume']),
                })
            return result
        return self._safe(_do)

    # ============================================================
    # 4. 回测
    # ============================================================

    def run_backtest(self, code=None):
        """异步运行回测，返回 task_id（结果载荷在前端轮询时通过 get_task_status 获取）。
        计算路径与 CLI --eval 完全一致：strategy.run(df) -> bt.run(signals)，
        绩效指标取同一 results 字典，保证"指标与 CLI 一致"。

        优化：锁仅保护 activate_profile（config是模块全局），计算本身不在锁内，
        避免长时间阻塞其他快操作（信号查询/数据概览等）。

        Returns: {ok, data: {task_id}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            target_code = config.CURRENT_PROFILE_CODE
            task_id = self._new_task_id()
            self._tasks[task_id] = {'status': 'running', 'progress': 0, 'result': None, 'error': None}

            def _worker():
                try:
                    # 只在激活标的时加锁（config是模块全局变量，需保护）
                    with self._lock:
                        import config as cfg
                        cfg.activate_profile(target_code)
                    # 回测计算不在锁内执行，避免阻塞其他 GUI 操作
                    payload = _build_backtest_payload()
                    self._tasks[task_id]['status'] = 'done'
                    self._tasks[task_id]['result'] = {'backtest': payload}
                except Exception as e:
                    traceback.print_exc()
                    self._tasks[task_id]['status'] = 'error'
                    self._tasks[task_id]['error'] = str(e)

            # 回测用独立 daemon 线程执行，不占用公共线程池（max_workers=2），
            # 避免被 update_data 等长任务占满 worker 时回测排队，导致前端无限转圈
            threading.Thread(target=_worker, name='gui_backtest', daemon=True).start()
            return {'task_id': task_id}
        return self._safe(_do)

    # ============================================================
    # 5. 参数优化
    # ============================================================

    def list_optimize_modes(self):
        """列出所有优化模式（来自 optimizer_modes 注册表）
        Returns: {ok, data: [{key, label, desc, default_trials, default_file}]}
        """
        def _do():
            from optimizer_modes import MODES
            return [{
                'key': m.key,
                'label': m.label,
                'desc': m.desc,
                'default_trials': m.default_trials,
                'default_file': m.default_file,
            } for m in MODES]
        return self._safe(_do)

    def start_optimization(self, code=None, mode_key='light-excess', trials=300, cpu_limit=100):
        """启动参数优化（异步，返回 task_id）。

        复用 param_optimizer 的自适应控制 + 进度面板 + 控制信号机制：
        - enable_adaptive_control(cpu_limit) 初始化 DASHBOARD_STATE（幂等）
        - install_stdout_tee() 把 print/tqdm 逐行转发到日志面板
        - build_run(mode_key, trials, output_path) 构建计算入口
        结果落盘 runs\{code}\{default_file}.json。
        """
        def _do():
            import config
            self._ensure_profile(code)
            target_code = config.CURRENT_PROFILE_CODE
            from optimizer_modes import get_mode, build_run
            mode = get_mode(mode_key)
            if mode is None:
                raise ValueError(f'未知优化模式: {mode_key}')
            task_id = self._new_task_id()
            self._tasks[task_id] = {'status': 'running', 'progress': 0, 'result': None, 'error': None}

            def _worker():
                try:
                    with self._lock:
                        import config as cfg
                        cfg.activate_profile(target_code)
                    import param_optimizer as po
                    if not po.ADAPTIVE_ENABLED:
                        po.enable_adaptive_control(cpu_limit=float(cpu_limit))
                    else:
                        po.GOVERNOR.set_limit(float(cpu_limit))
                    po.clear_control_flags()
                    po.install_stdout_tee()
                    if po.DASHBOARD_STATE is not None:
                        po.DASHBOARD_STATE.clear_log()  # 清空上一任务残留日志
                    output_path = config.runs_path(f'{mode.default_file}.json')
                    n_jobs = max(1, int(os.cpu_count() or 4) * int(cpu_limit) // 100)
                    ga_n_jobs = max(1, n_jobs - 4)
                    run_fn, kwargs = build_run(
                        mode_key, int(trials), output_path,
                        n_jobs=n_jobs, ga_n_jobs=ga_n_jobs,
                    )
                    run_fn(**kwargs)
                    self._tasks[task_id]['status'] = 'done'
                    self._tasks[task_id]['result'] = {
                        'output_path': output_path,
                        'mode': mode_key,
                        'trials': int(trials),
                    }
                except Exception as e:
                    traceback.print_exc()
                    self._tasks[task_id]['status'] = 'error'
                    self._tasks[task_id]['error'] = str(e)

            threading.Thread(target=_worker, name='gui_optimize', daemon=True).start()
            return {'task_id': task_id}
        return self._safe(_do)

    def get_optimization_status(self, task_id):
        """查询优化任务状态 + 进度面板快照
        Returns: {ok, data: {status, result, error, dashboard}}
        dashboard = DASHBOARD_STATE.snapshot()（progress/events/log_lines/log_seq/workers/cpu）
        """
        def _do():
            task = self._tasks.get(task_id)
            if not task:
                raise ValueError(f'任务不存在: {task_id}')
            import param_optimizer as po
            dashboard = None
            if po.DASHBOARD_STATE is not None:
                dashboard = po.DASHBOARD_STATE.snapshot()
            return {
                'status': task['status'],
                'result': task['result'],
                'error': task['error'],
                'dashboard': dashboard,
            }
        return self._safe(_do)

    def pause_optimization(self):
        """请求暂停（当前计算完成后暂停）"""
        def _do():
            import param_optimizer as po
            po.request_pause()
            return True
        return self._safe(_do)

    def resume_optimization(self):
        """请求继续"""
        def _do():
            import param_optimizer as po
            po.request_resume()
            return True
        return self._safe(_do)

    def stop_optimization(self):
        """请求优雅停止（等待当前计算完成）"""
        def _do():
            import param_optimizer as po
            po.request_stop()
            return True
        return self._safe(_do)

    def set_cpu_limit(self, cpu_limit):
        """运行中动态调整 CPU 上限"""
        def _do():
            import param_optimizer as po
            if po.ADAPTIVE_ENABLED and po.GOVERNOR is not None:
                po.GOVERNOR.set_limit(float(cpu_limit))
            return True
        return self._safe(_do)

    def get_optimization_results(self, code=None):
        """读取当前标的所有优化结果文件
        Returns: {ok, data: {filename: {meta, phase_summary, top20, walk_forward}, acceptance}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            results = {}
            for fname in ('heavy_excess_results.json', 'light_excess_results.json',
                          'heavy_results.json', 'light_results.json'):
                path = config.runs_path(fname)
                if os.path.exists(path):
                    with open(path, encoding='utf-8') as f:
                        data = json.load(f)
                    results[fname] = {
                        'meta': data.get('meta', {}),
                        'phase_summary': data.get('phase_summary', {}),
                        'top20': data.get('top20', []),
                        'walk_forward': data.get('walk_forward', []),
                    }
            # 附带验收状态（供结果卡徽章展示）
            acceptance = None
            acc_path = config.runs_path('acceptance_report.json')
            if os.path.exists(acc_path):
                with open(acc_path, encoding='utf-8') as f:
                    acc = json.load(f)
                acceptance = {
                    'accepted': bool(acc.get('accepted', False)),
                    'generated_at': acc.get('meta', {}).get('generated_at', ''),
                    'checks': acc.get('checks', []),
                }
            results['acceptance'] = acceptance
            return results
        return self._safe(_do)

    def apply_optimized_params(self, code=None, results_json=None):
        """一键应用参数：调 gen_profiles.apply_optimized_auto 写回 profile。
        成功后重载 profile + 失效缓存。
        Returns: {ok, data: {applied, code, acceptance}}
        """
        def _do():
            import config
            self._ensure_profile(code)
            target_code = config.CURRENT_PROFILE_CODE
            rj = results_json or config.runs_path('light_excess_results.json')
            tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
            if tools_dir not in sys.path:
                sys.path.insert(0, tools_dir)
            import gen_profiles
            ok = gen_profiles.apply_optimized_auto(target_code, rj)
            # 重载 profile + 失效缓存
            config.activate_profile(target_code)
            self._strategy_cache = None
            self._data_cache = None
            self._cached_profile = target_code
            # 验收状态
            acceptance = None
            acc_path = config.runs_path('acceptance_report.json')
            if os.path.exists(acc_path):
                with open(acc_path, encoding='utf-8') as f:
                    acc = json.load(f)
                acceptance = {
                    'accepted': bool(acc.get('accepted', False)),
                    'generated_at': acc.get('meta', {}).get('generated_at', ''),
                    'checks': acc.get('checks', []),
                }
            return {'applied': ok, 'code': target_code, 'acceptance': acceptance}
        return self._safe(_do)

    def get_diagnostics(self, code=None):
        """优化前诊断：仓位分布 / 策略-基准相关性 / 超额来源分解"""
        def _do():
            import config
            self._ensure_profile(code)
            return _build_diagnostics()
        return self._safe(_do)

    def get_param_versions(self, code=None):
        """V6 vs V7 参数版本对比"""
        def _do():
            import config
            self._ensure_profile(code)
            return _build_param_versions()
        return self._safe(_do)


def _sanitize(value):
    """递归把 numpy 标量转换为原生 Python 类型（保证 JSON 可序列化）"""
    import numpy as np
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return round(float(value), 4)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _build_backtest_payload():
    """运行与 CLI --eval 完全一致的回测，返回可直接交给前端的 JSON 载荷。

    计算路径：strategy.run(df) -> bt.run(signals)，绩效指标读取同一 results 字典，
    进位规则与 evaluation.print_performance / backtest.export_all 保持一致。
    同时写入 runs\{code}\backtest_records.json（与 CLI 行为一致）。
    """
    import config
    from data_updater import load_data_from_db
    from strategy import V6Strategy
    from backtest import V6Backtest

    df = load_data_from_db()
    if df is None or len(df) == 0:
        return {'available': False, 'reason': '无法加载数据，请先更新数据'}

    strategy = V6Strategy(use_ml=True, emotion_method='weighted')
    signals = strategy.run(df)
    bt = V6Backtest(df, strategy=strategy)
    results = bt.run(signals)

    if not results:
        return {'available': False, 'reason': '回测失败，未生成结果'}

    # 写回测档案（与 CLI --eval 每次覆盖行为一致）
    try:
        bt.export_all(results=results)
    except Exception:
        pass

    # ---- 绩效指标（进位规则与 evaluation::print_performance / export_all 相同）----
    r = results
    performance = {
        'strategy_return_pct': round(r.get('strategy_return', 0) * 100, 2),
        'benchmark_return_pct': round(r.get('benchmark_return', 0) * 100, 2),
        'excess_return_pct': round(r.get('excess_return', 0) * 100, 2),
        'max_drawdown_pct': round(r.get('max_drawdown', 0) * 100, 2),
        'annualized_return_pct': round(r.get('annualized_return', 0) * 100, 2),
        'volatility_pct': round(r.get('volatility', 0) * 100, 2),
        'sharpe_ratio': round(r.get('sharpe_ratio', 0), 3),
        'sortino_ratio': round(r.get('sortino_ratio', 0), 3),
        'calmar_ratio': round(r.get('calmar_ratio', 0), 3),
        'profit_factor': round(r.get('profit_factor', 0), 2),
        'win_rate_pct': round(r.get('win_rate', 0) * 100, 1),
        'kelly': round(r.get('kelly', 0), 3),
        'expectancy_pct': round(r.get('expectancy', 0), 2),
        'total_trades': int(r.get('total_trades', 0)),
        'winning_trades': int(r.get('winning_trades', 0)),
        'losing_trades': int(r.get('losing_trades', 0)),
        'avg_hold_days': round(r.get('avg_hold_days', 0), 1),
        'final_equity': round(r.get('final_equity', 0), 2),
        'start_equity': round(r.get('start_equity', 0), 2),
        'max_consecutive_wins': int(r.get('max_consecutive_wins', 0)),
        'max_consecutive_losses': int(r.get('max_consecutive_losses', 0)),
    }

    # ---- 净值 / 基准 / 回撤曲线 ----
    import numpy as np
    eq_map = {e['date']: e['equity'] for e in bt.daily_equity}
    start_price = bt.df['close'].iloc[0]
    start_eq = r.get('start_equity', eq_map.get(bt.df.index[0], 0))
    equity = []
    eq_series = []
    for date, row in bt.df.iterrows():
        eq = eq_map.get(date)
        if eq is None:
            continue
        bench = start_eq * (float(row['close']) / float(start_price))
        equity.append({
            'date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'strategy': round(float(eq), 2),
            'benchmark': round(float(bench), 2),
        })
        eq_series.append(float(eq))

    # 回撤序列（基于策略净值）
    if eq_series:
        cummax = np.maximum.accumulate(eq_series)
        dd = [(float(e) - float(c)) / float(c) * 100 for e, c in zip(eq_series, cummax)]
        for p, d in zip(equity, dd):
            p['drawdown'] = round(d, 2)

    # ---- K线 + 买卖成交标记 ----
    price_series = []
    for date, row in bt.df.iterrows():
        price_series.append({
            'date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'open': round(float(row['open']), 4),
            'high': round(float(row['high']), 4),
            'low': round(float(row['low']), 4),
            'close': round(float(row['close']), 4),
        })
    buy_markers, sell_markers = [], []
    for ds in bt.daily_signals:
        if not ds.get('executed'):
            continue
        action = ds.get('action', '')
        date = ds.get('date')
        if date is None:
            continue
        try:
            close = float(bt.df.loc[date, 'close'])
        except Exception:
            continue
        ds_date = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)
        if action == 'BUY':
            buy_markers.append({'date': ds_date, 'price': round(close, 4)})
        elif action == 'SELL':
            sell_markers.append({'date': ds_date, 'price': round(close, 4)})

    # ---- 交易明细 ----
    trades = []
    for t in bt.trades:
        trades.append({
            'trade_id': int(t.get('trade_id', 0)),
            'entry_date': str(t.get('entry_date', '')),
            'exit_date': str(t.get('exit_date', '')),
            'entry_price': round(float(t.get('entry_price', 0)), 4),
            'exit_price': round(float(t.get('exit_price', 0)), 4),
            'pnl_pct': round(float(t.get('pnl_pct', 0)), 2),
            'pnl_label': t.get('pnl_label', ''),
            'entry_behavior': t.get('entry_behavior', []),
            'exit_behavior': t.get('exit_behavior', []),
            'entry_regime': t.get('entry_regime', 'Unknown'),
            'entry_psychology': t.get('entry_psychology', 'Unknown'),
            'exit_psychology': t.get('exit_psychology', 'Unknown'),
            'entry_score': round(float(t.get('entry_score', 0)), 1),
            'exit_score': round(float(t.get('exit_score', 0)), 1),
            'entry_emotion_improving': bool(t.get('entry_emotion_improving', False)),
            'entry_factors': _sanitize(t.get('entry_factors', {})),
            'exit_factors': _sanitize(t.get('exit_factors', {})),
        })

    return {
        'available': True,
        'meta': {
            'etf_name': config.ETF_NAME,
            'code': config.CURRENT_PROFILE_CODE or config.STOCK_CODE,
            'data_start': df.index[0].strftime('%Y-%m-%d'),
            'data_end': df.index[-1].strftime('%Y-%m-%d'),
            'data_rows': int(len(df)),
            'backtest_start': str(bt.df.index[0])[:10],
            'backtest_end': str(bt.df.index[-1])[:10],
            'trading_days': int(r.get('trading_days', len(bt.daily_equity))),
            'generated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
        'performance': performance,
        'equity': equity,
        'price': price_series,
        'buy_markers': buy_markers,
        'sell_markers': sell_markers,
        'trades': trades,
    }


def _build_diagnostics():
    """优化前诊断数据：仓位分布直方图 / 策略-基准日收益相关性 / 超额来源分解。

    计算路径与 CLI --eval 同源（strategy.run -> bt.run）。
    超额分解恒等式：excess = position + timing + residual（残差吸收成本与近似误差）：
      - position_pp = (mean(pos_held) - 1) * Σ r_t         平均仓位效应
      - timing_pp   = Σ (pos_held - mean(pos_held)) * r_t  择时协方差效应
      - residual_pp = excess - position - timing
    """
    import config
    from data_updater import load_data_from_db
    from strategy import V6Strategy
    from backtest import V6Backtest
    import numpy as np

    df = load_data_from_db()
    if df is None or len(df) == 0:
        return {'available': False, 'reason': '无法加载数据，请先更新数据'}

    strategy = V6Strategy(use_ml=True, emotion_method='weighted')
    signals = strategy.run(df)
    bt = V6Backtest(df, strategy=strategy)
    results = bt.run(signals)
    if not results:
        return {'available': False, 'reason': '回测失败，未生成结果'}

    # ---- 仓位分布（10 等分桶）----
    positions = [s.get('current_position', 0) for s in bt.daily_signals]
    pos_dist = []
    n = len(positions)
    if n:
        for i in range(10):
            lo, hi = i / 10, (i + 1) / 10
            cnt = sum(1 for p in positions if lo <= p < hi)
            pos_dist.append({
                'bin': f'{lo:.1f}-{hi:.1f}',
                'count': cnt,
                'pct': round(cnt / n * 100, 1),
            })
        pos_stats = {
            'min': round(min(positions), 4),
            'max': round(max(positions), 4),
            'mean': round(sum(positions) / n, 4),
            'pct_ge_90': round(sum(1 for p in positions if p >= 0.90) / n * 100, 1),
            'pct_lt_30': round(sum(1 for p in positions if p < 0.30) / n * 100, 1),
            'pct_lt_70': round(sum(1 for p in positions if p < 0.70) / n * 100, 1),
        }
    else:
        pos_stats = {}

    # ---- 策略/基准日收益 + 相关性 + 超额分解 ----
    closes = bt.df['close'].astype(float).values
    r_bench = np.diff(closes) / closes[:-1]
    pos_arr = np.array(positions[:len(r_bench)], dtype=float)
    pos_held = np.concatenate([[pos_arr[0]], pos_arr[:-1]]) if len(pos_arr) else pos_arr
    eq = np.array([e['equity'] for e in bt.daily_equity], dtype=float)
    r_strat = np.diff(eq) / eq[:-1]

    m = min(len(r_strat), len(r_bench))
    corr = float(np.corrcoef(r_strat[:m], r_bench[:m])[0, 1]) if m > 1 else 0.0

    excess = float(np.sum((pos_held - 1.0) * r_bench))
    position_eff = float((np.mean(pos_held) - 1.0) * np.sum(r_bench))
    timing_eff = float(np.sum((pos_held - np.mean(pos_held)) * r_bench))
    residual = excess - position_eff - timing_eff

    return {
        'available': True,
        'meta': {
            'code': config.CURRENT_PROFILE_CODE or config.STOCK_CODE,
            'data_start': str(df.index[0])[:10],
            'data_end': str(df.index[-1])[:10],
            'trading_days': int(len(df)),
        },
        'position_dist': pos_dist,
        'position_stats': pos_stats,
        'correlation': {
            'pearson_r': round(corr, 4),
            'n': m,
            'strategy_vol_pct': round(float(np.std(r_strat) * np.sqrt(252) * 100), 2) if len(r_strat) > 1 else 0,
            'benchmark_vol_pct': round(float(np.std(r_bench) * np.sqrt(252) * 100), 2) if len(r_bench) > 1 else 0,
        },
        'excess_decomp': {
            'excess_pp': round(excess * 100, 2),
            'position_pp': round(position_eff * 100, 2),
            'timing_pp': round(timing_eff * 100, 2),
            'residual_pp': round(residual * 100, 2),
            'avg_position': round(float(np.mean(pos_held)), 4) if len(pos_held) else 0,
            'benchmark_total_pct': round(float(np.sum(r_bench) * 100), 2),
        },
        'performance': {
            'strategy_return_pct': round(results.get('strategy_return', 0) * 100, 2),
            'benchmark_return_pct': round(results.get('benchmark_return', 0) * 100, 2),
            'excess_return_pct': round(results.get('excess_return', 0) * 100, 2),
            'max_drawdown_pct': round(results.get('max_drawdown', 0) * 100, 2),
            'total_trades': int(results.get('total_trades', 0)),
        },
    }


def _build_param_versions():
    """V6 vs V7 参数版本对比：V6 值取自 V6.2.3 旧 config，V7 值取自当前 profile"""
    import config
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import gen_profiles
    code = config.CURRENT_PROFILE_CODE or config.STOCK_CODE
    old_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'V6.2.3')
    old_name = '科创' if code == '589800' else 'A500'
    old_path = os.path.join(old_dir, old_name, 'config.py')
    if not os.path.exists(old_path):
        return {'available': False, 'reason': f'旧版 config 不存在: {old_path}'}
    old = gen_profiles.load_module('old_config', old_path)
    profile = config.load_profile(code)

    rows = []
    for k in gen_profiles.STRATEGY_KEYS:
        v6 = getattr(old, k, None)
        v7 = profile.get(k)
        if v6 is None and v7 is None:
            continue
        is_new = v6 is None
        changed = (not is_new) and (_sanitize(v6) != _sanitize(v7))
        rows.append({
            'key': k,
            'v6': _sanitize(v6),
            'v7': _sanitize(v7),
            'changed': bool(changed),
            'is_new': bool(is_new),
        })
    rows.sort(key=lambda r: (r['is_new'], r['key']))
    return {
        'available': True,
        'code': code,
        'rows': rows,
        'summary': {
            'total': len(rows),
            'changed': sum(1 for r in rows if r['changed']),
            'new': sum(1 for r in rows if r['is_new']),
        },
    }
