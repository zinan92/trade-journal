(() => {
  const BIAS_OPTIONS = [
    '踏空犹豫', '不敢下注', '怕回撤', '低估反转幅度', '没等确认',
    'FOMO追高', '盯盘干扰', '止损太紧', '拿不住利润', '报复性交易', '完美执行',
  ];

  const KIND_META = {
    buy:     { text: '🟢 买入',   color: '#15803d', pos: 'belowBar', shape: 'arrowUp' },
    sell:    { text: '🔴 卖出',   color: '#b91c1c', pos: 'aboveBar', shape: 'arrowDown' },
    missed:  { text: '🟡 Should', color: '#d97706', pos: 'belowBar', shape: 'circle' },
    mistake: { text: '🟣 破纪律', color: '#7e22ce', pos: 'aboveBar', shape: 'square' },
  };

  // 上涨绿色空心阳线 + 下跌红色实心阴线
  const HOLLOW_CANDLE_OPTS = {
    upColor: '#ffffff',
    downColor: '#dc2626',
    borderVisible: true,
    borderUpColor: '#16a34a',
    borderDownColor: '#dc2626',
    wickUpColor: '#16a34a',
    wickDownColor: '#dc2626',
  };

  // 四条核心均线：EMA 20 / 50 / 100 / 200
  const EMA_LINES = [
    { period: 20,  color: '#ef4444', width: 1.2 }, // 红
    { period: 50,  color: '#d97706', width: 1.2 }, // 橙
    { period: 100, color: '#0891b2', width: 1.2 }, // 青
    { period: 200, color: '#2563eb', width: 1.4 }, // 蓝
  ];

  // 全图支持滚轮/双指缩放 + 拖动横轴与纵轴 + 双击坐标轴重置
  const FULL_CHART_INTERACTION = {
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: true,
    },
    handleScale: {
      axisPressedMouseMove: { time: true, price: true },
      axisDoubleClickReset: { time: true, price: true },
      mouseWheel: true,
      pinch: true,
    },
  };

  const S = {
    today: '',
    entries: [],
    rules: [],
    cache: [],
    quickSymbols: [],
    intervals: ['5m', '15m', '30m', '1h', '4h', '1d', '1wk'],
    stats: {},
    activeEntryId: null,
    symbol: 'NVDA',
    interval: '15m',
    pinKind: 'missed',
    chartMode: 'snapshot', // snapshot | single | none
    snapDays: 14,
    showEma: true,
    hideEmptyStruct: false,
    chartData: null,
    snapData: null,
    radarData: null,
    coachMessages: [],
    bannerDismissed: false,
    todayPlan: null,
    planStreak: { streak: 0, totalSigned: 0, totalReviewed: 0 },
    morningInterval: '1wk',
    morningEma: true,
    morningSummary: null,
    morningChartsCache: {},
    morningLoaded: false,
    zoomSymbol: null,
    zoomInterval: '1wk',
    autoRefresh: localStorage.getItem('tj_auto_refresh') !== '0',
    autoTimer: null,
    autoTickCount: 0,
    saveTimer: null,
    priceLines: [],
  };

  let morningChartInstances = [];
  let morningObserver = null;
  let inlineThumbCharts = [];
  let zoomChart = null;
  let zoomCandleSeries = null;
  let zoomEmaSeriesList = [];
  let zoomCandles = [];
  let zoomPriceLines = [];

  const $ = (id) => document.getElementById(id);

  async function api(method, url, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  }

  function toast(msg) {
    const el = $('toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add('hidden'), 2800);
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmtTs(ts) {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function activeEntry() {
    return S.entries.find((e) => e.id === S.activeEntryId) || null;
  }

  // 标准 TradingView 口径 EMA：前 period 根均值起算，K 线不足 period 根时不画伪均线
  function calcEma(candles, period = 20) {
    if (!candles || candles.length < period) return [];
    const k = 2 / (period + 1);
    let sum = 0;
    for (let i = 0; i < period; i += 1) sum += candles[i].close;
    let ema = sum / period;
    const out = [{ time: candles[period - 1].time, value: Number(ema.toFixed(4)) }];
    for (let i = period; i < candles.length; i += 1) {
      ema = candles[i].close * k + ema * (1 - k);
      out.push({ time: candles[i].time, value: Number(ema.toFixed(4)) });
    }
    return out;
  }

  function autoGrowTextarea(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(46, el.scrollHeight)}px`;
  }

  function autoGrowAllProse() {
    document.querySelectorAll(".prose-in").forEach(autoGrowTextarea);
  }

  function setDictationCollapsed(collapsed) {
    S.dictationCollapsed = collapsed;
    $("rawBodyWrap")?.classList.toggle("hidden", collapsed);
    const ic = $("dictToggleIcon");
    const lb = $("dictToggleLabel");
    if (ic) ic.textContent = collapsed ? "▸" : "▾";
    if (lb) {
      lb.textContent = collapsed
        ? "🎙 原始口述已归类（点击展开原话或补充口述）"
        : "🎙 口述 / 自由输入（写完点右侧一键归类成文章）";
    }
  }

  function destroyInlineThumbCharts() {
    inlineThumbCharts.forEach((c) => { try { c.remove(); } catch {} });
    inlineThumbCharts = [];
  }

  // ---------- Slash Command Bar ----------
  function renderCmdMenu(query) {
    const q = (query || '').trim().replace(/^[/$\s]+/, '').toUpperCase();
    const menu = $('cmdMenu');
    const items = [
      ...S.quickSymbols.map((x) => ({ cmd: `/${x.symbol}`, desc: `${x.label} · 15m K 线` })),
      { cmd: `/${q || 'NVDA'} 1d`, desc: '日线大结构 K 线' },
      { cmd: `/${q || 'QQQ'} 5m`, desc: '5 分钟超短日内 K 线' },
      { cmd: `/${q || 'NDX'} 1h`, desc: '1 小时波段 K 线' },
      { cmd: '/tv', desc: '粘贴 TradingView 截图到当前复盘' },
    ].filter((it) => !q || it.cmd.toUpperCase().includes(q) || it.desc.toUpperCase().includes(q));

    if (q && !items.some((x) => x.cmd.toUpperCase() === `/${q}`)) {
      items.unshift({ cmd: `/${q}`, desc: `直接查询 ${q} 实时 K 线并写入本地库` });
    }

    menu.innerHTML = items.slice(0, 10).map((it, idx) => `
      <div class="cmd-item ${idx === 0 ? 'active' : ''}" data-cmd="${esc(it.cmd)}">
        <code>${esc(it.cmd)}</code>
        <span class="muted">${esc(it.desc)}</span>
      </div>
    `).join('');
    menu.classList.remove('hidden');

    menu.querySelectorAll('.cmd-item').forEach((el) => {
      el.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        runSlashCommand(el.dataset.cmd);
      });
    });
  }

  async function runSlashCommand(raw) {
    const text = (raw || '').trim();
    if (!text) return;
    $('cmdMenu').classList.add('hidden');
    $('cmdInput').value = '';
    try {
      const res = await api('POST', '/api/command', { command: text });
      if (res.action === 'tv') { toast(res.hint); return; }
      const ch = res.chart;
      S.symbol = ch.symbol;
      S.interval = ch.interval;
      openZoomModal(ch.symbol, ch.interval);
    } catch (e) {
      toast(e.message);
    }
  }

  // ---------- Gemini Auto-Organize ----------
  async function organizeCurrentEntry() {
    const cur = await ensureEntryForCurrentChart();
    const raw = ($('edRaw').value || '').trim();
    if (!raw) { toast('请先在口述框里写下或粘贴你的复盘'); return; }
    const btn = $('btnOrganize');
    const prevTxt = btn.textContent;
    btn.disabled = true;
    btn.textContent = '✦ Gemini 归类整理中…';
    try {
      const res = await api('POST', `/api/entries/${cur.id}/organize`, { raw_note: raw });
      replaceEntry(res.entry);
      S.stats = res.stats;
      S.symbol = res.entry.symbol;
      renderEntryList();
      renderEditor({ autoCollapseRaw: true });
      toast('Gemini 已归类整理成文章 ✓');
    } catch (e) {
      toast(`整理失败：${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = prevTxt;
    }
  }

  // ---------- Entries CRUD ----------
  function replaceEntry(entry) {
    const idx = S.entries.findIndex((x) => x.id === entry.id);
    if (idx >= 0) S.entries[idx] = entry;
    else S.entries.unshift(entry);
  }

  async function ensureEntryForCurrentChart() {
    let cur = activeEntry();
    if (cur) return cur;
    const res = await api('POST', '/api/entries', {
      trade_date: S.today, symbol: S.symbol, interval: S.interval,
      title: `${S.today} 交易复盘`,
    });
    S.entries.unshift(res.entry);
    S.activeEntryId = res.entry.id;
    S.stats = res.stats;
    renderEntryList();
    renderEditor();
    return res.entry;
  }

  async function createNewEntry() {
    switchView('journal');
    const res = await api('POST', '/api/entries', {
      trade_date: S.today, symbol: S.symbol, interval: S.interval,
      title: `${S.today} 交易复盘`,
    });
    S.entries.unshift(res.entry);
    S.activeEntryId = res.entry.id;
    S.stats = res.stats;
    renderEntryList();
    renderEditor();
    setDictationCollapsed(false);
    $('edRaw').focus();
    toast('已新建复盘，直接口述或分段写作即可');
  }

  function renderEntryList() {
    const q = ($('searchInput').value || '').trim().toLowerCase();
    const list = S.entries.filter((e) => !q || `${e.title} ${e.symbol} ${e.raw_note} ${e.context_note} ${e.execution_note} ${e.did_right_note || ''} ${e.should_have_note} ${e.takeaway_note}`.toLowerCase().includes(q));
    const box = $('entryList');
    if (!list.length) {
      box.innerHTML = `<div class="muted" style="padding:14px 8px">还没有复盘记录。点上方「+ 新建复盘」开始写。</div>`;
      return;
    }
    box.innerHTML = list.map((e) => {
      const syms = (e.symbols && e.symbols.length) ? e.symbols.slice(0, 3).join('·') : e.symbol;
      const pnlCls = (e.pnl || '').startsWith('-') ? 'pnl-neg' : ((e.pnl || '').startsWith('+') ? 'pnl-pos' : '');
      const tagPreview = (e.bias_tags || []).slice(0, 2).map((t) => `#${t}`).join(' ');
      return `
        <div class="entry-card ${e.id === S.activeEntryId ? 'on' : ''}" data-eid="${e.id}">
          <div class="ec-top">
            <span class="ec-sym">${esc(syms)}</span>
            <span class="ec-date">${esc(e.trade_date)}</span>
          </div>
          <div class="ec-title">${esc(e.title)}</div>
          <div class="ec-foot">
            <span>${esc(tagPreview || e.outcome)}</span>
            <span class="${pnlCls}">${esc(e.pnl || '')}</span>
          </div>
        </div>
      `;
    }).join('');

    box.querySelectorAll('[data-eid]').forEach((el) => {
      el.addEventListener('click', () => {
        S.activeEntryId = Number(el.dataset.eid);
        const cur = activeEntry();
        if (cur) { S.symbol = cur.symbol; S.interval = cur.interval; }
        renderEntryList();
        renderEditor();
      });
    });
  }

  async function renderInlineChartThumbs(entry) {
    destroyInlineThumbCharts();
    const grid = $('inlineChartsGrid');
    if (!grid) return;
    const syms = [...new Set((entry.symbols && entry.symbols.length) ? entry.symbols : (entry.symbol ? [entry.symbol] : []))].slice(0, 12);
    if (!syms.length) {
      grid.innerHTML = '';
      return;
    }
    grid.innerHTML = syms.map((sym) => `
      <div class="inline-chart-thumb" data-ithumb="${esc(sym)}" title="点击放大查看 ${esc(sym)} 交互 K 线">
        <div class="ict-head">
          <span class="ict-sym">${esc(sym)}</span>
          <span style="display:flex;align-items:center;gap:5px">
            <span class="muted" data-ipx="${esc(sym)}">--</span>
            <button class="ict-rm" data-irmsym="${esc(sym)}" title="从本文移除">×</button>
          </span>
        </div>
        <div class="ict-canvas" data-icanvas="${esc(sym)}"></div>
        <div class="ict-foot">
          <span data-ichg="${esc(sym)}">日线缩略</span>
          <span>点击放大 ⤢</span>
        </div>
      </div>
    `).join('');

    grid.querySelectorAll('[data-irmsym]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const cur = activeEntry();
        if (!cur) return;
        const target = btn.dataset.irmsym;
        const next = ((cur.symbols && cur.symbols.length) ? cur.symbols : [cur.symbol]).filter((s) => s !== target);
        cur.symbols = next;
        renderEditor();
        scheduleSave();
      });
    });
    grid.querySelectorAll('[data-ithumb]').forEach((card) => {
      card.addEventListener('click', () => openZoomModal(card.dataset.ithumb, '1d'));
    });

    // 异步渲染每张小缩略图（本地缓存命中毫秒级出图）
    const entryIdSnap = entry.id;
    await Promise.all(syms.map(async (sym) => {
      try {
        const data = await api('GET', `/api/candles?symbol=${encodeURIComponent(sym)}&interval=1d`);
        if (S.activeEntryId !== entryIdSnap) return;
        const pxEl = grid.querySelector(`[data-ipx="${CSS.escape(sym)}"]`);
        const chgEl = grid.querySelector(`[data-ichg="${CSS.escape(sym)}"]`);
        const canvasEl = grid.querySelector(`[data-icanvas="${CSS.escape(sym)}"]`);
        if (pxEl) pxEl.textContent = fmtPrice(data.lastPrice);
        if (chgEl) {
          const sign = data.changePct >= 0 ? '+' : '';
          chgEl.textContent = `${sign}${data.changePct}%`;
          chgEl.className = data.changePct >= 0 ? 'sc-up' : 'sc-dn';
        }
        if (canvasEl && data.candles?.length) {
          const bars = data.candles.slice(-60);
          const c = LightweightCharts.createChart(canvasEl, {
            width: canvasEl.clientWidth || 190,
            height: 96,
            layout: { background: { color: '#ffffff' }, textColor: '#9ca3af', fontSize: 9 },
            grid: { vertLines: { visible: false }, horzLines: { color: '#f5f4f0' } },
            rightPriceScale: { visible: false },
            timeScale: { visible: false },
            crosshair: { mode: LightweightCharts.CrosshairMode.Hidden },
            handleScroll: false,
            handleScale: false,
          });
          const cs = c.addCandlestickSeries({ ...HOLLOW_CANDLE_OPTS, priceLineVisible: false });
          cs.setData(bars);
          EMA_LINES.slice(0, 2).forEach((cfg) => {
            const pts = calcEma(data.candles, cfg.period).slice(-bars.length);
            if (!pts.length) return;
            const ln = c.addLineSeries({ color: cfg.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
            ln.setData(pts);
          });
          c.timeScale().fitContent();
          inlineThumbCharts.push(c);
        }
      } catch {
        // Ignore single thumbnail load error
      }
    }));
  }

  function renderHashtags(e) {
    const active = e.bias_tags || [];
    $('biasWrap').innerHTML = active.length
      ? active.map((t) => `<span class="hashtag-item" data-rmtag="${esc(t)}" title="点击移除">#${esc(t)}</span>`).join('')
      : `<span class="muted">暂无标签（点右侧 + #标签，或让 Gemini 归类自动生成）</span>`;

    $('biasWrap').querySelectorAll('[data-rmtag]').forEach((el) => {
      el.addEventListener('click', () => {
        const cur = activeEntry();
        if (!cur) return;
        cur.bias_tags = (cur.bias_tags || []).filter((x) => x !== el.dataset.rmtag);
        renderHashtags(cur);
        scheduleSave();
      });
    });

    const activeSet = new Set(active);
    const allPresets = [...new Set([...BIAS_OPTIONS, ...active])];
    $('tagPresets').innerHTML = allPresets.map((t) =>
      `<button type="button" class="tag-preset-btn ${activeSet.has(t) ? 'on' : ''}" data-ptag="${esc(t)}">#${esc(t)}</button>`
    ).join('');
    $('tagPresets').querySelectorAll('[data-ptag]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const cur = activeEntry();
        if (!cur) return;
        const set = new Set(cur.bias_tags || []);
        if (set.has(btn.dataset.ptag)) set.delete(btn.dataset.ptag);
        else set.add(btn.dataset.ptag);
        cur.bias_tags = [...set];
        renderHashtags(cur);
        scheduleSave();
      });
    });
  }

  function renderEditor({ autoCollapseRaw = false } = {}) {
    const e = activeEntry();
    if (!e) return;
    $('edTitle').value = e.title || '';
    $('edDate').value = e.trade_date || S.today;
    $('edOutcome').value = e.outcome || 'review';
    $('edPnl').value = e.pnl || '';
    $('edRaw').value = e.raw_note || '';
    $('edContext').value = e.context_note || '';
    $('edExec').value = e.execution_note || '';
    $('edDidRight').value = e.did_right_note || '';
    $('edShould').value = e.should_have_note || '';
    $('edTakeaway').value = e.takeaway_note || '';

    const hasStructured = Boolean(
      (e.context_note || '').trim() || (e.execution_note || '').trim() ||
      (e.did_right_note || '').trim() || (e.should_have_note || '').trim() || (e.takeaway_note || '').trim()
    );
    setDictationCollapsed(autoCollapseRaw || hasStructured);
    requestAnimationFrame(autoGrowAllProse);

    renderArticlePlanSnapshot(e.trade_date || S.today);
    renderHashtags(e);
    renderInlineChartThumbs(e);

    const atts = e.attachments || [];
    $('attGrid').innerHTML = atts.length ? atts.map((a) => `
      <div class="att-item">
        <img src="${esc(a.url)}" alt="${esc(a.caption)}" data-zoom="${esc(a.url)}">
        <button class="att-del" data-attdel="${a.id}" title="删除截图">×</button>
      </div>
    `).join('') : '';

    $('attGrid').querySelectorAll('[data-zoom]').forEach((img) => {
      img.addEventListener('click', () => {
        $('lightboxImg').src = img.dataset.zoom;
        $('lightbox').classList.remove('hidden');
      });
    });
    $('attGrid').querySelectorAll('[data-attdel]').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const res = await api('DELETE', `/api/attachments/${btn.dataset.attdel}`);
        replaceEntry(res.entry);
        renderEditor();
      });
    });
  }

  async function renderArticlePlanSnapshot(tradeDate) {
    const card = $('articlePlanCard');
    if (!card || !tradeDate) return;
    let plan = (S.todayPlan && S.todayPlan.plan_date === tradeDate) ? S.todayPlan : null;
    if (!plan) {
      try {
        const res = await api('GET', `/api/packages/${encodeURIComponent(tradeDate)}`);
        plan = res.plan;
      } catch {
        plan = null;
      }
    }
    if (!plan || !plan.has_plan) {
      card.classList.add('hidden');
      card.innerHTML = '';
      return;
    }
    const ms = plan.market_snapshot || {};
    const mktParts = [];
    if (ms.vixPrice !== undefined) mktParts.push(`VIX ${ms.vixPrice}`);
    if (ms.medianPct !== undefined) {
      const s = Number(ms.medianPct) >= 0 ? '+' : '';
      mktParts.push(`自选中位数 ${s}${Number(ms.medianPct).toFixed(2)}%`);
    }
    if (ms.extremeDaysInYear && ms.tradingDaysInYear) {
      mktParts.push(`1年排位 ${ms.extremeDaysInYear}/${ms.tradingDaysInYear}天`);
    }
    const rows = [];
    if (plan.step1_note) rows.push(`<div class="aps-row"><b>Step 1 大盘</b><span>${esc(plan.step1_note)}</span></div>`);
    if (plan.step2_note) rows.push(`<div class="aps-row"><b>Step 2 板块</b><span>${esc(plan.step2_note)}</span></div>`);
    const finalPlan = plan.step3_note || plan.plan_note;
    if (finalPlan) rows.push(`<div class="aps-row"><b>Step 3 决策</b><span>${esc(finalPlan)}</span></div>`);
    if (!rows.length) {
      card.classList.add('hidden');
      return;
    }
    card.innerHTML = `
      <div class="aps-head">
        <strong>☀ ${esc(tradeDate)} 盘前计划快照</strong>
        <span class="aps-mkt">${esc(mktParts.join(' · '))}</span>
      </div>
      <div class="aps-rows">${rows.join('')}</div>
    `;
    card.classList.remove('hidden');
  }

  function scheduleSave() {
    const cur = activeEntry();
    if (!cur) return;
    $('saveState').textContent = '保存中…';
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(async () => {
      try {
        const res = await api('PATCH', `/api/entries/${cur.id}`, {
          title: $('edTitle').value,
          trade_date: $('edDate').value,
          outcome: $('edOutcome').value,
          pnl: $('edPnl').value,
          bias_tags: cur.bias_tags || [],
          symbols: cur.symbols || [],
          raw_note: $('edRaw').value,
          context_note: $('edContext').value,
          execution_note: $('edExec').value,
          did_right_note: $('edDidRight').value,
          should_have_note: $('edShould').value,
          takeaway_note: $('edTakeaway').value,
        });
        replaceEntry(res.entry);
        S.stats = res.stats;
        $('saveState').textContent = '已保存 ✓';
        renderEntryList();
      } catch (e) {
        $('saveState').textContent = '保存失败';
      }
    }, 380);
  }

  // ---------- Views: Rules & Cache ----------
  function renderRulesView() {
    const st = S.stats || {};
    const mks = st.markers || {};
    $('kpiRow').innerHTML = `
      <div class="kpi"><div class="muted">累计复盘日记</div><div class="k-num">${st.totalEntries || 0}</div></div>
      <div class="kpi"><div class="muted">实际买卖打点</div><div class="k-num" style="color:var(--buy)">${(mks.buy || 0) + (mks.sell || 0)}</div></div>
      <div class="kpi"><div class="muted">Should have done</div><div class="k-num" style="color:var(--missed)">${mks.missed || 0}</div></div>
      <div class="kpi"><div class="muted">破纪律 / 冲动单</div><div class="k-num" style="color:var(--mistake)">${mks.mistake || 0}</div></div>
    `;

    const rBox = $('rulesList');
    rBox.innerHTML = S.rules.length ? S.rules.map((r) => `
      <div class="rule-item">
        <div>
          <div style="font-weight:600">${esc(r.rule_text)}</div>
          <div class="muted">${r.symbol ? `${esc(r.symbol)} · ` : ''}已提醒 ${r.hit_count} 次</div>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn xs" data-rhit="${r.id}">+1 再次触发</button>
          <button class="btn xs danger-ghost" data-rdel="${r.id}">删</button>
        </div>
      </div>
    `).join('') : `<div class="muted">在右侧复盘点「★ 收入纪律库」，这里会自动累计你的交易铁律。</div>`;

    rBox.querySelectorAll('[data-rhit]').forEach((b) => b.addEventListener('click', async () => {
      const res = await api('POST', `/api/rules/${b.dataset.rhit}/hit`);
      const idx = S.rules.findIndex((x) => x.id === res.rule.id);
      if (idx >= 0) S.rules[idx] = res.rule;
      renderRulesView();
    }));
    rBox.querySelectorAll('[data-rdel]').forEach((b) => b.addEventListener('click', async () => {
      await api('DELETE', `/api/rules/${b.dataset.rdel}`);
      S.rules = S.rules.filter((x) => x.id !== Number(b.dataset.rdel));
      renderRulesView();
    }));

    const biases = st.topBiases || [];
    const maxB = Math.max(1, ...biases.map((b) => b.count));
    $('biasChart').innerHTML = biases.length ? biases.map((b) => `
      <div class="bar-row">
        <div style="display:flex;justify-content:space-between"><b>${esc(b.tag)}</b><span class="muted">${b.count} 次</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.round((b.count / maxB) * 100)}%"></div></div>
      </div>
    `).join('') : `<div class="muted">AI 归类或手动打上心魔/偏差标签后，这里会自动统计哪类失误最值钱。</div>`;
  }

  function renderCacheView() {
    $('cacheTotal').textContent = `${S.cache.length} 组`;
    $('cacheTable').innerHTML = S.cache.length ? S.cache.map((c) => `
      <div class="cache-row">
        <div>
          <b style="font-family:var(--mono)">${esc(c.symbol)} · ${esc(c.interval)}</b>
          <span class="muted" style="margin-left:8px">${c.bar_count} 根 · ${esc(c.exchange || c.yahoo_sym)}</span>
        </div>
        <button class="btn xs" data-opench="${esc(c.symbol)}|${esc(c.interval)}">打开 K 线</button>
      </div>
    `).join('') : `<div class="muted">暂无缓存，输入 /SPX 即可自动拉取并写入本地 SQLite。</div>`;

    $('cacheTable').querySelectorAll('[data-opench]').forEach((b) => b.addEventListener('click', () => {
      const [sym, iv] = b.dataset.opench.split('|');
      closeCacheModal();
      openZoomModal(sym, iv);
    }));
  }

  function openCacheModal() {
    renderCacheView();
    $('cacheModal').classList.remove('hidden');
  }

  function closeCacheModal() {
    $('cacheModal').classList.add('hidden');
  }

  // ---------- Unified Freshness & Slide-Out Proactive Coach Drawer ----------
  function computeFreshnessInfo(summary) {
    if (!summary) return { text: '🕒 Last updated: 正在加载…', stale: false, shortTime: '--' };
    const iso = summary.lastUpdatedAt || summary.freshness?.lastUpdatedAt || summary.scannedAt || '';
    const eqDate = summary.freshness?.equityDate || summary.thermometer?.latestDate || summary.today || '--';
    let mins = 0;
    if (iso) {
      const parsed = Date.parse(iso);
      if (!Number.isNaN(parsed)) {
        mins = Math.max(0, Math.floor((Date.now() - parsed) / 60000));
      }
    }
    const stale = mins >= 120;
    const agoTxt = mins < 1 ? '刚刚' : (mins < 60 ? `${mins} 分钟前` : `${Math.floor(mins / 60)} 小时前`);
    const fullTs = iso ? iso.replace('T', ' ').slice(0, 19) : '--';
    const shortTime = iso ? iso.slice(11, 19) : '--';
    const text = stale
      ? `⚠ Last updated: ${fullTs}（${agoTxt} · 美股截至 ${eqDate}）· 点此重扫`
      : `● Last updated: ${fullTs}（${agoTxt}）· 美股日线截至 ${eqDate}`;
    return { text, stale, shortTime };
  }

  function setFreshnessLoading(isLoading) {
    const mf = $('morningFreshness');
    const btn = $('btnMorningRefresh');
    if (isLoading) {
      if (mf) {
        mf.className = 'fresh-pill loading';
        mf.textContent = '↻ 正在拉取 Yahoo 最新行情（48 标的）…';
      }
      if (btn) {
        btn.disabled = true;
        btn.textContent = '刷新中…';
      }
    } else {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '↻ 刷新全盘';
      }
      syncCoachBadgeAndFreshness();
    }
  }

  function syncCoachBadgeAndFreshness() {
    const sum = S.morningSummary;
    const { text, stale } = computeFreshnessInfo(sum);
    const mf = $('morningFreshness');
    if (mf && !mf.classList.contains('loading') && !mf.classList.contains('err')) {
      mf.textContent = text;
      mf.className = `fresh-pill${stale ? ' stale' : ''}`;
    }
    const cf = $('coachFreshness');
    if (cf) {
      cf.textContent = text;
      cf.classList.toggle('stale', stale);
    }

    const trigger = $('btnOpenCoach');
    const badge = $('coachBadge');
    if (!sum || !badge || !trigger) return;
    const reg = sum.regime || 'calm';
    trigger.classList.remove('alert-hot', 'alert-squeeze');
    badge.className = 'coach-badge';
    if (reg === 'extreme') {
      trigger.classList.add('alert-hot');
      badge.textContent = '极端日';
      badge.classList.remove('hidden');
    } else if (reg === 'squeeze') {
      trigger.classList.add('alert-squeeze');
      badge.textContent = '冰点变盘';
      badge.classList.add('squeeze');
      badge.classList.remove('hidden');
    } else if (reg === 'elevated') {
      badge.textContent = '高波动';
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  }

  function openCoachDrawer() {
    $('coachBackdrop').classList.remove('hidden');
    $('coachDrawer').classList.remove('hidden');
    if (!S.coachMessages.length) {
      loadCoachState(false);
    } else {
      renderCoachDrawer();
    }
  }

  function closeCoachDrawer() {
    $('coachBackdrop').classList.add('hidden');
    $('coachDrawer').classList.add('hidden');
  }

  async function loadCoachState(force = false) {
    try {
      const res = await api('GET', `/api/coach/state?force=${force}`);
      if (res.summary) {
        S.morningSummary = res.summary;
        if (res.summary.plan) S.todayPlan = res.summary.plan;
        if (res.summary.streak) S.planStreak = res.summary.streak;
        syncPlanNavDot();
        renderThermometer();
      }
      S.coachMessages = res.messages || [];
      syncCoachBadgeAndFreshness();
      if (!$('coachDrawer').classList.contains('hidden')) {
        renderCoachDrawer();
      }
    } catch (e) {
      if (!$('coachDrawer').classList.contains('hidden')) {
        toast(`加载波动教练失败：${e.message}`);
      }
    }
  }

  function fmtMd(s) {
    return esc(s).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  }

  function renderCoachDrawer() {
    const sum = S.morningSummary;
    syncCoachBadgeAndFreshness();
    if (sum) {
      const rl = $('coachRegimeLabel');
      rl.textContent = sum.regimeLabel || '扫描中…';
      rl.className = `radar-regime ${sum.regime || 'calm'}`;

      const t = sum.thermometer || {};
      const v = sum.vix;
      const med = Number(t.medianPct || 0);
      const chips = [];
      if (v) {
        chips.push(`<span class="cd-sync-chip">VIX <strong>${v.price}</strong> <span class="muted">60日${v.percentile60d}%</span></span>`);
      }
      if (t.totalCount) {
        chips.push(`<span class="cd-sync-chip">自选中位数 <strong class="${med >= 0 ? 'sc-up' : 'sc-dn'}">${med >= 0 ? '+' : ''}${med.toFixed(2)}%</strong></span>`);
        chips.push(`<span class="cd-sync-chip">1年罕见度 <strong>${t.extremeDaysInYear}/${t.tradingDaysInYear}天</strong></span>`);
      }
      $('coachSyncStrip').innerHTML = chips.join('');
    }

    const feed = $('coachFeed');
    feed.innerHTML = (S.coachMessages || []).map((m) => `
      <div class="c-msg ${m.role}">
        <div class="c-meta">${m.role === 'agent' ? '🔥 Proactive Volatility Coach' : '我'}${m.created_at ? ` · ${esc(m.created_at.slice(11, 16))}` : ''}</div>
        <div class="c-body">${fmtMd(m.content)}</div>
      </div>
    `).join('');
    feed.scrollTop = feed.scrollHeight;
  }

  async function sendCoachMessage(overrideText) {
    const inp = $('coachInput');
    const text = (overrideText !== undefined ? overrideText : inp.value).trim();
    if (!text) return;
    inp.value = '';
    S.coachMessages.push({ role: 'user', content: text });
    S.coachMessages.push({ role: 'agent', content: '正在结合当前实时体温计和你的交易铁律思考方案…' });
    renderCoachDrawer();
    const btn = $('btnCoachSend');
    btn.disabled = true;
    try {
      const res = await api('POST', '/api/coach/reply', { message: text });
      if (res.summary) {
        S.morningSummary = res.summary;
        renderThermometer();
      }
      S.coachMessages = res.messages;
      renderCoachDrawer();
    } catch (e) {
      S.coachMessages.pop();
      toast(`教练回复失败：${e.message}`);
      renderCoachDrawer();
    } finally {
      btn.disabled = false;
    }
  }

  function applyCoachToTodayPlan() {
    const userMsgs = (S.coachMessages || []).filter((m) => m.role === 'user');
    const lastUser = userMsgs[userMsgs.length - 1]?.content || '';
    const lastAgent = (S.coachMessages || []).slice().reverse().find((m) => m.role === 'agent')?.content || '';
    const snippet = (lastUser || lastAgent.split('\n').find((l) => l.trim() && !l.startsWith('🕒')) || '').replace(/\*\*/g, '').slice(0, 140);
    closeCoachDrawer();
    switchView('morning');
    const el = $('planStep3');
    if (el && snippet) {
      el.value = el.value.trim() ? `${el.value.trim()}\n${snippet}` : snippet;
      autoGrowTextarea(el);
      schedulePlanSave();
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.focus();
      toast('已写入 Step 3 今日交易决策并自动保存 ✓');
    }
  }

  // ---------- Morning Desk (Pre-trading: 2-Card Thermometer & 3-Step Auto-Save Plan) ----------
  function fmtPrice(px) {
    if (px === null || px === undefined || Number.isNaN(Number(px))) return '--';
    const n = Number(px);
    if (n >= 10000) return n.toLocaleString('en-US', { maximumFractionDigits: 1 });
    if (n >= 1000) return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
    if (n >= 10) return n.toFixed(2);
    return n.toFixed(3);
  }

  function destroyMorningCharts() {
    if (morningObserver) {
      morningObserver.disconnect();
      morningObserver = null;
    }
    morningChartInstances.forEach((inst) => {
      try { inst.ro?.disconnect(); } catch {}
      try { inst.chart?.remove(); } catch {}
    });
    morningChartInstances = [];
  }

  function syncPlanNavDot() {
    const dot = $('planNavDot');
    if (!dot) return;
    const hasPlan = Boolean(S.todayPlan?.has_plan || S.todayPlan?.is_signed);
    dot.classList.toggle('signed', hasPlan);
    dot.title = hasPlan ? '今日盘前计划已记录 ✓' : '今日盘前计划尚未填写';
  }

  function captureMarketSnapshot() {
    const sum = S.morningSummary;
    if (!sum) return {};
    const t = sum.thermometer || {};
    const v = sum.vix || {};
    return {
      latestDate: t.latestDate || sum.today || '',
      medianPct: t.medianPct,
      meanPct: t.meanPct,
      upCount: t.upCount,
      downCount: t.downCount,
      totalCount: t.totalCount,
      extremeDaysInYear: t.extremeDaysInYear,
      tradingDaysInYear: t.tradingDaysInYear,
      extremePctRank: t.extremePctRank,
      vixPrice: v.price,
      vixPct60d: v.percentile60d,
      regime: sum.regime || 'calm',
    };
  }

  function schedulePlanSave() {
    const statusEls = document.querySelectorAll('.plan-save-hint');
    statusEls.forEach((el) => { el.textContent = '保存中…'; });
    clearTimeout(S.planSaveTimer);
    S.planSaveTimer = setTimeout(async () => {
      const s1 = $('planStep1')?.value ?? (S.todayPlan?.step1_note || '');
      const s2 = $('planStep2')?.value ?? (S.todayPlan?.step2_note || '');
      const s3 = $('planStep3')?.value ?? (S.todayPlan?.step3_note || '');
      try {
        const res = await api('POST', '/api/plans/today', {
          step1_note: s1,
          step2_note: s2,
          step3_note: s3,
          plan_note: s3,
          market_snapshot: captureMarketSnapshot(),
        });
        S.todayPlan = res.plan;
        S.planStreak = res.streak;
        syncPlanNavDot();
        document.querySelectorAll('.plan-save-hint').forEach((el) => { el.textContent = '已自动保存 ✓'; });
      } catch (e) {
        document.querySelectorAll('.plan-save-hint').forEach((el) => { el.textContent = '保存失败'; });
      }
    }, 450);
  }

  function renderThermometer() {
    const sum = S.morningSummary;
    if (!sum) return;
    const t = sum.thermometer || {};
    const v = sum.vix;

    const med = Number(t.medianPct || 0);
    const avg = Number(t.meanPct || 0);
    const tot = Math.max(1, t.totalCount || 0);
    const upW = Math.round(((t.upCount || 0) / tot) * 100);
    const flatW = Math.round(((t.flatCount || 0) / tot) * 100);
    const fmtSign = (x) => `${x >= 0 ? '+' : ''}${Number(x).toFixed(2)}%`;

    const groupPills = (t.groupStats || []).map((g) => {
      const cls = g.medianPct >= 0 ? 'sc-up' : 'sc-dn';
      const shortName = g.id === 'ai_compute' ? 'AI芯片/设备' : (g.id === 'memory_optical' ? '存储/光通信' : '巨头/防御');
      return `<span class="tc-gpill"><span>${shortName}</span><strong class="${cls}">${fmtSign(g.medianPct)}</strong></span>`;
    }).join('');

    const moversHtml = [
      ...(t.topLosers || []).slice(0, 3).map((m) => `<span class="tc-mover dn" data-zsym="${esc(m.symbol)}">${esc(m.symbol)} ${fmtSign(m.pct)}</span>`),
      ...(t.topGainers || []).slice(0, 3).map((m) => `<span class="tc-mover up" data-zsym="${esc(m.symbol)}">${esc(m.symbol)} ${fmtSign(m.pct)}</span>`),
    ].join('');

    const sevLabel = t.severity === 'extreme'
      ? (t.direction === 'down' ? '🔥 极端普跌日' : '🔥 极端普涨日')
      : (t.severity === 'elevated' ? '⚡ 显著波动日' : '🟢 常规交易日');

    const showCoachCta = sum.regime === 'extreme' || sum.regime === 'squeeze' || sum.regime === 'elevated';
    const coachCtaLabel = sum.regime === 'squeeze'
      ? '🧊 变盘窗口 · 问波动教练 ›'
      : (sum.regime === 'extreme' ? '🔥 极端波动 · 问教练定 Bet ›' : '🔥 问盘前波动教练 ›');

    const vixCard = v ? `
      <div class="thermo-card vix-dark ${esc(v.state)}" data-zsym="VIX" style="cursor:pointer">
        <div class="tc-head">
          <span class="tc-label">VIX 恐慌指数中枢</span>
          <span class="vh-state">${esc(v.stateLabel)}</span>
        </div>
        <div class="tc-nums-row">
          <span class="tc-big">${v.price}</span>
          <div class="tc-sub-stat">
            <span class="${v.dayPct >= 0 ? 'sc-dn' : 'sc-up'}">日 ${fmtSign(v.dayPct)}</span>
            <span class="muted">5日 ${fmtSign(v.weekPct)}</span>
          </div>
        </div>
        <div class="vh-bar-wrap">
          <div class="vh-labels"><span>低位 ${v.low60d}</span><span>60日 ${v.percentile60d}% · 1年 ${v.percentile1y}%</span><span>高位 ${v.high60d}</span></div>
          <div class="vh-track"><div class="vh-pin" style="left:${Math.max(3, Math.min(97, v.percentile60d))}%"></div></div>
        </div>
        <div class="vh-meta">
          <span>1年区间 ${v.low1y} – ${v.high1y}</span>
          <span>压缩比 ${v.squeezeRatio}×</span>
        </div>
        <button class="vix-coach-cta" data-open-coach="1" type="button">${showCoachCta ? coachCtaLabel : '🔥 盘前问波动教练 ›'}</button>
      </div>
    ` : `<div class="thermo-card vix-dark"><div class="tc-label">VIX 暂无数据</div></div>`;

    $('thermoGrid').innerHTML = `
      <div class="thermo-card ${t.severity === 'extreme' ? 'extreme' : ''}">
        <div class="tc-head">
          <span class="tc-label">昨日自选股票池（${t.totalCount || 0} 只 · ${esc(t.latestDate || '--')}）</span>
          <span class="muted">${t.upCount || 0} 涨 / ${t.downCount || 0} 跌</span>
        </div>
        <div class="tc-nums-row">
          <div>
            <div class="muted" style="font-size:11px">35 只中位数涨跌</div>
            <div class="tc-big ${med >= 0 ? 'up' : 'dn'}">${fmtSign(med)}</div>
          </div>
          <div class="tc-sub-stat">
            <span class="muted">平均涨跌</span>
            <strong class="${avg >= 0 ? 'sc-up' : 'sc-dn'}">${fmtSign(avg)}</strong>
          </div>
        </div>
        <div class="breadth-bar">
          <div class="bb-up" style="width:${upW}%"></div>
          <div class="bb-flat" style="width:${flatW}%"></div>
        </div>
        <div class="tc-rank-line ${esc(t.severity || 'normal')}">
          <span>按 35 只中位数涨跌幅：过去 1 年仅 <strong>${t.extremeDaysInYear ?? '--'} / ${t.tradingDaysInYear || 252} 天</strong> 达到此幅度（前 ${t.extremePctRank ?? '--'}%）</span>
          <span class="rarity-pill ${esc(t.severity || 'normal')}">${sevLabel}</span>
        </div>
        <div class="tc-group-pills">${groupPills}</div>
        <div class="tc-movers">${moversHtml}</div>
      </div>

      ${vixCard}
    `;

    $('thermoGrid').querySelectorAll('[data-open-coach]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        openCoachDrawer();
      });
    });
    $('thermoGrid').querySelectorAll('[data-zsym]').forEach((el) => {
      el.addEventListener('click', () => openZoomModal(el.dataset.zsym, S.morningInterval));
    });
    syncCoachBadgeAndFreshness();
  }

  function zoomChartRange(c, factor) {
    if (!c) return;
    const ts = c.timeScale();
    const r = ts.getVisibleLogicalRange();
    if (!r) return;
    const span = Math.max(8, r.to - r.from);
    const nextSpan = Math.max(10, Math.min(400, span * factor));
    ts.setVisibleLogicalRange({ from: r.to - nextSpan, to: r.to });
  }

  function resetChartAxes(c, barCount, defaultBars = 68) {
    if (!c) return;
    try { c.priceScale('right').applyOptions({ autoScale: true }); } catch {}
    if (barCount > defaultBars + 4) {
      c.timeScale().setVisibleLogicalRange({ from: barCount - defaultBars, to: barCount + 2 });
    } else {
      c.timeScale().fitContent();
    }
  }

  function mountMiniChart(el, card, interval, chartRegistry) {
    if (!el || el.dataset.mounted === '1') return;
    el.dataset.mounted = '1';
    const bars = card.candles || [];
    if (!bars.length) return;
    const isIntraday = interval === '15m' || interval === '4h';
    const c = LightweightCharts.createChart(el, {
      width: el.clientWidth || 295,
      height: el.clientHeight || 195,
      layout: {
        background: { color: '#ffffff' },
        textColor: '#52525b',
        fontSize: 10,
        fontFamily: 'SF Mono, ui-monospace, Menlo, monospace',
      },
      grid: { vertLines: { color: '#f5f4f0' }, horzLines: { color: '#f5f4f0' } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: {
        borderVisible: true,
        borderColor: '#e4e1d8',
        autoScale: true,
        scaleMargins: { top: 0.08, bottom: 0.08 },
      },
      timeScale: {
        borderVisible: true,
        borderColor: '#e4e1d8',
        timeVisible: isIntraday,
        secondsVisible: false,
        rightOffset: 3,
      },
      ...FULL_CHART_INTERACTION,
    });
    const cs = c.addCandlestickSeries({
      ...HOLLOW_CANDLE_OPTS,
      priceLineVisible: false,
    });
    cs.setData(bars);
    if (S.morningEma) {
      EMA_LINES.forEach((cfg) => {
        const pts = calcEma(bars, cfg.period);
        if (!pts.length) return;
        const line = c.addLineSeries({
          color: cfg.color,
          lineWidth: cfg.width,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        line.setData(pts);
      });
    }
    resetChartAxes(c, bars.length, 68);
    const ro = new ResizeObserver(() => {
      if (el.clientWidth > 0 && el.clientHeight > 0) {
        c.applyOptions({ width: el.clientWidth, height: el.clientHeight });
      }
    });
    ro.observe(el);
    const entry = { chart: c, ro, barCount: bars.length };
    morningChartInstances.push(entry);
    if (chartRegistry && el.dataset.chartkey) {
      chartRegistry[el.dataset.chartkey] = entry;
    }
  }

  function renderMorningSections(chartBundle) {
    destroyMorningCharts();
    const wrap = $('morningSections');
    const sections = chartBundle?.sections || [];
    const iv = chartBundle?.interval || S.morningInterval;
    const gMap = {};
    (S.morningSummary?.thermometer?.groupStats || []).forEach((g) => { gMap[g.id] = g; });

    const cardLookup = {};
    const chartRegistry = {};
    wrap.innerHTML = sections.map((sec) => {
      const g = gMap[sec.id];
      const statBadge = g
        ? `<span class="msec-stat ${g.medianPct >= 0 ? 'sc-up' : 'sc-dn'}">分组中位数 ${g.medianPct >= 0 ? '+' : ''}${g.medianPct}% · ${g.upCount}涨/${g.downCount}跌</span>`
        : `<span class="muted">${sec.cards.length} 个标的</span>`;

      const cardsHtml = sec.cards.map((card) => {
        const key = `${sec.id}__${card.symbol}`;
        cardLookup[key] = card;
        const pct = card.dayPct;
        const pctCls = pct === null || pct === undefined ? '' : (pct >= 0 ? 'up' : 'dn');
        const pctTxt = pct === null || pct === undefined ? '--' : `${pct >= 0 ? '+' : ''}${Number(pct).toFixed(2)}%`;
        const posTxt = card.posPct !== null && card.posPct !== undefined ? `区间位置 ${card.posPct}%` : '暂无数据';
        return `
          <article class="m-card" data-ckey="${esc(key)}" data-sym="${esc(card.symbol)}">
            <div class="mc-head" data-zsym="${esc(card.symbol)}" title="点击放大查看或切换周期">
              <div class="mc-left">
                <div class="mc-sym-row">
                  <span class="mc-sym">${esc(card.symbol)}</span>
                  <span class="mc-subtag">${esc(card.subtag)}</span>
                </div>
                <span class="mc-name">${esc(card.name)}</span>
              </div>
              <div class="mc-right">
                <span class="mc-px">${fmtPrice(card.lastPrice)}</span>
                <span class="mc-pct ${pctCls}">${pctTxt}</span>
              </div>
            </div>
            <div class="mc-chart-wrap" title="滚轮/双指缩放 · 拖动横轴/纵轴可自由拉伸 · 双击坐标轴重置">
              ${card.available
                ? `<div class="mc-chart-el" data-chartkey="${esc(key)}"></div>`
                : `<div class="mc-empty">暂无 ${esc(iv)} K 线数据</div>`}
            </div>
            <div class="mc-foot">
              <span>${posTxt}</span>
              <div class="mc-foot-acts">
                <button class="mc-zoom-btn" data-mzoom="in" data-mkey="${esc(key)}" title="放大 K 线">＋</button>
                <button class="mc-zoom-btn" data-mzoom="out" data-mkey="${esc(key)}" title="缩小 K 线">－</button>
                <button class="mc-zoom-btn" data-mzoom="reset" data-mkey="${esc(key)}" title="重置横纵轴">↺</button>
                <button class="mc-zoom-btn" data-zsym="${esc(card.symbol)}">放大 ⤢</button>
              </div>
            </div>
          </article>
        `;
      }).join('');

      const p = S.todayPlan || {};
      let stepNoteHtml = '';
      if (sec.id === 'indices') {
        stepNoteHtml = `
          <div class="step-note-box">
            <div class="snb-head">
              <span class="snb-title">✎ Step 1 结论 · 大盘定调</span>
              <span class="muted plan-save-hint">${p.step1_note ? '已保存' : '输入自动保存'}</span>
            </div>
            <textarea id="planStep1" class="step-note-in" rows="2" placeholder="看完标普 / 道指 / 纳指 / 黄金 / BTC 的周线与日线，大盘整体处于什么位置？今天大方向偏多、偏空还是震荡？">${esc(p.step1_note || '')}</textarea>
          </div>
        `;
      } else if (sec.id === 'etfs') {
        stepNoteHtml = `
          <div class="step-note-box">
            <div class="snb-head">
              <span class="snb-title">✎ Step 2 结论 · 板块轮动与强弱</span>
              <span class="muted plan-save-hint">${p.step2_note ? '已保存' : '输入自动保存'}</span>
            </div>
            <textarea id="planStep2" class="step-note-in" rows="2" placeholder="对比 SMH / QQQ / XLV / EWY / SCHD：是全盘普涨普跌，还是资金切去了防御或半导体领跌？">${esc(p.step2_note || '')}</textarea>
          </div>
        `;
      } else if (sec.id === 'mega_other') {
        stepNoteHtml = `
          <div class="step-note-box final-decision">
            <div class="snb-head">
              <span class="snb-title">🎯 Step 3 结论 · 今日交易决策</span>
              <div class="snb-acts">
                <span class="muted plan-save-hint">${(p.step3_note || p.plan_note) ? '已保存' : '输入自动保存'}</span>
                <button id="btnStepCoach" class="btn xs accent" type="button">🔥 问盘前波动教练</button>
                <button id="btnGoAfterReview" class="btn xs" type="button">去写盘后复盘 ›</button>
              </div>
            </div>
            <textarea id="planStep3" class="step-note-in" rows="2" placeholder="写下今天的最终交易决策（哪怕只是 Do Nothing 也是完整计划）：准备做哪一只？15m 等什么信号入场？最大亏损上限是多少？">${esc(p.step3_note || p.plan_note || '')}</textarea>
          </div>
        `;
      }

      return `
        <section class="msec" id="msec-${esc(sec.id)}">
          <div class="msec-head">
            <div class="msec-title-wrap">
              <span class="msec-step">${esc(sec.step)}</span>
              <h3>${esc(sec.title)}</h3>
              <span class="muted">${esc(sec.subtitle)}</span>
            </div>
            ${statBadge}
          </div>
          <div class="mcard-grid">${cardsHtml}</div>
          ${stepNoteHtml}
        </section>
      `;
    }).join('');

    ['planStep1', 'planStep2', 'planStep3'].forEach((id) => {
      const ta = $(id);
      if (!ta) return;
      autoGrowTextarea(ta);
      ta.addEventListener('input', () => {
        autoGrowTextarea(ta);
        if (S.todayPlan) {
          if (id === 'planStep1') S.todayPlan.step1_note = ta.value;
          if (id === 'planStep2') S.todayPlan.step2_note = ta.value;
          if (id === 'planStep3') { S.todayPlan.step3_note = ta.value; S.todayPlan.plan_note = ta.value; }
        }
        schedulePlanSave();
      });
    });
    $('btnStepCoach')?.addEventListener('click', openCoachDrawer);
    $('btnGoAfterReview')?.addEventListener('click', () => openOrCreateEntryForDate(S.today));

    wrap.querySelectorAll('[data-zsym]').forEach((el) => {
      el.addEventListener('click', () => openZoomModal(el.dataset.zsym, iv));
    });
    wrap.querySelectorAll('[data-mzoom]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const entry = chartRegistry[btn.dataset.mkey];
        if (!entry) return;
        if (btn.dataset.mzoom === 'in') zoomChartRange(entry.chart, 0.7);
        else if (btn.dataset.mzoom === 'out') zoomChartRange(entry.chart, 1.45);
        else resetChartAxes(entry.chart, entry.barCount, 68);
      });
    });

    // 视口懒加载 LightweightCharts，48 张交互图丝滑零卡顿
    morningObserver = new IntersectionObserver((entries) => {
      entries.forEach((ent) => {
        if (!ent.isIntersecting) return;
        const chartEl = ent.target;
        const card = cardLookup[chartEl.dataset.chartkey];
        if (card) mountMiniChart(chartEl, card, iv, chartRegistry);
        morningObserver?.unobserve(chartEl);
      });
    }, { rootMargin: '260px 0px' });

    wrap.querySelectorAll('.mc-chart-el').forEach((el) => morningObserver.observe(el));
  }

  async function loadMorningCharts(interval = S.morningInterval, { force = false } = {}) {
    S.morningInterval = interval;
    document.querySelectorAll('#morningIvSeg [data-miv]').forEach((b) => {
      b.classList.toggle('on', b.dataset.miv === interval);
    });
    if (!force && S.morningChartsCache[interval]) {
      renderMorningSections(S.morningChartsCache[interval]);
      return;
    }
    $('morningSections').innerHTML = `<div class="morning-skeleton">正在并发加载全部 48 个标的的 <strong>${esc(interval)}</strong> K 线（首次拉取约 3–5 秒，之后本地缓存秒开）…</div>`;
    try {
      const res = await api('GET', `/api/morning/charts?interval=${encodeURIComponent(interval)}&force=${force ? 'true' : 'false'}`);
      S.morningChartsCache[interval] = res;
      renderMorningSections(res);
    } catch (e) {
      $('morningSections').innerHTML = `<div class="morning-skeleton">K 线加载失败：${esc(e.message)}</div>`;
    }
  }

  async function loadMorningDesk({ force = false } = {}) {
    if (force) S.morningChartsCache = {};
    setFreshnessLoading(true);
    let sumErr = null;
    try {
      const sumPromise = api('GET', `/api/morning/summary?force=${force ? 'true' : 'false'}`).then((res) => {
        S.morningSummary = res;
        if (res.plan) S.todayPlan = res.plan;
        if (res.streak) S.planStreak = res.streak;
        syncPlanNavDot();
        renderThermometer();
        return res;
      }).catch((e) => {
        sumErr = e;
        toast(`晨间摘要刷新失败：${e.message}`);
      });
      const chartPromise = loadMorningCharts(S.morningInterval, { force });
      await Promise.all([sumPromise, chartPromise]);
      if (S.morningChartsCache[S.morningInterval]) {
        renderMorningSections(S.morningChartsCache[S.morningInterval]);
      }
      S.morningLoaded = true;
    } finally {
      const mf = $('morningFreshness');
      if (mf) mf.classList.remove('loading');
      setFreshnessLoading(false);
      if (sumErr && mf) {
        mf.className = 'fresh-pill err';
        mf.textContent = `✕ 刷新失败：${sumErr.message} · 点此重试`;
      } else if (force && S.morningSummary) {
        const info = computeFreshnessInfo(S.morningSummary);
        toast(`全盘数据已更新（Last updated: ${info.shortTime}）✓`);
      }
    }
  }

  // ---------- Zoom Modal for Single Ticker Inspection ----------
  function closeZoomModal() {
    $('zoomModal').classList.add('hidden');
    if (zoomChart) {
      try { zoomChart.remove(); } catch {}
      zoomChart = null;
      zoomCandleSeries = null;
      zoomEmaSeriesList = [];
    }
  }

  async function openZoomModal(symbol, interval = S.morningInterval) {
    S.zoomSymbol = symbol;
    S.zoomInterval = interval;
    $('zoomModal').classList.remove('hidden');
    $('zoomSym').textContent = symbol;
    $('zoomName').textContent = '加载中…';
    $('zoomPrice').textContent = '--';
    $('zoomPct').textContent = '--';
    document.querySelectorAll('#zoomIvSeg [data-ziv]').forEach((b) => {
      b.classList.toggle('on', b.dataset.ziv === interval);
    });

    const el = $('zoomChartEl');
    if (zoomChart) {
      try { zoomChart.remove(); } catch {}
      zoomChart = null;
    }
    el.innerHTML = '';
    try {
      const data = await api('GET', `/api/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);
      $('zoomSym').textContent = data.symbol;
      $('zoomName').textContent = data.label || '';
      $('zoomPrice').textContent = fmtPrice(data.lastPrice);
      const sign = data.changePct >= 0 ? '+' : '';
      $('zoomPct').textContent = `${sign}${data.changePct}%`;
      $('zoomPct').className = `chg-tag ${data.changePct >= 0 ? 'sc-up' : 'sc-dn'}`;

      const isIntraday = interval === '15m' || interval === '4h' || interval === '5m' || interval === '1h';
      zoomChart = LightweightCharts.createChart(el, {
        width: el.clientWidth || 880,
        height: 460,
        layout: { background: { color: '#ffffff' }, textColor: '#374151', fontFamily: 'SF Mono, ui-monospace, Menlo, monospace' },
        grid: { vertLines: { color: '#f3f2ee' }, horzLines: { color: '#f3f2ee' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#e4e1d8', borderVisible: true, autoScale: true },
        timeScale: { borderColor: '#e4e1d8', borderVisible: true, timeVisible: isIntraday, secondsVisible: false, rightOffset: 4 },
        ...FULL_CHART_INTERACTION,
      });
      zoomCandleSeries = zoomChart.addCandlestickSeries({
        ...HOLLOW_CANDLE_OPTS,
      });
      const bars = data.candles || [];
      zoomCandleSeries.setData(bars);
      zoomEmaSeriesList = [];
      EMA_LINES.forEach((cfg) => {
        const pts = calcEma(bars, cfg.period);
        if (!pts.length) return;
        const s = zoomChart.addLineSeries({
          color: cfg.color,
          lineWidth: cfg.width + 0.2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        s.setData(pts);
        zoomEmaSeriesList.push(s);
      });
      S.zoomBarCount = bars.length;
      S.zoomBarCount = bars.length;
      zoomCandles = bars;
      renderZoomMarkers();
      zoomChart.subscribeClick(async (param) => {
        if (!param || !param.time || !zoomCandles.length) return;
        const ts = Number(param.time);
        const bar = zoomCandles.find((c) => c.time === ts);
        if (!bar) return;
        const entry = await ensureEntryForCurrentChart();
        const defaultNote = S.pinKind === 'missed'
          ? `在 ${bar.close} 看到信号但没下单`
          : (S.pinKind === 'mistake' ? `在 ${bar.close} 违背纪律` : '');
        try {
          const res = await api('POST', `/api/entries/${entry.id}/markers`, {
            kind: S.pinKind, ts, price: bar.close, note: defaultNote,
          });
          replaceEntry(res.entry);
          S.stats = res.stats;
          renderZoomMarkers();
          renderEntryList();
          toast(`已打点：${KIND_META[S.pinKind].text} @ ${bar.close}`);
        } catch (e) {
          toast(`打点失败：${e.message}`);
        }
      });
      resetChartAxes(zoomChart, bars.length, 100);
    } catch (e) {
      el.innerHTML = `<div class="mc-empty">加载 ${esc(symbol)} (${esc(interval)}) 失败：${esc(e.message)}</div>`;
    }
  }

  function renderZoomMarkers() {
    if (!zoomCandleSeries) return;
    zoomPriceLines.forEach((pl) => { try { zoomCandleSeries.removePriceLine(pl); } catch {} });
    zoomPriceLines = [];
    const cur = activeEntry();
    const markers = (cur && cur.markers) ? cur.markers : [];
    const sorted = [...markers].sort((a, b) => a.ts - b.ts);
    const barTimes = new Set(zoomCandles.map((c) => c.time));
    const visible = sorted.filter((m) => barTimes.has(m.ts));
    zoomCandleSeries.setMarkers(visible.map((m) => {
      const meta = KIND_META[m.kind] || KIND_META.missed;
      return { time: m.ts, position: meta.pos, color: meta.color, shape: meta.shape, text: meta.text };
    }));
    visible.forEach((m) => {
      const meta = KIND_META[m.kind] || KIND_META.missed;
      zoomPriceLines.push(zoomCandleSeries.createPriceLine({
        price: m.price,
        color: meta.color,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: meta.text,
      }));
    });
    const cntEl = $('zoomMarkerCount');
    if (cntEl) cntEl.textContent = visible.length ? `当前图 ${visible.length} 个锚点` : '';
  }

  // ---------- Tab 3: Monthly Daily Package Calendar ----------
  async function openOrCreateEntryForDate(dateStr) {
    const existing = S.entries.find((e) => e.trade_date === dateStr);
    if (existing) {
      S.activeEntryId = existing.id;
      renderEntryList();
      switchView('journal');
      return;
    }
    const res = await api('POST', '/api/entries', {
      trade_date: dateStr,
      symbol: 'SPX',
      interval: '15m',
      title: `${dateStr} 盘后复盘`,
    });
    S.entries.unshift(res.entry);
    S.activeEntryId = res.entry.id;
    S.stats = res.stats;
    renderEntryList();
    switchView('journal');
    setDictationCollapsed(false);
    $('edRaw')?.focus();
  }

  function shiftMonth(ym, delta) {
    const [y, m] = ym.split('-').map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }

  async function loadCalendarMonth(ym) {
    S.calMonth = ym || (S.today ? S.today.slice(0, 7) : new Date().toISOString().slice(0, 7));
    const [y, m] = S.calMonth.split('-');
    $('calMonthLabel').textContent = `${y} 年 ${Number(m)} 月`;
    try {
      const res = await api('GET', `/api/calendar?month=${encodeURIComponent(S.calMonth)}`);
      S.calDays = res.days || {};
      renderCalendarGrid();
      const datesWithData = Object.keys(S.calDays).sort();
      if (!S.selectedCalDate || !S.selectedCalDate.startsWith(S.calMonth)) {
        if (S.today && S.today.startsWith(S.calMonth)) S.selectedCalDate = S.today;
        else if (datesWithData.length) S.selectedCalDate = datesWithData[datesWithData.length - 1];
        else S.selectedCalDate = `${S.calMonth}-01`;
      }
      renderCalendarGrid();
      renderPackageDetail(S.selectedCalDate);
    } catch (e) {
      $('calendarGrid').innerHTML = `<div class="muted" style="grid-column:1/-1;padding:16px">日历加载失败：${esc(e.message)}</div>`;
    }
  }

  function renderCalendarGrid() {
    const grid = $('calendarGrid');
    if (!grid || !S.calMonth) return;
    const [y, m] = S.calMonth.split('-').map(Number);
    const firstDow = (new Date(y, m - 1, 1).getDay() + 6) % 7; // Mon=0..Sun=6
    const daysInMonth = new Date(y, m, 0).getDate();
    const cells = [];
    for (let i = 0; i < firstDow; i += 1) {
      cells.push(`<div class="cal-cell empty"></div>`);
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
      const dstr = `${y}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const pkg = S.calDays?.[dstr];
      const isToday = dstr === S.today;
      const isSel = dstr === S.selectedCalDate;
      const dots = [];
      if (pkg?.hasPlan) dots.push(`<span class="cc-dot plan" title="有盘前计划"></span>`);
      if (pkg?.hasReview) dots.push(`<span class="cc-dot ${esc(pkg.primaryOutcome || 'review')}" title="有盘后复盘"></span>`);
      const ms = pkg?.marketSnapshot || {};
      const mktTxt = ms.medianPct !== undefined
        ? `${Number(ms.medianPct) >= 0 ? '+' : ''}${Number(ms.medianPct).toFixed(1)}%${ms.vixPrice ? ` · V${Math.round(ms.vixPrice)}` : ''}`
        : (pkg?.pnl || '');
      const snippet = pkg?.titlePreview || pkg?.decisionPreview || '';
      cells.push(`
        <div class="cal-cell ${isToday ? 'today' : ''} ${isSel ? 'selected' : ''} ${pkg ? 'has-pkg' : ''}" data-cdate="${dstr}">
          <div class="cc-top">
            <span class="cc-day">${day}</span>
            <span class="cc-dots">${dots.join('')}</span>
          </div>
          ${mktTxt ? `<div class="cc-mkt">${esc(mktTxt)}</div>` : ''}
          ${snippet ? `<div class="cc-snippet">${esc(snippet)}</div>` : ''}
        </div>
      `);
    }
    grid.innerHTML = cells.join('');
    grid.querySelectorAll('[data-cdate]').forEach((el) => {
      el.addEventListener('click', () => {
        S.selectedCalDate = el.dataset.cdate;
        renderCalendarGrid();
        renderPackageDetail(S.selectedCalDate);
      });
    });
  }

  function renderPackageDetail(dstr) {
    const box = $('pkgDetail');
    if (!box) return;
    const pkg = S.calDays?.[dstr];
    const plan = pkg?.plan;
    const entries = pkg?.entries || [];
    const ms = pkg?.marketSnapshot || {};

    const mktChips = [];
    if (ms.vixPrice !== undefined) mktChips.push(`VIX ${ms.vixPrice}`);
    if (ms.medianPct !== undefined) mktChips.push(`35只中位数 ${Number(ms.medianPct) >= 0 ? '+' : ''}${Number(ms.medianPct).toFixed(2)}%`);
    if (ms.extremeDaysInYear && ms.tradingDaysInYear) mktChips.push(`1年排位 ${ms.extremeDaysInYear}/${ms.tradingDaysInYear}天`);

    const planRows = [];
    if (plan?.step1_note) planRows.push(`<div><b>Step 1 大盘：</b>${esc(plan.step1_note)}</div>`);
    if (plan?.step2_note) planRows.push(`<div><b>Step 2 板块：</b>${esc(plan.step2_note)}</div>`);
    const p3 = plan?.step3_note || plan?.plan_note;
    if (p3) planRows.push(`<div><b>Step 3 决策：</b>${esc(p3)}</div>`);

    const outcomeLabel = { win: '✅ 盈利', loss: '❌ 亏损', missed: '🟡 踏空', review: '📝 观察' };
    const entriesHtml = entries.length ? entries.map((e) => `
      <div class="pkg-entry-card">
        <div class="pkg-entry-top">
          <strong>${esc(e.title)}</strong>
          <span class="muted">${esc(outcomeLabel[e.outcome] || e.outcome)} ${esc(e.pnl || '')}</span>
        </div>
        ${e.did_right_note ? `<div style="font-size:12.5px"><b>做对了：</b>${esc(e.did_right_note.slice(0, 120))}</div>` : ''}
        ${e.should_have_note ? `<div style="font-size:12.5px"><b>Should have：</b>${esc(e.should_have_note.slice(0, 120))}</div>` : ''}
        ${e.takeaway_note ? `<div style="font-size:12.5px;color:#15803d"><b>学到了：</b>${esc(e.takeaway_note.slice(0, 120))}</div>` : ''}
        ${(e.bias_tags || []).length ? `<div class="pkg-tags">${e.bias_tags.map((t) => `#${esc(t)}`).join(' ')}</div>` : ''}
        <div><button class="btn xs" data-open-eid="${e.id}">打开完整复盘文章 ›</button></div>
      </div>
    `).join('') : `<div class="pkg-box muted">当日暂无盘后复盘记录。</div>`;

    box.innerHTML = `
      <div class="pkg-head">
        <h3>${esc(dstr)}</h3>
        <button class="btn xs primary" id="btnPkgWriteReview">${entries.length ? '编辑当日复盘 ›' : '+ 补写当日复盘'}</button>
      </div>
      ${mktChips.length ? `
        <div class="pkg-sec">
          <div class="pkg-sec-label">当日市场快照</div>
          <div class="pkg-box" style="font-family:var(--mono);font-size:12px">${esc(mktChips.join(' · '))}</div>
        </div>
      ` : ''}
      <div class="pkg-sec">
        <div class="pkg-sec-label">☀ 盘前计划（Pre-trading）</div>
        <div class="pkg-box">${planRows.length ? planRows.join('') : '<span class="muted">当日暂无盘前 3 步记录。</span>'}</div>
      </div>
      <div class="pkg-sec">
        <div class="pkg-sec-label">📝 盘后复盘（After-trading）</div>
        ${entriesHtml}
      </div>
    `;

    $('btnPkgWriteReview')?.addEventListener('click', () => openOrCreateEntryForDate(dstr));
    box.querySelectorAll('[data-open-eid]').forEach((btn) => {
      btn.addEventListener('click', () => {
        S.activeEntryId = Number(btn.dataset.openEid);
        renderEntryList();
        switchView('journal');
      });
    });
  }

  function switchView(name) {
    const view = ['morning', 'journal', 'rules'].includes(name) ? name : 'morning';
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
    $('viewMorning').classList.toggle('hidden', view !== 'morning');
    $('viewJournal').classList.toggle('hidden', view !== 'journal');
    $('viewRules').classList.toggle('hidden', view !== 'rules');
    // 盘前波动教练只属于盘前计划，切走时自动收起抽屉
    if (view !== 'morning') closeCoachDrawer();
    if (view === 'morning') {
      syncPlanNavDot();
      if (!S.morningLoaded) loadMorningDesk({ force: false });
      else syncCoachBadgeAndFreshness();
    }
    if (view === 'journal') {
      renderEditor();
    }
    if (view === 'rules') {
      renderRulesView();
      loadCalendarMonth(S.calMonth);
    }
  }

  // ---------- Events & Boot ----------
  function bindEvents() {
    document.querySelectorAll('.nav-btn').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.view)));

    $('btnNewEntry').addEventListener('click', createNewEntry);
    $('btnOrganize').addEventListener('click', organizeCurrentEntry);
    $('btnToggleDictation').addEventListener('click', () => setDictationCollapsed(!S.dictationCollapsed));
    $('searchInput').addEventListener('input', renderEntryList);

    // Inline Hashtag Picker
    $('btnToggleTagPicker').addEventListener('click', (ev) => {
      ev.stopPropagation();
      $('tagPickerPop').classList.toggle('hidden');
      if (!$('tagPickerPop').classList.contains('hidden')) $('customTagIn').focus();
    });
    $('customTagIn').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        const raw = ($('customTagIn').value || '').trim().replace(/^#+/, '');
        if (!raw) return;
        const cur = activeEntry();
        if (!cur) return;
        const set = new Set(cur.bias_tags || []);
        set.add(raw);
        cur.bias_tags = [...set];
        $('customTagIn').value = '';
        renderHashtags(cur);
        scheduleSave();
      }
    });
    document.addEventListener('click', (ev) => {
      const pop = $('tagPickerPop');
      if (pop && !pop.classList.contains('hidden') && !pop.contains(ev.target) && ev.target !== $('btnToggleTagPicker')) {
        pop.classList.add('hidden');
      }
      const spop = $('symPickerPop');
      if (spop && !spop.classList.contains('hidden') && !spop.contains(ev.target) && ev.target !== $('btnInsertSym')) {
        spop.classList.add('hidden');
      }
    });

    // Insert Inline K-Line Thumbnail Popover
    const QUICK_CHART_SYMS = ['SPX', 'NDX', 'NVDA', 'SMH', 'QQQ', 'MU', 'TSLA', 'AAPL', 'GOOG', 'AMZN', 'GOLD', 'BTC', 'VIX'];
    const addInlineSym = (rawInput) => {
      const raw = (rawInput || '').trim().replace(/^[/$\s]+/, '').toUpperCase();
      if (!raw) { toast('请输入标的代码或点击下方常用标的'); $('insertSymIn').focus(); return; }
      const cur = activeEntry();
      if (!cur) return;
      const existing = [...new Set((cur.symbols && cur.symbols.length) ? cur.symbols : (cur.symbol ? [cur.symbol] : []))];
      if (existing.includes(raw)) {
        toast(`${raw} 小图已在文章底部`);
        $('symPickerPop').classList.add('hidden');
        return;
      }
      cur.symbols = [...existing, raw].slice(0, 12);
      $('insertSymIn').value = '';
      $('symPickerPop').classList.add('hidden');
      renderInlineChartThumbs(cur);
      scheduleSave();
      toast(`已插入 ${raw} K 线小图 ✓`);
    };
    const renderSymPicker = () => {
      const cur = activeEntry();
      const existing = new Set((cur?.symbols && cur.symbols.length) ? cur.symbols : (cur?.symbol ? [cur.symbol] : []));
      $('symPresets').innerHTML = QUICK_CHART_SYMS.map((s) =>
        `<button type="button" class="tag-preset-btn ${existing.has(s) ? 'on' : ''}" data-qsym="${s}">${s}</button>`
      ).join('');
      $('symPresets').querySelectorAll('[data-qsym]').forEach((b) => {
        b.addEventListener('click', () => addInlineSym(b.dataset.qsym));
      });
    };
    $('btnInsertSym').addEventListener('click', (ev) => {
      ev.stopPropagation();
      renderSymPicker();
      $('symPickerPop').classList.toggle('hidden');
      if (!$('symPickerPop').classList.contains('hidden')) $('insertSymIn').focus();
    });
    $('btnInsertSymConfirm').addEventListener('click', () => addInlineSym($('insertSymIn').value));
    $('insertSymIn').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); addInlineSym($('insertSymIn').value); }
    });

    document.querySelectorAll('.pin-btn').forEach((b) => {
      b.addEventListener('click', () => {
        S.pinKind = b.dataset.kind;
        document.querySelectorAll('.pin-btn').forEach((x) => x.classList.toggle('on', x.dataset.kind === S.pinKind));
      });
    });

    const cmd = $('cmdInput');
    cmd.addEventListener('focus', () => renderCmdMenu(cmd.value));
    cmd.addEventListener('input', () => renderCmdMenu(cmd.value));
    cmd.addEventListener('blur', () => setTimeout(() => $('cmdMenu').classList.add('hidden'), 150));
    cmd.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); runSlashCommand(cmd.value); }
      else if (ev.key === 'Escape') { $('cmdMenu').classList.add('hidden'); cmd.blur(); }
    });

    window.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        if (!$('zoomModal').classList.contains('hidden')) { closeZoomModal(); return; }
        if (!$('cacheModal').classList.contains('hidden')) { closeCacheModal(); return; }
        if (!$('coachDrawer').classList.contains('hidden')) { closeCoachDrawer(); return; }
        if (!$('tagPickerPop').classList.contains('hidden')) { $('tagPickerPop').classList.add('hidden'); return; }
        if (!$('symPickerPop').classList.contains('hidden')) { $('symPickerPop').classList.add('hidden'); return; }
      }
      if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') {
        ev.preventDefault(); cmd.focus(); cmd.select(); return;
      }
      const tag = (document.activeElement?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (ev.key === '/') { ev.preventDefault(); cmd.focus(); }
      else if (['1', '2', '3', '4'].includes(ev.key)) {
        const map = { '1': 'buy', '2': 'sell', '3': 'missed', '4': 'mistake' };
        S.pinKind = map[ev.key];
        document.querySelectorAll('.pin-btn').forEach((x) => x.classList.toggle('on', x.dataset.kind === S.pinKind));
      }
    });

    ['edTitle', 'edDate', 'edOutcome', 'edPnl', 'edRaw', 'edContext', 'edExec', 'edDidRight', 'edShould', 'edTakeaway'].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener('input', () => {
        if (el.classList.contains('prose-in')) autoGrowTextarea(el);
        scheduleSave();
      });
      el.addEventListener('change', scheduleSave);
    });
    ['edRaw', 'edContext', 'edExec', 'edDidRight', 'edShould'].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && !ev.shiftKey) {
          const before = el.value.slice(0, el.selectionStart);
          const line = before.split('\n').pop().trim();
          if (/^\/[A-Za-z0-9._^-]{1,12}(\s+(5m|15m|30m|1h|4h|1d|1wk))?$/i.test(line)) {
            ev.preventDefault();
            runSlashCommand(line);
          }
        }
      });
    });

    $('btnDelEntry').addEventListener('click', async () => {
      const cur = activeEntry();
      if (!cur || !confirm(`确定删除「${cur.title}」吗？`)) return;
      const res = await api('DELETE', `/api/entries/${cur.id}`);
      S.entries = S.entries.filter((x) => x.id !== cur.id);
      S.activeEntryId = S.entries[0]?.id || null;
      S.stats = res.stats;
      renderEntryList();
      if (S.activeEntryId) renderEditor();
      else await createNewEntry();
    });

    $('btnPromoteRule').addEventListener('click', async () => {
      const cur = activeEntry();
      const text = ($('edTakeaway').value || '').trim();
      if (!text) { toast('请先在「下次学到了什么」里写一句规则'); return; }
      const res = await api('POST', '/api/rules', { entry_id: cur?.id || null, symbol: cur?.symbol || S.symbol, rule_text: text });
      S.rules.unshift(res.rule);
      toast('已收入交易纪律库 ★');
    });

    $('btnAddRule').addEventListener('click', async () => {
      const text = ($('newRuleIn').value || '').trim();
      if (!text) return;
      const res = await api('POST', '/api/rules', { rule_text: text });
      S.rules.unshift(res.rule);
      $('newRuleIn').value = '';
      renderRulesView();
    });

    $('btnAiReview').addEventListener('click', async () => {
      const box = $('aiReviewBox');
      box.classList.remove('hidden');
      box.textContent = '正在用 Gemini 分析你近期的交易复盘与 Should have done 模式…';
      try {
        const res = await api('POST', '/api/ai/review', {});
        box.textContent = res.reply;
      } catch (e) {
        box.textContent = `分析失败：${e.message}`;
      }
    });

    // Slide-Out Coach Drawer & Cache Modal & Calendar events
    $('btnCoachClose').addEventListener('click', closeCoachDrawer);
    $('coachBackdrop').addEventListener('click', closeCoachDrawer);
    $('btnCoachToPlan').addEventListener('click', applyCoachToTodayPlan);
    $('btnOpenCache').addEventListener('click', openCacheModal);
    $('btnCacheClose').addEventListener('click', closeCacheModal);
    $('cacheModal').addEventListener('click', (ev) => {
      if (ev.target === $('cacheModal')) closeCacheModal();
    });
    $('morningFreshness').addEventListener('click', () => {
      loadMorningDesk({ force: true });
    });
    $('edDate').addEventListener('change', () => {
      renderArticlePlanSnapshot($('edDate').value);
    });
    $('btnCalPrev').addEventListener('click', () => loadCalendarMonth(shiftMonth(S.calMonth || S.today.slice(0, 7), -1)));
    $('btnCalNext').addEventListener('click', () => loadCalendarMonth(shiftMonth(S.calMonth || S.today.slice(0, 7), 1)));
    $('btnCalToday').addEventListener('click', () => {
      S.selectedCalDate = S.today;
      loadCalendarMonth(S.today.slice(0, 7));
    });

    $('btnCoachSend').addEventListener('click', () => sendCoachMessage());
    $('coachInput').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendCoachMessage(); }
    });
    document.querySelectorAll('.q-chip').forEach((b) => {
      b.addEventListener('click', () => sendCoachMessage(b.dataset.qmsg));
    });
    $('btnCoachReset').addEventListener('click', async () => {
      const btn = $('btnCoachReset');
      btn.disabled = true;
      try {
        const res = await api('POST', '/api/coach/reset');
        if (res.summary) {
          S.morningSummary = res.summary;
          renderThermometer();
        }
        S.coachMessages = res.messages;
        renderCoachDrawer();
        toast('已按最新实时行情重扫并更新提问 ✓');
      } catch (e) {
        toast(`重扫失败：${e.message}`);
      } finally {
        btn.disabled = false;
      }
    });
    $('btnCoachCommit').addEventListener('click', async () => {
      try {
        const res = await api('POST', '/api/coach/commit');
        S.entries.unshift(res.entry);
        S.activeEntryId = res.entry.id;
        S.stats = res.stats;
        renderEntryList();
        renderEditor();
        closeCoachDrawer();
        switchView('journal');
        toast('已将本次推演存入复盘日记，可点「✦ Gemini 一键归类」结构化！');
      } catch (e) {
        toast(e.message);
      }
    });

    $('btnImportCsv').addEventListener('click', async () => {
      try {
        const res = await api('POST', '/api/candles/import-csv', {
          symbol: $('csvSym').value, interval: $('csvIv').value, csv_text: $('csvText').value,
        });
        S.cache = res.cache;
        renderCacheView();
        toast(`已导入 ${res.barCount} 根 K 线到本地 SQLite`);
      } catch (e) {
        toast(e.message);
      }
    });

    window.addEventListener('paste', async (ev) => {
      const items = ev.clipboardData?.items || [];
      for (const it of items) {
        if (it.type && it.type.startsWith('image/')) {
          ev.preventDefault();
          const file = it.getAsFile();
          if (file) await uploadImageFile(file);
          break;
        }
      }
    });
    $('imgFileIn').addEventListener('change', async (ev) => {
      const file = ev.target.files?.[0];
      if (file) await uploadImageFile(file);
      ev.target.value = '';
    });
    $('lightbox').addEventListener('click', () => $('lightbox').classList.add('hidden'));

    // Morning Desk & Daily Plan events
    document.querySelectorAll('#morningIvSeg [data-miv]').forEach((b) => {
      b.addEventListener('click', () => loadMorningCharts(b.dataset.miv));
    });
    $('btnMorningEma').addEventListener('click', () => {
      S.morningEma = !S.morningEma;
      $('btnMorningEma').classList.toggle('on', S.morningEma);
      if (S.morningChartsCache[S.morningInterval]) {
        renderMorningSections(S.morningChartsCache[S.morningInterval]);
      }
    });
    $('btnMorningRefresh').addEventListener('click', () => loadMorningDesk({ force: true }));

    // Zoom Modal events
    document.querySelectorAll('#zoomIvSeg [data-ziv]').forEach((b) => {
      b.addEventListener('click', () => {
        if (S.zoomSymbol) openZoomModal(S.zoomSymbol, b.dataset.ziv);
      });
    });
    $('btnZoomToJournal').addEventListener('click', async () => {
      const sym = S.zoomSymbol;
      closeZoomModal();
      if (sym) {
        const cur = await ensureEntryForCurrentChart();
        const nextSyms = [...new Set([...(cur.symbols || []), sym])].slice(0, 12);
        cur.symbols = nextSyms;
        switchView('journal');
        scheduleSave();
        toast(`已将 ${sym} 小图插入当前复盘 ✓`);
      }
    });
    $('btnZoomReset').addEventListener('click', () => {
      if (zoomChart) resetChartAxes(zoomChart, S.zoomBarCount || 100, 100);
    });
    $('btnZoomClose').addEventListener('click', closeZoomModal);
    $('zoomModal').addEventListener('click', (ev) => {
      if (ev.target === $('zoomModal')) closeZoomModal();
    });
  }

  async function uploadImageFile(file) {
    const entry = await ensureEntryForCurrentChart();
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const res = await api('POST', `/api/entries/${entry.id}/attachments`, { data_url: reader.result, caption: file.name || 'TradingView 截图' });
        replaceEntry(res.entry);
        renderEditor();
        toast('截图已保存到当前复盘 ✓');
      } catch (e) {
        toast(e.message);
      }
    };
    reader.readAsDataURL(file);
  }

  function startAutoRefreshLoop() {
    clearInterval(S.autoTimer);
    S.autoTimer = setInterval(() => {
      if (document.hidden) return;
      syncCoachBadgeAndFreshness();
    }, 30000);
  }

  async function boot() {
    bindEvents();
    const b = await api('GET', '/api/bootstrap');
    S.today = b.today;
    S.entries = b.entries || [];
    S.rules = b.rules || [];
    S.cache = b.cache || [];
    S.quickSymbols = b.quickSymbols || [];
    S.intervals = b.intervals || S.intervals;
    S.stats = b.stats || {};
    S.todayPlan = b.todayPlan || null;
    S.planStreak = b.planStreak || { streak: 0, totalSigned: 0, totalReviewed: 0 };
    syncPlanNavDot();

    if (!S.entries.length) {
      await createNewEntry();
    } else {
      S.activeEntryId = S.entries[0].id;
      S.symbol = S.entries[0].symbol;
      S.interval = S.entries[0].interval;
      renderEntryList();
    }

    const params = new URLSearchParams(window.location.search);
    const initialView = params.get('view') || 'morning';
    switchView(initialView);
    startAutoRefreshLoop();
  }

  boot().catch((e) => toast(`初始化失败：${e.message}`));
})();
