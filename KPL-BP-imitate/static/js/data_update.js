/* v32-4：数据更新页 —— 显示当前数据概况 + 导入官方数据包 zip。 */
(function () {
  'use strict';

  var el = {
    info: document.getElementById('infoBox'),
    dir: document.getElementById('dataDir'),
    file: document.getElementById('packFile'),
    imp: document.getElementById('btnImport'),
    reload: document.getElementById('btnReload'),
    log: document.getElementById('duLog'),
    table: document.getElementById('leagueTable')
  };

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function say(text, kind) {
    if (!el.log) return;
    el.log.className = 'du-log' + (kind ? ' is-' + kind : '');
    el.log.textContent = text;
  }

  function renderInfo(info) {
    var rows = [
      ['英雄', (info.heroes || 0) + ' 个'],
      ['赛事', (info.leagues || 0) + ' 个'],
      ['数据时间', info.fetched_at || '未知'],
      ['可写', info.writable ? '可以导入' : '目录只读（请检查权限）']
    ];
    el.info.innerHTML = rows.map(function (r) {
      return '<span>' + esc(r[0]) + '</span><b>' + esc(r[1]) + '</b>';
    }).join('');
    el.dir.textContent = '数据目录：' + (info.data_dir || '—');
    renderTable(info);
  }

  function renderTable(info) {
    var ids = info.league_ids || [];
    if (!ids.length) {
      el.table.innerHTML = '<span class="du-badge">没有赛季数据</span>';
      return;
    }
    var names = info.league_names || {};
    var head = '<tr><th>赛季</th><th>英雄条目</th><th>选手</th><th>战队</th><th>BP 对局</th></tr>';
    var body = ids.map(function (id) {
      return '<tr><td>' + esc(names[id] || id) + '</td>' +
        '<td>' + esc((info.hero_stats || {})[id] == null ? '—' : (info.hero_stats || {})[id]) + '</td>' +
        '<td>' + esc((info.player_stats || {})[id] == null ? '—' : (info.player_stats || {})[id]) + '</td>' +
        '<td>' + esc((info.team_stats || {})[id] == null ? '—' : (info.team_stats || {})[id]) + '</td>' +
        '<td>' + esc((info.bp_history || {})[id] == null ? '—' : (info.bp_history || {})[id]) + '</td></tr>';
    }).join('');
    el.table.innerHTML = '<table class="du-table">' + head + body + '</table>';
  }

  function load() {
    KPL.api.get('/api/data/info').then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '读取失败');
      renderInfo(res);
    }).catch(function (err) {
      el.info.innerHTML = '<span>状态</span><b>读取失败：' + esc(err && err.message ? err.message : err) + '</b>';
    });
  }

  function doImport() {
    var file = el.file && el.file.files && el.file.files[0];
    if (!file) { say('请先选择一个 .zip 数据包', 'err'); return; }
    if (!/\.zip$/i.test(file.name)) { say('数据包必须是 .zip 格式', 'err'); return; }
    say('正在上传并校验 ' + file.name + ' …');
    el.imp.disabled = true;
    var form = new FormData();
    form.append('file', file);
    fetch('/api/data/import', { method: 'POST', body: form })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        var body = res.body || {};
        if (res.status >= 400 || body.ok === false) throw new Error(body.error || ('导入失败（HTTP ' + res.status + '）'));
        say('导入成功：英雄 ' + body.heroes + ' 个，赛事 ' + body.leagues + ' 个。\n' +
            '替换：' + (body.replaced || []).join('、') + '\n' +
            '旧数据已备份到：' + body.backup + '\n' +
            '刷新页面即可看到新赛季。', 'ok');
        load();
      })
      .catch(function (err) {
        say('导入失败：' + (err && err.message ? err.message : err), 'err');
      })
      .then(function () { el.imp.disabled = false; });
  }

  if (el.imp) el.imp.addEventListener('click', doImport);
  if (el.reload) el.reload.addEventListener('click', load);
  load();
})();

/* ------------------------------------------------------------------ */
/* v33：应用内一键抓取（带冷静期，防被官方限流）                        */
/* ------------------------------------------------------------------ */
(function () {
  var el = {
    btnRefresh: document.getElementById('btnRefresh'),
    btnStatus: document.getElementById('btnRefreshStatus'),
    badge: document.getElementById('refreshBadge'),
    log: document.getElementById('refreshLog'),
    coolText: document.getElementById('coolText')
  };
  if (!el.btnRefresh) return;
  var timer = null;

  function fmtSec(s) {
    s = Math.max(0, s | 0);
    if (s < 60) return s + ' 秒';
    return Math.ceil(s / 60) + ' 分钟';
  }

  function render(state) {
    if (!state) return;
    el.coolText.textContent = Math.round((state.min_interval || 1800) / 60);
    if (state.running) {
      el.badge.textContent = '抓取中…';
      el.badge.className = 'du-badge is-err';
      el.btnRefresh.disabled = true;
      el.btnRefresh.textContent = '正在抓取…';
    } else {
      el.btnRefresh.disabled = false;
      el.btnRefresh.textContent = '更新并抓取';
      if (state.ok === true) { el.badge.textContent = '已完成'; el.badge.className = 'du-badge is-ok'; }
      else if (state.ok === false) { el.badge.textContent = '失败'; el.badge.className = 'du-badge is-err'; }
      else { el.badge.textContent = '空闲'; el.badge.className = 'du-badge'; }
      if (state.cooldown > 0) {
        el.btnRefresh.textContent = '更新并抓取（冷静 ' + fmtSec(state.cooldown) + '）';
      }
    }
    var lines = (state.log || []).slice(-14);
    if (state.error && !state.running) lines = lines.concat(['错误：' + state.error]);
    if (lines.length) {
      el.log.textContent = lines.join('\n');
    }
    if (state.running && !timer) {
      timer = setInterval(poll, 1500);
    } else if (!state.running && timer) {
      clearInterval(timer);
      timer = null;
      var rb = document.getElementById('btnReload');
      if (rb) rb.click();
    }
  }

  function poll() {
    KPL.api.get('/api/data/refresh/status').then(function (res) {
      if (res && res.ok) render(res.state);
    }).catch(function () {});
  }

  function start(force) {
    el.log.textContent = '正在启动抓取任务…';
    KPL.api.post('/api/data/refresh', { force: !!force }).then(function (res) {
      if (res && res.ok) { render(res.state); poll(); }
    }).catch(function (err) {
      var msg = (err && err.message) || '启动失败';
      el.log.textContent = '⚠ ' + msg;
      if (msg.indexOf('冷静') >= 0) {
        if (window.confirm('刚刚抓过，可能会被官方限流。确定要强制重抓吗？')) start(true);
      }
      poll();
    });
  }

  el.btnRefresh.addEventListener('click', function () { start(false); });
  el.btnStatus.addEventListener('click', poll);
  poll();
})();


/* ------------------------------------------------------------------ */
/* v44：更新王者荣耀官网英雄数据（官方分路 + 压制关系）                 */
/* ------------------------------------------------------------------ */
(function () {
  var el = {
    card: document.getElementById('heroRefreshCard'),
    btn: document.getElementById('btnHeroRefresh'),
    btnStatus: document.getElementById('btnHeroRefreshStatus'),
    badge: document.getElementById('heroRefreshBadge'),
    log: document.getElementById('heroRefreshLog'),
    info: document.getElementById('heroExtraInfo')
  };
  if (!el.card || !el.btn) return;
  var timer = null;

  function fmtSec(s) {
    s = Math.max(0, s | 0);
    if (s < 60) return s + ' 秒';
    return Math.ceil(s / 60) + ' 分钟';
  }

  function renderInfo(info) {
    if (!info || !el.info) return;
    var kb = [];
    if (info.bundled) {
      kb.push('<span>数据来源</span><b>王者荣耀官网（已内置）</b>');
    } else {
      kb.push('<span>数据来源</span><b style="color:#C0392B">未找到 hero_relations.json</b>');
    }
    kb.push('<span>英雄关系</span><b>' + (info.relations_available || 0) + ' / ' +
      (info.relations_total || 0) + ' 个英雄有官方克制数据</b>');
    if (info.relations_missing) {
      kb.push('<span>缺数据</span><b>' + info.relations_missing + ' 个（官网暂无该英雄详情页）</b>');
    }
    kb.push('<span>数据时间</span><b>' + (info.relations_fetched_at || '—') + '</b>');
    kb.push('<span>官方分路兜底</span><b>' + (info.official_lane_heroes || 0) +
      ' 个英雄（KPL 没出场过，用官网分路）</b>');
    el.info.innerHTML = kb.join('');
  }

  function render(state) {
    if (!state) return;
    if (state.running) {
      el.badge.textContent = '更新中…';
      el.badge.className = 'du-badge is-err';
      el.btn.disabled = true;
      el.btn.textContent = '正在更新…';
    } else {
      el.btn.disabled = false;
      el.btn.textContent = '更新官网英雄数据';
      if (state.ok === true) { el.badge.textContent = '已完成'; el.badge.className = 'du-badge is-ok'; }
      else if (state.ok === false) { el.badge.textContent = '失败'; el.badge.className = 'du-badge is-err'; }
      else { el.badge.textContent = '空闲'; el.badge.className = 'du-badge'; }
      if (state.cooldown > 0) {
        el.btn.textContent = '更新官网英雄数据（冷静 ' + fmtSec(state.cooldown) + '）';
      }
    }
    var lines = (state.log || []).slice(-14);
    if (state.error && !state.running) lines = lines.concat(['错误：' + state.error]);
    if (lines.length) el.log.textContent = lines.join('\n');
    if (state.running && !timer) timer = setInterval(poll, 1500);
    else if (!state.running && timer) {
      clearInterval(timer);
      timer = null;
      loadInfo();
    }
  }

  function loadInfo() {
    KPL.api.get('/api/data/refresh_hero/status').then(function (res) {
      if (!res) return;
      render(res.state);
      renderInfo(res.info);
    }).catch(function () {});
  }

  function poll() {
    KPL.api.get('/api/data/refresh_hero/status').then(function (res) {
      if (res && res.ok) { render(res.state); renderInfo(res.info); }
    }).catch(function () {});
  }

  function start(force) {
    el.log.textContent = '正在启动官网数据更新…';
    KPL.api.post('/api/data/refresh_hero', { force: !!force }).then(function (res) {
      if (res && res.ok) { render(res.state); poll(); }
    }).catch(function (err) {
      var msg = (err && err.message) || '启动失败';
      el.log.textContent = '⚠ ' + msg;
      if (msg.indexOf('冷静') >= 0) {
        if (window.confirm('刚更新过，可能会被官方限流。确定要强制重抓吗？')) start(true);
      }
      poll();
    });
  }

  el.btn.addEventListener('click', function () { start(false); });
  if (el.btnStatus) el.btnStatus.addEventListener('click', poll);
  loadInfo();
})();
