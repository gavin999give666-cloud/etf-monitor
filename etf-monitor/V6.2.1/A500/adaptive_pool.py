"""
自适应进程池 + CPU 资源治理器（Adaptive Worker Pool & CPU Governor）
=====================================================================
为 param_optimizer.py 的可视化控制面板提供底层模块：

- CpuMonitor        : 采样"程序自身 CPU 占用"（主进程 + 全部子进程合计，
                      按系统总核数换算为 0~100%）与"系统整体 CPU 利用率"。
- CpuGovernor       : AIMD（加性增 / 乘性减）自适应调节算法，根据资源限制
                      动态给出目标 worker 数。
                      * 限制 < 100%：约束的是程序自身 CPU 占用（严格限流）
                      * 限制 = 100%：进入"最大性能模式"，目标是把系统整体
                        CPU 打满到 100%，动态逼近"恰好打满 CPU 的进程数"。
- AdaptiveWorkerPool: 基于 multiprocessing.Queue 的动态进程池。worker 数量
                      由 CpuGovernor 每 ~1s 采样反馈，实时增减。
- DashboardState    : 线程安全的监控指标存储（供 Web 面板 SSE / API 读取）。

一般不需要直接调用 —— param_optimizer.py 通过 `--dashboard` 自动集成。

用法示例：
    from adaptive_pool import CpuGovernor, DashboardState, AdaptiveWorkerPool

    gov = CpuGovernor(limit_pct=20)          # 限制程序最多占用 20% CPU
    st  = DashboardState()
    with AdaptiveWorkerPool(_worker_fn, governor=gov, state=st,
                            pool_name='Eval') as pool:
        fut = pool.submit((params, start_date))   # worker_fn 只接收一个参数
        res = fut.result()
"""

import math
import multiprocessing
import os
import queue
import threading
import time
from collections import deque

import psutil

__all__ = ['CpuMonitor', 'CpuGovernor', 'DashboardState', 'AdaptiveWorkerPool']


# ============================================================
# CPU 采样
# ============================================================

class CpuMonitor:
    """采样程序自身 CPU 占用（主进程 + 全部子进程）与系统整体 CPU 利用率。

    基于 CPU 时间差（user+system）除以墙钟时间差计算，跨进程/跨平台稳定：
    - program_pct: 程序自身占用（相对系统总核数，0~100）
    - system_pct : 系统整体利用率（0~100）
    """

    def __init__(self, cpu_count=None):
        self._cpu_count = cpu_count or os.cpu_count() or 4
        self._proc = psutil.Process()
        self._prev_times = {}      # pid -> cpu_time(user+system)
        self._prev_wall = None
        self._smoothed = 0.0

    def _all_procs(self):
        procs = [self._proc]
        try:
            procs += list(self._proc.children(recursive=True))
        except Exception:
            pass
        return procs

    def sample(self):
        """返回 (program_pct, system_pct)，均为 0~100"""
        now = time.time()
        try:
            system_pct = psutil.cpu_percent(interval=None)
        except Exception:
            system_pct = 0.0
        cur = {}
        cpu_delta = 0.0
        for p in self._all_procs():
            try:
                t = p.cpu_times()
                cpu = t.user + t.system
            except Exception:
                continue
            pid = p.pid
            cur[pid] = cpu
            prev = self._prev_times.get(pid)
            if prev is not None:
                cpu_delta += max(0.0, cpu - prev)
        # 只保留本轮存活进程的基线（自动清理已退出的 worker）
        self._prev_times = cur
        if self._prev_wall is None:
            self._prev_wall = now
            return 0.0, system_pct
        dt = max(1e-6, now - self._prev_wall)
        self._prev_wall = now
        # cpu_delta 是"核心-秒" → 除以 (dt × 核数) 得到系统总能力百分比
        pct = min(100.0, cpu_delta / (dt * self._cpu_count) * 100.0)
        # 轻度平滑，抑制采样抖动
        self._smoothed = self._smoothed * 0.4 + pct * 0.6
        return round(self._smoothed, 1), system_pct


# ============================================================
# AIMD 资源治理器
# ============================================================

class CpuGovernor:
    """AIMD（加性增 / 乘性减）CPU 资源治理器。

    - 资源限制模式（limit < 100%）：
        program_pct > limit + overshoot_band  → worker 数乘性下降（快速回撤）
        program_pct < limit - undershoot_band → worker 数加性增加（+1）
        其余情况保持，配合冷却期抑制震荡。
    - 最大性能模式（limit = 100%）：
        初始取核数；系统 CPU 未饱和则 +1 逼近，过载（>=100%）则退让，
        最终收敛到"恰好把系统 CPU 打满到 100%"的进程数（如 10、12...）。
    """

    def __init__(self, limit_pct=100.0, cpu_count=None, min_workers=1,
                 max_workers=None, sample_interval=1.0, cooldown_s=4.0,
                 inc_step=1, dec_factor=0.75, overshoot_band=3.0,
                 undershoot_band=6.0, max_mode_hold_pct=98.5):
        self._cpu_count = cpu_count or os.cpu_count() or 4
        self._max_hard = max_workers or (self._cpu_count * 2)
        self.min_workers = max(1, min_workers)
        self.sample_interval = max(0.3, float(sample_interval))
        self.cooldown_s = max(2.0, float(cooldown_s))
        self.inc_step = max(1, int(inc_step))
        self.dec_factor = max(0.3, min(0.95, float(dec_factor)))
        self.overshoot_band = float(overshoot_band)
        self.undershoot_band = float(undershoot_band)
        self.max_mode_hold_pct = float(max_mode_hold_pct)

        self._lock = threading.RLock()
        self._limit_pct = 100.0
        self._is_max_mode = True
        self._reinit = False
        self._last_change_t = 0.0
        self.set_limit(limit_pct)

    # ── 限制设置 ──
    def set_limit(self, pct):
        """设置最大 CPU 使用率（1~100）。>=99.5 视为最大性能模式。"""
        pct = float(pct)
        if pct >= 99.5:
            pct = 100.0
        pct = min(max(pct, 1.0), 100.0)
        with self._lock:
            changed = abs(self._limit_pct - pct) > 0.01
            self._limit_pct = pct
            self._is_max_mode = pct >= 99.5
            if changed:
                # 限制变化 → 立即响应（跳向新限制对应的理论进程数）
                self._reinit = True
                self._last_change_t = 0.0
        return pct

    @property
    def limit_pct(self):
        with self._lock:
            return self._limit_pct

    @property
    def is_max_mode(self):
        with self._lock:
            return self._is_max_mode

    def mode_label(self):
        if self.is_max_mode:
            return '最大性能模式'
        return f'资源限制 {self.limit_pct:.0f}%'

    def reset_cooldown(self):
        """新进程池启动时调用，立即响应目标值。"""
        with self._lock:
            self._last_change_t = 0.0
            self._reinit = True

    # ── 进程数边界 ──
    def initial_workers(self):
        """当前限制对应的理论初始 worker 数。"""
        with self._lock:
            if self._is_max_mode:
                n = self._cpu_count
            else:
                # 向下取整：起步就不超过限制（严格限流）
                n = max(1, math.floor(self._limit_pct / 100.0 * self._cpu_count))
        return min(max(n, self.min_workers), self._max_hard)

    def worker_cap(self):
        """当前限制下允许的最大 worker 数（上限）。"""
        with self._lock:
            if self._is_max_mode:
                n = self._max_hard
            else:
                # 1.5 倍留出 AIMD 余量，供乘性下降后的恢复
                n = max(1, math.floor(self._limit_pct / 100.0 * self._cpu_count * 1.5))
        return min(max(n, self.min_workers), self._max_hard)

    # ── AIMD 调节 ──
    def target_workers(self, program_pct, system_pct, current):
        """根据最新采样给出目标 worker 数。"""
        now = time.time()
        cap = self.worker_cap()
        with self._lock:
            reinit = self._reinit
            last_change = self._last_change_t
            is_max = self._is_max_mode
            limit = self._limit_pct

        # 限制刚变化：跳过冷却，直接跳向理论值（快速响应）
        if reinit and now - last_change >= 1.0:
            with self._lock:
                self._reinit = False
                self._last_change_t = now
            target = min(max(self.initial_workers(), self.min_workers), cap)
            return target

        if now - last_change < self.cooldown_s:
            return current  # 冷却期：保持

        target = current
        if is_max:
            # ── 最大性能模式：逼近系统 CPU = 100% ──
            if system_pct >= 100.0:
                target = max(self.min_workers, current - self.inc_step)   # 过载退让
            elif system_pct >= self.max_mode_hold_pct:
                target = current                                           # 已饱和，保持
            elif current < cap and self._workers_busy(program_pct, current):
                target = current + self.inc_step                           # 未饱和且 worker 忙碌：加
        else:
            # ── 资源限制模式：约束"程序自身 CPU 占用" ──
            if program_pct > limit + self.overshoot_band:
                target = max(self.min_workers, int(current * self.dec_factor))  # 超限：乘性降
            elif (program_pct < limit - self.undershoot_band
                  and current < cap and self._workers_busy(program_pct, current)):
                target = min(current + self.inc_step, cap)                 # 有余量且 worker 忙碌：加性增

        if target != current:
            with self._lock:
                self._last_change_t = now
        return target

    def _workers_busy(self, program_pct, current):
        """判断当前 worker 是否足够忙碌（>50% 满载）。

        用于避免在任务已清空/worker 空闲时仍然加进程：
        若 current 个 worker 全部满载，程序自身 CPU 约为 current/cores*100。
        """
        if current <= 0:
            return False
        expected = current / self._cpu_count * 100.0   # 全部满载时的程序 CPU
        return program_pct >= 0.5 * expected


# ============================================================
# 面板指标存储
# ============================================================

class DashboardState:
    """线程安全的监控指标存储（Web 面板 SSE / API 读取）。"""

    def __init__(self, maxlen=240):
        self._lock = threading.RLock()
        self._cur = {
            'running': False,
            'pool': '',
            'workers': 0,
            'target_workers': 0,
            'program_cpu': 0.0,
            'system_cpu': 0.0,
            'limit': 100.0,
            'mode': '最大性能模式',
            'tasks_pending': 0,
            'server_time': '',
        }
        self._hist = {
            'ts': deque(maxlen=maxlen),
            'program_cpu': deque(maxlen=maxlen),
            'system_cpu': deque(maxlen=maxlen),
            'workers': deque(maxlen=maxlen),
            'target_workers': deque(maxlen=maxlen),
        }
        self._events = deque(maxlen=60)

    def update(self, **fields):
        with self._lock:
            self._cur.update(fields)

    def push_sample(self, ts, program_cpu, system_cpu, workers, target_workers):
        with self._lock:
            self._hist['ts'].append(ts)
            self._hist['program_cpu'].append(program_cpu)
            self._hist['system_cpu'].append(system_cpu)
            self._hist['workers'].append(workers)
            self._hist['target_workers'].append(target_workers)

    def add_event(self, msg):
        with self._lock:
            self._events.append(f'[{time.strftime("%H:%M:%S")}] {msg}')

    def snapshot(self):
        with self._lock:
            snap = dict(self._cur)
            snap.update({
                'hist_ts': list(self._hist['ts']),
                'hist_program_cpu': list(self._hist['program_cpu']),
                'hist_system_cpu': list(self._hist['system_cpu']),
                'hist_workers': list(self._hist['workers']),
                'hist_target_workers': list(self._hist['target_workers']),
                'events': list(self._events),
                'server_time': time.strftime('%H:%M:%S'),
            })
            return snap


# ============================================================
# 动态进程池
# ============================================================

class PoolFuture:
    """极简 Future：等待单个任务完成。"""

    def __init__(self):
        self._event = threading.Event()
        self._result = None

    def set_result(self, value):
        self._result = value
        self._event.set()

    def result(self, timeout=None):
        self._event.wait(timeout)
        return self._result

    def done(self):
        return self._event.is_set()


def _pool_worker(task_q, result_q, ctrl_q, worker_fn):
    """进程池 worker 主体：从任务队列取任务执行，周期性检查停止信号。"""
    stop = False
    while not stop:
        # 优先消费控制信号（STOP → 优雅退出）
        try:
            while True:
                if ctrl_q.get_nowait() == 'STOP':
                    stop = True
        except queue.Empty:
            pass
        if stop:
            break
        try:
            item = task_q.get(timeout=0.5)
        except queue.Empty:
            continue
        tid, args = item
        try:
            res = worker_fn(args)          # worker_fn 只接收一个参数（任务元组）
            result_q.put((tid, True, res))
        except BaseException:
            result_q.put((tid, False, None))


class AdaptiveWorkerPool:
    """基于 CPU 反馈的动态进程池。

    worker 数量不固定：CpuGovernor 每 sample_interval 秒采样一次 CPU 占用，
    按 AIMD 算法给出目标 worker 数，池实时增减进程（STOP 信号优雅退出）。

    用法（与 ProcessPoolExecutor 类似）：
        with AdaptiveWorkerPool(fn, governor=gov, state=st) as pool:
            fut = pool.submit((p1, date))     # fn 只接收这一个参数
            res = fut.result()
    """

    def __init__(self, worker_fn, governor=None, state=None, cpu_limit=100.0,
                 pool_name='pool', min_workers=None):
        self.worker_fn = worker_fn
        self.pool_name = pool_name
        self._cpu_count = os.cpu_count() or 4
        self.governor = governor or CpuGovernor(
            limit_pct=cpu_limit, cpu_count=self._cpu_count,
            min_workers=min_workers or 1)
        self.state = state or DashboardState()
        self._monitor = CpuMonitor(cpu_count=self._cpu_count)

        self._ctx = multiprocessing.get_context('spawn')
        self._task_q = self._ctx.Queue()
        self._result_q = self._ctx.Queue()
        self._ctrl_q = self._ctx.Queue()

        self._futures = {}
        self._futures_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._procs = []                     # list[multiprocessing.Process]
        self._procs_lock = threading.Lock()
        self._next_id = 0
        self._pending = 0
        self._cond = threading.Condition()
        self._started = False
        self._stopped = threading.Event()

        self._control_thread = threading.Thread(target=self._control_loop,
                                                daemon=True, name='awp-control')
        self._result_thread = threading.Thread(target=self._result_loop,
                                               daemon=True, name='awp-result')
        self.governor.reset_cooldown()
        self.state.update(limit=self.governor.limit_pct,
                          mode=self.governor.mode_label())
        self.state.add_event(
            f'进程池 [{pool_name}] 已创建（限制 {self.governor.limit_pct:.0f}%'
            f'{("，最大性能" if self.governor.is_max_mode else "")}）')

    # ── 生命周期 ──
    def start(self):
        if self._started:
            return
        self._started = True
        self._control_thread.start()
        self._result_thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown(wait=True)
        return False

    # ── 任务提交 ──
    def submit(self, args):
        if self._stopped.is_set():
            raise RuntimeError('进程池已关闭')
        with self._submit_lock:
            tid = self._next_id
            self._next_id += 1
        fut = PoolFuture()
        with self._futures_lock:
            self._futures[tid] = fut
        with self._cond:
            self._pending += 1
        self._task_q.put((tid, args))
        self.start()
        return fut

    def map(self, tasks):
        """提交全部任务并阻塞等待，按提交顺序返回结果（失败/None 已过滤）。"""
        futures = [self.submit(t) for t in tasks]
        self.wait_all()
        out = []
        for f in futures:
            r = f.result()
            if r is not None:
                out.append(r)
        return out

    def wait_all(self, timeout=None):
        """等待所有已提交任务完成。"""
        deadline = time.time() + (timeout if timeout else float('inf'))
        with self._cond:
            while self._pending > 0:
                if timeout is not None and time.time() >= deadline:
                    return False
                self._cond.wait(timeout=1.0)
            return True

    def shutdown(self, wait=True):
        if self._stopped.is_set():
            return
        self._stopped.set()
        with self._procs_lock:
            n = len(self._procs)
        for _ in range(n):
            try:
                self._ctrl_q.put('STOP')
            except Exception:
                pass
        if wait:
            self.wait_all(timeout=30)
            deadline = time.time() + 15
            with self._procs_lock:
                self._prune_procs()
                while self._procs and time.time() < deadline:
                    self._prune_procs()
                    time.sleep(0.2)
        self._control_thread.join(timeout=5)
        self._result_thread.join(timeout=5)
        for q in (self._task_q, self._result_q, self._ctrl_q):
            try:
                q.close()
            except Exception:
                pass
        self.state.update(running=False, pool=self.pool_name, workers=0,
                          target_workers=0, program_cpu=0.0, system_cpu=0.0,
                          tasks_pending=0)
        self.state.add_event(f'进程池 [{self.pool_name}] 已关闭')

    # ── 内部线程 ──
    def _control_loop(self):
        try:
            self._resize_to(self.governor.initial_workers(), reason='初始化')
        except Exception as e:
            self.state.add_event(f'[进程池] 初始化失败: {e}')
        while not self._stopped.is_set():
            time.sleep(self.governor.sample_interval)
            try:
                program_pct, system_pct = self._monitor.sample()
            except Exception:
                program_pct, system_pct = 0.0, 0.0
            cur = self.worker_count()
            try:
                target = self.governor.target_workers(program_pct, system_pct, cur)
            except Exception:
                target = cur
            self._resize_to(target, reason='治理器')
            self.state.update(
                running=True,
                pool=self.pool_name,
                workers=cur,
                target_workers=target,
                program_cpu=round(program_pct, 1),
                system_cpu=round(system_pct, 1),
                limit=self.governor.limit_pct,
                mode=self.governor.mode_label(),
                tasks_pending=self._pending,
            )
            self.state.push_sample(time.time(), round(program_pct, 1),
                                   round(system_pct, 1), cur, target)

    def _result_loop(self):
        while True:
            try:
                tid, ok, res = self._result_q.get(timeout=0.5)
            except queue.Empty:
                if self._stopped.is_set() and self._pending == 0 \
                        and not self._any_worker_alive():
                    break
                continue
            except (ValueError, OSError):
                break  # 队列已关闭（shutdown 完成），正常退出
            with self._futures_lock:
                fut = self._futures.pop(tid, None)
            if fut is not None:
                fut.set_result(res if ok else None)
            with self._cond:
                self._pending = max(0, self._pending - 1)
                self._cond.notify_all()

    # ── 进程管理 ──
    def worker_count(self):
        with self._procs_lock:
            self._prune_procs()
            return len(self._procs)

    def _any_worker_alive(self):
        with self._procs_lock:
            self._prune_procs()
            return len(self._procs) > 0

    def _prune_procs(self):
        """清理已退出的进程对象（调用方需持有 _procs_lock）"""
        self._procs = [p for p in self._procs if p.is_alive()]

    def _spawn_worker(self):
        p = self._ctx.Process(target=_pool_worker,
                              args=(self._task_q, self._result_q,
                                    self._ctrl_q, self.worker_fn),
                              daemon=True,
                              name=f'awp-{self.pool_name}-{len(self._procs)}')
        p.start()
        self._procs.append(p)

    def _resize_to(self, target, reason=''):
        cap = self.governor.worker_cap()
        lo = self.governor.min_workers
        target = int(max(lo, min(int(target), cap)))
        with self._procs_lock:
            self._prune_procs()
            cur = len(self._procs)
            if target > cur:
                for _ in range(target - cur):
                    self._spawn_worker()
                self.state.add_event(f'[{self.pool_name}] 增加进程 → {len(self._procs)}（{reason}）')
            elif target < cur:
                for _ in range(cur - target):
                    try:
                        self._ctrl_q.put('STOP')
                    except Exception:
                        pass
                self.state.add_event(f'[{self.pool_name}] 减少进程 → {target}（{reason}）')
