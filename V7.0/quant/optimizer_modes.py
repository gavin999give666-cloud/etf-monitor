"""
运算模式注册表 —— UI 与计算逻辑解耦的核心
=================================================
- 新增 / 修改运算模式时，只需在本文件注册表（MODES）增删条目，
  无需改动 optimizer_gui.py 界面代码。
- GUI 读取 MODES 渲染"模式选择"下拉框；点击"开始优化"时调用
  build_run() 获取 (run_fn, kwargs)，在后台线程执行。
- run_fn 均为 param_optimizer.py 中定义的顶层计算入口
  （heavy = 全量四阶段级联管道；light = 单阶段 Optuna 快速搜索），
  计算细节全部封装在 param_optimizer.py 内部。
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass(frozen=True)
class OptimizeMode:
    key: str                  # 模式标识：'heavy' / 'light'
    label: str                # 下拉框显示名
    desc: str                 # 界面说明文字
    default_trials: int       # 默认试验次数
    default_file: str         # 默认导出文件名（不含扩展名）
    params: Dict = field(default_factory=dict)  # 模式附加默认参数


MODES = (
    OptimizeMode(
        key='heavy',
        label='全量运算',
        desc='四阶段级联搜索：Optuna(大样本) → 遗传算法 → 局部网格 → '
             'Walk-Forward 验证。耗时数小时，产出最稳健参数，支持断点续算。',
        default_trials=10000,
        default_file='heavy_results',
        params={'ga_generations': 100, 'ga_population': 60, 'wf_top_k': 20},
    ),
    OptimizeMode(
        key='light',
        label='轻量运算',
        desc='单阶段 Optuna 快速搜索，分钟级完成，适合快速迭代调试参数。',
        default_trials=300,
        default_file='light_results',
        params={'wf_top_k': 10},
    ),
    # ── V7.0 P6.6: excess 系列（新目标函数 benchmark_beating + 搜索空间 v2）──
    OptimizeMode(
        key='heavy-excess',
        label='全量运算(excess)',
        desc='V7 三层架构专用：EXCESS_SEARCH_SPACE(31维) + benchmark_beating 目标'
             '（超额收益为主 + 回撤≤基准一半）。四阶段级联管道，耗时长，支持断点续算。',
        default_trials=10000,
        default_file='heavy_excess_results',
        params={'ga_generations': 100, 'ga_population': 60, 'wf_top_k': 20},
    ),
    OptimizeMode(
        key='light-excess',
        label='轻量运算(excess)',
        desc='V7 三层架构专用：单阶段 Optuna 快速搜索（EXCESS_SEARCH_SPACE + '
             'benchmark_beating 目标），分钟级完成，适合 V7 参数快速迭代。',
        default_trials=300,
        default_file='light_excess_results',
        params={'wf_top_k': 10},
    ),
)


def get_mode(mode_key: str):
    """按 key 查找模式定义，找不到返回 None"""
    for m in MODES:
        if m.key == mode_key:
            return m
    return None


def get_mode_by_label(label: str):
    """按显示名查找模式定义（GUI 下拉框回查用）"""
    for m in MODES:
        if m.label == label:
            return m
    return None


def build_run(mode_key: str, trials: int, output_path: str,
              n_jobs: int = 14, ga_n_jobs: int = 10,
              resume: bool = True) -> Tuple:
    """构建 (run_fn, kwargs)。

    计算逻辑全部来自 param_optimizer.py，界面不感知具体实现：
    - heavy: 全量四阶段管道（Optuna→GA→FineGrid→Walk-Forward）
    - light: 单阶段 Optuna 快速搜索

    Args:
        mode_key:   'heavy' / 'light'
        trials:     试验次数（覆盖模式默认值）
        output_path: 结果 JSON 保存路径（GUI 中用户自定义）
        n_jobs:      并行进程参考数（自适应模式仅影响任务分块）
        ga_n_jobs:   遗传算法专用并行数（仅 heavy）
        resume:      是否断点续算（仅 heavy）
    """
    import param_optimizer as po

    if mode_key == 'heavy':
        return po.run_heavy_optimization, dict(
            resume=resume,
            n_trials=int(trials),
            n_jobs=n_jobs,
            ga_n_jobs=ga_n_jobs,
            wf_top_k=20,
            output_path=output_path,
        )

    if mode_key == 'heavy-excess':
        return po.run_heavy_optimization, dict(
            resume=resume,
            n_trials=int(trials),
            n_jobs=n_jobs,
            ga_n_jobs=ga_n_jobs,
            wf_top_k=20,
            output_path=output_path,
            objective='benchmark_beating',
        )

    if mode_key == 'light-excess':
        # light：单阶段 Optuna 快速搜索（excess）
        return po.run_light_optimization, dict(
            n_trials=int(trials),
            n_jobs=n_jobs,
            output_path=output_path,
            verbose=True,
            objective='benchmark_beating',
        )

    # light：单阶段 Optuna 快速搜索
    return po.run_light_optimization, dict(
        n_trials=int(trials),
        n_jobs=n_jobs,
        output_path=output_path,
        verbose=True,
    )
