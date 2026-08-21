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

    // 切换页面时刷新对应数据
    watch(currentPage, (page) => {
      if (page === 'data') {
        refreshDataOverview();
      } else if (page === 'signal') {
        if (!signal.available || signalLoading.value === false) {
          refreshSignal();
        }
      }
    });

    // 全部刷新
    function refreshAll() {
      refreshDataOverview();
      refreshSignal();
      loadRuntimeContext();
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

      // 方法
      onProfileChange,
    };
  },
}).mount('#app');
