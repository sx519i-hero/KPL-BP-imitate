/* ============================================================
   llm.js — 大模型 AI 对战入口（单局 / 联赛）
   ------------------------------------------------------------
   · 单局：直接 /api/bp/new { ai_engine: 'llm' | 'builtin' } 开局，
     然后跳到 /arena?match_id=…；轮到大模型时后端调用
     game/llm_coach.py，失败自动回退内置 AI。
   · 联赛：走年总模式（/career/team?league_id=&llm_scope=），
     存档里记录 llm_scope，赛季进程页每次开局按它决定 ai_engine。
   · 没配置 API 时点开局会拿到后端 400，这里原样展示文案，不卡死。
   ============================================================ */
(function () {
  'use strict';

  var KPL = (window.KPL = window.KPL || {});
  var U = KPL.util || {};
  var esc = U.esc || function (s) { return String(s == null ? '' : s); };
  function toast(msg) {
    if (typeof KPL.toast === 'function') { try { return KPL.toast(msg); } catch (e) {} }
    if (window.console && console.warn) console.warn('[kpl]', msg);
  }

  var S = { mode: 'single', status: null, leagues: [], teams: [], templates: [], template: 'spring', leagueId: '' };

  function byId(id) { return document.getElementById(id); }
  function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  var el = {
    llmBadge: byId('llmBadge'),
    statusDot: byId('statusDot'),
    statusTitle: byId('statusTitle'),
    statusDesc: byId('statusDesc'),
    modeSeg: byId('modeSeg'),
    singleCard: byId('singleCard'),
    leagueCard: byId('leagueCard'),
    smLeague: byId('smLeague'),
    smBo: byId('smBo'),
    smSide: byId('smSide'),
    smEngine: byId('smEngine'),
    smDiff: byId('smDiff'),
    btnStartSingle: byId('btnStartSingle'),
    singleStatus: byId('singleStatus'),
    lgTemplate: byId('lgTemplate'),
    lgEdition: byId('lgEdition'),
    lgTeamName: byId('lgTeamName'),
    lgSaveName: byId('lgSaveName'),
    leagueStatus: byId('leagueStatus'),
    btnStartLeague: byId('btnStartLeague'),
    llmSaveList: byId('llmSaveList'),
    llmSavesCount: byId('llmSavesCount'),
    btnRefreshLlmSaves: byId('btnRefreshLlmSaves'),
    llmLog: byId('llmLog')
  };

  function log(msg) {
    if (window.console && console.info) console.info('[llm]', msg);
  }

  /* ---------------------------- 状态检测 ---------------------------- */
  function renderStatus() {
    var st = S.status || {};
    var ready = st.ready === true;
    el.statusDot.className = 'v4-status-dot' + (ready ? ' is-ok' : ' is-warn');
    el.llmBadge.textContent = ready ? '已配置' : '未配置';
    el.llmBadge.className = 'v4-topbar-sub' + (ready ? ' is-ok' : ' is-warn');
    if (ready) {
      el.statusTitle.textContent = '大模型已就绪';
      el.statusDesc.textContent = '模型 ' + (st.model || '—') + ' · 接口 ' + (st.endpoint || '—');
    } else {
      el.statusTitle.textContent = '大模型尚未启用';
      var miss = (st.missing || []).join('、') || '配置不完整';
      el.statusDesc.textContent = '缺少：' + miss + '。可以先点右上角「去设置」配置，或直接把对手切换成内置 AI。';
    }
  }

  function loadStatus() {
    var fn = KPL.api && KPL.api.llmStatus;
    if (typeof fn !== 'function') { renderStatus(); return; }
    fn().then(function (res) {
      S.status = (res && res.llm) || null;
      renderStatus();
      log('llm status: ' + JSON.stringify(S.status));
    }).catch(function (err) {
      S.status = { ready: false, missing: ['无法读取配置（' + (err && err.message ? err.message : '网络错误') + '）'] };
      renderStatus();
    });
  }

  /* ---------------------------- 数据加载 ---------------------------- */
  function fillLeagues(select, rows) {
    var html = '';
    rows.forEach(function (r) {
      html += '<option value="' + esc(r.league_id) + '">' + esc(r.name) + '</option>';
    });
    select.innerHTML = html;
    var def = (KPL.getLeagueId && KPL.getLeagueId()) || '';
    if (def) select.value = def;
  }

  function loadTeams(leagueId) {
    if (typeof KPL.api.teams !== 'function') return Promise.resolve([]);
    return KPL.api.teams(leagueId).then(function (res) {
      var rows = (res && (res.teams || res)) || [];
      S.teams = Array.isArray(rows) ? rows : [];
      return S.teams;
    }).catch(function () { S.teams = []; return []; });
  }

  function refreshTeams() {
    var lid = el.smLeague.value;
    if (!lid) return Promise.resolve();
    return loadTeams(lid).then(function () {
      if (el.lgLeague) el.lgLeague.value = lid;
    });
  }

  /* ---------------------------- 开始动作 ---------------------------- */
  function startSingle() {
    var engine = el.smEngine.value;
    if (engine === 'llm' && (!S.status || S.status.ready !== true)) {
      var msg = '请先在「设置」里配置大模型 API（接口地址 + 密钥）';
      el.singleStatus.textContent = msg;
      toast(msg);
      return;
    }
    el.btnStartSingle.disabled = true;
    el.btnStartSingle.textContent = '正在开局…';
    el.singleStatus.textContent = '';
    // v47：不再让你挑两个 KPL 战队 —— 你执一边，另一边固定叫「DeepSeek」
    var me = '你';
    var side = el.smSide.value === 'red' ? 'red' : 'blue';
    var payload = {
      mode: 'ai',
      league_id: el.smLeague.value,
      blue_team: side === 'blue' ? me : 'DeepSeek',
      red_team: side === 'red' ? me : 'DeepSeek',
      bo: parseInt(el.smBo.value, 10) || 1,
      side: side,
      difficulty: el.smDiff.value,
      ai_engine: engine
    };
    log('开局：' + JSON.stringify(payload));
    KPL.api.bpNew(payload).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '开局失败');
      var mid = res.match_id || (res.match && res.match.match_id);
      if (!mid) throw new Error('后端未返回 match_id');
      window.location.href = '/arena?match_id=' + encodeURIComponent(mid);
    }).catch(function (err) {
      el.btnStartSingle.disabled = false;
      el.btnStartSingle.textContent = '开始单局';
      var msg = (err && err.message) ? err.message : '开局失败';
      el.singleStatus.textContent = msg;
      toast(msg);
      log('开局失败：' + msg);
    });
  }

  /* v6：大模型联赛 = 玩家 1 队 + DeepSeek 1 队 + 其余内置 AI，直接建在统一赛程引擎上 */
  /* v31：届次（哪一年哪一届）→ 官方赛事名 + 该年数据 */
  function currentEdition() {
    var list = (S.editions && S.editions[el.lgTemplate.value]) || [];
    var want = el.lgEdition ? el.lgEdition.value : '';
    var hit = null;
    list.forEach(function (e) { if (e.edition_id === want) hit = e; });
    return hit || list[0] || null;
  }

  function fillEditions() {
    if (!el.lgEdition) return;
    var tpl = el.lgTemplate ? el.lgTemplate.value : '';
    var list = (S.editions && S.editions[tpl]) || [];
    el.lgEdition.innerHTML = list.map(function (e) {
      var label = e.name || '';
      var stations = (e.stations || []).map(function (st) {
        return st && st.label ? st.label : '';
      }).filter(Boolean);
      if (stations.length) label += '（' + stations.join(' → ') + '）';
      return '<option value="' + esc(e.edition_id) + '">' + esc(label) + '</option>';
    }).join('') || '<option value="">（无可用届次）</option>';
  }

  /* ------------------ v41：继续上次的大模型联赛（只列 mode=llm） ------------------ */
  function loadLlmSaves() {
    if (!el.llmSaveList) return;
    fetch('/api/tourney/saves?mode=llm', { method: 'GET' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var rows = (res && res.saves) || [];
        if (el.llmSavesCount) el.llmSavesCount.textContent = rows.length ? rows.length + ' 个' : '暂无';
        if (!rows.length) {
          el.llmSaveList.innerHTML = '<li class="v4-hint">还没有大模型联赛存档：上面开一局就会自动生成。</li>';
          return;
        }
        el.llmSaveList.innerHTML = rows.map(function (row) {
          var tag = (row.status === 'done') ? '已结束' : (row.stage_label || '进行中');
          return '<li class="v4-save">' +
            '<div class="v4-save-main">' +
            '<strong>' + esc(row.save_name || row.save_id) + '</strong>' +
            '<span class="v4-hint">' + esc((row.event && row.event.name) || row.template || '') +
            ' · ' + esc(tag) + ' · 已打 ' + esc(row.matches_done == null ? 0 : row.matches_done) +
            ' / ' + esc(row.matches_total == null ? 0 : row.matches_total) + ' 场 · 进度 ' +
            esc(row.progress == null ? 0 : row.progress) + '%</span>' +
            '</div>' +
            '<button type="button" class="btn btn-primary btn-sm" data-llm-save="' + esc(row.save_id) + '"' +
            (row.status === 'done' ? ' disabled' : '') + '>继续执教</button>' +
            '</li>';
        }).join('');
        Array.prototype.forEach.call(el.llmSaveList.querySelectorAll('[data-llm-save]'), function (btn) {
          btn.addEventListener('click', function () {
            var sid = btn.getAttribute('data-llm-save');
            log('继续大模型联赛存档 → ' + sid);
            window.location.href = '/career/season?save_id=' + encodeURIComponent(sid);
          });
        });
      })
      .catch(function (err) {
        console.warn('[llm] 大模型存档读取失败', err);
        if (el.llmSavesCount) el.llmSavesCount.textContent = '读取失败';
      });
  }

  function startLeague() {
    var tpl = el.lgTemplate.value || 'spring';
    var ed = currentEdition();
    // v47：玩家只能用自己起的名字（不能用 KPL 战队），大模型固定叫 DeepSeek
    var custom = (el.lgTeamName.value || '').trim();
    if (!custom) { toast('请先填你的战队名（不能用 KPL 战队名）'); if (el.lgTeamName) el.lgTeamName.focus(); return; }
    if (custom.length > 5) { toast('战队名称最多 5 个字'); return; }
    if (custom === 'DeepSeek') { toast('这个队名留给大模型，请换一个'); return; }
    var sname = el.lgSaveName && el.lgSaveName.value ? el.lgSaveName.value.trim() : '';
    if (!sname) sname = custom + ' · 大模型联赛';
    console.info('[llm] 大模型联赛存档名 → ' + sname);
    var body = {
      mode: 'llm', template: tpl, league_id: S.leagueId || undefined,
      edition_id: ed ? ed.edition_id : undefined,
      team_mode: 'custom',
      team_name: custom,
      save_name: sname
    };
    el.btnStartLeague.disabled = true;
    el.btnStartLeague.textContent = '正在生成赛程…';
    if (el.leagueStatus) el.leagueStatus.textContent = '正在抽签并生成完整赛程，请稍候…';
    log('创建大模型联赛：' + JSON.stringify(body));
    fetch('/api/tourney/new', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || res.j.ok === false) throw new Error((res.j && res.j.error) || '创建失败');
        var sid = res.j.save_id || (res.j.state && res.j.state.save_id);
        log('赛程已创建：' + sid);
        window.location.href = '/career/season?save_id=' + encodeURIComponent(sid);
      })
      .catch(function (err) {
        el.btnStartLeague.disabled = false;
        el.btnStartLeague.textContent = '开启大模型联赛';
        var msg = (err && err.message) ? err.message : '创建失败';
        if (el.leagueStatus) el.leagueStatus.textContent = msg;
        toast(msg);
        log('创建失败：' + msg);
      });
  }

  /* 模板 → 战队列表（后端会按模板性质挑赛季，这里只为下拉框取名称） */
  function refreshLeagueTeams() {
    var tpl = el.lgTemplate.value || 'spring';
    S.template = tpl;
    var row = null;
    S.templates.forEach(function (t) { if (t.key === tpl) row = t; });
    if (!row) return;
    var hint = tpl === 'challenger' ? '挑战者杯'
      : (tpl === 'annual_finals' ? '年度总决赛' : '春季赛');
    var ed = currentEdition();
    var lid = ed && ed.league_id ? ed.league_id : '';
    if (!lid) {
      var pool = (row.leagues || []).filter(function (l) { return (l.name || '').indexOf(hint) >= 0; });
      if (!pool.length) pool = (row.leagues || []);
      lid = pool.length ? pool[0].league_id : '';
    }
    S.leagueId = lid;
    if (!lid) return;
    var fn = KPL.api && KPL.api.teams;
    if (typeof fn !== 'function') return;
    fn(lid).then(function (res) {
      var rows = (res && (res.teams || res)) || [];
      S.teams = Array.isArray(rows) ? rows : [];
    }).catch(function (err) { log('战队列表读取失败：' + (err && err.message ? err.message : '网络错误')); });
  }

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ---------------------------- 绑定 / 启动 ---------------------------- */
  function bindEvents() {
    $$('#modeSeg .v4-seg-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        S.mode = btn.getAttribute('data-mode');
        $$('#modeSeg .v4-seg-btn').forEach(function (b) { b.classList.toggle('is-active', b === btn); });
        el.singleCard.hidden = S.mode !== 'single';
        el.leagueCard.hidden = S.mode !== 'league';
      });
    });
    el.smLeague.addEventListener('change', function () { refreshTeams(); });
    el.btnStartSingle.addEventListener('click', startSingle);
    el.btnStartLeague.addEventListener('click', startLeague);
    /* v8 需求 2：与好友 PK 面板一致的次级按钮 */
    ['btnBackHomeSingle', 'btnBackHomeLeague'].forEach(function (id) {
      var b = byId(id);
      if (b) b.addEventListener('click', function () { window.location.href = '/'; });
    });
    if (el.lgTemplate) el.lgTemplate.addEventListener('change', function () {
      fillEditions();
      refreshLeagueTeams();
    });
    if (el.btnRefreshLlmSaves) el.btnRefreshLlmSaves.addEventListener('click', loadLlmSaves);
    if (el.lgEdition) el.lgEdition.addEventListener('change', refreshLeagueTeams);
  }

  function loadTemplates() {
    fetch('/api/tourney/templates', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var rows = (res && res.templates) || [];
        S.templates = rows;
        S.editions = (res && res.editions) || {};
        if (!rows.length) return;
        if (el.lgTemplate) {
          el.lgTemplate.innerHTML = rows.map(function (t) {
            return '<option value="' + esc(t.key) + '">' + esc(t.name || t.key) +
              (t.category_label ? '（' + esc(t.category_label) + '）' : '') + '</option>';
          }).join('');
          el.lgTemplate.value = 'spring';
        }
        fillEditions();
        refreshLeagueTeams();
      })
      .catch(function (err) { log('赛事模板读取失败：' + (err && err.message ? err.message : '网络错误')); });
  }

  function boot() {
    bindEvents();
    loadStatus();
    var fn = KPL.api && KPL.api.leagues;
    if (typeof fn !== 'function') return;
    fn().then(function (res) {
      var rows = (res && (res.leagues || res)) || [];
      S.leagues = Array.isArray(rows) ? rows : [];
      fillLeagues(el.smLeague, S.leagues);
      return refreshTeams();
    }).catch(function (err) {
      log('赛事列表读取失败：' + (err && err.message ? err.message : '网络错误'));
    });
    loadTemplates();
    loadLlmSaves();
    log('大模型 AI 对战页已就绪');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.KPL_LLM = { state: S, reload: loadStatus };
})();
