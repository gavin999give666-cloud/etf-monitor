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
                    # 子线程中重新激活（确保线程安全，各线程有独立config状态？
                    # 注意：config是模块全局，多线程共用。这里用锁保护。
                    with self._lock:
                        import config as cfg
                        cfg.activate_profile(target_code)
                        from data_updater import update_stock_data
                        # 捕获print输出
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
