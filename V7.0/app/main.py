"""
V7.0 GUI 入口 —— pywebview + WebView2
=======================================

启动方式：
  python app\main.py

技术栈：
- pywebview（Windows 走 Edge WebView2）
- Vue3 + ECharts（全部本地化，离线可用，无构建链）
- bridge.py 提供 js_api

设计要点（见 V7.0_合并设计方案.md §四）：
- 快操作（信号/数据查询）线程池直接执行
- 长任务（数据更新）后台线程执行，前端轮询任务状态
- 主进程同一时刻只激活一个标的，切换标的重建缓存
"""
import os
import sys

# 确保 app 目录在 path 中
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# Windows 控制台编码保护
try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass


def main():
    import webview
    from bridge import ApiBridge

    api = ApiBridge()
    html_path = os.path.join(_APP_DIR, 'web', 'index.html')

    # 窗口配置
    window = webview.create_window(
        'V7.0 多标的量化平台',
        url=html_path,
        js_api=api,
        width=1280,
        height=800,
        min_size=(960, 640),
        resizable=True,
        background_color='#0f172a',  # 深色主题背景
        text_select=True,
    )

    # 启动 WebView2
    # Windows 上默认使用 Edge Chromium (WebView2)
    webview.start(
        debug=False,     # 生产环境设为 False，调试时设为 True 可右键审查
        func=None,
        gui='edgechromium',  # 强制 WebView2
    )


if __name__ == '__main__':
    main()
