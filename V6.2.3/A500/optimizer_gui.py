"""
参数优化器 · 资源控制面板（Tkinter 原生界面，随 EXE 分发，无需浏览器）
==================================================================
运算配置与实时监控功能：
- 运算模式选择：全量运算 / 轻量运算（模式定义来自 optimizer_modes.py 注册表，
  新增模式无需改动本界面代码）
- 断点续算开关：勾选=从断点继续；取消=放弃断点，从头全新计算（仅全量运算）
- 试验次数与结果保存路径：可自定义文件保存位置与文件名
- 实时显示：程序自身 CPU 占用 / 系统整体 CPU / 当前进程数 / 目标进程数 / 资源限制
- 实时调节：滑块 + 快捷按钮设置"最大 CPU 使用率"（100% = 最大性能模式）
- 趋势曲线：CPU 与进程数的最近 240 个采样
- 事件日志：进程增减、限制变更、运算启停等
- 右侧终端打印区：捕获计算期间终端（stdout/stderr）的全部实时输出
  —— 详细事件打印与计算进度（tqdm 进度条、Phase 阶段输出、Optuna/GA 信息
  等）逐行保留显示；原终端照常显示，面板关闭后恢复。

DPI 适配：启动时启用进程级 DPI 感知（dpi_utils.setup_dpi_awareness），
按显示器缩放系数放大窗口几何、画布与内边距等像素尺寸（self._px），
并通过 tk scaling 对齐字体基线 —— 在 100% ~ 200% 缩放下均清晰可读。

线程模型：本界面运行在 Tk 主线程；优化任务运行在后台线程；
两者通过 adaptive_pool.DashboardState（线程安全）通信。

一般不直接使用 —— param_optimizer.py 通过 `--gui` 自动集成。
"""

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from dpi_utils import apply_tk_scaling, setup_dpi_awareness, window_scale
from optimizer_modes import MODES, build_run, get_mode_by_label

import param_optimizer as po

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

# Windows 进程/线程优先级常量（kernel32）
_PROC_HIGH = 0x80          # HIGH_PRIORITY_CLASS
_THREAD_HIGHEST = 2        # THREAD_PRIORITY_HIGHEST


def _raise_priority():
    """将控制面板所在进程提升为高优先级，保证计算满载时 GUI 仍实时刷新。

    同进程内优化线程与 worker 满载时，普通优先级的 Tk 主线程会被抢占，
    表现为界面周期性假死。将进程提升为 HIGH_PRIORITY_CLASS（最高档之一，
    更高档 REALTIME 需管理员特权且会抢占系统关键线程，故不采用），
    并把 GUI 主线程设为 THREAD_PRIORITY_HIGHEST。
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), _PROC_HIGH)
        k.SetThreadPriority(k.GetCurrentThread(), _THREAD_HIGHEST)
    except Exception:
        pass


class OptimizerGUI:
    """参数优化器资源控制面板"""

    def __init__(self, governor, state,
                 title='V6.2.3 参数优化器 · 资源控制面板',
                 shutdown_cb=None):
        self.governor = governor
        self.state = state
        # ── 双模块加载防护 ──
        # 以 `python param_optimizer.py --gui` 运行时，本文件 import 的
        # param_optimizer 与入口 __main__ 是同一文件的**两个模块实例**，
        # 优化任务（po.run_light_optimization 等）读的是 param_optimizer
        # 实例的全局；必须把入口侧创建好的 GOVERNOR / DASHBOARD_STATE /
        # ADAPTIVE_ENABLED 同步过去，否则自适应调度与监控数据都接不上。
        po.GOVERNOR = governor
        po.DASHBOARD_STATE = state
        po.ADAPTIVE_ENABLED = True
        self._shutdown_cb = shutdown_cb
        self._last_evt = 0
        self._log_seq = 0          # 运算日志增量游标
        self._slider_pending = None
        self._slider_after = None
        self._setting_slider = False
        self._cfg_controls = []   # [(widget, normal_state), ...] 运行中禁用

        # ── DPI 适配：先启用进程 DPI 感知（必须在创建根窗口之前），
        #    再按当前显示器缩放系数放大窗口几何与像素尺寸 ──
        _raise_priority()          # 提升控制面板进程/线程优先级（防假死）
        setup_dpi_awareness()
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=BG)
        self._mapped_once = False
        self._S = window_scale(self.root)     # 初始估算（映射前显示器 DPI 可能不准）
        apply_tk_scaling(self.root)
        self.root.bind('<Map>', self._on_map)  # 首次显示后再精校准一次
        self.root.geometry(f'{self._px(1500)}x{self._px(900)}')
        self.root.minsize(self._px(1180), self._px(760))
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        po.clear_control_flags()   # 启动时清理残留信号文件
        self._apply_modern_style()  # 现代化外观（clam 扁平主题，替代 WinXP 控件）
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
        self.root.geometry(f'{self._px(1500)}x{self._px(900)}')
        self.root.minsize(self._px(1180), self._px(760))

    def _apply_modern_style(self):
        """现代化外观：clam 扁平主题 + 深色自定义样式。

        替代默认 WinXP 风格控件（3D 凸起滑块/滚动条/下拉箭头等），
        统一为扁平、低饱和、悬停高亮的现代视觉。
        必须在 Tk 根窗口创建后、构建控件前调用。
        """
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            return
        f = ('Microsoft YaHei', 10)
        # 全局默认：深色面板 + 细边框
        style.configure('.', background=PANEL, foreground=TEXT, font=f,
                        fieldbackground=PANEL2, bordercolor=BORDER)
        style.map('.', foreground=[('disabled', DIM)],
                  background=[('disabled', PANEL2)])
        # 下拉框
        style.configure('Modern.TCombobox', padding=(6, 4),
                        arrowcolor=TEXT, arrowsize=16)
        style.map('Modern.TCombobox',
                  fieldbackground=[('readonly', PANEL2)],
                  foreground=[('readonly', TEXT)],
                  selectbackground=[('readonly', PANEL2)],
                  selectforeground=[('readonly', TEXT)],
                  bordercolor=[('focus', ACC), ('active', '#33405f')])
        # 复选（断点续算）
        style.configure('Modern.TCheckbutton', background=PANEL,
                        foreground=TEXT, indicatorcolor=PANEL2,
                        bordercolor=BORDER, padding=(4, 3))
        style.map('Modern.TCheckbutton',
                  background=[('active', PANEL)],
                  indicatorcolor=[('selected', ACC), ('active', PANEL2)],
                  foreground=[('disabled', DIM)])
        # 数字微调框
        style.configure('Modern.TSpinbox', padding=(4, 3),
                        arrowsize=14, arrowcolor=TEXT,
                        fieldbackground=PANEL2, bordercolor=BORDER)
        # 滚动条（细窄、扁平、悬停高亮）
        style.configure('Modern.Vertical.TScrollbar', background=PANEL2,
                        troughcolor=PANEL, bordercolor=PANEL, arrowcolor=TEXT,
                        relief='flat')
        style.configure('Modern.Horizontal.TScrollbar', background=PANEL2,
                        troughcolor=PANEL, bordercolor=PANEL, arrowcolor=TEXT,
                        relief='flat')
        style.map('Modern.Vertical.TScrollbar',
                  background=[('active', '#33405f')])
        style.map('Modern.Horizontal.TScrollbar',
                  background=[('active', '#33405f')])
        # 滑块（扁平槽 + 细边框 + 中调滑块头）
        style.configure('Modern.Horizontal.TScale', background=PANEL,
                        troughcolor=PANEL2, bordercolor=BORDER,
                        lightcolor='#33405f', darkcolor='#33405f')
        style.map('Modern.Horizontal.TScale', background=[('active', PANEL)])
        # 进度条（蓝色高亮填充）
        style.configure('Modern.Horizontal.TProgressbar', background=ACC,
                        troughcolor=PANEL2, bordercolor=BORDER, borderwidth=0,
                        lightcolor=ACC, darkcolor=ACC)

    # ══════════════════════ UI 构建 ══════════════════════

    def _build_ui(self):
        root = self.root

        # ── 顶栏 ──
        top = tk.Frame(root, bg=BG)
        top.pack(fill='x', padx=self._px(14), pady=(self._px(12), self._px(8)))
        tk.Label(top, text='V6.2.3 智能参数优化器', font=('Microsoft YaHei', 13, 'bold'),
                 bg=BG, fg=TEXT).pack(side='left')
        self._lbl_mode = tk.Label(top, text='', font=('Microsoft YaHei', 10), bg=BG, fg=CYAN)
        self._lbl_mode.pack(side='right', padx=self._px(10))
        self._lbl_status = tk.Label(top, text='等待任务...', font=('Microsoft YaHei', 10),
                                    bg=BG, fg=DIM)
        self._lbl_status.pack(side='right')

        # ── 左右分栏：左=资源监控与配置，右=终端打印区（详细事件与计算进度）──
        paned = tk.PanedWindow(root, orient='horizontal', bg=BG,
                               sashwidth=self._px(6), sashrelief='flat',
                               borderwidth=0)
        paned.pack(fill='both', expand=True, padx=self._px(14), pady=(0, self._px(12)))
        self._paned = paned
        left = tk.Frame(paned, bg=BG)
        right = tk.Frame(paned, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1)
        paned.add(left, minsize=self._px(640))
        paned.add(right, minsize=self._px(420))

        # ── 运算配置面板（模式选择 / 试验次数 / 保存路径 / 开始按钮）──
        self._build_cfg_panel(left)

        # ── 指标卡片 ──
        cards = tk.Frame(left, bg=BG)
        cards.pack(fill='x')
        self._gauge_prog = self._make_card(cards, '程序自身 CPU 占用')
        self._gauge_sys = self._make_card(cards, '系统整体 CPU')
        self._card_workers = self._make_num_card(cards, '当前进程数')
        self._card_target = self._make_num_card(cards, '目标进程数')
        self._card_limit = self._make_num_card(cards, '资源限制')

        # ── 资源限制控制 ──
        ctrl = tk.Frame(left, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        ctrl.pack(fill='x', pady=self._px(10))
        tk.Label(ctrl, text='最大 CPU 使用率（程序自身占用上限；100% = 最大性能模式）',
                 bg=PANEL, fg=DIM, font=('Microsoft YaHei', 10)).pack(anchor='w',
                 padx=self._px(12), pady=(self._px(8), self._px(2)))
        row = tk.Frame(ctrl, bg=PANEL)
        row.pack(fill='x', padx=self._px(12), pady=(0, self._px(10)))
        self._slider = ttk.Scale(row, from_=1, to=100, orient='horizontal',
                                 length=self._px(360),
                                 style='Modern.Horizontal.TScale',
                                 command=self._on_slider)
        # ttk.Scale.set() 会同步触发 -command，需临时加锁避免在
        # _lbl_limit_val 尚未创建时触发 _on_slider
        self._setting_slider = True
        self._slider.set(int(round(self.governor.limit_pct)))
        self._setting_slider = False
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
        charts = tk.Frame(left, bg=BG)
        charts.pack(fill='x')
        self._chart_cpu = self._make_chart(charts, 'CPU 利用率趋势',
                                           '程序自身 #4f8cff | 系统 #e74c3c')
        self._chart_workers = self._make_chart(charts, '进程数趋势',
                                               '当前 #00d4c8 | 目标 #8b96ad')

        # ── 事件日志（概要，占据左栏剩余空间）──
        logbox = tk.Frame(left, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        logbox.pack(fill='both', expand=True)
        tk.Label(logbox, text='事件日志', bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(anchor='w',
                 padx=self._px(10), pady=(self._px(6), 0))
        log_body = tk.Frame(logbox, bg=PANEL)
        log_body.pack(fill='both', expand=True, padx=self._px(8),
                      pady=(self._px(2), self._px(8)))
        self._log = tk.Text(log_body, height=18, bg=PANEL, fg='#c8d6e5',
                            insertbackground='#c8d6e5',
                            font=('Consolas', self._px(9)),
                            relief='flat', wrap='none', state='disabled',
                            padx=6, pady=4)
        self._log_vsb = ttk.Scrollbar(log_body, command=self._log.yview,
                                      style='Modern.Vertical.TScrollbar')
        self._log.configure(yscrollcommand=self._log_vsb.set)
        self._log_vsb.pack(side='right', fill='y')
        self._log.pack(side='left', fill='both', expand=True)

        # ── 右侧：独立计算进度面板 + 终端打印区 ──
        self._build_progress_panel(right)
        self._build_term_panel(right)

    def _build_term_panel(self, parent):
        """右侧终端打印区：捕获计算期间终端（stdout/stderr）的全部实时输出。

        数据来自 DashboardState.log_lines —— param_optimizer 的 stdout/stderr
        分流器（po.install_stdout_tee）将每个 print 更新逐行写入，
        本面板按行渲染并自动滚动；行数超限时裁剪头部。
        计算进度已独立到 _build_progress_panel，本区只保留详细打印结果。
        """
        head = tk.Frame(parent, bg=PANEL)
        head.pack(fill='x', padx=self._px(10), pady=(self._px(6), 0))
        tk.Label(head, text='终端打印区 · 详细事件与结果',
                 bg=PANEL, fg=DIM, font=('Microsoft YaHei', 9)).pack(side='left')
        tk.Button(head, text='清空', command=self._clear_term,
                  bg=PANEL2, fg=TEXT, relief='flat', activebackground=ACC,
                  activeforeground='#ffffff', cursor='hand2',
                  font=('Microsoft YaHei', 9), padx=self._px(10),
                  pady=self._px(2)).pack(side='right')

        body = tk.Frame(parent, bg=PANEL)
        body.pack(fill='both', expand=True, padx=self._px(8),
                  pady=(self._px(4), self._px(8)))
        self._term = tk.Text(body, bg=PANEL, fg='#c8d6e5',
                             insertbackground='#c8d6e5',
                             font=('Consolas', self._px(9)),
                             relief='flat', wrap='none', state='disabled',
                             padx=6, pady=4)
        vsb = ttk.Scrollbar(body, command=self._term.yview,
                            style='Modern.Vertical.TScrollbar')
        hsb = ttk.Scrollbar(body, orient='horizontal', command=self._term.xview,
                            style='Modern.Horizontal.TScrollbar')
        self._term.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side='right', fill='y')
        hsb.pack(side='bottom', fill='x')
        self._term.pack(side='left', fill='both', expand=True)

    def _build_progress_panel(self, parent):
        """独立计算进度面板（与终端打印区分离）。

        数据来自 DashboardState.progress —— param_optimizer._update_progress
        在各 Phase / GA 代 / 候选验证处写入结构化进度（阶段、current/total、
        百分比、明细），本面板渲染：阶段徽标 + 进度条 + 明细文本。
        """
        box = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                       highlightthickness=1)
        box.pack(fill='x', padx=self._px(8), pady=(self._px(8), self._px(6)))
        head = tk.Frame(box, bg=PANEL)
        head.pack(fill='x', padx=self._px(10), pady=(self._px(6), 0))
        tk.Label(head, text='计算进度', bg=PANEL, fg=DIM,
                 font=('Microsoft YaHei', 9)).pack(side='left')
        self._lbl_prog_phase = tk.Label(head, text='等待任务...', bg=PANEL,
                                        fg=DIM, font=('Microsoft YaHei', 9))
        self._lbl_prog_phase.pack(side='right')
        self._prog_bar = ttk.Progressbar(box, orient='horizontal',
                                         mode='determinate',
                                         style='Modern.Horizontal.TProgressbar')
        self._prog_bar.pack(fill='x', padx=self._px(10),
                            pady=(self._px(6), self._px(2)))
        self._lbl_prog_detail = tk.Label(box, text='', bg=PANEL, fg=DIM,
                                         font=('Microsoft YaHei', 8), anchor='w')
        self._lbl_prog_detail.pack(fill='x', padx=self._px(10),
                                   pady=(0, self._px(6)))

    def _clear_term(self):
        """清空终端打印区（缓冲 + 界面显示）"""
        try:
            self.state.clear_log()
        except Exception:
            pass
        self._log_seq = 0
        self._term.config(state='normal')
        self._term.delete('1.0', 'end')
        self._term.config(state='disabled')

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
                                        state='readonly', width=14,
                                        style='Modern.TCombobox')
        self._combo_mode.pack(side='left', padx=(self._px(8), self._px(18)))
        self._combo_mode.bind('<<ComboboxSelected>>', self._on_mode_change)
        tk.Label(row1, text='试验次数', bg=PANEL, fg=TEXT,
                 font=('Microsoft YaHei', 10)).pack(side='left')
        self._spin_trials = ttk.Spinbox(row1, from_=10, to=100000, increment=100,
                                        width=9, style='Modern.TSpinbox',
                                        font=('Consolas', 10))
        self._spin_trials.pack(side='left', padx=(self._px(8), 0))
        # 断点续算：勾选=从断点继续；取消=放弃断点，从头全新计算（仅全量运算）
        self._resume_var = tk.BooleanVar(value=True)
        self._chk_resume = ttk.Checkbutton(row1, text='断点续算', variable=self._resume_var,
                                           style='Modern.TCheckbutton')
        self._chk_resume.pack(side='left', padx=(self._px(14), 0))
        # 完成后自动关机：勾选=本次优化成功完成后自动关机（GUI 交互，
        # 替代原先在 cmd 终端弹 input 询问）
        self._shutdown_var = tk.BooleanVar(value=False)
        self._chk_shutdown = ttk.Checkbutton(row1, text='完成后自动关机',
                                             variable=self._shutdown_var,
                                             style='Modern.TCheckbutton')
        self._chk_shutdown.pack(side='left', padx=(self._px(14), 0))

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
        self._btn_stop = tk.Button(row3, text='停止', command=self._request_stop,
                                   bg=RED, fg='#ffffff', relief='flat',
                                   activebackground='#c0392b', activeforeground='#ffffff',
                                   cursor='hand2', font=('Microsoft YaHei', 10, 'bold'),
                                   padx=self._px(16), pady=self._px(4), state='disabled')
        self._btn_stop.pack(side='right', padx=(0, self._px(8)))
        self._btn_pause = tk.Button(row3, text='暂停', command=self._toggle_pause,
                                    bg='#f39c12', fg='#ffffff', relief='flat',
                                    activebackground='#d68910', activeforeground='#ffffff',
                                    cursor='hand2', font=('Microsoft YaHei', 10, 'bold'),
                                    padx=self._px(16), pady=self._px(4), state='disabled')
        self._btn_pause.pack(side='right', padx=(0, self._px(8)))
        # 紧急执行：立即杀死超出目标进程数的进程（放弃其正在运行的数据）
        self._btn_emergency_trim = tk.Button(
            row3, text='紧急执行', command=self._request_emergency_trim,
            bg='#e67e22', fg='#ffffff', relief='flat',
            activebackground='#ca6f1e', activeforeground='#ffffff',
            cursor='hand2', font=('Microsoft YaHei', 10, 'bold'),
            padx=self._px(14), pady=self._px(4), state='disabled')
        self._btn_emergency_trim.pack(side='right', padx=(0, self._px(8)))
        # 紧急停止：立即保存断点 + 强行终止所有进程
        self._btn_emergency_stop = tk.Button(
            row3, text='紧急停止', command=self._request_emergency_stop,
            bg='#8e1c1c', fg='#ffffff', relief='flat',
            activebackground='#641e1e', activeforeground='#ffffff',
            cursor='hand2', font=('Microsoft YaHei', 10, 'bold'),
            padx=self._px(14), pady=self._px(4), state='disabled')
        self._btn_emergency_stop.pack(side='right', padx=(0, self._px(8)))

        # 初始选中第一个模式（全量运算）
        self._combo_mode.current(0)
        self._apply_mode(get_mode_by_label(self._combo_mode.get()))
        self._cfg_controls = [
            (self._combo_mode, 'readonly'),
            (self._spin_trials, 'normal'),
            (self._entry_path, 'normal'),
            (self._chk_resume, 'normal'),
            (self._chk_shutdown, 'normal'),
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
        # 断点续算仅对全量运算有意义（轻量每次全新运行），其余模式禁用勾选
        if getattr(mode, 'key', '') == 'light':
            self._chk_resume.configure(state='disabled')
        else:
            self._chk_resume.configure(state='normal')

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
        # 断点续算：取消勾选 = 放弃断点，从头全新计算（仅全量运算生效）
        resume = bool(self._resume_var.get())
        try:
            run_fn, kwargs = build_run(mode.key, trials, out_path,
                                       n_jobs=n_jobs, ga_n_jobs=ga_n_jobs,
                                       resume=resume)
        except Exception as e:
            messagebox.showerror('启动失败', f'无法构建计算任务：\n{e}')
            return

        self._set_cfg_enabled(False)
        self._btn_start.config(text='优化进行中...', state='disabled')
        po.clear_control_flags()
        # 自动关机交互（GUI 勾选，替代 cmd input 询问）：优化成功完成后关机
        self._shutdown_cb = (po._shutdown_sequence
                             if self._shutdown_var.get() else None)
        self._btn_pause.config(state='normal', text='暂停')
        self._btn_stop.config(state='normal')
        self._btn_emergency_stop.config(state='normal')
        self._btn_emergency_trim.config(state='normal')
        # 新一轮运算：清空终端打印区与日志缓冲（事件日志保留历史）
        try:
            self.state.clear_log()
        except Exception:
            pass
        self._log_seq = 0
        self._term.config(state='normal')
        self._term.delete('1.0', 'end')
        self._term.config(state='disabled')
        self.state.update(running=True, pool=mode.label, mode=mode.label)
        try:
            self.state.set_progress(phase='启动', label='准备开始计算',
                                    current=0, total=1, pct=0.0)
        except Exception:
            pass
        resume_note = '断点续算' if resume else '放弃断点，从头计算'
        self.state.add_event(f'开始{mode.label}：{trials} trials（{resume_note}），结果 → {out_path}')

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
            if os.path.exists(po.STOP_FILE) or os.path.exists(po.EMERGENCY_FILE):
                ok = False  # 停止/紧急停止不算成功完成，不触发关机回调
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
        self._btn_pause.config(state='disabled', text='暂停')
        self._btn_stop.config(state='disabled', text='停止')
        self._btn_emergency_stop.config(state='disabled')
        self._btn_emergency_trim.config(state='disabled')
        po.clear_control_flags()
        try:
            self.state.set_progress(phase='', label='任务完成' if ok else '任务已停止',
                                    current=1, total=1, pct=100.0)
        except Exception:
            pass
        self.state.add_event('配置已解锁，可修改参数后再次运行')

    def _toggle_pause(self):
        """暂停 / 继续：通过信号文件通知后台优化进程"""
        if not self.state.snapshot().get('running', False):
            return
        if self._btn_pause.cget('text') == '暂停':
            po.request_pause()
            self._btn_pause.config(text='继续')
            self._lbl_status.config(text='⏸ 已暂停', fg='#f39c12')
        else:
            po.request_resume()
            self._btn_pause.config(text='暂停')
            self._lbl_status.config(text='运行中', fg=GREEN)

    def _request_stop(self):
        """请求优雅停止：等待当前计算完成后退出"""
        po.request_stop()
        self._btn_pause.config(state='disabled')
        self._btn_stop.config(state='disabled')
        self._lbl_status.config(text='正在停止...', fg='#f39c12')

    def _request_emergency_stop(self):
        """紧急停止：立即保存当前断点并强行终止所有进程。

        断点保存可能耗时，放到后台线程执行，避免卡住 Tk 主线程。
        """
        if not self.state.snapshot().get('running', False):
            return
        threading.Thread(target=po.request_emergency_stop, daemon=True,
                         name='emergency-stop-thread').start()
        self._btn_pause.config(state='disabled')
        self._btn_stop.config(state='disabled')
        self._btn_emergency_stop.config(state='disabled')
        self._btn_emergency_trim.config(state='disabled')
        self._lbl_status.config(text='🛑 紧急停止中...', fg='#e74c3c')

    def _request_emergency_trim(self):
        """紧急执行：立即杀死超出目标进程数的进程，放弃其正在运行的数据。"""
        if not self.state.snapshot().get('running', False):
            return
        threading.Thread(target=po.request_emergency_trim, daemon=True,
                         name='emergency-trim-thread').start()
        self._lbl_status.config(text='⚡ 已执行紧急进程裁剪', fg='#e67e22')

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
        c = tk.Canvas(box, height=self._px(105), bg=PANEL, highlightthickness=0)
        c.pack(fill='both', expand=True, padx=self._px(6), pady=(0, self._px(8)))
        return c

    # ══════════════════════ 渲染 ══════════════════════

    def _draw_gauge(self, canvas, pct, color):
        """CPU 占用卡片：水平进度条 + 大号百分比数字（直观、无裁剪问题）"""
        canvas.delete('all')
        w = max(canvas.winfo_width(), self._px(150))
        h = max(canvas.winfo_height(), self._px(88))
        val = max(0.0, min(100.0, float(pct)))

        # 进度条轨道
        bar_h = self._px(12)
        bar_y = self._px(16)
        x0, x1 = self._px(14), w - self._px(14)
        canvas.create_rectangle(x0, bar_y, x1, bar_y + bar_h,
                                fill='#26304d', outline='')
        # 填充
        if val > 0.05:
            bw = max(self._px(2), (x1 - x0) * val / 100.0)
            canvas.create_rectangle(x0, bar_y, x0 + bw, bar_y + bar_h,
                                    fill=color, outline='')
        # 大号百分比数字
        canvas.create_text(w / 2, bar_y + bar_h + self._px(22),
                           text=f'{val:.1f}%', fill=TEXT,
                           font=('Consolas', 18, 'bold'))
        # 0 / 100 刻度
        canvas.create_text(x0, bar_y + bar_h + self._px(36), text='0',
                           fill=DIM, font=('Consolas', 8), anchor='w')
        canvas.create_text(x1, bar_y + bar_h + self._px(36), text='100',
                           fill=DIM, font=('Consolas', 8), anchor='e')

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
        self.root.after(500, self._tick)   # 0.5s 刷新（与采样频率同步提升一倍）

    def _render(self, snap):
        # 采样层已施加线性补偿（见 CpuMonitor.sample），此处直接显示，
        # GUI 显示值 = 调度器读取值（任务管理器口径）。
        prog = snap.get('program_cpu', 0)
        sys_ = snap.get('system_cpu', 0)
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

        # ── 独立计算进度面板（阶段 + 进度条 + 明细）──
        prog = snap.get('progress', {}) or {}
        p_phase = prog.get('phase', '') or ''
        p_label = prog.get('label', '') or ''
        p_cur = int(prog.get('current', 0) or 0)
        p_total = int(prog.get('total', 0) or 0)
        p_pct = float(prog.get('pct', 0.0) or 0.0)
        p_detail = prog.get('detail', '') or ''
        if p_phase or p_label:
            self._lbl_prog_phase.config(
                text=f'{p_phase} · {p_label}' if p_phase else p_label, fg=CYAN)
            if p_total and p_total > 0:
                detail_txt = f'{p_cur}/{p_total} · {p_pct:.0f}%'
            else:
                detail_txt = f'{p_pct:.0f}%'
            if p_detail:
                detail_txt += f'  {p_detail}'
            self._lbl_prog_detail.config(text=detail_txt)
            self._prog_bar.configure(value=p_pct, maximum=100.0)
        else:
            self._lbl_prog_phase.config(text='等待任务...', fg=DIM)
            self._lbl_prog_detail.config(text='')
            self._prog_bar.configure(value=0, maximum=100.0)

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

        # ── 终端打印区增量渲染（右侧，捕获的终端详细输出）──
        log_lines = snap.get('log_lines', [])
        log_seq = snap.get('log_seq', 0)
        if log_seq > self._log_seq:
            new_lines = log_lines[max(0, len(log_lines) - (log_seq - self._log_seq)):]
            if new_lines:
                self._term.config(state='normal')
                for line in new_lines:
                    self._term.insert('end', f'[{time.strftime("%H:%M:%S")}] {line}\n')
                # 行数限制：超过 5000 行裁剪头部，保留 3000 行
                line_count = int(self._term.index('end-1c').split('.')[0])
                if line_count > 5000:
                    self._term.delete('1.0', f'{line_count - 3000}.0')
                self._term.see('end')
                self._term.config(state='disabled')
            self._log_seq = log_seq

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
