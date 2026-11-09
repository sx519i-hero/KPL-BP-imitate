/* ============================================================
   data.js — 数据页（赛季 / 英雄 / 战队 / 选手 / BP 复盘）
   依赖：base.html + api.js（window.KPL），缺失时自动降级 fetch。
   原则：所有数值来自 API，null 一律渲染 —，绝不编造。
   ============================================================ */
(function () {
  'use strict';

  var KPL = (window.KPL = window.KPL || {});

  /* ---------------- 头像/logo 兜底（全局，供 onerror 调用） ---------------- */
  window.__kplAvtFallback = function (img) {
    try {
      var box = img.parentNode;
      if (!box) return;
      box.classList.add('avt-fallback');
      var txt = box.querySelector('.avt-txt');
      if (!txt) {
        txt = document.createElement('span');
        txt.className = 'avt-txt';
        txt.textContent = img.getAttribute('data-fb') || '?';
        box.appendChild(txt);
      }
      img.parentNode.removeChild(img);
    } catch (e) { /* noop */ }
  };

  /* ---------------- 格式化（优先用 api.js 的实现） ---------------- */
  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function pct(v) {
    if (typeof KPL.fmtPct === 'function') {
      try { return KPL.fmtPct(v); } catch (e) { /* fallthrough */ }
    }
    return isNum(v) ? v.toFixed(2) + '%' : '—';
  }
  function num(v) {
    if (typeof KPL.fmtNum === 'function') {
      try { return KPL.fmtNum(v); } catch (e) { /* fallthrough */ }
    }
    return isNum(v) ? String(Math.round(v * 100) / 100) : '—';
  }
  function int(v) {
    if (typeof KPL.fmtInt === 'function') {
      try { return KPL.fmtInt(v); } catch (e) { /* fallthrough */ }
    }
    return isNum(v) ? String(Math.round(v)) : '—';
  }
  function toast(msg) {
    if (typeof KPL.toast === 'function') { try { KPL.toast(msg); return; } catch (e) {} }
    if (window.console && console.warn) console.warn('[kpl]', msg);
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ---------------- API 层（优先 window.KPL.api，缺失则直连 fetch） ---------------- */
  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || d.ok === false) throw new Error((d && d.error) || '接口返回失败');
        return d;
      });
  }

  function apiFromFetch() {
    return {
      leagues: function () { return getJSON('/api/leagues'); },
      leagueMeta: function (id) { return getJSON('/api/league_meta?league_id=' + encodeURIComponent(id)); },
      heroes: function (id) { return getJSON('/api/heroes?league_id=' + encodeURIComponent(id)); },
      teams: function (id) { return getJSON('/api/teams?league_id=' + encodeURIComponent(id)); },
      players: function (id) { return getJSON('/api/players?league_id=' + encodeURIComponent(id)); },
      bpHistory: function (id, limit) {
        return getJSON('/api/bp/history?league_id=' + encodeURIComponent(id) + '&limit=' + (limit || 30));
      }
    };
  }

  var api = (KPL.api && typeof KPL.api.leagues === 'function') ? KPL.api : apiFromFetch();

  function call(name, a, b) {
    var fn = api && api[name];
    if (typeof fn !== 'function') return Promise.reject(new Error('缺少接口 KPL.api.' + name));
    try { return Promise.resolve(fn(a, b)); }
    catch (e) { return Promise.reject(e); }
  }

  /* ---------------- 全局赛季状态 ---------------- */
  var LS_KEY = 'kpl.league_id';

  function getLeagueId() {
    if (typeof KPL.getLeagueId === 'function') {
      try {
        var v = KPL.getLeagueId();
        if (v) return String(v);
      } catch (e) {}
    }
    try { return localStorage.getItem(LS_KEY) || null; } catch (e) { return null; }
  }
  function setLeagueId(id) {
    if (!id) return;
    id = String(id);
    if (typeof KPL.setLeagueId === 'function') { try { KPL.setLeagueId(id); return; } catch (e) {} }
    try { localStorage.setItem(LS_KEY, id); } catch (e) {}
  }

  /* ---------------- 小工具 ---------------- */
  function avatarHtml(path, ok, label, sizeCls) {
    sizeCls = sizeCls || 'avt-sm';
    var fb = esc(String(label || '?').slice(0, 1));
    if (path) {
      return '<span class="avt ' + sizeCls + '">' +
        '<img src="' + esc(path) + '" alt="' + esc(label) + '" loading="lazy" ' +
        'data-fb="' + fb + '" onerror="window.__kplAvtFallback&&window.__kplAvtFallback(this)">' +
        '<span class="avt-txt">' + fb + '</span></span>';
    }
    return '<span class="avt ' + sizeCls + ' avt-fallback"><span class="avt-txt">' + fb + '</span></span>';
  }

  // 只生成头像「内部」内容（用于已存在的 .avt 容器）
  function avatarInner(path, label) {
    var fb = esc(String(label || '?').slice(0, 1));
    if (path) {
      return '<img src="' + esc(path) + '" alt="' + esc(label) + '" loading="lazy" data-fb="' + fb +
        '" onerror="window.__kplAvtFallback&&window.__kplAvtFallback(this)">' +
        '<span class="avt-txt">' + fb + '</span>';
    }
    return '<span class="avt-txt">' + fb + '</span>';
  }

  function logoHtml(path, ok, label, sizeCls) {
    sizeCls = sizeCls || 'lg-sm';
    var fb = esc(String(label || '?').slice(0, 2));
    if (path) {
      return '<span class="lg-logo ' + sizeCls + '">' +
        '<img src="' + esc(path) + '" alt="' + esc(label) + '" loading="lazy" ' +
        'data-fb="' + fb + '" onerror="window.__kplAvtFallback&&window.__kplAvtFallback(this)">' +
        '<span class="avt-txt">' + fb + '</span></span>';
    }
    return '<span class="lg-logo ' + sizeCls + ' avt-fallback"><span class="avt-txt">' + fb + '</span></span>';
  }

  function winRateClass(v) {
    if (!isNum(v)) return 'wr-none';
    if (v >= 55) return 'wr-hi';
    if (v >= 50) return 'wr-mid';
    return 'wr-lo';
  }

  /* ---------------- 分路（v2：KPL 实战分路） ----------------
     契约 v2 §2.1 / §4：分路一律只看 lane_list（该赛季真实 position_desc 统计）。
     绝不回退 role / lane / hero_type —— 拿不到就是「暂无该赛季分路数据」，
     不猜、不用其它赛季或 herolist 定位补。 */
  var LANES = ['对抗路', '打野', '中路', '发育路', '游走'];
  var LANE_NONE = '暂无该赛季分路数据';

  // 只认 lane_list；缺失 / 非数组 / 全空 → []
  function laneList(h) {
    var v = h && h.lane_list;
    if (!Array.isArray(v)) return [];
    var out = [];
    for (var i = 0; i < v.length; i++) {
      var s = v[i] == null ? '' : String(v[i]).trim();
      if (s && out.indexOf(s) < 0) out.push(s);
    }
    return out;
  }

  // lane_games：{分路: 局数}；缺失 → {}
  function laneGames(h) {
    var g = h && h.lane_games;
    if (!g || typeof g !== 'object' || Array.isArray(g)) return {};
    return g;
  }

  // 标签组：最多显示 max 个，超出折叠为 +N（契约 §4.1）
  function laneChipsHtml(list, max) {
    if (!list || !list.length) return '<span class="lane-none">' + esc(LANE_NONE) + '</span>';
    max = max || 2;
    var shown = list.slice(0, max);
    var rest = list.length - shown.length;
    var html = '<span class="lane-chips">';
    for (var i = 0; i < shown.length; i++) {
      html += '<span class="lane-chip">' + esc(shown[i]) + '</span>';
    }
    if (rest > 0) html += '<span class="lane-chip lane-chip-more">+' + rest + '</span>';
    html += '</span>';
    return html;
  }

  // 各分路局数文案：「对抗路 80 局 · 打野 12 局」
  function laneGamesText(list, games) {
    if (!list || !list.length) return '';
    var parts = [];
    for (var i = 0; i < list.length; i++) {
      var n = games ? games[list[i]] : null;
      parts.push(esc(list[i]) + ' ' + (isNum(n) ? int(n) : '—') + ' 局');
    }
    return parts.join(' · ');
  }

  function shortLeagueName(name) {
    if (!name) return '—';
    var s = String(name).replace(/王者荣耀|KPL|kpl|职业联赛/g, '').replace(/\s+/g, '');
    // 去掉 "KPL" 后会留下重复的「年」：「2026年年度总决赛」→「2026年度总决赛」
    s = s.replace(/年年度/g, '年度');
    return s || String(name);
  }

  function showEmpty(el, text) {
    el.innerHTML = '<div class="d-empty">' + esc(text) + '</div>';
  }
  function showError(el, text) {
    el.innerHTML = '<div class="d-empty">' + esc(text) +
      '<div class="d-empty-btn"><button class="btn btn-sm" data-retry="1">重试</button></div></div>';
  }

  /* ---------------- 缓存 ---------------- */
  var cache = { heroes: {}, teams: {}, players: {}, bp: {}, meta: {} };

  /* ---------------- DOM ---------------- */
  var el = {
    leagueTabs: document.getElementById('leagueTabs'),
    overview: document.getElementById('leagueOverview'),
    subTabs: document.getElementById('subTabs'),
    heroList: document.getElementById('heroList'),
    heroCount: document.getElementById('heroCount'),
    heroLaneFilters: document.getElementById('heroLaneFilters'),
    teamWrap: document.getElementById('teamTableWrap'),
    teamCount: document.getElementById('teamCount'),
    playerWrap: document.getElementById('playerTableWrap'),
    playerCount: document.getElementById('playerCount'),
    posFilters: document.getElementById('playerPosFilters'),
    sortWrap: document.getElementById('playerSortWrap'),
    bpList: document.getElementById('bpList'),
    bpCount: document.getElementById('bpCount'),
    modal: document.getElementById('heroModal'),
    modalAvatar: document.getElementById('heroModalAvatar'),
    modalName: document.getElementById('heroModalName'),
    modalTitle: document.getElementById('heroModalTitle'),
    modalStats: document.getElementById('heroModalStats'),
    modalLanes: document.getElementById('heroModalLanes'),
    modalNote: document.getElementById('heroModalNote'),
    modalClose: document.getElementById('heroModalClose')
  };

  // v43：官方对位克制数据（王者荣耀官网英雄详情页「英雄关系」），全局只拉一次
  var RELATIONS = { loaded: false, map: {} };
  function loadRelations() {
    if (RELATIONS.loaded) return Promise.resolve(RELATIONS.map);
    return fetch('/api/hero_relations', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        (res && res.heroes || []).forEach(function (row) {
          RELATIONS.map[row.ename] = row;
        });
        RELATIONS.loaded = true;
        return RELATIONS.map;
      })
      .catch(function () { RELATIONS.loaded = true; return RELATIONS.map; });
  }

  if (!el.leagueTabs) return; // 不在数据页

  var state = {
    leagues: [],
    leagueId: null,
    meta: null,
    tab: 'heroes',
    heroMap: {},          // ename -> hero（当前赛季英雄表）
    heroLaneFilter: '全部',
    players: [],
    posFilter: '全部',
    playerSort: { key: 'win_rate', dir: 'desc' },
    bpOpen: {}
  };

  /* ============================================================
     一级：赛季 Tab
     ============================================================ */
  function renderLeagueTabs() {
    var frag = document.createDocumentFragment();
    state.leagues.forEach(function (lg) {
      var id = String(lg.league_id);
      var b = document.createElement('button');
      b.className = 'league-chip' + (id === state.leagueId ? ' is-active' : '') +
        (lg.status === 'upcoming' ? ' is-upcoming' : '');
      b.type = 'button';
      b.setAttribute('data-league-id', id);
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', id === state.leagueId ? 'true' : 'false');
      b.innerHTML = (lg.status === 'ongoing' ? '<i class="dot"></i>' : '') + esc(shortLeagueName(lg.name));
      b.title = lg.name || '';
      frag.appendChild(b);
    });
    el.leagueTabs.innerHTML = '';
    el.leagueTabs.appendChild(frag);
  }

  el.leagueTabs.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.league-chip') : null;
    if (!btn) return;
    var id = btn.getAttribute('data-league-id');
    if (!id || id === state.leagueId) return;
    switchLeague(id);
  });

  function switchLeague(id) {
    state.leagueId = String(id);
    setLeagueId(state.leagueId);
    state.meta = null;
    state.heroMap = {};
    state.heroAll = [];
    state.heroLaneFilter = '全部';   // v2：分路筛选随赛季重置，避免沿用上一赛季的分路
    state.players = [];
    state.posFilter = '全部';
    state.bpOpen = {};
    renderLeagueTabs();
    loadMeta();
    // v2 §4.3：切赛季必须重新拉 /api/heroes，lane_list 随赛季变化
    loadTab(state.tab, true);
  }

  /* ============================================================
     赛季概览
     ============================================================ */
  function renderOverview(meta, lg) {
    var counts = (meta && meta.counts) || {};
    var name = (meta && meta.league_name) || (lg && lg.name) || '—';
    var start = meta && meta.start ? meta.start : (lg && lg.start) || null;
    var end = meta && meta.end ? meta.end : (lg && lg.end) || null;
    var dates = (start || end) ? (start || '—') + ' ~ ' + (end || '—') : '赛季时间未收录';

    var stepCount = meta && isNum(meta.real_bp_step_count) ? String(meta.real_bp_step_count) : '—';
    var bpCount = isNum(counts.bp_matches) ? String(counts.bp_matches) : '—';

    el.overview.innerHTML =
      '<div class="ov-name">' + esc(name) + '</div>' +
      '<div class="ov-dates">' + esc(dates) + '</div>' +
      '<div class="ov-grid">' +
        '<div class="ov-cell"><span class="ov-k">真实对局</span><span class="ov-v">' + esc(bpCount) + '</span></div>' +
        '<div class="ov-cell"><span class="ov-k">英雄</span><span class="ov-v">' + esc(isNum(counts.heroes) ? String(counts.heroes) : '—') + '</span></div>' +
        '<div class="ov-cell"><span class="ov-k">战队</span><span class="ov-v">' + esc(isNum(counts.teams) ? String(counts.teams) : '—') + '</span></div>' +
        '<div class="ov-cell"><span class="ov-k">选手</span><span class="ov-v">' + esc(isNum(counts.players) ? String(counts.players) : '—') + '</span></div>' +
        '<div class="ov-cell"><span class="ov-k">BP 步数</span><span class="ov-v">' + esc(stepCount) + '</span></div>' +
      '</div>' +
      '<div class="ov-note">' +
        (isNum(counts.heroes_with_stats) ? '有统计英雄 ' + counts.heroes_with_stats + ' 名 · ' : '') +
        '全部数值来自腾讯 smoba 官方联赛公开数据，缺失项显示 —。' +
      '</div>';
  }

  function renderOverviewLoading() {
    el.overview.innerHTML =
      '<div class="ov-name">加载中…</div><div class="ov-dates">—</div>' +
      '<div class="ov-grid">' +
      '<div class="ov-cell"><span class="ov-k">真实对局</span><span class="ov-v">—</span></div>' +
      '<div class="ov-cell"><span class="ov-k">英雄</span><span class="ov-v">—</span></div>' +
      '<div class="ov-cell"><span class="ov-k">战队</span><span class="ov-v">—</span></div>' +
      '<div class="ov-cell"><span class="ov-k">选手</span><span class="ov-v">—</span></div>' +
      '<div class="ov-cell"><span class="ov-k">BP 步数</span><span class="ov-v">—</span></div>' +
      '</div>';
  }

  function currentLeague() {
    for (var i = 0; i < state.leagues.length; i++) {
      if (String(state.leagues[i].league_id) === String(state.leagueId)) return state.leagues[i];
    }
    return null;
  }

  function loadMeta() {
    var id = state.leagueId;
    renderOverviewLoading();
    if (!id) { renderOverview(null, null); return Promise.resolve(); }
    if (cache.meta[id]) { state.meta = cache.meta[id]; renderOverview(state.meta, currentLeague()); return Promise.resolve(); }
    return call('leagueMeta', id).then(function (d) {
      if (id !== state.leagueId) return;
      var meta = {};
      if (d && typeof d === 'object') {
        // 兼容 {ok, counts...} 与 {ok, meta:{...}} 两种包装
        meta = d.meta ? d.meta : d;
      }
      cache.meta[id] = meta;
      state.meta = meta;
      renderOverview(meta, currentLeague());
    }).catch(function (err) {
      if (id !== state.leagueId) return;
      renderOverview(null, currentLeague());
      toast('赛季概览加载失败：' + (err && err.message ? err.message : err));
    });
  }

  /* ============================================================
     二级 Tab
     ============================================================ */
  el.subTabs.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.sub-tab') : null;
    if (!btn) return;
    var tab = btn.getAttribute('data-tab');
    if (!tab || tab === state.tab) return;
    setTab(tab);
  });

  function setTab(tab) {
    state.tab = tab;
    Array.prototype.forEach.call(el.subTabs.querySelectorAll('.sub-tab'), function (b) {
      var on = b.getAttribute('data-tab') === tab;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    ['heroes', 'teams', 'players', 'bp'].forEach(function (t) {
      var p = document.getElementById('panel-' + t);
      if (p) p.classList.toggle('is-active', t === tab);
    });
    loadTab(tab, false);
  }

  /* 没有可用赛季（列表失败 / 空列表）时，四个面板必须脱离骨架屏并给出明确提示，
     否则页面会永远停在「加载中…」的骨架屏上。重试入口统一在赛季 Tab 区域。 */
  function failAllPanels(text) {
    var msg = text || '暂无赛季数据';
    el.heroCount.textContent = '—';
    showEmpty(el.heroList, msg);
    el.teamCount.textContent = '—';
    showEmpty(el.teamWrap, msg);
    el.playerCount.textContent = '—';
    showEmpty(el.playerWrap, msg);
    el.bpCount.textContent = '—';
    showEmpty(el.bpList, msg);
  }

  function loadTab(tab, force) {
    if (!state.leagueId) return;
    if (tab === 'heroes') loadHeroes(force);
    else if (tab === 'teams') loadTeams(force);
    else if (tab === 'players') loadPlayers(force);
    else if (tab === 'bp') loadBP(force);
  }

  /* ---------------- 英雄 ---------------- */
  function sortHeroes(list) {
    return list.slice().sort(function (a, b) {
      var sa = a.has_stats ? 0 : 1, sb = b.has_stats ? 0 : 1;
      if (sa !== sb) return sa - sb;
      if (!a.has_stats) { // 都没数据：按 ename 升序
        return (Number(a.ename) || 0) - (Number(b.ename) || 0);
      }
      var wa = isNum(a.win_rate) ? a.win_rate : -Infinity;
      var wb = isNum(b.win_rate) ? b.win_rate : -Infinity;
      if (wa !== wb) return wb - wa;
      var pa = isNum(a.pick_rate) ? a.pick_rate : -Infinity;
      var pb = isNum(b.pick_rate) ? b.pick_rate : -Infinity;
      if (pa !== pb) return pb - pa;
      return (Number(a.ename) || 0) - (Number(b.ename) || 0);
    });
  }

  function heroRowHtml(h) {
    var stats = h.has_stats
      ? '<span class="hs"><i>出场</i><b>' + esc(int(h.pick_count)) + '</b></span>' +
        '<span class="hs"><i>出场率</i><b>' + esc(pct(h.pick_rate)) + '</b></span>' +
        '<span class="hs"><i>Ban率</i><b>' + esc(pct(h.ban_rate)) + '</b></span>' +
        '<span class="hs"><i>胜率</i><b class="' + winRateClass(h.win_rate) + '">' + esc(pct(h.win_rate)) + '</b></span>'
      : '<span class="hs"><i>本赛季无出场记录</i></span>';

    var lanes = laneList(h);
    var dataLanes = lanes.length ? ' data-lanes="' + esc(lanes.join(',')) + '"' : '';

    return '<button type="button" class="hero-row' + (h.has_stats ? '' : ' no-stats') +
      '" data-ename="' + esc(h.ename) + '" data-label="' + esc(h.cname) + '"' + dataLanes + '>' +
      '<span class="hero-avatar">' + avatarHtml(h.avatar_path, h.avatar_ok, h.cname, 'avt-md') + '</span>' +
      '<span class="hero-body">' +
        '<span class="hero-top">' +
          '<span class="hero-name">' + esc(h.cname || '—') + '</span>' +
          laneChipsHtml(lanes, 2) +
        '</span>' +
        '<span class="hero-stats">' + stats + '</span>' +
      '</span>' +
      '<span class="hero-chev">›</span>' +
      '</button>';
  }

  function loadHeroes(force) {
    var id = state.leagueId;
    if (!id) return Promise.resolve();

    if (!force && cache.heroes[id]) { renderHeroes(cache.heroes[id]); return Promise.resolve(); }

    el.heroList.innerHTML =
      '<div class="skel-card"><span class="skel skel-circle"></span><div class="skel-lines">' +
      '<div class="skel" style="width:45%"></div><div class="skel" style="width:70%"></div></div></div>' +
      '<div class="skel-card"><span class="skel skel-circle"></span><div class="skel-lines">' +
      '<div class="skel" style="width:38%"></div><div class="skel" style="width:62%"></div></div></div>';
    el.heroCount.textContent = '加载中…';

    return call('heroes', id).then(function (d) {
      if (id !== state.leagueId) return;
      var list = (d && d.heroes) || [];
      var sorted = sortHeroes(list);
      cache.heroes[id] = sorted;
      renderHeroes(sorted);
    }).catch(function (err) {
      if (id !== state.leagueId) return;
      el.heroCount.textContent = '—';
      showError(el.heroList, '英雄数据加载失败：' + (err && err.message ? err.message : err));
      toast('英雄数据加载失败');
    });
  }

  function renderHeroes(list) {
    state.heroAll = list;
    if (!list.length) {
      el.heroCount.textContent = '0 名';
      showEmpty(el.heroList, '该赛季暂无英雄数据');
      if (el.heroLaneFilters) el.heroLaneFilters.innerHTML = '';
      return;
    }

    // v2 §4.2：按 lane_list 是否包含该分路筛选（不用 lane 单值、不用 role）
    var filtered = list;
    if (state.heroLaneFilter && state.heroLaneFilter !== '全部') {
      var want = state.heroLaneFilter;
      filtered = list.filter(function (h) { return laneList(h).indexOf(want) >= 0; });
    }

    renderHeroLaneFilters();

    if (!filtered.length) {
      el.heroCount.textContent = '0 名';
      showEmpty(el.heroList, '该赛季「' + state.heroLaneFilter + '」暂无英雄数据');
      return;
    }

    var map = {};
    var html = [];
    for (var i = 0; i < filtered.length; i++) {
      map[String(filtered[i].ename)] = filtered[i];
      html.push(heroRowHtml(filtered[i]));
    }
    state.heroMap = map;
    // 一次性插入，避免逐条 appendChild 到 live DOM
    el.heroList.innerHTML = html.join('');

    var withStats = 0, withLane = 0;
    for (var j = 0; j < filtered.length; j++) {
      if (filtered[j].has_stats) withStats++;
      if (laneList(filtered[j]).length) withLane++;
    }
    var txt = '共 ' + filtered.length + ' 名 · 有统计 ' + withStats + ' 名';
    if (state.heroLaneFilter !== '全部') txt = state.heroLaneFilter + '：' + txt;
    el.heroCount.textContent = txt;
  }

  function renderHeroLaneFilters() {
    if (!el.heroLaneFilters) return;
    var opts = ['全部'].concat(LANES);
    var frag = document.createDocumentFragment();
    opts.forEach(function (p) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'f-chip' + (p === state.heroLaneFilter ? ' is-active' : '');
      b.setAttribute('data-lane', p);
      b.textContent = p;
      frag.appendChild(b);
    });
    el.heroLaneFilters.innerHTML = '';
    el.heroLaneFilters.appendChild(frag);
  }

  if (el.heroLaneFilters) {
    el.heroLaneFilters.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.f-chip') : null;
      if (!btn) return;
      var p = btn.getAttribute('data-lane');
      if (!p || p === state.heroLaneFilter) return;
      state.heroLaneFilter = p;
      renderHeroes(state.heroAll || []);
    });
  }

  el.heroList.addEventListener('click', function (e) {
    var row = e.target.closest ? e.target.closest('.hero-row') : null;
    if (!row) return;
    var ename = row.getAttribute('data-ename');
    var h = state.heroMap[String(ename)];
    if (h) openHeroModal(h);
  });

  el.heroList.addEventListener('click', function (e) {
    var r = e.target.closest ? e.target.closest('[data-retry]') : null;
    if (r) loadHeroes(true);
  });

  /* ---------------- 英雄详情弹层 ---------------- */
  function statCell(k, v, accent) {
    return '<div class="stat-cell' + (accent ? ' accent' : '') + '">' +
      '<span class="sc-k">' + esc(k) + '</span>' +
      '<span class="sc-v">' + esc(v) + '</span></div>';
  }

  function openHeroModal(h) {
    var name = h.cname || '—';
    var lanes = laneList(h);
    var games = laneGames(h);
    // 容器本身是头像壳（含 #heroModalAvatar），只换内部内容
    el.modalAvatar.className = 'avt avt-xl' + (h.avatar_path ? '' : ' avt-fallback');
    el.modalAvatar.innerHTML = avatarInner(h.avatar_path, name);

    el.modalName.textContent = name;
    el.modalTitle.textContent = (h.title || '') + (lanes.length ? (h.title ? ' · ' : '') + lanes[0] : '') || '—';

    // 后端可能在 heroes 里附带更多字段；有则显示，无则 —（绝不编造）
    var winCount = isNum(h.win_count) ? h.win_count : (isNum(h.wins) ? h.wins : null);
    var avgK = isNum(h.avg_kill) ? h.avg_kill : null;
    var avgD = isNum(h.avg_death) ? h.avg_death : null;
    var avgA = isNum(h.avg_assist) ? h.avg_assist : null;

    var cells = [
      statCell('出场次数', h.has_stats ? int(h.pick_count) : '—', true),
      statCell('胜场', winCount == null ? '—' : int(winCount)),
      statCell('胜率', h.has_stats ? pct(h.win_rate) : '—', true),
      statCell('Ban 率', h.has_stats ? pct(h.ban_rate) : '—'),
      statCell('出场率', h.has_stats ? pct(h.pick_rate) : '—'),
      statCell('KDA', h.has_stats ? num(h.kda) : '—'),
      statCell('场均击杀', avgK == null ? '—' : num(avgK)),
      statCell('场均死亡', avgD == null ? '—' : num(avgD)),
      statCell('场均助攻', avgA == null ? '—' : num(avgA)),
      statCell('Ban 次数', h.has_stats ? int(h.ban_count) : '—')
    ];
    el.modalStats.innerHTML = cells.join('');

    // v2：该赛季分路（lane_list + lane_games），无数据也必须给明确文案
    if (el.modalLanes) {
      if (!lanes.length) {
        el.modalLanes.innerHTML = '<div class="ml-k">该赛季分路</div>' +
          '<div class="lane-none">' + esc(LANE_NONE) + '</div>';
      } else {
        el.modalLanes.innerHTML = '<div class="ml-k">该赛季分路</div>' +
          '<div class="lane-chips">' +
          lanes.map(function (l) { return '<span class="lane-chip">' + esc(l) + '</span>'; }).join('') +
          '</div>' +
          '<div class="ml-games">' + laneGamesText(lanes, games) + '</div>';
      }
    }

    // v43：官方分路（KPL 无该赛季数据时的兜底依据）+ 官方克制关系
    var officialLanes = Array.isArray(h.lane_official) ? h.lane_official : [];
    if (el.modalLanes && !lanes.length && officialLanes.length) {
      el.modalLanes.innerHTML = '<div class="ml-k">该赛季分路</div>' +
        '<div class="lane-none">该赛季没有出场记录</div>' +
        '<div class="ml-k" style="margin-top:8px">官方分路（王者荣耀官网）</div>' +
        '<div class="lane-chips">' +
        officialLanes.map(function (l) { return '<span class="lane-chip">' + esc(l) + '</span>'; }).join('') +
        '</div>';
    }
    if (el.modalLanes) {
      loadRelations().then(function (map) {
        var row = map[h.ename];
        if (!row || !el.modalLanes) return;
        var nameOf = function (en) {
          var item = state.heroMap[en];
          return item ? item.cname : null;
        };
        var chipOf = function (name) {
          return '<span class="lane-chip">' + esc(name) + '</span>';
        };
        var mine = (row.counters || []).map(nameOf).filter(Boolean);
        var theirs = (row.countered_by || []).map(nameOf).filter(Boolean);
        if (!mine.length && !theirs.length) return;
        var html = '<div class="ml-k" style="margin-top:8px">官方克制关系</div>' +
          '<div class="ml-rel">' +
          (mine.length ? '<div><span class="ml-rel-k">压制</span>' + mine.map(chipOf).join('') + '</div>' : '') +
          (theirs.length ? '<div><span class="ml-rel-k">被压制</span>' + theirs.map(chipOf).join('') + '</div>' : '') +
          '</div>' +
          '<div class="ml-games">数据来自王者荣耀官网英雄详情页「英雄关系」</div>';
        el.modalLanes.insertAdjacentHTML('beforeend', html);
      });
    }

    var lg = currentLeague();
    el.modalNote.textContent = (lg ? shortLeagueName(lg.name) + ' · ' : '') +
      (h.has_stats
        ? '数据来自该赛季真实联赛统计。'
        : '该赛季暂无出场统计。') + ' 缺失字段显示 —。';

    el.modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function closeHeroModal() {
    el.modal.hidden = true;
    document.body.style.overflow = '';
  }

  if (el.modalClose) el.modalClose.addEventListener('click', closeHeroModal);
  if (el.modal) {
    el.modal.addEventListener('click', function (e) {
      if (e.target === el.modal) closeHeroModal();
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && el.modal && !el.modal.hidden) closeHeroModal();
  });

  /* ---------------- 战队 ---------------- */
  var TEAM_COLS = [
    { key: 'rank', label: '名次', sortable: false },
    { key: 'name', label: '战队', sortable: false },
    { key: 'matches', label: '场次', sortable: true },
    { key: 'wins', label: '胜', sortable: true },
    { key: 'losses', label: '负', sortable: true },
    { key: 'game_diff', label: '净胜局', sortable: true },
    { key: 'win_rate', label: '胜率', sortable: true },
    { key: 'blue_win_rate', label: '蓝方胜率', sortable: true },
    { key: 'red_win_rate', label: '红方胜率', sortable: true },
    { key: 'avg_kda', label: '场均 KDA', sortable: true }
  ];

  function loadTeams(force) {
    var id = state.leagueId;
    if (!id) return Promise.resolve();
    if (!force && cache.teams[id]) { renderTeams(cache.teams[id]); return Promise.resolve(); }

    el.teamWrap.innerHTML = '<div class="d-loading"><span class="spinner"></span><span>加载中…</span></div>';
    el.teamCount.textContent = '加载中…';

    return call('teams', id).then(function (d) {
      if (id !== state.leagueId) return;
      var list = ((d && d.teams) || []).slice().sort(function (a, b) {
        var ra = isNum(a.rank) ? a.rank : 999, rb = isNum(b.rank) ? b.rank : 999;
        if (ra !== rb) return ra - rb;
        var wa = isNum(a.win_rate) ? a.win_rate : -Infinity;
        var wb = isNum(b.win_rate) ? b.win_rate : -Infinity;
        return wb - wa;
      });
      cache.teams[id] = list;
      renderTeams(list);
    }).catch(function (err) {
      if (id !== state.leagueId) return;
      el.teamCount.textContent = '—';
      showError(el.teamWrap, '战队数据加载失败：' + (err && err.message ? err.message : err));
      toast('战队数据加载失败');
    });
  }

  function renderTeams(list) {
    if (!list.length) {
      el.teamCount.textContent = '0 支';
      showEmpty(el.teamWrap, '该赛季暂无战队数据');
      return;
    }
    el.teamCount.textContent = '共 ' + list.length + ' 支战队';

    var head = '<tr><th>名次</th><th>战队</th><th class="num">场次</th><th class="num">胜</th>' +
      '<th class="num">负</th><th class="num">净胜局</th><th class="num">胜率</th>' +
      '<th class="num">蓝方胜率</th><th class="num">红方胜率</th><th class="num">场均 KDA</th></tr>';

    var rows = ['<table class="d-table" role="table">', '<thead>' + head + '</thead>', '<tbody>'];
    list.forEach(function (t, i) {
      var rank = isNum(t.rank) ? t.rank : i + 1;
      rows.push(
        '<tr>' +
        '<td class="td-lead" data-label=""><span class="td-rank">' + esc(String(rank)) + '</span></td>' +
        '<td class="td-lead" data-label=""><span class="td-team">' +
          logoHtml(t.logo_path, t.logo_ok, t.abbreviation || t.name, 'lg-md') +
          '<span class="td-team-name">' + esc(t.name || '—') + '</span>' +
        '</span></td>' +
        '<td class="num" data-label="场次">' + esc(int(t.matches)) + '</td>' +
        '<td class="num" data-label="胜">' + esc(int(t.wins)) + '</td>' +
        '<td class="num" data-label="负">' + esc(int(t.losses)) + '</td>' +
        '<td class="num" data-label="净胜局">' + esc(t.game_diff == null ? '—' : (t.game_diff > 0 ? '+' : '') + int(t.game_diff)) + '</td>' +
        '<td class="num" data-label="胜率"><b class="' + winRateClass(t.win_rate) + '">' + esc(pct(t.win_rate)) + '</b></td>' +
        '<td class="num" data-label="蓝方胜率">' + esc(pct(t.blue_win_rate)) + '</td>' +
        '<td class="num" data-label="红方胜率">' + esc(pct(t.red_win_rate)) + '</td>' +
        '<td class="num" data-label="场均KDA">' + esc(num(t.avg_kda)) + '</td>' +
        '</tr>'
      );
    });
    rows.push('</tbody></table>');
    el.teamWrap.innerHTML = rows.join('');
  }

  /* ---------------- 选手 ---------------- */
  var POSITIONS = ['全部', '对抗路', '打野', '中路', '发育路', '游走'];

  var PLAYER_SORTS = {
    win_rate: '胜率',
    kda: 'KDA',
    matches: '场次',
    gpm: '分均经济',
    damage_share: '伤害占比',
    participation_rate: '参团率'
  };

  function renderPosFilters() {
    var frag = document.createDocumentFragment();
    POSITIONS.forEach(function (p) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'f-chip' + (p === state.posFilter ? ' is-active' : '');
      b.setAttribute('data-pos', p);
      b.textContent = p;
      frag.appendChild(b);
    });
    el.posFilters.innerHTML = '';
    el.posFilters.appendChild(frag);

    // 移动端排序（表头在移动端被隐藏，故提供下拉）；放在工具栏右侧，不塞进横向滚动的 chip 行
    var sel = document.createElement('select');
    sel.className = 'sort-select';
    sel.id = 'playerSortSelect';
    sel.setAttribute('aria-label', '排序方式');
    Object.keys(PLAYER_SORTS).forEach(function (k) {
      var o = document.createElement('option');
      o.value = k;
      o.textContent = '按' + PLAYER_SORTS[k];
      if (k === state.playerSort.key) o.selected = true;
      sel.appendChild(o);
    });
    var o1 = document.createElement('option');
    o1.value = '__dir';
    o1.textContent = state.playerSort.dir === 'desc' ? '↓ 降序' : '↑ 升序';
    sel.appendChild(o1);
    var host = el.sortWrap || el.posFilters;
    host.innerHTML = '';
    host.appendChild(sel);
  }

  function onPlayerSortChange(e) {
    var sel = e.target;
    if (!sel || sel.tagName !== 'SELECT') return;
    if (sel.value === '__dir') {
      state.playerSort.dir = state.playerSort.dir === 'desc' ? 'asc' : 'desc';
    } else if (PLAYER_SORTS[sel.value]) {
      state.playerSort.key = sel.value;
      state.playerSort.dir = 'desc';
    }
    renderPosFilters();
    renderPlayers();
  }

  el.posFilters.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.f-chip') : null;
    if (!btn) return;
    var p = btn.getAttribute('data-pos');
    if (!p || p === state.posFilter) return;
    state.posFilter = p;
    renderPosFilters();
    renderPlayers();
  });

  el.posFilters.addEventListener('change', onPlayerSortChange);
  if (el.sortWrap) el.sortWrap.addEventListener('change', onPlayerSortChange);

  function loadPlayers(force) {
    var id = state.leagueId;
    if (!id) return Promise.resolve();
    if (!force && cache.players[id]) { state.players = cache.players[id]; renderPlayers(); return Promise.resolve(); }

    el.playerWrap.innerHTML = '<div class="d-loading"><span class="spinner"></span><span>加载中…</span></div>';
    el.playerCount.textContent = '加载中…';
    if (!el.posFilters.childElementCount) renderPosFilters();

    return call('players', id).then(function (d) {
      if (id !== state.leagueId) return;
      var list = (d && d.players) || [];
      cache.players[id] = list;
      state.players = list;
      renderPlayers();
    }).catch(function (err) {
      if (id !== state.leagueId) return;
      el.playerCount.textContent = '—';
      showError(el.playerWrap, '选手数据加载失败：' + (err && err.message ? err.message : err));
      toast('选手数据加载失败');
    });
  }

  function playerTable(list) {
    var s = state.playerSort;
    function th(key, label, extraCls) {
      var sorted = s.key === key;
      var ind = sorted ? (s.dir === 'desc' ? '▼' : '▲') : '⇅';
      return '<th class="' + (extraCls || 'num') + '">' +
        '<button type="button" class="th-sort' + (sorted ? ' is-sorted' : '') + '" data-sort="' + key + '">' +
        esc(label) + '<span class="sort-ind">' + ind + '</span></button></th>';
    }

    var head = '<tr>' + th('name', '选手', '') + '<th>战队</th><th>位置</th>' +
      th('matches', '场次') + th('win_rate', '胜率') + th('kda', 'KDA') +
      th('gpm', '分均经济') + th('damage_share', '伤害占比') + th('participation_rate', '参团率') + '</tr>';

    var rows = ['<table class="d-table" role="table">', '<thead>' + head + '</thead>', '<tbody>'];
    list.forEach(function (p) {
      rows.push(
        '<tr>' +
        '<td class="td-lead" data-label=""><span class="td-player">' + esc(p.name || '—') + '</span></td>' +
        '<td data-label="战队">' + esc(p.team || '—') + '</td>' +
        '<td data-label="位置">' + esc(p.position || '—') + '</td>' +
        '<td class="num" data-label="场次">' + esc(int(p.matches)) + '</td>' +
        '<td class="num" data-label="胜率"><b class="' + winRateClass(p.win_rate) + '">' + esc(pct(p.win_rate)) + '</b></td>' +
        '<td class="num" data-label="KDA">' + esc(num(p.kda)) + '</td>' +
        '<td class="num" data-label="分均经济">' + esc(num(p.gpm)) + '</td>' +
        '<td class="num" data-label="伤害占比">' + esc(pct(p.damage_share)) + '</td>' +
        '<td class="num" data-label="参团率">' + esc(pct(p.participation_rate)) + '</td>' +
        '</tr>'
      );
    });
    rows.push('</tbody></table>');
    return rows.join('');
  }

  function renderPlayers() {
    var all = state.players || [];
    if (!all.length) {
      el.playerCount.textContent = '0 人';
      showEmpty(el.playerWrap, '该赛季暂无选手数据');
      return;
    }
    var filtered = state.posFilter === '全部'
      ? all.slice()
      : all.filter(function (p) { return p.position === state.posFilter; });

    var s = state.playerSort;
    filtered.sort(function (a, b) {
      var va = a[s.key], vb = b[s.key];
      var na = isNum(va) ? va : -Infinity;
      var nb = isNum(vb) ? vb : -Infinity;
      if (na !== nb) return s.dir === 'desc' ? nb - na : na - nb;
      // 同值时：胜率降序 → 场次降序 兜底
      var wa = isNum(a.win_rate) ? a.win_rate : -Infinity;
      var wb = isNum(b.win_rate) ? b.win_rate : -Infinity;
      if (wa !== wb) return wb - wa;
      var ma = isNum(a.matches) ? a.matches : -Infinity;
      var mb = isNum(b.matches) ? b.matches : -Infinity;
      return mb - ma;
    });

    el.playerCount.textContent = '共 ' + filtered.length + ' 人' +
      (state.posFilter !== '全部' ? '（' + state.posFilter + '）' : '');

    if (!filtered.length) {
      showEmpty(el.playerWrap, '该赛季「' + state.posFilter + '」暂无选手数据');
      return;
    }
    el.playerWrap.innerHTML = playerTable(filtered);
  }

  el.playerWrap.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.th-sort') : null;
    if (!btn) { // 重试
      var r = e.target.closest ? e.target.closest('[data-retry]') : null;
      if (r) loadPlayers(true);
      return;
    }
    var key = btn.getAttribute('data-sort');
    if (!key) return;
    if (state.playerSort.key === key) {
      state.playerSort.dir = state.playerSort.dir === 'desc' ? 'asc' : 'desc';
    } else {
      state.playerSort.key = key;
      state.playerSort.dir = 'desc';
    }
    renderPosFilters();
    renderPlayers();
  });

  /* ---------------- BP 复盘 ---------------- */
  function loadBP(force) {
    var id = state.leagueId;
    if (!id) return Promise.resolve();
    if (!force && cache.bp[id]) { renderBP(cache.bp[id]); return Promise.resolve(); }

    el.bpList.innerHTML = '<div class="d-loading"><span class="spinner"></span><span>加载中…</span></div>';
    el.bpCount.textContent = '加载中…';

    return call('bpHistory', id, 30).then(function (d) {
      if (id !== state.leagueId) return;
      var data = { total: (d && d.total) || 0, matches: (d && d.matches) || [] };
      cache.bp[id] = data;
      renderBP(data);
    }).catch(function (err) {
      if (id !== state.leagueId) return;
      el.bpCount.textContent = '—';
      showError(el.bpList, 'BP 复盘数据加载失败：' + (err && err.message ? err.message : err));
      toast('BP 复盘加载失败');
    });
  }

  function renderBP(data) {
    var matches = data.matches || [];
    if (!matches.length) {
      el.bpCount.textContent = '0 场';
      showEmpty(el.bpList, '该赛季暂未收录 BP 对局数据');
      return;
    }
    el.bpCount.textContent = '展示 ' + matches.length + ' 场' +
      (isNum(data.total) ? ' / 共 ' + data.total + ' 场' : '');

    var frag = document.createDocumentFragment();
    matches.forEach(function (m, idx) {
      frag.appendChild(bpItemEl(m, idx));
    });
    el.bpList.innerHTML = '';
    el.bpList.appendChild(frag);
  }

  function bpItemEl(m, idx) {
    var key = String(m.battle_id || m.match_id || idx);
    var open = !!state.bpOpen[key];
    var winnerLabel = m.winner === 'blue' ? '蓝方胜' : (m.winner === 'red' ? '红方胜' : '—');

    var wrap = document.createElement('div');
    wrap.className = 'bp-item' + (open ? ' is-open' : '');
    wrap.setAttribute('data-bp-key', key);

    var stepCount = isNum(m.step_count) ? m.step_count
      : (Array.isArray(m.sequence) ? m.sequence.length : null);

    var tags = [];
    if (m.winner === 'blue' || m.winner === 'red') {
      tags.push('<span class="bp-tag win-' + m.winner + '">' + winnerLabel + '</span>');
    }
    if (isNum(m.bo)) tags.push('<span class="bp-tag">BO' + m.bo + '</span>');
    if (m.stage) tags.push('<span class="bp-tag">' + esc(m.stage) + '</span>');
    if (stepCount != null) tags.push('<span class="bp-tag">' + stepCount + ' 手</span>');

    var headHtml =
      '<button type="button" class="bp-head" aria-expanded="' + (open ? 'true' : 'false') + '">' +
        '<span class="bp-teams">' +
          '<span class="bp-team blue" title="' + esc(m.blue_team || '') + '">' + esc(m.blue_team || '蓝方') + '</span>' +
          '<span class="bp-vs">VS</span>' +
          '<span class="bp-team red" title="' + esc(m.red_team || '') + '">' + esc(m.red_team || '红方') + '</span>' +
        '</span>' +
        '<span class="bp-meta">' + tags.join('') + '</span>' +
        '<span class="bp-arrow">▾</span>' +
      '</button>';

    wrap.innerHTML = headHtml + '<div class="bp-body"' + (open ? '' : ' hidden') + '></div>';
    if (open) fillBpBody(wrap.querySelector('.bp-body'), m);
    return wrap;
  }

  function seqListHtml(seq, sideClass, sideName) {
    var items = [];
    if (!Array.isArray(seq) || !seq.length) {
      return '<div class="bp-col ' + sideClass + '"><div class="bp-col-title">' +
        '<span class="side-dot"></span>' + esc(sideName) + '</div>' +
        '<div class="bp-summary">暂无序列数据</div></div>';
    }
    seq.forEach(function (s) {
      var act = s.action === 'ban' ? 'BAN' : (s.action === 'pick' ? 'PICK' : '—');
      var actCls = s.action === 'ban' ? 'act-ban' : (s.action === 'pick' ? 'act-pick' : '');
      var heroName = s.hero || s.cname || '';
      var h = state.heroMap[String(s.ename)] || null;
      var avatar = h ? avatarHtml(h.avatar_path, h.avatar_ok, heroName, 'avt-xs')
        : avatarHtml(null, null, heroName, 'avt-xs');
      items.push(
        '<li class="seq">' +
          '<span class="seq-no">' + (isNum(s.step) ? '第' + s.step + '手' : '·') + '</span>' +
          '<span class="seq-act ' + actCls + '">' + act + '</span>' +
          '<span class="seq-hero">' + avatar +
            '<span class="seq-name">' + esc(heroName || '—') + '</span></span>' +
        '</li>'
      );
    });
    return '<div class="bp-col ' + sideClass + '">' +
      '<div class="bp-col-title"><span class="side-dot"></span>' + esc(sideName) + '</div>' +
      '<ul class="bp-col-list">' + items.join('') + '</ul></div>';
  }

  function fillBpBody(body, m) {
    if (!body || body.getAttribute('data-filled') === '1') return;
    body.setAttribute('data-filled', '1');

    var seq = Array.isArray(m.sequence) ? m.sequence : [];
    var blueSeq, redSeq, note = '';

    if (seq.length) {
      blueSeq = seq.filter(function (s) { return s.side === 'blue' || s.camp === 1; });
      redSeq = seq.filter(function (s) { return s.side === 'red' || s.camp === 2; });
    } else {
      // 契约兜底：没有 sequence 时用 picks/bans 平铺（不编造步数）
      blueSeq = [];
      redSeq = [];
      note = '该场未返回 BP 序列，以下为双方 picks/bans 摘要。';
    }

    var html = '<div class="bp-timeline">' +
      seqListHtml(seq.length ? blueSeq : [], 'blue', m.blue_team || '蓝方') +
      seqListHtml(seq.length ? redSeq : [], 'red', m.red_team || '红方') +
      '</div>';

    if (!seq.length) {
      var bp = (m.blue_picks || []).join('、') || '—';
      var rp = (m.red_picks || []).join('、') || '—';
      var bb = (m.blue_bans || []).join('、') || '—';
      var rb = (m.red_bans || []).join('、') || '—';
      html += '<div class="bp-summary">' + esc(note) +
        '<br><b>蓝方 Pick</b>：' + esc(bp) +
        '<br><b>红方 Pick</b>：' + esc(rp) +
        '<br><b>蓝方 Ban</b>：' + esc(bb) +
        '<br><b>红方 Ban</b>：' + esc(rb) + '</div>';
    } else {
      var bluePicks = blueSeq.filter(function (s) { return s.action === 'pick'; }).length;
      var redPicks = redSeq.filter(function (s) { return s.action === 'pick'; }).length;
      html += '<div class="bp-summary">共 ' + seq.length + ' 手 · 蓝方 pick ' + bluePicks +
        ' / 红方 pick ' + redPicks + (m.stage ? ' · ' + esc(m.stage) : '') + '</div>';
    }

    body.innerHTML = html;
  }

  el.bpList.addEventListener('click', function (e) {
    var retry = e.target.closest ? e.target.closest('[data-retry]') : null;
    if (retry) { loadBP(true); return; }

    var head = e.target.closest ? e.target.closest('.bp-head') : null;
    if (!head) return;
    var item = head.closest('.bp-item');
    if (!item) return;
    var key = item.getAttribute('data-bp-key');
    var body = item.querySelector('.bp-body');
    var index = -1;
    var matches = (cache.bp[state.leagueId] || {}).matches || [];
    for (var i = 0; i < matches.length; i++) {
      var k = String(matches[i].battle_id || matches[i].match_id || i);
      if (k === key) { index = i; break; }
    }
    var open = !item.classList.contains('is-open');
    item.classList.toggle('is-open', open);
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (body) {
      if (open) {
        if (index >= 0) fillBpBody(body, matches[index]);
        body.hidden = false;
      } else {
        body.hidden = true;
      }
    }
    state.bpOpen[key] = open;
  });

  /* ============================================================
     启动
     ============================================================ */
  function boot() {
    renderPosFilters();
    call('leagues').then(function (d) {
      var leagues = (d && d.leagues) || [];
      if (!leagues.length) {
        state.leagues = [];
        el.leagueTabs.innerHTML = '<div class="d-empty" style="width:100%">暂无赛季数据</div>';
        renderOverview(null, null);
        failAllPanels('暂无赛季数据');
        return;
      }
      state.leagues = leagues;

      var wanted = getLeagueId();
      var found = leagues.some(function (l) { return String(l.league_id) === String(wanted); });
      var id = found ? String(wanted) : String(d.default_league_id || leagues[leagues.length - 1].league_id);
      state.leagueId = id;
      setLeagueId(id);

      renderLeagueTabs();
      loadMeta();
      loadTab(state.tab, true);
    }).catch(function (err) {
      el.leagueTabs.innerHTML = '';
      var box = document.createElement('div');
      box.className = 'd-empty';
      box.style.width = '100%';
      box.innerHTML = '赛季列表加载失败：' + esc(err && err.message ? err.message : err) +
        '<div class="d-empty-btn"><button class="btn btn-sm" id="leagueRetry">重试</button></div>';
      el.leagueTabs.appendChild(box);
      var rb = box.querySelector('#leagueRetry');
      if (rb) rb.addEventListener('click', boot);
      renderOverview(null, null);
      failAllPanels('赛季列表加载失败，请重试');
      toast('赛季列表加载失败');
    });

    // 订阅全局赛季切换（其他页面/组件改动 league 时同步）
    if (typeof KPL.onLeague === 'function') {
      try {
        KPL.onLeague(function (id) {
          if (!id) return;
          var sid = String(id);
          if (sid === state.leagueId) return;
          state.leagueId = sid;
          state.meta = null;
          state.heroMap = {};
          state.heroAll = [];
          state.heroLaneFilter = '全部';   // v2：分路筛选随赛季重置
          state.players = [];
          state.bpOpen = {};
          renderLeagueTabs();
          loadMeta();
          loadTab(state.tab, true);
        });
      } catch (e) { /* noop */ }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
