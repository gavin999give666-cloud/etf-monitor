"""
参数优化器 · 资源控制面板（Tkinter 原生界面，随 EXE 分发，无需浏览器）
==================================================================
运算配置与实时监控功能：
- 运算模式选择：全量运算 / 轻量运算（模式定义来自 optimizer_modes.py 注册表，
  新增模式无需改动本界面代码）
- 试验次数与结果保存路径：可自定义文件保存位置与文件名
- 实时显示：程序自身 CPU 占用 / 系统整体 CPU / 当前进程数 / 目标进程数 / 资源限制
- 实时调节：滑块 + 快捷按钮设置"最大 CPU 使用率"（100% = 最大性能模式）
- 趋势曲线：CPU 与进程数的最近 240 个采样
- 事件日志：进程增减、限制变更、运算启停等

DPI 适配：启动时启用进程级 DPI 感知（dpi_utils.setup_dpi_awareness），
按显示器缩放系数放大窗口几何、画布与内边距等像素尺寸（self._px），
并通过 tk scaling 对齐字体基线 —— 在 100% ~ 200% 缩放下均清晰可读。

线程模型：本界面运行在 Tk 主线程；优化任务运行在后台线程；
两者通过 adaptive_pool.DashboardState（线程安全）通信。

一般不直接使用 —— param_optimizer.py 通过 `--gui` 自动集成。
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from dpi_utils import apply_tk_scaling, setup_dpi_awareness, window_scale
from optimizer_modes import MODES, build_run, get_mode_by_label

# 配色
BG = '#12171f'
PANEL = '#1a2334'
PANEL2 = '#1d2537'
BORDER = '#2a3450'
TEXT = '#dfe6f3'
DIM = '#8b96ad'
ACC = '#4f8cff'
GREEN = '#2ecc71'
RED = '#e74c3c'
CYAN = '#00d4c8'

HIST_MAX = 180


class OptimizerGUI:
    """参数优化器资源控制面板"""

    def __init__(self, governor, state,
                 title='V6.2.1 参数优化器 · 资源控制面板',
                 shutdown_cb=None):
        self.governor = governor
        self.state = state
        self._shutdown_cb = shutdown_cb
        self._last_evt = 0
        self._slider_pending = None
        self._slider_after = None
        self._setting_slider = False
        self._cfg_controls = []   # [(widget, normal_state), ...] 运行中禁用

        # ── DPI 适配：先启用进程 DPI 感知（必须在创建根窗口之前），
        #    再按当前显示器缩放系数放大窗口几何与像素尺寸 ──
        setup_dpi_awareness()
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=BG)
        self._mapped_once = False
        self._S = window_scale(self.root)     # 初始估算（映射前显示器 DPI 可能不准）
        apply_tk_scaling(self.root)
        self.root.bind('<Map>', self._on_map)  # 首次显示后再精校准一次
        self.root.geometry(f'{self._px(940)}x{self._px(780)}')
        self.root.minsize(self._px(820), self._px(620))
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self._build_ui()
        # 立即渲染一帧，避免窗口空白
        self.root.after(50, self._tick)

    def _px(self, v):
        """按当前 DPI 缩放系数放大像素尺寸（1.0 = 100% 缩放时不放大）"""
        return int(round(v * self._S))

    def _on_map(self, _event=None):
        """窗口首次映射到屏幕后精校准 DPI（映射前 GetDpiForWindow 可能不准确）"""
        if self._mapped_once:
            return
        self._mapped_once = True
        self._S = window_scale(self.root)
        apply_tk_scaling(self.root)
        self.root.geometry(f'{self._px(940)}x{self._px(780)}')
        self.root.minsize(self._px(820), self._px(620))

    # ══════════════════════ UI 构建 ══════════════════════

    def _build_ui(self):
        root = self.root

        # ── 顶栏 ──
        top = tk.Frame(root, bg=BG)
        top.pack(fill='x', padx=self._px(14), pady=(self._px(12), self._px(8)))
        tk.Label(top, text='V6.2.1 智能参数优化器', font=('Microsoft YaHei', 13, 'bold'),
                 bg=BG, fg=TEXT).pack(side='left')
        self._lbl_mode = tk.Label(top, text='', font=('Microsoft YaHei', 10), bg=BG, fg=CYAN)
        self._lbl_mode.pack(side='right', padx=self._px(10))
        self._lbl_status = tk.Label(top, text='等待任务...', font=('Microsoft YaHei', 10),
                                    bg=BG, fg=DIM)
        self._lbl_status.pack(side='right')

        # ── 运算配置面板（模式选择 / 试验次数 / 保存路径 / 开始按钮）──
        self._build_cfg_panel(root)

        # ── 指标卡片 ──
        cards = tk.Frame(root, bg=BG)
        cards.pack(fill='x', padx=self._px(14))
        self._gauge_prog = self._make_card(cards, '程序自身 CPU 占用')
        self._gauge_sys = self._make_card(cards, '系统整体 CPU')
        self._card_workers = self._make_num_card(cards, '当前进程数')
        self._card_target = self._make_num_card(cards, '目标进程数')
        self._card_limit = self._make_num_card(cards, '资源限制')

        # ── 资源限制控制 ──
        ctrl = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        ctrl.pack(fill='x', padx=self._px(14), pady=self._px(10))
        tk.Label(ctrl, text='最大 CPU 使用率（程序自身占用上限；100% = 最大性能模式）',
                 bg=PANEL, fg=DIM, font=('Microsoft YaHei', 10)).pack(anchor='w',
                 padx=self._px(12), pady=(self._px(8), self._px(2)))
        row = tk.Frame(ctrl, bg=PANEL)
        row.pack(fill='x', padx=self._px(12), pady=(0, self._px(10)))
        self._slider = tk.Scale(row, from_=1, to=100, orient='horizontal',
                                length=self._px(360),
                                bg=PANEL, fg=TEXT, troughcolor=PANEL2, highlightthickness=0,
                                command=self._on_slider)
        self._slider.set(int(round(self.governor.limit_pct)))
        self._slider.pack(side='left')
        self._lbl_limit_val = tk.Label(row, text=f'{int(round(self.governor.limit_pct))}%',
                                       font=('Consolas', 26, 'bold'), bg=PANEL, fg=ACC, width=5)
        self._lbl_limit_val.pack(side='left', padx=(self._px(12), self._px(16)))
        for v, name in ((20, '20%'), (50, '50%'), (80, '80%'), (100, '100% 最大性能')):
            tk.Button(row, text=name, command=lambda vv=v: self._set_limit(vv),
                      bg=PANEL2, fg=TEXT, relief='flat', activebackground=ACC,
                      activeforeground='#ffffff', cursor='hand2',
                      font=('Microsoft YaHei', 10), padx=self._px(12),
                      pady=self._px(4)).pack(side='left', padx=self._px(4))

        # ── 趋势图 ──
        charts = tk.Frame(root, bg=BG)
        charts.pack(fill='both', expand=True, padx=self._px(14))
        self._chart_cpu = self._make_chart(charts, 'CPU 利用率趋势',
                                           '程序自身 #4f8cff | 系统 #e74c3c')
        self._chart_workers = self._make_chart(charts, '进程数趋势',
                                               '当前 #00d4c8 | 目标 #8b96ad')

        # ── 事件日志 ──
        logbox = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        logbox.pack(fill='x', padx=self._px(14), pady=(0, self._px(12)))
        tk.Label(logbox, text='事件日志', bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(anchor='w',
                 padx=self._px(10), pady=(self._px(6), 0))
        self._log = tk.Text(logbox, height=6, bg=PANEL, fg=DIM, relief='flat',
                            font=('Consolas', 9), state='disabled', wrap='none')
        self._log.pack(fill='x', padx=self._px(8), pady=(self._px(2), self._px(8)))

    # ══════════════════════ 运算配置 ══════════════════════

    def _build_cfg_panel(self, root):
        """运算配置面板：模式选择 / 试验次数 / 保存路径 / 开始按钮。

        模式定义来自 optimizer_modes.MODES 注册表 —— 新增运算模式
        只需在注册表中添加条目，无需修改本界面代码。
        """
        cfg = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        cfg.pack(fill='x', padx=self._px(14), pady=(0, self._px(10)))
        tk.Label(cfg, text='运算配置（选择模式后点击【开始优化】）', bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(anchor='w',
                 padx=self._px(12), pady=(self._px(6), 0))

        # 行1：运算模式 + 试验次数
        row1 = tk.Frame(cfg, bg=PANEL)
        row1.pack(fill='x', padx=self._px(12), pady=(self._px(6), self._px(2)))
        tk.Label(row1, text='运算模式', bg=PANEL, fg=TEXT,
                 font=('Microsoft YaHei', 10)).pack(side='left')
        self._combo_mode = ttk.Combobox(row1, values=[m.label for m in MODES],
                                        state='readonly', width=14)
        self._combo_mode.pack(side='left', padx=(self._px(8), self._px(18)))
        self._combo_mode.bind('<<ComboboxSelected>>', self._on_mode_change)
        tk.Label(row1, text='试验次数', bg=PANEL, fg=TEXT,
                 font=('Microsoft YaHei', 10)).pack(side='left')
        self._spin_trials = tk.Spinbox(row1, from_=10, to=100000, increment=100,
                                       width=9, font=('Consolas', 10),
                                       bg=PANEL2, fg=TEXT, buttonbackground=PANEL2,
                                       relief='flat')
        self._spin_trials.pack(side='left', padx=(self._px(8), 0))

        # 行2：结果保存路径 + 浏览
        row2 = tk.Frame(cfg, bg=PANEL)
        row2.pack(fill='x', padx=self._px(12), pady=(self._px(2), self._px(2)))
        tk.Label(row2, text='结果保存', bg=PANEL, fg=TEXT,
                 font=('Microsoft YaHei', 10)).pack(side='left')
        self._entry_path = tk.Entry(row2, bg=PANEL2, fg=TEXT, relief='flat',
                                    font=('Consolas', 9))
        self._entry_path.pack(side='left', fill='x', expand=True,
                              padx=(self._px(8), self._px(6)), ipady=self._px(3))
        tk.Button(row2, text='浏览...', command=self._browse_path,
                  bg=PANEL2, fg=TEXT, relief='flat', activebackground=ACC,
                  activeforeground='#ffffff', cursor='hand2',
                  font=('Microsoft YaHei', 9), padx=self._px(12),
                  pady=self._px(2)).pack(side='left')

        # 行3：模式描述 + 开始按钮
        row3 = tk.Frame(cfg, bg=PANEL)
        row3.pack(fill='x', padx=self._px(12), pady=(self._px(2), self._px(8)))
        self._lbl_mode_desc = tk.Label(row3, text='', bg=PANEL, fg=DIM,
                                       font=('Microsoft YaHei', 9), anchor='w')
        self._lbl_mode_desc.pack(side='left', fill='x', expand=True)
        self._btn_start = tk.Button(row3, text='开始优化',
                                    command=self._start_optimization,
                                    bg=GREEN, fg='#ffffff', relief='flat',
                                    activebackground='#27ae60', activeforeground='#ffffff',
                                    cursor='hand2', font=('Microsoft YaHei', 11, 'bold'),
                                    padx=self._px(24), pady=self._px(4))
        self._btn_start.pack(side='right')

        # 初始选中第一个模式（全量运算）
        self._combo_mode.current(0)
        self._apply_mode(get_mode_by_label(self._combo_mode.get()))
        self._cfg_controls = [
            (self._combo_mode, 'readonly'),
            (self._spin_trials, 'normal'),
            (self._entry_path, 'normal'),
            (self._btn_start, 'normal'),
        ]

    def _apply_mode(self, mode):
        """按模式定义刷新试验次数默认值、描述与默认保存文件名"""
        if mode is None:
            return
        self._spin_trials.delete(0, 'end')
        self._spin_trials.insert(0, str(mode.default_trials))
        self._lbl_mode_desc.config(text=mode.desc)
        self._entry_path.delete(0, 'end')
        self._entry_path.insert(0, os.path.join(os.getcwd(),
                                                mode.default_file + '.json'))

    def _on_mode_change(self, _event=None):
        self._apply_mode(get_mode_by_label(self._combo_mode.get()))

    def _browse_path(self):
        mode = get_mode_by_label(self._combo_mode.get())
        init_file = mode.default_file + '.json' if mode else 'optimizer_results.json'
        path = filedialog.asksaveasfilename(
            title='选择结果保存位置',
            initialdir=os.getcwd(),
            initialfile=init_file,
            defaultextension='.json',
            filetypes=[('JSON 结果文件', '*.json'), ('所有文件', '*.*')])
        if path:
            self._entry_path.delete(0, 'end')
            self._entry_path.insert(0, path)

    def _set_cfg_enabled(self, enabled):
        for widget, normal_state in self._cfg_controls:
            try:
                widget.configure(state=normal_state if enabled else 'disabled')
            except Exception:
                pass

    def _start_optimization(self):
        """读取配置 → 构建执行计划 → 后台线程启动优化"""
        if self.state.snapshot().get('running', False):
            return
        mode = get_mode_by_label(self._combo_mode.get())
        if mode is None:
            messagebox.showerror('启动失败', '未识别的运算模式')
            return
        try:
            trials = int(self._spin_trials.get())
            trials = max(10, min(trials, 100000))
        except ValueError:
            trials = mode.default_trials
        out_path = self._entry_path.get().strip() or \
            os.path.join(os.getcwd(), mode.default_file + '.json')

        # 并行参考数随当前限制收敛（限制模式下不超限；100% 保持原上限）
        n_jobs = max(1, min(14, int(self.governor.initial_workers())))
        ga_n_jobs = max(1, min(10, n_jobs))
        try:
            run_fn, kwargs = build_run(mode.key, trials, out_path,
                                       n_jobs=n_jobs, ga_n_jobs=ga_n_jobs)
        except Exception as e:
            messagebox.showerror('启动失败', f'无法构建计算任务：\n{e}')
            return

        self._set_cfg_enabled(False)
        self._btn_start.config(text='优化进行中...', state='disabled')
        self.state.update(running=True, pool=mode.label, mode=mode.label)
        self.state.add_event(f'开始{mode.label}：{trials} trials，结果 → {out_path}')

        threading.Thread(target=self._run_worker,
                         args=(run_fn, kwargs, mode),
                         daemon=True, name='optimizer-gui-thread').start()

    def _run_worker(self, run_fn, kwargs, mode):
        """后台线程执行体：调用计算入口，完成后回调主线程解锁配置"""
        ok = False
        try:
            run_fn(**kwargs)
            ok = True
            self.state.add_event(f'{mode.label}完成，结果已保存')
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.state.add_event(f'{mode.label}出错: {e}')
        finally:
            self.state.update(running=False)
            self.root.after(0, self._finish_run, ok)
            if ok and self._shutdown_cb:
                try:
                    self._shutdown_cb()
                except Exception:
                    pass

    def _finish_run(self, ok):
        """回到 Tk 主线程：解锁配置，允许修改后再次运行（便于迭代调试）"""
        self._set_cfg_enabled(True)
        self._btn_start.config(text='开始优化', state='normal')
        self.state.add_event('配置已解锁，可修改参数后再次运行')

    def _make_card(self, parent, title):
        card = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(side='left', fill='both', expand=True, padx=self._px(3))
        tk.Label(card, text=title, bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(pady=(self._px(8), 0))
        c = tk.Canvas(card, width=self._px(150), height=self._px(88),
                      bg=PANEL, highlightthickness=0)
        c.pack()
        return c

    def _make_num_card(self, parent, title):
        card = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(side='left', fill='both', expand=True, padx=self._px(3))
        tk.Label(card, text=title, bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(pady=(self._px(8), 0))
        lbl = tk.Label(card, text='0', font=('Consolas', 24, 'bold'), bg=PANEL, fg=TEXT)
        lbl.pack(pady=(self._px(10), self._px(14)))
        return lbl

    def _make_chart(self, parent, title, legend):
        box = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        box.pack(side='left', fill='both', expand=True,
                 padx=self._px(3), pady=self._px(2))
        head = tk.Frame(box, bg=PANEL)
        head.pack(fill='x', padx=self._px(10), pady=(self._px(6), 0))
        tk.Label(head, text=title, bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(side='left')
        tk.Label(head, text=legend, bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 8)).pack(side='right')
        c = tk.Canvas(box, height=self._px(140), bg=PANEL, highlightthickness=0)
        c.pack(fill='both', expand=True, padx=self._px(6), pady=(0, self._px(8)))
        return c

    # ══════════════════════ 渲染 ══════════════════════

    def _draw_gauge(self, canvas, pct, color):
        canvas.delete('all')
        # 画布随 DPI 放大，圆心/半径/线宽同步等比缩放
        cx, cy, r = self._px(75), self._px(80), self._px(58)
        width = max(4, self._px(9))
        canvas.create_arc(cx - r, cy - r, cx + r, cy + r, start=180, extent=180,
                          style='arc', width=width, outline='#26304d')
        val = max(0.0, min(100.0, float(pct)))
        if val > 0:
            canvas.create_arc(cx - r, cy - r, cx + r, cy + r, start=180,
                              extent=180 * val / 100.0, style='arc',
                              width=width, outline=color)
        canvas.create_text(cx, cy + self._px(14), text=f'{val:.1f}%', fill=TEXT,
                           font=('Consolas', 14, 'bold'))

    def _draw_chart(self, canvas, series, maxv, ylabel):
        canvas.delete('all')
        w = max(canvas.winfo_width(), self._px(100))
        h = max(canvas.winfo_height(), self._px(100))
        pl, pr, pt, pb = self._px(34), self._px(8), self._px(8), self._px(18)
        pw, ph = w - pl - pr, h - pt - pb
        for i in range(5):
            y = pt + ph - ph * i / 4
            canvas.create_line(pl, y, w - pr, y, fill='#1e2740')
            canvas.create_text(2, y, anchor='w', text=str(int(maxv * i / 4)),
                               fill=DIM, font=('Consolas', 8))
        if series and len(series[0]['data']) >= 2:
            n = len(series[0]['data'])
            for s in series:
                pts = []
                for i, v in enumerate(s['data']):
                    x = pl + pw * i / max(n - 1, 1)
                    y = pt + ph - ph * min(float(v), maxv) / max(maxv, 1e-6)
                    pts.append((x, y))
                canvas.create_line(pts, fill=s['color'], width=2, smooth=True)
        canvas.create_text(pl, h - self._px(4), anchor='w', text=ylabel, fill=DIM,
                           font=('Consolas', 8))

    def _tick(self):
        try:
            self._render(self.state.snapshot())
        except Exception:
            pass
        self.root.after(1000, self._tick)

    def _render(self, snap):
        prog = float(snap.get('program_cpu', 0))
        sys_ = float(snap.get('system_cpu', 0))
        workers = snap.get('workers', 0)
        target = snap.get('target_workers', 0)
        limit = float(snap.get('limit', 100))
        mode = snap.get('mode', '')
        running = snap.get('running', False)
        pool = snap.get('pool', '')

        self._draw_gauge(self._gauge_prog, prog, ACC)
        self._draw_gauge(self._gauge_sys, sys_, RED)
        self._card_workers.config(text=str(workers))
        self._card_target.config(text=str(target))
        self._card_limit.config(text=f'{limit:.0f}%')
        self._lbl_status.config(text=f'运行中 · {pool}' if running else '等待任务...',
                                fg=GREEN if running else DIM)
        self._lbl_mode.config(text=mode)

        self._draw_chart(self._chart_cpu,
                         [{'data': snap.get('hist_program_cpu', [])[-HIST_MAX:], 'color': ACC},
                          {'data': snap.get('hist_system_cpu', [])[-HIST_MAX:], 'color': RED}],
                         100, '100%')
        tw = snap.get('hist_target_workers', []) or [0]
        w = snap.get('hist_workers', []) or [0]
        mx = max(max(tw), max(w), 1) * 1.3
        self._draw_chart(self._chart_workers,
                         [{'data': w[-HIST_MAX:], 'color': CYAN},
                          {'data': tw[-HIST_MAX:], 'color': DIM}],
                         mx, str(int(mx)))

        evts = snap.get('events', [])
        if len(evts) > self._last_evt:
            self._log.config(state='normal')
            for e in evts[self._last_evt:]:
                self._log.insert('end', e + '\n')
            self._log.see('end')
            self._log.config(state='disabled')
            self._last_evt = len(evts)

    # ══════════════════════ 限制调节 ══════════════════════

    def _on_slider(self, raw):
        if self._setting_slider:
            return
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            return
        self._lbl_limit_val.config(text=f'{v}%')
        if self._slider_after:
            self.root.after_cancel(self._slider_after)
        self._slider_pending = v
        self._slider_after = self.root.after(400, self._apply_pending)

    def _apply_pending(self):
        self._slider_after = None
        if self._slider_pending is not None:
            self._set_limit(self._slider_pending)
            self._slider_pending = None

    def _set_limit(self, v):
        v = int(min(max(v, 1), 100))
        self._setting_slider = True
        self._slider.set(v)
        self._setting_slider = False
        self._lbl_limit_val.config(text=f'{v}%')
        self.governor.set_limit(v)
        # 立即同步面板状态（与进程池激活与否无关）
        self.state.update(limit=self.governor.limit_pct,
                          mode=self.governor.mode_label())
        label = '（最大性能模式）' if v >= 99.5 else ''
        self.state.add_event(f'资源限制已更新 → {v}% {label}')

    def _on_close(self):
        if self.state.snapshot().get('running', False):
            from tkinter import messagebox
            if not messagebox.askyesno('确认', '优化仍在进行，确定退出？'):
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()
