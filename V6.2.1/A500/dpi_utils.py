"""
DPI 适配工具（Windows / Tkinter）
=================================
让优化器控制面板在不同 DPI 缩放（100% / 125% / 150% / 200% ...）与
不同分辨率显示器下保持清晰渲染与合理布局：

- setup_dpi_awareness()：必须在创建 Tk 根窗口之前调用。依次尝试
  Per-Monitor V2 / Per-Monitor / System Aware，避免 Windows 对界面
  做位图拉伸导致文字发虚。
- window_scale(window)：返回窗口所在显示器的缩放系数（96 DPI = 1.0，
  144 DPI = 1.5 ...），供像素尺寸（几何尺寸 / 画布大小 / 内边距）等比放大。
- apply_tk_scaling(root)：将 Tk 的"每点像素数"设为与显示器 DPI 一致，
  使以 point 为单位的内容（字体、行高）随 DPI 自动缩放。

用法（见 optimizer_gui.py）：
    from dpi_utils import setup_dpi_awareness, window_scale, apply_tk_scaling
    setup_dpi_awareness()                      # 1. 创建根窗口前
    root = tk.Tk()
    S = window_scale(root)                     # 2. 缩放系数
    apply_tk_scaling(root)                     # 3. 字体基线
    root.geometry(f'{round(940*S)}x{round(780*S)}')   # 4. 像素尺寸放大
"""

import ctypes

try:
    _user32 = ctypes.windll.user32
    _shcore = ctypes.windll.shcore
except (AttributeError, OSError):      # 非 Windows 环境
    _user32 = _shcore = None

_BASE_DPI = 96.0

# shcore.SetProcessDpiAwareness 的感知等级
_DPI_AWARENESS_UNAWARE = 0
_DPI_AWARENESS_SYSTEM = 1
_DPI_AWARENESS_PER_MONITOR = 2


def setup_dpi_awareness():
    """启用进程级 DPI 感知；必须在创建 Tk 根窗口之前调用（幂等）。"""
    if _shcore is None:
        return
    # Per-Monitor 优先级最高（在启动显示器上最清晰）；Tk 8.6 不支持运行时
    # 换屏重排，故若设置失败再回退 System Aware（跨屏由 Windows 虚拟化）。
    for level in (_DPI_AWARENESS_PER_MONITOR, _DPI_AWARENESS_SYSTEM):
        try:
            if _shcore.SetProcessDpiAwareness(level) == 0:
                return
        except Exception:
            continue
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


def get_dpi(window=None):
    """获取目标 DPI。window 为 Tk 根窗口时按该窗口所在显示器计算。"""
    if window is not None and _user32 is not None:
        try:
            hwnd = window.winfo_id()
            if hwnd:
                dpi = _user32.GetDpiForWindow(hwnd)      # Windows 10 1607+
                if dpi:
                    return float(dpi)
        except Exception:
            pass
    if _user32 is not None:
        try:
            return float(_user32.GetDpiForSystem())
        except Exception:
            pass
        try:
            hdc = _user32.GetDC(0)
            try:
                # LOGPIXELSX = 88
                return float(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))
            finally:
                _user32.ReleaseDC(0, hdc)
        except Exception:
            pass
    return _BASE_DPI


def window_scale(window=None):
    """显示器缩放系数：96 DPI = 1.0（100%），192 DPI = 2.0（200%）。"""
    return get_dpi(window) / _BASE_DPI


def apply_tk_scaling(root):
    """设置 Tk 缩放基线（每点像素数 = DPI / 72），字体随 DPI 自动放大。"""
    scale = get_dpi(root) / 72.0
    root.tk.call('tk', 'scaling', scale)
    return scale
