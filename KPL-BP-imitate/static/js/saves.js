/* ==========================================================================
 * 存档页（saves.html）—— v47
 * 「存档」从「我的」里独立出来，底部导航第二格进入。
 * 只做四件事：按分类列存档、继续执教、重命名、删除。
 * 数据全部来自后端 /api/tourney/saves，不编造任何数字。
 * ========================================================================== */
(function (global) {
  'use strict';

  var KPL = global.KPL;
  var document = global.document;
  if (!KPL) return;

  var $ = function (sel) { return document.querySelector(sel); };
  var el = {
    wrap: document.getElementById('currentWrap'),
    filter: document.getElementById('saveFilter'),
    newBtn: document.getElementById('btnNewCareer')
  };
  if (!el.wrap) return;

  var state = { saves: [], filter: 'all' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }
  function int(v) { return (v === null || v === undefined || isNaN(v)) ? '0' : String(Math.round(v)); }
  function pctText(v) { return (v === null || v === undefined || isNaN(v)) ? '—' : (Math.round(v * 10) / 10) + '%'; }
  function toast(msg) { if (KPL.toast) KPL.toast(msg); }

  var CAT_LABEL = { league: '联赛', cup: '杯赛', package: '赛程包' };

  function cardHtml(save) {
    var ev = save.event || {};
    var st = save.stats || {};
    var done = save.status === 'done';
    var stageLabel = save.stage_label || '未开始';
    var progress = (save.progress === null || save.progress === undefined) ? 0 : Math.max(0, Math.min(100, save.progress));
    var dayText = (save.current_day && save.total_days) ? (save.current_day + '/' + save.total_days + ' 比赛日') : '';
    var teamName = (save.coach && save.coach.team_name) || '未命名战队';
    var target = '/career/season?save_id=' + encodeURIComponent(save.save_id);
    var action = done ? (save.champion ? '🏆 ' + esc(save.champion) + ' 夺冠' : '赛季结束') : '继续执教';
    if (!save.drawn) { target = '/career/draw?save_id=' + encodeURIComponent(save.save_id); action = '去抽签'; }
    var history = done
      ? '<a class="c-btn c-btn-ghost c-btn-sm" href="/career/season?save_id=' +
        encodeURIComponent(save.save_id) + '&history=1">📜 结果回看</a>' : '';
    var catLabel = CAT_LABEL[ev.category] || '联赛';

    return '' +
      '<section class="c-card" data-save="' + esc(save.save_id) + '">' +
        '<div class="c-current-head">' +
          '<h2 class="c-card-title" style="margin:0;">' + catLabel + ' · ' +
            esc(save.save_name || ev.short_name || ev.name || '未命名存档') + '</h2>' +
          '<span class="c-link">' + esc(dayText) + '</span>' +
        '</div>' +
        '<h3 class="c-current-name">' + esc(teamName) + '</h3>' +
        '<div style="margin-bottom:8px;"><span class="c-tag">' + esc(stageLabel) + '</span></div>' +
        '<p class="c-current-league">｜' + esc(ev.short_name || ev.name || '') + '</p>' +
        '<div class="c-progress"><i style="width:' + progress + '%"></i></div>' +
        '<div class="c-current-foot">' +
          '<span class="c-record">战绩 ' + int(st.wins) + '胜' + int(st.losses) + '负 · 胜率 ' +
            pctText(st.win_rate) + '</span>' +
          history +
          '<a class="c-btn c-btn-primary c-btn-sm" href="' + target + '">' + action + ' ›</a>' +
        '</div>' +
        '<div class="c-save-actions">' +
          '<button type="button" class="c-btn c-btn-ghost c-btn-sm" data-act="rename" data-save="' +
            esc(save.save_id) + '">重命名</button>' +
          '<button type="button" class="c-btn c-btn-ghost c-btn-sm is-danger" data-act="delete" data-save="' +
            esc(save.save_id) + '">删除存档</button>' +
        '</div>' +
      '</section>';
  }

  function render() {
    var pool = state.saves.filter(function (s) {
      var c = (s.event || {}).category || 'league';
      return state.filter === 'all' || c === state.filter;
    });
    if (!state.saves.length) {
      el.wrap.innerHTML = '<section class="c-card"><div class="c-empty">' +
        '还没有存档。<br>点下面【去选赛事，开启新的年总赛程】开始一段新赛程。</div></section>';
      return;
    }
    if (!pool.length) {
      el.wrap.innerHTML = '<section class="c-card"><div class="c-empty">该分类下还没有存档。</div></section>';
      return;
    }
    var running = pool.filter(function (s) { return s.status !== 'done'; });
    var done = pool.filter(function (s) { return s.status === 'done'; });
    el.wrap.innerHTML = running.concat(done).map(cardHtml).join('');
    bindCards();
  }

  function bindCards() {
    Array.prototype.forEach.call(el.wrap.querySelectorAll('[data-act]'), function (btn) {
      btn.addEventListener('click', function () {
        var id = btn.getAttribute('data-save');
        var act = btn.getAttribute('data-act');
        if (act === 'rename') renameSave(id);
        else if (act === 'delete') deleteSave(id);
      });
    });
  }

  function load() {
    var fn = KPL.api && KPL.api.careerSaves;
    if (typeof fn !== 'function') {
      el.wrap.innerHTML = '<section class="c-card"><div class="c-empty">存档接口不可用。</div></section>';
      return;
    }
    Promise.resolve(fn()).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '读取失败');
      state.saves = res.saves || [];
      console.info('[saves] 已载入 ' + state.saves.length + ' 个存档');
      render();
    }).catch(function (err) {
      el.wrap.innerHTML = '<section class="c-card"><div class="c-empty">存档读取失败：' +
        esc(err && err.message ? err.message : String(err)) + '</div></section>';
    });
  }

  function renameSave(id) {
    var row = state.saves.filter(function (s) { return s.save_id === id; })[0] || {};
    var name = global.prompt('新的存档名称：', row.save_name || '');
    if (name === null) return;
    name = String(name).trim();
    if (!name) { toast('存档名称不能为空'); return; }
    Promise.resolve(KPL.api.tourneyRename(id, name)).then(function (res) {
      if (res && res.ok === false) throw new Error(res.error || '重命名失败');
      toast('存档已重命名');
      load();
    }).catch(function (err) { toast('重命名失败：' + (err && err.message ? err.message : String(err))); });
  }

  function deleteSave(id) {
    if (!global.confirm('删除后该赛程的抽签、比赛结果、年度积分都会丢失，确定删除？')) return;
    Promise.resolve(KPL.api.careerDelete(id)).then(function (res) {
      if (res && res.ok === false) throw new Error(res.error || '删除失败');
      toast('存档已删除');
      load();
    }).catch(function (err) { toast('删除失败：' + (err && err.message ? err.message : String(err))); });
  }

  if (el.filter) {
    el.filter.addEventListener('click', function (evt) {
      var btn = evt.target && evt.target.closest ? evt.target.closest('.c-filter-btn') : null;
      if (!btn) return;
      var cat = btn.getAttribute('data-cat') || 'all';
      if (cat === state.filter) return;
      state.filter = cat;
      Array.prototype.forEach.call(el.filter.querySelectorAll('.c-filter-btn'), function (b) {
        b.classList.toggle('is-active', b === btn);
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
      });
      render();
    });
  }

  if (el.newBtn) {
    el.newBtn.addEventListener('click', function () { global.location.href = '/career/team'; });
  }

  load();
})(window);
