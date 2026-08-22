# P5 开发计划：优化体系 + GUI（优化页 / 诊断卡 / 参数对比卡 / 验收脚本）

> 依据 `V7.0/V7.0_策略增强设计方案.md` §3.4 与 §五 P5 阶段。P6 算法重构已基本完成（P6.1~P6.8 工具齐备），本计划聚焦 P5 三个交付：P5.1 优化页 GUI、P5.2 优化前诊断卡 + 参数版本对比卡、P5.3 `tools/p5_gui_verify.py` 验收脚本。

---

## 一、Summary

在现有 V7.0 pywebview GUI（Vue3 + ECharts）上，把"参数优化"占位页实现为完整优化工作台：

1. **P5.1 优化页 GUI**：模式选择（复用 `optimizer_modes.MODES` 注册表，含 excess 系列）/ 试验次数 / CPU 上限 / 开始·暂停·继续·停止 / 实时日志 / 进度条 / Top20 结果表 / 一键应用参数。
2. **P5.2 优化前诊断卡 + 参数版本对比卡**：仓位分布直方图、策略-基准日收益相关性、超额来源分解（择时/仓位/残差）；V6 参数 vs V7 参数并排 diff。
3. **P5.3 `tools/p5_gui_verify.py`**：GUI 优化 API 与 CLI 一致性 + 小 trial 端到端跑通 + 应用参数后信号变化校验。

**关键约束**：后台 563360 heavy-excess 优化正在运行（约 1252/10000 trials），P5 开发与验收**不得触发 heavy 重算**，验证一律用小 trial（light-excess 30 trials）跑通管线。

---

## 二、Current State Analysis

### 2.1 已有基础设施（全部可复用）

| 组件 | 位置 | 现状 |
|------|------|------|
| 优化入口 | `quant/param_optimizer.py` | `run_heavy_optimization(resume, output_path, objective, ...)`、`run_light_optimization(n_trials, n_jobs, output_path, objective)`；`objective='benchmark_beating'` 走 EXCESS 搜索空间 |
| 模式注册表 | `quant/optimizer_modes.py` | `MODES` 含 heavy / light / heavy-excess / light-excess；`build_run(mode_key, trials, output_path)` 返回 `(run_fn, kwargs)` |
| 进度面板 | `quant/param_optimizer.py` + `adaptive_pool.py` | `DASHBOARD_STATE`（DashboardState）提供 `set_progress(phase/label/current/total/pct/detail)`、`add_event`、`add_log`、`snapshot()`；`enable_adaptive_control(cpu_limit)` 初始化；`install_stdout_tee()` 把 print/tqdm 逐行转发到日志 |
| 控制信号 | `quant/param_optimizer.py` | `request_pause()/request_resume()/request_stop()/request_emergency_stop()`（文件信号，`_check_control()` 轮询） |
| 结果格式 | `_export_light_results` / heavy 导出 | 统一 `{meta, phase_summary, top20}`（heavy 另有 `walk_forward`），落盘 `runs\{code}\{default_file}.json` |
| 参数写回 | `tools/gen_profiles.py` | `apply_optimized(code, results_json, kc_config, a5_config, v7_config)` 程序化合并写回 `profiles\{code}.json`（项目铁律：profile 必须经此导出） |
| 验收矩阵 | `tools/p6_8_acceptance.py` | 生成 `runs\{code}\acceptance_report.json`（含 `accepted` / `params` / `metrics` / `checks`） |
| 回测载荷 | `app/bridge.py` `_build_backtest_payload()` | 与 CLI --eval 同源计算路径，可复用做诊断数据源 |
| 前端骨架 | `app/web/index.html` + `js/app.js` + `css/style.css` | 优化页为占位；已有 ECharts 渲染范式（`btChartBase`、`renderBt*Chart`）可复用 |

### 2.2 关键事实与风险

- **后台 heavy 任务**：`job-687f34fc4e9f4e839b763a95084dc5d9`（563360 heavy-excess）运行中，Phase 1/4 Optuna 10000 trials，当前 ~1252/10000。P5 验收不得干扰它。
- **控制信号文件共享**：`PAUSE_FILE/STOP_FILE/EMERGENCY_FILE` 位于 `os.getcwd()`（V7.0 目录），GUI 与 CLI 优化任务**共享同一组信号文件**。GUI 点击停止会影响同目录下运行中的 CLI 优化。计划中明确此限制，GUI 仅对自身任务显示控制按钮，并在文档中说明。
- **多进程 spawn 安全**：`app/main.py` 与 `main.py` 均有 `if __name__ == '__main__'` 保护，GUI 进程内跑优化（spawn 子进程）不会重复启动 webview。已验证。
- **589800 验收未通过**：`runs\589800\acceptance_report.json` 显示 `accepted: false`（超额 -17.18pp）。"一键应用参数"需展示验收状态并警示，但允许用户手动确认后应用（GUI 只做工具，不替用户做决策）。
- **CPU 竞争**：GUI 优化与后台 heavy 是独立进程，各自 governor 互不可见。GUI 侧用 `cpu_limit` 滑块 + 自适应池控制自身进程数；验收用小 n_jobs（≤4）避免与 heavy 抢资源。

---

## 三、Proposed Changes

### 3.1 后端：`app/bridge.py` 新增 8 个 API

在 `ApiBridge` 类新增"5. 优化"区块，全部走 `_safe()` 包装 + JSON 可序列化。

| # | 方法 | 说明 |
|---|------|------|
| 1 | `list_optimize_modes()` | 读 `optimizer_modes.MODES`，返回 `[{key, label, desc, default_trials, default_file}]` |
| 2 | `start_optimization(code, mode_key, trials, cpu_limit)` | 异步任务：`_ensure_profile` → 校验模式 → `enable_adaptive_control(cpu_limit)`（幂等，仅当 `ADAPTIVE_ENABLED=False`）→ `clear_control_flags()` → `install_stdout_tee()` → `build_run(mode_key, trials, runs_path(f'{default_file}.json'))` → 后台线程执行 `run_fn(**kwargs)`。任务完成标记 `done`，result 含 `output_path` |
| 3 | `get_optimization_status(task_id)` | 返回 `{status, result, error, dashboard}`，`dashboard = DASHBOARD_STATE.snapshot()`（含 progress / events / log_lines / log_seq / workers / cpu） |
| 4 | `pause_optimization() / resume_optimization() / stop_optimization()` | 分别调 `request_pause/resume/stop` |
| 5 | `set_cpu_limit(cpu_limit)` | 运行中动态改 `GOVERNOR.limit_pct`（若自适应已启用） |
| 6 | `get_optimization_results(code)` | 读 `runs\{code}\` 下 4 个结果文件（heavy_excess/light_excess/heavy/light `*_results.json`），返回 `{filename: {meta, phase_summary, top20, walk_forward}}` |
| 7 | `apply_optimized_params(code, results_json)` | 调 `gen_profiles.apply_optimized`（先加载 kc/a5/v7 三个 config 模块），成功后 `config.activate_profile(code)` 重载 + 失效 `_strategy_cache/_data_cache`；返回 `{applied, code, acceptance}`（acceptance 读 `acceptance_report.json` 状态） |
| 8 | `get_diagnostics(code)` | 优化前诊断（见 3.2） |
| 9 | `get_param_versions(code)` | V6 vs V7 参数对比（见 3.3） |

**实现要点**：
- `start_optimization` 的 `n_jobs` 由 `cpu_limit` 推导：`n_jobs = max(1, int(os.cpu_count() * cpu_limit / 100))`，`ga_n_jobs = max(1, n_jobs - 4)`（heavy 用）。自适应池启用时实际进程数由 governor 钳制。
- 优化任务用独立 daemon 线程（同 `run_backtest` 模式），不占公共线程池。
- `enable_adaptive_control` 需在 `param_optimizer` 模块级调用；桥接层 import `param_optimizer as po` 后调用 `po.enable_adaptive_control(...)`。
- `apply_optimized_params` 需要 kc/a5/v7 config：在 `gen_profiles.py` 新增薄封装 `apply_optimized_auto(code, results_json)`（内部 `load_module` 三个 config 再调 `apply_optimized`），避免 bridge 重复加载逻辑。

### 3.2 后端：`get_diagnostics(code)` 诊断数据

复用 `_build_backtest_payload` 同源路径（`strategy.run(df) -> bt.run(signals)`），返回：

```json
{
  "available": true,
  "meta": {"code", "data_start", "data_end", "trading_days"},
  "position_dist": [{"bin": "0.0-0.1", "count": n, "pct": p}, ...],   // 10 等分桶，来源 bt.daily_signals current_position
  "position_stats": {"min", "max", "mean", "pct_ge_90", "pct_lt_30", "pct_lt_70"},
  "correlation": {"pearson_r": 0.xx, "n": N, "strategy_vol_pct": x, "benchmark_vol_pct": y},
  "excess_decomp": {
    "excess_pp": x, "position_pp": x, "timing_pp": x, "residual_pp": x,
    "avg_position": x, "benchmark_total_pct": x
  },
  "performance": {"strategy_return_pct", "benchmark_return_pct", "max_drawdown_pct", "total_trades"}
}
```

**超额分解公式**（择时/仓位/残差，精确恒等式 + 残差吸收成本）：
- 基准日收益 `r_t = close_t/close_{t-1} - 1`；持仓 `pos_held_t = current_position` 前移一日（当日信号决定次日持仓）
- `excess = Σ (pos_held_t - 1) · r_t`
- `position_pp = (mean(pos_held) - 1) · Σ r_t`（平均仓位效应）
- `timing_pp = Σ (pos_held_t - mean(pos_held)) · r_t`（择时协方差效应）
- `residual_pp = excess - position - timing`（吸收成本与近似误差）
- 相关性：策略日收益（净值差分）与基准日收益的 Pearson r

### 3.3 后端：`get_param_versions(code)` 参数对比

- 加载旧版 `V6.2.3\{科创|A500}\config.py`（`gen_profiles.load_module`）与当前 `profiles\{code}.json`
- 遍历 `gen_profiles.STRATEGY_KEYS`（V6 旧参数 + V7 新参数全量），返回：
```json
{
  "rows": [{"key", "v6", "v7", "changed": bool, "is_new": bool}],
  "summary": {"total": n, "changed": n, "new": n}
}
```
- `is_new = key 不在旧 config`（V7 新增：CENTER_*/TARGET_VOL/STOP_LOSS_*/TAKE_PROFIT_*/TRAIL_EXIT_DRAWDOWN 等）

### 3.4 前端：`app/web/index.html` 优化页

替换占位块为完整工作台，自上而下 5 张卡：

1. **优化前诊断卡**：`#diagChartPos`（仓位分布直方图，ECharts bar）+ 相关性/超额分解指标（`#diagChartExcess` 堆叠条或三格指标）
2. **参数版本对比卡**：V6 vs V7 diff 表格（`v-for` 渲染 rows，changed 行高亮，is_new 标"新增"徽章）
3. **优化操作卡**：模式下拉（`list_optimize_modes`）/ 试验次数输入（默认取模式 default_trials）/ CPU 上限滑块（1-100，默认 100）/ 开始·暂停·继续·停止按钮
4. **进度卡**：阶段标签 + 进度条（`dashboard.progress`）+ 实时日志面板（`dashboard.log_lines` 按 `log_seq` 增量追加，自动滚动到底）
5. **结果卡**：Top20 表格（objective/收益/年化/夏普/回撤/交易）+ 验收状态徽章（读 `acceptance_report.json`）+ "应用此参数"按钮（应用 top1）

### 3.5 前端：`app/web/js/app.js`

新增状态与方法（复用现有 `api` 封装与 ECharts 范式）：
- `api` 封装新增：`list_optimize_modes / start_optimization / get_optimization_status / pause_optimization / resume_optimization / stop_optimization / set_cpu_limit / get_optimization_results / apply_optimized_params / get_diagnostics / get_param_versions`
- 状态：`optModes / optMode / optTrials / optCpuLimit / optRunning / optTaskId / optProgress / optLogLines / optLogSeq / optResults / optDiagnostics / optParamVersions / optAcceptance`
- 方法：`loadOptimizeModes / loadDiagnostics / loadParamVersions / doStartOptimization / doPause / doResume / doStop / pollOptimization / loadOptimizationResults / doApplyParams / renderDiagCharts`
- `pollOptimization`：每 1.2s 轮询 `get_optimization_status`，更新进度条 + 增量追加日志；`done` 后调 `loadOptimizationResults` + 刷新诊断/信号
- `watch(currentPage)` 增加 `optimize` 分支：首次进入加载模式列表 + 诊断 + 参数对比
- `refreshAll()`（切换标的）增加优化页状态清空

### 3.6 前端：`app/web/css/style.css`

新增样式：`.opt-form`（表单行）、`.opt-log`（日志面板，等宽字体、滚动）、`.opt-table`（Top20 表格，复用 `.bt-table` 风格）、`.opt-diff-row.changed`（参数对比高亮）、`.diag-chart`（诊断图容器，复用 `.bt-chart` 高度）。

### 3.7 验收脚本：`tools/p5_gui_verify.py`

沿用 `p4_backtest_verify.py` 模式（直接实例化 `ApiBridge`），5 项测试：

| # | 测试 | 断言 |
|---|------|------|
| 1 | `list_optimize_modes` | 返回 4 个模式，key/label/default_trials 与 `optimizer_modes.MODES` 逐项一致 |
| 2 | `get_diagnostics` | 589800 仓位统计（min/max/mean/pct_lt_30）与 CLI `p6_baseline.compute_position_stats` 一致；相关性 ∈ [-1,1]；超额分解恒等式 `position+timing+residual ≈ excess`（容差 1e-6） |
| 3 | `get_param_versions` | rows 与 `gen_profiles.dump` 差异集一致；`is_new` 标记的 V7 新参数正确 |
| 4 | 端到端小 trial | `start_optimization('589800', 'light-excess', trials=30, cpu_limit=50)` 跑通 → `done` → `get_optimization_results` 返回 top20 非空 → 结果 JSON 落盘 `runs\589800\light_excess_results.json` |
| 5 | `apply_optimized_params` | 备份 profile → 应用 → profile 中 top1 参数键值写入 → 恢复 profile（不污染真实 profile） |

**注意**：测试 4 用小 n_jobs（cpu_limit=50 → n_jobs≈4），避免与后台 heavy 抢资源；测试 5 必须备份/恢复 profile，不改变线上参数。

---

## 四、Assumptions & Decisions

1. **GUI 优化在 GUI 进程内后台线程跑**（非 Tk 面板）：复用 `DASHBOARD_STATE` 进度面板 + 控制信号文件，与 CLI 共用同一套已验证机制。`run_with_gui`（Tk OptimizerGUI）不用于 V7.0 pywebview GUI。
2. **控制信号文件共享**：GUI 与同目录 CLI 优化共享 `PAUSE/STOP/EMERGENCY` 信号文件。GUI 控制按钮只对 GUI 自身任务有意义；若 CLI heavy 正在运行，GUI 点停止也会停掉它。接受此限制并文档化（P5 验收时后台 heavy 在跑，验收脚本不调用 stop）。
3. **"一键应用参数"不强制验收门槛**：展示 `acceptance_report.json` 的 `accepted` 状态并警示未通过项，但允许用户确认后应用（GUI 是工具，决策权在用户）。写回仍走 `gen_profiles.apply_optimized`（项目铁律）。
4. **参数对比数据源**：V6 值取自 `V6.2.3\{科创|A500}\config.py`（与 gen_profiles 同源），V7 值取自当前 profile。
5. **诊断分解采用"仓位/择时/残差"恒等式**：`excess = position + timing + residual`，残差吸收交易成本与近似误差（见 3.2 公式）。
6. **P5 验收不触发 heavy 重算**：验证只用 light-excess 30 trials；后台 563360 heavy-excess 继续运行不受影响。
7. **独立"参数对比"导航页保持占位**：P5.2 的对比卡放在优化页内（设计文档 §3.4 归入"GUI 优化页"），不额外填充 params 导航页。

---

## 五、Verification

1. **语法/导入检查**：`python -c "import bridge"`、`python -c "import gen_profiles"` 无报错。
2. **后端单测**：`python tools/p5_gui_verify.py` 5 项全绿（含小 trial 端到端）。
3. **GUI 手动冒烟**：`cd V7.0 && python app\main.py`，进入"参数优化"页：
   - 诊断卡渲染仓位直方图/相关性/超额分解，数值与 CLI 一致
   - 参数对比卡显示 V6 vs V7 diff
   - 选择 light-excess + 30 trials + CPU 50 → 开始 → 进度条推进、日志滚动 → 完成后 Top20 表格出现
   - 暂停/继续/停止按钮可用
   - 应用参数按钮出现验收状态提示
4. **回归**：`python tools/p4_backtest_verify.py` 仍全绿（确认优化页改动未破坏回测页）。
5. **提交**：完成后按项目规范 commit（自然口语化中文，主题简短），推送 origin/main（如 Clash 未运行则 `git -c http.proxy= -c https.proxy= push`）。

---

## 六、实施顺序

1. `tools/gen_profiles.py`：新增 `apply_optimized_auto(code, results_json)` 薄封装
2. `app/bridge.py`：新增 5.优化区块 9 个 API + `_build_diagnostics` / `_build_param_versions` 辅助
3. `app/web/index.html`：优化页占位替换为 5 卡工作台
4. `app/web/js/app.js`：优化页状态/方法/轮询/图表
5. `app/web/css/style.css`：优化页样式
6. `tools/p5_gui_verify.py`：5 项验收脚本
7. 运行验收 + GUI 冒烟 + 回归 + 提交推送
