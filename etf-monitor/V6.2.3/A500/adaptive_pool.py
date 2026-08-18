"""
自适应进程池 + CPU 资源治理器（Adaptive Worker Pool & CPU Governor）
=====================================================================
为 param_optimizer.py 的可视化控制面板提供底层模块：

- CpuMonitor        : 采样"程序自身 CPU 占用"（主进程 + 全部子进程合计，
                      按系统总核数换算为 0~100%）与"系统整体 CPU 利用率"；
                      两者均取最近若干秒的窗口平均（非 1 秒瞬时值）。
- CpuGovernor       : AIMD（加性增 / 乘性减）自适应调节算法，根据资源限制
                      动态给出目标 worker 数。
                      * 限制 < 100%：约束的是程序自身 CPU 占用（严格限流）
                      * 限制 = 100%：进入"最大性能模式"，目标是把系统整体
                        CPU 打满到 100%，动态逼近"恰好打满 CPU 的进程数"。
- AdaptiveWorkerPool: 基于 multiprocessing.Queue 的动态进程池。worker 数量
                      由 CpuGovernor 基于平滑采样低频反馈（冷却期 + 持续
                      确认 + 小步调整），实时增减。
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
import sys
import threading
import time
from collections import deque

import psutil

__all__ = ['CpuMonitor', 'CpuGovernor', 'DashboardState', 'AdaptiveWorkerPool']


# ============================================================
# 活动进程池注册表（供 GUI 紧急停止 / 紧急执行等外部干预使用）
# ============================================================
_LIVE_POOLS = set()
_LIVE_POOLS_LOCK = threading.Lock()


def _register_pool(pool):
    with _LIVE_POOLS_LOCK:
        _LIVE_POOLS.add(pool)


def _unregister_pool(pool):
    with _LIVE_POOLS_LOCK:
        _LIVE_POOLS.discard(pool)


def live_pools():
    """当前所有存活（未关闭）的 AdaptiveWorkerPool 实例快照。

    param_optimizer 的紧急停止 / 紧急执行接口通过它定位活动进程池。
    """
    with _LIVE_POOLS_LOCK:
        return list(_LIVE_POOLS)


# ============================================================
# CPU 采样
# ============================================================

class CpuMonitor:
    """采样程序自身 CPU 占用（主进程 + 全部子进程）与系统整体 CPU 利用率。

    精度与平滑策略：
    - 系统整体：基于 psutil.cpu_times() 各分量之和的时间差计算，
      busy% = (总时间差 - 空闲时间差) / 总时间差，与任务管理器口径一致，
      且自归一化（与调用间隔无关），避免 cpu_percent(interval=None)
      "自上次调用起"的语义偏差。
    - 程序自身：主进程 + 全部子进程的 cpu_times 差分除以墙钟与核数。
    - 时间段平均：每 sample_interval 秒取一个原始样本存入窗口缓存，
      sample() 返回最近 window_s 秒的算术平均，替代"0.5 秒瞬时值"，
      供调度器使用的即是平滑后的平均值。
    - 线性补偿（任务管理器口径）：采样值比任务管理器系统性偏低，
      显示/调度统一施加偏移 off = 5 + 0.15*v（最低 +5%、最高 +20%）；
      sample() 与历史缓存（峰值平均、95% 系统护栏）均返回补偿后值，
      使"GUI 显示值 = 调度器读取值"。最大性能模式按真实系统利用率
      逼近 100%，用 last_raw() 获取未补偿值做饱和判定。
    """

    def __init__(self, cpu_count=None, window_s=5.0, sample_interval=0.5,
                 hist_s=10.0):
        self._cpu_count = cpu_count or os.cpu_count() or 4
        self._proc = psutil.Process()
        self._prev_times = {}      # pid -> cpu_time(user+system)
        self._prev_wall = None
        self._prev_sys = None      # (total, idle) 系统累计时间基线
        self._sample_interval = max(0.5, float(sample_interval))
        # 平均窗口缓存（去首尾各 1 个样本抖动，默认 5 秒窗口）
        maxlen = max(3, int(round(window_s / self._sample_interval)) + 1)
        self._buf_program = deque(maxlen=maxlen)
        self._buf_system = deque(maxlen=maxlen)
        # 历史样本缓存：调度参考"过去前 50% 最大值的平均值"
        # （默认 10 秒窗口 = 20 个样本 @0.5s，反映程序实际负载高位水平）
        hist_len = max(6, int(round(hist_s / self._sample_interval)))
        self._hist_program = deque(maxlen=hist_len)
        # 系统整体 CPU 历史样本（同 10 秒窗口）：供"任何一次超过阈值即
        # 激活下调"的系统过载护栏使用（system_any_above）。
        self._hist_system = deque(maxlen=hist_len)
        self._last_raw = (0.0, 0.0)  # 最近一次真实（未补偿）样本，供最大性能模式饱和判定

    @staticmethod
    def _comp(v):
        """线性补偿：off = 5 + 0.15*v，最低 +5%、最高 +20%（任务管理器口径）。"""
        v = float(v)
        return min(100.0, max(0.0, v + 5.0 + 0.15 * v))

    def _all_procs(self):
        procs = [self._proc]
        try:
            procs += list(self._proc.children(recursive=True))
        except Exception:
            pass
        return procs

    def _raw_sample(self):
        """返回当前瞬时原始样本 (program_pct, system_pct)，均为 0~100"""
        now = time.time()
        # ── 系统整体：cpu_times 差分（经典公式，与任务管理器接近）──
        system_pct = 0.0
        try:
            t = psutil.cpu_times()
            total = sum(float(x) for x in t)
            idle = float(t.idle)
            if self._prev_sys is not None:
                p_total, p_idle = self._prev_sys
                d_total = total - p_total
                if d_total > 1e-9:
                    system_pct = max(0.0, min(100.0,
                        100.0 * (d_total - max(0.0, idle - p_idle)) / d_total))
            self._prev_sys = (total, idle)
        except Exception:
            pass
        # ── 程序自身：主进程 + 全部子进程 CPU 时间差分 ──
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
        return pct, system_pct

    def sample(self):
        """返回时间段平均 (program_pct, system_pct)，均为 0~100（已线性补偿）。

        对最近 window_s 秒内的原始样本取算术平均（去掉最早 1 个旧样本，
        因为它是上一次窗口的"尾"，会引入滞后），再施加线性补偿。
        由于补偿 off = 5 + 0.15*v 是线性函数，平均后补偿与补偿后平均等价。

        返回值为"任务管理器口径"，GUI 显示与调度器判定（峰值平均、
        95% 系统护栏、Max 越限）统一使用该值；未补偿真实值见 last_raw()。
        """
        program_pct, system_pct = self._raw_sample()
        self._last_raw = (program_pct, system_pct)
        self._buf_program.append(program_pct)
        self._buf_system.append(system_pct)
        # 历史缓存存"补偿后"值：peak_avg / system_any_above 按任务管理器口径判定
        self._hist_program.append(self._comp(program_pct))
        self._hist_system.append(self._comp(system_pct))
        n_prog = len(self._buf_program)
        n_sys = len(self._buf_system)
        if n_prog == 0 and n_sys == 0:
            return 0.0, 0.0
        avg_prog = sum(self._buf_program) / max(1, n_prog)
        avg_sys = sum(self._buf_system) / max(1, n_sys)
        return round(self._comp(avg_prog), 1), round(self._comp(avg_sys), 1)

    def last_raw(self):
        """最近一次真实（未补偿）样本 (program_pct, system_pct)。
        供最大性能模式按真实系统利用率逼近 100% 的饱和判定使用。"""
        return self._last_raw

    def peak_avg(self, ratio=0.5):
        """过去 hist_s 秒内样本中"前 ratio 最大值"的平均（默认前 50%）。

        调度器增减进程的参考值：取历史样本排序后最大的一半求平均，
        反映程序实际负载的高位水平，避免瞬时低谷诱使调度器误加进程。
        样本不足时退化为当前窗口平均。
        """
        h = list(self._hist_program)
        n = len(h)
        if n == 0:
            return 0.0
        h.sort()
        k = max(1, int(round(n * min(1.0, max(0.0, ratio)))))
        top = h[-k:]
        return round(sum(top) / len(top), 1)

    def system_any_above(self, threshold):
        """过去 hist_s 秒内系统整体 CPU 是否有任一采样值 > threshold。

        用于资源限制模式的系统过载护栏：比瞬时值/窗口平均更严格，
        只要过去 10 秒内出现过任何一次超过阈值（默认 95%）即激活进程下调。
        """
        return any(v > threshold for v in self._hist_system)


# ============================================================
# AIMD 资源治理器
# ============================================================

class CpuGovernor:
    """AIMD（加性增 / 乘性减）CPU 资源治理器。

    为抑制"1 秒瞬时值 → 立即调进程"造成的震荡，本治理器采用三重降频策略：
    - 输入平滑：消费的 program_pct / system_pct 已由 CpuMonitor 做时间段平均；
    - 冷却期：两次决策之间至少间隔 cooldown_s 秒（默认 8s）；
    - 持续确认：加进程前需连续 sustain_inc 轮采样都满足"有余量"条件；
      降进程仅在峰值平均（前 50% 最大值平均）超过 Max 时发生，
      轻微越限每次最多砍 max_dec_step 个（默认 3），严重越限才全力乘性回撤。

    - 资源限制模式（limit < 100%）：
        调度参考 = 过去"前 50% 最大值的平均值"（峰值平均，见 CpuMonitor.peak_avg，
        避免瞬时低谷诱使误加进程）；
        程序利用率允许长期停留在 [limit - dead_band, limit]（默认 Max~Max-10%）：
        program_peak > limit                  → 立即下调进程数（绝对不允许超过 Max）
        program_peak < limit - dead_band      → 连续确认后 +1
        落在 [limit - dead_band, limit]       → 死区：调度器不做任何调整
        （dead_band = 10，即 Max 减 10% 以内程序可长期运行，无需增删进程）
        过去 10s 内系统整体 CPU 出现过任何一次 > system_guard_pct（默认 95%）
        → 也激活进程数下调（严格护栏：允许系统整体 CPU 稳定区间为 <95%，
        避免本程序挤占其他进程）
    - 最大性能模式（limit = 100%）：
        初始取核数；系统 CPU 未饱和则持续确认后 +1 逼近，过载则退让，
        最终收敛到"恰好把系统 CPU 打满到 100%"的进程数。
    """

    def __init__(self, limit_pct=100.0, cpu_count=None, min_workers=1,
                 max_workers=None, sample_interval=0.5, cooldown_s=8.0,
                 inc_step=1, dec_factor=0.85, overshoot_band=3.0,
                 dead_band=10.0, max_mode_hold_pct=98.5,
                 sustain_inc=2, max_dec_step=3, system_guard_pct=95.0):
        self._cpu_count = cpu_count or os.cpu_count() or 4
        # 总进程数硬上限：不得超过逻辑 CPU 总数的 1.1 倍（用户要求）。
        # 向下取整（floor）保证 1.1 倍是绝对不可逾越的顶；
        # 显式传入的 max_workers 同样被钳制，不破坏该硬约束。
        _hard_ceiling = int(self._cpu_count * 1.1)
        self._max_hard = min(max_workers or _hard_ceiling, _hard_ceiling)
        self.min_workers = max(1, min_workers)
        self.sample_interval = max(0.3, float(sample_interval))
        self.cooldown_s = max(2.0, float(cooldown_s))
        self.inc_step = max(1, int(inc_step))
        self.dec_factor = max(0.3, min(0.95, float(dec_factor)))
        self.overshoot_band = float(overshoot_band)
        self.dead_band = float(dead_band)  # Max~Max-10% 死区宽度，区间内调度器不调整
        self.max_mode_hold_pct = float(max_mode_hold_pct)
        self.sustain_inc = max(1, int(sustain_inc))
        self.max_dec_step = max(1, int(max_dec_step))
        self.system_guard_pct = float(system_guard_pct)  # 系统整体 CPU 过载护栏（默认 95%）

        self._lock = threading.RLock()
        self._limit_pct = 100.0
        self._is_max_mode = True
        self._reinit = False
        self._last_change_t = 0.0
        self._under_count = 0   # 连续满足"有余量"的轮数（持续确认）
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
                self._under_count = 0
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
            self._under_count = 0

    # ── 进程数边界 ──
    def initial_workers(self):
        """当前限制对应的理论初始 worker 数。

        资源限制模式（Max<100%）：向下取整后整体再下调 2 个线程
        （更保守起步，避免开局即冲顶），全局最低 1 个（min_workers）；
        100% 最大性能模式保持核数不变。
        """
        with self._lock:
            if self._is_max_mode:
                n = self._cpu_count
            else:
                # 向下取整后整体下调 2 个；全局最低线程数 = 1
                n = max(1, math.floor(self._limit_pct / 100.0 * self._cpu_count) - 2)
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
    def target_workers(self, program_pct, system_pct, current, program_peak=None,
                       sys_any_above=None, system_raw=None):
        """根据平滑后的采样给出目标 worker 数（低频、小步决策）。

        Args:
            program_pct: 程序自身 CPU 的窗口平均（已线性补偿，任务管理器口径）
            system_pct:  系统整体 CPU 利用率（已线性补偿，任务管理器口径）
            current:     当前 worker 数
            program_peak: 过去"前 50% 最大值的平均值"（补偿后口径，调度参考基准）；
                          为 None 时退化为 program_pct。
            sys_any_above: 过去 hist_s 秒内系统整体 CPU 是否出现过 > 阈值
                           （95%）的补偿后采样；为 None 时退化为
                           瞬时 system_pct > system_guard_pct 判断。
            system_raw:   最近一次真实（未补偿）系统 CPU，最大性能模式饱和
                          判定专用（按真实利用率逼近 100%）；为 None 时退化
                          为 system_pct。

        限制模式判定（用户要求）：
            - 参考基准 = program_peak（历史峰值平均，避免瞬时低谷误加进程）
            - program_peak > limit → 立即下调进程数（绝对不允许超过 Max）
            - program_peak < limit - dead_band → 持续确认后 +1
            - 落在 [limit - dead_band, limit] → 死区，不做任何调整
              （程序利用率长期保持 Max~Max-10%，调度器无需工作）
            - 过去 10s 内系统整体 CPU 任何一次 > 95% → 也激活进程数下调
              （允许系统整体 CPU 稳定区间为 <95%；100% 最大性能模式除外）
        注：所有阈值（Max、95%、死区）均为任务管理器口径（补偿后值），
        与 GUI 显示一致；仅最大性能模式按真实系统利用率判定饱和。
        """
        now = time.time()
        cap = self.worker_cap()
        with self._lock:
            reinit = self._reinit
            last_change = self._last_change_t
            is_max = self._is_max_mode
            limit = self._limit_pct
            under = self._under_count
        if program_peak is None:
            program_peak = program_pct
        if system_raw is None:
            system_raw = system_pct
        # 系统过载判定：优先用历史"任一超阈值"（更严格），缺省退化为瞬时值
        if sys_any_above is None:
            sys_overload = system_pct > self.system_guard_pct
        else:
            sys_overload = bool(sys_any_above)

        # 限制刚变化：跳过冷却，直接跳向理论值（快速响应）
        if reinit and now - last_change >= 1.0:
            with self._lock:
                self._reinit = False
                self._last_change_t = now
                self._under_count = 0
            target = min(max(self.initial_workers(), self.min_workers), cap)
            return target

        if now - last_change < self.cooldown_s:
            return current  # 冷却期：保持（决策频率受控）

        target = current
        if is_max:
            # ── 最大性能模式：按真实系统利用率逼近 100%（system_raw）──
            if system_raw >= 99.5:
                target = max(self.min_workers, current - self.inc_step)   # 过载退让
            elif system_raw >= self.max_mode_hold_pct:
                target = current                                           # 已饱和，保持
            elif current < cap and self._workers_busy(program_pct, current):
                under += 1                                                 # 未饱和：持续确认
                if under >= self.sustain_inc:
                    under = 0
                    target = current + self.inc_step                       # 确认足够：+1
            else:
                under = 0
        else:
            # ── 资源限制模式：约束"程序自身 CPU 占用"（参考峰值平均）──
            # 判定基准 program_peak = 过去前 50% 最大值的平均值。
            # 用户要求：
            #   1) 峰值平均一旦超过 Max 就立即下调进程数（不设容忍带）；
            #   2) 过去 10s 内系统整体 CPU 出现过任何一次 >95% 也激活下调
            #      （sys_overload 已由 sys_any_above 历史判定得出）。
            prog_over = program_peak > limit
            if prog_over or sys_overload:
                under = 0
                dec_target = max(self.min_workers, int(current * self.dec_factor))
                if prog_over and program_peak > limit + 2 * self.overshoot_band:
                    # 程序严重越限：全力乘性回撤
                    target = dec_target
                else:
                    # 程序轻微越限 / 系统整体过载：小步回撤（单次最多砍 max_dec_step 个）
                    removed = min(current - dec_target, self.max_dec_step)
                    target = max(self.min_workers, current - removed)
            elif (program_peak < limit - self.dead_band
                  and current < cap and self._workers_busy(program_pct, current)):
                under += 1                                                 # 有余量：持续确认
                if under >= self.sustain_inc:
                    under = 0
                    target = min(current + self.inc_step, cap)             # 确认足够：+1
            else:
                under = 0

        if target != current:
            with self._lock:
                self._last_change_t = now
                self._under_count = under
        else:
            # 持续确认计数无条件写回（即使本轮未调整进程数也要累积）
            with self._lock:
                self._under_count = under
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
        self.log_lines = deque(maxlen=6000)  # 运算日志缓冲（终端分流输出，容量较大）
        self._log_seq = 0  # 日志序列号（单调递增，用于增量读取）
        # 结构化计算进度（独立进度面板数据，与终端打印解耦）
        self._progress = {'phase': '', 'label': '', 'current': 0, 'total': 0,
                          'pct': 0.0, 'detail': ''}

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

    def add_log(self, line):
        """添加运算日志行"""
        with self._lock:
            self.log_lines.append(line)
            self._log_seq += 1

    def clear_log(self):
        """清空日志缓冲"""
        with self._lock:
            self.log_lines.clear()
            self._log_seq = 0

    def set_progress(self, phase=None, label=None, current=None, total=None,
                     pct=None, detail=None):
        """更新结构化计算进度（GUI 独立进度面板读取）。

        未显式给出 pct 时，若 total > 0 则按 current/total 自动换算。
        线程安全：优化后台线程与 Tk 主线程可并发调用。
        """
        with self._lock:
            if phase is not None:
                self._progress['phase'] = str(phase)
            if label is not None:
                self._progress['label'] = str(label)
            if current is not None:
                self._progress['current'] = int(current)
            if total is not None:
                self._progress['total'] = int(total)
            if detail is not None:
                self._progress['detail'] = str(detail)
            if pct is not None:
                self._progress['pct'] = float(max(0.0, min(100.0, pct)))
            elif self._progress['total'] and self._progress['total'] > 0:
                self._progress['pct'] = float(max(
                    0.0, min(100.0,
                             self._progress['current'] * 100.0
                             / self._progress['total'])))
            return dict(self._progress)

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
                'log_lines': list(self.log_lines),
                'log_seq': self._log_seq,
                'progress': dict(self._progress),
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


class _WorkerLogWriter:
    """worker 进程内的输出分流器：print 同时写入原 stdout/stderr 与 log_q。

    使每个 worker 的实时输出（进程名 + 函数名 + 任务参数/结果摘要）逐行
    转发到 GUI 右侧"终端打印区"（主进程 _log_loop → state.add_log）。
    """

    def __init__(self, real, log_q):
        self._real = real
        self._q = log_q
        self._buf = ''

    def write(self, s):
        try:
            self._real.write(s)
        except Exception:
            pass
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                try:
                    self._q.put(('LOG', line))
                except Exception:
                    pass

    def flush(self):
        if self._buf.strip():
            try:
                self._q.put(('LOG', self._buf))
            except Exception:
                pass
            self._buf = ''
        try:
            self._real.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_worker_tee(log_q):
    """在 worker 进程内重定向 stdout/stderr 到 log_q（保留原控制台输出）。"""
    try:
        sys.stdout = _WorkerLogWriter(sys.stdout, log_q)
    except Exception:
        pass
    try:
        sys.stderr = _WorkerLogWriter(sys.stderr, log_q)
    except Exception:
        pass


def _brief(obj, limit=150):
    """生成任务/结果的单行摘要（worker 日志打印用）。

    - dict（任务参数）：取前 5 个 key=value，避免 55 参数全量刷屏；
    - 结果对象（TrialResult 等）：尽量取 objective / return / sharpe；
    - 其余：str 截断。
    """
    if obj is None:
        return 'None'
    if isinstance(obj, dict):
        keys = list(obj.keys())[:5]
        s = ', '.join(f'{k}={obj[k]!r}' for k in keys)
        if len(obj) > 5:
            s += f', ...(+{len(obj)-5})'
        return s
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        head = ', '.join(_brief(x, limit // 2) for x in list(obj)[:4])
        if n > 4:
            head += f', ...(+{n-4})'
        return head
    o = getattr(obj, 'objective', None)
    r = getattr(obj, 'strategy_return', None)
    sh = getattr(obj, 'sharpe_ratio', None)
    if o is not None:
        parts = [f'obj={o:.2f}' if isinstance(o, (int, float)) else f'obj={o}']
        if isinstance(r, (int, float)):
            parts.append(f'return={r*100:.2f}%')
        if isinstance(sh, (int, float)):
            parts.append(f'sharpe={sh:.3f}')
        return ' | '.join(parts)
    s = str(obj)
    return s if len(s) <= limit else s[:limit] + '...'


def _pool_worker(task_q, result_q, ctrl_q, ack_q, log_q, worker_fn):
    """进程池 worker 主体：从任务队列取任务执行，周期性检查停止信号。

    ack_q: 任务开始/结束上报队列（'RUN'/'IDLE' + tid），供池侧精确掌握
           每个进程"正在运行的任务"，用于紧急执行时放弃被杀进程的数据。
    log_q: 日志转发队列 —— worker 的 print（进程名/函数名/参数/结果）逐行
           转发到主进程 → GUI 终端打印区。
    """
    _proc_name = multiprocessing.current_process().name
    _install_worker_tee(log_q)
    print(f'[{_proc_name}] worker 就绪（执行函数: {worker_fn.__name__}）', flush=True)
    stop = False
    while not stop:
        # 优先消费控制信号（STOP → 优雅退出）。
        # 注意：每轮最多消费 1 个 STOP —— 若用 while 连续 get_nowait，
        # 单个 worker 会抢走多个 STOP，其余 worker 收不到信号，
        # 导致"减少进程"降不到位（进程数/目标进程数异常）。
        try:
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
        print(f'[{_proc_name}] {worker_fn.__name__} 开始 任务#{tid} 参数: {_brief(args)}',
              flush=True)
        try:
            ack_q.put(('RUN', tid))
        except Exception:
            pass
        try:
            res = worker_fn(args)          # worker_fn 只接收一个参数（任务元组）
            result_q.put((tid, True, res))
        except BaseException:
            result_q.put((tid, False, None))
            print(f'[{_proc_name}] {worker_fn.__name__} 异常 任务#{tid}', flush=True)
        else:
            print(f'[{_proc_name}] {worker_fn.__name__} 完成 任务#{tid} '
                  f'结果: {_brief(res)}', flush=True)
        try:
            ack_q.put(('IDLE', tid))
        except Exception:
            pass


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
        self._monitor = CpuMonitor(cpu_count=self._cpu_count,
                                   sample_interval=self.governor.sample_interval)

        self._ctx = multiprocessing.get_context('spawn')
        self._task_q = self._ctx.Queue()
        self._result_q = self._ctx.Queue()
        self._ctrl_q = self._ctx.Queue()
        self._log_q = self._ctx.Queue()      # worker print 日志转发（→ GUI 终端区）

        self._futures = {}
        self._futures_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._procs = []                     # list[multiprocessing.Process]
        self._procs_lock = threading.Lock()
        # 任务归属跟踪（紧急执行用）：pid → 该进程正在运行的任务 id / None
        # worker 通过 per-process ack 队列上报，_ack_loop 维护此映射。
        self._ack_qs = {}                    # pid -> ctx.Queue()
        self._proc_tids = {}                 # pid -> tid | None
        self._next_id = 0
        self._pending = 0
        self._cond = threading.Condition()
        self._started = False
        self._stopped = threading.Event()
        # 调度器意图进程数：控制循环把目标交给调整线程后，冷却期内
        # target_workers() 会返回"当前进程数"，若直接上屏会与当前数同源
        # 同步（用户反馈）。此处单独保留最近一次真实决策目标，供
        # "目标进程数"显示使用（显示调度器给出的意图值，而非镜像当前值）。
        self._desired_target = None

        self._control_thread = threading.Thread(target=self._control_loop,
                                                daemon=True, name='awp-control')
        self._result_thread = threading.Thread(target=self._result_loop,
                                               daemon=True, name='awp-result')
        # 进程数调整线程：Windows spawn 新 worker 需重新导入主模块，耗时可达数秒，
        # 若在控制线程内同步执行会阻塞采样循环 → GUI 数据停滞约数秒（假死观感）。
        # 因此进程增减全部交给独立的调整线程，采样循环只计算目标、永不阻塞。
        self._adjust_q = queue.Queue(maxsize=1)
        self._adjust_thread = threading.Thread(target=self._adjust_loop,
                                               daemon=True, name='awp-adjust')
        # 任务归属上报线程：消费各 worker 的 ack 队列，维护 pid→tid 映射
        self._ack_thread = threading.Thread(target=self._ack_loop,
                                            daemon=True, name='awp-ack')
        # worker 日志转发线程：log_q → state.add_log（GUI 终端打印区）
        self._log_thread = threading.Thread(target=self._log_loop,
                                            daemon=True, name='awp-log')
        _register_pool(self)
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
        self._adjust_thread.start()
        self._ack_thread.start()
        self._log_thread.start()

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
        self._adjust_thread.join(timeout=5)
        self._ack_thread.join(timeout=5)
        self._log_thread.join(timeout=5)
        for q in (self._task_q, self._result_q, self._ctrl_q):
            try:
                q.close()
            except Exception:
                pass
        try:
            self._log_q.close()
        except Exception:
            pass
        with self._procs_lock:
            for q in self._ack_qs.values():
                try:
                    q.close()
                except Exception:
                    pass
            self._ack_qs.clear()
            self._proc_tids.clear()
        self.state.update(running=False, pool=self.pool_name, workers=0,
                          target_workers=0, program_cpu=0.0, system_cpu=0.0,
                          tasks_pending=0)
        self._desired_target = None
        _unregister_pool(self)
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
                # 调度参考基准 = 过去前 50% 最大值的平均值（补偿后峰值平均）
                program_peak = self._monitor.peak_avg()
                # 系统过载护栏 = 过去 10s 内系统整体 CPU 是否出现过 >95%（补偿后）
                sys_any_above = self._monitor.system_any_above(
                    self.governor.system_guard_pct)
                # 最大性能模式按真实系统利用率判定饱和（补偿不参与逼近 100%）
                _, system_raw = self._monitor.last_raw()
                target = self.governor.target_workers(
                    program_pct, system_pct, cur, program_peak,
                    sys_any_above, system_raw)
            except Exception:
                target = cur
            # 仅在实际变化（真实决策）时才交给调整线程执行（spawn 不阻塞采样）。
            # 冷却期 / 死区内 target_workers() 返回 current 的"保持"信号不发
            # resize：若仍下发，调整线程 _resize_to 里 _prune_procs() 后 cur
            # 可能已减少，导致 target > cur 反而回增进程，且 _desired_target
            # 会被实时进程数顶掉 → 目标进程数显示回升（用户反馈）。
            if target != cur:
                self._request_resize(target, reason='治理器')
                self._desired_target = target
            display_target = self._desired_target if self._desired_target is not None else cur
            self.state.update(
                running=True,
                pool=self.pool_name,
                workers=cur,
                target_workers=display_target,
                program_cpu=round(program_pct, 1),
                system_cpu=round(system_pct, 1),
                limit=self.governor.limit_pct,
                mode=self.governor.mode_label(),
                tasks_pending=self._pending,
            )
            self.state.push_sample(time.time(), round(program_pct, 1),
                                   round(system_pct, 1), cur, display_target)

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
                # 仅在 future 尚在时减 pending（被 kill_excess/abort 放弃的
                # 任务已提前减过，此处避免重复扣除）
                if fut is not None:
                    self._pending = max(0, self._pending - 1)
                self._cond.notify_all()

    def _ack_loop(self):
        """消费各 worker 的 ack 队列，维护 pid → 正在运行任务 id 的映射。

        供"紧急执行"精确放弃被立即终止进程正在运行的任务数据。
        """
        while not self._stopped.is_set():
            with self._procs_lock:
                items = list(self._ack_qs.items())
            for pid, q in items:
                try:
                    while True:
                        msg, tid = q.get_nowait()
                        with self._procs_lock:
                            if msg == 'RUN':
                                self._proc_tids[pid] = tid
                            else:
                                self._proc_tids[pid] = None
                except queue.Empty:
                    pass
                except (ValueError, OSError):
                    pass
            time.sleep(0.05)

    def _log_loop(self):
        """消费 worker 转发来的 print 行，写入 state.add_log（GUI 终端打印区）。

        worker 的每进程/每任务日志（进程名 + 函数名 + 参数/结果摘要）经
        log_q 逐行回流，与主进程 _TeeWriter 捕获的输出汇合显示。
        """
        while not self._stopped.is_set():
            try:
                msg, line = self._log_q.get(timeout=0.3)
            except queue.Empty:
                continue
            except (ValueError, OSError, EOFError):
                break                # 队列已关闭
            if msg == 'LOG':
                try:
                    self.state.add_log(line)
                except Exception:
                    pass

    # ── 进程数调整（独立线程，不阻塞采样）──
    def _request_resize(self, target, reason=''):
        """把调整请求交给调整线程执行；队列只保留最新一个请求。"""
        try:
            self._adjust_q.get_nowait()          # 丢弃未执行的旧请求
        except queue.Empty:
            pass
        try:
            self._adjust_q.put_nowait((int(target), reason))
        except queue.Full:
            pass

    def _adjust_loop(self):
        """消费调整请求：spawn / 停进程。Windows spawn 耗时数秒，
        因此本循环独立于控制循环，期间 CPU 采样与 GUI 数据照常刷新。"""
        while True:
            try:
                target, reason = self._adjust_q.get(timeout=0.5)
            except queue.Empty:
                if self._stopped.is_set():
                    break
                continue
            try:
                self._resize_to(target, reason=reason)
            except Exception as e:
                self.state.add_event(f'[进程池] 进程调整失败: {e}')
            if self._stopped.is_set():
                try:
                    self._adjust_q.get_nowait()
                except queue.Empty:
                    pass
                break

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
        alive = [p for p in self._procs if p.is_alive()]
        if len(alive) != len(self._procs):
            self._procs = alive
            alive_pids = {p.pid for p in alive}
            for pid in list(self._ack_qs):
                if pid not in alive_pids:
                    try:
                        self._ack_qs[pid].close()
                    except Exception:
                        pass
                    self._ack_qs.pop(pid, None)
                    self._proc_tids.pop(pid, None)

    def _spawn_worker(self):
        ack_q = self._ctx.Queue()
        p = self._ctx.Process(target=_pool_worker,
                              args=(self._task_q, self._result_q,
                                    self._ctrl_q, ack_q, self._log_q,
                                    self.worker_fn),
                              daemon=True,
                              name=f'awp-{self.pool_name}-{len(self._procs)}')
        p.start()
        self._procs.append(p)
        self._ack_qs[p.pid] = ack_q
        self._proc_tids[p.pid] = None

    def _resize_to(self, target, reason=''):
        cap = self.governor.worker_cap()
        lo = min(self.governor.min_workers, cap)   # 下限也钳进 cap，保证不破 1.1 倍硬顶
        target = int(max(lo, min(int(target), cap)))
        with self._procs_lock:
            self._prune_procs()
            cur = len(self._procs)
            # 同步调度器意图：仅在实际增减进程时更新（target != cur）。
            # 冷却期内 target_workers() 返回 current 的"保持"信号若也覆盖，
            # _desired_target 会被实时进程数顶掉 → 目标进程数显示回升
            # （用户反馈：调低限制后约 1 秒目标进程数又回到当前进程数）。
            if target != cur:
                self._desired_target = target
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

    def kill_excess(self, target):
        """立即杀死超出 target 的进程，放弃这些进程正在运行的任务数据。

        用于 GUI"紧急执行"按钮：目标进程数来自调度器意图值，超出部分
        直接 terminate / kill（不等待任务完成，正在运行的任务结果丢弃）。
        队列中尚未被取走的任务不受影响，由剩余进程继续执行。

        返回被终止的进程数。
        """
        target = int(max(0, target))
        with self._procs_lock:
            self._prune_procs()
            cur = len(self._procs)
            if cur <= target:
                return 0
            victims = self._procs[target:]
            # 收集受害者进程正在运行的任务（由 ack 映射精确对应）
            abandoned = []
            for p in victims:
                tid = self._proc_tids.get(p.pid)
                if tid is not None:
                    abandoned.append(tid)
            for p in victims:
                try:
                    p.terminate()
                except Exception:
                    pass
            for p in victims:
                try:
                    p.join(timeout=3)
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=2)
                except Exception:
                    pass
            self._procs = [p for p in self._procs if p not in set(victims)]
            for p in victims:
                try:
                    self._ack_qs.pop(p.pid, None)
                    self._proc_tids.pop(p.pid, None)
                except Exception:
                    pass
        # 放弃被终止进程正在运行的任务（这些 future 不再有结果）
        with self._futures_lock:
            for tid in abandoned:
                fut = self._futures.pop(tid, None)
                if fut is not None:
                    fut.set_result(None)          # 标记为"已放弃"（结果 None）
        with self._cond:
            self._pending = max(0, self._pending - len(abandoned))
            self._cond.notify_all()
        if victims:
            self.state.add_event(
                f'[{self.pool_name}] 紧急执行：立即终止 {len(victims)} 个进程'
                f'（目标 {target}，放弃 {len(abandoned)} 个正在运行的任务）')
        return len(victims)

    def abort(self):
        """紧急停止：立即终止所有 worker 进程并放弃所有未完成任务。

        调用方通常是 GUI 紧急停止按钮（param_optimizer.request_emergency_stop）。
        _stopped 置位后，with/__exit__ 触发的 shutdown(wait=True) 会立即返回，
        不再等待任何任务或进程收尾。返回被终止的进程数。
        """
        self._stopped.set()
        with self._procs_lock:
            self._prune_procs()
            procs = list(self._procs)
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.join(timeout=3)
                if p.is_alive():
                    p.kill()
                    p.join(timeout=2)
            except Exception:
                pass
        with self._procs_lock:
            self._procs = []
            for q in self._ack_qs.values():
                try:
                    q.close()
                except Exception:
                    pass
            self._ack_qs.clear()
            self._proc_tids.clear()
        try:
            self._log_q.close()
        except Exception:
            pass
        # 放弃所有未完成任务：主线程 _wait_future 的 done() 立即成立，不卡等待
        with self._futures_lock:
            futs = list(self._futures.values())
            self._futures.clear()
        for f in futs:
            f.set_result(None)
        with self._cond:
            self._pending = 0
            self._cond.notify_all()
        self._desired_target = None
        _unregister_pool(self)
        self.state.update(running=False, workers=0, target_workers=0,
                          tasks_pending=0)
        if procs:
            self.state.add_event(
                f'[{self.pool_name}] 紧急停止：已强制终止 {len(procs)} 个进程')
        return len(procs)
