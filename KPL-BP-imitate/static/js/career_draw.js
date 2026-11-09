/* ============================================================
   career_draw.js — 分组抽签动画（契约 v3 §5.4）
   路由：/career/draw?save_id=xxx
   数据源：GET /api/career/state?save_id=
   draw_order / groups 全部来自后端存档，前端只做「逐个揭示」动画。
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
  function toast(msg) {
    if (typeof KPL.toast === 'function') { try { return KPL.toast(msg); } catch (e) {} }
    if (window.console && console.warn) console.warn('[kpl]', msg);
  }

  var el = {
    drawStage: byId('drawStage'),
    drawTitle: byId('drawTitle'),
    drawCount: byId('drawCount'),
    drawHero: byId('drawHero'),
    drawGroup: byId('drawGroup'),
    groupWrap: byId('groupWrap'),
    groupGrid: byId('groupGrid'),
    btnSkipDraw: byId('btnSkipDraw'),
    drawDone: byId('drawDone'),
    drawDoneSub: byId('drawDoneSub'),
    doneWrap: byId('doneWrap'),
    doneGrid: byId('doneGrid'),
    btnEnterSeason: byId('btnEnterSeason'),
    drawError: byId('drawError'),
    drawErrorText: byId('drawErrorText')
  };

  if (!el.drawStage) return;

  var REVEAL_MS = 620;

  var state = {
    saveId: null,
    save: null,
    order: [],
    groups: [],
    logos: {},     /* 队名 → logo_path */
    myTeam: null,
    cursor: 0,
    timer: null,
    finished: false
  };

  function api() { return KPL.api || null; }
  function call(name, a) {
    var a2 = api();
    if (!a2 || typeof a2[name] !== 'function') return Promise.reject(new Error('接口不可用：' + name));
    try { return Promise.resolve(a2[name](a)); }
    catch (e) { return Promise.reject(e); }
  }

  function querySaveId() {
    try { return new URLSearchParams(window.location.search).get('save_id') || null; }
    catch (e) { return null; }
  }

  function logoOf(name) {
    var p = state.logos[name];
    if (!p) return '';
    return '<img src="' + esc(p) + '" alt="' + esc(name) + '" loading="lazy">';
  }

  /* ------------------------------------------------------------------ *
   * 分组列渲染
   * ------------------------------------------------------------------ */
  function renderGroups(container, { revealCount, doneMode }) {
    if (!state.groups.length) {
      /* 杯赛无分组：单列展示种子序 */
      var rows = [];
      for (var i = 0; i < state.order.length; i++) {
        if (!doneMode && i >= revealCount) break;
        var tm = state.order[i].team;
        var me = tm === state.myTeam;
        rows.push('<div class="c-group-item' + (me ? ' is-me' : '') + '">' +
          logoOf(tm) + '<span class="c-gi-name">' + esc(tm) + '</span></div>');
      }
      var total = state.order.length;
      if (!doneMode) {
        for (var k = revealCount; k < total; k++) {
          rows.push('<div class="c-group-item is-empty">空位</div>');
        }
      }
      container.className = '';
      container.innerHTML = '<div class="c-group-col" style="margin-bottom:10px;">' +
        '<h4>参赛战队</h4>' + rows.join('') + '</div>';
      return;
    }

    container.className = 'c-group-grid';
    container.innerHTML = state.groups.map(function (g) {
      var items = [];
      var revealed = 0;
      (g.teams || []).forEach(function (tm) {
        var r = state.order.findIndex(function (o) { return o.team === tm; });
        var shown = doneMode || (r >= 0 && r < revealCount);
        if (shown) revealed++;
        if (!shown) {
          items.push('<div class="c-group-item is-empty">空位</div>');
          return;
        }
        var me = tm === state.myTeam;
        items.push('<div class="c-group-item' + (me ? ' is-me' : '') + '">' +
          logoOf(tm) + '<span class="c-gi-name">' + esc(tm) + '</span></div>');
      });
      return '<div class="c-group-col"><h4>' + esc(g.name) + '</h4>' + items.join('') + '</div>';
    }).join('');
  }

  /* ------------------------------------------------------------------ *
   * 逐个揭示
   * ------------------------------------------------------------------ */
  function revealNext() {
    if (state.cursor >= state.order.length) { finishDraw(); return; }
    var item = state.order[state.cursor];
    state.cursor += 1;

    el.drawCount.textContent = '第 ' + state.cursor + ' / ' + state.order.length + ' 签';
    el.drawHero.innerHTML =
      logoOf(item.team) +
      '<div class="c-dh-name">' + esc(item.team) + '</div>';
    el.drawGroup.textContent = item.group || item.label || '参赛';
    renderGroups(el.groupGrid, { revealCount: state.cursor, doneMode: false });

    /* 自动滚动到「我的战队」那一签后暂停一下 */
    if (state.cursor >= state.order.length) {
      state.timer = setTimeout(finishDraw, REVEAL_MS + 160);
    } else {
      state.timer = setTimeout(revealNext, REVEAL_MS);
    }
  }

  function finishDraw() {
    if (state.finished) return;
    state.finished = true;
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }

    el.drawStage.hidden = true;
    el.drawDone.hidden = false;

    var myGroup = null;
    for (var i = 0; i < state.order.length; i++) {
      if (state.order[i].team === state.myTeam) { myGroup = state.order[i].group; break; }
    }
    if (state.kind === 'stage') {
      /* v28：站内淘汰赛抽签 —— 只说分在哪半区，没出线就说没进 */
      var stLabel = (state.save && state.save.stage_label) || '本阶段';
      el.drawDoneSub.innerHTML = myGroup
        ? ('你的战队分在 <b>' + esc(myGroup) + '</b>')
        : ('你的战队未进入 <b>' + esc(stLabel) + '</b>');
    } else {
      el.drawDoneSub.innerHTML = myGroup
        ? ('你的战队进入 <b>' + esc(myGroup) + '</b>')
        : ('你的战队 <b>' + esc(state.myTeam || '—') + '</b> 已就位');
    }

    renderGroups(el.doneGrid, { revealCount: state.order.length, doneMode: true });
    el.btnEnterSeason.disabled = false;
  }

  if (el.btnSkipDraw) {
    el.btnSkipDraw.addEventListener('click', function () {
      if (state.timer) { clearTimeout(state.timer); state.timer = null; }
      state.cursor = state.order.length;
      finishDraw();
    });
  }

  if (el.btnEnterSeason) {
    el.btnEnterSeason.addEventListener('click', function () {
      window.location.href = '/career/season?save_id=' + encodeURIComponent(state.saveId);
    });
  }

  function showError(msg) {
    el.drawStage.hidden = true;
    el.drawDone.hidden = true;
    el.drawError.hidden = false;
    el.drawErrorText.innerHTML = esc(msg) +
      '<br><a class="c-btn c-btn-ghost c-btn-sm" href="/career" style="margin-top:14px;">返回生涯首页</a>';
  }

  /* ------------------------------------------------------------------ *
   * 启动
   * ------------------------------------------------------------------ */
  function boot() {
    state.saveId = querySaveId();
    if (!state.saveId) { showError('缺少 save_id，请从生涯首页重新进入。'); return; }

    call('careerState', state.saveId).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '存档读取失败');
      /* 契约 v3 §4：/api/career/state 返回 {ok, save, plan, schedule, standings,
         bracket, rules, draw_order, groups}。save 是 summary（不含 pool/draw_order），
         draw_order/groups 在顶层。 */
      var save = res.save || null;
      if (!save) throw new Error('后端未返回存档');

      state.save = save;
      /* v28：优先用后端 draw_view（支持「站内淘汰赛 8 队抽签」，kind='stage'） */
      var dv = res.draw_view || null;
      state.order = Array.isArray(dv && dv.order) && dv.order.length
        ? dv.order
        : (Array.isArray(res.draw_order) ? res.draw_order : []);
      state.groups = Array.isArray(dv && dv.groups) && dv.groups.length
        ? dv.groups
        : (Array.isArray(res.groups) ? res.groups : []);
      state.kind = (dv && dv.kind) || 'series';
      state.myTeam = (save.coach && save.coach.team_name) || null;

      /* 队徽：从 schedule 的真实对阵里收集（blue_logo / red_logo），不编造 */
      var sched = res.schedule || {};
      var pool = [];
      (sched.matches || []).forEach(function (m) {
        pool.push({ name: m.blue_team, logo_path: m.blue_logo });
        pool.push({ name: m.red_team, logo_path: m.red_logo });
      });
      pool.forEach(function (t) {
        if (t && t.name && t.logo_path) state.logos[t.name] = t.logo_path;
      });

      var ev = save.event || {};
      el.drawTitle.textContent = (dv && dv.title)
        || ((ev.short_name || ev.name || '') + ' · 分组抽签');

      if (!state.order.length) {
        showError('该存档没有抽签数据，请返回生涯首页重新开启。');
        return;
      }

      el.btnEnterSeason.disabled = true;
      el.drawCount.textContent = '第 1 / ' + state.order.length + ' 签';
      renderGroups(el.groupGrid, { revealCount: 0, doneMode: false });
      state.timer = setTimeout(revealNext, 320);
    }).catch(function (err) {
      showError('存档读取失败：' + (err && err.message ? err.message : String(err)));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
