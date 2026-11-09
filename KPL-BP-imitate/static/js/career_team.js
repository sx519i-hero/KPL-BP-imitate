/* ============================================================
   career_team.js — 选择赛事 / 选择战队 / 自建战队（契约 v3 §5.2 §5.3）
   路由：/career/team[?league_id=xxx]
   数据源：/api/career/events、/api/career/teams、/api/career/saves、
           POST /api/career/new
   严禁编造：所有数字都来自接口，缺失显示「—」。
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

  var el = {
    topTitle: byId('topTitle'),
    eventHead: byId('eventHead'),
    eventName: byId('eventName'),
    eventDesc: byId('eventDesc'),
    coachTip: byId('coachTip'),
    coachTier: byId('coachTier'),
    teamGridView: byId('teamGridView'),
    teamGrid: byId('teamGrid'),
    ruleLeague: byId('ruleLeague'),
    ruleLeague2: byId('ruleLeague2'),
    btnCustomToggle: byId('btnCustomToggle'),
    customView: byId('customView'),
    formNo: byId('formNo'),
    customName: byId('customName'),
    nameCount: byId('nameCount'),
    customLeague: byId('customLeague'),
    customScale: byId('customScale'),
    btnCustomSubmit: byId('btnCustomSubmit'),
    btnCustomBack: byId('btnCustomBack'),
    bottomBar: byId('bottomBar'),
    btnPickTeam: byId('btnPickTeam'),
    needEvent: byId('needEvent'),
    btnOpenEvents: byId('btnOpenEvents'),
    eventModal: byId('eventModal'),
    eventGroups: byId('eventGroups'),
    btnEventCancel: byId('btnEventCancel'),
    btnEventConfirm: byId('btnEventConfirm'),
    tplSelect: byId('tplSelect'),
    editionSelect: byId('editionSelect'),
    tplPickHint: byId('tplPickHint'),
    tplPickStatus: byId('tplPickStatus')
  };

  if (!el.teamGrid) return;

  var state = {
    template: null,
    editionId: null,
    edition: null,
    templates: [],
    leagueId: null,
    event: null,
    teams: [],
    selected: null,
    events: null,
    picks: null,
    submitting: false
  };

  function api() { return KPL.api || null; }
  function call(name, a, b) {
    var a2 = api();
    if (!a2 || typeof a2[name] !== 'function') return Promise.reject(new Error('接口不可用：' + name));
    try { return Promise.resolve(a2[name](a, b)); }
    catch (e) { return Promise.reject(e); }
  }

  /* ------------------------------------------------------------------ *
   * v4 需求 1（BUG 修复）：所有请求都加超时，绝不允许 UI 永久卡在骨架屏
   * ------------------------------------------------------------------ */
  var REQ_TIMEOUT_MS = 20000;

  function dbg() {
    if (!window.console) return;
    var args = Array.prototype.slice.call(arguments);
    args.unshift('[career]');
    console.info.apply(console, args);
  }

  function warn() {
    if (!window.console) return;
    var args = Array.prototype.slice.call(arguments);
    args.unshift('[career]');
    console.warn.apply(console, args);
  }

  /** 给任意 Promise 套一个超时，超时后 reject（不会卡死界面） */
  function withTimeout(promise, ms, label) {
    ms = ms || REQ_TIMEOUT_MS;
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        warn(label + ' 超时（' + Math.round(ms / 1000) + 's）');
        reject(new Error(label + '超时（' + Math.round(ms / 1000) + ' 秒）'));
      }, ms);
      promise.then(function (v) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(v);
      }, function (e) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(e);
      });
    });
  }

  function queryLeagueId() {
    try {
      var p = new URLSearchParams(window.location.search);
      var v = p.get('league_id');
      return v ? String(v) : null;
    } catch (e) { return null; }
  }

  /* v4：大模型 AI 范围（off / final / all），由 /llm 页带参数进入 */
  function queryLlmScope() {
    try {
      var v = new URLSearchParams(window.location.search).get('llm_scope');
      return (v === 'all' || v === 'final') ? v : 'off';
    } catch (e) { return 'off'; }
  }

  /* ------------------------------------------------------------------ *
   * v6：赛事模板（KPL 官方赛制）
   * ------------------------------------------------------------------ */
  function queryTemplate() {
    try {
      var v = new URLSearchParams(window.location.search).get('template');
      return v ? String(v) : null;
    } catch (e) { return null; }
  }

  /** v29：届次（哪一年哪一届），形如 ``spring:20260001`` / ``annual_package:2026`` */
  function queryEdition() {
    try {
      var v = new URLSearchParams(window.location.search).get('edition');
      return v ? String(v) : null;
    } catch (e) { return null; }
  }

  function editionOf(editionId) {
    var rows = [];
    (state.templates || []).forEach(function (t) {
      (t.editions || []).forEach(function (e) { rows.push(e); });
    });
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].edition_id) === String(editionId)) return rows[i];
    }
    return null;
  }

  function stationText(edition) {
    var list = (edition && edition.stations) || [];
    if (list.length < 2) return '';
    return list.map(function (st) { return st.label || st.key; }).join(' \u2192 ');
  }

  /** 把 /api/tourney/templates 的 templates 数组转成弹窗需要的 groups 结构 */
  function hitTemplates(templates) {
    var groups = { league: [], cup: [], package: [] };
    templates.forEach(function (t) {
      var key = t.category === 'package' ? 'package' : (t.category === 'cup' ? 'cup' : 'league');
      var editions = t.editions || [];
      if (!editions.length) {
        // 兼容：后端没给届次时退回模板本身
        groups[key].push({
          league_id: t.key,
          template: t.key,
          name: t.name,
          short_name: t.name,
          format_desc: t.desc || (int(t.team_count) + ' 支战队'),
          team_count: t.team_count,
          stages: t.stages || [],
          category: t.category
        });
        return;
      }
      editions.forEach(function (e) {
        var stations = stationText(e);
        var text = (t.desc || '') + (stations ? ' · ' + stations : '');
        groups[key].push({
          league_id: e.edition_id,
          template: t.key,
          edition_id: e.edition_id,
          name: e.name,
          short_name: e.name,
          format_desc: text || (int(t.team_count) + ' 支战队'),
          team_count: t.team_count,
          stages: t.stages || [],
          category: t.category
        });
      });
    });
    return groups;
  }

  function findTemplate(key) {
    var rows = state.templates || [];
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].key) === String(key)) return rows[i];
    }
    return null;
  }

  /** 模板 -> 赛季数据源的匹配规则（按赛事性质挑，绝不乱配） */
  var TEMPLATE_LEAGUE_HINT = {
    spring: ['春季赛'],
    autumn: ['夏季赛', '秋季赛'],
    challenger: ['挑战者杯'],
    annual_finals: ['年度总决赛'],
    annual_package: ['春季赛']
  };

  function pickLeagueOf(tpl) {
    var leagues = (tpl && tpl.leagues) || [];
    var hints = TEMPLATE_LEAGUE_HINT[tpl && tpl.key] || [];
    var want = (tpl && tpl.team_count) || 0;
    var best = null;
    var bestScore = -1;
    leagues.forEach(function (lg) {
      if (!lg || !lg.league_id) return;
      var name = String(lg.name || '');
      var score = 0;
      hints.forEach(function (hint) { if (name.indexOf(hint) >= 0) score += 10; });
      // 参赛规模匹配优先级高：模板要 18/32/12 支，队伍数对不上就没法排赛程
      if (want && (lg.team_count || 0) === want) score += 5;
      // 年份越新越好（同分时取后出现的）
      var year = parseInt((name.match(/(\d{4})/) || [])[1], 10) || 0;
      score += year / 10000;
      if (score > bestScore) { bestScore = score; best = lg; }
    });
    if (best && bestScore < 5) {
      // 没找到同规模的赛事，退回同规模优先的一支，至少保证赛程能排
      var sameSize = null;
      leagues.forEach(function (lg) {
        if (sameSize) return;
        if ((lg.team_count || 0) === want) sameSize = lg;
      });
      best = sameSize || best;
    }
    return best ? best.league_id : null;
  }

  function leagueNameOf(tpl, leagueId) {
    var leagues = (tpl && tpl.leagues) || [];
    for (var i = 0; i < leagues.length; i++) {
      if (String(leagues[i].league_id) === String(leagueId)) return String(leagues[i].name || '');
    }
    return '';
  }

  function buildSaveName(teamName) {
    var tpl = findTemplate(state.template) || state.event || {};
    var evName = (state.event && state.event.name) || tpl.name || 'KPL';
    return evName + ' · ' + (teamName || '我的战队');
  }

  /* v7 需求 1：赛事下拉（年度赛程包 / 春季赛 / 挑战者杯 / 夏季赛 ...） */
  var TPL_ORDER = ['annual_package', 'spring', 'challenger', 'autumn', 'annual_finals'];
  var TPL_TEXT = {
    annual_package: '年度赛程包（春季赛 → 挑战者杯 → 夏季赛 → 年度总决赛）',
    spring: 'KPL 春季赛（单赛季）',
    challenger: '挑战者杯（单杯赛）',
    autumn: 'KPL 夏季赛（单赛季）',
    annual_finals: '年度总决赛（单杯赛）'
  };

  /* v29：届次下拉（2026年KPL春季赛 / 2025 年度赛程包 …） */
  function fillEditionSelect(tpl) {
    if (!el.editionSelect) return;
    var rows = (tpl && tpl.editions) || [];
    if (!rows.length) {
      el.editionSelect.innerHTML = '<option value="">该赛事暂无可用赛季数据</option>';
      el.editionSelect.disabled = true;
      state.edition = null;
      state.editionId = null;
      return;
    }
    el.editionSelect.disabled = false;
    el.editionSelect.innerHTML = rows.map(function (e) {
      var stations = stationText(e);
      var tail = stations ? ('（' + stations + '）') : '';
      var status = e.status === 'ongoing' ? ' · 进行中' : '';
      return '<option value="' + esc(e.edition_id) + '">' +
        esc(e.name) + esc(tail) + esc(status) + '</option>';
    }).join('');
    var want = state.editionId;
    var hit = null;
    rows.forEach(function (e) { if (String(e.edition_id) === String(want)) hit = e; });
    if (!hit) hit = rows[0];
    el.editionSelect.value = hit.edition_id;
    state.editionId = hit.edition_id;
    state.edition = hit;
    dbg('届次下拉就绪：' + rows.length + ' 项，当前 ' + hit.edition_id);
  }

  function applyEdition(edition) {
    var tpl = findTemplate(state.template) || {};
    state.edition = edition || null;
    state.editionId = edition ? edition.edition_id : null;
    state.leagueId = (edition && edition.league_id) || null;
    var dataName = (edition && edition.name) || '';
    state.event = {
      name: dataName || tpl.name || '',
      desc: (tpl.desc || '') + (dataName ? '（BP 数据取自' + dataName + '）' : ''),
      format_desc: (tpl.desc || '') + (dataName ? '（BP 数据取自' + dataName + '）' : ''),
      team_count: tpl.team_count,
      short_name: dataName || tpl.name || '',
      category: tpl.category
    };
    renderEventHead();
    if (el.formNo) {
      el.formNo.textContent = 'NO.' + ((edition && edition.year) || '----') +
        '-' + String(tpl.team_count || 0);
    }
    if (el.tplPickStatus) el.tplPickStatus.textContent = '';
  }

  function fillTplSelect(templates) {
    if (!el.tplSelect) return;
    var byKey = {};
    (templates || []).forEach(function (t) { byKey[t.key || t.template] = t; });
    var keys = TPL_ORDER.filter(function (k) { return byKey[k]; });
    Object.keys(byKey).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    if (!keys.length) return;
    el.tplSelect.innerHTML = keys.map(function (k) {
      var label = TPL_TEXT[k] || (byKey[k] && byKey[k].name) || k;
      return '<option value="' + esc(k) + '">' + esc(label) + '</option>';
    }).join('');
    if (keys.indexOf(state.template) >= 0) el.tplSelect.value = state.template;
    if (el.tplPickStatus) el.tplPickStatus.textContent = '';
    dbg('赛事下拉就绪：' + keys.join(',') + '，当前 ' + state.template);
  }

  if (el.tplSelect) {
    el.tplSelect.addEventListener('change', function () {
      var v = el.tplSelect.value;
      if (!v || v === state.template) return;
      state.template = v;
      state.selected = null;
      dbg('切换赛事模板 → ' + v);
      try {
        window.history.replaceState(null, '', '/career/team?template=' + encodeURIComponent(v));
      } catch (e) { /* 老浏览器忽略 */ }
      bootByTemplate();
    });
  }

  if (el.editionSelect) {
    el.editionSelect.addEventListener('change', function () {
      var v = el.editionSelect.value;
      if (!v) return;
      var edition = editionOf(v);
      if (!edition) { toast('未找到该届赛事数据'); return; }
      dbg('切换届次 → ' + v);
      state.selected = null;
      applyEdition(edition);
      try {
        window.history.replaceState(null, '', '/career/team?template=' +
          encodeURIComponent(state.template) + '&edition=' + encodeURIComponent(v));
      } catch (e) { /* 老浏览器忽略 */ }
      loadTeams();
    });
  }

  function bootByTemplate() {
    dbg('按赛事模板启动：template=' + state.template + ' edition=' + state.editionId);
    el.eventHead.hidden = false;
    el.needEvent.hidden = true;
    el.eventName.textContent = '正在加载赛事…';
    if (el.eventDesc) el.eventDesc.textContent = '正在读取赛制模板…';

    withTimeout(call('careerEvents'), REQ_TIMEOUT_MS, '赛制模板加载').then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '模板接口失败');
      state.templates = res.templates || [];
      fillTplSelect(state.templates);
      var tpl = findTemplate(state.template);
      if (!tpl) throw new Error('未知赛事模板：' + state.template);
      fillEditionSelect(tpl);
      if (state.edition) {
        applyEdition(state.edition);
      } else {
        // 兼容：后端没有届次数据时回退旧逻辑（按名称挑一个赛季）
        state.leagueId = pickLeagueOf(tpl);
        var dataName = leagueNameOf(tpl, state.leagueId);
        state.event = {
          name: tpl.name,
          desc: (tpl.desc || '') + (dataName ? '（BP 数据取自' + dataName + '）' : ''),
          format_desc: (tpl.desc || '') + (dataName ? '（BP 数据取自' + dataName + '）' : ''),
          team_count: tpl.team_count,
          short_name: tpl.name,
          category: tpl.category
        };
        renderEventHead();
        if (el.formNo) el.formNo.textContent = 'NO.------' + String(tpl.team_count || 0);
      }
      if (el.tplPickHint) {
        el.tplPickHint.textContent = '当前：' + ((state.event && state.event.name) || '') +
          '。切换赛事 / 届次会重新加载赛制与战队列表。';
      }
      if (!state.leagueId) {
        throw new Error('该赛事没有可用的赛季数据，请先运行 scraper.py');
      }
      loadTeams();
    }).catch(function (err) {
      var msg = (err && err.message) ? err.message : String(err);
      warn('赛制模板加载失败：' + msg);
      el.eventName.textContent = '赛事加载失败';
      if (el.eventDesc) el.eventDesc.textContent = msg;
      toast('赛事加载失败：' + msg);
    });

    loadCoachTip();
    syncCustom();
  }

function renderEventGroups(data) {
    var groups = (data && data.groups) || {};
    var cats = (data && data.categories) ||
      { league: '联赛', cup: '杯赛', package: '年度赛程包' };
    var html = '';

    ['league', 'cup', 'package'].forEach(function (key) {
      var rows = groups[key] || [];
      html += '<div class="c-modal-label">' + esc(cats[key] || key) + '</div>';
      if (!rows.length) {
        html += '<p class="c-modal-more">暂无可用赛事</p>';
      }
      rows.forEach(function (r) {
        var active = state.picks === String(r.league_id) ? ' is-active' : '';
        html += '<button type="button" class="c-event-opt' + active + '" data-lid="' + esc(r.league_id) + '">' +
          '<span class="c-eo-name">' + esc(r.short_name || r.name) + '</span>' +
          '<span class="c-eo-sub">' + esc(r.format_desc || (int(r.team_count) + ' 支战队')) + '</span>' +
          '<span class="c-eo-check" aria-hidden="true">✓</span>' +
        '</button>';
      });
      if (key !== 'package') {
        html += '<p class="c-modal-more">后续' + esc(cats[key] || key) + '敬请期待……</p>';
      }
    });

    el.eventGroups.innerHTML = html;

    Array.prototype.forEach.call(el.eventGroups.querySelectorAll('.c-event-opt'), function (btn) {
      btn.addEventListener('click', function () {
        state.picks = String(btn.getAttribute('data-lid'));
        renderEventGroups(state.events);
      });
    });
    syncEventConfirm();
  }

  function syncEventConfirm() {
    var ok = !!state.picks;
    el.btnEventConfirm.disabled = !ok;
    el.btnEventConfirm.setAttribute('aria-disabled', ok ? 'false' : 'true');
  }

  function openEventModal() {
    if (!el.eventModal) return;
    el.eventModal.hidden = false;
    loadEvents();
  }

  /**
   * v4 需求 1：加载赛事列表
   *  - 立刻给出「正在加载赛事列表...」文案（不再是无声骨架屏）
   *  - 20 秒超时 + 失败原因 + 「重试」按钮
   *  - 每一步都打 debug 日志，便于排查
   */
  function loadEvents() {
    if (!el.eventGroups) return;
    dbg('开始加载赛事列表…');
    el.eventGroups.innerHTML =
      '<div class="c-loading">' +
        '<span class="c-loading-dot" aria-hidden="true"></span>' +
        '<span>正在加载赛事列表…</span>' +
      '</div>' +
      '<div class="c-skel"></div><div class="c-skel"></div>';

    withTimeout(call('careerEvents'), REQ_TIMEOUT_MS, '赛事列表加载')
      .then(function (res) {
        if (!res || res.ok === false) {
          throw new Error((res && res.error) || '赛事接口返回失败');
        }
        state.templates = res.templates || [];
        var groups = hitTemplates(res.templates || []);
        res.groups = groups;
        state.events = res;
        var n = (groups.league || []).length + (groups.cup || []).length +
                (groups.package || []).length;
        dbg('赛事列表加载完成：' + n + ' 项', {
          league: (groups.league || []).length,
          cup: (groups.cup || []).length,
          default_league_id: res.default_league_id,
          skipped: res.skipped || []
        });
        if (!n) {
          el.eventGroups.innerHTML =
            '<div class="c-empty">后端没有返回任何赛事。请先运行 <code>python scraper.py</code> 抓取数据，然后重试。' +
            '<br><button type="button" class="btn btn-ghost btn-sm" id="btnRetryEvents">重试</button></div>';
          bindRetry();
          return;
        }
        if (!state.picks) {
          var pkg = (res.templates || []).filter(function (t) {
            return t.key === 'annual_package';
          })[0];
          state.picks = (pkg && pkg.default_edition_id) || String(res.default_league_id || '');
        }
        renderEventGroups(res);
      })
      .catch(function (err) {
        var msg = (err && err.message) ? err.message : String(err);
        warn('赛事列表加载失败：' + msg);
        el.eventGroups.innerHTML =
          '<div class="c-empty">赛事读取失败：' + esc(msg) +
          '<br><span class="c-empty-sub">生涯玩法本身没有坏，只是这次没读到赛事数据。</span>' +
          '<br><button type="button" class="btn btn-primary btn-sm" id="btnRetryEvents">重试</button></div>';
        bindRetry();
      });
  }

  function bindRetry() {
    var btn = byId('btnRetryEvents');
    if (btn) btn.addEventListener('click', loadEvents);
  }

  function closeEventModal() { if (el.eventModal) el.eventModal.hidden = true; }

  if (el.btnEventCancel) el.btnEventCancel.addEventListener('click', closeEventModal);
  if (el.btnOpenEvents) el.btnOpenEvents.addEventListener('click', openEventModal);
  if (el.btnEventConfirm) {
    el.btnEventConfirm.addEventListener('click', function () {
      if (!state.picks) { toast('请先选择一项赛事'); return; }
      var picked = null;
      (state.templates || []).forEach(function (t) {
        (t.editions || []).forEach(function (e) {
          if (String(e.edition_id) === String(state.picks)) {
            picked = { tpl: t.key, ed: e.edition_id };
          }
        });
      });
      window.location.href = picked
        ? ('/career/team?template=' + encodeURIComponent(picked.tpl) +
           '&edition=' + encodeURIComponent(picked.ed))
        : ('/career/team?template=' + encodeURIComponent(state.picks));
    });
  }

  /* ------------------------------------------------------------------ *
   * 战队网格
   * ------------------------------------------------------------------ */
  function teamCardHtml(t) {
    var active = state.selected && String(state.selected.name) === String(t.name) ? ' is-active' : '';
    var logo = t.logo_path
      ? '<img src="' + esc(t.logo_path) + '" alt="' + esc(t.name) + '" loading="lazy">'
      : '<span class="c-tc-name" aria-hidden="true">' + esc(String(t.name || '?').slice(0, 1)) + '</span>';
    var sub = '第 ' + int(t.rank) + ' 名 · 胜率 ' + pct(t.win_rate);
    var badge = isNum(t.win_rate) ? '<span class="c-tc-badge">' + Math.round(t.win_rate) + '</span>' : '';
    return '<button type="button" class="c-team-card' + active + '" data-team="' + esc(t.name) + '">' +
      badge + logo +
      '<span class="c-tc-name">' + esc(t.name) + '</span>' +
      '<span class="c-tc-sub">' + esc(sub) + '</span>' +
    '</button>';
  }

  function renderTeams() {
    if (!state.teams.length) {
      el.teamGrid.innerHTML = '<div class="c-empty">该赛事没有可用战队数据。</div>';
      return;
    }
    el.teamGrid.innerHTML = state.teams.map(teamCardHtml).join('');
    Array.prototype.forEach.call(el.teamGrid.querySelectorAll('.c-team-card'), function (btn) {
      btn.addEventListener('click', function () {
        state.selected = { name: btn.getAttribute('data-team') };
        renderTeams();
        syncPickBtn();
      });
    });
  }

  function syncPickBtn() {
    if (!el.btnPickTeam) return;
    var ok = !!(state.selected && state.selected.name) && !state.submitting;
    el.btnPickTeam.disabled = !ok;
    el.btnPickTeam.setAttribute('aria-disabled', ok ? 'false' : 'true');
    el.btnPickTeam.textContent = ok ? ('选择「' + state.selected.name + '」') : '选择一支战队';
  }

  function renderEventHead() {
    var ev = state.event || {};
    el.topTitle.textContent = ev.category === 'cup' ? '冠军杯' : '年总模式';
    el.eventName.textContent = ev.short_name || ev.name || '—';
    el.eventDesc.textContent = ev.format_desc || (int(ev.team_count) + ' 支战队');
    el.ruleLeague.textContent = ev.short_name || ev.name || '该赛事';
    el.ruleLeague2.textContent = ev.short_name || ev.name || '该赛事';
    el.customLeague.textContent = ev.short_name || ev.name || '—';
    el.customScale.textContent = int(ev.team_count) + ' 支战队';
  }

  /* 教练档案提示条：只有真实统计，不编造「全服 N 冠」 */
  function loadCoachTip() {
    call('careerSaves').then(function (res) {
      if (!res || res.ok === false) return;
      var coach = res.coach || {};
      var saves = res.saves || [];
      var teams = {};
      saves.forEach(function (s) {
        var tn = (s.coach && s.coach.team_name) || '';
        if (tn) teams[tn] = 1;
      });
      var n = Object.keys(teams).length;
      var titles = isNum(coach.total_titles) ? coach.total_titles : 0;
      el.coachTip.innerHTML = '你带领过 <b>' + int(n) + '</b> 支战队，夺得了 <b>' +
        int(titles) + '</b> 冠，目前';
      el.coachTier.textContent = coach.tier || '无名之辈';
    }).catch(function () { /* 静默：提示条不影响主流程 */ });
  }

  /* ------------------------------------------------------------------ *
   * 自建战队
   * ------------------------------------------------------------------ */
  function syncCustom() {
    var v = String(el.customName.value || '').trim();
    el.nameCount.textContent = String(v.length);
    var ok = v.length >= 1 && v.length <= 5 && !state.submitting;
    el.btnCustomSubmit.disabled = !ok;
    el.btnCustomSubmit.setAttribute('aria-disabled', ok ? 'false' : 'true');
  }

  function showCustom() {
    el.teamGridView.hidden = true;
    el.bottomBar.hidden = true;
    el.customView.hidden = false;
    el.customName.focus();
    syncCustom();
  }
  function hideCustom() {
    el.customView.hidden = true;
    el.teamGridView.hidden = false;
    el.bottomBar.hidden = false;
    syncPickBtn();
  }

  if (el.btnCustomToggle) el.btnCustomToggle.addEventListener('click', showCustom);
  if (el.btnCustomBack) el.btnCustomBack.addEventListener('click', hideCustom);
  if (el.customName) el.customName.addEventListener('input', syncCustom);

  /* ------------------------------------------------------------------ *
   * 创建生涯
   * ------------------------------------------------------------------ */
  function createCareer(teamMode, teamName) {
    if (state.submitting) return;
    state.submitting = true;
    syncPickBtn();
    syncCustom();

    var btn = teamMode === 'custom' ? el.btnCustomSubmit : el.btnPickTeam;
    var oldText = btn ? btn.textContent : '';
    if (btn) { btn.textContent = '正在创建生涯…'; btn.disabled = true; }
    dbg('创建生涯：team_mode=' + teamMode + ' league_id=' + state.leagueId);

    withTimeout(call('careerNew', {
      template: state.template || 'spring',
      edition_id: state.editionId || undefined,
      league_id: state.leagueId,
      team_mode: teamMode,
      team_name: teamName,
      coach_name: '教练',
      save_name: buildSaveName(teamName)
    }), REQ_TIMEOUT_MS, '创建生涯').then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '创建生涯失败');
      var sid = res.save_id || (res.state && res.state.save && res.state.save.save_id);
      if (!sid) throw new Error('后端未返回 save_id');
      window.location.href = '/career/draw?save_id=' + encodeURIComponent(sid);
    }).catch(function (err) {
      state.submitting = false;
      if (btn) { btn.textContent = oldText; btn.disabled = false; }
      syncPickBtn();
      syncCustom();
      warn('创建生涯失败：' + (err && err.message ? err.message : String(err)));
      toast('创建生涯失败：' + (err && err.message ? err.message : String(err)));
    });
  }

  if (el.btnPickTeam) {
    el.btnPickTeam.addEventListener('click', function () {
      if (!state.selected || !state.selected.name) { toast('请先选择一支战队'); return; }
      createCareer('existing', state.selected.name);
    });
  }
  if (el.btnCustomSubmit) {
    el.btnCustomSubmit.addEventListener('click', function () {
      var v = String(el.customName.value || '').trim();
      if (!v) { toast('请填写战队名称'); el.customName.focus(); return; }
      if (v.length > 5) { toast('战队名称最长 5 个字'); return; }
      createCareer('custom', v);
    });
  }

  /* ------------------------------------------------------------------ *
   * 启动
   * ------------------------------------------------------------------ */
  function boot() {
    state.template = queryTemplate();
    state.editionId = queryEdition();
    if (!state.template && state.editionId) {
      state.template = String(state.editionId).split(':')[0];
    }
    state.leagueId = queryLeagueId();
    /* v7 需求 1：进页面就有赛事下拉；没带 template 参数时默认「年度赛程包」，
     * 不再卡在空白「请选择赛事」弹窗上（这是之前『点进来选不了赛事』的根因）。 */
    if (!state.template) {
      state.template = 'annual_package';
      dbg('未指定赛事模板，默认年度赛程包');
    }
    bootByTemplate();
    return;
    el.eventHead.hidden = false;
    el.needEvent.hidden = true;

    /* 赛事元信息 + 战队表并行拉取 */
    withTimeout(call('careerEvents'), REQ_TIMEOUT_MS, '赛事信息加载').then(function (res) {
      var rows = (res && res.leagues) || [];
      var hit = null;
      for (var i = 0; i < rows.length; i++) {
        if (String(rows[i].league_id) === String(state.leagueId)) { hit = rows[i]; break; }
      }
      if (!hit) throw new Error('未知赛事：' + state.leagueId);
      state.event = hit;
      renderEventHead();
      /* 编号格式对齐设计稿 NO.2026-18（年份-参赛规模），不编造额外含义 */
      var year = String(hit.name || '').match(/(\d{4})/);
      el.formNo.textContent = 'NO.' + (year ? year[1] : '----') + '-' + String(hit.team_count || 0);
    }).catch(function (err) {
      toast('赛事信息读取失败：' + (err && err.message ? err.message : String(err)));
      el.eventName.textContent = state.leagueId;
    });

    loadTeams();

    loadCoachTip();
    syncCustom();
  }

  function loadTeams() {
    dbg('开始加载战队列表：league_id=' + state.leagueId);
    el.teamGrid.innerHTML = '<div class="c-loading">' +
      '<span class="c-loading-dot" aria-hidden="true"></span><span>正在加载战队列表…</span></div>';
    withTimeout(call('careerTeams', state.leagueId), REQ_TIMEOUT_MS, '战队列表加载')
      .then(function (res) {
        if (!res || res.ok === false) throw new Error((res && res.error) || '战队接口失败');
        state.teams = res.teams || [];
        dbg('战队列表加载完成：' + state.teams.length + ' 支');
        renderTeams();
        syncPickBtn();
      })
      .catch(function (err) {
        var msg = (err && err.message) ? err.message : String(err);
        warn('战队列表加载失败：' + msg);
        el.teamGrid.innerHTML = '<div class="c-empty">战队读取失败：' + esc(msg) +
          '<br><button type="button" class="btn btn-ghost btn-sm" id="btnRetryTeams">重试</button></div>';
        var b = byId('btnRetryTeams');
        if (b) b.addEventListener('click', loadTeams);
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
