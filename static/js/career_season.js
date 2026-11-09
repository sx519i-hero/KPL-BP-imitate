/* ============================================================
   career_season.js — 赛季进程：赛程 / 积分榜 / 模拟 / 进 BP（契约 v3 §5.5）
   路由：/career/season?save_id=xxx
   数据源：GET /api/career/state、/api/career/schedule、/api/career/standings、
           /api/career/bracket；POST /api/career/simulate、/api/career/next_stage、
           /api/bp/new（为「我的比赛」开一局真实 BP）
   ============================================================ */
(function () {
  'use strict';

  var KPL = (window.KPL = window.KPL || {});

  function byId(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function int(v) { return isNum(v) ? String(Math.round(v)) : '—'; }
  function pct(v) { return isNum(v) ? v.toFixed(1) + '%' : '—'; }
  function toast(msg) {
    if (typeof KPL.toast === 'function') { try { return KPL.toast(msg); } catch (e) {} }
    if (window.console && console.warn) console.warn('[kpl]', msg);
  }

  function dbg() {
    if (!window.console || !console.info) return;
    var args = Array.prototype.slice.call(arguments);
    args.unshift('[career-season]');
    console.info.apply(console, args);
  }

  var el = {
    stageName: byId('stageName'),
    stageRec: byId('stageRec'),
    stageProgress: byId('stageProgress'),
    stageDay: byId('stageDay'),
    stageRules: byId('stageRules'),
    tabSchedule: byId('tabSchedule'),
    tabStandings: byId('tabStandings'),
    tabBracket: byId('tabBracket'),
    paneSchedule: byId('paneSchedule'),
    paneStandings: byId('paneStandings'),
    paneBracket: byId('paneBracket'),
    bracketHost: byId('bracketHost'),
    bracketStageChips: byId('bracketStageChips'),
    dayScroll: byId('dayScroll'),
    matchList: byId('matchList'),
    standingsBody: byId('standingsBody'),
    btnSimNext: byId('btnSimNext'),
    btnJumpMine: byId('btnJumpMine'),
    finishModal: byId('finishModal'),
    finishBody: byId('finishBody'),
    finishClose: byId('finishClose'),
    finishTitle: byId('finishTitle'),
    /* v26：年度积分成为第四个 Tab */
    tabAnnual: byId('tabAnnual'),
    paneAnnual: byId('paneAnnual'),
    annualBody: byId('annualBody'),
    annualTip: byId('annualTip'),
    /* v28：赛程闸门 */
    gateCard: byId('gateCard'),
    gateIcon: byId('gateIcon'),
    gateTitle: byId('gateTitle'),
    gateSub: byId('gateSub'),
    gateTeams: byId('gateTeams'),
    btnGate: byId('btnGate')
  };

  if (!el.stageName) return;

  var state = {
    saveId: null,
    tab: 'schedule',
    data: null,        /* /api/career/state 全量 */
    schedule: null,    /* /api/career/schedule（按 day 过滤的视图） */
    day: null,
    busy: false,
    finishedShown: false
  };

  function api() { return KPL.api || null; }
  function call(name) {
    var a = api();
    if (!a || typeof a[name] !== 'function') return Promise.reject(new Error('接口不可用：' + name));
    var args = Array.prototype.slice.call(arguments, 1);
    try { return Promise.resolve(a[name].apply(a, args)); }
    catch (e) { return Promise.reject(e); }
  }

  function querySaveId() {
    try { return new URLSearchParams(window.location.search).get('save_id') || null; }
    catch (e) { return null; }
  }

  /* ------------------------------------------------------------------ *
   * 阶段头
   * ------------------------------------------------------------------ */
  function renderStage(save, plan) {
    var st = save.stats || {};
    var wins = isNum(st.wins) ? st.wins : 0;
    var losses = isNum(st.losses) ? st.losses : 0;
    el.stageName.textContent = save.stage_label || '赛季进程';
    var seriesEl = document.getElementById('stageSeries');
    if (seriesEl) {
      seriesEl.textContent = save.series_label ? ('所属赛事：' + save.series_label) : '';
      seriesEl.hidden = !save.series_label;
    }
    el.stageRec.textContent = wins + '胜' + losses + '负';

    var progress = 0;
    if (isNum(save.current_day) && isNum(save.total_days) && save.total_days > 0) {
      progress = Math.round(save.current_day * 100 / save.total_days);
    } else if (isNum(save.stage_index) && isNum(save.stage_total) && save.stage_total > 0) {
      progress = Math.round((save.stage_index + 1) * 100 / save.stage_total);
    }
    el.stageProgress.style.width = Math.max(0, Math.min(100, progress)) + '%';

    var dayTxt = '';
    if (isNum(save.current_day) && isNum(save.total_days)) {
      dayTxt = '第 ' + save.current_day + ' / ' + save.total_days + ' 比赛日';
    } else {
      dayTxt = '第 ' + (isNum(save.stage_index) ? save.stage_index + 1 : 1) +
               ' / ' + (isNum(save.stage_total) ? save.stage_total : 1) + ' 阶段';
    }
    el.stageDay.textContent = dayTxt;

    /* rules 优先取当前 stage 的 plan 条目，缺失再退回 state.rules */
    var rules = '';
    if (Array.isArray(plan)) {
      for (var i = 0; i < plan.length; i++) {
        if (plan[i] && plan[i].key === save.stage) { rules = plan[i].rules || ''; break; }
      }
    }
    if (!rules && state.data && state.data.rules) rules = state.data.rules;
    el.stageRules.textContent = rules || '';
  }

  /* ------------------------------------------------------------------ *
   * 比赛日 chip + 比赛卡
   * ------------------------------------------------------------------ */
  function renderDays(view) {
    var days = (view && view.days) || [];
    if (!days.length) { el.dayScroll.innerHTML = ''; return; }
    var active = isNum(state.day) ? state.day : view.current_day;
    el.dayScroll.innerHTML = days.map(function (d) {
      var cls = 'c-day' + (Number(d.day) === Number(active) ? ' is-active' : '');
      var dot = (Number(d.mine) > 0 && Number(d.done) < Number(d.total)) ? '<span class="c-day-dot"></span>' : '';
      return '<button type="button" class="' + cls + '" data-day="' + esc(d.day) + '">' +
        esc(d.label || ('第' + d.day + '日')) + dot + '</button>';
    }).join('');

    Array.prototype.forEach.call(el.dayScroll.querySelectorAll('.c-day'), function (btn) {
      btn.addEventListener('click', function () {
        state.day = Number(btn.getAttribute('data-day'));
        loadSchedule(state.day);
      });
    });

    /* 默认把当前比赛日滚进视野 */
    var cur = el.dayScroll.querySelector('.c-day.is-active');
    if (cur && cur.scrollIntoView) {
      try { cur.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (e) {}
    }
  }

  function logoHtml(path, name) {
    if (path) {
      return '<span class="c-side-logo"><img src="' + esc(path) + '" alt="' + esc(name) + '" loading="lazy"></span>';
    }
    return '<span class="c-side-logo">' + esc(String(name || '?').slice(0, 1)) + '</span>';
  }

  function matchCardHtml(m) {
    var mine = !!m.is_mine;
    var done = m.status === 'done';
    var scoreTxt = done && m.score
      ? (int(m.score.blue) + ' : ' + int(m.score.red))
      : '– : –';
    var stateTxt = done ? '已结束' : (m.my_turn ? '你的回合' : '未开始');
    var stateCls = (!done && m.my_turn) ? ' is-turn' : '';

    var blueWin = done && m.winner === 'blue';
    var redWin = done && m.winner === 'red';
    var extra = m.bracket_round_name ? (' · ' + esc(m.bracket_round_name)) : '';

    return '<button type="button" class="c-match' + (mine ? ' is-mine' : '') + '"' +
      (mine && !done ? ' data-mine="1"' : '') + ' data-key="' + esc(m.match_key) + '">' +
      '<div class="c-match-top">' +
        '<span class="c-match-bo">BO' + esc(m.bo) + '</span>' +
        (mine ? '<span class="c-mine-tag">我的比赛</span>' : '') +
        '<span class="c-match-state' + stateCls + '">' + esc(stateTxt + extra) + '</span>' +
      '</div>' +
      '<div class="c-match-body">' +
        '<span class="c-side">' + logoHtml(m.blue_logo, m.blue_team) +
          '<span class="c-side-name' + (blueWin ? ' is-win' : '') + '">' + esc(m.blue_team) + '</span></span>' +
        '<span class="c-score' + (done ? '' : ' is-pending') + '">' + esc(scoreTxt) + '</span>' +
        '<span class="c-side is-right">' +
          '<span class="c-side-name' + (redWin ? ' is-win' : '') + '">' + esc(m.red_team) + '</span>' +
          logoHtml(m.red_logo, m.red_team) + '</span>' +
      '</div>' +
    '</button>';
  }

  /**
   * v24（用户 m16286）：赛程表固定只显示「当前选中的那一个比赛日」。
   * 服务端返回的 state.schedule 是全量赛程（45 场 / 363 场），直接渲染会把所有战队铺一屏，
   * 这里按 state.day 先过滤，点比赛日 chip 时也只换这一天。
   */
  function rowsForActiveDay(view) {
    var list = (view && view.matches) || [];
    if (!isNum(state.day)) return list;
    var day = Number(state.day);
    return list.filter(function (m) { return Number(m.day) === day; });
  }

  function renderMatches(view) {
    var list = rowsForActiveDay(view);
    if (!list.length) {
      el.matchList.innerHTML = '<div class="c-empty">这一天没有安排比赛。</div>';
      return;
    }
    el.matchList.innerHTML = list.map(matchCardHtml).join('');
    Array.prototype.forEach.call(el.matchList.querySelectorAll('.c-match[data-mine]'), function (btn) {
      btn.addEventListener('click', function () {
        var key = btn.getAttribute('data-key');
        enterBp(key);
      });
    });
  }

  function renderSchedule(view) {
    state.schedule = view;
    renderDays(view);
    renderMatches(view);
    syncBottom();
  }

  function loadSchedule(day) {
    var target = (day === null || day === undefined) ? state.day : day;
    return call('careerSchedule', state.saveId, target, false).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '赛程读取失败');
      if (!isNum(state.day)) state.day = res.current_day;
      renderSchedule(res);
      return res;
    }).catch(function (err) {
      el.matchList.innerHTML = '<div class="c-empty">赛程读取失败：' +
        esc(err && err.message ? err.message : String(err)) + '</div>';
      return null;
    });
  }

  /* ------------------------------------------------------------------ *
   * 积分榜
   * ------------------------------------------------------------------ */
  function tableHtml(group) {
    var rows = (group && group.rows) || [];
    if (!rows.length) return '';
    var html = '<div class="c-group-title">' + esc(group.name || group.key || '') + '</div>';
    html += '<div class="c-table-wrap"><table class="c-table"><thead><tr>' +
      '<th>#</th><th>战队</th><th>场次</th><th>胜</th><th>负</th><th>净胜局</th><th>积分</th><th>胜率</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var mine = state.data && state.data.save &&
        ((state.data.save.coach || {}).team_name === r.team) ? ' class="is-mine"' : '';
      html += '<tr' + mine + '>' +
        '<td>' + int(r.rank) + '</td>' +
        '<td><span class="c-td-team">' + esc(r.team) + '</span></td>' +
        '<td>' + int(r.played) + '</td>' +
        '<td>' + int(r.wins) + '</td>' +
        '<td>' + int(r.losses) + '</td>' +
        '<td>' + (isNum(r.game_diff) && r.game_diff > 0 ? '+' : '') + int(r.game_diff) + '</td>' +
        '<td>' + int(r.points) + '</td>' +
        '<td>' + pct(r.win_rate) + '</td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
  }

  function swissTableHtml(group) {
    var rows = (group && group.rows) || [];
    if (!rows.length) return '';
    var html = '<div class="c-table-wrap"><table class="c-table"><thead><tr>' +
      '<th>#</th><th>战队</th><th>胜</th><th>负</th><th>净胜局</th><th>状态</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var cls = r.state === '已晋级' ? ' is-adv' : (r.state === '已淘汰' ? ' is-out' : '');
      var mine = state.data && state.data.save &&
        ((state.data.save.coach || {}).team_name === r.team) ? ' class="is-mine"' : '';
      html += '<tr' + mine + '>' +
        '<td>' + int(r.rank) + '</td>' +
        '<td><span class="c-td-team">' + esc(r.team) + '</span></td>' +
        '<td>' + int(r.wins) + '</td>' +
        '<td>' + int(r.losses) + '</td>' +
        '<td>' + (isNum(r.game_diff) && r.game_diff > 0 ? '+' : '') + int(r.game_diff) + '</td>' +
        '<td><span class="c-state-chip' + cls + '">' + esc(r.state || '进行中') + '</span></td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
  }

  function bracketHtml(bracket) {
    if (!bracket) return '';
    var html = '';

    function sideHtml(title, rounds, champ) {
      if (!rounds || !rounds.length) return '';
      var out = '<div class="c-bracket-side"><h3>' + esc(title) + '</h3>';
      rounds.forEach(function (rd) {
        out += '<div class="c-bracket-round"><h4>' + esc(rd.name || rd.key) + '</h4>';
        (rd.slots || []).forEach(function (s) {
          var a = s.team || '待定';
          var b = s.team_b || '待定';
          var aWin = s.status === 'done' && s.winner === a;
          var bWin = s.status === 'done' && s.winner === b;
          var sc = (s.status === 'done' && s.score)
            ? (int(s.score.blue) + ':' + int(s.score.red)) : '–';
          out += '<div class="c-bracket-slot">' +
            '<span class="c-bs-name' + (aWin ? ' is-win' : '') + '">' + esc(a) + '</span>' +
            '<span class="c-bs-score">' + esc(sc) + '</span>' +
            '<span class="c-bs-name' + (bWin ? ' is-win' : '') + '">' + esc(b) + '</span>' +
          '</div>';
        });
        out += '</div>';
      });
      if (champ) out += '<div class="c-group-title">晋级：' + esc(champ) + '</div>';
      out += '</div>';
      return out;
    }

    html += sideHtml('胜者组', bracket.winners, bracket.winners_champion);
    html += sideHtml('败者组', bracket.losers, bracket.losers_champion);

    if (bracket.champion) {
      html += '<div class="c-card"><div class="c-empty" style="padding:14px;">🏆 冠军：<b>' +
        esc(bracket.champion) + '</b>' +
        (bracket.runner_up ? '<br>亚军：' + esc(bracket.runner_up) : '') + '</div></div>';
    }
    if (bracket.qualifiers && bracket.qualifiers.length) {
      html += '<div class="c-group-title">晋级战队</div><div class="c-table-wrap"><table class="c-table"><tbody>' +
        bracket.qualifiers.map(function (n) {
          return '<tr><td>' + esc(n) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    return html;
  }

  /* ------------------------------------------------------------------ *
   * 年度积分（v21：年度赛程包全程显示）
   * ------------------------------------------------------------------ */
  var ANNUAL_SLOT_LABEL = { spring: '春季赛', challenger: '挑战者杯', autumn: '夏季赛' };

  function annualColumns(d) {
    var cols = [];
    var rows = d.annual_series;
    if (Array.isArray(rows) && rows.length) {
      rows.forEach(function (r) {
        if (!r || !r.slot) return;
        cols.push({ slot: r.slot, label: r.label || ANNUAL_SLOT_LABEL[r.slot] || r.slot });
      });
    }
    if (!cols.length) {
      ['spring', 'challenger', 'autumn'].forEach(function (s) {
        cols.push({ slot: s, label: ANNUAL_SLOT_LABEL[s] });
      });
    }
    return cols;
  }

  function annualTableHtml(d) {
    var rows = Array.isArray(d.annual) ? d.annual : [];
    if (!rows.length) return '';
    var cols = annualColumns(d);
    var myTeam = ((d.save || {}).coach || {}).team_name || '';
    var html = '<div class="c-table-wrap"><table class="c-table c-table-annual"><thead><tr>' +
      '<th>#</th><th>战队</th>';
    cols.forEach(function (c) { html += '<th>' + esc(c.label) + '</th>'; });
    html += '<th>总积分</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      var mine = (myTeam && myTeam === r.team) ? ' class="is-mine"' : '';
      html += '<tr' + mine + '>' +
        '<td>' + int(r.rank) + '</td>' +
        '<td><span class="c-td-team">' + esc(r.team) + '</span></td>';
      cols.forEach(function (c) {
        var v = r[c.slot];
        html += '<td>' + (isNum(v) ? int(v) : '—') + '</td>';
      });
      html += '<td class="c-td-total">' + (isNum(r.total) ? int(r.total) : '—') + '</td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
  }

  /* v26（用户 m16579）：年度积分从「常驻卡片」改成和赛程 / 积分榜 / 对阵图并列的第四个 Tab。
     只有年度赛程包才显示这个 Tab，其它存档（联赛、杯赛）按钮直接隐藏。 */
  /* v30：只同步「年度积分 Tab 该不该显示」，不重建表格 —— 供每次 applyState 便宜调用 */
  function syncAnnualTab() {
    var d = state.data || {};
    var isPackage = (d.save || {}).template === 'annual_package';
    var show = isPackage && !!(d.annual && d.annual.length);
    if (el.tabAnnual) {
      el.tabAnnual.hidden = !show;
      if (!show) el.tabAnnual.classList.remove('is-active');
    }
    if (!show) {
      if (el.annualBody) el.annualBody.innerHTML = '';
      if (el.paneAnnual) el.paneAnnual.hidden = true;
      if (state.tab === 'annual') setTab('schedule');
    }
    return show;
  }

  function renderAnnual() {
    var d = state.data || {};
    if (!syncAnnualTab()) return;
    var html = annualTableHtml(d);
    if (!html) return;
    if (el.annualTip) {
      var labels = annualColumns(d).map(function (c) { return c.label; }).join(' / ');
      el.annualTip.textContent = labels + ' 累计积分（每站结束即结算）';
    }
    el.annualBody.innerHTML = html;
  }

  function renderStandings() {
    var d = state.data || {};
    var st = d.standings || {};
    var html = '';

    if (st && Array.isArray(st.groups) && st.groups.length) {
      var isSwiss = st.kind === 'swiss';
      st.groups.forEach(function (g) {
        html += isSwiss ? swissTableHtml(g) : tableHtml(g);
      });
    } else if (d.bracket) {
      html += bracketHtml(d.bracket);
    }

    if (!html) {
      html = '<div class="c-empty">当前阶段还没有积分榜数据，先打几场比赛。</div>';
    }
    if (st && st.rules) {
      html += '<div class="c-stage-rules" style="margin-top:12px;">' + esc(st.rules) + '</div>';
    }
    el.standingsBody.innerHTML = html;
  }

  /* ------------------------------------------------------------------ *
   * Tab 切换
   * ------------------------------------------------------------------ */
  function setTab(tab) {
    state.tab = tab;
    var isSched = tab === 'schedule';
    var isStand = tab === 'standings';
    var isBracket = tab === 'bracket';
    var isAnnual = tab === 'annual';
    el.tabSchedule.classList.toggle('is-active', isSched);
    el.tabStandings.classList.toggle('is-active', isStand);
    if (el.tabBracket) el.tabBracket.classList.toggle('is-active', isBracket);
    if (el.tabAnnual) el.tabAnnual.classList.toggle('is-active', isAnnual);
    el.tabSchedule.setAttribute('aria-selected', isSched ? 'true' : 'false');
    el.tabStandings.setAttribute('aria-selected', isStand ? 'true' : 'false');
    if (el.tabBracket) el.tabBracket.setAttribute('aria-selected', isBracket ? 'true' : 'false');
    if (el.tabAnnual) el.tabAnnual.setAttribute('aria-selected', isAnnual ? 'true' : 'false');
    el.paneSchedule.hidden = !isSched;
    el.paneStandings.hidden = !isStand;
    if (el.paneBracket) el.paneBracket.hidden = !isBracket;
    if (el.paneAnnual) el.paneAnnual.hidden = !isAnnual;
    if (isStand) renderStandings();
    if (isBracket) renderBracket(true);
    if (isAnnual) renderAnnual();
  }

  el.tabSchedule.addEventListener('click', function () { setTab('schedule'); });
  el.tabStandings.addEventListener('click', function () { setTab('standings'); });
  if (el.tabBracket) el.tabBracket.addEventListener('click', function () { setTab('bracket'); });
  if (el.tabAnnual) el.tabAnnual.addEventListener('click', function () { setTab('annual'); });

  /* ------------------------------------------------------------------ *
   * 对阵图（v13：淘汰赛流程图，随每次赛果刷新）
   * ------------------------------------------------------------------ */
  function renderBracket(force) {
    if (!el.bracketHost || !window.KPLBracket) return;
    var d = state.data || {};
    var view = state.bracketView || d.bracket;
    var chips = el.bracketStageChips;
    var books = (view && view.available) || [];

    if (chips) {
      if (books.length > 1) {
        chips.hidden = false;
        chips.innerHTML = books.map(function (b) {
          var act = (view.stage || '') === b.stage ? ' is-active' : '';
          return '<button type="button" class="c-day-chip' + act + '" data-bstage="' + esc(b.stage) + '">' +
            esc(b.label || b.stage) + '</button>';
        }).join('');
        [].forEach.call(chips.querySelectorAll('[data-bstage]'), function (btn) {
          btn.addEventListener('click', function () {
            loadBracketStage(btn.getAttribute('data-bstage'));
          });
        });
      } else {
        chips.hidden = true;
        chips.innerHTML = '';
      }
    }

    if (!view) {
      el.bracketHost.innerHTML = '<div class="bk-empty">还没有进入淘汰赛阶段，先打完常规赛 / 擂台赛。</div>';
      return;
    }
    var save = d.save || {};
    try {
      KPLBracket.render(el.bracketHost, view, { myTeam: (save.coach && save.coach.team_name) || save.my_team || null });
    } catch (err) {
      console.error('[season] 对阵图渲染失败：', err);
      el.bracketHost.innerHTML = '<div class="bk-empty">对阵图渲染失败，请刷新重试。</div>';
    }
    if (force) console.info('[season] 对阵图刷新', view.stage_label || view.stage);
  }

  function loadBracketStage(stage) {
    var saveId = state.saveId;
    if (!saveId) return;
    KPL.api.get('/api/tourney/bracket?save_id=' + encodeURIComponent(saveId) +
      '&stage=' + encodeURIComponent(stage)).then(function (res) {
      var view = (res && res.bracket) || null;
      if (!view) return;
      state.bracketView = view;
      renderBracket(true);
    }).catch(function (err) {
      toast('对阵图加载失败：' + (err && err.message ? err.message : err));
    });
  }

  /* ------------------------------------------------------------------ *
   * 底部按钮
   * ------------------------------------------------------------------ */
  function stageDone() {
    var save = (state.data && state.data.save) || {};
    return save.stage_complete === true || save.status === 'done';
  }

  function syncBottom() {
    var save = (state.data && state.data.save) || {};
    var done = stageDone();
    var isFinal = save.status === 'done';
    var gate = save.draw_gate || null;      /* v28：闸门没开启前不允许继续推进 */
    el.btnSimNext.disabled = !!state.busy || isFinal || !!gate;
    el.btnJumpMine.disabled = !!state.busy || isFinal || !!gate;
    if (done && !isFinal) {
      el.btnSimNext.textContent = '进入下一阶段';
      el.btnJumpMine.textContent = '查看赛季结算';
    } else if (isFinal) {
      el.btnSimNext.textContent = '赛季已结束';
      el.btnJumpMine.textContent = '查看赛季结算';
    } else {
      el.btnSimNext.textContent = '模拟下一场比赛';
      el.btnJumpMine.textContent = '推进到我的比赛';
    }
    if (gate) {
      var hint = gate.kind === 'series' ? '请先开启下一赛程'
        : (gate.kind === 'breakout' ? '请先完成突围赛选对手' : '请先完成淘汰赛抽签');
      el.btnSimNext.textContent = hint;
      el.btnJumpMine.textContent = hint;
    }
  }

  /* ------------------------------------------------------------------ *
   * v28：赛程闸门卡片
   * ------------------------------------------------------------------ */
  function renderGate() {
    if (!el.gateCard) return;
    var save = (state.data && state.data.save) || {};
    var gate = save.draw_gate || null;
    if (!gate) { el.gateCard.hidden = true; return; }
    el.gateCard.hidden = false;
    var isSeries = gate.kind === 'series';
    var isBreakout = gate.kind === 'breakout';   /* v31：年总突围赛选对手 */
    el.gateIcon.textContent = isSeries ? '🏁' : (isBreakout ? '⚔️' : '🎲');
    el.gateTitle.textContent = isSeries
      ? ('「' + (gate.label || '下一站') + '」' + (gate.needs_draw ? '已可开启' : '可直接开启'))
      : (isBreakout
        ? ((gate.label || '') + ' · 突围赛选对手')
        : ((gate.stage_label || '淘汰赛') + ' · 8 队抽签'));
    var sub;
    if (isSeries) {
      sub = gate.needs_draw
        ? ('本站已结束，赛程停在结果页。点击按钮进入「' + (gate.label || '下一站') +
           '」抽签，抽完签再开赛。')
        : ('本站已结束。年度总决赛按四站累计积分取前 12 队，不需要抽签，点击直接开启。');
    } else if (isBreakout) {
      sub = '擂台赛已结束。大师组第 5、第 6 与精英组第 2~5 共 6 队互相选对手：' +
            '大师 5 先选，大师 6 再选，剩下两支自动配对。点击按钮进入选对手页面。';
    } else {
      sub = '资格赛已结束。8 支晋级队需要重新抽签决定上下半区，抽完再开打。';
    }
    var br = (state.data && state.data.bracket) || {};
    if (isSeries && br.champion) {
      sub += '本站冠军：' + br.champion +
             (br.runner_up ? '（亚军 ' + br.runner_up + '）' : '') + '。';
    }
    el.gateSub.textContent = sub;
    var teams = gate.teams || [];
    if (teams.length) {
      var shown = teams.slice(0, 8).map(function (t) {
        return '<span class="c-gate-team">' + esc(t) + '</span>';
      }).join('');
      if (teams.length > 8) {
        shown += '<span class="c-gate-team is-more">+' + (teams.length - 8) + '</span>';
      }
      el.gateTeams.innerHTML = shown;
      el.gateTeams.hidden = false;
    } else {
      el.gateTeams.innerHTML = '';
      el.gateTeams.hidden = true;
    }
    el.btnGate.textContent = gate.button || '开启下一赛程';
    el.btnGate.disabled = !!state.busy;
  }

  if (byId('btnGate')) byId('btnGate').addEventListener('click', openGate);

  function openGate() {
    if (state.busy || !state.saveId) return;
    state.busy = true;
    renderGate();
    syncBottom();
    var gateNow = (state.data && state.data.save && state.data.save.draw_gate) || {};
    if (gateNow.kind === 'breakout') {
      /* v31：突围赛选对手是独立页面，不用开赛接口 */
      state.busy = false;
      window.location.href = '/career/breakout?save_id=' + encodeURIComponent(state.saveId);
      return;
    }
    dbg('开启闸门', state.saveId);
    call('tourneyDrawCommit', state.saveId).then(function (res) {
      state.busy = false;
      if (!res || res.ok === false) throw new Error((res && res.error) || '开启失败');
      var draw = res.draw || {};
      dbg('闸门已开启', draw.kind, draw.title);
      if (draw.needs_draw) {
        window.location.href = '/career/draw?save_id=' + encodeURIComponent(state.saveId);
        return null;
      }
      if (res.state) return applyState(res.state, state.day);
      return reloadAll();
    }).catch(function (err) {
      state.busy = false;
      toast('开启赛程失败：' + (err && err.message ? err.message : err));
      renderGate();
      syncBottom();
      reloadAll();
    });
  }

  function guard(fn) {
    if (state.busy) return;
    state.busy = true;
    syncBottom();
    /* v30：用 Promise.resolve().then 包一层，fn 同步抛错也能进 catch；
       结束时必须重绘闸门卡，否则「模拟」把闸门渲染成 busy 灰按钮后永远不恢复。 */
    Promise.resolve().then(fn).catch(function (err) {
      toast(err && err.message ? err.message : String(err));
    }).then(function () {
      state.busy = false;
      syncBottom();
      renderGate();
    });
  }

  el.btnSimNext.addEventListener('click', function () {
    var save = (state.data && state.data.save) || {};
    if (save.status === 'done') { openFinish(); return; }
    if (stageDone()) {
      guard(function () {
        return call('careerNextStage', state.saveId).then(function (res) {
          if (!res || res.ok === false) throw new Error((res && res.error) || '进入下一阶段失败');
          toast('已进入下一阶段');
          return reloadAll();
        });
      });
      return;
    }
    guard(function () {
      return call('careerSimulate', state.saveId, { count: 1, until_mine: false })
        .then(function (res) {
          if (!res || res.ok === false) throw new Error((res && res.error) || '模拟失败');
          var n = isNum(res.played) ? res.played : 0;
          toast(n > 0 ? ('已模拟 ' + n + ' 场比赛') : '没有可模拟的比赛了，点「推进到我的比赛」看你的对局');
          if (res.state) return applyState(res.state, state.day);   // v23：直接用响应里的 state
          return reloadAll();
        });
    });
  });

  el.btnJumpMine.addEventListener('click', function () {
    var save = (state.data && state.data.save) || {};
    if (save.status === 'done') { openFinish(); return; }
    if (stageDone()) {
      if (state.tab !== 'standings') setTab('standings');
      toast('本阶段已结束，请看积分榜 / 对阵，然后点「进入下一阶段」');
      return;
    }
    guard(function () {
      /* v24：count 给足（后端 until_mine 时把它当迭代预算），一次把我这场比赛之前的所有场次跑完 */
      return call('careerSimulate', state.saveId, { count: 200, until_mine: true })
        .then(function (res) {
          if (!res || res.ok === false) throw new Error((res && res.error) || '推进失败');
          var played = isNum(res.played) ? res.played : 0;
          /* v23：simulate 的响应已带 state，先直接渲染，再只拉需要的那一天 */
          var apply = res.state ? applyState(res.state, state.day) : reloadAll();
          return Promise.resolve(apply).then(function () {
            /* 推进后定位到我的比赛所在比赛日 */
            var mine = findMyPendingMatch();
            if (mine && isNum(mine.day)) {
              state.day = mine.day;
              return loadSchedule(mine.day).then(function () {
                toast(played > 0 ? ('已模拟 ' + played + ' 场，轮到你了') : '轮到你上场了');
              });
            }
            toast(played > 0 ? ('已模拟 ' + played + ' 场，本阶段没有你的比赛') : '本阶段没有你的比赛');
          });
        });
    });
  });

  function findMyPendingMatch() {
    var view = state.data && state.data.schedule;
    var list = (view && view.matches) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].is_mine && list[i].status !== 'done') return list[i];
    }
    var save = (state.data && state.data.save) || {};
    return save.next_match || null;
  }

  /* ------------------------------------------------------------------ *
   * 进入 BP
   * ------------------------------------------------------------------ */
  function enterBp(matchKey) {
    var save = (state.data && state.data.save) || {};
    var nm = save.next_match || null;
    var m = null;
    var view = state.data && state.data.schedule;
    ((view && view.matches) || []).forEach(function (x) {
      if (x.match_key === matchKey) m = x;
    });
    if (!m && nm && nm.match_key === matchKey) m = nm;
    if (!m) { toast('找不到这场比赛'); return; }
    if (m.bp_match_id) {
      /* 已开过局，直接续战 */
      window.location.href = arenaUrl(m.bp_match_id, matchKey);
      return;
    }

    var ev = save.event || {};
    /* v29：年度赛程包每站用自己的赛季数据（BP 英雄池 / AI 偏好都按该赛季） */
    var lid = save.series_league_id || ev.league_id;
    /* 玩家执教的是自己的战队：阵营由「我方是蓝还是红」推导，不能盲目给 blue */
    var myTeam = (save.coach && save.coach.team_name) || '';
    var side = 'blue';
    if (myTeam && String(myTeam) === String(m.red_team)) side = 'red';
    else if (myTeam && String(myTeam) === String(m.blue_team)) side = 'blue';
    /* v4：按存档里的「大模型 AI 出场范围」决定本场由谁决策
       off  -> builtin（内置 AI）
       all  -> 全赛季都用大模型
       final-> 只有「总决赛」阶段（以及淘汰赛最后一场）用大模型，省 token */
    /* v6：大模型联赛严格三分主体 —— 只有「对手是大模型队」这一场才走外部 LLM，
       其余对手（含所有其它队伍的 AI 对 AI 场次）一律用游戏内置 AI。 */
    var scope = ((save.options) || {}).llm_scope || 'off';
    var roleOf = {};
    (save.participants || []).forEach(function (p) { roleOf[p.name] = p.role || 'ai'; });
    var opponent = (myTeam && String(myTeam) === String(m.blue_team)) ? m.red_team : m.blue_team;
    /* v7 需求 3：外部大模型固定只执教 1 支战队。
       —— 只有「对手正是那支大模型队」这一场才走外部 LLM；
       —— 旧存档的 all 也被收敛为 single（不会再出现「大模型控制所有队伍」）；
       —— final 仍然只影响「大模型队是否在决赛才出场」，不会扩大到大模型执教全联盟。 */
    var useLlm = false;
    if (save.mode === 'llm' || scope === 'single' || scope === 'all') {
      useLlm = roleOf[opponent] === 'llm';
    } else if (scope === 'final') {
      useLlm = roleOf[opponent] === 'llm'
        ? ((m.stage === 'final') || (m.stage === 'playoff' && /决赛/.test(m.bracket_round_name || '')))
        : false;
    }
    dbg('enterBp', matchKey, 'opponent=' + opponent, 'role=' + (roleOf[opponent] || '?'),
        'llm_scope=' + scope, 'useLlm=' + useLlm);
    guard(function () {
      return call('bpNew', {
        mode: 'ai',
        league_id: lid,
        blue_team: m.blue_team,
        red_team: m.red_team,
        bo: m.bo,
        side: side,
        difficulty: 'pro',
        ai_engine: useLlm ? 'llm' : 'builtin'
      }).then(function (res) {
        if (!res || res.ok === false) throw new Error((res && res.error) || '开局失败');
        var mid = res.match_id || (res.match && res.match.match_id);
        if (!mid) throw new Error('后端未返回 match_id');
        /* 不做任何预先回写：胜负结果在 arena 结算时由 /api/career/result 写入 */
        window.location.href = arenaUrl(mid, matchKey);
      });
    });
  }

  function arenaUrl(matchId, matchKey) {
    var q = new URLSearchParams();
    q.set('match_id', matchId);
    q.set('save_id', state.saveId);
    q.set('match_key', matchKey);
    return '/arena?' + q.toString();
  }

  /* ------------------------------------------------------------------ *
   * 结算弹层
   * ------------------------------------------------------------------ */
  function openFinish() {
    var save = (state.data && state.data.save) || {};
    var st = save.stats || {};
    var champ = save.champion || (state.data.bracket && state.data.bracket.champion);
    el.finishTitle.textContent = champ ? '🏆 夺冠！' : '赛季结束';
    el.finishBody.innerHTML =
      '执教场次 ' + int(st.matched) + ' 场<br>' +
      '胜 ' + int(st.wins) + ' · 负 ' + int(st.losses) + ' · 胜率 ' + pct(st.win_rate) + '<br>' +
      (champ ? ('冠军：<b>' + esc(champ) + '</b>') : '本赛季未夺冠');
    el.finishModal.hidden = false;
  }
  if (el.finishClose) {
    el.finishClose.addEventListener('click', function () { window.location.href = '/career'; });
  }

  /* ------------------------------------------------------------------ *
   * 加载
   * ------------------------------------------------------------------ */
  function reloadAll() {
    return call('careerState', state.saveId).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '存档读取失败');
      return applyState(res, state.day);
    });
  }

  /**
   * v23：/api/tourney/simulate 的响应里本来就带着完整 state。
   * 直接渲染它，省掉「模拟完再拉一次 65KB state」的往返，点击到刷新快一倍。
   */
  /** 该比赛日还有没有没打的比赛（用来判断要不要把视角挪到新的当前比赛日） */
  function dayHasPending(view, day) {
    if (!isNum(day)) return false;
    var list = (view && view.matches) || [];
    for (var i = 0; i < list.length; i++) {
      if (Number(list[i].day) === Number(day) && list[i].status !== 'done') return true;
    }
    return false;
  }

  function applyState(res, day) {
    state.data = res;
    var save = res.save || {};
    var view = res.schedule || {};
    /* v24：没指定比赛日就落在「当前比赛日」，绝不把整份赛程铺出来 */
    if (!isNum(day)) {
      day = isNum(view.current_day) ? view.current_day : (isNum(save.current_day) ? save.current_day : null);
    }
    if (isNum(day)) state.day = Number(day);
    /* 当前这一天的比赛都打完了 → 视角自动跟到新的当前比赛日 */
    if (!dayHasPending(view, state.day) && isNum(view.current_day) && Number(view.current_day) !== Number(state.day)) {
      state.day = Number(view.current_day);
    }
    renderStage(save, res.plan);
    renderGate();
    syncBottom();
    syncAnnualTab();
    /* v30：只渲染当前 Tab；隐藏 Tab 切过去时 setTab 会现渲染，避免每次模拟都重建三张表 */
    setTab(state.tab);
    if (state.tab === 'schedule') renderSchedule(view);
    return null;
  }

  function boot() {
    state.saveId = querySaveId();
    if (!state.saveId) {
      el.matchList.innerHTML = '<div class="c-empty">缺少 save_id，请从生涯首页重新进入。</div>';
      return;
    }
    reloadAll().then(function () {
      var save = (state.data && state.data.save) || {};
      if (save.status === 'done' && !state.finishedShown) {
        state.finishedShown = true;
        openFinish();
      }
      /* v24：默认停在「当前比赛日」，不再自动跳到我的下一场所在日。
         想看自己的比赛点「推进到我的比赛」即可，这样顶部「第 X / Y 比赛日」和下面的赛程始终一致。 */
      return null;
    }).catch(function (err) {
      el.matchList.innerHTML = '<div class="c-empty">存档读取失败：' +
        esc(err && err.message ? err.message : String(err)) + '</div>';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
