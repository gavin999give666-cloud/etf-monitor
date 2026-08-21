"""
V6.2.3 智能参数优化器（Smart Parameter Optimizer）
==================================================

超越传统网格搜索的三层优化方法：
1. 贝叶斯优化（Optuna TPE）—— 比网格搜索高效 10-100 倍
2. 遗传算法（Genetic Algorithm）—— 种群进化，全局搜索
3. 逐级精细网格搜索（Coarse-to-Fine Grid）—— 层级收敛

核心改进（vs 原 param_search.py）：
- 搜索空间扩展 X3：加入评分权重、市场状态乘数、仓位映射参数
- 复合目标函数：同时优化收益率、夏普、回撤、Calmar
- 智能采样：不浪费算力在显然低收益的组合上

用法：
  python param_optimizer.py --method optuna --trials 500 --jobs -1
  python param_optimizer.py --method genetic  --generations 50 --population 40
  python param_optimizer.py --method coarse2fine --levels 3
  python param_optimizer.py --method all  # 依次运行三种方法

可视化控制面板（独立 EXE，无需浏览器）：
  python param_optimizer.py --gui --cpu-limit 20   # 控制面板 + 程序CPU占用≤20%
  python param_optimizer.py --gui                   # 默认 100% 最大性能模式
  ParamOptimizerUI.exe                              # 打包后的独立 EXE（见 build_exe.ps1）
"""

import copy
import gc
import multiprocessing
import os

# ═══ 在 import numpy 之前限制 BLAS 线程数 ═══
# 每个进程（主进程 + 每个 spawn 的 worker）的 numpy/OpenBLAS 默认按
# 全部逻辑核初始化线程池。多进程场景下：主进程 + N 个 worker × 核数
# = 数百条 BLAS 线程，每条线程都 commit 内存，直接推高"已提交内存"
# 到提交上限（表现为：committed 占满、pagefile 使用率却很低、内存分配
# 报"页面文件太小"）。策略回测为串行小计算，限制线程数无性能损失。
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')
os.environ.setdefault('NUMEXPR_NUM_THREADS', '1')

import pickle
import random
import sys
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Callable

import numpy as np
import pandas as pd

import config
from data_updater import load_data_from_db

# ═══ 预先导入策略/回测模块（重要！避免子进程重复导入的锁竞争）═══
# 这些模块在子进程启动时一次性加载，不会每 trial 重复 import
from indicators import calculate_indicators
from strategy import V6Strategy
from backtest import V6Backtest

# ═══ 数据缓存文件（子进程通过 pickle 高速反序列化）═══
_DATA_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '.data_cache.pkl')


def _prepare_data_cache(df, regenerate=False):
    """
    将预处理后的 DataFrame 写入 pickle 缓存文件。
    子进程通过 `_load_data_cache()` 直接反序列化，比 JSON 快 10 倍以上。
    """
    if os.path.exists(_DATA_CACHE_PATH) and not regenerate:
        return
    df_with = calculate_indicators(df.copy())
    cache = {
        'columns': df_with.columns.tolist(),
        'index_str': [str(d) for d in df_with.index],
        'values_dict': {col: df_with[col].tolist() for col in df_with.columns},
        'start_date': str(df_with.index[0].date()),
    }
    with open(_DATA_CACHE_PATH, 'wb') as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)


# 进程级内存缓存（spawn子进程首次加载后永久复用，避免每 trial 重复磁盘 I/O）
_DATA_CACHE_MEMORY = None


def _load_data_cache():
    """从 pickle 缓存加载 DataFrame（进程内首次加载后永久复用，零磁盘 I/O）"""
    global _DATA_CACHE_MEMORY
    if _DATA_CACHE_MEMORY is not None:
        return _DATA_CACHE_MEMORY
    with open(_DATA_CACHE_PATH, 'rb') as f:
        cache = pickle.load(f)
    df = pd.DataFrame(cache['values_dict'], columns=cache['columns'])
    df.index = pd.to_datetime(cache['index_str'])
    _DATA_CACHE_MEMORY = (df, cache['start_date'])
    return _DATA_CACHE_MEMORY

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

try:
    import optuna
    HAS_OPTUNA = True
    # optuna 4.x: JournalFileBackend 移到了 optuna.storages.journal 下
    try:
        from optuna.storages.journal import JournalStorage, JournalFileBackend
    except ImportError:
        # optuna 3.x: 直接从 optuna.storages 导入
        from optuna.storages import JournalStorage, JournalFileBackend
except ImportError:
    HAS_OPTUNA = False

# ═══ 自适应资源控制 + 可视化控制面板（--gui 启用，Tkinter 原生界面，无需浏览器）═══
# 启用后：并行进程数不再固定，由 CpuGovernor 根据"程序自身 CPU 占用"
# 与设定的资源限制（1~100%，100%=最大性能模式）实时增减；同时弹出
# 独立控制面板窗口（EXE 内嵌，不走外部浏览器）。
try:
    from adaptive_pool import (AdaptiveWorkerPool, CpuGovernor, DashboardState,
                               live_pools)
    HAS_ADAPTIVE = True
    ADAPTIVE_IMPORT_ERR = ''
except Exception as _adaptive_imp_err:
    HAS_ADAPTIVE = False
    ADAPTIVE_IMPORT_ERR = str(_adaptive_imp_err)

ADAPTIVE_ENABLED = False
GOVERNOR = None
DASHBOARD_STATE = None

# ===== 信号文件控制（暂停/停止/紧急停止） =====
PAUSE_FILE = os.path.join(os.getcwd(), '.optimizer_pause.flag')
STOP_FILE  = os.path.join(os.getcwd(), '.optimizer_stop.flag')
EMERGENCY_FILE = os.path.join(os.getcwd(), '.optimizer_emergency.flag')

# 当前正在运行的全量优化器实例（紧急停止时用于立即保存断点）
_ACTIVE_HEAVY = None

def _check_control():
    """检查暂停/停止/紧急停止信号。返回 'stop' 或 None。暂停时阻塞等待。"""
    if os.path.exists(STOP_FILE) or os.path.exists(EMERGENCY_FILE):
        return 'stop'
    while os.path.exists(PAUSE_FILE):
        if os.path.exists(STOP_FILE) or os.path.exists(EMERGENCY_FILE):
            return 'stop'
        time.sleep(0.3)
    return None

def request_pause():
    """请求暂停（GUI调用）"""
    with open(PAUSE_FILE, 'w') as f:
        f.write(str(time.time()))
    if DASHBOARD_STATE:
        DASHBOARD_STATE.add_event('⏸ 已请求暂停，等待当前计算完成...')

def request_resume():
    """请求继续（GUI调用）"""
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    if DASHBOARD_STATE:
        DASHBOARD_STATE.add_event('▶ 已请求继续运行')

def request_stop():
    """请求优雅停止（GUI调用）"""
    with open(STOP_FILE, 'w') as f:
        f.write(str(time.time()))
    if DASHBOARD_STATE:
        DASHBOARD_STATE.add_event('⏹ 已请求优雅停止，等待当前计算完成...')

def request_emergency_stop():
    """紧急停止（GUI 紧急停止按钮）：立即保存当前断点并强行终止所有进程。

    由后台线程调用（断点保存可能耗时，避免卡住 Tk 主线程）：
    1. 立即保存全量优化断点（若 heavy 正在运行）；
    2. 对所有活动进程池 abort() —— 直接 terminate 全部 worker，
       放弃未完成任务（不等待任何任务/进程收尾）。
    """
    with open(EMERGENCY_FILE, 'w') as f:
        f.write(str(time.time()))
    saved = False
    if _ACTIVE_HEAVY is not None:
        try:
            _ACTIVE_HEAVY._save_checkpoint()
            saved = True
        except Exception as e:
            if DASHBOARD_STATE:
                DASHBOARD_STATE.add_event(f'[紧急停止] 断点保存失败: {e}')
    killed = 0
    for _pool in live_pools():
        try:
            killed += _pool.abort()
        except Exception:
            pass
    if DASHBOARD_STATE:
        DASHBOARD_STATE.add_event(
            f'🛑 紧急停止：断点已保存，已强制终止 {killed} 个进程' if saved
            else f'🛑 紧急停止：已强制终止 {killed} 个进程')

def request_emergency_trim():
    """紧急执行（GUI 紧急执行按钮）：立即杀死超出目标进程数的进程。

    目标进程数 = 各活动池调度器最近一次真实决策意图（_desired_target，
    即 GUI"目标进程数"显示值同源）；超出部分直接 terminate，
    其正在运行的任务数据被放弃（不等待结果）。
    """
    pools = live_pools()
    if not pools:
        if DASHBOARD_STATE:
            DASHBOARD_STATE.add_event('⚡ 紧急执行：无活动进程池，跳过')
        return 0
    targets = []
    for _pool in pools:
        try:
            _t = getattr(_pool, '_desired_target', None)
        except Exception:
            _t = None
        if _t and int(_t) >= 1:
            targets.append(int(_t))
    if not targets:
        if DASHBOARD_STATE:
            DASHBOARD_STATE.add_event('⚡ 紧急执行：目标进程数不可用，跳过')
        return 0
    target = max(targets)   # 多池取最大意图（保守：不误杀目标内的进程）
    killed = 0
    for _pool in pools:
        try:
            killed += _pool.kill_excess(target)
        except Exception:
            pass
    if DASHBOARD_STATE:
        DASHBOARD_STATE.add_event(
            f'⚡ 紧急执行：已立即终止 {killed} 个进程（目标 {target}）')
    return killed

def clear_control_flags():
    """清理所有信号文件（启动/结束时调用）"""
    for f in [PAUSE_FILE, STOP_FILE, EMERGENCY_FILE]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

def _report_progress(msg):
    """上报进度到 GUI 日志"""
    if DASHBOARD_STATE:
        DASHBOARD_STATE.add_event(msg)
        DASHBOARD_STATE.add_log(msg)


def _update_progress(phase=None, label=None, current=None, total=None,
                     pct=None, detail=None):
    """上报结构化计算进度到 GUI 独立进度面板（与终端打印完全解耦）。

    进度不再走 print/日志，而是写入 DashboardState.progress 字段，
    由 GUI 右侧"计算进度"面板渲染（阶段 + 进度条 + 明细）。
    """
    if DASHBOARD_STATE is not None:
        try:
            DASHBOARD_STATE.set_progress(phase=phase, label=label,
                                         current=current, total=total,
                                         pct=pct, detail=detail)
        except Exception:
            pass


def _wait_future(fut, timeout=0.5):
    """轮询等待 future 完成，同时响应暂停/停止/紧急停止信号。

    修复"点击暂停后再点停止停不下来"：原来各循环直接 `fut.result()`
    阻塞（可能数分钟），暂停/停止信号要等当前任务完成后才能生效；
    改为 timeout 轮询 + `_check_control()`，信号到达后最多 timeout 秒内
    返回并令调用方 break。

    返回 (result, stopped)：stopped=True 表示收到停止信号且任务未完成
    （调用方应放弃剩余任务）。兼容 PoolFuture（超时返回 None 不抛异常）
    与 concurrent.futures.Future（超时抛 TimeoutError）。
    """
    while not fut.done():
        if _check_control() == 'stop':
            return None, True
        try:
            fut.result(timeout=timeout)
        except TimeoutError:
            continue            # concurrent.futures：超时 → 继续轮询
        except Exception:
            break               # 任务失败 → 结束等待
    try:
        return fut.result(), False
    except Exception:
        return None, False


class _TeeWriter:
    """终端输出分流器：把 print / tqdm 进度条等写入内容同时转发到
    GUI 终端打印区（DASHBOARD_STATE.add_log），原终端照常显示。

    - 按 \n 与 \r 切行：进度条（用 \r 刷新）的每一次状态都保留为独立
      日志行（"全部保留"语义），而非覆盖同一条。
    - 线程安全：后台优化线程与 Tk 主线程可并发写。
    - 真实流写入失败（如 UnicodeEncodeError 管道编码限制）不阻断日志，
      该行仍会进入 GUI 终端区。
    """

    def __init__(self, real, sink):
        self._real = real          # 原始 stdout/stderr（可为 None）
        self._sink = sink          # callable(str) 每行转发目标
        self._buf = ''
        self._lock = threading.Lock()

    def write(self, s):
        if not s:
            return
        if isinstance(s, bytes):
            try:
                enc = getattr(self._real, 'encoding', None) or 'utf-8'
                s = s.decode(enc, 'replace')
            except Exception:
                s = s.decode('utf-8', 'replace')
        with self._lock:
            if self._real is not None:
                try:
                    self._real.write(s)
                except (UnicodeEncodeError, ValueError, OSError):
                    pass
            self._buf += s
            while True:
                i_n = self._buf.find('\n')
                i_r = self._buf.find('\r')
                if i_n < 0 and i_r < 0:
                    break
                i = i_n if i_n >= 0 and (i_r < 0 or i_n < i_r) else i_r
                line = self._buf[:i]
                self._buf = self._buf[i + 1:]
                if line:
                    self._emit(line)
            # 无换行的超长流：强制切行，防缓冲无限膨胀
            if len(self._buf) > 4096:
                self._emit(self._buf)
                self._buf = ''

    def _emit(self, line):
        line = line.rstrip()
        if line:
            try:
                self._sink(line)
            except Exception:
                pass

    def flush(self):
        # 把未换行的残段也转发（进度条末尾无 \n 时状态不丢失）
        with self._lock:
            if self._buf:
                self._emit(self._buf)
                self._buf = ''
        if self._real is not None:
            try:
                self._real.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):
        if self._real is not None:
            return self._real.fileno()
        raise OSError('fileno not available')

    def __getattr__(self, name):
        return getattr(self._real, name)


_ORIG_STDOUT = None
_ORIG_STDERR = None


def install_stdout_tee():
    """把 sys.stdout / sys.stderr 替换为分流器（终端照常显示 + 逐行转发
    到 GUI 终端打印区）。重复调用无副作用。"""
    global _ORIG_STDOUT, _ORIG_STDERR
    if _ORIG_STDOUT is not None and _ORIG_STDERR is not None:
        return
    st = DASHBOARD_STATE
    if st is None:
        return

    def _sink(line):
        st.add_log(line)

    if _ORIG_STDOUT is None and sys.stdout is not None:
        _ORIG_STDOUT = sys.stdout
        try:
            sys.stdout = _TeeWriter(_ORIG_STDOUT, _sink)
        except Exception:
            _ORIG_STDOUT = None
    if _ORIG_STDERR is None and sys.stderr is not None:
        _ORIG_STDERR = sys.stderr
        try:
            sys.stderr = _TeeWriter(_ORIG_STDERR, _sink)
        except Exception:
            _ORIG_STDERR = None


def restore_stdout():
    """恢复原始 sys.stdout / sys.stderr（GUI 关闭时调用）。"""
    global _ORIG_STDOUT, _ORIG_STDERR
    if _ORIG_STDOUT is not None:
        try:
            _ORIG_STDOUT.flush()
        except Exception:
            pass
        sys.stdout = _ORIG_STDOUT
        _ORIG_STDOUT = None
    if _ORIG_STDERR is not None:
        try:
            _ORIG_STDERR.flush()
        except Exception:
            pass
        sys.stderr = _ORIG_STDERR
        _ORIG_STDERR = None


def enable_adaptive_control(cpu_limit=100.0):
    """启用自适应进程调度（供 --gui 模式调用）。

    - cpu_limit: 最大 CPU 使用率（1~100，100 = 最大性能模式）
    """
    global ADAPTIVE_ENABLED, GOVERNOR, DASHBOARD_STATE
    if not HAS_ADAPTIVE:
        print(f"[警告] 自适应模块导入失败: {ADAPTIVE_IMPORT_ERR}")
        print("       已回退到固定进程数模式（--jobs）")
        return False
    GOVERNOR = CpuGovernor(limit_pct=cpu_limit)
    DASHBOARD_STATE = DashboardState()
    DASHBOARD_STATE.update(limit=GOVERNOR.limit_pct, mode=GOVERNOR.mode_label())
    ADAPTIVE_ENABLED = True
    return True


def run_with_gui(cpu_limit=100.0, shutdown_cb=None):
    """以控制面板 GUI 方式运行：面板运行在 Tk 主线程，优化在后台线程。

    面板打开后由用户选择"运算模式 / 试验次数 / 结果保存路径"，
    点击"开始优化"后才启动后台线程（计算逻辑来自 optimizer_modes.build_run）。

    Args:
        cpu_limit: 初始最大 CPU 使用率（1~100，100 = 最大性能模式）
        shutdown_cb: 优化完成后回调（无人值守关机等），可为 None

    返回 True 表示已以 GUI 方式接管主流程（调用方应结束）。
    """
    if not HAS_ADAPTIVE or not enable_adaptive_control(cpu_limit=cpu_limit):
        print("[警告] 控制面板不可用，回退到命令行模式")
        return False
    try:
        from optimizer_gui import OptimizerGUI
    except Exception as e:
        print(f"[警告] 控制面板加载失败: {e}，回退到命令行模式")
        return False
    print(f"  资源控制面板已启动（程序自身 CPU 占用上限 {cpu_limit:.0f}%"
          f"{'，最大性能模式' if GOVERNOR.is_max_mode else ''}）")
    print(f"  在面板中选择运算模式并点击【开始优化】；关闭面板窗口将结束程序。")
    # 终端分流：计算期间的 print / tqdm 进度条等输出，逐行转发到
    # GUI 右侧"终端打印区"（原终端照常显示，关闭面板后恢复）。
    install_stdout_tee()
    try:
        app = OptimizerGUI(GOVERNOR, DASHBOARD_STATE, shutdown_cb=shutdown_cb)
        app.run()
    finally:
        restore_stdout()
    return True

# ============================================================
# 核心数据结构
# ============================================================

@dataclass
class TrialResult:
    """单次试验结果"""
    params: Dict
    strategy_return: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    sortino_ratio: float = 0.0
    annualized_return: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    profit_factor: float = 0.0
    avg_hold_days: float = 0.0
    volatility: float = 0.0
    objective: float = 0.0  # 复合目标函数值
    eval_time: float = 0.0   # 评估耗时（秒）

    @classmethod
    def from_backtest(cls, params, results, eval_time):
        return cls(
            params=params,
            strategy_return=results.get('strategy_return', 0),
            benchmark_return=results.get('benchmark_return', 0),
            excess_return=results.get('excess_return', 0),
            sharpe_ratio=results.get('sharpe_ratio', 0),
            max_drawdown=results.get('max_drawdown', 0),
            calmar_ratio=results.get('calmar_ratio', 0),
            sortino_ratio=results.get('sortino_ratio', 0),
            annualized_return=results.get('annualized_return', 0),
            win_rate=results.get('win_rate', 0),
            total_trades=results.get('total_trades', 0),
            profit_factor=results.get('profit_factor', 0),
            avg_hold_days=results.get('avg_hold_days', 0),
            volatility=results.get('volatility', 0),
            eval_time=eval_time,
        )


# ============================================================
# 扩展搜索空间（关键升级！）
# ============================================================
# 搜索空间分为三组，覆盖影响收益率的全部关键参数

OPTIMIZER_SEARCH_SPACE = {
    # ─── A组：评分公式权重（影响买卖信号的数值）──────────────────
    #        这四个权重是"如何把检测到的行为转化为买卖信号"的核心
    'SCORE_BEHAVIOR_WEIGHT': {'type': 'float', 'range': (0.15, 0.45), 'step': None, 'group': 'weights'},
    'SCORE_CONFIDENCE_WEIGHT': {'type': 'float', 'range': (0.05, 0.30), 'step': None, 'group': 'weights'},
    'SCORE_REWARD_WEIGHT': {'type': 'float', 'range': (0.25, 0.55), 'step': None, 'group': 'weights'},
    'SCORE_RISK_WEIGHT': {'type': 'float', 'range': (0.05, 0.25), 'step': None, 'group': 'weights'},

    # ─── B组：市场状态权重乘数（影响不同市场下的买卖力度）──────────
    'BULL_BUY_MULT': {'type': 'float', 'range': (1.20, 2.50), 'step': None, 'group': 'regime'},
    'BULL_SELL_DIV': {'type': 'float', 'range': (0.05, 0.30), 'step': None, 'group': 'regime'},
    'RANGE_BUY_MULT': {'type': 'float', 'range': (0.30, 0.90), 'step': None, 'group': 'regime'},
    'RANGE_SELL_DIV': {'type': 'float', 'range': (0.60, 1.30), 'step': None, 'group': 'regime'},
    'BEAR_BUY_MULT': {'type': 'float', 'range': (0.60, 1.30), 'step': None, 'group': 'regime'},
    'BEAR_SELL_DIV': {'type': 'float', 'range': (0.80, 1.40), 'step': None, 'group': 'regime'},

    # ─── C组：交易执行参数（影响持仓和交易频率）────────────────────
    'CONFIRMATION_THRESHOLD': {'type': 'int', 'range': (55, 80), 'step': 5, 'group': 'exec'},
    'CONFIDENCE_INCREMENT': {'type': 'int', 'range': (3, 15), 'step': 1, 'group': 'exec'},
    'OBSERVATION_WINDOW_MAX': {'type': 'int', 'range': (2, 8), 'step': 1, 'group': 'exec'},
    'EXPIRY_THRESHOLD': {'type': 'int', 'range': (10, 35), 'step': 5, 'group': 'exec'},
    'MIN_HOLD_DAYS': {'type': 'int', 'range': (3, 35), 'step': 1, 'group': 'exec'},
    'SCORE_HOLD_ZONE': {'type': 'int', 'range': (8, 30), 'step': 1, 'group': 'exec'},
    'TRADE_TARGET_DELTA': {'type': 'float', 'range': (0.01, 0.08), 'step': 0.005, 'group': 'exec'},
    'TRADE_ACTUAL_DELTA': {'type': 'float', 'range': (0.01, 0.08), 'step': 0.005, 'group': 'exec'},
    'MAX_POSITION': {'type': 'float', 'range': (0.70, 0.98), 'step': 0.01, 'group': 'exec'},
    'INITIAL_POSITION': {'type': 'float', 'range': (0.70, 0.98), 'step': 0.01, 'group': 'exec'},

    # ─── D组：行为检测阈值（从原 GRID_SEARCH_PARAMS 精选）─────────
    'DOUBLE_BOTTOM_REBOUND_MIN': {'type': 'float', 'range': (0.005, 0.030), 'step': 0.002, 'group': 'behavior'},
    'DOUBLE_BOTTOM_SCORE': {'type': 'int', 'range': (30, 60), 'step': 5, 'group': 'behavior'},
    'FALSE_BREAK_BREAK_DIST': {'type': 'float', 'range': (0.005, 0.015), 'step': 0.001, 'group': 'behavior'},
    'FALSE_BREAK_SCORE': {'type': 'int', 'range': (25, 50), 'step': 5, 'group': 'behavior'},
    'MOMO_EXH_RETURN_THRESHOLD': {'type': 'float', 'range': (0.015, 0.040), 'step': 0.002, 'group': 'behavior'},
    'MOMO_EXH_ACCEL_DECLINE': {'type': 'float', 'range': (0.35, 0.90), 'step': 0.05, 'group': 'behavior'},
    'MOMO_EXH_SCORE': {'type': 'int', 'range': (35, 65), 'step': 5, 'group': 'behavior'},
    'PULLBACK_MA_DIST': {'type': 'float', 'range': (0.010, 0.035), 'step': 0.002, 'group': 'behavior'},
    'PULLBACK_SCORE': {'type': 'int', 'range': (20, 45), 'step': 5, 'group': 'behavior'},
    'PANIC_SELL_DROP_THRESHOLD': {'type': 'float', 'range': (-0.060, -0.020), 'step': 0.002, 'group': 'behavior'},
    'PANIC_SELL_SCORE': {'type': 'int', 'range': (30, 60), 'step': 5, 'group': 'behavior'},
    'TREND_FAIL_MA_SLOPE_NEG': {'type': 'float', 'range': (-0.0020, -0.0002), 'step': 0.0002, 'group': 'behavior'},
    'TREND_FAIL_SCORE': {'type': 'int', 'range': (35, 65), 'step': 5, 'group': 'behavior'},
    'BREAKOUT_CONFIRM_DAYS': {'type': 'int', 'range': (1, 5), 'step': 1, 'group': 'behavior'},
    'BREAKOUT_SCORE': {'type': 'int', 'range': (25, 50), 'step': 5, 'group': 'behavior'},
    'RSI_OVERBOUGHT_THRESHOLD': {'type': 'int', 'range': (60, 78), 'step': 2, 'group': 'behavior'},
    'RSI_OVERBOUGHT_SCORE': {'type': 'int', 'range': (15, 40), 'step': 5, 'group': 'behavior'},
}


# ============================================================
# 复合目标函数
# ============================================================

def composite_objective(
    strategy_return: float,
    sharpe_ratio: float,
    max_drawdown: float,
    calmar_ratio: float,
    sortino_ratio: float = 0,
    win_rate: float = 0,
    total_trades: int = 0,
    annualized_return: float = 0,
    excess_return: float = 0,
    profit_factor: float = 0,
    volatility: float = 0,
) -> float:
    """
    多目标复合评分函数

    设计理念：
    - 收益率是核心，但要有夏普和回撤的约束
    - 年化收益和超额收益提供额外信号
    - 过多交易次数有惩罚
    - 波动率过高有惩罚

    Returns:
        得分越高越好（可正可负）
    """
    score = 0.0

    # 核心：年化收益率（最直接的收益指标）
    if annualized_return != 0:
        score += annualized_return * 100  # e.g. 15% → +15
    elif strategy_return != 0:
        score += strategy_return * 80

    # 超额收益（相对benchmark的价值）
    if excess_return > 0:
        score += excess_return * 60
    else:
        score += excess_return * 40  # 负超额也惩罚

    # 夏普比率（风险调整后收益）
    if sharpe_ratio > 0:
        score += min(sharpe_ratio * 8, 15)  # cap at +15
    else:
        score += sharpe_ratio * 10

    # Sortino 比率（下行风险调整，比夏普更精准）
    if sortino_ratio > 0:
        score += min(sortino_ratio * 3, 10)

    # Calmar 比率（收益/最大回撤）
    if calmar_ratio > 0:
        score += min(calmar_ratio * 10, 12)

    # 最大回撤惩罚（回撤越小越好）
    if max_drawdown < 0:
        dd_pct = abs(max_drawdown)
        if dd_pct < 0.05:
            score += 8
        elif dd_pct < 0.10:
            score += 3
        elif dd_pct < 0.15:
            score += 0
        else:
            score += (0.05 - dd_pct) * 60  # >15%惩罚

    # 胜率微调
    if win_rate > 0.55:
        score += (win_rate - 0.50) * 10

    # 盈亏比
    if profit_factor > 1.5:
        score += min(profit_factor - 1.0, 5)

    # 交易次数惩罚（太少不合适，太多怀疑过度交易）
    if 5 <= total_trades <= 60:
        score += 2
    elif total_trades > 60:
        score -= min((total_trades - 60) * 0.15, 5)
    elif total_trades < 5 and total_trades > 0:
        score -= (5 - total_trades) * 1.5

    # 波动率惩罚
    if volatility > 0.30:
        score -= (volatility - 0.30) * 30

    return score


# ============================================================
# Worker 函数（子进程评估）
# ============================================================

def _single_eval_worker(args_tuple):
    """
    独立 worker 函数 —— 在子进程中运行单次策略回测

    模块级别的导入 + pickle 数据缓存，避免每 trial 重复 import/反序列化。
    """
    params, start_date = args_tuple

    try:
        for param_name, value in params.items():
            if hasattr(config, param_name):
                setattr(config, param_name, value)

        if 'BULL_BUY_MULT' in params:
            config.REGIME_WEIGHTS = _build_regime_from_params(params)

        # ⚠️ 关键修复：重载导入时捕获配置的模块，使 setattr 的新参数生效
        strategy_mod, backtest_mod = _reload_config_capture_modules()

        # 从 pickle 缓存加载 DataFrame（首次加载后永久缓存）
        df, _ = _load_data_cache()

        strategy = strategy_mod.V6Strategy()
        signals = strategy.run(df)
        bt = backtest_mod.V6Backtest(df, start_date=start_date)
        bt_results = bt.run(signals)

        if not bt_results:
            return None

        result = TrialResult.from_backtest(params, bt_results, -1.0)
        result.objective = composite_objective(**{
            'strategy_return': result.strategy_return,
            'sharpe_ratio': result.sharpe_ratio,
            'max_drawdown': result.max_drawdown,
            'calmar_ratio': result.calmar_ratio,
            'sortino_ratio': result.sortino_ratio,
            'win_rate': result.win_rate,
            'total_trades': result.total_trades,
            'annualized_return': result.annualized_return,
            'excess_return': result.excess_return,
            'profit_factor': result.profit_factor,
            'volatility': result.volatility,
        })
        return result

    except Exception:
        return None


# ============================================================
# 基础优化器抽象类
# ============================================================

class BaseOptimizer(ABC):
    """所有优化器的基类"""

    def __init__(self, df, start_date=None, resume=True):
        self.df = df
        self.start_date = start_date if start_date is not None else str(df.index[0].date())
        self.resume = resume
        self.results: List[TrialResult] = []
        self._df_json = None
        self._original_config = {}
        self._original_regime = None
        self._checkpoint_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '.optimizer_checkpoint.json'
        )

    def _save_config(self):
        for attr in dir(config):
            if not attr.startswith('_') and attr.isupper():
                try:
                    self._original_config[attr] = copy.deepcopy(getattr(config, attr))
                except:
                    self._original_config[attr] = getattr(config, attr)
        self._original_regime = copy.deepcopy(getattr(config, 'REGIME_WEIGHTS', {}))

    def _restore_config(self):
        for attr, val in self._original_config.items():
            try:
                setattr(config, attr, val)
            except:
                pass

    def _prepare_df_json(self):
        """准备数据缓存（子进程首次调用时自动加载，不重复计算）"""
        _prepare_data_cache(self.df)
        return None  # 不再需要主进程传递 JSON，子进程统一从 pickle 加载

    def _build_regime_weights(self, params):
        """从 flatten 参数构建嵌套 REGIME_WEIGHTS dict"""
        weights = copy.deepcopy(self._original_regime)
        mapping = {
            'BULL_BUY_MULT': ('Bull', 'buy_mult'),
            'BULL_SELL_DIV': ('Bull', 'sell_div'),
            'RANGE_BUY_MULT': ('Range', 'buy_mult'),
            'RANGE_SELL_DIV': ('Range', 'sell_div'),
            'BEAR_BUY_MULT': ('Bear', 'buy_mult'),
            'BEAR_SELL_DIV': ('Bear', 'sell_div'),
        }
        for param_key, (regime_key, weight_key) in mapping.items():
            if param_key in params and regime_key in weights:
                weights[regime_key][weight_key] = params[param_key]
        return weights

    def _build_task(self, params):
        """构建单个评估任务（使用 pickle 缓存，无需传递 DataFrame）"""
        return (copy.deepcopy(params), self.start_date)

    def _eval_with_pool(self, pool, tasks, results, verbose=True, progress_cb=None):
        """在给定的自适应池中评估任务（支持跨调用复用同一池）。

        池复用避免"每批任务重建进程池"导致的反复 spawn 峰值内存
        与 worker 重复加载数据（_load_data_cache 只在 worker 首次使用时
        加载一次，复用池后各代评估零重复加载）。
        progress_cb: 可选回调 fn(done, total)，每完成一个任务调用一次。
        """
        futures = [pool.submit(t) for t in tasks]
        _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
        pbar = (tqdm(total=len(futures), desc="Evaluating (自适应)",
                     unit="trial") if _use_tqdm else None)
        done = 0
        for f in futures:
            # ── 暂停/停止信号检查（轮询式，信号到达后立即响应）──
            r, _stopped = _wait_future(f)
            if _stopped:
                # 立即终止池进程：避免 with 退出时 shutdown(wait=True)
                # 长时间等待已提交任务完成（"停不下来"的直接原因）
                try:
                    pool.abort()
                except Exception:
                    pass
                break
            if r is not None:
                results.append(r)
            done += 1
            if pbar is not None:
                pbar.update(1)
            if progress_cb is not None:
                progress_cb(done, len(futures))
        if pbar is not None:
            pbar.close()

    def _run_evaluations(self, tasks, n_jobs=-1, verbose=True, pool=None,
                         progress_cb=None):
        """并行运行评估任务

        progress_cb: 可选回调 fn(done, total)，每完成一个任务调用一次。
        """
        if n_jobs < 0:
            n_jobs = os.cpu_count() or 4
        n_jobs = min(n_jobs, len(tasks))

        results = []

        if n_jobs == 1 or len(tasks) <= 1:
            iterator = enumerate(tasks, 1)
            _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
            if _use_tqdm:
                iterator = tqdm(iterator, total=len(tasks), desc="Evaluating", unit="trial")
            for idx, task in iterator:
                # ── 暂停/停止信号检查 ──
                if _check_control() == 'stop':
                    break
                result = _single_eval_worker(task)
                if result is not None:
                    results.append(result)
                if progress_cb is not None:
                    progress_cb(idx, len(tasks))
        elif ADAPTIVE_ENABLED and HAS_ADAPTIVE:
            # ── 自适应进程池（可视化控制面板模式）──
            # worker 数量由 CpuGovernor 实时增减，确保程序自身 CPU 占用
            # 不超过面板设定的资源限制。pool 传入时复用（GA 多代共享），
            # 否则新建。
            if pool is not None:
                self._eval_with_pool(pool, tasks, results, verbose, progress_cb)
            else:
                with AdaptiveWorkerPool(_single_eval_worker, governor=GOVERNOR,
                                        state=DASHBOARD_STATE, pool_name='Eval') as _pool:
                    self._eval_with_pool(_pool, tasks, results, verbose, progress_cb)
        else:
            ctx = multiprocessing.get_context('spawn')
            with ProcessPoolExecutor(max_workers=n_jobs, mp_context=ctx) as executor:
                futures = {executor.submit(_single_eval_worker, t): t for t in tasks}
                _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
                pbar = (tqdm(total=len(futures), desc="Evaluating",
                             unit="trial") if _use_tqdm else None)
                done = 0
                remaining = set(futures)
                while remaining:
                    # ── 暂停/停止信号检查（as_completed 带超时轮询）──
                    if _check_control() == 'stop':
                        break
                    try:
                        for future in as_completed(list(remaining), timeout=0.5):
                            remaining.discard(future)
                            try:
                                result = future.result()
                                if result is not None:
                                    results.append(result)
                            except:
                                pass
                            done += 1
                            if pbar is not None:
                                pbar.update(1)
                            if progress_cb is not None:
                                progress_cb(done, len(futures))
                    except TimeoutError:
                        continue
                if pbar is not None:
                    pbar.close()

        return results

    def print_summary(self, top_n=15):
        results = sorted(self.results, key=lambda x: x.objective, reverse=True)
        if not results:
            print("无有效结果")
            return

        print(f"\n{'='*100}")
        print(f"  TOP {top_n} 参数组合（按复合目标函数排序）")
        print(f"{'='*100}")
        header = (f"{'#':>3} {'目标分':>8} {'收益':>8} {'超额':>7} {'夏普':>6} "
                  f"{'回撤':>7} {'Calmar':>7} {'胜率':>6} {'交易':>5} | 关键参数")
        print(header)
        print("-" * 100)

        for i, r in enumerate(results[:top_n]):
            # 提取关键参数
            key_params = []
            for k in ['SCORE_REWARD_WEIGHT', 'BULL_BUY_MULT', 'BULL_SELL_DIV',
                       'CONFIRMATION_THRESHOLD', 'MIN_HOLD_DAYS', 'MAX_POSITION']:
                if k in r.params:
                    key_params.append(f"{k.split('_')[-1]}={r.params[k]}")
            param_str = ', '.join(key_params[:5])

            line = (f"{i+1:>3} {r.objective:>8.2f} "
                    f"{r.strategy_return*100:>7.2f}% "
                    f"{r.excess_return*100:>6.2f}% "
                    f"{r.sharpe_ratio:>6.3f} "
                    f"{r.max_drawdown*100:>6.2f}% "
                    f"{r.calmar_ratio:>7.3f} "
                    f"{r.win_rate*100:>5.1f}% "
                    f"{r.total_trades:>5} | {param_str}")
            print(line)

        # 统计
        objectives = [r.objective for r in results]
        returns = [r.strategy_return for r in results]
        print(f"\n--- 搜索统计 ---")
        print(f"有效组合数: {len(results)}")
        print(f"目标分范围: {min(objectives):.2f} ~ {max(objectives):.2f}")
        print(f"收益率范围: {min(returns)*100:.2f}% ~ {max(returns)*100:.2f}%")
        print(f"复合目标均值: {np.mean(objectives):.2f}")

    def get_best_result(self):
        if not self.results:
            return None
        return max(self.results, key=lambda x: x.objective)

    def apply_best_params(self, dry_run=False):
        """将最优参数写回 config.py"""
        import re

        best = self.get_best_result()
        if best is None:
            print("无有效结果，跳过")
            return None

        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
        if not os.path.exists(config_path):
            print(f"config.py 未找到: {config_path}")
            return None

        with open(config_path, 'r', encoding='utf-8') as f:
            source = f.read()

        updated = {}
        # 特殊键映射
        regime_key_map = {
            'BULL_BUY_MULT': ('REGIME_WEIGHTS', 'Bull', 'buy_mult'),
            'BULL_SELL_DIV': ('REGIME_WEIGHTS', 'Bull', 'sell_div'),
            'RANGE_BUY_MULT': ('REGIME_WEIGHTS', 'Range', 'buy_mult'),
            'RANGE_SELL_DIV': ('REGIME_WEIGHTS', 'Range', 'sell_div'),
            'BEAR_BUY_MULT': ('REGIME_WEIGHTS', 'Bear', 'buy_mult'),
            'BEAR_SELL_DIV': ('REGIME_WEIGHTS', 'Bear', 'sell_div'),
        }

        for param_name, new_value in best.params.items():
            if param_name in regime_key_map:
                # 稍后处理 REGIME_WEIGHTS
                continue
            if not hasattr(config, param_name):
                continue
            old_val = getattr(config, param_name)
            pattern = rf'^({param_name}\s*=\s*)([^\n#]+)(.*)$'
            match = re.search(pattern, source, re.MULTILINE)
            if not match:
                continue

            if isinstance(new_value, float):
                new_str = str(round(new_value, 4))
            elif isinstance(new_value, int):
                new_str = str(new_value)
            else:
                new_str = repr(new_value)

            source = source[:match.start()] + f'{param_name} = {new_str}{match.group(3)}' + source[match.end():]
            updated[param_name] = {'old': old_val, 'new': new_value}

        # 处理 REGIME_WEIGHTS
        for param_key, (section, regime, weight) in regime_key_map.items():
            new_val = best.params.get(param_key)
            if new_val is None:
                continue
            old_val = getattr(config, 'REGIME_WEIGHTS', {}).get(regime, {}).get(weight)
            # REGIME_WEIGHTS 在 config 中是 dict literal，行匹配复杂，这里只打日志
            print(f"  [REGIME] {regime}.{weight}: {old_val} → {new_val}（请手动更新 config.py）")

        if not updated:
            print("没有可自动更新的参数")
            return None

        print(f"\n{'='*60}")
        print(f"  应用最优参数到 config.py")
        print(f"{'='*60}")
        for name, vals in updated.items():
            print(f"  {name:<32} {vals['old']} → {vals['new']}")

        if dry_run:
            print(f"\n  [DRY RUN] 未实际写入")
            return updated

        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(source)
        print(f"\n  已写入 config.py ({len(updated)} 个参数)")
        print(f"{'='*60}\n")
        return updated

    @abstractmethod
    def run(self, **kwargs):
        """子类实现具体的优化流程"""
        pass


# ============================================================
# Optuna 目标函数（模块级别，确保可 pickle）
# ============================================================

# 模块级别的 REGIME 权重映射（可 pickle）
_REGIME_MAPPING = {
    'BULL_BUY_MULT': ('Bull', 'buy_mult'),
    'BULL_SELL_DIV': ('Bull', 'sell_div'),
    'RANGE_BUY_MULT': ('Range', 'buy_mult'),
    'RANGE_SELL_DIV': ('Range', 'sell_div'),
    'BEAR_BUY_MULT': ('Bear', 'buy_mult'),
    'BEAR_SELL_DIV': ('Bear', 'sell_div'),
}


def _build_regime_from_params(params):
    """从扁平参数构建 REGIME_WEIGHTS（模块级别，可 pickle）

    注意：不能从 config.REGIME_WEIGHTS deepcopy —— 优化过程中 config 模块可能
    已被前一轮 setattr 污染，必须使用固定默认结构再覆盖搜索参数。
    """
    import copy
    _orig_regime = {
        'Bull': {'buy_mult': 1.80, 'sell_div': 0.12},
        'Bear': {'buy_mult': 0.90, 'sell_div': 1.05},
        'Range': {'buy_mult': 0.55, 'sell_div': 0.85},
        'Unknown': {'buy_mult': 0.75, 'sell_div': 0.95},
    }
    weights = copy.deepcopy(_orig_regime)
    for param_key, (regime_key, weight_key) in _REGIME_MAPPING.items():
        if param_key in params and regime_key in weights:
            weights[regime_key][weight_key] = params[param_key]
    return weights


# ═══ 需要重载的模块：均通过 `from config import *` 在导入时捕获配置 ═══
# 优化器 worker 先 setattr(config, ...) 再执行回测，若这些模块不重载，
# 策略仍使用导入时的旧配置（这是 heavy 优化结果无法复现的根因）。
_RELOAD_CONFIG_CAPTURE_MODULES = [
    'indicators', 'regime_detector', 'behavior_detector', 'emotion_builder',
    'event_engine', 'evidence_engine', 'reward_risk', 'scoring_engine',
    'position_manager', 'replay_engine', 'crowd_psychology', 'strategy', 'backtest',
]


def _reload_config_capture_modules():
    """setattr(config, ...) 之后调用：强制重载所有导入时捕获配置的模块。

    返回 (strategy_module, backtest_module)，调用方必须使用返回的新模块
    （旧模块对象已被替换，原 import 句柄失效）。
    """
    import importlib
    import sys as _sys
    for mod_name in _RELOAD_CONFIG_CAPTURE_MODULES:
        if mod_name in _sys.modules:
            del _sys.modules[mod_name]
    import strategy as _strategy_mod
    import backtest as _backtest_mod
    return _strategy_mod, _backtest_mod


class _OptunaEvaluator:
    """包装评估逻辑，供 Optuna objective 使用（模块级别，可 pickle）"""
    def __init__(self, start_date, space):
        self.start_date = start_date
        self.space = space

    def __call__(self, trial):
        params = {}
        for name, spec in self.space.items():
            if spec['type'] in ('float',):
                params[name] = trial.suggest_float(
                    name, spec['range'][0], spec['range'][1],
                    step=spec.get('step') if spec.get('step') else None
                )
            elif spec['type'] == 'int':
                params[name] = trial.suggest_int(
                    name, spec['range'][0], spec['range'][1],
                    step=spec.get('step', 1)
                )

        import copy
        task = (copy.deepcopy(params), self.start_date)
        result = _single_eval_worker(task)
        if result is None:
            return float('-inf')

        # 存入 trial 的用户属性
        trial.set_user_attr('strategy_return', result.strategy_return)
        trial.set_user_attr('sharpe_ratio', result.sharpe_ratio)
        trial.set_user_attr('max_drawdown', result.max_drawdown)
        trial.set_user_attr('calmar_ratio', result.calmar_ratio)
        trial.set_user_attr('total_trades', result.total_trades)
        trial.set_user_attr('win_rate', result.win_rate)

        return result.objective


# ============================================================
# 方法1：贝叶斯优化（Optuna TPE）
# ============================================================

class BayesianOptimizer(BaseOptimizer):
    """
    贝叶斯优化器 —— 使用 Optuna 的 TPE (Tree-structured Parzen Estimator)

    优势：
    - 比网格搜索效率高 10-100 倍
    - 自动平衡探索（Exploration）和利用（Exploitation）
    - 500-1000 次试验可覆盖 30+ 维参数空间
    - 从历史试验中学习哪些参数区域更有希望
    """

    def __init__(self, df, start_date=None, resume=True):
        super().__init__(df, start_date, resume)
        if not HAS_OPTUNA:
            raise ImportError(
                "需要安装 optuna: pip install optuna\n"
                "如果无法安装，请使用 --method coarse2fine 或 --method genetic"
            )

    def run(self, n_trials=500, n_jobs=-1, verbose=True,
            space=None, timeout_seconds=None, **kwargs):
        """
        运行贝叶斯优化

        Args:
            n_trials: 试验次数（推荐 300-1000）
            n_jobs: 并行 jobs（Optuna 内部的 trial 并行度；注意每 trial 约 1-3 秒）
            verbose: 是否打印进度
            space: 自定义搜索空间，None 使用默认
            timeout_seconds: 超时（秒），None 不限时
        """
        if space is None:
            space = dict(OPTIMIZER_SEARCH_SPACE)

        cpu_count = os.cpu_count() or 4
        if n_jobs < 0:
            n_jobs = cpu_count

        print(f"\n{'='*60}")
        print(f"贝叶斯优化（Optuna TPE）")
        print(f"{'='*60}")
        print(f"参数数量: {len(space)}")
        print(f"试验次数: {n_trials}")
        print(f"并行 trial: {n_jobs}")
        print(f"{'='*60}\n")

        self._save_config()
        self._prepare_df_json()
        self.results = []

        # ─── 存储后端选择 ───
        # InMemoryStorage: 零 I/O，但不支持多进程和断点续算
        # JournalStorage: 支持多进程 + 断点续算，有文件 I/O 开销
        _journal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      '.optuna_journal.log')
        _use_in_memory = (n_jobs == 1) and (not self.resume or not os.path.exists(_journal_path))

        if _use_in_memory:
            storage = optuna.storages.InMemoryStorage()
            print(f"  存储: InMemoryStorage（n_jobs=1, 零 I/O）")
        else:
            storage = JournalStorage(JournalFileBackend(_journal_path))
            print(f"  存储: JournalStorage（{n_jobs}进程并行 + 断点续算）")

        sampler = optuna.samplers.TPESampler(
            seed=42,
            n_startup_trials=max(15, n_trials // 15),
        )

        study = optuna.create_study(
            study_name='bayesian_search',
            storage=storage,
            load_if_exists=not _use_in_memory,
            direction='maximize',
            sampler=sampler,
            pruner=optuna.pruners.MedianPruner(
                n_startup_trials=10,
                n_warmup_steps=5,
            ),
        )

        # 统计已有 trial，计算剩余
        existing_complete = sum(1 for t in study.trials
                                if t.state == optuna.trial.TrialState.COMPLETE)
        remaining = max(0, n_trials - existing_complete)

        if self.resume and existing_complete > 0:
            print(f"\n  断点续算: {existing_complete}/{n_trials} trials 已完成, 剩余 {remaining}")
        elif existing_complete > 0 and not self.resume:
            # 强制全新
            if not _use_in_memory and os.path.exists(_journal_path):
                os.remove(_journal_path)
                storage = JournalStorage(JournalFileBackend(_journal_path))
            study = optuna.create_study(
                study_name='bayesian_search',
                storage=storage,
                direction='maximize',
                sampler=sampler,
                pruner=optuna.pruners.MedianPruner(
                    n_startup_trials=10,
                    n_warmup_steps=5,
                ),
            )
            remaining = n_trials

        # 模块级 evaluator —— 确保 pickle 安全
        evaluator = _OptunaEvaluator(
            start_date=self.start_date,
            space=space,
        )

        # 执行优化
        if remaining > 0:
            study.optimize(
                evaluator,
                n_trials=remaining,
                n_jobs=n_jobs,
                timeout=timeout_seconds,
                show_progress_bar=HAS_TQDM and verbose,
            )

        # 收集结果
        for trial in study.trials:
            if trial.state == optuna.trial.TrialState.COMPLETE and trial.value is not None:
                params = {k: v for k, v in trial.params.items()}
                result = TrialResult(
                    params=params,
                    strategy_return=trial.user_attrs.get('strategy_return', 0),
                    sharpe_ratio=trial.user_attrs.get('sharpe_ratio', 0),
                    max_drawdown=trial.user_attrs.get('max_drawdown', 0),
                    calmar_ratio=trial.user_attrs.get('calmar_ratio', 0),
                    total_trades=trial.user_attrs.get('total_trades', 0),
                    win_rate=trial.user_attrs.get('win_rate', 0),
                    objective=trial.value,
                )
                self.results.append(result)

        self._restore_config()

        if verbose:
            print(f"\n--- 贝叶斯优化完成 ---")
            print(f"有效试验: {len(self.results)}/{n_trials}")
            print(f"最优目标值: {study.best_value:.2f}")
            print(f"最优参数:")
            for key, val in study.best_params.items():
                print(f"  {key}: {val}")

            # 参数重要性分析
            try:
                importance = optuna.importance.get_param_importances(study)
                if importance:
                    print(f"\n参数重要性 (Top 10):")
                    for name, imp in sorted(importance.items(), key=lambda x: -x[1])[:10]:
                        bar = '█' * int(imp * 40)
                        print(f"  {name:<40} {imp:.3f} {bar}")
            except:
                pass

        # Optuna journal 文件清理（数据已全部读取后删除，仅 JournalStorage 时需要）
        if not _use_in_memory and (len(self.results) >= n_trials or not self.resume):
            _journal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          '.optuna_journal.log')
            try:
                if os.path.exists(_journal_path):
                    os.remove(_journal_path)
            except:
                pass

        return self.results


# ============================================================
# 方法2：遗传算法（Genetic Algorithm）
# ============================================================

class GeneticOptimizer(BaseOptimizer):
    """
    遗传算法优化器

    适用场景：
    - 参数空间高度非线性
    - 存在多个局部最优
    - 需要跳出局部最优的全局搜索

    流程：
    1. 初始化种群（随机 N 个个体）
    2. 评估适应度（复合目标函数）
    3. 锦标赛选择 → 交叉 → 变异 → 精英保留
    4. 重复 G 代
    """

    def __init__(self, df, start_date=None, resume=True):
        super().__init__(df, start_date, resume)

    def _random_individual(self, space):
        """随机生成一个个体"""
        ind = {}
        for name, spec in space.items():
            lo, hi = spec['range']
            if spec['type'] == 'float':
                if spec.get('step'):
                    steps = int((hi - lo) / spec['step']) + 1
                    val = lo + random.randint(0, steps) * spec['step']
                else:
                    val = lo + random.random() * (hi - lo)
                val = round(val, 6)
            else:
                step = spec.get('step', 1)
                steps = (hi - lo) // step
                val = lo + random.randint(0, steps) * step
            ind[name] = val
        return ind

    def _crossover(self, parent1, parent2, space):
        """均匀交叉"""
        child = {}
        for key in parent1:
            if random.random() < 0.5:
                child[key] = parent1[key]
            else:
                child[key] = parent2[key]
        return child

    def _mutate(self, individual, space, mutation_rate=0.15):
        """高斯/离散变异"""
        for name, spec in space.items():
            if random.random() < mutation_rate:
                lo, hi = spec['range']
                if spec['type'] == 'float':
                    # 高斯扰动
                    sigma = (hi - lo) * 0.1
                    new_val = individual[name] + random.gauss(0, sigma)
                    new_val = max(lo, min(hi, new_val))
                    if spec.get('step'):
                        new_val = round(new_val / spec['step']) * spec['step']
                    individual[name] = round(new_val, 6)
                else:
                    step = spec.get('step', 1)
                    delta = random.choice([-2, -1, 1, 2]) * step
                    new_val = individual[name] + delta
                    new_val = max(lo, min(hi, new_val))
                    individual[name] = int(new_val)
        return individual

    def _tournament_select(self, population, fitness, tournament_size=3):
        """锦标赛选择"""
        selected = []
        pop_size = len(population)
        for _ in range(2):  # 选两个父代
            candidates = random.sample(range(pop_size), tournament_size)
            winner = max(candidates, key=lambda i: fitness[i])
            selected.append(population[winner])
        return selected

    def run(self, space=None, population_size=40, generations=50,
            elite_count=4, mutation_rate=0.15, crossover_rate=0.80,
            n_jobs=-1, verbose=True, val_eval_fn=None,
            val_stagnant_limit=5, val_tolerance=1.0, **kwargs):
        """
        运行遗传算法优化

        Args:
            population_size: 种群大小
            generations: 进化代数
            elite_count: 精英保留数量
            mutation_rate: 变异概率
            crossover_rate: 交叉概率
            n_jobs: 并行进程数
            verbose: 是否打印进度
            val_eval_fn: 可选验证段评估函数 fn(params) -> float objective
                         （用于保留验证段早停；None = 不启用）
            val_stagnant_limit: 验证段无改善的连续代数上限（达到则提前停止）
            val_tolerance: 判定"改善"的最小验证分提升量
        """
        if space is None:
            space = dict(OPTIMIZER_SEARCH_SPACE)

        total_evals = population_size + (population_size - elite_count) * generations

        print(f"\n{'='*60}")
        print(f"遗传算法优化（Genetic Algorithm）")
        print(f"{'='*60}")
        print(f"参数数量: {len(space)}")
        print(f"种群大小: {population_size} | 进化代数: {generations}")
        print(f"预估总评估: ~{total_evals}")
        print(f"并行进程: {n_jobs if n_jobs > 0 else min(os.cpu_count() or 4, 6)}")
        print(f"{'='*60}\n")

        self._save_config()
        df_json = self._prepare_df_json()
        self.results = []

        random.seed(42)

        # 初始化种群
        if verbose:
            print("初始化种群...")
        population = [self._random_individual(space) for _ in range(population_size)]

        best_overall = None
        best_overall_obj = float('-inf')
        best_val_obj = float('-inf')
        val_stagnant = 0

        # ── 自适应模式：单个跨代复用的进程池 ──
        # 若每代重建进程池，Windows spawn 会反复触发"导入重库 + 加载数据"的
        # 内存峰值（每个 worker 数百 MB），在页面文件较小时直接 MemoryError
        # 崩溃（Error.txt 中 OpenBLAS/numpy 分配失败的根因）。复用池后：
        #   - spawn 只在池创建时发生一次；
        #   - worker 的 _load_data_cache 只在首次任务时加载一次，后续代复用。
        _pool = None
        _pool_owner = None
        if ADAPTIVE_ENABLED and HAS_ADAPTIVE:
            _pool_owner = AdaptiveWorkerPool(_single_eval_worker, governor=GOVERNOR,
                                             state=DASHBOARD_STATE, pool_name='GA')
            _pool = _pool_owner.__enter__()

        for gen in range(generations):
            # 评估当前种群
            tasks = [self._build_task(ind) for ind in population]
            results_list = self._run_evaluations(tasks, n_jobs=n_jobs, verbose=False,
                                                 pool=_pool)

            # 构建适应度
            fitness = []
            for i, r in enumerate(results_list):
                if r is None:
                    fitness.append(float('-inf'))
                else:
                    fitness.append(r.objective)
                    self.results.append(r)

            # 找最优
            gen_best_idx = max(range(len(fitness)), key=lambda i: fitness[i])
            gen_best = population[gen_best_idx]
            gen_best_obj = fitness[gen_best_idx]

            if gen_best_obj > best_overall_obj:
                best_overall = copy.deepcopy(gen_best)
                best_overall_obj = gen_best_obj

            if verbose:
                _gb = results_list[gen_best_idx] if gen_best_idx < len(results_list) else None
                if _gb is not None:
                    _metrics = (f'Best={gen_best_obj:.2f} | '
                                f'Return={_gb.strategy_return*100:.2f}% | '
                                f'Sharpe={_gb.sharpe_ratio:.3f} | '
                                f'DD={_gb.max_drawdown*100:.1f}%')
                else:
                    _metrics = f'Best={gen_best_obj:.2f}'
                if ADAPTIVE_ENABLED and DASHBOARD_STATE is not None:
                    # GUI 模式：进度走独立进度面板，不再逐代打印到终端
                    _update_progress(phase='Phase 2/4', label='遗传算法精炼',
                                     current=gen + 1, total=generations,
                                     detail=_metrics)
                else:
                    print(f"  Gen {gen+1:>3}/{generations} | {_metrics}")

            # ── 保留验证段早停：连续 val_stagnant_limit 代无改善则提前停止 ──
            if val_eval_fn is not None:
                v_score = val_eval_fn(gen_best)
                if v_score is not None:
                    if v_score > best_val_obj + val_tolerance:
                        best_val_obj = v_score
                        val_stagnant = 0
                    else:
                        val_stagnant += 1
                    if verbose:
                        print(f"       [验证段] Val={v_score:.2f} "
                              f"(best={best_val_obj:.2f}, 停滞{val_stagnant}/{val_stagnant_limit})")
                    if val_stagnant >= val_stagnant_limit:
                        print(f"  ⏹ 验证段连续 {val_stagnant_limit} 代无改善，"
                              f"遗传算法提前停止")
                        break

            if gen == generations - 1:
                break

            # 精英保留
            sorted_idx = sorted(range(len(fitness)), key=lambda i: fitness[i], reverse=True)
            new_population = [copy.deepcopy(population[i]) for i in sorted_idx[:elite_count]]

            # 生成新一代
            while len(new_population) < population_size:
                parents = self._tournament_select(population, fitness)
                if random.random() < crossover_rate:
                    child = self._crossover(parents[0], parents[1], space)
                else:
                    child = copy.deepcopy(parents[0])
                child = self._mutate(child, space, mutation_rate)
                new_population.append(child)

            population = new_population

            # ── 停止信号检查（每代结束后）──
            if _check_control() == 'stop':
                break

        self._restore_config()

        # 关闭跨代复用的进程池（正常结束或早停/停止信号 break 后统一收尾）
        if _pool_owner is not None:
            try:
                _pool_owner.__exit__(None, None, None)
            except Exception:
                pass

        if verbose and best_overall:
            print(f"\n全局最优参数 (Generation):")
            for key, val in sorted(best_overall.items()):
                print(f"  {key}: {val}")
            print(f"最优目标值: {best_overall_obj:.2f}")

        return self.results


# ============================================================
# 方法3：逐级精细网格搜索（Coarse-to-Fine Grid）
# ============================================================

class CoarseToFineGridOptimizer(BaseOptimizer):
    """
    逐级精细网格搜索

    思路：
    - Level 1: 粗网格 3-4 个点 × 全参数 → 锁定最优区域
    - Level 2: 在最优区域中网格搜索
    - Level 3: 精细搜索最优子空间

    相比纯网格搜索 34560 组合，只需 500-2000 次即可收敛到近似最优。
    """

    def run(self, space=None, levels=3, top_k=10, n_jobs=-1, verbose=True, **kwargs):
        """
        运行逐级精细网格搜索

        Args:
            space: 搜索空间
            levels: 精细层级数
            top_k: 每层保留的最优数量
            n_jobs: 并行进程数
            verbose: 是否打印进度
        """
        if space is None:
            space = dict(OPTIMIZER_SEARCH_SPACE)

        print(f"\n{'='*60}")
        print(f"逐级精细网格搜索（Coarse-to-Fine Grid）")
        print(f"{'='*60}")
        print(f"参数数量: {len(space)}")
        print(f"精细层级: {levels}")
        print(f"每层保留: top {top_k}")
        print(f"{'='*60}\n")

        self._save_config()
        df_json = self._prepare_df_json()
        self.results = []
        all_trial_results = []

        # 初始化搜索区域为全空间
        current_bounds = {}
        for name, spec in space.items():
            lo, hi = spec['range']
            current_bounds[name] = {
                'lo': lo, 'hi': hi,
                'type': spec['type'],
                'step': spec.get('step'),
            }

        for level in range(1, levels + 1):
            # 网格点数随层级递增
            if level == 1:
                grid_size = 3
            elif level == 2:
                grid_size = 4
            else:
                grid_size = 5

            if verbose:
                print(f"\n--- Level {level}/{levels} (网格={grid_size}点/维度) ---")

            # 生成网格
            grid_points = self._generate_grid(current_bounds, grid_size)
            total = sum(1 for _ in grid_points)

            if verbose:
                print(f"  本层组合数: {total}")

            # 重建网格迭代器
            grid_points = list(self._generate_grid(current_bounds, grid_size))
            tasks = [self._build_task(p) for p in grid_points]
            results = self._run_evaluations(tasks, n_jobs=n_jobs, verbose=verbose)

            # 保存
            all_trial_results.extend(results)
            self.results = all_trial_results

            if level == levels:
                break

            # 选出 top_k 用于缩小搜索区域
            sorted_results = sorted(
                [r for r in results if r is not None],
                key=lambda x: x.objective, reverse=True
            )[:top_k]

            if not sorted_results:
                print("  无有效结果，停止")
                break

            # 缩小搜索区域到 top_k 的邻域
            self._narrow_bounds(current_bounds, sorted_results, space, expand=0.3)

        self._restore_config()

        return self.results

    def _generate_grid(self, bounds, grid_size):
        """生成网格点"""
        param_names = list(bounds.keys())
        param_ranges = []
        for name in param_names:
            b = bounds[name]
            lo, hi = b['lo'], b['hi']
            if b['type'] == 'float':
                points = np.linspace(lo, hi, grid_size)
                if b.get('step'):
                    points = [round(p / b['step']) * b['step'] for p in points]
                points = [round(p, 6) for p in points]
            else:
                step = b.get('step', 1)
                points = list(range(int(lo), int(hi) + 1, max(1, (int(hi) - int(lo)) // (grid_size - 1))))
                if b.get('step'):
                    points = [int(lo) + i * int(step) for i in range(grid_size)]
                points = [int(p) for p in points]
            param_ranges.append(points)

        from itertools import product
        for combo in product(*param_ranges):
            yield dict(zip(param_names, combo))

    def _narrow_bounds(self, bounds, top_results, space, expand=0.3):
        """根据 top 结果缩小搜索区域"""
        for name, b in bounds.items():
            values = [r.params.get(name) for r in top_results if name in r.params]
            if not values:
                continue

            lo_val = min(values)
            hi_val = max(values)
            range_val = hi_val - lo_val if hi_val > lo_val else abs(lo_val) * 0.1 + 1e-6

            new_lo = max(b['lo'], lo_val - range_val * expand)
            new_hi = min(b['hi'], hi_val + range_val * expand)

            b['lo'] = new_lo
            b['hi'] = new_hi


# ============================================================
# 便捷入口和命令行接口
# ============================================================

def _export_standard_results(optimizers, method):
    """将标准优化方法的结果导出到 JSON 文件"""
    import json as _json

    # 收集所有结果
    all_results = []
    for name, opt in optimizers:
        all_results.extend(opt.results)

    if not all_results:
        return

    # 按目标分排序
    all_sorted = sorted(all_results, key=lambda x: x.objective, reverse=True)

    # 各方法汇总
    phase_summary = {}
    for name, opt in optimizers:
        if opt.results:
            best = max(opt.results, key=lambda x: x.objective)
            phase_summary[name] = {
                'objective': round(best.objective, 2),
                'return_pct': round(best.strategy_return * 100, 2),
                'sharpe': round(best.sharpe_ratio, 3),
                'max_dd_pct': round(best.max_drawdown * 100, 2),
                'total_trades': best.total_trades,
                'n_results': len(opt.results),
            }

    # Top-20
    top20_export = []
    for r in all_sorted[:20]:
        entry = {
            'objective': round(r.objective, 2),
            'return_pct': round(r.strategy_return * 100, 2),
            'excess_pct': round(r.excess_return * 100, 2),
            'annualized_pct': round(r.annualized_return * 100, 2),
            'sharpe': round(r.sharpe_ratio, 3),
            'max_dd_pct': round(r.max_drawdown * 100, 2),
            'calmar': round(r.calmar_ratio, 3),
            'win_rate_pct': round(r.win_rate * 100, 1),
            'total_trades': r.total_trades,
            'params': r.params,
        }
        top20_export.append(entry)

    output = {
        'meta': {
            'version': 'V6.2.3 Standard',
            'method': method,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_evaluations': len(all_results),
        },
        'phase_summary': phase_summary,
        'top20': top20_export,
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'standard_results.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        _json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n结果已保存到: {out_path}")


def find_best_params(method='optuna', **kwargs):
    """
    一站式参数优化入口

    Args:
        method: 'optuna' | 'genetic' | 'coarse2fine' | 'all'
        **kwargs: 传递给具体优化器

    Returns:
        (optimizer_instance, best_params_dict)
    """
    print("=" * 60)
    print("V6.2.3 智能参数优化器")
    print("=" * 60)

    df = load_data_from_db()
    if df is None:
        print("无法加载数据，退出")
        return None, None

    print(f"数据: {len(df)} 行, {df.index[0].date()} ~ {df.index[-1].date()}")
    print(f"搜索空间: {len(OPTIMIZER_SEARCH_SPACE)} 个参数\n")

    optimizers = []

    if method in ('optuna', 'all'):
        if HAS_OPTUNA:
            opt = BayesianOptimizer(df)
            opt_kwargs = copy.deepcopy(kwargs)
            trials = opt_kwargs.pop('n_trials', 500)
            opt.run(n_trials=trials, **opt_kwargs)
            opt.print_summary()
            optimizers.append(('optuna', opt))
        else:
            print("[跳过] Optuna 未安装，pip install optuna")

    if method in ('genetic', 'all'):
        opt = GeneticOptimizer(df)
        opt.run(**kwargs)
        opt.print_summary()
        optimizers.append(('genetic', opt))

    if method in ('coarse2fine', 'all'):
        opt = CoarseToFineGridOptimizer(df)
        opt.run(**kwargs)
        opt.print_summary()
        optimizers.append(('coarse2fine', opt))

    # 汇总最优
    if optimizers:
        print(f"\n{'='*60}")
        print(f"所有方法最优汇总")
        print(f"{'='*60}")
        best_opt = None
        best_obj = float('-inf')
        for name, opt in optimizers:
            top = opt.get_best_result()
            if top and top.objective > best_obj:
                best_obj = top.objective
                best_opt = opt
            if top:
                print(f"\n[{name}] Top: {top.strategy_return*100:.2f}%, Objective={top.objective:.2f}")

        # 导出结果到 JSON
        _export_standard_results(optimizers, method)

        if best_opt:
            print(f"\n>>> 全局最优（来自 {best_opt.__class__.__name__}）<<<")
            best = best_opt.get_best_result()
            for k, v in sorted(best.params.items()):
                print(f"  {k}: {v}")
            print(f"  收益率: {best.strategy_return*100:.2f}%")
            print(f"  超额:   {best.excess_return*100:.2f}%")
            print(f"  夏普:   {best.sharpe_ratio:.3f}")
            print(f"  回撤:   {best.max_drawdown*100:.2f}%")
            print(f"  目标分: {best.objective:.2f}")

            # 询问是否写回
            if '--apply' in sys.argv:
                best_opt.apply_best_params()
            else:
                print("\n提示: 使用 --apply 参数自动将最优参数写入 config.py")

            return best_opt, best.params

    return None, None


# ============================================================
# 退化兼容：无 Optuna 时的自适应随机搜索
# ============================================================

class AdaptiveRandomOptimizer(BaseOptimizer):
    """
    自适应随机搜索（Optuna 不可用时的退路）

    使用简单的局部搜索策略：
    1. 随机采样 N 个点
    2. 在最优 K 个点周围做局部搜索
    """

    def run(self, n_random=200, n_local=100, n_jobs=-1, verbose=True, space=None):
        if space is None:
            space = dict(OPTIMIZER_SEARCH_SPACE)

        print(f"\n{'='*60}")
        print(f"自适应随机搜索（Adaptive Random）")
        print(f"{'='*60}")
        print(f"随机采样: {n_random} + 局部搜索: {n_local}")
        print(f"{'='*60}\n")

        self._save_config()
        df_json = self._prepare_df_json()
        self.results = []

        random.seed(42)

        # Phase 1: 随机采样
        if verbose:
            print("[Phase 1] 随机采样...")
        random_individuals = []
        for _ in range(n_random):
            ind = {}
            for name, spec in space.items():
                lo, hi = spec['range']
                if spec['type'] == 'float':
                    val = lo + random.random() * (hi - lo)
                    if spec.get('step'):
                        val = round(val / spec['step']) * spec['step']
                    ind[name] = round(val, 6)
                else:
                    step = spec.get('step', 1)
                    steps = (hi - lo) // step
                    ind[name] = int(lo + random.randint(0, steps) * step)
            random_individuals.append(ind)

        tasks = [self._build_task(ind) for ind in random_individuals]
        results = self._run_evaluations(tasks, n_jobs=n_jobs, verbose=verbose)
        self.results.extend([r for r in results if r is not None])

        if verbose and self.results:
            best_r = max(self.results, key=lambda x: x.objective)
            print(f"  最优: {best_r.strategy_return*100:.2f}%, Obj={best_r.objective:.2f}")

        # Phase 2: 在最优周围局部搜索
        sorted_r = sorted(self.results, key=lambda x: x.objective, reverse=True)
        top_k = sorted_r[:max(3, n_random // 20)]
        if not top_k:
            self._restore_config()
            return self.results

        if verbose:
            print(f"\n[Phase 2] 局部搜索（top {len(top_k)} 个点周围）...")

        local_individuals = []
        for base in top_k:
            for _ in range(n_local // len(top_k)):
                ind = {}
                for name, spec in space.items():
                    lo, hi = spec['range']
                    range_val = hi - lo
                    sigma = range_val * 0.05  # 5% 的搜索半径
                    base_val = base.params.get(name, (lo + hi) / 2)
                    new_val = base_val + random.gauss(0, sigma)
                    new_val = max(lo, min(hi, new_val))
                    if spec['type'] == 'float':
                        if spec.get('step'):
                            new_val = round(new_val / spec['step']) * spec['step']
                        ind[name] = round(new_val, 6)
                    else:
                        step = spec.get('step', 1)
                        ind[name] = int(round(new_val / step) * step)
                local_individuals.append(ind)

        tasks = [self._build_task(ind) for ind in local_individuals]
        results = self._run_evaluations(tasks, n_jobs=n_jobs, verbose=verbose)
        self.results.extend([r for r in results if r is not None])

        self._restore_config()
        return self.results


# ============================================================
# ╔══════════════════════════════════════════════════════════╗
# ║  高算力全量优化模块（Heavy Compute / Returns-Max）      ║
# ╚══════════════════════════════════════════════════════════╝
#
# 当你有充足算力和时间时，运行此模块能将收益率推到极致。
#
# 核心特性：
# 1. 搜索空间扩展至 50+ 参数（仓位曲线、情绪修正、Evidence权重、TimeDecay等）
# 2. Walk-Forward 滚动窗口交叉验证（防过拟合）
# 3. Cascading Pipeline：Optuna → GA → 局部精细网格 → Ensemble
# 4. Returns-Aggressive 目标函数（以年化收益为核心）
# 5. 多时段稳健性评分
#
# 用法：
#   python param_optimizer.py --method heavy --trials 2000 --jobs 8
# ============================================================

# ============================================================
# H1：超大规模搜索空间（50+ 参数）
# ============================================================

HEAVY_SEARCH_SPACE = {
    # ─── A组：评分公式权重 ──────────────────────────────────
    'SCORE_BEHAVIOR_WEIGHT':    {'type': 'float', 'range': (0.10, 0.45), 'step': None, 'group': 'weights'},
    'SCORE_CONFIDENCE_WEIGHT':  {'type': 'float', 'range': (0.05, 0.30), 'step': None, 'group': 'weights'},
    'SCORE_REWARD_WEIGHT':      {'type': 'float', 'range': (0.20, 0.60), 'step': None, 'group': 'weights'},
    'SCORE_RISK_WEIGHT':        {'type': 'float', 'range': (0.05, 0.30), 'step': None, 'group': 'weights'},

    # ─── B组：市场状态权重 ──────────────────────────────────
    'BULL_BUY_MULT':   {'type': 'float', 'range': (1.00, 3.00), 'step': None, 'group': 'regime'},
    'BULL_SELL_DIV':   {'type': 'float', 'range': (0.03, 0.35), 'step': None, 'group': 'regime'},
    'RANGE_BUY_MULT':  {'type': 'float', 'range': (0.20, 1.00), 'step': None, 'group': 'regime'},
    'RANGE_SELL_DIV':  {'type': 'float', 'range': (0.50, 1.50), 'step': None, 'group': 'regime'},
    'BEAR_BUY_MULT':   {'type': 'float', 'range': (0.40, 1.40), 'step': None, 'group': 'regime'},
    'BEAR_SELL_DIV':   {'type': 'float', 'range': (0.60, 1.60), 'step': None, 'group': 'regime'},

    # ─── C组：交易执行参数 ──────────────────────────────────
    'CONFIRMATION_THRESHOLD':    {'type': 'int',   'range': (50, 82),  'step': 1, 'group': 'exec'},
    'CONFIDENCE_INCREMENT':      {'type': 'int',   'range': (2, 18),   'step': 1, 'group': 'exec'},
    'OBSERVATION_WINDOW_MAX':    {'type': 'int',   'range': (2, 10),   'step': 1, 'group': 'exec'},
    'EXPIRY_THRESHOLD':          {'type': 'int',   'range': (8, 40),   'step': 1, 'group': 'exec'},
    'MIN_HOLD_DAYS':             {'type': 'int',   'range': (2, 40),   'step': 1, 'group': 'exec'},
    'SCORE_HOLD_ZONE':           {'type': 'int',   'range': (5, 35),   'step': 1, 'group': 'exec'},
    'TRADE_TARGET_DELTA':        {'type': 'float', 'range': (0.005, 0.10), 'step': 0.002, 'group': 'exec'},
    'TRADE_ACTUAL_DELTA':        {'type': 'float', 'range': (0.005, 0.10), 'step': 0.002, 'group': 'exec'},
    'MAX_POSITION':              {'type': 'float', 'range': (0.60, 0.99), 'step': 0.01, 'group': 'exec'},
    'INITIAL_POSITION':          {'type': 'float', 'range': (0.60, 0.99), 'step': 0.01, 'group': 'exec'},

    # ─── D组：仓位映射曲线（买入端：高评分→高仓位）──────────
    # 原有4层映射：(68, 0.95), (62, 0.90), (56, 0.85), (50, 0.75)
    'BUY_T1_THRESHOLD':  {'type': 'float', 'range': (60, 80),   'step': 1, 'group': 'position'},
    'BUY_T1_POSITION':   {'type': 'float', 'range': (0.85, 1.00), 'step': 0.01, 'group': 'position'},
    'BUY_T2_THRESHOLD':  {'type': 'float', 'range': (55, 72),   'step': 1, 'group': 'position'},
    'BUY_T2_POSITION':   {'type': 'float', 'range': (0.75, 0.95), 'step': 0.01, 'group': 'position'},
    'BUY_T3_THRESHOLD':  {'type': 'float', 'range': (48, 65),   'step': 1, 'group': 'position'},
    'BUY_T3_POSITION':   {'type': 'float', 'range': (0.65, 0.88), 'step': 0.01, 'group': 'position'},
    'BUY_T4_THRESHOLD':  {'type': 'float', 'range': (40, 58),   'step': 1, 'group': 'position'},
    'BUY_T4_POSITION':   {'type': 'float', 'range': (0.50, 0.80), 'step': 0.01, 'group': 'position'},

    # ─── E组：仓位映射曲线（卖出端：高卖出评分→高减仓比例）────
    'SELL_T1_THRESHOLD':   {'type': 'float', 'range': (55, 78),  'step': 1, 'group': 'position'},
    'SELL_T1_REDUCTION':   {'type': 'float', 'range': (0.30, 0.80), 'step': 0.02, 'group': 'position'},
    'SELL_T2_THRESHOLD':   {'type': 'float', 'range': (48, 70),  'step': 1, 'group': 'position'},
    'SELL_T2_REDUCTION':   {'type': 'float', 'range': (0.15, 0.55), 'step': 0.02, 'group': 'position'},
    'SELL_T3_THRESHOLD':   {'type': 'float', 'range': (40, 62),  'step': 1, 'group': 'position'},
    'SELL_T3_REDUCTION':   {'type': 'float', 'range': (0.05, 0.35), 'step': 0.02, 'group': 'position'},
    'SELL_T4_THRESHOLD':   {'type': 'float', 'range': (30, 55),  'step': 1, 'group': 'position'},
    'SELL_T4_REDUCTION':   {'type': 'float', 'range': (0.02, 0.20), 'step': 0.02, 'group': 'position'},

    # ─── F组：情绪修正系数 ──────────────────────────────────
    'PSYCH_PANIC_BUY_BOOST':    {'type': 'float', 'range': (0.95, 1.30), 'step': 0.02, 'group': 'emotion'},
    'PSYCH_EUPHORIA_SELL_BOOST':{'type': 'float', 'range': (1.00, 1.50), 'step': 0.02, 'group': 'emotion'},
    'PSYCH_EUPHORIA_BUY_CUT':   {'type': 'float', 'range': (0.20, 0.70), 'step': 0.02, 'group': 'emotion'},
    'PSYCH_EXHAUSTION_SELL_BOOST':{'type': 'float','range': (1.00, 1.60), 'step': 0.02, 'group': 'emotion'},

    # ─── G组：Evidence Engine 权重 ──────────────────────────
    'EVIDENCE_WEIGHT_RULE':    {'type': 'float', 'range': (0.10, 0.50), 'step': 0.02, 'group': 'evidence'},
    'EVIDENCE_WEIGHT_REPLAY':  {'type': 'float', 'range': (0.10, 0.40), 'step': 0.02, 'group': 'evidence'},
    'EVIDENCE_WEIGHT_ML':      {'type': 'float', 'range': (0.10, 0.50), 'step': 0.02, 'group': 'evidence'},
    'EVIDENCE_WEIGHT_EMOTION': {'type': 'float', 'range': (0.02, 0.20), 'step': 0.02, 'group': 'evidence'},

    # ─── H组：Time Decay 参数 ───────────────────────────────
    'TIME_DECAY_GRACE_PERIOD':  {'type': 'int',   'range': (2, 15),   'step': 1, 'group': 'timedecay'},
    'TIME_DECAY_TAU':           {'type': 'int',   'range': (30, 200), 'step': 5, 'group': 'timedecay'},
    'TIME_DECAY_MIN_MULTIPLIER':{'type': 'float', 'range': (0.40, 0.85), 'step': 0.02, 'group': 'timedecay'},

    # ─── I组：行为检测阈值 ──────────────────────────────────
    'DOUBLE_BOTTOM_REBOUND_MIN':  {'type': 'float', 'range': (0.003, 0.035), 'step': 0.002, 'group': 'behavior'},
    'DOUBLE_BOTTOM_SCORE':        {'type': 'int',   'range': (25, 65), 'step': 5, 'group': 'behavior'},
    'MOMO_EXH_RETURN_THRESHOLD':  {'type': 'float', 'range': (0.012, 0.045), 'step': 0.002, 'group': 'behavior'},
    'MOMO_EXH_SCORE':             {'type': 'int',   'range': (30, 70), 'step': 5, 'group': 'behavior'},
    'PULLBACK_MA_DIST':           {'type': 'float', 'range': (0.008, 0.040), 'step': 0.002, 'group': 'behavior'},
    'PANIC_SELL_DROP_THRESHOLD':  {'type': 'float', 'range': (-0.070, -0.015), 'step': 0.002, 'group': 'behavior'},
    'TREND_FAIL_SCORE':           {'type': 'int',   'range': (30, 70), 'step': 5, 'group': 'behavior'},
    'RSI_OVERBOUGHT_THRESHOLD':   {'type': 'int',   'range': (58, 80), 'step': 2, 'group': 'behavior'},
}


def _build_position_curves_from_params(params):
    """从扁平参数重建仓位映射曲线"""
    buy_tiers = []
    sell_tiers = []

    for tier in [1, 2, 3, 4]:
        t_key = f'BUY_T{tier}_THRESHOLD'
        p_key = f'BUY_T{tier}_POSITION'
        if t_key in params and p_key in params:
            buy_tiers.append((params[t_key], params[p_key]))

        st_key = f'SELL_T{tier}_THRESHOLD'
        sr_key = f'SELL_T{tier}_REDUCTION'
        if st_key in params and sr_key in params:
            sell_tiers.append((params[st_key], params[sr_key]))

    # Sort descending for correct threshold logic
    buy_tiers.sort(key=lambda x: -x[0])
    sell_tiers.sort(key=lambda x: -x[0])

    return buy_tiers, sell_tiers


def _build_evidence_weights_from_params(params):
    """从扁平参数重建 EVIDENCE_WEIGHTS dict"""
    if 'EVIDENCE_WEIGHT_RULE' not in params:
        return None
    return {
        'rule':    params.get('EVIDENCE_WEIGHT_RULE', 0.30),
        'replay':  params.get('EVIDENCE_WEIGHT_REPLAY', 0.25),
        'ml':      params.get('EVIDENCE_WEIGHT_ML', 0.35),
        'emotion': params.get('EVIDENCE_WEIGHT_EMOTION', 0.10),
    }


# ============================================================
# H2：Returns-Aggressive 目标函数
# ============================================================

def returns_aggressive_objective(
    annualized_return=0.0, strategy_return=0.0,
    excess_return=0.0, max_drawdown=0.0,
    sharpe_ratio=0.0, calmar_ratio=0.0,
    total_trades=0, win_rate=0.0, volatility=0.0,
    **kwargs
) -> float:
    """
    激进收益型目标函数 —— 年化收益权重极大，仅保留基本风险约束

    设计理念：用户追求收益率最大化，只要回撤不太离谱即可。
    """
    score = 0.0

    # ── 核心：年化收益率最大化 ──
    if annualized_return != 0:
        score += annualized_return * 150  # 权重提升到150（原100）
    elif strategy_return != 0:
        score += strategy_return * 120

    # ── 超额收益大额加分 ──
    excess_contrib = excess_return * 80
    score += excess_contrib

    # ── 风险底线约束（回撤 > 10% 开始惩罚，>25% 强惩罚）──
    if max_drawdown < 0:
        dd_pct = abs(max_drawdown)
        if dd_pct > 0.25:
            score += (0.20 - dd_pct) * 80  # >25% 强惩罚
        elif dd_pct > 0.15:
            score -= (dd_pct - 0.15) * 40  # 15-25% 中惩罚
        elif dd_pct > 0.10:
            score -= (dd_pct - 0.10) * 30  # 10-15% 小惩罚
        # <10% 不惩罚

    # ── 夏普比率加分 ──
    if sharpe_ratio > 0:
        score += min(sharpe_ratio * 6, 10)
    else:
        score += sharpe_ratio * 8

    # ── Calmar 微调 ──
    if calmar_ratio > 0:
        score += min(calmar_ratio * 5, 8)

    # ── 交易次数：最低保证 ──
    if total_trades < 3:
        score -= 8
    elif total_trades > 100:
        score -= min((total_trades - 100) * 0.1, 5)

    # ── 胜率 ──
    if win_rate > 0.50:
        score += (win_rate - 0.50) * 8

    # ── 波动率 ──
    if volatility > 0.35:
        score -= (volatility - 0.35) * 20

    return score


# ============================================================
# H3：Walk-Forward 滚动窗口验证
# ============================================================

@dataclass
class WalkForwardResult:
    """一次 Walk-Forward 评估的总结果"""
    params: Dict
    train_objective: float
    val_objective: float
    robustness: float          # 跨窗口标准差归一化得分
    train_results: List[TrialResult] = field(default_factory=list)
    val_results: List[TrialResult] = field(default_factory=list)
    all_results: List[TrialResult] = field(default_factory=list)

    @property
    def combined_score(self) -> float:
        """综合评分 = 验证集目标 × 稳健性"""
        return self.val_objective * max(0.3, self.robustness)


def run_walk_forward_validation(
    df, params, n_splits=3, validation_ratio=0.25,
    objective_fn=returns_aggressive_objective
) -> Optional[WalkForwardResult]:
    """
    滚动窗口交叉验证：
    将数据按时间切分为 n_splits 个训练/验证段，
    每段独立回测，最终汇总稳健性评分。

    Returns:
        WalkForwardResult 或 None
    """
    all_train_results = []
    all_val_results = []
    all_obj = []

    total_len = len(df)
    if total_len < 120:
        return None

    valid_end = total_len
    split_size = int(total_len * (1 - validation_ratio))

    for split_idx in range(n_splits):
        val_start = total_len - int(total_len * validation_ratio * (split_idx + 1) / n_splits)
        val_end = total_len - int(total_len * validation_ratio * split_idx / n_splits)
        train_start = max(0, val_start - split_size)

        if val_end - val_start < 30 or train_start >= val_start:
            continue

        # 训练集回测
        train_df = df.iloc[train_start:val_start].copy()
        val_df = df.iloc[val_start:val_end].copy()

        if len(train_df) < 60 or len(val_df) < 20:
            continue

        train_result = _eval_params_on_df(params, train_df, str(train_df.index[0].date()))
        val_result = _eval_params_on_df(params, val_df, str(val_df.index[0].date()))

        if train_result and val_result:
            train_obj = objective_fn(
                annualized_return=train_result.annualized_return,
                strategy_return=train_result.strategy_return,
                excess_return=train_result.excess_return,
                max_drawdown=train_result.max_drawdown,
                sharpe_ratio=train_result.sharpe_ratio,
                calmar_ratio=train_result.calmar_ratio,
                total_trades=train_result.total_trades,
                win_rate=train_result.win_rate,
                volatility=train_result.volatility,
            )
            val_obj = objective_fn(
                annualized_return=val_result.annualized_return,
                strategy_return=val_result.strategy_return,
                excess_return=val_result.excess_return,
                max_drawdown=val_result.max_drawdown,
                sharpe_ratio=val_result.sharpe_ratio,
                calmar_ratio=val_result.calmar_ratio,
                total_trades=val_result.total_trades,
                win_rate=val_result.win_rate,
                volatility=val_result.volatility,
            )
            all_obj.append(val_obj)
            all_train_results.append(train_result)
            all_val_results.append(val_result)

    if not all_obj:
        return None

    avg_val_obj = float(np.mean(all_obj))
    # 稳健性：1 - (std/mean) 归一化
    std_obj = float(np.std(all_obj))
    robustness = 1.0 - min(1.0, std_obj / (abs(avg_val_obj) + 1e-6))

    return WalkForwardResult(
        params=params,
        train_objective=float(np.mean([r.objective for r in all_train_results])),
        val_objective=avg_val_obj,
        robustness=robustness,
        train_results=all_train_results,
        val_results=all_val_results,
        all_results=all_train_results + all_val_results,
    )


def _wf_worker(args):
    """Walk-Forward worker（模块级别，可 pickle）"""
    df, params, objective_fn = args
    return run_walk_forward_validation(df, params, n_splits=3, objective_fn=objective_fn)


def _run_wf_parallel(tasks, n_jobs=-1, verbose=True, progress_cb=None):
    """并行执行多个 Walk-Forward 验证

    progress_cb: 可选回调 fn(done, total)，每完成一个候选调用一次。
    """
    results = []
    if n_jobs == 1 or len(tasks) <= 1:
        for idx, t in enumerate(tasks, 1):
            # ── 暂停/停止信号检查 ──
            if _check_control() == 'stop':
                break
            r = _wf_worker(t)
            if r is not None:
                results.append(r)
            if progress_cb is not None:
                progress_cb(idx, len(tasks))
    elif ADAPTIVE_ENABLED and HAS_ADAPTIVE:
        # ── 自适应进程池（可视化控制面板模式）──
        with AdaptiveWorkerPool(_wf_worker, governor=GOVERNOR, state=DASHBOARD_STATE,
                                pool_name='WFV') as _pool:
            futures = [_pool.submit(t) for t in tasks]
            _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
            pbar = (tqdm(total=len(futures), desc="WFV (自适应)",
                         unit="candidate") if _use_tqdm else None)
            done = 0
            for future in futures:
                # ── 暂停/停止信号检查（轮询式）──
                r, _stopped = _wait_future(future)
                if _stopped:
                    try:
                        _pool.abort()
                    except Exception:
                        pass
                    break
                if r is not None:
                    results.append(r)
                done += 1
                if pbar is not None:
                    pbar.update(1)
                if progress_cb is not None:
                    progress_cb(done, len(futures))
            if pbar is not None:
                pbar.close()
    else:
        ctx = multiprocessing.get_context('spawn')
        with ProcessPoolExecutor(max_workers=min(n_jobs, len(tasks)), mp_context=ctx) as executor:
            futures = {executor.submit(_wf_worker, t): t for t in tasks}
            _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
            pbar = (tqdm(total=len(futures), desc="WFV",
                         unit="candidate") if _use_tqdm else None)
            done = 0
            remaining = set(futures)
            while remaining:
                # ── 暂停/停止信号检查（as_completed 带超时轮询）──
                if _check_control() == 'stop':
                    break
                try:
                    for future in as_completed(list(remaining), timeout=0.5):
                        remaining.discard(future)
                        try:
                            r = future.result()
                            if r is not None:
                                results.append(r)
                        except:
                            pass
                        done += 1
                        if pbar is not None:
                            pbar.update(1)
                        if progress_cb is not None:
                            progress_cb(done, len(futures))
                except TimeoutError:
                    continue
            if pbar is not None:
                pbar.close()
    return results


def _eval_params_on_df(params, df, start_date):
    """在给定的 DataFrame 上执行回测（Walk-Forward 使用不同的子数据集）"""
    try:
        # 重新计算数据集的指标
        df_with_ind = calculate_indicators(df.copy())

        # 检测是否为 Heavy 参数
        is_heavy = any(k.startswith(('BUY_T', 'SELL_T', 'EVIDENCE_WEIGHT_', 'PSYCH_'))
                       for k in params)

        if is_heavy:
            return _eval_single_heavy_on_df(params, df_with_ind, start_date)
        else:
            return _eval_single_plain_on_df(params, df_with_ind, start_date)
    except:
        return None


def _eval_single_plain_on_df(params, df, start_date):
    """在指定 DataFrame 上运行标准参数评估"""
    try:
        for param_name, value in params.items():
            if hasattr(config, param_name):
                setattr(config, param_name, value)
        if 'BULL_BUY_MULT' in params:
            config.REGIME_WEIGHTS = _build_regime_from_params(params)

        # ⚠️ 关键修复：重载导入时捕获配置的模块，使 setattr 的新参数生效
        strategy_mod, backtest_mod = _reload_config_capture_modules()

        strategy = strategy_mod.V6Strategy()
        signals = strategy.run(df)
        bt = backtest_mod.V6Backtest(df, start_date=start_date)
        bt_results = bt.run(signals)
        if not bt_results:
            return None
        result = TrialResult.from_backtest(params, bt_results, -1.0)
        result.objective = composite_objective(**vars_for_obj(result))
        return result
    except:
        return None


def _eval_single_heavy_on_df(params, df, start_date):
    """在指定 DataFrame 上运行 heavy 参数评估"""
    try:
        for param_name, value in params.items():
            if hasattr(config, param_name) and not param_name.startswith(
                    ('BUY_T', 'SELL_T', 'EVIDENCE_WEIGHT_', 'PSYCH_')):
                setattr(config, param_name, value)
        if 'BULL_BUY_MULT' in params:
            config.REGIME_WEIGHTS = _build_regime_from_params(params)

        buy_curve, sell_curve = _build_position_curves_from_params(params)
        if buy_curve:
            config.BUY_SCORE_THRESHOLDS = buy_curve
        if sell_curve:
            config.SELL_SCORE_THRESHOLDS = sell_curve
        ev_weights = _build_evidence_weights_from_params(params)
        if ev_weights:
            config.EVIDENCE_WEIGHTS = ev_weights

        # ⚠️ 关键修复：重载导入时捕获配置的模块，使 setattr 的新参数生效
        strategy_mod, backtest_mod = _reload_config_capture_modules()

        strategy = strategy_mod.V6Strategy()
        signals = strategy.run(df)
        bt = backtest_mod.V6Backtest(df, start_date=start_date)
        bt_results = bt.run(signals)
        if not bt_results:
            return None
        result = TrialResult.from_backtest(params, bt_results, -1.0)
        result.objective = returns_aggressive_objective(**vars_for_obj(result))
        return result
    except:
        return None


def vars_for_obj(result):
    return {
        'annualized_return': result.annualized_return,
        'strategy_return': result.strategy_return,
        'excess_return': result.excess_return,
        'max_drawdown': result.max_drawdown,
        'sharpe_ratio': result.sharpe_ratio,
        'calmar_ratio': result.calmar_ratio,
        'total_trades': result.total_trades,
        'win_rate': result.win_rate,
        'volatility': result.volatility,
    }


# ============================================================
# H4：Cascading Pipeline（级联优化管道）
# ============================================================

class HeavyOptimizer:
    """
    全量级联优化器 —— 榨干算力的终极搜索

    管道：
    Phase 1: Optuna 贝叶斯优化（2000-5000 trials）—— 全局粗搜
    Phase 2: 遗传算法精炼（100 代 × 60 种群）—— 跳出局部最优
    Phase 3: 局部精细网格（Top-K 周围）—— 微调最优解
    Phase 4: Walk-Forward 验证 + Ensemble 选择 —— 选最稳健的
    """

    def __init__(self, df, start_date=None):
        self.df = df
        self.start_date = start_date if start_date is not None else str(df.index[0].date())
        self.all_results: List[TrialResult] = []
        self.wf_results: List[WalkForwardResult] = []
        self.phase_results: Dict[str, List[TrialResult]] = {}
        self._original_config = {}
        self._original_regime = None
        self._checkpoint_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '.heavy_checkpoint.json'
        )
        self._completed_phases = set()
        self._checkpoint = {}  # 断点元数据
        # ── 保留验证段（三阶段搜索早停监控，独立于 Phase 4 Walk-Forward）──
        self._val_df = None
        self._val_baseline = None

    def _save_checkpoint(self):
        """保存断点到磁盘（含 phase_results 持久化，支持跨进程恢复）"""
        import json as _json
        ckpt = {
            'version': 'V6.2.3',
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'completed_phases': sorted(list(self._completed_phases)),
            'phase_results_count': {k: len(v) for k, v in self.phase_results.items()},
            'total_results': len(self.all_results),
            'n_trials': self._checkpoint.get('n_trials', 0),
            'ga_generations': self._checkpoint.get('ga_generations', 0),
            'ga_population': self._checkpoint.get('ga_population', 0),
        }
        tmp = self._checkpoint_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            _json.dump(ckpt, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._checkpoint_path)

        # 持久化 phase_results（pickle，跨进程恢复）
        _results_pkl_path = self._checkpoint_path.replace('.json', '_results.pkl')
        try:
            tmp_pkl = _results_pkl_path + '.tmp'
            with open(tmp_pkl, 'wb') as f:
                pickle.dump(self.phase_results, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_pkl, _results_pkl_path)
        except Exception as e:
            print(f"  [警告] phase_results 持久化失败: {e}")

    def _load_checkpoint(self):
        """加载断点，返回已完成的 phases 集合，并恢复 phase_results"""
        import json as _json
        if not os.path.exists(self._checkpoint_path):
            return set(), {}
        try:
            with open(self._checkpoint_path, 'r', encoding='utf-8') as f:
                ckpt = _json.load(f)
            completed = set(ckpt.get('completed_phases', []))

            # 恢复 phase_results
            _results_pkl_path = self._checkpoint_path.replace('.json', '_results.pkl')
            if os.path.exists(_results_pkl_path):
                try:
                    with open(_results_pkl_path, 'rb') as f:
                        self.phase_results = pickle.load(f)
                    # 重建 all_results
                    self.all_results = []
                    for phase_res in self.phase_results.values():
                        self.all_results.extend(phase_res)
                    print(f"  已恢复 phase_results: "
                          f"{', '.join(f'{k}={len(v)}' for k, v in self.phase_results.items() if v)}")
                except Exception as e:
                    print(f"  [警告] phase_results 恢复失败: {e}")

            return completed, ckpt
        except:
            return set(), {}

    def _save_and_prep(self):
        self._original_config = {}
        for attr in dir(config):
            if not attr.startswith('_') and attr.isupper():
                try:
                    self._original_config[attr] = copy.deepcopy(getattr(config, attr))
                except:
                    self._original_config[attr] = getattr(config, attr)
        self._original_regime = copy.deepcopy(getattr(config, 'REGIME_WEIGHTS', {}))

    def _save_phase1_results_to_pickle(self, study):
        """Phase 1 运行中定期保存 results 到 pickle（防 journal 断电丢失）"""
        import optuna as _opt
        _results_pkl_path = self._checkpoint_path.replace('.json', '_results.pkl')
        try:
            phase1_results = []
            for t in study.trials:
                if t.state == _opt.trial.TrialState.COMPLETE and t.value is not None:
                    r = TrialResult(
                        params={k: v for k, v in t.params.items()},
                        strategy_return=t.user_attrs.get('strategy_return', 0),
                        sharpe_ratio=t.user_attrs.get('sharpe_ratio', 0),
                        max_drawdown=t.user_attrs.get('max_drawdown', 0),
                        calmar_ratio=t.user_attrs.get('calmar_ratio', 0),
                        total_trades=t.user_attrs.get('total_trades', 0),
                        win_rate=t.user_attrs.get('win_rate', 0),
                        objective=t.value,
                    )
                    phase1_results.append(r)
            self.phase_results['optuna'] = phase1_results
            self.all_results = list(phase1_results)
            # 原子写入
            tmp_pkl = _results_pkl_path + '.tmp'
            with open(tmp_pkl, 'wb') as f:
                pickle.dump(self.phase_results, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_pkl, _results_pkl_path)
        except Exception as e:
            print(f"  [警告] Phase1 pickle checkpoint 失败: {e}")

    def _restore(self):
        for attr, val in self._original_config.items():
            try:
                setattr(config, attr, val)
            except:
                pass

    def _make_val_split(self, val_ratio=0.15):
        """划分保留验证段（数据最后 val_ratio，至少 30 行，与训练段无重叠）"""
        total = len(self.df)
        if total < 100:
            return None, "数据过少，跳过早停监控"
        n_val = max(30, int(total * val_ratio))
        n_train = total - n_val
        if n_train < 60:
            return None, "训练段过短，跳过早停监控"
        val_df = self.df.iloc[n_train:].copy()
        note = (f"{len(val_df)} 行 "
                f"{val_df.index[0].date()} ~ {val_df.index[-1].date()} "
                f"(训练 {n_train} 行)")
        return val_df, note

    def _val_eval_fn(self, params):
        """验证段评估回调：在保留验证段上运行回测，返回 objective（None=失败）。

        评估前后保存/恢复 config，避免污染主进程后续阶段的配置状态。
        """
        if self._val_df is None:
            return None
        saved = {}
        for attr in dir(config):
            if not attr.startswith('_') and attr.isupper():
                try:
                    saved[attr] = copy.deepcopy(getattr(config, attr))
                except Exception:
                    pass
        try:
            r = _eval_params_on_df(params, self._val_df,
                                   str(self._val_df.index[0].date()))
            return r.objective if r is not None else None
        finally:
            for attr, val in saved.items():
                try:
                    setattr(config, attr, val)
                except Exception:
                    pass

    def _update_val_baseline(self, top_k=10):
        """用当前所有结果中训练目标 Top-K 在验证段上的最优值刷新早停基线。

        Returns:
            float: 验证段最优 objective；无有效结果返回 None
        """
        if self._val_df is None or not self.all_results:
            return None
        top_k = max(1, min(top_k, len(self.all_results)))
        top_sorted = sorted(self.all_results, key=lambda x: x.objective,
                            reverse=True)[:top_k]
        best_val = None
        for r in top_sorted:
            v = self._val_eval_fn(r.params)
            if v is not None:
                best_val = v if best_val is None else max(best_val, v)
        self._val_baseline = best_val
        return best_val

    def run(self, n_trials=10000, ga_generations=100, ga_population=60,
            n_jobs=14, ga_n_jobs=10, wf_top_k=20, resume=True, verbose=True,
            output_path=None):
        """全量优化管道入口：注册当前实例供紧急停止保存断点，再转 _run_pipeline。

        紧急停止（request_emergency_stop）需要在优化线程被终止前立即保存
        当前断点 —— 通过模块级 _ACTIVE_HEAVY 定位正在运行的实例。
        """
        global _ACTIVE_HEAVY
        _ACTIVE_HEAVY = self
        try:
            return self._run_pipeline(
                n_trials=n_trials, ga_generations=ga_generations,
                ga_population=ga_population, n_jobs=n_jobs,
                ga_n_jobs=ga_n_jobs, wf_top_k=wf_top_k,
                resume=resume, verbose=verbose, output_path=output_path)
        finally:
            _ACTIVE_HEAVY = None

    def _run_pipeline(self, n_trials=10000, ga_generations=100, ga_population=60,
            n_jobs=14, ga_n_jobs=10, wf_top_k=20, resume=True, verbose=True,
            output_path=None):
        """
        启动全量优化管道（支持断点续算）

        Args:
            n_trials: Optuna 阶段试验次数
            ga_generations: GA 代数
            ga_population: GA 种群大小
            n_jobs: 全局并行数（用于 Optuna / FineGrid / WFV）
            ga_n_jobs: 遗传算法专用并行数
            wf_top_k: Walk-Forward 验证的候选数
            resume: 是否启用断点续算
            output_path: 结果 JSON 导出路径（None = 脚本目录 heavy_results.json）
        """
        space = dict(HEAVY_SEARCH_SPACE)
        cpu_count = os.cpu_count() or 4
        if n_jobs < 0:
            n_jobs = cpu_count
        if ga_n_jobs < 0:
            ga_n_jobs = min(cpu_count, 10)

        # 加载断点
        if resume:
            self._completed_phases, self._checkpoint = self._load_checkpoint()
        else:
            self._completed_phases = set()
            self._checkpoint = {}

        self._checkpoint['n_trials'] = n_trials
        self._checkpoint['ga_generations'] = ga_generations
        self._checkpoint['ga_population'] = ga_population

        resumed_phases = ', '.join(sorted(self._completed_phases)) if self._completed_phases else '全新'
        print("=" * 70)
        print("  V6.2.3 全量高算力优化管道（HEAVY COMPUTE）")
        print("=" * 70)
        print(f"  搜索空间: {len(space)} 个参数")
        print(f"  管道: Optuna({n_trials}) → GA({ga_generations}代×{ga_population}) → FineGrid → WFV({wf_top_k})")
        if ADAPTIVE_ENABLED and HAS_ADAPTIVE:
            _parallel_desc = f"自适应（受面板 CPU 限制控制，初始上限 {n_jobs}）"
        else:
            _parallel_desc = f"{n_jobs} 进程"
        print(f"  并行: {_parallel_desc} | GA专用: {ga_n_jobs} 进程 | 断点续算: {resumed_phases}")
        print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        t_total_start = time.time()
        _update_progress(phase='准备', label='加载数据与构建缓存', pct=0.0)
        self._save_and_prep()

        # ── 划分保留验证段（最后 val_ratio 数据，用于三阶段搜索早停监控）──
        self._val_df, _val_note = self._make_val_split(val_ratio=0.15)
        if self._val_df is not None and verbose:
            print(f"  保留验证段（早停监控）: {_val_note}")

        # ──────── Phase 1: Optuna 贝叶斯优化 ────────
        if 'optuna' in self._completed_phases:
            print(f"\n{'─'*70}")
            print(f"  Phase 1/4: Optuna 已跳过（断点续算，results 已从 pickle 恢复）")
            print(f"{'─'*70}")
            _optuna_restored = self.phase_results.get('optuna', [])
            print(f"  Phase 1 results: {len(_optuna_restored)} 条")
        elif HAS_OPTUNA and n_trials > 0:
            print(f"\n{'─'*70}")
            print(f"  Phase 1/4: Optuna 贝叶斯优化 ({n_trials} trials)")
            print(f"{'─'*70}")
            _report_progress('Phase 1/4: Optuna 贝叶斯搜索开始...')
            t0 = time.time()

            _prepare_data_cache(self.df)
            evaluator = _HeavyOptunaEvaluator(self.start_date, space)

            sampler = optuna.samplers.TPESampler(
                seed=42,
                n_startup_trials=max(30, n_trials // 20),
            )

            # ─── 存储后端选择 ───
            # InMemoryStorage: 零 I/O，仅 n_jobs=1 时使用
            # JournalStorage: 支持多进程 + 断点续算（多进程模式强制使用）
            _journal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          '.heavy_optuna_journal.log')
            _use_in_memory = (n_jobs == 1) and (not resume or not os.path.exists(_journal_path))
            if ADAPTIVE_ENABLED and HAS_ADAPTIVE:
                # 自适应进程调度需要多进程共享结果 → 强制 JournalStorage
                _use_in_memory = False

            if _use_in_memory:
                print(f"  存储: InMemoryStorage（n_jobs=1, 零 I/O）")
            elif ADAPTIVE_ENABLED and HAS_ADAPTIVE:
                print(f"  存储: JournalStorage（自适应进程 + 断点续算）")
            else:
                print(f"  存储: JournalStorage（{n_jobs}进程并行 + 断点续算）")

            # ─── 断点续算处理 ───
            if not _use_in_memory and os.path.exists(_journal_path):
                _tmp_storage = JournalStorage(JournalFileBackend(_journal_path))
                _tmp_study = optuna.create_study(
                    study_name='heavy_optuna_search',
                    storage=_tmp_storage,
                    load_if_exists=True,
                    direction='maximize',
                )
                _existing_for_check = sum(
                    1 for t in _tmp_study.trials
                    if t.state == optuna.trial.TrialState.COMPLETE
                )
            else:
                _existing_for_check = 0

            if resume and _existing_for_check > 0:
                remaining = max(0, n_trials - _existing_for_check)
                print(f"\n  断点续算: {_existing_for_check}/{n_trials} trials 已完成, 剩余 {remaining}")
            elif _existing_for_check > 0 and not resume:
                if os.path.exists(_journal_path):
                    os.remove(_journal_path)
                remaining = n_trials
            else:
                remaining = n_trials

            # ─── 执行优化：ProcessPoolExecutor 绕过 GIL ───
            # Optuna 的 n_jobs > 1 使用 ThreadPoolExecutor（线程池），
            # 受 GIL 限制，CPU 密集型任务无法真正并行。
            # 改用 ProcessPoolExecutor（进程池），每个 worker 进程独立运行 trials，
            # 通过 JournalStorage 共享结果，实现真正的多核并行。
            if remaining > 0:
                _update_progress(phase='Phase 1/4', label='Optuna 贝叶斯搜索',
                                 current=0, total=remaining)
                if n_jobs > 1 and not (ADAPTIVE_ENABLED and HAS_ADAPTIVE):
                    # ── 固定进程池并行（原逻辑）：每个 worker 进程分配一批 trials ──
                    _trials_per_worker = remaining // n_jobs
                    _worker_args = []
                    for i in range(n_jobs):
                        _n = _trials_per_worker + (1 if i < remaining % n_jobs else 0)
                        if _n > 0:
                            _worker_args.append((
                                _n,
                                None if _use_in_memory else _journal_path,
                                self.start_date,
                                space,
                                resume,
                                42,  # TPE seed
                            ))

                    ctx = multiprocessing.get_context('spawn')
                    with ProcessPoolExecutor(max_workers=n_jobs, mp_context=ctx) as _pool:
                        _futures = [_pool.submit(_phase1_worker, a) for a in _worker_args]
                        # GUI 模式不再打印进度（走独立进度面板）；CLI 保留 tqdm
                        _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
                        _pbar = (tqdm(total=remaining, desc="Phase 1 (processes)",
                                      unit="trial") if _use_tqdm else None)
                        _done_acc = 0
                        _futures_set = set(_futures)
                        while _futures_set:
                            # ── 暂停/停止信号检查（as_completed 带超时轮询）──
                            if _check_control() == 'stop':
                                break
                            try:
                                for _f in as_completed(list(_futures_set), timeout=0.5):
                                    _futures_set.discard(_f)
                                    try:
                                        _done = _f.result() or 0
                                        _done_acc += _done
                                        if _pbar is not None:
                                            _pbar.update(_done)
                                        _update_progress(
                                            phase='Phase 1/4', label='Optuna 贝叶斯搜索',
                                            current=min(_done_acc, remaining),
                                            total=remaining)
                                    except Exception as _e:
                                        print(f"  [worker 异常] {_e}")
                            except TimeoutError:
                                continue
                        if _pbar is not None:
                            _pbar.close()

                    # 从 journal 读取所有结果（主进程 study 需要加载 journal）
                    _result_storage = JournalStorage(JournalFileBackend(_journal_path))
                    study = optuna.create_study(
                        study_name='heavy_optuna_search',
                        storage=_result_storage,
                        load_if_exists=True,
                        direction='maximize',
                        sampler=sampler,
                    )
                elif ADAPTIVE_ENABLED and HAS_ADAPTIVE:
                    # ── 自适应进程调度：把 trials 切成小批次分块 ──
                    # worker 数量由 CpuGovernor 实时增减；块越小，降限制时
                    # worker 在块间退出的延迟越低（块内已完成 trial 已写入
                    # journal，不会丢失）。
                    _chunk_size = max(2, min(15, remaining // max(n_jobs * 40, 1)))
                    _chunk_args = []
                    _rem = remaining
                    _seed_idx = 0
                    while _rem > 0:
                        _n = min(_chunk_size, _rem)
                        _chunk_args.append((
                            _n,
                            _journal_path,
                            self.start_date,
                            space,
                            resume,
                            42 + _seed_idx,   # 每块独立 seed，避免重复采样
                        ))
                        _seed_idx += 1
                        _rem -= _n

                    with AdaptiveWorkerPool(_phase1_worker, governor=GOVERNOR,
                                            state=DASHBOARD_STATE,
                                            pool_name='Phase1') as _pool:
                        _futures = [_pool.submit(a) for a in _chunk_args]
                        # GUI 模式不再打印进度（走独立进度面板）；CLI 保留 tqdm
                        _use_tqdm = HAS_TQDM and verbose and not ADAPTIVE_ENABLED
                        _pbar = (tqdm(total=remaining, desc="Phase 1 (自适应)",
                                      unit="trial") if _use_tqdm else None)
                        _done_acc = 0
                        for _f in _futures:
                            # ── 暂停/停止信号检查（轮询式；原实现无检查点，
                            #    暂停/停止在此阶段完全无效，是"停不下来"根因）──
                            _res, _stopped = _wait_future(_f)
                            if _stopped:
                                try:
                                    _pool.abort()
                                except Exception:
                                    pass
                                break
                            try:
                                _done = _res or 0
                                _done_acc += _done
                                if _pbar is not None:
                                    _pbar.update(_done)
                                _update_progress(phase='Phase 1/4', label='Optuna 贝叶斯搜索',
                                                 current=min(_done_acc, remaining),
                                                 total=remaining)
                            except Exception as _e:
                                print(f"  [worker 异常] {_e}")
                        if _pbar is not None:
                            _pbar.close()

                    # 从 journal 读取所有结果
                    _result_storage = JournalStorage(JournalFileBackend(_journal_path))
                    study = optuna.create_study(
                        study_name='heavy_optuna_search',
                        storage=_result_storage,
                        load_if_exists=True,
                        direction='maximize',
                        sampler=sampler,
                    )
                else:
                    # 单进程：直接运行
                    storage = optuna.storages.InMemoryStorage() if _use_in_memory \
                        else JournalStorage(JournalFileBackend(_journal_path))
                    study = optuna.create_study(
                        study_name='heavy_optuna_search',
                        storage=storage,
                        load_if_exists=not _use_in_memory,
                        direction='maximize',
                        sampler=sampler,
                        pruner=optuna.pruners.MedianPruner(
                            n_startup_trials=20, n_warmup_steps=10,
                        ),
                    )
                    # ── 单进程分支循环化：逐 trial 检查暂停/停止信号 ──
                    # 保持同一 study 对象复用，TPE 采样器状态不变
                    for _i in range(remaining):
                        if _check_control() == 'stop':
                            break
                        study.optimize(evaluator, n_trials=1, n_jobs=1,
                                       show_progress_bar=HAS_TQDM and verbose)
            else:
                # remaining == 0：所有 trials 已完成（断点续算场景），从 journal 加载结果
                if not _use_in_memory and os.path.exists(_journal_path):
                    _result_storage = JournalStorage(JournalFileBackend(_journal_path))
                    study = optuna.create_study(
                        study_name='heavy_optuna_search',
                        storage=_result_storage,
                        load_if_exists=True,
                        direction='maximize',
                        sampler=sampler,
                    )

            phase1_results = []
            for t in study.trials:
                if t.state == optuna.trial.TrialState.COMPLETE and t.value is not None:
                    r = TrialResult(
                        params={k: v for k, v in t.params.items()},
                        strategy_return=t.user_attrs.get('strategy_return', 0),
                        sharpe_ratio=t.user_attrs.get('sharpe_ratio', 0),
                        max_drawdown=t.user_attrs.get('max_drawdown', 0),
                        calmar_ratio=t.user_attrs.get('calmar_ratio', 0),
                        total_trades=t.user_attrs.get('total_trades', 0),
                        win_rate=t.user_attrs.get('win_rate', 0),
                        objective=t.value,
                    )
                    phase1_results.append(r)
            self.phase_results['optuna'] = phase1_results
            self.all_results.extend(phase1_results)

            elapsed = time.time() - t0
            if verbose and phase1_results:
                best = max(phase1_results, key=lambda x: x.objective)
                print(f"  Phase 1 完成: {len(phase1_results)} 有效, "
                      f"Best Obj={best.objective:.2f}, "
                      f"Return={best.strategy_return*100:.2f}%, "
                      f"耗时 {elapsed/60:.0f}min")
            _report_progress(f'Phase 1/4 完成: {len(phase1_results)} 有效结果')
            _update_progress(phase='Phase 1/4', label='已完成', current=1, total=1,
                             pct=100.0, detail=f'{len(phase1_results)} 有效结果')

            # Phase 1 完成，保存断点
            self._completed_phases.add('optuna')
            print(f"  正在保存 checkpoint...")
            self._save_checkpoint()
            print(f"  checkpoint 保存完成，即将进入 Phase 2...")

            # ── 早停监控：更新验证段基线 ──
            if self._val_df is not None:
                _v1 = self._update_val_baseline(top_k=10)
                self._val_baseline_p1 = self._val_baseline  # 记录 Phase 1 基线
                if verbose and _v1 is not None:
                    print(f"  [早停] Phase 1 验证段 Top-10 最优目标: {_v1:.2f}")
                elif verbose:
                    print(f"  [早停] 验证段评估失败，将不触发早停")

            # ── 阶段间停止检查 ──
            if _check_control() == 'stop':
                print("  ⏹ 已收到优雅停止请求，保存进度后退出...")
                _report_progress('⏹ 已优雅停止（Phase 1 完成后）')
                self._save_checkpoint()
                return

        # ── 释放 Phase 1 主进程内存（study/journal 已持久化），
        #    降低 GA 阶段 spawn 新 worker 时的内存峰值 ──
        try:
            del study
        except (NameError, UnboundLocalError):
            pass
        try:
            del _result_storage
        except (NameError, UnboundLocalError):
            pass
        try:
            del _tmp_storage
        except (NameError, UnboundLocalError):
            pass
        try:
            del _tmp_study
        except (NameError, UnboundLocalError):
            pass
        gc.collect()

        # ──────── Phase 2: 遗传算法精炼 ────────
        if 'genetic' in self._completed_phases:
            print(f"\n{'─'*70}")
            print(f"  Phase 2/4: 遗传算法 已跳过（断点续算）")
            print(f"{'─'*70}")
        elif ga_generations > 0:
            print(f"\n{'─'*70}")
            print(f"  Phase 2/4: 遗传算法精炼 ({ga_generations}代 × {ga_population})")
            print(f"{'─'*70}")
            _report_progress('Phase 2/4: 遗传算法精炼开始...')
            _update_progress(phase='Phase 2/4', label='遗传算法精炼',
                             current=0, total=ga_generations)
            t0 = time.time()

            # 用 Phase 1 的 Top-K 初始化种群
            _prepare_data_cache(self.df)
            ga = GeneticOptimizer(self.df, self.start_date)
            ga._save_config = lambda: None
            ga._restore_config = lambda: None

            ga.run(
                space=space,
                population_size=ga_population,
                generations=ga_generations,
                n_jobs=ga_n_jobs,
                verbose=verbose,
                val_eval_fn=self._val_eval_fn if self._val_df is not None else None,
                val_stagnant_limit=5,
                val_tolerance=1.0,
            )
            phase2_results = list(ga.results)
            self.phase_results['genetic'] = phase2_results
            self.all_results.extend(phase2_results)

            elapsed = time.time() - t0
            if verbose and ga.results:
                best = max(ga.results, key=lambda x: x.objective)
                print(f"  Phase 2 完成: {len(ga.results)} 有效, "
                      f"Best Obj={best.objective:.2f}, "
                      f"Return={best.strategy_return*100:.2f}%, "
                      f"耗时 {elapsed/60:.0f}min")
            _report_progress('Phase 2/4 完成')
            _update_progress(phase='Phase 2/4', label='已完成', current=1, total=1,
                             pct=100.0, detail='遗传算法精炼完成')

            # ── 早停监控：Phase 2 后刷新验证段基线 ──
            if self._val_df is not None:
                _v2 = self._update_val_baseline(top_k=10)
                if verbose and _v2 is not None:
                    print(f"  [早停] Phase 2 验证段 Top-10 最优目标: {_v2:.2f}")
                # ── 跨阶段早停：若验证段较 Phase 1 无实质改善，跳过 Phase 3 ──
                _p1 = getattr(self, '_val_baseline_p1', None)
                if (_v2 is not None and _p1 is not None
                        and _v2 < _p1 + 1.0):
                    print(f"  ⏹ 验证段较 Phase 1 无改善（{_p1:.2f} → {_v2:.2f}），"
                          f"跳过 Phase 3 局部网格")
                    self._completed_phases.add('local_grid')

            # Phase 2 完成
            self._completed_phases.add('genetic')
            self._save_checkpoint()

            # ── 阶段间停止检查 ──
            if _check_control() == 'stop':
                print("  ⏹ 已收到优雅停止请求，保存进度后退出...")
                _report_progress('⏹ 已优雅停止（Phase 2 完成后）')
                self._save_checkpoint()
                return

        # ──────── Phase 3: 局部精细网格 ────────
        if 'local_grid' in self._completed_phases:
            print(f"\n{'─'*70}")
            print(f"  Phase 3/4: 局部精细网格 已跳过（断点续算）")
            print(f"{'─'*70}")
        else:
            print(f"\n{'─'*70}")
            print(f"  Phase 3/4: 局部精细网格（Top 5 周围）")
            print(f"{'─'*70}")
            _report_progress('Phase 3/4: 局部精细网格开始...')
            t0 = time.time()

            all_sorted = sorted(self.all_results, key=lambda x: x.objective, reverse=True)
            top_5 = all_sorted[:5]

            grid_tasks = []
            for base in top_5:
                for _ in range(50):
                    local_params = {}
                    for name, spec in space.items():
                        lo, hi = spec['range']
                        rng = hi - lo
                        base_val = base.params.get(name, (lo + hi) / 2)
                        sigma = rng * 0.03
                        new_val = base_val + random.gauss(0, sigma)
                        new_val = max(lo, min(hi, new_val))
                        if spec['type'] == 'int':
                            step = spec.get('step', 1)
                            new_val = int(round(new_val / step) * step)
                        else:
                            if spec.get('step'):
                                new_val = round(new_val / spec['step']) * spec['step']
                            new_val = round(new_val, 6)
                        local_params[name] = new_val
                    grid_tasks.append(local_params)

            grid_opt = CoarseToFineGridOptimizer(self.df, self.start_date)
            tasks = [grid_opt._build_task(p) for p in grid_tasks]
            _update_progress(phase='Phase 3/4', label='局部精细网格',
                             current=0, total=len(grid_tasks))

            def _p3_cb(done, total):
                _update_progress(phase='Phase 3/4', label='局部精细网格',
                                 current=done, total=total)

            p3_results = grid_opt._run_evaluations(tasks, n_jobs=n_jobs,
                                                   verbose=verbose,
                                                   progress_cb=_p3_cb)
            self.phase_results['local_grid'] = p3_results
            self.all_results.extend(p3_results)

            elapsed = time.time() - t0
            if verbose and p3_results:
                best = max(p3_results, key=lambda x: x.objective)
                print(f"  Phase 3 完成: {len(p3_results)} 有效, "
                      f"Best Obj={best.objective:.2f}, Return={best.strategy_return*100:.2f}%, "
                      f"耗时 {elapsed:.0f}s")
            _report_progress('Phase 3/4 完成')
            _update_progress(phase='Phase 3/4', label='已完成', current=1, total=1,
                             pct=100.0, detail='局部精细网格完成')

            # Phase 3 完成
            self._completed_phases.add('local_grid')
            self._save_checkpoint()

            # ── 早停监控：Phase 3 后刷新验证段基线 ──
            if self._val_df is not None:
                _v3 = self._update_val_baseline(top_k=10)
                if verbose and _v3 is not None:
                    print(f"  [早停] Phase 3 验证段 Top-10 最优目标: {_v3:.2f}")

            # ── 阶段间停止检查 ──
            if _check_control() == 'stop':
                print("  ⏹ 已收到优雅停止请求，保存进度后退出...")
                _report_progress('⏹ 已优雅停止（Phase 3 完成后）')
                self._save_checkpoint()
                return

        # ──────── Phase 4: Walk-Forward 验证（并行）────────
        if 'walkforward' in self._completed_phases:
            print(f"\n{'─'*70}")
            print(f"  Phase 4/4: Walk-Forward 已跳过（断点续算）")
            print(f"{'─'*70}")
        else:
            print(f"\n{'─'*70}")
            print(f"  Phase 4/4: Walk-Forward 交叉验证（Top {wf_top_k} 候选，{n_jobs} 并行）")
            print(f"{'─'*70}")
            _report_progress('Phase 4/4: Walk-Forward 验证开始...')
            t0 = time.time()

            final_sorted = sorted(self.all_results, key=lambda x: x.objective, reverse=True)
            candidates = final_sorted[:wf_top_k]
            _update_progress(phase='Phase 4/4', label='Walk-Forward 交叉验证',
                             current=0, total=len(candidates))

            def _p4_cb(done, total):
                _update_progress(phase='Phase 4/4', label='Walk-Forward 交叉验证',
                                 current=done, total=total)

            # 并行 Walk-Forward
            wf_tasks = [(self.df, c.params, returns_aggressive_objective) for c in candidates]
            wf_results_raw = _run_wf_parallel(wf_tasks, n_jobs=n_jobs, verbose=verbose,
                                              progress_cb=_p4_cb)

            for wf in wf_results_raw:
                if wf is not None:
                    self.wf_results.append(wf)

            if self.wf_results:
                self.wf_results.sort(key=lambda x: x.combined_score, reverse=True)

            elapsed = time.time() - t0
            n_passed = sum(1 for w in self.wf_results if w.val_objective > 0)
            if verbose:
                print(f"  Phase 4 完成: {len(self.wf_results)}/{len(candidates)} 有效"
                      f"（{n_passed} 通过样本外验证）, 耗时 {elapsed:.0f}s")
            _report_progress('Phase 4/4 完成')
            _update_progress(phase='Phase 4/4', label='已完成', current=1, total=1,
                             pct=100.0, detail='Walk-Forward 验证完成')

            # Phase 4 完成（全部结束，清理断点）
            self._completed_phases.add('walkforward')
            self._save_checkpoint()

        # ──────── 汇总 ────────
        self._restore()
        total_elapsed = time.time() - t_total_start

        # 全部四阶段完成，清理断点 + journal + results pickle
        if len(self._completed_phases) >= 4:
            _results_pkl_path = self._checkpoint_path.replace('.json', '_results.pkl')
            for _f in [self._checkpoint_path,
                       _results_pkl_path,
                       os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    '.heavy_optuna_journal.log')]:
                try:
                    if os.path.exists(_f):
                        os.remove(_f)
                except:
                    pass

        print(f"\n{'='*70}")
        print(f"  HEAVY COMPUTE 管道完成")
        print(f"  总耗时: {total_elapsed/60:.0f}min | 总评估: {len(self.all_results)} 次")
        print(f"{'='*70}")
        _update_progress(phase='', label='全部完成', current=1, total=1,
                         pct=100.0, detail='HEAVY COMPUTE 管道完成')

        if self.wf_results:
            self._print_final_report()

        # 自动导出结果到 JSON（关机后也能查看）
        self._export_results_json(total_elapsed, out_path=output_path)

    def _export_results_json(self, total_elapsed, out_path=None):
        """导出所有结果到 JSON（默认 heavy_results.json，可自定义路径）"""
        import json as _json

        if out_path:
            # 确保父目录存在；允许绝对/相对路径
            _dir = os.path.dirname(os.path.abspath(out_path))
            if _dir and not os.path.exists(_dir):
                os.makedirs(_dir, exist_ok=True)
            _final_path = out_path
        else:
            _final_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'heavy_results.json')

        # Phase 汇总
        phase_summary = {}
        for phase_name, results in self.phase_results.items():
            if results:
                best = max(results, key=lambda x: x.objective)
                phase_summary[phase_name] = {
                    'objective': round(best.objective, 2),
                    'return_pct': round(best.strategy_return * 100, 2),
                    'sharpe': round(best.sharpe_ratio, 3),
                    'max_dd_pct': round(best.max_drawdown * 100, 2),
                    'total_trades': best.total_trades,
                    'n_results': len(results),
                }

        # Top-20 全参数 —— 以 Walk-Forward 验证结果作为最终筛选依据
        # 排序优先级：
        #   1) WF 验证通过（val_objective > 0）→ 按 combined_score 降序
        #   2) 有 WF 但验证未通过            → 按 val_objective 降序（供诊断）
        #   3) 无 WF 验证                    → 按训练 objective 降序
        wf_index = {}
        for _wf in self.wf_results:
            _k = tuple(sorted(_wf.params.items()))
            if _k not in wf_index:  # 保留首个（combined_score 已降序）
                wf_index[_k] = _wf

        def _top20_key(r):
            _wf = wf_index.get(tuple(sorted(r.params.items())))
            if _wf is None:
                return (2, 0.0, -r.objective)
            if _wf.val_objective > 0:
                return (0, -_wf.combined_score, -r.objective)
            return (1, -_wf.val_objective, -r.objective)

        top20 = sorted(self.all_results, key=_top20_key)[:20]
        top20_export = []
        for r in top20:
            _wf = wf_index.get(tuple(sorted(r.params.items())))
            entry = {
                'objective': round(r.objective, 2),
                'return_pct': round(r.strategy_return * 100, 2),
                'excess_pct': round(r.excess_return * 100, 2),
                'annualized_pct': round(r.annualized_return * 100, 2),
                'sharpe': round(r.sharpe_ratio, 3),
                'max_dd_pct': round(r.max_drawdown * 100, 2),
                'calmar': round(r.calmar_ratio, 3),
                'win_rate_pct': round(r.win_rate * 100, 1),
                'total_trades': r.total_trades,
                'params': r.params,
            }
            if _wf is not None:
                entry['wf_combined_score'] = round(_wf.combined_score, 2)
                entry['wf_val_objective'] = round(_wf.val_objective, 2)
                entry['wf_robustness'] = round(_wf.robustness, 3)
                entry['wf_passed'] = bool(_wf.val_objective > 0)
            else:
                entry['wf_passed'] = None  # 未验证
            top20_export.append(entry)

        # Walk-Forward 结果（附带通过标记）
        wf_export = []
        for wf in self.wf_results:
            wf_export.append({
                'combined_score': round(wf.combined_score, 2),
                'val_objective': round(wf.val_objective, 2),
                'robustness': round(wf.robustness, 3),
                'passed': bool(wf.val_objective > 0),
                'val_returns_pct': [round(r.strategy_return * 100, 2)
                                    for r in wf.val_results],
                'params': wf.params,
            })

        output = {
            'meta': {
                'version': 'V6.2.3 Heavy',
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_elapsed_min': round(total_elapsed / 60, 1),
                'total_evaluations': len(self.all_results),
            },
            'phase_summary': phase_summary,
            'top20': top20_export,
            'walk_forward': wf_export,
        }

        with open(_final_path, 'w', encoding='utf-8') as f:
            _json.dump(output, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n  结果已保存到: {_final_path}")

    def _prepare_df_json_heavy(self):
        """准备 pickle 数据缓存"""
        _prepare_data_cache(self.df)

    def _print_final_report(self):
        best_wf = self.wf_results[0]

        # WF 通过统计
        n_passed = sum(1 for w in self.wf_results if w.val_objective > 0)
        n_total = len(self.wf_results)

        print(f"\n{'='*70}")
        print(f"  最终推荐参数（Walk-Forward 验证最优）")
        print(f"{'='*70}")
        if n_total > 0 and n_passed == 0:
            print(f"  ⚠️ 警告: {n_total} 个候选全部未通过样本外验证"
                  f"（val_objective ≤ 0），参数疑似过拟合，不建议直接采用")
        else:
            print(f"  ✅ {n_passed}/{n_total} 个候选通过样本外验证")
        print(f"  验证集目标分: {best_wf.val_objective:.2f}")
        print(f"  稳健性评分:   {best_wf.robustness:.3f}")
        print(f"  综合得分:     {best_wf.combined_score:.2f}")
        print(f"\n  参数列表:")
        for k, v in sorted(best_wf.params.items()):
            print(f"    {k}: {v}")

        # 各验证窗口详情
        if best_wf.val_results:
            print(f"\n  各窗口验证结果:")
            for i, vr in enumerate(best_wf.val_results):
                print(f"    Win{i+1}: Return={vr.strategy_return*100:.2f}%, "
                      f"Sharpe={vr.sharpe_ratio:.3f}, DD={vr.max_drawdown*100:.1f}%")

        # Phase 对比
        print(f"\n  各阶段最优对比:")
        for phase_name, results in self.phase_results.items():
            if results:
                best = max(results, key=lambda x: x.objective)
                print(f"    {phase_name:<15}: Obj={best.objective:>8.2f}  "
                      f"Return={best.strategy_return*100:>7.2f}%  "
                      f"DD={best.max_drawdown*100:>6.2f}%")

        # 自动应用
        if '--apply' in sys.argv:
            self._apply_wf_best(best_wf)

    def _apply_wf_best(self, best_wf):
        """将 Walk-Forward 最优参数写入 config.py"""
        import re

        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
        if not os.path.exists(config_path):
            print("config.py 未找到")
            return

        with open(config_path, 'r', encoding='utf-8') as f:
            source = f.read()

        updated = 0
        for param_name, new_value in best_wf.params.items():
            # 跳过结构体参数
            if param_name.startswith(('BUY_T', 'SELL_T', 'EVIDENCE_WEIGHT_', 'PSYCH_')):
                continue
            if not hasattr(config, param_name):
                continue
            pattern = rf'^({param_name}\s*=\s*)([^\n#]+)(.*)$'
            match = re.search(pattern, source, re.MULTILINE)
            if not match:
                continue

            if isinstance(new_value, float):
                new_str = str(round(new_value, 4))
            elif isinstance(new_value, int):
                new_str = str(new_value)
            else:
                new_str = repr(new_value)

            source = source[:match.start()] + f'{param_name} = {new_str}{match.group(3)}' + source[match.end():]
            updated += 1

        if updated > 0:
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(source)
            print(f"\n已写入 config.py ({updated} 个参数)")

        # 打印需要手动设置的参数
        manual_params = [k for k in best_wf.params
                         if k.startswith(('BUY_T', 'SELL_T', 'EVIDENCE_WEIGHT_', 'PSYCH_'))]
        if manual_params:
            print(f"\n以下参数需要手动更新 config.py:")
            for mp in manual_params:
                print(f"  {mp} = {best_wf.params[mp]}")


# ============================================================
# H5：Heavy Optuna Evaluator（支持扩展空间）
# ============================================================

class _HeavyOptunaEvaluator:
    """高算力版 Optuna Evaluator —— 支持 50+ 参数 + 仓位曲线重建"""

    def __init__(self, start_date, space):
        self.start_date = start_date
        self.space = space

    def __call__(self, trial):
        params = {}
        for name, spec in self.space.items():
            if spec['type'] in ('float',):
                params[name] = trial.suggest_float(
                    name, spec['range'][0], spec['range'][1],
                    step=spec.get('step') if spec.get('step') else None
                )
            elif spec['type'] == 'int':
                params[name] = trial.suggest_int(
                    name, spec['range'][0], spec['range'][1],
                    step=spec.get('step', 1)
                )

        task = _build_heavy_task(params, self.start_date)
        result = _heavy_eval_worker(task)
        if result is None:
            return float('-inf')

        trial.set_user_attr('strategy_return', result.strategy_return)
        trial.set_user_attr('sharpe_ratio', result.sharpe_ratio)
        trial.set_user_attr('max_drawdown', result.max_drawdown)
        trial.set_user_attr('calmar_ratio', result.calmar_ratio)
        trial.set_user_attr('total_trades', result.total_trades)
        trial.set_user_attr('win_rate', result.win_rate)

        return result.objective


def _build_heavy_task(params, start_date):
    """构建高算力评估任务（使用 pickle 缓存，无需传递 DataFrame）"""
    import copy as _copy
    task_params = _copy.deepcopy(params)

    # 仓位曲线
    buy_curve, sell_curve = _build_position_curves_from_params(params)

    # Evidence 权重
    ev_weights = _build_evidence_weights_from_params(params)

    return (task_params, start_date, buy_curve, sell_curve, ev_weights)


def _heavy_eval_worker(args_tuple):
    """
    高算力版 Worker —— 支持仓位曲线、Evidence权重、情绪系数的运行时替换
    """
    params, start_date, buy_curve, sell_curve, ev_weights = args_tuple

    try:
        for param_name, value in params.items():
            if hasattr(config, param_name) and not param_name.startswith(
                    ('BUY_T', 'SELL_T', 'EVIDENCE_WEIGHT_', 'PSYCH_')):
                setattr(config, param_name, value)

        if 'BULL_BUY_MULT' in params:
            config.REGIME_WEIGHTS = _build_regime_from_params(params)

        # 仓位曲线
        if buy_curve:
            config.BUY_SCORE_THRESHOLDS = buy_curve
        if sell_curve:
            config.SELL_SCORE_THRESHOLDS = sell_curve

        # Evidence 权重
        if ev_weights:
            config.EVIDENCE_WEIGHTS = ev_weights

        # 情绪修正（strategy.py 运行时从 config 读取）
        # 已在上面通过 setattr 设置好了

        # 从 pickle 缓存加载数据
        df, _ = _load_data_cache()

        # ⚠️ 关键修复：strategy 及依赖模块在导入时用 `from config import *` 捕获了
        # 旧配置，必须重载后才会使用 setattr 的新参数（否则优化结果失真）
        strategy_mod, backtest_mod = _reload_config_capture_modules()

        strategy = strategy_mod.V6Strategy()
        signals = strategy.run(df)
        bt = backtest_mod.V6Backtest(df, start_date=start_date)
        bt_results = bt.run(signals)

        if not bt_results:
            return None

        result = TrialResult.from_backtest(params, bt_results, -1.0)
        result.objective = returns_aggressive_objective(
            annualized_return=result.annualized_return,
            strategy_return=result.strategy_return,
            excess_return=result.excess_return,
            max_drawdown=result.max_drawdown,
            sharpe_ratio=result.sharpe_ratio,
            calmar_ratio=result.calmar_ratio,
            total_trades=result.total_trades,
            win_rate=result.win_rate,
            volatility=result.volatility,
        )
        return result

    except Exception:
        return None


def _phase1_worker(args):
    """Phase 1 进程级 worker —— 在独立进程中运行一批 Optuna trials。

    每个 worker 进程拥有自己的 study 实例，通过 JournalStorage 共享试验结果。
    使用 ProcessPoolExecutor 绕过 GIL，实现真正的 CPU 并行。
    """
    (n_trials_for_worker, journal_path, start_date, space, resume, seed) = args[:6]
    study_name = args[6] if len(args) > 6 else 'heavy_optuna_search'

    # ── 导入（spawn 子进程首次加载时执行，后续复用）──
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)  # 避免 [I] 日志刷屏
    try:
        from optuna.storages.journal import JournalStorage, JournalFileBackend
    except ImportError:
        from optuna.storages import JournalStorage, JournalFileBackend

    # ── 存储 ──
    if journal_path:
        storage = JournalStorage(JournalFileBackend(journal_path))
    else:
        storage = optuna.storages.InMemoryStorage()

    # ── 创建 study（每个进程独立实例，通过 journal 共享结果）──
    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        load_if_exists=True,
        direction='maximize',
        sampler=optuna.samplers.TPESampler(
            seed=seed,
            n_startup_trials=max(30, n_trials_for_worker // 10),
        ),
        pruner=optuna.pruners.MedianPruner(
            n_startup_trials=20, n_warmup_steps=10,
        ),
    )

    # ── 评估器 ──
    evaluator = _HeavyOptunaEvaluator(start_date, space)

    # ── 运行 trials（逐 trial 循环，支持暂停/停止信号检查）──
    # 保持同一 study 对象复用，TPE 采样器状态不变
    for _i in range(n_trials_for_worker):
        if _check_control() == 'stop':
            break
        study.optimize(evaluator, n_trials=1, n_jobs=1, show_progress_bar=False)

    return len([t for t in study.trials
                if t.state == optuna.trial.TrialState.COMPLETE])


# ============================================================
# H6：Heavy Compute 入口
# ============================================================

def run_heavy_optimization(resume=True, output_path=None, **kwargs):
    """全量高算力优化入口（支持断点续算）

    Args:
        resume: 是否启用断点续算
        output_path: 结果 JSON 导出路径（None = 脚本目录 heavy_results.json）
        **kwargs: 其余参数透传 HeavyOptimizer.run
    """
    df = load_data_from_db()
    if df is None:
        print("无法加载数据")
        return None

    print(f"数据: {len(df)} 行, {df.index[0].date()} ~ {df.index[-1].date()}")

    heavy = HeavyOptimizer(df)
    heavy.run(resume=resume, output_path=output_path, **kwargs)
    return heavy


def run_light_optimization(n_trials=300, n_jobs=14, output_path=None,
                           verbose=True):
    """轻量运算：单阶段 Optuna 快速搜索（自适应并行），结果导出为 JSON。

    - 与全量模式（HeavyOptimizer）共用 _phase1_worker 评估器与搜索空间；
    - 每次全新运行（清理旧 journal），保证结果可复现，适合迭代调试；
    - 受面板 CPU 限制控制（ADAPTIVE_ENABLED 时走 AdaptiveWorkerPool）。

    Args:
        n_trials: Optuna 试验次数
        n_jobs: 并行参考数（自适应模式仅影响任务分块）
        output_path: 结果 JSON 导出路径（None = 脚本目录 light_results.json）
        verbose: 是否打印进度
    """
    df = load_data_from_db()
    if df is None:
        print("无法加载数据")
        return None
    start_date = str(df.index[0].date())
    space = dict(HEAVY_SEARCH_SPACE)

    print("=" * 60)
    print("  轻量运算（单阶段 Optuna 快速搜索）")
    print("=" * 60)
    print(f"  数据: {len(df)} 行, {df.index[0].date()} ~ {df.index[-1].date()}")
    print(f"  搜索空间: {len(space)} 个参数 | 试验: {n_trials}")
    if ADAPTIVE_ENABLED and HAS_ADAPTIVE:
        print(f"  并行: 自适应（受面板 CPU 限制控制，初始上限 {n_jobs}）")
    else:
        print(f"  并行: {n_jobs} 进程")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    _prepare_data_cache(df)

    # 轻量模式每次全新运行：清理旧 journal，保证结果可复现
    _journal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '.light_optuna_journal.log')
    for _f in (_journal_path, _journal_path + '.lock'):
        try:
            if os.path.exists(_f):
                os.remove(_f)
        except OSError:
            pass

    results = []
    if HAS_OPTUNA and n_trials > 0:
        t0 = time.time()
        # 任务分块（与 Heavy Phase 1 相同策略）：块越小，降限制时退出越快
        _chunk_size = max(2, min(15, n_trials // max(n_jobs * 40, 1)))
        _chunks = []
        _rem, _seed_idx = n_trials, 0
        while _rem > 0:
            _n = min(_chunk_size, _rem)
            _chunks.append((_n, _journal_path, start_date, space, False,
                            42 + _seed_idx, 'light_optuna_search'))
            _seed_idx += 1
            _rem -= _n
        _update_progress(phase='轻量运算', label='Optuna 快速搜索',
                         current=0, total=n_trials)
        _done_acc = 0

        if ADAPTIVE_ENABLED and HAS_ADAPTIVE:
            # ── 自适应进程调度（受面板 CPU 限制控制）──
            with AdaptiveWorkerPool(_phase1_worker, governor=GOVERNOR,
                                    state=DASHBOARD_STATE,
                                    pool_name='Light') as _pool:
                _futures = [_pool.submit(c) for c in _chunks]
                for _f in _futures:
                    # ── 暂停/停止信号检查（轮询式）──
                    _res, _stopped = _wait_future(_f)
                    if _stopped:
                        try:
                            _pool.abort()
                        except Exception:
                            pass
                        break
                    try:
                        _done_acc += _res or 0
                        _update_progress(phase='轻量运算', label='Optuna 快速搜索',
                                         current=min(_done_acc, n_trials),
                                         total=n_trials)
                    except Exception as _e:
                        print(f"  [worker 异常] {_e}")
        elif n_jobs > 1:
            # ── 固定进程池并行 ──
            ctx = multiprocessing.get_context('spawn')
            with ProcessPoolExecutor(max_workers=min(n_jobs, len(_chunks)),
                                     mp_context=ctx) as _pool:
                _futures = [_pool.submit(_phase1_worker, c) for c in _chunks]
                _futures_set = set(_futures)
                while _futures_set:
                    # ── 暂停/停止信号检查（as_completed 带超时轮询）──
                    if _check_control() == 'stop':
                        break
                    try:
                        for _f in as_completed(list(_futures_set), timeout=0.5):
                            _futures_set.discard(_f)
                            try:
                                _done_acc += _f.result() or 0
                            except Exception as _e:
                                print(f"  [worker 异常] {_e}")
                            _update_progress(phase='轻量运算', label='Optuna 快速搜索',
                                             current=min(_done_acc, n_trials),
                                             total=n_trials)
                    except TimeoutError:
                        continue
        else:
            # 单进程直跑
            for c in _chunks:
                # ── 停止信号检查 ──
                if _check_control() == 'stop':
                    break
                _done_acc += _phase1_worker(c) or 0
                _update_progress(phase='轻量运算', label='Optuna 快速搜索',
                                 current=min(_done_acc, n_trials), total=n_trials)

        # 从 journal 收集结果
        storage = JournalStorage(JournalFileBackend(_journal_path))
        study = optuna.create_study(
            study_name='light_optuna_search',
            storage=storage,
            load_if_exists=True,
            direction='maximize',
        )
        for t in study.trials:
            if t.state == optuna.trial.TrialState.COMPLETE and t.value is not None:
                r = TrialResult(
                    params={k: v for k, v in t.params.items()},
                    strategy_return=t.user_attrs.get('strategy_return', 0),
                    sharpe_ratio=t.user_attrs.get('sharpe_ratio', 0),
                    max_drawdown=t.user_attrs.get('max_drawdown', 0),
                    calmar_ratio=t.user_attrs.get('calmar_ratio', 0),
                    total_trades=t.user_attrs.get('total_trades', 0),
                    win_rate=t.user_attrs.get('win_rate', 0),
                    objective=t.value,
                )
                results.append(r)
        results.sort(key=lambda x: x.objective, reverse=True)

        elapsed = time.time() - t0
        if verbose and results:
            best = results[0]
            print(f"  轻量运算完成: {len(results)} 有效, "
                  f"Best Obj={best.objective:.2f}, "
                  f"Return={best.strategy_return*100:.2f}%, 耗时 {elapsed:.0f}s")
        else:
            print(f"  轻量运算完成: 0 有效结果（请检查数据与配置），耗时 {elapsed:.0f}s")
        _report_progress('轻量运算完成')
        _update_progress(phase='轻量运算', label='已完成', current=1, total=1,
                         pct=100.0, detail=f'{len(results)} 有效结果')
    else:
        print("  [跳过] Optuna 未安装，无法执行轻量运算（pip install optuna）")

    # 导出结果
    if results:
        _out_path = output_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'light_results.json')
        _export_light_results(results, _out_path)

    return results


def _export_light_results(results, out_path):
    """轻量运算结果导出为 JSON（格式与 heavy 结果一致：meta/phase_summary/top20）"""
    import json as _json

    results = sorted(results, key=lambda x: x.objective, reverse=True)

    phase_summary = {}
    if results:
        best = results[0]
        phase_summary['optuna'] = {
            'objective': round(best.objective, 2),
            'return_pct': round(best.strategy_return * 100, 2),
            'sharpe': round(best.sharpe_ratio, 3),
            'max_dd_pct': round(best.max_drawdown * 100, 2),
            'total_trades': best.total_trades,
            'n_results': len(results),
        }

    top20_export = []
    for r in results[:20]:
        top20_export.append({
            'objective': round(r.objective, 2),
            'return_pct': round(r.strategy_return * 100, 2),
            'excess_pct': round(r.excess_return * 100, 2),
            'annualized_pct': round(r.annualized_return * 100, 2),
            'sharpe': round(r.sharpe_ratio, 3),
            'max_dd_pct': round(r.max_drawdown * 100, 2),
            'calmar': round(r.calmar_ratio, 3),
            'win_rate_pct': round(r.win_rate * 100, 1),
            'total_trades': r.total_trades,
            'params': r.params,
        })

    output = {
        'meta': {
            'version': 'V6.2.3 Light',
            'mode': 'light',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_evaluations': len(results),
        },
        'phase_summary': phase_summary,
        'top20': top20_export,
    }

    _dir = os.path.dirname(os.path.abspath(out_path))
    if _dir and not os.path.exists(_dir):
        os.makedirs(_dir, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        _json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n  结果已保存到: {out_path}")


def view_saved_results():
    """查看已保存的 heavy_results.json"""
    import json as _json

    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'heavy_results.json')
    if not os.path.exists(json_path):
        print(f"未找到结果文件: {json_path}")
        print("请先运行 --method heavy 完成优化")
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        data = _json.load(f)

    meta = data.get('meta', {})
    print("=" * 70)
    print(f"  Heavy 优化结果（{meta.get('generated_at', '?')}）")
    print(f"  总耗时: {meta.get('total_elapsed_min', '?')}min | "
          f"总评估: {meta.get('total_evaluations', '?')} 次")
    print("=" * 70)

    # Phase 汇总
    phase = data.get('phase_summary', {})
    if phase:
        print(f"\n  {'Phase':<15} {'目标分':>8} {'收益%':>10} {'夏普':>7} {'回撤%':>8} {'交易':>6} {'样本':>6}")
        print(f"  {'-'*65}")
        for name, info in phase.items():
            print(f"  {name:<15} {info['objective']:>8.2f} {info['return_pct']:>9.2f}% "
                  f"{info['sharpe']:>7.3f} {info['max_dd_pct']:>7.2f}% "
                  f"{info['total_trades']:>6} {info['n_results']:>6}")

    # Walk-Forward Top
    wf = data.get('walk_forward', [])
    if wf:
        best_wf = wf[0]
        print(f"\n  Walk-Forward 最优:")
        print(f"    综合得分: {best_wf['combined_score']:.2f}")
        print(f"    稳健性:   {best_wf['robustness']:.3f}")
        print(f"    各窗口收益: {best_wf['val_returns_pct']}")

    # Top-10
    top20 = data.get('top20', [])
    if top20:
        print(f"\n  {'Top10 (按目标分)':<5} {'收益%':>10} {'年化%':>9} {'夏普':>7} {'回撤%':>8}")
        print(f"  {'-'*50}")
        for i, t in enumerate(top20[:10]):
            print(f"  {i+1:>2}.  {t['return_pct']:>9.2f}% {t['annualized_pct']:>8.2f}% "
                  f"{t['sharpe']:>7.3f} {t['max_dd_pct']:>7.2f}%")

        best = top20[0]
        print(f"\n  最优参数:")
        for k, v in sorted(best['params'].items()):
            print(f"    {k}: {v}")

    print()


# ============================================================
# 命令行入口（文件末尾——所有函数定义之后）
# ============================================================

def _run_optimization(args):
    """按 args 执行具体优化（Heavy 或其它方法）"""
    if args.method == 'heavy':
        run_heavy_optimization(
            resume=not args.no_resume,
            n_trials=args.trials,
            ga_generations=args.generations,
            ga_population=args.population,
            n_jobs=args.jobs,
            ga_n_jobs=args.ga_jobs,
            wf_top_k=args.wf_top_k,
        )
        return

    kwargs = {'n_jobs': args.jobs}

    if args.method == 'all' or args.method == 'optuna':
        if args.method != 'all':
            kwargs['n_trials'] = args.trials

    if args.method == 'genetic':
        kwargs['population_size'] = args.population
        kwargs['generations'] = args.generations

    if args.method == 'coarse2fine':
        kwargs['levels'] = args.levels

    if args.method == 'adaptive':
        kwargs['n_random'] = args.n_random
        kwargs['n_local'] = args.n_local

    opt, params = find_best_params(method=args.method, **kwargs)

    if args.dry_run and opt:
        opt.apply_best_params(dry_run=True)


def _shutdown_sequence():
    """无人值守模式：优化完成后自动关机（30 秒倒计时）"""
    print(f"\n{'='*60}")
    print(f"  无人值守模式：优化完成，30秒后自动关机")
    print(f"  如需取消，请在 30 秒内按 Ctrl+C")
    print(f"{'='*60}")
    for remaining in range(30, 0, -1):
        print(f"\r  关机倒计时: {remaining:>2} 秒...", end='', flush=True)
        time.sleep(1)
    print(f"\r  正在关机...                          ")
    os.system('shutdown /s /t 0')


def main(argv=None):
    """命令行入口：python param_optimizer.py [选项]

    使用 `--gui` 时弹出独立控制面板窗口（EXE 模式默认开启）。
    面板中可选择运算模式（全量/轻量）、试验次数与结果保存路径。
    """
    import argparse

    parser = argparse.ArgumentParser(description='V6.2.3 智能参数优化器')
    parser.add_argument('--method', type=str, default='heavy',
                        choices=['optuna', 'genetic', 'coarse2fine', 'adaptive', 'all', 'heavy'],
                        help='优化方法: optuna/genetic/coarse2fine/adaptive/all/heavy (默认heavy)')
    parser.add_argument('--trials', type=int, default=10000,
                        help='Optuna 试验次数 (默认10000; heavy模式默认10000)')
    parser.add_argument('--generations', type=int, default=50,
                        help='遗传算法代数 (默认50; heavy模式默认100)')
    parser.add_argument('--population', type=int, default=40,
                        help='遗传算法种群大小 (默认40; heavy模式默认60)')
    parser.add_argument('--levels', type=int, default=3,
                        help='逐级网格搜索层级 (默认3)')
    parser.add_argument('--n-random', type=int, default=300,
                        help='自适应搜索随机采样数 (默认300)')
    parser.add_argument('--n-local', type=int, default=200,
                        help='自适应搜索局部搜索数 (默认200)')
    parser.add_argument('--jobs', type=int, default=14,
                        help='并行进程数 (默认14)')
    parser.add_argument('--ga-jobs', type=int, default=10,
                        help='遗传算法专用并行进程数 (默认10)')
    parser.add_argument('--apply', action='store_true',
                        help='自动将最优参数写入 config.py')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅打印最优参数，不写入')
    parser.add_argument('--wf-top-k', type=int, default=20,
                        help='[Heavy] Walk-Forward 验证候选数')
    parser.add_argument('--shutdown', action='store_true',
                        help='完成后自动关机（无人值守模式）')
    parser.add_argument('--no-resume', action='store_true',
                        help='[Heavy] 忽略断点，强制全新开始')
    parser.add_argument('--view', action='store_true',
                        help='查看上次 heavy_results.json 结果')
    parser.add_argument('--gui', action='store_true',
                        help='弹出资源控制面板窗口（独立 EXE，无需浏览器）+ 自适应进程调度')
    parser.add_argument('--cpu-limit', type=float, default=100.0,
                        help='最大CPU使用率限制%% (1~100; 100=最大性能模式; 例: --cpu-limit 20)')

    args = parser.parse_args(argv)

    # --view：查看已有结果，不运行优化
    if args.view:
        view_saved_results()
        return 0

    if args.apply:
        sys.argv.append('--apply')

    # 自动关机交互已迁移到 GUI（配置面板"完成后自动关机"勾选）；
    # CLI 模式仅通过 --shutdown 参数显式启用，不再在终端弹 input 询问。
    if args.shutdown and not args.gui:
        print("  已启用：计算完成后自动关机（30秒倒计时，可按 Ctrl+C 取消）")

    # ── GUI 模式：控制面板主线程 + 优化后台线程（用户点击"开始优化"后启动）──
    # 自动关机由面板内"完成后自动关机"勾选框控制（不再由 cmd 询问）
    if args.gui:
        if run_with_gui(cpu_limit=args.cpu_limit):
            return 0
        print("[警告] 已回退到命令行模式继续运行")

    try:
        _run_optimization(args)
    finally:
        if args.shutdown:
            _shutdown_sequence()
    return 0


if __name__ == '__main__':
    import multiprocessing as _mp
    _mp.freeze_support()   # PyInstaller 打包 EXE 后子进程 spawn 必需
    sys.exit(main())
