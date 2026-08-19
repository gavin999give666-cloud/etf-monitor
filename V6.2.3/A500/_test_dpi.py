# -*- coding: utf-8 -*-
"""DPI adaptation smoke test: real Tk window + fake governor/state."""
import multiprocessing


def main():
    from adaptive_pool import DashboardState
    from optimizer_gui import OptimizerGUI
    import dpi_utils

    class FakeGovernor:
        limit_pct = 100.0

        def initial_workers(self):
            return 16

        def set_limit(self, v):
            self.limit_pct = float(v)

        def mode_label(self):
            return 'max' if self.limit_pct >= 99.5 else 'limited'

    # 1) dpi_utils unit checks
    dpi = dpi_utils.get_dpi()
    assert dpi > 0, 'get_dpi() invalid: %s' % dpi
    s = dpi_utils.window_scale()
    assert s > 0, 'window_scale invalid: %s' % s
    print('[dpi_utils] dpi=%.0f scale=%.3f' % (dpi, s))

    # 2) real-window smoke
    app = OptimizerGUI(FakeGovernor(), DashboardState(), shutdown_cb=None)
    assert app._S > 0, 'S invalid: %s' % app._S
    assert app._px(940) == round(940 * app._S)
    assert app._px(1) == 1          # 100% scaling stays identity
    print('[gui] pre-map S=%.3f px940=%d px150=%d px360=%d'
          % (app._S, app._px(940), app._px(150), app._px(360)))

    # 3) render one frame + limit linkage without error
    app._render(app.state.snapshot())
    app._set_limit(20)
    assert app.governor.limit_pct == 20.0
    print('[gui] render+limit OK')

    # 4) after first map, scaling must match the real monitor DPI
    res = {}

    def check_and_close():
        tksc = float(app.root.tk.call('tk', 'scaling'))
        expect = dpi_utils.get_dpi(app.root) / 72.0
        res['tksc'], res['expect'], res['S'] = tksc, expect, app._S
        print('[gui] post-map S=%.3f tk_scaling=%.4f expect=%.4f geometry=%s'
              % (app._S, tksc, expect, app.root.geometry()))
        # GetDpiForWindow 有量化误差，容差需 ≥1e-3（不能按理论值用 1e-6）
        assert abs(tksc - expect) < 1e-3, 'tk scaling mismatch: %s vs %s' % (tksc, expect)
        assert app._mapped_once is True
        app.root.destroy()

    app.root.after(250, check_and_close)
    app.run()
    print('[gui] smoke passed, window closed cleanly')


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
