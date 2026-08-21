/* ============================================================
   V7.0 多标的量化平台 —— Vue 3 应用逻辑
   ============================================================ */

const { createApp, reactive, computed, onMounted, ref, watch, nextTick } = Vue;

// ------------------------------------------------------------
// pywebview API 封装 —— 带降级（浏览器直接打开时用 mock 数据）
// ------------------------------------------------------------
const api = {
  _hasPywebview() {
    return window.pywebview && window.pywebview.api;
  },

  async _call(method, ...args) {
    if (!this._hasPywebview()) {
      console.warn('[pywebview not available]', method, args);
      return { ok: false, error: 'pywebview 未连接，请通过 app/main.py 启动' };
    }
    try {
      const result = await window.pywebview.api[method](...args);
      return result;
    } catch (e) {
      console.error(`API 调用失败: ${method}`, e);
      return { ok: false, error: e.message || String(e) };
    }
  },

  // 标的管理
  list_profiles: () => api._call('list_profiles'),
  get_current_profile: () => api._call('get_current_profile'),
  switch_profile: (code) => api._call('switch_profile', code),
  add_profile: (code, name, market) => api._call('add_profile', code, name, market),

  // 数据
  get_data_overview: (code) => api._call('get_data_overview', code),
  get_runtime_context: () => api._call('get_runtime_context'),
  update_data: (code) => api._call('update_data', code),
  update_intraday: (code) => api._call('update_intraday', code),
  backfill_data: (code) => api._call('backfill_data', code),
  get_task_status: (taskId) => api._call('get_task_status', taskId),

  // 信号
  get_today_signal: (code) => api._call('get_today_signal', code),
  get_recent_prices: (code, days) => api._call('get_recent_prices', code, days),

  // 回测
  run_backtest: (code) => api._call('run_backtest', code),
};

// ------------------------------------------------------------
// Vue 应用
// ------------------------------------------------------------
createApp({
  setup() {
    // 状态
    const currentPage = ref('signal');
    const profiles = ref([]);
    const currentCode = ref('');
    const currentProfile = reactive({
      code: '',
      name: '',
      optimized: false,
      market: 'sh',
    });
    const runtimeCtx = reactive({
      now: '',
      is_trading_day: false,
      phase: '',
      description: '',
    });

    // 数据页状态
    const dataOverview = reactive({
      db_exists: false,
      start_date: null,
      end_date: null,
      row_count: 0,
      has_estimated: false,
      estimated_count: 0,
    });
    const taskRunning = ref(false);
    const currentTaskId = ref('');
    const currentTaskOutput = ref('');
    const taskError = ref('');

    // 信号页状态
    const signalLoading = ref(false);
    const signal = reactive({
      available: false,
      reason: '',
      buy_score: 0,
      sell_score: 0,
      net_score: 0,
      reward_score: 0,
      risk_score: 0,
      regime: '',
      psychology: '',
      emotion_score: 0,
      emotion_improving: false,
      target_position: 0,
      buy_behaviors: [],
      sell_behaviors: [],
      confirmed_buy_events: 0,
      confirmed_sell_events: 0,
      active_events: 0,
      evidence_buy: [],
      evidence_sell: [],
      last_date: '',
      data_rows: 0,
    });

    // 回测页状态
    const btRunning = ref(false);
    const btAvailable = ref(false);
    const btReason = ref('');
    const btError = ref('');
    const btMeta = reactive({});
    const btPerformance = reactive({});
    const btEquity = ref([]);
    const btPrice = ref([]);
    const btBuyMarkers = ref([]);
    const btSellMarkers = ref([]);
    const btTrades = ref([]);
    let btCharts = {}; // 已初始化的 ECharts 实例缓存

    // 绩效指标卡片配置
    const btMetricDefs = [
      { key: 'strategy_return_pct', label: '策略收益率', fmt: v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%', size: 22, color: v => v >= 0 ? 'buy' : 'sell', sub: p => `超额 ${fmtSigned(p.excess_return_pct)}%` },
      { key: 'benchmark_return_pct', label: '基准收益率', fmt: v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%', size: 18, color: 'info', sub: '买入持有' },
      { key: 'annualized_return_pct', label: '年化收益率', fmt: v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%', size: 18, color: v => v >= 0 ? 'buy' : 'sell' },
      { key: 'max_drawdown_pct', label: '最大回撤', fmt: v => v.toFixed(2) + '%', size: 18, color: 'sell' },
      { key: 'sharpe_ratio', label: '夏普比率', fmt: v => v.toFixed(2), size: 18, color: v => v >= 1 ? 'buy' : (v > 0 ? 'info' : 'sell') },
      { key: 'sortino_ratio', label: 'Sortino', fmt: v => v.toFixed(2), size: 18, color: v => v >= 1 ? 'buy' : 'info' },
      { key: 'calmar_ratio', label: 'Calmar', fmt: v => v.toFixed(2), size: 18, color: v => v >= 1 ? 'buy' : 'info' },
      { key: 'volatility_pct', label: '年化波动率', fmt: v => v.toFixed(2) + '%', size: 18, color: 'info' },
      { key: 'win_rate_pct', label: '胜率', fmt: v => v.toFixed(1) + '%', size: 18, color: v => v >= 50 ? 'buy' : 'sell', sub: p => `${p.winning_trades}胜 / ${p.losing_trades}负` },
      { key: 'profit_factor', label: '盈亏比', fmt: v => v === Infinity ? '∞' : v.toFixed(2), size: 18, color: v => v >= 1 ? 'buy' : 'sell' },
      { key: 'kelly', label: 'Kelly值', fmt: v => v.toFixed(2), size: 18, color: v => v > 0 ? 'buy' : 'sell' },
      { key: 'expectancy_pct', label: '期望收益/笔', fmt: v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%', size: 18, color: 'buy' },
      { key: 'total_trades', label: '总交易次数', fmt: v => v, size: 18, color: 'info', sub: p => `连续胜 ${p.max_consecutive_wins} · 连续亏 ${p.max_consecutive_losses}` },
      { key: 'avg_hold_days', label: '平均持仓', fmt: v => v.toFixed(1) + ' 天', size: 18, color: 'info' },
      { key: 'final_equity', label: '最终资产', fmt: v => v.toLocaleString(), size: 18, color: 'info', sub: p => `初始 ${Number(p.start_equity || 0).toLocaleString()}` },
    ];
    function fmtSigned(v) { return (v >= 0 ? '+' : '') + Number(v).toFixed(2); }
    const btMetrics = computed(() => {
      return btMetricDefs.map(d => {
        const raw = btPerformance[d.key];
        return {
          key: d.key,
          label: d.label,
          value: d.fmt(raw),
          color: typeof d.color === 'function' ? d.color(raw) : d.color,
          size: d.size,
          sub: d.sub ? d.sub(btPerformance) : '',
          dim: raw === undefined || raw === null,
        };
      });
    });

    // 导航
    const navItems = [
      { id: 'signal', label: '今日信号', icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>' },
      { id: 'data', label: '数据管理', icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>' },
      { id: 'backtest', label: '策略回测', icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>' },
      { id: 'optimize', label: '参数优化', icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>' },
      { id: 'params', label: '参数对比', icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>' },
    ];

    // ------------------------------------------------------------
    // 计算属性
    // ------------------------------------------------------------
    const buyEvidenceNormalized = computed(() => {
      const items = signal.evidence_buy || [];
      const total = items.reduce((s, i) => s + Math.abs(i.contribution), 0);
      if (total === 0) return [];
      return items.map(item => ({
        source: item.source,
        contribution: item.contribution,
        pct: Math.round((Math.abs(item.contribution) / total) * 100),
      }));
    });

    const sellEvidenceNormalized = computed(() => {
      const items = signal.evidence_sell || [];
      const total = items.reduce((s, i) => s + Math.abs(i.contribution), 0);
      if (total === 0) return [];
      return items.map(item => ({
        source: item.source,
        contribution: item.contribution,
        pct: Math.round((Math.abs(item.contribution) / total) * 100),
      }));
    });

    // ------------------------------------------------------------
    // 方法
    // ------------------------------------------------------------
    function getEvidenceValue(list, source) {
      const item = (list || []).find(i => i.source === source);
      return item ? (item.contribution > 0 ? '+' : '') + item.contribution.toFixed(1) : '0.0';
    }

    // 加载标的列表
    async function loadProfiles() {
      const res = await api.list_profiles();
      if (res.ok) {
        profiles.value = res.data;
      }
    }

    // 加载当前标的
    async function loadCurrentProfile() {
      const res = await api.get_current_profile();
      if (res.ok) {
        const d = res.data;
        currentProfile.code = d.code;
        currentProfile.name = d.name;
        currentProfile.optimized = d.optimized;
        currentProfile.market = d.market;
        currentCode.value = d.code;
      }
    }

    // 切换标的
    async function onProfileChange() {
      const code = currentCode.value;
      if (!code) return;
      const res = await api.switch_profile(code);
      if (res.ok) {
        const d = res.data;
        currentProfile.code = d.code;
        currentProfile.name = d.name;
        currentProfile.optimized = d.optimized;
        // 刷新各页面数据
        refreshAll();
      }
    }

    // 加载运行时信息
    async function loadRuntimeContext() {
      const res = await api.get_runtime_context();
      if (res.ok) {
        const d = res.data;
        runtimeCtx.now = d.now;
        runtimeCtx.is_trading_day = d.is_trading_day;
        runtimeCtx.phase = d.phase;
        runtimeCtx.description = d.description;
      }
    }

    // 数据页：刷新概览
    async function refreshDataOverview() {
      const res = await api.get_data_overview();
      if (res.ok) {
        const d = res.data;
        dataOverview.db_exists = d.db_exists;
        dataOverview.start_date = d.start_date;
        dataOverview.end_date = d.end_date;
        dataOverview.row_count = d.row_count;
        dataOverview.has_estimated = d.has_estimated;
        dataOverview.estimated_count = d.estimated_count || 0;
      }
    }

    // 任务轮询
    function pollTask(taskId) {
      currentTaskId.value = taskId;
      taskRunning.value = true;
      taskError.value = '';
      currentTaskOutput.value = '任务开始执行...\n';

      const poll = async () => {
        const res = await api.get_task_status(taskId);
        if (!res.ok) {
          taskError.value = res.error;
          taskRunning.value = false;
          return;
        }
        const task = res.data;
        if (task.status === 'done') {
          taskRunning.value = false;
          if (task.result && task.result.output) {
            currentTaskOutput.value = task.result.output;
          } else {
            currentTaskOutput.value += '\n完成。';
          }
          // 任务完成后刷新数据概览和信号
          refreshDataOverview();
          if (currentPage.value === 'signal') {
            refreshSignal();
          }
          return;
        }
        if (task.status === 'error') {
          taskRunning.value = false;
          taskError.value = task.error;
          return;
        }
        // 还在运行，继续轮询
        setTimeout(poll, 1500);
      };
      setTimeout(poll, 800);
    }

    // 数据更新
    async function doUpdate() {
      const res = await api.update_data();
      if (res.ok) {
        pollTask(res.data.task_id);
      } else {
        taskError.value = res.error;
      }
    }

    // 盘中估算
    async function doIntraday() {
      const res = await api.update_intraday();
      if (res.ok) {
        pollTask(res.data.task_id);
      } else {
        taskError.value = res.error;
      }
    }

    // T+1回填
    async function doBackfill() {
      const res = await api.backfill_data();
      if (res.ok) {
        pollTask(res.data.task_id);
      } else {
        taskError.value = res.error;
      }
    }

    // 信号页：刷新
    async function refreshSignal() {
      signalLoading.value = true;
      const res = await api.get_today_signal();
      signalLoading.value = false;
      if (res.ok) {
        const d = res.data;
        Object.assign(signal, d);
      } else {
        signal.available = false;
        signal.reason = res.error || '获取信号失败';
      }
    }

    // 回测页：应用结果载荷
    function applyBacktestPayload(d) {
      btAvailable.value = !!d.available;
      btReason.value = d.reason || '';
      if (!d.available) return;
      Object.keys(d.meta || {}).forEach(k => { btMeta[k] = d.meta[k]; });
      Object.keys(d.performance || {}).forEach(k => { btPerformance[k] = d.performance[k]; });
      btEquity.value = d.equity || [];
      btPrice.value = d.price || [];
      btBuyMarkers.value = d.buy_markers || [];
      btSellMarkers.value = d.sell_markers || [];
      btTrades.value = d.trades || [];
      nextTick(() => {
        renderBtEquityChart();
        renderBtPriceChart();
      });
    }

    // 回测任务轮询
    function pollBacktest(taskId) {
      btRunning.value = true;
      btError.value = '';
      const poll = async () => {
        const res = await api.get_task_status(taskId);
        if (!res.ok) {
          btError.value = res.error;
          btRunning.value = false;
          return;
        }
        const task = res.data;
        if (task.status === 'done') {
          btRunning.value = false;
          const btRes = (task.result && task.result.backtest) || {};
          applyBacktestPayload(btRes);
          return;
        }
        if (task.status === 'error') {
          btRunning.value = false;
          btError.value = task.error;
          return;
        }
        setTimeout(poll, 1200);
      };
      setTimeout(poll, 500);
    }

    // 运行回测
    async function doBacktest() {
      if (btRunning.value) return;
      const res = await api.run_backtest();
      if (res.ok) {
        pollBacktest(res.data.task_id);
      } else {
        btError.value = res.error;
      }
    }

    // 回测页：刷新（有结果时重绘图表）
    function refreshBacktest() {
      if (btRunning.value) return;
      btError.value = '';
      if (btAvailable.value) {
        nextTick(() => {
          renderBtEquityChart();
          renderBtPriceChart();
        });
      } else {
        doBacktest();
      }
    }

    // ECharts 通用主题
    const BT_CHART_TEXT = '#94a3b8';
    const BT_CHART_AXIS = '#334155';
    function btChartBase(grid, yAxis) {
      return {
        backgroundColor: 'transparent',
        textStyle: { color: BT_CHART_TEXT, fontFamily: 'Consolas, monospace' },
        grid,
        xAxis: { type: 'category', data: [], boundaryGap: true, axisLine: { lineStyle: { color: BT_CHART_AXIS } }, axisLabel: { color: BT_CHART_TEXT }, axisTick: { show: false } },
        yAxis,
        tooltip: { trigger: 'axis', backgroundColor: '#0f172a', borderColor: BT_CHART_AXIS, textStyle: { color: '#e2e8f0', fontSize: 12 } },
        dataZoom: [
          { type: 'inside', xAxisIndex: [0], start: 0, end: 100 },
          { type: 'slider', xAxisIndex: [0], height: 16, bottom: 6, borderColor: BT_CHART_AXIS, backgroundColor: '#1e293b', fillerColor: 'rgba(59,130,246,0.15)' },
        ],
      };
    }

    // 净值 + 回撤图
    function renderBtEquityChart() {
      const el = document.getElementById('btChartEquity');
      if (!el) return;
      if (!btCharts.equity) btCharts.equity = echarts.init(el, null, { renderer: 'canvas' });
      const chart = btCharts.equity;
      const dates = btEquity.value.map(p => p.date);
      const strategy = btEquity.value.map(p => p.strategy);
      const benchmark = btEquity.value.map(p => p.benchmark);
      const drawdown = btEquity.value.map(p => p.drawdown);
      const option = btChartBase(
        { top: 40, left: 70, right: 30, bottom: 70, height: '52%' },
        [
          { type: 'value', name: '净值', position: 'left', nameTextStyle: { color: BT_CHART_TEXT }, axisLabel: { color: BT_CHART_TEXT }, splitLine: { lineStyle: { color: '#1e293b' } } },
          null,
        ],
      );
      Object.assign(option, { grid: [option.grid, { top: '58%', left: 70, right: 30, bottom: 70, height: '30%' }] });
      option.yAxis = [
        { type: 'value', name: '净值', axisLabel: { color: BT_CHART_TEXT }, splitLine: { lineStyle: { color: '#1e293b' } }, nameTextStyle: { color: BT_CHART_TEXT } },
        { type: 'value', name: '回撤%', gridIndex: 1, axisLabel: { color: BT_CHART_TEXT }, splitLine: { show: false }, nameTextStyle: { color: BT_CHART_TEXT } },
      ];
      option.xAxis = [option.xAxis, { type: 'category', gridIndex: 1, data: [], axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false } }];
      option.xAxis[0].data = dates;
      option.xAxis[1].data = dates;
      option.dataZoom.forEach(dz => { dz.xAxisIndex = [0, 1]; });
      option.series = [
        {
          name: '策略净值', type: 'line', data: strategy, showSymbol: false,
          lineStyle: { color: '#10b981', width: 1.6 }, itemStyle: { color: '#10b981' }, smooth: true,
        },
        {
          name: '基准(买入持有)', type: 'line', data: benchmark, showSymbol: false,
          lineStyle: { color: '#3b82f6', width: 1.4, type: 'dashed' }, itemStyle: { color: '#3b82f6' }, smooth: true,
        },
        {
          name: '回撤', type: 'line', data: drawdown, xAxisIndex: 1, yAxisIndex: 1, showSymbol: false,
          lineStyle: { color: '#ef4444', width: 1.2 }, itemStyle: { color: '#ef4444' },
          areaStyle: { color: 'rgba(239,68,68,0.15)' }, smooth: true,
        },
      ];
      option.legend = {
        data: ['策略净值', '基准(买入持有)', '回撤'],
        top: 10, left: 'left', textStyle: { color: BT_CHART_TEXT },
      };
      chart.setOption(option, true);
    }

    // 价格 + 买卖标记图
    function renderBtPriceChart() {
      const el = document.getElementById('btChartPrice');
      if (!el) return;
      if (!btCharts.price) btCharts.price = echarts.init(el, null, { renderer: 'canvas' });
      const chart = btCharts.price;
      const dates = btPrice.value.map(p => p.date);
      const closes = btPrice.value.map(p => p.close);
      const buyPts = btBuyMarkers.value.map(m => [m.date, m.price]);
      const sellPts = btSellMarkers.value.map(m => [m.date, m.price]);
      const option = btChartBase(
        { top: 40, left: 70, right: 30, bottom: 60 },
        [{ type: 'value', axisLabel: { color: BT_CHART_TEXT }, splitLine: { lineStyle: { color: '#1e293b' } } }],
      );
      option.xAxis = [{ type: 'category', data: dates, axisLine: { lineStyle: { color: BT_CHART_AXIS } }, axisLabel: { color: BT_CHART_TEXT }, axisTick: { show: false } }];
      option.yAxis = [{ type: 'value', axisLabel: { color: BT_CHART_TEXT }, splitLine: { lineStyle: { color: '#1e293b' } } }];
      option.dataZoom.forEach(dz => { dz.xAxisIndex = [0]; });
      option.series = [
        {
          name: '收盘价', type: 'line', data: closes, showSymbol: false,
          lineStyle: { color: '#38bdf8', width: 1.4 }, itemStyle: { color: '#38bdf8' }, smooth: false,
        },
        {
          name: '买入成交', type: 'scatter', data: buyPts, symbol: 'triangle', symbolSize: 14,
          itemStyle: { color: 'transparent', borderColor: '#10b981', borderWidth: 1.6 },
        },
        {
          name: '卖出成交', type: 'scatter', data: sellPts, symbol: 'triangle', symbolRotate: 180, symbolSize: 14,
          itemStyle: { color: 'transparent', borderColor: '#ef4444', borderWidth: 1.6 },
        },
      ];
      option.legend = {
        data: ['收盘价', '买入成交', '卖出成交'],
        top: 10, left: 'left', textStyle: { color: BT_CHART_TEXT },
      };
      chart.setOption(option, true);
    }

    // 切换页面时刷新对应数据
    watch(currentPage, (page) => {
      if (page === 'data') {
        refreshDataOverview();
      } else if (page === 'signal') {
        if (!signal.available || signalLoading.value === false) {
          refreshSignal();
        }
      } else if (page === 'backtest') {
        if (!btAvailable.value && !btRunning.value) {
          doBacktest();
        } else if (btAvailable.value) {
          nextTick(() => {
            if (btCharts.equity) btCharts.equity.resize();
            if (btCharts.price) btCharts.price.resize();
          });
        }
      }
    });

    // 清空回测状态（切换标的时调用，避免展示旧标的回测）
    function clearBacktest() {
      btAvailable.value = false;
      btReason.value = '';
      btError.value = '';
      Object.keys(btMeta).forEach(k => delete btMeta[k]);
      Object.keys(btPerformance).forEach(k => delete btPerformance[k]);
      btTrades.value = [];
      btEquity.value = [];
      btPrice.value = [];
      btBuyMarkers.value = [];
      btSellMarkers.value = [];
      if (btCharts.equity) { btCharts.equity.dispose(); delete btCharts.equity; }
      if (btCharts.price) { btCharts.price.dispose(); delete btCharts.price; }
    }

    // 全部刷新
    function refreshAll() {
      refreshDataOverview();
      refreshSignal();
      loadRuntimeContext();
      clearBacktest();
    }

    // ------------------------------------------------------------
    // 初始化
    // ------------------------------------------------------------
    onMounted(async () => {
      await loadProfiles();
      await loadCurrentProfile();
      await loadRuntimeContext();

      // 默认加载信号页数据
      if (currentPage.value === 'signal') {
        refreshSignal();
      }
      if (currentPage.value === 'data') {
        refreshDataOverview();
      }

      // 定时刷新运行时信息（30秒一次）
      setInterval(loadRuntimeContext, 30000);
    });

    return {
      // 状态
      currentPage,
      profiles,
      currentCode,
      currentProfile,
      runtimeCtx,
      navItems,

      // 数据页
      dataOverview,
      taskRunning,
      currentTaskOutput,
      taskError,
      refreshDataOverview,
      doUpdate,
      doIntraday,
      doBackfill,

      // 信号页
      signalLoading,
      signal,
      buyEvidenceNormalized,
      sellEvidenceNormalized,
      getEvidenceValue,
      refreshSignal,

      // 回测页
      btRunning,
      btAvailable,
      btReason,
      btError,
      btMeta,
      btPerformance,
      btMetrics,
      btTrades,
      doBacktest,
      refreshBacktest,

      // 方法
      onProfileChange,
    };
  },
}).mount('#app');
