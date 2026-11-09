/* ============================================================
   settings.js — 设置页（v4 需求 2/3/4 + 需求 8 资源管理）
   数据源：GET/POST /api/settings、POST /api/settings/llm_test、
           POST /api/settings/scoring、GET /api/resources
   严禁编造：所有状态都来自接口。
   ============================================================ */
(function () {
  'use strict';

  var KPL = (window.KPL = window.KPL || {});

  function byId(id) { return document.getElementById(id); }
  // v9 补丁：服务器地址列表需要转义（地址里可能有 & / 引号）
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function dbg() {
    if (!window.console) return;
    var a = Array.prototype.slice.call(arguments);
    a.unshift('[settings]');
    console.info.apply(console, a);
  }
  function toast(msg) {
    if (typeof KPL.toast === 'function') { try { return KPL.toast(msg); } catch (e) {} }
    if (window.console) console.warn('[kpl]', msg);
  }
  function api() { return KPL.api || null; }

  var el = {
    llmEnabled: byId('llmEnabled'),
    llmEndpoint: byId('llmEndpoint'),
    llmKey: byId('llmKey'),
    llmKeyHint: byId('llmKeyHint'),
    llmModel: byId('llmModel'),
    llmTemp: byId('llmTemp'),
    llmTimeout: byId('llmTimeout'),
    llmScope: byId('llmScope'),
    llmState: byId('llmState'),
    llmTestResult: byId('llmTestResult'),
    btnSaveLlm: byId('btnSaveLlm'),
    btnTestLlm: byId('btnTestLlm'),
    btnClearKey: byId('btnClearKey'),

    btnSaveDsh: byId('btnSaveDsh'),
    btnTestDsh: byId('btnTestDsh'),

    scoringState: byId('scoringState'),
    scoringPath: byId('scoringPath'),
    scoringResult: byId('scoringResult'),
    scoringHelpList: byId('scoringHelpList'),
    swSystem: byId('swSystem'),
    swCounter: byId('swCounter'),
    swVersion: byId('swVersion'),
    swLane: byId('swLane'),
    swWeakness: byId('swWeakness'),
    swTotal: byId('swTotal'),
    scPrimary: byId('scPrimary'),
    scSecondary: byId('scSecondary'),
    scDiscount: byId('scDiscount'),
    scWeakGap: byId('scWeakGap'),
    btnSaveScoring: byId('btnSaveScoring'),
    btnResetScoring: byId('btnResetScoring'),
    resState: byId('resState'),
    resSummary: byId('resSummary'),
    resLog: byId('resLog'),
    btnResCheck: byId('btnResCheck'),
    btnResUpdate: byId('btnResUpdate'),

    // v9 补丁：服务器监听地址 / 端口
    srvHost: byId('srvHost'),
    srvPort: byId('srvPort'),
    btnServerSave: byId('btnServerSave'),
    btnServerReload: byId('btnServerReload'),
    serverStatus: byId('serverStatus'),
    serverNow: byId('serverNow'),
    serverAddrs: byId('serverAddrs')
  };

  var state = {
    llmKeySet: false,
    busy: false
  };

  var REQ_TIMEOUT = 25000;

  function withTimeout(promise, ms, label) {
    ms = ms || REQ_TIMEOUT;
    return new Promise(function (resolve, reject) {
      var done = false;
      var t = setTimeout(function () {
        if (done) return;
        done = true;
        reject(new Error(label + '超时（' + Math.round(ms / 1000) + ' 秒）'));
      }, ms);
      promise.then(function (v) {
        if (done) return;
        done = true; clearTimeout(t); resolve(v);
      }, function (e) {
        if (done) return;
        done = true; clearTimeout(t); reject(e);
      });
    });
  }

  function setState(node, text, kind) {
    if (!node) return;
    node.textContent = text;
    node.className = 'v4-state' + (kind ? ' is-' + kind : '');
  }

  function showResult(node, text, isErr) {
    if (!node) return;
    node.hidden = false;
    node.textContent = text;
    node.className = 'v4-result' + (isErr ? ' is-err' : ' is-ok');
  }

  /* ------------------------------------------------------------------ *
   * 载入配置
   * ------------------------------------------------------------------ */
  function fillLlm(llm) {
    llm = llm || {};
    el.llmEnabled.checked = !!llm.enabled;
    el.llmEndpoint.value = llm.endpoint || '';
    el.llmModel.value = llm.model || '';
    el.llmTemp.value = (llm.temperature === null || llm.temperature === undefined) ? '' : llm.temperature;
    el.llmTimeout.value = (llm.timeout === null || llm.timeout === undefined) ? '' : llm.timeout;
    /* v7 需求 3：执教范围固定为「只控制 1 支战队」，UI 只做展示 */
    if (el.llmScope) el.llmScope.value = 'single_team';
    state.llmKeySet = !!llm.api_key_set;
    el.llmKey.value = '';
    el.llmKey.placeholder = state.llmKeySet ? (llm.api_key_hint || '已保存（留空不修改）') : 'sk-xxxxxxxxxxxxxxxx';
    el.llmKeyHint.textContent = state.llmKeySet
      ? '已保存密钥：' + String(llm.api_key_hint || '******') + '（留空表示不修改，填新值则覆盖）'
      : '还没有配置密钥。填好并保存后才能使用大模型 AI 对战。';
  }


  function renderStatus(res) {
    var llmState = res.llm_state || {};
    if (res.llm && res.llm.enabled) {
      setState(el.llmState, llmState.ready ? '已启用' : '已启用（未就绪）', llmState.ready ? 'on' : 'err');
    } else {
      setState(el.llmState, '未启用', 'off');
    }
  }

  function load() {
    var a = api();
    if (!a || typeof a.settings !== 'function') { toast('接口不可用'); return; }
    dbg('读取配置…');
    withTimeout(a.settings(), REQ_TIMEOUT, '读取设置').then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '读取设置失败');
      fillLlm(res.llm);
      renderStatus(res);
      fillScoring(res.scoring);
      dbg('配置已载入', { path: res.path, updated_at: res.updated_at });
    }).catch(function (err) {
      setState(el.llmState, '读取失败', 'err');
      toast('读取设置失败：' + (err && err.message ? err.message : String(err)));
    });
  }

  /* ------------------------------------------------------------------ *
   * 保存
   * ------------------------------------------------------------------ */
  function llmPatch(extra) {
    var patch = {
      llm: {
        enabled: !!el.llmEnabled.checked,
        endpoint: String(el.llmEndpoint.value || '').trim(),
        model: String(el.llmModel.value || '').trim(),
        temperature: el.llmTemp.value === '' ? undefined : Number(el.llmTemp.value),
        timeout: el.llmTimeout.value === '' ? undefined : Number(el.llmTimeout.value),
        league_scope: (el.llmScope && el.llmScope.value) || 'single_team'
      }
    };
    var key = String(el.llmKey.value || '').trim();
    if (key) patch.llm.api_key = key;
    if (extra) {
      Object.keys(extra).forEach(function (k) { patch[k] = extra[k]; });
    }
    return patch;
  }

  function doSaveLlm(extra, quiet) {
    var a = api();
    if (!a) return Promise.reject(new Error('接口不可用'));
    if (state.busy) return Promise.reject(new Error('正在保存，请稍候'));
    state.busy = true;
    var patch = llmPatch(extra);
    dbg('保存大模型配置', { enabled: patch.llm.enabled, model: patch.llm.model, scope: patch.llm.league_scope });
    return withTimeout(a.saveSettings(patch), REQ_TIMEOUT, '保存设置').then(function (res) {
      state.busy = false;
      if (!res || res.ok === false) throw new Error((res && res.error) || '保存失败');
      fillLlm(res.llm);
      renderStatus(res);
      if (!quiet) toast('已保存');
      return res;
    }).catch(function (err) {
      state.busy = false;
      throw err;
    });
  }

  if (el.btnSaveLlm) {
    el.btnSaveLlm.addEventListener('click', function () {
      doSaveLlm(null, false).catch(function (err) {
        showResult(el.llmTestResult, '保存失败：' + (err && err.message ? err.message : String(err)), true);
      });
    });
  }

  if (el.btnTestLlm) {
    el.btnTestLlm.addEventListener('click', function () {
      var a = api();
      if (!a || typeof a.llmTest !== 'function') { toast('接口不可用'); return; }
      showResult(el.llmTestResult, '正在向大模型发一次极短的测试请求…', false);
      dbg('测试大模型连接');
      var patch = llmPatch(null);
      patch.llm.enabled = true;
      withTimeout(a.llmTest(patch), 60000, '大模型测试').then(function (res) {
        var r = (res && res.result) || res || {};
        if (r.ok) {
          showResult(el.llmTestResult,
            '连接成功 · 模型 ' + (r.model || '—') + ' · ' + (r.latency_ms || 0) + 'ms' +
            (r.reply ? '\n模型回复：' + r.reply : ''), false);
          setState(el.llmState, '已启用', 'on');
        } else {
          showResult(el.llmTestResult,
            '连接失败（' + (r.kind || 'error') + '）：' + (r.error || '未知错误'), true);
          setState(el.llmState, '连通性失败', 'err');
        }
      }).catch(function (err) {
        showResult(el.llmTestResult, '连接失败：' + (err && err.message ? err.message : String(err)), true);
      });
    });
  }

  if (el.btnClearKey) {
    el.btnClearKey.addEventListener('click', function () {
      var a = api();
      if (!a) return;
      if (!window.confirm('确定清除已保存的 API 密钥？清除后大模型 AI 将不可用。')) return;
      withTimeout(a.saveSettings({ llm: { clear_api_key: true, enabled: false } }), REQ_TIMEOUT, '清除密钥')
        .then(function (res) {
          if (!res || res.ok === false) throw new Error((res && res.error) || '清除失败');
          fillLlm(res.llm);
          renderStatus(res);
          toast('已清除密钥');
        }).catch(function (err) {
          toast('清除失败：' + (err && err.message ? err.message : String(err)));
        });
    });
  }


  /* ------------------------------------------------------------------ *
   * 评分细则（v42：查看 / 修改 / 恢复默认）
   * ------------------------------------------------------------------ */
  var WEIGHT_FIELDS = [
    ['system', 'swSystem'], ['counter', 'swCounter'], ['version', 'swVersion'],
    ['lane', 'swLane'], ['weakness', 'swWeakness']
  ];

  function scoringTotal() {
    var total = 0;
    WEIGHT_FIELDS.forEach(function (pair) {
      var v = Number((el[pair[1]] || {}).value || 0);
      if (!isNaN(v)) total += v;
    });
    return total;
  }

  function updateScoringTotal() {
    if (el.swTotal) el.swTotal.value = String(scoringTotal());
  }

  function fillScoring(scoring) {
    if (!scoring || !el.swSystem) return;
    var rules = scoring.rules || {};
    var weights = rules.weights || {};
    WEIGHT_FIELDS.forEach(function (pair) {
      var box = el[pair[1]];
      if (!box) return;
      var v = weights[pair[0]];
      box.value = (v === null || v === undefined) ? '' : v;
      box.addEventListener('input', updateScoringTotal);
    });
    if (el.scPrimary) el.scPrimary.value = rules.lane_primary;
    if (el.scSecondary) el.scSecondary.value = rules.lane_secondary;
    if (el.scDiscount) el.scDiscount.value = rules.lane_secondary_discount;
    if (el.scWeakGap) el.scWeakGap.value = rules.weakness_win_rate_gap;
    if (el.scoringPath) el.scoringPath.textContent = scoring.path || 'data/scoring_rules.json';
    updateScoringTotal();

    var meta = scoring.meta || [];
    if (el.scoringHelpList && meta.length) {
      el.scoringHelpList.innerHTML = meta.map(function (item) {
        return '<li><b>' + esc(item.label || item.key) + '</b>' +
          '（<code>' + esc(item.key) + '</code>，默认 <b>' + esc(item.default) +
          esc(item.unit || '') + '</b>，范围 ' + esc(item.min) + '~' + esc(item.max) + '）：' +
          esc(item.desc || '') + '</li>';
      }).join('');
    }
    setState(el.scoringState, '已就绪', 'on');
  }

  function scoringPatch() {
    var weights = {};
    WEIGHT_FIELDS.forEach(function (pair) {
      var v = Number((el[pair[1]] || {}).value);
      weights[pair[0]] = isNaN(v) ? 0 : v;
    });
    return {
      weights: weights,
      lane_primary: Number(el.scPrimary.value),
      lane_secondary: Number(el.scSecondary.value),
      lane_secondary_discount: Number(el.scDiscount.value),
      weakness_win_rate_gap: Number(el.scWeakGap.value)
    };
  }

  function afterScoringSave(res, msg) {
    var scoring = (res && res.rules) ? res : (res && res.scoring ? res.scoring : res);
    fillScoring(scoring);
    showResult(el.scoringResult, msg, false);
    toast(msg);
  }

  if (el.btnSaveScoring) {
    el.btnSaveScoring.addEventListener('click', function () {
      var a = api();
      if (!a || typeof a.scoringSave !== 'function') { toast('接口不可用'); return; }
      var patch = scoringPatch();
      dbg('保存评分细则', patch);
      el.btnSaveScoring.disabled = true;
      withTimeout(a.scoringSave(patch), REQ_TIMEOUT, '保存评分细则').then(function (res) {
        el.btnSaveScoring.disabled = false;
        if (!res || res.ok === false) throw new Error((res && res.error) || '保存失败');
        afterScoringSave(res, '评分细则已保存并立即生效');
      }).catch(function (err) {
        el.btnSaveScoring.disabled = false;
        showResult(el.scoringResult, '保存失败：' + (err && err.message ? err.message : String(err)), true);
      });
    });
  }

  if (el.btnResetScoring) {
    el.btnResetScoring.addEventListener('click', function () {
      var a = api();
      if (!a || typeof a.scoringReset !== 'function') { toast('接口不可用'); return; }
      if (!window.confirm('确定把评分细则恢复成默认值吗？')) return;
      el.btnResetScoring.disabled = true;
      withTimeout(a.scoringReset(), REQ_TIMEOUT, '恢复默认').then(function (res) {
        el.btnResetScoring.disabled = false;
        if (!res || res.ok === false) throw new Error((res && res.error) || '恢复失败');
        afterScoringSave(res, '已恢复默认评分细则');
      }).catch(function (err) {
        el.btnResetScoring.disabled = false;
        showResult(el.scoringResult, '恢复失败：' + (err && err.message ? err.message : String(err)), true);
      });
    });
  }

  /* ------------------------------------------------------------------ *
   * 需求 8：本地赛事资源
   * ------------------------------------------------------------------ */
  function renderResources(res) {
    var s = (res && res.summary) || {};
    var items = [
      ['赛事赛季', s.leagues],
      ['比赛记录', s.matches],
      ['已下载资源', s.files],
      ['待更新', s.pending],
      ['文件损坏', s.corrupt]
    ];
    el.resSummary.innerHTML = items.map(function (pair) {
      var v = pair[1];
      return '<div class="v4-kv-item"><div class="v4-kv-k">' + pair[0] +
        '</div><div class="v4-kv-v">' + (v === null || v === undefined ? '—' : String(v)) +
        '</div></div>';
    }).join('');
    setState(el.resState, '已就绪', 'on');
  }

  function runResAction(fn, label) {
    var a = api();
    if (!a || typeof a.get !== 'function') { toast('接口不可用'); return; }
    el.resLog.hidden = false;
    el.resLog.textContent = '[' + label + '] 开始…';
    setState(el.resState, '处理中…', 'on');
    withTimeout(a.get(fn), 120000, label).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || label + '失败');
      renderResources(res);
      var lines = (res.log || []).join('\n');
      el.resLog.textContent = '[' + label + '] 完成\n' + (lines || '（无输出）');
      dbg(label + ' 完成', res.summary);
    }).catch(function (err) {
      setState(el.resState, '失败（不影响游戏）', 'err');
      el.resLog.textContent = '[' + label + '] 失败：' + (err && err.message ? err.message : String(err)) +
        '\n本地已有资源仍可正常使用。';
    });
  }

  if (el.btnResCheck) {
    el.btnResCheck.addEventListener('click', function () {
      runResAction('/api/resources/check', '校验本地资源');
    });
  }
  if (el.btnResUpdate) {
    el.btnResUpdate.addEventListener('click', function () {
      runResAction('/api/resources/update', '增量更新');
    });
  }

  function loadResources() {
    // v34：设置页的「本地赛事资源」卡片已移除（数据更新统一走「更新数据」页）
    if (!el.resSummary || !el.resState) return;
    var a = api();
    if (!a || typeof a.get !== 'function') return;
    withTimeout(a.get('/api/resources'), 20000, '资源状态').then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '读取失败');
      renderResources(res);
    }).catch(function (err) {
      setState(el.resState, '读取失败（不影响游戏）', 'err');
      el.resSummary.textContent = (err && err.message) ? err.message : '读取失败';
    });
  }

  /* ---------------- v9 补丁：服务器访问地址（IP / 端口） ---------------- */

  function setServerStatus(text, kind) {
    if (!el.serverStatus) return;
    el.serverStatus.hidden = !text;
    el.serverStatus.textContent = text || '';
    el.serverStatus.className = 'v4-hint' + (kind === 'err' ? ' is-err' : (kind === 'ok' ? ' is-ok' : ''));
  }

  function renderServerAddrs(rows) {
    if (!el.serverAddrs) return;
    var html = '';
    (rows || []).forEach(function (row) {
      html += '<li><span class="v4-addr-label">' + esc(row.label || '') + '</span>' +
        '<span class="v4-addr-url">' + esc(row.url || '') + '</span>' +
        '<span class="v4-addr-note">' + esc(row.note || '') + '</span></li>';
    });
    el.serverAddrs.innerHTML = html;
  }

  function renderServerConfig(payload) {
    if (!payload) return;
    var cfg = payload.config || {};
    if (el.srvHost && document.activeElement !== el.srvHost) el.srvHost.value = cfg.host || '';
    if (el.srvPort && document.activeElement !== el.srvPort) el.srvPort.value = cfg.port || '';
    var eff = payload.effective || {};
    if (el.serverNow) {
      el.serverNow.textContent = '当前生效：' + (eff.host || '?') + ':' + (eff.port || '?') +
        (payload.restart_required ? '（已保存的配置与当前进程不同，重启后生效）' : '');
    }
    renderServerAddrs(payload.addresses || []);
    if (payload.restart_required) setServerStatus(payload.note || '配置已保存，重启后生效。', 'ok');
  }

  function loadServerConfig() {
    var a = api();
    if (!a || typeof a.get !== 'function' || !el.srvHost) return;
    withTimeout(a.get('/api/server/config'), 15000, '服务器配置').then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '读取失败');
      renderServerConfig(res);
      console.info('[settings] 服务器监听配置：', (res.effective || {}).host + ':' + (res.effective || {}).port);
    }).catch(function (err) {
      setServerStatus('读取服务器配置失败：' + ((err && err.message) || '未知错误'), 'err');
    });
  }

  function saveServerConfig() {
    var a = api();
    if (!a || typeof a.post !== 'function') return;
    var host = (el.srvHost && el.srvHost.value || '').trim();
    var port = (el.srvPort && el.srvPort.value || '').trim();
    if (!host) { setServerStatus('请填写监听地址（例如 0.0.0.0）', 'err'); return; }
    if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
      setServerStatus('端口必须是 1~65535 的整数', 'err');
      return;
    }
    setServerStatus('正在保存 ...', '');
    withTimeout(a.post('/api/server/config', { host: host, port: Number(port) }), 15000, '保存服务器配置')
      .then(function (res) {
        if (!res || res.ok === false) throw new Error((res && res.error) || '保存失败');
        renderServerConfig(res);
        setServerStatus(res.note || '已保存，重启后生效。', 'ok');
        console.info('[settings] 服务器配置已保存：', host + ':' + port);
      })
      .catch(function (err) {
        setServerStatus('保存失败：' + ((err && err.message) || '未知错误'), 'err');
      });
  }

  if (el.btnServerSave) el.btnServerSave.addEventListener('click', saveServerConfig);
  if (el.btnServerReload) el.btnServerReload.addEventListener('click', loadServerConfig);

  function boot() {
    if (!el.llmEnabled) return;
    load();
    loadResources();
    loadServerConfig();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
