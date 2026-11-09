/* ==========================================================================
 * KPL 模拟 BP 教练 —— 历史回看（v19）
 * 原生 JS，无框架无构建。只读：不改存档、不影响赛季进度。
 *
 * 依赖：
 *   /static/js/api.js     → KPL.api.tourneyHistory / KPL.api.tourneyHistoryStage
 *   /static/js/bracket.js → window.KPLBracket.render(container, view, opts)
 *
 * 后端契约：
 *   GET /api/tourney/history?save_id=
 *     { ok, save_id, save_name, template, category, status, champion, coach_team,
 *       current:{series_index, stage},
 *       series:[{index,key,slot,name,label,pipeline,status,is_current,champion,
 *                ranking,points,stages:[{key,label,kind,bo,status,total,done,mine,
 *                                        started,is_current,bracket_available}],
 *                total,done}],
 *       annual:[...] }
 *   GET /api/tourney/history_stage?save_id=&series=&stage=
 *     { ok, series_index, series_key, series_label, series_status,
 *       stage, stage_label, stage_kind, bo, status, total, done, is_current,
 *       rules, days:[{day,label,total,done,mine}],
 *       matches:[{match_key,series,day,stage,stage_label,group,kind,bo,
 *                 blue_team,red_team,blue_logo,red_logo,status,score,winner,
 *                 winner_team,loser_team,is_mine,result_source,bp_match_id,
 *                 bracket_round_name}],
 *       tables:[{key,name,rows:[{team,rank,played,wins,losses,game_wins,
 *                                game_losses,game_diff,points,win_rate}],merged?}],
 *       ranking:[{team,rank,...}], bracket:{...}|null, bracket_available }
 * ========================================================================== */
(function (global) {
  'use strict';

  var document = global.document;
  if (!document) return;

  var KPL = global.KPL || (global.KPL = {});
  var DASH = '—';

  /* ------------------------------------------------------------------ *
   * 工具
   * ------------------------------------------------------------------ */
  function byId(id) { return document.getElementById(id); }

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function isNil(v) { return v === null || v === undefined || v === ''; }

  function num(v, fallback) {
    if (isNil(v)) return fallback === undefined ? null : fallback;
    var n = Number(v);
    return isFinite(n) ? n : (fallback === undefined ? null : fallback);
  }

  function pct(v) {
    var n = num(v);
    return n === null ? DASH : (Math.round(n * 10) / 10) + '%';
  }

  function toast(msg) {
    if (KPL.toast) { KPL.toast(msg); return; }
    if (global.console) global.console.log('[history]', msg);
  }

  function dbg() {
    if (!global.console || !global.console.log) return;
    var args = Array.prototype.slice.call(arguments);
    args.unshift('[history]');
    global.console.log.apply(global.console, args);
  }

  function statusText(status) {
    return ({
      none: '未开始',
      pending: '待开赛',
      running: '进行中',
      done: '已结束'
    })[status] || (status || DASH);
  }

  function logoHtml(path, name) {
    if (path) {
      return '<span class="hx-logo"><img src="' + esc(path) + '" alt="" loading="lazy"></span>';
    }
    return '';
  }

  function querySaveId() {
    try {
      return new URLSearchParams(global.location.search).get('save_id') || '';
    } catch (e) {
      return '';
    }
  }

  /* ------------------------------------------------------------------ *
   * DOM
   * ------------------------------------------------------------------ */
  var el = {
    btnHistory: byId('btnHistory'),
    modal: byId('historyModal'),
    close: byId('historyClose'),
    resume: byId('historyResume'),
    title: byId('historyTitle'),
    series: byId('historySeries'),
    stage: byId('historyStage'),
    tabSchedule: byId('hxTabSchedule'),
    tabStandings: byId('hxTabStandings'),
    tabBracket: byId('hxTabBracket'),
    paneSchedule: byId('hxPaneSchedule'),
    paneStandings: byId('hxPaneStandings'),
    paneBracket: byId('hxPaneBracket'),
    dayScroll: byId('hxDayScroll'),
    matchList: byId('hxMatchList'),
    standings: byId('hxStandings'),
    bracketHost: byId('hxBracketHost'),
    summary: byId('hxSummary')
  };

  /* ------------------------------------------------------------------ *
   * 状态
   * ------------------------------------------------------------------ */
  var S = {
    saveId: '',
    index: null,        // /history 返回体
    seriesIdx: 0,       // 当前查看的系列赛序号
    stageKey: '',       // 当前查看的阶段 key
    detail: null,       // /history_stage 返回体
    day: null,          // 当前查看的比赛日
    tab: 'schedule',
    loading: false,
    opened: false
  };

  /* ------------------------------------------------------------------ *
   * 渲染：站点 chips
   * ------------------------------------------------------------------ */
  function seriesChipHtml(item) {
    var cls = 'hx-chip';
    if (item.index === S.seriesIdx) cls += ' is-active';
    if (item.status === 'done') cls += ' is-done';
    var sub = '';
    if (item.champion) sub = '🏆 ' + esc(item.champion);
    else if (item.status === 'running') sub = '进行中';
    else if (item.status === 'pending') sub = '未开始';
    else if (item.status === 'done') sub = '已结束';
    return '<button type="button" class="' + cls + '" data-series="' + item.index + '">' +
      esc(item.label || item.name || ('第 ' + (item.index + 1) + ' 站')) +
      (sub ? '<span class="hx-chip-sub">' + sub + '</span>' : '') +
      '</button>';
  }

  function renderSeries() {
    if (!el.series) return;
    var list = (S.index && S.index.series) || [];
    if (!list.length) { el.series.innerHTML = ''; return; }
    el.series.innerHTML = list.map(seriesChipHtml).join('');
    Array.prototype.forEach.call(el.series.querySelectorAll('[data-series]'), function (btn) {
      btn.addEventListener('click', function () {
        var idx = Number(btn.getAttribute('data-series'));
        if (idx === S.seriesIdx) return;
        S.seriesIdx = idx;
        // 切站后默认选「该站最后一个已开始/已结束的阶段」，让用户立刻看到结果
        S.stageKey = pickDefaultStage(idx);
        S.day = null;
        S.detail = null;
        renderSeries();
        renderStageChips();
        loadStage();
      });
    });
  }

  function seriesByIndex(idx) {
    var list = (S.index && S.index.series) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].index === idx) return list[i];
    }
    return null;
  }

  function pickDefaultStage(idx) {
    var item = seriesByIndex(idx);
    if (!item || !item.stages || !item.stages.length) return '';
    var started = item.stages.filter(function (s) { return s.started; });
    if (started.length) return started[started.length - 1].key;
    return item.stages[0].key;
  }

  /* ------------------------------------------------------------------ *
   * 渲染：阶段 chips
   * ------------------------------------------------------------------ */
  function stageChipHtml(stage) {
    var cls = 'hx-chip';
    if (stage.key === S.stageKey) cls += ' is-active';
    if (stage.status === 'done') cls += ' is-done';
    var sub = stage.total ? (stage.done + '/' + stage.total) : statusText(stage.status);
    var dis = stage.started ? '' : ' disabled';
    return '<button type="button" class="' + cls + '" data-stage="' + esc(stage.key) + '"' + dis +
      ' title="' + esc(stage.label + ' · ' + statusText(stage.status) + ' · BO' + stage.bo) + '">' +
      esc(stage.label) + '<span class="hx-chip-sub">' + esc(sub) + '</span></button>';
  }

  function renderStageChips() {
    if (!el.stage) return;
    var item = seriesByIndex(S.seriesIdx);
    var stages = (item && item.stages) || [];
    el.stage.innerHTML = stages.map(stageChipHtml).join('');
    Array.prototype.forEach.call(el.stage.querySelectorAll('[data-stage]'), function (btn) {
      if (btn.disabled) return;
      btn.addEventListener('click', function () {
        var key = btn.getAttribute('data-stage');
        if (key === S.stageKey) return;
        S.stageKey = key;
        S.day = null;
        S.detail = null;
        renderStageChips();
        loadStage();
      });
    });
  }

  /* ------------------------------------------------------------------ *
   * 渲染：赛程
   * ------------------------------------------------------------------ */
  function renderDays() {
    if (!el.dayScroll) return;
    var days = (S.detail && S.detail.days) || [];
    if (!days.length) { el.dayScroll.innerHTML = ''; return; }
    var html = days.map(function (d) {
      var cls = 'c-day' + (S.day === d.day ? ' is-active' : '');
      return '<button type="button" class="' + cls + '" data-day="' + d.day + '">' +
        esc(d.label) + (d.total ? ' · ' + d.done + '/' + d.total : '') + '</button>';
    }).join('');
    el.dayScroll.innerHTML = html;
    Array.prototype.forEach.call(el.dayScroll.querySelectorAll('[data-day]'), function (btn) {
      btn.addEventListener('click', function () {
        S.day = Number(btn.getAttribute('data-day'));
        renderDays();
        renderMatches();
      });
    });
    var active = el.dayScroll.querySelector('.c-day.is-active');
    if (active && active.scrollIntoView) {
      try { active.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (e) { /* 老浏览器忽略 */ }
    }
  }

  function matchRowHtml(m) {
    var mine = !!m.is_mine;
    var score = m.score || {};
    var done = m.status === 'done';
    var blueWin = done && m.winner === 'blue';
    var redWin = done && m.winner === 'red';
    var scoreTxt;
    if (done) {
      scoreTxt = (isNil(score.blue) ? DASH : score.blue) + ' : ' + (isNil(score.red) ? DASH : score.red);
    } else {
      scoreTxt = statusText(m.status);
    }
    var top = [];
    top.push('<span class="hx-badge">' + esc(m.stage_label || '') + ' · BO' + esc(m.bo || DASH) + '</span>');
    if (m.bracket_round_name) top.push('<span class="hx-badge">' + esc(m.bracket_round_name) + '</span>');
    if (m.group && m.group !== 'cross') top.push('<span class="hx-badge">' + esc(m.group) + ' 组</span>');
    if (mine) top.push('<span class="hx-badge is-mine">我队</span>');

    var winTeam = done && m.winner_team ? '<div class="hx-win-team">胜方：<b>' + esc(m.winner_team) + '</b></div>' : '';

    return '<div class="hx-row' + (mine ? ' is-mine' : '') + '">' +
      '<div class="hx-row-main">' +
        '<div class="hx-row-top">' + top.join('') + '</div>' +
        '<div class="hx-versus">' +
          '<span class="hx-team' + (blueWin ? ' is-win' : '') + '">' + esc(m.blue_team || '待定') + '</span>' +
          '<span class="hx-vs">vs</span>' +
          '<span class="hx-team' + (redWin ? ' is-win' : '') + '">' + esc(m.red_team || '待定') + '</span>' +
        '</div>' +
        winTeam +
      '</div>' +
      '<span class="hx-score' + (done ? '' : ' is-pending') + '">' + esc(scoreTxt) + '</span>' +
    '</div>';
  }

  function renderMatches() {
    if (!el.matchList) return;
    var all = (S.detail && S.detail.matches) || [];
    var rows = (S.day === null || S.day === undefined)
      ? all
      : all.filter(function (m) { return num(m.day) === S.day; });
    if (!all.length) {
      el.matchList.innerHTML = '<div class="hx-empty">这个阶段的比赛还没开始，暂时没有可回看的赛程。</div>';
      return;
    }
    if (!rows.length) {
      el.matchList.innerHTML = '<div class="hx-empty">当天没有比赛。</div>';
      return;
    }
    el.matchList.innerHTML = '<div class="hx-list">' + rows.map(matchRowHtml).join('') + '</div>';
  }

  /* ------------------------------------------------------------------ *
   * 渲染：积分榜
   * ------------------------------------------------------------------ */
  function tableHtml(group) {
    var rows = group.rows || [];
    if (!rows.length) return '';
    var head = '<tr><th>#</th><th class="team">战队</th><th>场次</th><th>胜</th><th>负</th>' +
      '<th>净胜局</th><th>积分</th><th>胜率</th></tr>';
    var body = rows.map(function (r) {
      var mine = S.myTeam && r.team === S.myTeam;
      return '<tr' + (mine ? ' class="is-mine"' : '') + '>' +
        '<td>' + esc(isNil(r.rank) ? DASH : r.rank) + '</td>' +
        '<td class="team">' + esc(r.team) + '</td>' +
        '<td>' + esc(isNil(r.played) ? DASH : r.played) + '</td>' +
        '<td>' + esc(isNil(r.wins) ? DASH : r.wins) + '</td>' +
        '<td>' + esc(isNil(r.losses) ? DASH : r.losses) + '</td>' +
        '<td>' + esc(isNil(r.game_diff) ? DASH : (r.game_diff > 0 ? '+' + r.game_diff : r.game_diff)) + '</td>' +
        '<td>' + esc(isNil(r.points) ? DASH : r.points) + '</td>' +
        '<td>' + esc(pct(r.win_rate)) + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="hx-table-group">' +
      '<h3 class="hx-table-title">' + esc(group.name || group.key) + '</h3>' +
      '<div class="hx-table-wrap"><table class="hx-table"><thead>' + head + '</thead><tbody>' +
      body + '</tbody></table></div>' +
    '</div>';
  }

  function renderStandings() {
    if (!el.standings) return;
    var tables = (S.detail && S.detail.tables) || [];
    if (!tables.length) {
      el.standings.innerHTML = '<div class="hx-empty">这个阶段没有循环赛积分榜（淘汰赛请查看「对阵图」）。</div>';
      return;
    }
    el.standings.innerHTML = tables.map(tableHtml).join('');
  }

  /* ------------------------------------------------------------------ *
   * 渲染：对阵图
   * ------------------------------------------------------------------ */
  function renderRankingList(node, ranking, note) {
    if (!node) return;
    if (!ranking || !ranking.length) {
      node.innerHTML = '<div class="hx-empty">' + esc(note || '这个阶段暂无排名数据。') + '</div>';
      return;
    }
    var html = note ? '<div class="hx-empty hx-empty-top">' + esc(note) + '</div>' : '';
    html += '<div class="hx-table-group"><div class="hx-table-title">阶段名次</div>' +
      '<div class="hx-table-wrap"><table class="hx-table"><thead><tr>' +
      '<th>#</th><th>战队</th></tr></thead><tbody>' +
      ranking.map(function (r) {
        var mine = S.myTeam && r.team === S.myTeam;
        return '<tr' + (mine ? ' class="is-mine"' : '') + '>' +
          '<td>' + esc(num(r.rank) === null ? DASH : r.rank) + '</td>' +
          '<td>' + esc(r.team || DASH) + '</td></tr>';
      }).join('') + '</tbody></table></div></div>';
    node.innerHTML = html;
  }

  function renderBracket() {
    if (!el.bracketHost) return;
    var view = S.detail && S.detail.bracket;
    if (!view || !view.rounds || !view.rounds.length) {
      // v25（用户 m16430）：淘汰赛阶段现在会按真实赛果重建对阵图；能走到这里说明
      // 这个阶段本身就不是淘汰赛（循环赛 / 卡位赛 / 擂台赛），或者是一场决胜的总决赛。
      var rank = (S.detail && S.detail.ranking) || [];
      renderRankingList(el.bracketHost, rank,
        rank.length ? '这个阶段没有淘汰赛对阵图，下面按真实赛果给出阶段名次。'
                    : '这个阶段还没有对阵图数据。');
      return;
    }
    if (global.KPLBracket && typeof global.KPLBracket.render === 'function') {
      try {
        global.KPLBracket.render(el.bracketHost, view, { myTeam: S.myTeam });
        return;
      } catch (e) {
        dbg('对阵图渲染失败，回退为名次列表', e);
      }
    }
    renderRankingList(el.bracketHost, S.detail && S.detail.ranking, '对阵图暂时无法显示。');
  }

  /* ------------------------------------------------------------------ *
   * 汇总信息
   * ------------------------------------------------------------------ */
  function renderSummary() {
    if (!el.summary) return;
    var d = S.detail;
    if (!d) { el.summary.textContent = DASH; return; }
    var parts = [];
    parts.push((d.series_label || '') + ' · ' + (d.stage_label || ''));
    parts.push(statusText(d.status));
    if (d.total) parts.push('已完赛 ' + d.done + '/' + d.total + ' 场');
    // 只有「存档还在打」且这就是当前阶段时才提示，避免已结束的存档出现自相矛盾的文案
    var saveRunning = S.index && S.index.status !== 'done';
    if (d.is_current && saveRunning) parts.push('当前正在进行的阶段');
    el.summary.textContent = parts.filter(Boolean).join(' · ');
    el.summary.title = el.summary.textContent;
  }

  function renderTitle() {
    if (!el.title) return;
    var name = (S.index && S.index.save_name) || '存档';
    // 只有整个赛程包跑完时 save.champion 才是「总冠军」，中途它只是最近一站的冠军，
    // 显示出来会误导，所以只在 status === 'done' 时带到标题里。
    var champ = (S.index && S.index.status === 'done') ? S.index.champion : null;
    el.title.textContent = '历史回看 · ' + name + (champ ? '（🏆 ' + champ + '）' : '');
  }

  /* ------------------------------------------------------------------ *
   * Tab
   * ------------------------------------------------------------------ */
  function setTab(tab) {
    S.tab = tab;
    var map = [
      ['schedule', el.tabSchedule, el.paneSchedule],
      ['standings', el.tabStandings, el.paneStandings],
      ['bracket', el.tabBracket, el.paneBracket]
    ];
    map.forEach(function (row) {
      var on = row[0] === tab;
      if (row[1]) {
        row[1].classList.toggle('is-active', on);
        row[1].setAttribute('aria-selected', on ? 'true' : 'false');
      }
      if (row[2]) row[2].hidden = !on;
    });
    if (tab === 'standings') renderStandings();
    if (tab === 'bracket') renderBracket();
  }

  /* ------------------------------------------------------------------ *
   * 渲染当前阶段
   * ------------------------------------------------------------------ */
  function renderDetail() {
    var d = S.detail;
    if (!d) {
      if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">加载中…</div>';
      return;
    }
    if (S.day === null || S.day === undefined) {
      var days = d.days || [];
      if (days.length) S.day = days[0].day;
    }
    renderDays();
    renderMatches();
    if (S.tab === 'standings') renderStandings();
    if (S.tab === 'bracket') renderBracket();
    renderSummary();
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  function api() { return KPL.api || null; }

  function loadStage() {
    var a = api();
    if (!a || !a.tourneyHistoryStage || !S.saveId || !S.stageKey) {
      if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">暂时无法加载历史数据。</div>';
      return;
    }
    if (el.matchList) el.matchList.innerHTML = '<div class="c-skel"></div><div class="c-skel"></div>';
    if (el.standings) el.standings.innerHTML = '<div class="c-skel"></div>';
    if (el.bracketHost) el.bracketHost.innerHTML = '<div class="c-skel"></div>';
    if (el.summary) el.summary.textContent = '加载中…';
    dbg('拉取阶段明细', S.saveId, S.seriesIdx, S.stageKey);
    a.tourneyHistoryStage(S.saveId, S.seriesIdx, S.stageKey).then(function (res) {
      if (!res || res.ok === false) {
        var msg = (res && res.error) || '历史数据加载失败';
        if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">' + esc(msg) + '</div>';
        return;
      }
      S.detail = res;
      S.day = null;
      renderDetail();
    }).catch(function (err) {
      dbg('阶段明细请求失败', err);
      if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">历史数据加载失败，请稍后重试。</div>';
      toast('历史数据加载失败');
    });
  }

  function loadIndex() {
    var a = api();
    if (!a || !a.tourneyHistory) {
      if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">暂时无法加载历史数据。</div>';
      return Promise.resolve();
    }
    if (S.loading) return Promise.resolve();
    S.loading = true;
    if (el.series) el.series.innerHTML = '<span class="hx-chip is-active">加载中…</span>';
    return a.tourneyHistory(S.saveId).then(function (res) {
      S.loading = false;
      if (!res || res.ok === false) {
        var msg = (res && res.error) || '历史索引加载失败';
        if (el.series) el.series.innerHTML = '';
        if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">' + esc(msg) + '</div>';
        return;
      }
      S.index = res;
      S.myTeam = res.coach_team || null;
      var cur = res.current || {};
      var list = res.series || [];
      if (!list.length) {
        if (el.series) el.series.innerHTML = '';
        if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">这个存档还没有任何赛事记录。</div>';
        return;
      }
      S.seriesIdx = num(cur.series_index, 0) || 0;
      if (S.seriesIdx >= list.length) S.seriesIdx = list.length - 1;
      S.stageKey = cur.stage || pickDefaultStage(S.seriesIdx);
      renderTitle();
      renderSeries();
      renderStageChips();
      dbg('历史索引', list.length, '站', '当前', S.seriesIdx, S.stageKey);
      loadStage();
    }).catch(function (err) {
      S.loading = false;
      dbg('历史索引请求失败', err);
      if (el.matchList) el.matchList.innerHTML = '<div class="hx-empty">历史数据加载失败，请稍后重试。</div>';
      toast('历史数据加载失败');
    });
  }

  /* ------------------------------------------------------------------ *
   * 打开 / 关闭
   * ------------------------------------------------------------------ */
  function open() {
    if (!el.modal) return;
    el.modal.hidden = false;
    if (document.body) document.body.classList.add('hx-lock');
    if (!S.opened || !S.index) {
      S.opened = true;
      loadIndex();
    }
    setTab(S.tab || 'schedule');
  }

  function close() {
    if (!el.modal) return;
    el.modal.hidden = true;
    if (document.body) document.body.classList.remove('hx-lock');
  }

  /* ------------------------------------------------------------------ *
   * 绑定
   * ------------------------------------------------------------------ */
  function boot() {
    if (!el.btnHistory || !el.modal) return;   // 非 /career/season 页面直接退出
    S.saveId = querySaveId();
    if (!S.saveId) dbg('缺少 save_id，历史回看不可用');

    el.btnHistory.addEventListener('click', open);
    if (el.close) el.close.addEventListener('click', close);
    if (el.resume) el.resume.addEventListener('click', close);
    if (el.modal) {
      el.modal.addEventListener('click', function (ev) {
        if (ev.target === el.modal) close();   // 点遮罩关闭
      });
    }
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && el.modal && !el.modal.hidden) close();
    });
    if (el.tabSchedule) el.tabSchedule.addEventListener('click', function () { setTab('schedule'); });
    if (el.tabStandings) el.tabStandings.addEventListener('click', function () { setTab('standings'); });
    if (el.tabBracket) el.tabBracket.addEventListener('click', function () { setTab('bracket'); });

    dbg('历史回看已就绪 save_id=', S.saveId);

    // 从 /career 存档列表点「📜 结果回看」进来时带 ?history=1，直接展开历史面板
    try {
      var auto = new URLSearchParams(global.location.search).get('history');
      if (auto === '1' && S.saveId) open();
    } catch (e) { /* 老浏览器忽略 */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  global.KPLHistory = {
    open: open,
    close: close,
    reload: function () { S.index = null; S.detail = null; return loadIndex(); },
    _state: S
  };
})(typeof window !== 'undefined' ? window : this);
