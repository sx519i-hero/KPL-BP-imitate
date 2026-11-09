/* ==========================================================================
 * 首页（index.html）
 * 赛季选择 → 概览条 → 模式卡片 → 对阵设置 → 跳转 /arena
 * 依赖 window.KPL（api.js）
 * ========================================================================== */
(function (global) {
  'use strict';

  var KPL = global.KPL;
  var document = global.document;

  if (!KPL) {
    if (global.console) console.error('[index] KPL 未加载：请确认 /static/js/api.js 已在页面中引入');
    return;
  }

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  /* ------------------------------------------------------------------ *
   * 状态
   * ------------------------------------------------------------------ */
  var state = {
    leagues: [],
    leagueId: null,
    teams: [],        // 当前赛季的 /api/teams 列表
    mode: 'ai',       // 'ai' | 'free'
    bo: 7,
    side: 'blue',
    difficulty: 'pro',
    blueTeam: '',
    redTeam: '',
    loadingLeague: false,
    loadingTeams: false
  };

  var BO_LIST = [1, 3, 5, 7, 9];
  var DIFF_LIST = ['rookie', 'pro', 'boss'];

  /* ------------------------------------------------------------------ *
   * 小渲染工具
   * ------------------------------------------------------------------ */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined && html !== null) n.innerHTML = html;
    return n;
  }

  function text(value) {
    return value === undefined || value === null ? '' : String(value);
  }

  /** 安全的 innerHTML（转义插值） */
  var esc = KPL.util.esc;

  function setSeg(container, activeValue, dataKey) {
    if (!container) return;
    $$('[' + dataKey + ']', container).forEach(function (btn) {
      var v = btn.getAttribute(dataKey);
      btn.classList.toggle('active', v === String(activeValue));
      btn.setAttribute('aria-pressed', v === String(activeValue) ? 'true' : 'false');
    });
  }

  function statusText(status) {
    if (status === 'ongoing') return '进行中';
    if (status === 'upcoming') return '未开始';
    if (status === 'ended') return '已结束';
    return '';
  }

  function statusChip(status) {
    var map = {
      ongoing: { cls: 'chip badge-ok', label: '进行中' },
      upcoming: { cls: 'chip badge-warn', label: '未开始' },
      ended: { cls: 'chip', label: '已结束' }
    };
    var it = map[status];
    if (!it) return '';
    return '<span class="' + it.cls + ' tag">' + esc(it.label) + '</span>';
  }

  function formatRange(start, end) {
    var s = start ? String(start) : '';
    var e = end ? String(end) : '';
    if (!s && !e) return KPL.DASH;
    if (s && e) return s + ' ~ ' + e;
    return s || e;
  }

  /* ------------------------------------------------------------------ *
   * 赛季下拉
   * ------------------------------------------------------------------ */
  function renderLeagueOptions() {
    var sel = $('#league-select');
    if (!sel) return;

    if (!state.leagues.length) {
      sel.innerHTML = '<option value="">暂无赛季数据</option>';
      sel.disabled = true;
      return;
    }

    sel.disabled = false;
    sel.innerHTML = state.leagues.map(function (lg) {
      var id = text(lg.league_id);
      var name = text(lg.name) || id;
      var st = lg.status ? '（' + statusText(lg.status) + '）' : '';
      return '<option value="' + esc(id) + '">' + esc(name + st) + '</option>';
    }).join('');

    if (state.leagueId) sel.value = String(state.leagueId);
  }

  function renderLeagueCardHeader() {
    var badge = $('#league-status');
    if (!badge) return;
    var cur = state.leagues.filter(function (l) {
      return String(l.league_id) === String(state.leagueId);
    })[0];
    badge.innerHTML = cur ? statusChip(cur.status) : '';
  }

  /* ------------------------------------------------------------------ *
   * 赛季概览（/api/league_meta）
   * ------------------------------------------------------------------ */
  function renderMetaSkeleton() {
    var box = $('#league-meta');
    if (!box) return;
    box.innerHTML = [0, 1, 2, 3].map(function () {
      return '<div class="meta-item"><div class="skeleton skeleton-line" style="width:52%"></div>' +
        '<div class="skeleton skeleton-line" style="width:78%;margin-top:6px"></div></div>';
    }).join('');
  }

  function renderMetaEmpty(msg) {
    var box = $('#league-meta');
    if (!box) return;
    box.innerHTML = '<div class="meta-item" style="grid-column:1/-1">' +
      '<div class="k">赛季概览</div>' +
      '<div class="v" style="font-weight:600;color:var(--ink-2)">' + esc(msg || '暂无数据') + '</div>' +
      '</div>';
  }

  function renderMeta(meta) {
    var box = $('#league-meta');
    if (!box) return;

    var counts = (meta && meta.counts) || {};
    var hasBp = !!(meta && meta.has_bp_history);
    var bpMatches = counts.bp_matches;
    var heroes = counts.heroes;

    var items = [
      { k: '起始日期', v: (meta && meta.start) ? String(meta.start) : KPL.DASH },
      { k: '结束日期', v: (meta && meta.end) ? String(meta.end) : KPL.DASH },
      { k: '真实 BP 对局', v: KPL.fmtInt(bpMatches), suffix: bpMatches === null || bpMatches === undefined ? '' : '' },
      { k: '英雄收录', v: KPL.fmtInt(heroes) }
    ];

    box.innerHTML = items.map(function (it) {
      return '<div class="meta-item"><div class="k">' + esc(it.k) + '</div>' +
        '<div class="v">' + esc(it.v) + '</div></div>';
    }).join('') + (hasBp ? '' :
      '<div class="meta-item" style="grid-column:1/-1;background:var(--line)">' +
      '<div class="k">提示</div>' +
      '<div class="v" style="font-weight:600;color:var(--ink-2)">该赛季暂无真实 BP 历史，AI 将主要依据英雄版本数据决策</div>' +
      '</div>');

    // 概览里的战队/选手数量放进副标题（空间有限）
    var sub = $('#banner-sub');
    if (sub && meta) {
      var teamN = counts.teams;
      var playerN = counts.players;
      if (teamN || playerN) {
        sub.textContent = '像职业教练一样排兵布阵，用真实 KPL 数据打完一场完整 BP' +
          '（本季 ' + KPL.fmtInt(teamN) + ' 支战队 · ' + KPL.fmtInt(playerN) + ' 名选手）';
      }
    }
  }

  function loadLeagueMeta(leagueId) {
    if (!leagueId) { renderMetaEmpty('请先选择赛季'); return Promise.resolve(); }
    renderMetaSkeleton();
    return KPL.api.leagueMeta(leagueId).then(function (res) {
      // 防止竞态：返回时赛季已经换了就丢弃
      if (String(state.leagueId) !== String(leagueId)) return;
      renderMeta(res);
    }).catch(function (err) {
      if (String(state.leagueId) !== String(leagueId)) return;
      renderMetaEmpty('概览加载失败：' + (err && err.message ? err.message : '未知错误'));
    });
  }

  /* ------------------------------------------------------------------ *
   * 战队下拉（/api/teams）
   * ------------------------------------------------------------------ */
  function renderTeamOptions() {
    var blue = $('#blue-select');
    var red = $('#red-select');
    if (!blue || !red) return;

    if (!state.teams.length) {
      var emptyOpt = '<option value="">暂无战队数据</option>';
      blue.innerHTML = emptyOpt;
      red.innerHTML = emptyOpt;
      blue.disabled = true;
      red.disabled = true;
      state.blueTeam = '';
      state.redTeam = '';
      return;
    }

    blue.disabled = false;
    red.disabled = false;

    var opts = state.teams.map(function (t) {
      var name = text(t.name);
      var bits = [name];
      if (!KPL.util.isNil(t.rank)) bits.push('第' + KPL.fmtInt(t.rank) + '名');
      if (!KPL.util.isNil(t.win_rate)) bits.push('胜率 ' + KPL.fmtPct(t.win_rate, 1));
      else bits.push('胜率 ' + KPL.DASH);
      return '<option value="' + esc(name) + '">' + esc(bits.join(' · ')) + '</option>';
    }).join('');

    blue.innerHTML = opts;
    red.innerHTML = opts;

    // 默认前两名
    var top1 = text(state.teams[0] && state.teams[0].name);
    var top2 = text(state.teams[1] && state.teams[1].name) || top1;

    if (state.blueTeam && findTeam(state.blueTeam)) {
      blue.value = state.blueTeam;
    } else {
      state.blueTeam = top1;
      blue.value = top1;
    }
    if (state.redTeam && findTeam(state.redTeam)) {
      red.value = state.redTeam;
    } else {
      state.redTeam = (top2 === state.blueTeam)
        ? text(state.teams[2] && state.teams[2].name) || top2
        : top2;
      red.value = state.redTeam;
    }

    updateSetupHint();
  }

  function findTeam(name) {
    if (!name) return null;
    for (var i = 0; i < state.teams.length; i++) {
      if (text(state.teams[i].name) === String(name)) return state.teams[i];
    }
    return null;
  }

  function loadTeams(leagueId) {
    if (!leagueId) {
      state.teams = [];
      renderTeamOptions();
      return Promise.resolve();
    }
    state.loadingTeams = true;

    var blue = $('#blue-select');
    var red = $('#red-select');
    if (blue) { blue.disabled = true; blue.innerHTML = '<option value="">加载中…</option>'; }
    if (red) { red.disabled = true; red.innerHTML = '<option value="">加载中…</option>'; }

    return KPL.api.teams(leagueId).then(function (res) {
      if (String(state.leagueId) !== String(leagueId)) return;
      state.teams = (res && res.teams) || [];
      state.loadingTeams = false;
      // 换赛季后原队名可能不存在，重置选择（renderTeamOptions 内会校验）
      renderTeamOptions();
    }).catch(function (err) {
      if (String(state.leagueId) !== String(leagueId)) return;
      state.loadingTeams = false;
      state.teams = [];
      renderTeamOptions();
      var msg = err && err.message ? err.message : '未知错误';
      if (blue) { blue.innerHTML = '<option value="">战队加载失败</option>'; }
      if (red) { red.innerHTML = '<option value="">战队加载失败</option>'; }
      var hint = $('#setup-hint');
      if (hint) hint.textContent = '战队数据加载失败：' + msg;
    });
  }

  /* ------------------------------------------------------------------ *
   * 设置区提示（同队 / 空态）
   * ------------------------------------------------------------------ */
  // 开始按钮的启用/禁用：同时控制视觉类与 aria，避免「空态灰了再也亮不回来」
  function setStartEnabled(enabled) {
    var btn = $('#start-btn');
    if (!btn) return;
    if (enabled) {
      btn.classList.remove('is-disabled');
      btn.removeAttribute('aria-disabled');
    } else {
      btn.classList.add('is-disabled');
      btn.setAttribute('aria-disabled', 'true');
    }
  }

  /* v2 契约 §3.1 第 3/6 条：
   *   BO1 单局模式 → 关闭败方选边，阵营由玩家开局手选（执边 chip 保持可用）
   *   BO3/5/7 → 第 2 局起由上一局败方选边
   * 文案由需求方点名，勿随意改写。 */
  function updateBoHint() {
    var boHint = $('#bo-hint');
    var sideLabel = $('#side-field-label');
    var isSingle = Number(state.bo) === 1;

    if (boHint) {
      boHint.textContent = isSingle
        ? '单局模式无败方选边，阵营由你开局选定'
        : '第 2 局起由上一局败方选边';
    }
    // BO1 时执边是必选项，标签点明「开局手选」，避免玩家以为不用选
    if (sideLabel) {
      sideLabel.textContent = isSingle ? '执边（开局手选）' : '执边';
    }
    // 执边 chip 永远可用（单局靠它选边，多局是首局归属），只做 aria 说明
    var sideSeg = $('#side-seg');
    if (sideSeg) {
      sideSeg.setAttribute('aria-description', isSingle
        ? '单局模式，阵营由你在此选择'
        : '首局阵营归属；第 2 局起由上一局败方选边');
    }
  }

  function updateSetupHint() {
    var hint = $('#setup-hint');
    if (!hint) return;

    setStartEnabled(!!state.leagueId && state.teams.length > 0);
    updateBoHint();

    if (!state.teams.length) {
      hint.textContent = '当前赛季暂无战队数据，无法开始对战。可切换赛季或等待数据抓取完成。';
      return;
    }
    if (!state.blueTeam || !state.redTeam) {
      hint.textContent = '请选择蓝方和红方战队。';
      return;
    }
    if (state.blueTeam === state.redTeam) {
      hint.textContent = '蓝方与红方不能是同一支战队，换一支吧。';
      return;
    }

    if (state.mode === 'free') {
      hint.textContent = '自由模式：红蓝双方各执一边，AI 不出手（难度设置已忽略）。';
    } else {
      var diffLabel = { rookie: '入门', pro: '职业', boss: '魔王' }[state.difficulty] || state.difficulty;
      hint.textContent = 'AI 模式：你执' + (state.side === 'blue' ? '蓝方' : '红方') +
        '，AI 掌控另一方（难度：' + diffLabel + '）。';
    }
  }

  /* ------------------------------------------------------------------ *
   * 模式选择
   * ------------------------------------------------------------------ */
  // v4/v5：这些入口是「整页跳转」，不参与 BO/执边等表单状态
  var PAGE_MODES = {
    career: '/career/team',
    friend: '/friend',
    llm: '/llm',
    settings: '/settings'
  };

  function selectMode(mode) {
    if (PAGE_MODES[mode]) {
      window.location.href = PAGE_MODES[mode];
      return;
    }
    if (mode !== 'ai' && mode !== 'free') return;

    state.mode = mode;
    $$('#mode-grid .mode-card').forEach(function (card) {
      var m = card.getAttribute('data-mode');
      if (PAGE_MODES[m]) return;
      card.classList.toggle('is-active', m === mode);
      card.setAttribute('aria-pressed', m === mode ? 'true' : 'false');
    });

    // 难度只在 AI 模式显示
    var diffField = $('#difficulty-field');
    if (diffField) diffField.hidden = (mode !== 'ai');

    updateSetupHint();
  }

  /* v7 需求 4：独立的「开始对战」按钮已移除。
   * 三大入口卡片中，AI 对战卡片即开局按钮（读上方赛季 + 对阵设置直接进竞技场），
   * 自由 / 大模型 / 设置属于次级入口，跟随各自模式的内部按钮启动。 */
  function enterMode(mode) {
    if (mode === 'ai' || mode === 'free') {
      selectMode(mode);
      onStart();
      return;
    }
    selectMode(mode);
  }

  /* ------------------------------------------------------------------ *
   * 开始对战
   * ------------------------------------------------------------------ */
  function buildArenaUrl() {
    var params = [
      ['mode', state.mode],
      ['league_id', state.leagueId],
      ['blue', state.blueTeam],
      ['red', state.redTeam],
      ['bo', state.bo],
      ['side', state.side]
    ];
    if (state.mode === 'ai') params.push(['difficulty', state.difficulty]);

    var q = params
      .filter(function (p) { return p[1] !== null && p[1] !== undefined && p[1] !== ''; })
      .map(function (p) {
        // 用 + 表示空格更友好，但仍做标准编码
        return encodeURIComponent(p[0]) + '=' + encodeURIComponent(String(p[1]));
      })
      .join('&');

    return '/arena' + (q ? '?' + q : '');
  }

  function onStart() {
    if (!state.leagueId) {
      KPL.toast('请先选择赛季');
      return;
    }
    if (!state.teams.length) {
      KPL.toast('当前赛季暂无战队数据，无法开始');
      return;
    }
    if (!state.blueTeam || !state.redTeam) {
      KPL.toast('请选择蓝方和红方战队');
      return;
    }
    if (state.blueTeam === state.redTeam) {
      KPL.toast('蓝方与红方不能是同一支战队');
      return;
    }

    var url = buildArenaUrl();
    var btn = $('#start-btn');
    if (btn) btn.classList.add('is-disabled');
    global.location.href = url;
  }

  /* ------------------------------------------------------------------ *
   * 事件绑定
   * ------------------------------------------------------------------ */
  function bindEvents() {
    // 赛季切换
    var leagueSel = $('#league-select');
    if (leagueSel) {
      leagueSel.addEventListener('change', function () {
        var id = leagueSel.value;
        if (!id) return;
        state.leagueId = id;
        KPL.setLeagueId(id);          // 写入 localStorage 并通知订阅者
        refreshForLeague(id);         // 同页刷新：概览 + 战队
      });
    }

    // 模式卡片（v7：三大入口 + 次级入口）
    $$('#mode-grid .mode-card').forEach(function (card) {
      card.addEventListener('click', function () {
        enterMode(card.getAttribute('data-mode'));
      });
    });
    /* v9 修复：v8 把这三枚入口从 .chip 改成了 .btn（与好友 PK 面板统一风格），
       这里必须按 data-mode 查找，否则「同屏自由 BP / 大模型对战 / 设置」点了没反应 */
    /* v47：「AI 对战」从底部导航挪进对阵设置卡片，这里绑定新按钮 */
    /* v47：「AI 对战」从底部导航挪进对阵设置卡片 */
    var startAiBtn = document.getElementById('btnStartAi');
    if (startAiBtn) {
      startAiBtn.addEventListener('click', function () { enterMode('ai'); });
    }

    $$('#mode-more [data-mode]').forEach(function (chip) {
      chip.addEventListener('click', function () {
        enterMode(chip.getAttribute('data-mode'));
      });
    });

    // 战队
    var blue = $('#blue-select');
    var red = $('#red-select');
    if (blue) {
      blue.addEventListener('change', function () {
        state.blueTeam = blue.value;
        updateSetupHint();
      });
    }
    if (red) {
      red.addEventListener('change', function () {
        state.redTeam = red.value;
        updateSetupHint();
      });
    }

    // BO
    var boSeg = $('#bo-seg');
    if (boSeg) {
      boSeg.addEventListener('click', function (ev) {
        var btn = ev.target.closest ? ev.target.closest('[data-bo]') : null;
        if (!btn) return;
        var v = parseInt(btn.getAttribute('data-bo'), 10);
        if (BO_LIST.indexOf(v) < 0) return;
        state.bo = v;
        setSeg(boSeg, v, 'data-bo');
        updateSetupHint(); // 切换 BO 后要刷新「单局/多局选边」提示
      });
    }

    // 执边
    var sideSeg = $('#side-seg');
    if (sideSeg) {
      sideSeg.addEventListener('click', function (ev) {
        var btn = ev.target.closest ? ev.target.closest('[data-side]') : null;
        if (!btn) return;
        var v = btn.getAttribute('data-side');
        if (v !== 'blue' && v !== 'red') return;
        state.side = v;
        setSeg(sideSeg, v, 'data-side');
        updateSetupHint();
      });
    }

    // 难度
    var diffSeg = $('#difficulty-seg');
    if (diffSeg) {
      diffSeg.addEventListener('click', function (ev) {
        var btn = ev.target.closest ? ev.target.closest('[data-difficulty]') : null;
        if (!btn) return;
        var v = btn.getAttribute('data-difficulty');
        if (DIFF_LIST.indexOf(v) < 0) return;
        state.difficulty = v;
        setSeg(diffSeg, v, 'data-difficulty');
        updateSetupHint();
      });
    }

    // 开始对战
    var start = $('#start-btn');
    if (start) start.addEventListener('click', onStart);
  }

  /* ------------------------------------------------------------------ *
   * 赛季切换后刷新同页内容
   * ------------------------------------------------------------------ */
  function refreshForLeague(leagueId) {
    renderLeagueCardHeader();
    return Promise.all([
      loadLeagueMeta(leagueId),
      loadTeams(leagueId)
    ]);
  }

  /* ------------------------------------------------------------------ *
   * 初始化
   * ------------------------------------------------------------------ */
  function initSegDefaults() {
    setSeg($('#bo-seg'), state.bo, 'data-bo');
    setSeg($('#side-seg'), state.side, 'data-side');
    setSeg($('#difficulty-seg'), state.difficulty, 'data-difficulty');
    updateBoHint(); // 首屏就要显示选边规则文案（不等 API 回来）
  }

  function init() {
    initSegDefaults();
    bindEvents();
    // v7：首屏不自动开局，只把默认模式设为 AI（点卡片才进竞技场）
    state.mode = 'ai';

    // 订阅赛季变更（其它页面/Tab 也可能改）
    KPL.onLeague(function (id) {
      if (!id || String(id) === String(state.leagueId)) return;
      state.leagueId = String(id);
      var sel = $('#league-select');
      if (sel) sel.value = String(id);
      refreshForLeague(state.leagueId);
    });

    var stored = KPL.getLeagueId();

    renderMetaSkeleton();

    KPL.api.leagues().then(function (res) {
      state.leagues = (res && res.leagues) || [];
      var def = res && res.default_league_id;

      if (!state.leagues.length) {
        // 优雅空态：不白屏、不报错红字
        renderLeagueOptions();
        renderMetaEmpty('暂无赛季数据，请先运行数据抓取（scraper）生成 data/leagues.json');
        var blue = $('#blue-select');
        var red = $('#red-select');
        if (blue) { blue.innerHTML = '<option value="">暂无赛季数据</option>'; blue.disabled = true; }
        if (red) { red.innerHTML = '<option value="">暂无赛季数据</option>'; red.disabled = true; }
        var start = $('#start-btn');
        if (start) start.classList.add('is-disabled');
        var hint = $('#setup-hint');
        if (hint) hint.textContent = '暂无赛季数据，请先完成数据抓取。';
        return;
      }

      // 优先级：localStorage > 后端 default > 最后一个
      var exists = function (id) {
        return state.leagues.some(function (l) { return String(l.league_id) === String(id); });
      };
      var pick = null;
      if (stored && exists(stored)) pick = String(stored);
      else if (def && exists(def)) pick = String(def);
      else pick = String(state.leagues[state.leagues.length - 1].league_id);

      state.leagueId = pick;
      KPL.setLeagueId(pick);

      renderLeagueOptions();
      var sel = $('#league-select');
      if (sel) sel.value = pick;

      refreshForLeague(pick);
    }).catch(function (err) {
      // 后端未启动 / 网络错误：优雅空态
      state.leagues = [];
      renderLeagueOptions();
      renderMetaEmpty('赛季列表加载失败：' + (err && err.message ? err.message : '网络错误'));
      var blue = $('#blue-select');
      var red = $('#red-select');
      if (blue) { blue.innerHTML = '<option value="">—</option>'; blue.disabled = true; }
      if (red) { red.innerHTML = '<option value="">—</option>'; red.disabled = true; }
      var start = $('#start-btn');
      if (start) start.classList.add('is-disabled');
      var hint = $('#setup-hint');
      if (hint) hint.textContent = '暂时连不上服务器，请确认后端已启动（python app.py）。';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
