"""
独立 EXE 入口：双击即打开资源控制面板（可在面板中选择运算模式）
=============================================================================
面板功能：
- 运算模式选择：全量运算（四阶段级联） / 轻量运算（单阶段快速搜索）
- 试验次数、结果保存路径自定义
- CPU 资源限制实时调节（20%/50%/80%/100% 最大性能）

打包命令（见 build_exe.ps1）：
    pyinstaller --onefile --console --name ParamOptimizerUI \
        --hidden-import optimizer_gui --hidden-import adaptive_pool \
        --hidden-import optimizer_modes \
        optimizer_ui_main.py

注意事项：
- 将生成的 ParamOptimizerUI.exe 放在 stock_data.db 所在目录（如 A500 目录）
  或同一目录下，否则无法加载行情数据。
- 双击 = 默认参数（heavy + GUI + 100% 最大性能）；
  也可命令行传参覆盖，例如：
    ParamOptimizerUI.exe --cpu-limit 20            # 限制 20%
    ParamOptimizerUI.exe --method optuna --trials 500
"""
import multiprocessing
import sys

_DEFAULT_ARGS = ['--method', 'heavy', '--gui', '--cpu-limit', '100']


def main():
    from param_optimizer import main as po_main
    args = sys.argv[1:] or _DEFAULT_ARGS
    po_main(args)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
