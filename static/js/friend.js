/* ============================================================
   friend.js — 朋友 PK（局域网 / 内网穿透联机）
   ------------------------------------------------------------
   架构（参考 Minecraft 局域网联机）：
   · 房主在自己的机器上开一个独立的小服务端口（默认 5001），
     由 game/room_server.py 提供 /api/room/* 的 HTTP 接口。
   · 加入者把「房主 IP + 端口」填进来，直接请求同一个接口。
   · 房主端持有唯一权威状态（game/room_engine.py 的 Room），
     每次变更 version + 1；所有客户端只是「拉状态 + 提交动作」。
   · 代码不做任何「只允许局域网 IP」的校验 —— 走 frp / ngrok / 其他
     内网穿透拿到的公网地址一样能连。
   ------------------------------------------------------------
   容错：
   · 连接超时 3 秒 → 「连不上房主，检查 IP/端口/网络」
   · 404 → 「房主没开房间或端口不对」
   · 每 5 秒心跳，连续失败 → 「与房主断开连接」
   · 任何一次拉状态失败都不会卡住页面，只累加失败计数
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

  var CONNECT_TIMEOUT = 3000;   // 需求：连接超时 3 秒
  var POLL_MS = 1000;           // 拉状态间隔
  var HEARTBEAT_MS = 5000;      // 需求：每 5 秒心跳一次
  var MAX_SILENT_FAIL = 3;      // 连续失败几次判定断线
  var LANES = ['对抗路', '打野', '中路', '发育路', '游走'];

  var S = {
    mode: 'single',
    role: null,          // 'host' | 'client'
    base: null,          // http://ip:port
    pid: null,
    room: null,
    match: null,
    league: null,
    pending: null,
    lane: 'all',
    pollTimer: null,
    autoTimer: null,
    hbTimer: null,
    failCount: 0,
    busy: false,
    logs: [],
    lastKey: ''
  };

  function byId(id) { return document.getElementById(id); }
  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  var el = {
    netBadge: byId('netBadge'),
    modeSeg: byId('modeSeg'),
    modeHint: byId('modeHint'),
    hostCard: byId('hostCard'),
    hostPort: byId('hostPort'),
    hostName: byId('hostName'),
    singleBo: byId('singleBo'),
    singleBoWrap: byId('singleBoWrap'),
    hostLeague: byId('hostLeague'),
    btnHostStart: byId('btnHostStart'),
    hostInfo: byId('hostInfo'),
    roomCode: byId('roomCode'),
    roomIps: byId('roomIps'),
    roomAccess: byId('roomAccess'),
    btnCopyAccess: byId('btnCopyAccess'),
    btnHostStop: byId('btnHostStop'),
    clientCard: byId('clientCard'),
    joinAddr: byId('joinAddr'),
    leagueTemplate: byId('leagueTemplate'),
    leagueEdition: byId('leagueEdition'),
    leagueEditionWrap: byId('leagueEditionWrap'),
    leagueTemplateWrap: byId('leagueTemplateWrap'),
    saveName: byId('saveName'),
    saveNameWrap: byId('saveNameWrap'),
    saveInfo: byId('saveInfo'),
    stageCard: byId('stageCard'),
    stageEvent: byId('stageEvent'),
    stageStrip: byId('stageStrip'),
    stageRules: byId('stageRules'),
    groupGrid: byId('groupGrid'),
    leagueTables: byId('leagueTables'),
    bracketCard: byId('bracketCard'),
    bracketHost: byId('bracketHost'),
    bracketCaption: byId('bracketCaption'),
    bracketStageChips: byId('bracketStageChips'),
    leagueFlatWrap: byId('leagueFlatWrap'),
    resumeCard: byId('resumeCard'),
    resumeList: byId('resumeList'),
    resumeCount: byId('resumeCount'),
    resumeStatus: byId('resumeStatus'),
    btnRefreshSaves: byId('btnRefreshSaves'),
    playersHint: byId('playersHint'),
    portsCount: byId('portsCount'),
    portList: byId('portList'),
    portsNote: byId('portsNote'),
    btnRefreshPorts: byId('btnRefreshPorts'),
    btnClosePorts: byId('btnClosePorts'),
    joinName: byId('joinName'),
    btnJoin: byId('btnJoin'),
    joinStatus: byId('joinStatus'),
    playersCard: byId('playersCard'),
    playersCount: byId('playersCount'),
    playerList: byId('playerList'),
    hostControls: byId('hostControls'),
    btnStartSingle: byId('btnStartSingle'),
    btnStartLeague: byId('btnStartLeague'),
    leagueControls: byId('leagueControls'),
    btnLeagueNext: byId('btnLeagueNext'),
    bpCard: byId('bpCard'),
    /* v9 需求 1：BP 面板由共享渲染器 static/js/bp_board.js 挂载到 #bpHost */
    bpHost: byId('bpHost'),
    bpCaption: byId('bpCaption'),
    bpActions: byId('bpActions'),
    btnSideBlue: byId('btnSideBlue'),
    btnSideRed: byId('btnSideRed'),
    btnNextGame: byId('btnNextGame'),
    btnAutoBp: byId('btnAutoBp'),
    bpResult: byId('bpResult'),
    leagueCard: byId('leagueCard'),
    leagueBo: byId('leagueBo'),
    leagueBody: byId('leagueBody'),
    netLog: byId('netLog')
  };

  /* ------------------------------ 日志 ------------------------------ */
  function log(msg) {
    var t = new Date().toTimeString().slice(0, 8);
    S.logs.unshift('[' + t + '] ' + msg);
    S.logs = S.logs.slice(0, 60);
    if (el.netLog) el.netLog.textContent = S.logs.join('\n');
    if (window.console && console.info) console.info('[friend]', msg);
  }

  /* --------------------------- HTTP 封装 ---------------------------- */
  function roomUrl(path) { return S.base + '/api/room' + path; }

  function request(path, opts, timeoutMs) {
    var cfg = opts || {};
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = null;
    var init = {
      method: cfg.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store'
    };
    if (cfg.body !== undefined) init.body = JSON.stringify(cfg.body);
    if (ctrl) { init.signal = ctrl.signal; timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || 8000); }

    return fetch(roomUrl(path), init).then(function (res) {
      if (timer) clearTimeout(timer);
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var msg = (data && (data.error || data.message)) || ('HTTP ' + res.status);
          var err = new Error(msg);
          err.status = res.status;
          err.body = data;
          throw err;
        }
        return data || {};
      });
    }).catch(function (err) {
      if (timer) clearTimeout(timer);
      if (err && err.name === 'AbortError') {
        var te = new Error('连接超时');
        te.timeout = true;
        throw te;
      }
      throw err;
    });
  }

  /* --------------------------- 连接 / 断开 --------------------------- */
  function describeError(err) {
    if (!err) return '未知错误';
    if (err.timeout) return '连不上房主，检查 IP/端口/网络';
    if (err.status === 404) return '房主没开房间或端口不对';
    if (err.status === 409) return err.message || '房间已满或队名被占用';
    if (err.status) return err.message || ('房主返回错误 ' + err.status);
    return '连不上房主，检查 IP/端口/网络';
  }

  function setBadge(text, cls) {
    if (!el.netBadge) return;
    el.netBadge.textContent = text;
    el.netBadge.className = 'v4-topbar-sub' + (cls ? ' ' + cls : '');
  }

  function startLoops() {
    stopLoops();
    S.failCount = 0;
    S.pollTimer = setInterval(poll, POLL_MS);
    S.hbTimer = setInterval(heartbeat, HEARTBEAT_MS);
    setBadge('已连接', 'is-ok');
  }

  function stopLoops() {
    if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
    if (S.hbTimer) { clearInterval(S.hbTimer); S.hbTimer = null; }
  }

  function teardown(reason) {
    stopLoops();
    S.base = null;
    S.pid = null;
    S.room = null;
    S.match = null;
    S.league = null;
    S.pending = null;
    setBadge('未连接');
    if (el.playersCard) el.playersCard.hidden = true;
    if (el.bpCard) el.bpCard.hidden = true;
    if (el.leagueCard) el.leagueCard.hidden = true;
    if (reason) log(reason);
  }

  function poll() {
    if (!S.base || !S.pid) return;
    request('/state?pid=' + encodeURIComponent(S.pid), { method: 'GET' }, 8000)
      .then(function (res) {
        S.failCount = 0;
        applyState(res);
      })
      .catch(function (err) {
        S.failCount += 1;
        log('拉取状态失败（' + S.failCount + '/' + MAX_SILENT_FAIL + '）：' + describeError(err));
        if (S.failCount >= MAX_SILENT_FAIL) {
          toast('与房主断开连接');
          teardown('与房主断开连接');
        }
      });
  }

  function heartbeat() {
    if (!S.base || !S.pid) return;
    request('/heartbeat', { method: 'POST', body: { pid: S.pid } }, 5000)
      .then(function () { S.failCount = 0; })
      .catch(function (err) {
        S.failCount += 1;
        log('心跳失败（' + S.failCount + '/' + MAX_SILENT_FAIL + '）：' + describeError(err));
        if (S.failCount >= MAX_SILENT_FAIL) {
          toast('与房主断开连接');
          teardown('与房主断开连接');
        }
      });
  }

  /* --------------------------- 房主：建房 --------------------------- */
  function hostStart() {
    var port = parseInt(el.hostPort.value, 10);
    var name = (el.hostName.value || '').trim() || '房主';
    if (!port || port < 1024 || port > 65535) { toast('端口需在 1024–65535 之间'); return; }
    // v47：昵称与战队名合并成一个（填一次，昵称就是战队名）
    if (!name) { toast('请先填写你的名字（昵称 = 战队名）'); return; }
    var hostTeam = name;
    el.btnHostStart.disabled = true;
    el.btnHostStart.textContent = '正在创建房间…';
    var league = el.hostLeague.value || (KPL.getLeagueId && KPL.getLeagueId()) || '';

    fetch('/api/room/host/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        port: port, mode: S.mode, league_id: league, name: name,
        team: hostTeam
      })
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      el.btnHostStart.disabled = false;
      el.btnHostStart.textContent = '创建房间';
      if (!res.ok || !res.data || res.data.ok === false) {
        var msg = (res.data && (res.data.error || res.data.message)) || '创建房间失败';
        toast(msg);
        log('创建房间失败：' + msg);
        return;
      }
      var d = res.data;
      S.role = 'host';
      S.base = 'http://127.0.0.1:' + d.port;
      S.pid = d.pid;
      el.hostInfo.hidden = false;
      el.roomCode.textContent = d.room_id || '—';
      el.roomIps.textContent = (d.ips || []).join(' / ') || '—';
      el.roomAccess.textContent = (d.access || []).join('  |  ') || '—';
      log('房间已创建：' + d.room_id + '，端口 ' + d.port);
      if (d.state) applyState(d.state);
      startLoops();
    }).catch(function (err) {
      el.btnHostStart.disabled = false;
      el.btnHostStart.textContent = '创建房间';
      toast('创建房间失败：' + (err && err.message ? err.message : '网络错误'));
      log('创建房间失败：' + (err && err.message ? err.message : '网络错误'));
    });
  }

  function hostStop() {
    fetch('/api/room/host/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .catch(function () {})
      .then(function () {
        teardown('房间已关闭');
        el.hostInfo.hidden = true;
      });
  }

  /* --------------------------- 客户端：加入 ------------------------- */
  var DEFAULT_PORT = 5001;

  /* 解析用户输入的单个地址：
     - 127.0.0.1:25565 / ip:port            → 带端口
     - http://域名:端口 / https://域名       → 去掉协议再解析
     - 纯域名 / 公网地址（内网穿透不给端口）  → 默认 5001
     解析失败返回 {error}，由调用方给友好提示（不 throw，避免卡死页面）。 */
  function parseAddress(raw) {
    var text = String(raw == null ? '' : raw).trim();
    if (!text) return { error: '请填写房主地址' };
    text = text.replace(/[\s　]+/g, '');
    var proto = '';
    var m = text.match(/^(https?):\/\//i);
    if (m) { proto = m[1].toLowerCase(); text = text.slice(m[0].length); }
    text = text.split('/')[0].split('?')[0].split('#')[0];
    if (!text) return { error: '地址里没有主机名' };

    var host = text;
    var port = DEFAULT_PORT;
    // IPv6 支持 [::1]:5001 形式
    var v6 = text.match(/^\[([0-9a-f:]+)\](?::(\d+))?$/i);
    if (v6) {
      host = v6[1];
      if (v6[2]) port = parseInt(v6[2], 10);
    } else if (text.indexOf(':') >= 0) {
      var parts = text.split(':');
      if (parts.length > 2) return { error: '地址格式不对，正确写法 host:port（例如 127.0.0.1:25565）' };
      host = parts[0];
      port = parseInt(parts[1], 10);
    }
    if (!host) return { error: '地址里没有主机名' };
    if (!/^[0-9a-zA-Z.\-_]+$/.test(host) && !/^[0-9a-f:]+$/i.test(host)) {
      return { error: '主机名含非法字符：' + host };
    }
    if (!port || isNaN(port) || port < 1 || port > 65535) {
      return { error: '端口不合法（1–65535）' };
    }
    var scheme = proto === 'https' ? 'https' : 'http';
    console.info('[friend] 地址解析：输入=' + raw + ' → host=' + host + ' port=' + port + ' scheme=' + scheme);
    return { host: host, port: port, base: scheme + '://' + host + ':' + port };
  }

  function join() {
    var name = (el.joinName.value || '').trim() || '朋友';
    var parsed = parseAddress(el.joinAddr ? el.joinAddr.value : '');
    if (parsed.error) {
      toast(parsed.error);
      if (el.joinStatus) el.joinStatus.textContent = '地址解析失败：' + parsed.error;
      log('地址解析失败：' + parsed.error);
      return;
    }
    var clean = parsed.host;
    var port = parsed.port;
    var base = parsed.base;

    // v47：昵称 = 战队名，只填一次
    var joinTeam = name;
    el.btnJoin.disabled = true;
    el.btnJoin.textContent = '正在连接…';
    el.joinStatus.textContent = '正在连接 ' + clean + ':' + port + ' …';

    var savedBase = S.base;
    S.base = base;
    request('/ping', { method: 'GET' }, CONNECT_TIMEOUT).then(function (info) {
      log('ping 成功：' + (info && info.hoster ? info.hoster : 'ok'));
      var myTeam = name;       // v47：昵称 = 战队名
      return request('/join', { method: 'POST', body: { name: name, team: myTeam } }, 8000);
    }).then(function (res) {
      el.btnJoin.disabled = false;
      el.btnJoin.textContent = '连接房主';
      if (!res || !res.pid) throw new Error('房主未返回 pid');
      S.role = 'client';
      S.pid = res.pid;
      el.joinStatus.textContent = '已连接 ' + clean + ':' + port;
      log('加入房间成功：' + clean + ':' + port + '，pid=' + res.pid);
      if (res.state) applyState(res.state);
      startLoops();
    }).catch(function (err) {
      S.base = savedBase;
      el.btnJoin.disabled = false;
      el.btnJoin.textContent = '连接房主';
      var msg = describeError(err);
      el.joinStatus.textContent = msg;
      toast(msg);
      log('连接失败：' + msg);
    });
  }

  /* ------------------------------ 动作 ------------------------------ */
  function act(path, body, okMsg) {
    // v9 调试日志：动作入口（用于排查"点了没反应"）
    window.__acts = window.__acts || [];
    window.__acts.push({ path: path, base: !!S.base, pid: !!S.pid, busy: !!S.busy, t: Date.now() });
    console.info('[friend] act() 入口 →', path, 'base=' + !!S.base, 'pid=' + !!S.pid, 'busy=' + !!S.busy);
    if (!S.base || !S.pid) { toast('还没连上房主'); return Promise.resolve(false); }
    if (S.busy) { console.warn('[friend] act() 被 busy 拦截：' + path); return Promise.resolve(false); }
    S.busy = true;
    var payload = body || {};
    payload.pid = S.pid;
    return request(path, { method: 'POST', body: payload }, 12000).then(function (res) {
      S.busy = false;
      if (res && res.state) applyState(res.state);
      else poll();
      if (okMsg) log(okMsg);
      return true;
    }).catch(function (err) {
      S.busy = false;
      var msg = describeError(err);
      toast(msg);
      log('操作失败：' + msg);
      poll();
      return false;
    });
  }

  /* ------------------------------ 渲染 ------------------------------ */
  function laneOf(h) {
    if (h.lane_main) return h.lane_main;
    var list = h.lane_list;
    return (list && list.length) ? list[0] : '';
  }

  function renderSaveInfo() {
    if (!el.saveInfo) return;
    var room = (S.state && S.state.room) || S.room || {};
    var sid = room.save_id || '';
    if (!sid) { el.saveInfo.hidden = true; return; }
    el.saveInfo.hidden = false;
    el.saveInfo.textContent = '本次联赛存档：' + (room.save_name || sid) + '（' + sid +
      '）· 可在「读档继续联赛」里恢复';
  }

  function renderPlayers(room) {
    if (!room) { el.playersCard.hidden = true; return; }
    el.playersCard.hidden = false;
    var players = room.players || [];
    /* v7 需求 2：不再写死「/ 8」——单局模式 2 人，联赛模式按模板规模，空缺由内置 AI 补齐 */
    var online = players.filter(function (p) { return p.online; }).length;
    /* v8 需求 1：联赛口径 = 真人上限 8 + 参赛队数按模板规模（差额内置 AI 补齐） */
    var leagueSize = room.league_size || (S.league && S.league.teams && S.league.teams.length) || 0;
    if ((S.mode || room.mode) === 'league' || leagueSize) {
      var cap = room.hard_max_players || room.max_players || 8;
      var teamCount = leagueSize || room.max_players || 0;
      el.playersCount.textContent = '在线 ' + online + ' / ' + cap + ' 人' +
        (teamCount ? ' · 参赛 ' + teamCount + ' 队' : '');
      if (el.playersHint) {
        el.playersHint.hidden = false;
        el.playersHint.textContent = teamCount
          ? ('真人上限 ' + cap + ' 人（联赛 18 / 挑战者杯 32 / 年度总决赛 12 / 年度赛程包按最小的一段 18）；当前真人 ' +
             online + ' 人，其余 ' + Math.max(0, teamCount - online) +
             ' 支队伍由内置 AI 自动补齐，1~' + cap + ' 名真人都能开完整联赛，不必等满人。')
          : ('真人上限 ' + cap + ' 人；房主选定赛事模板后点「开始联赛」即可开局，空缺队伍自动由内置 AI 补齐。');
      }
    } else {
      el.playersCount.textContent = players.length + ' 人' + (room.max_players ? ' / ' + room.max_players : '');
      if (el.playersHint) el.playersHint.hidden = true;
    }
    var html = '';
    players.forEach(function (p) {
      html += '<li class="v4-player' + (p.is_me ? ' is-me' : '') + (p.online ? '' : ' is-off') + '">' +
        '<span class="v4-dot' + (p.online ? ' is-on' : '') + '"></span>' +
        '<span class="v4-player-name">' + esc(p.name) + (p.is_host ? ' <em>房主</em>' : '') + (p.is_me ? ' <em class="is-me-tag">你</em>' : '') + '</span>' +
        '<span class="v4-player-team">' + esc(p.team || '—') + '</span>' +
        '<span class="v4-player-side ' + (p.side === 'red' ? 'is-red' : 'is-blue') + '">' + (p.side === 'red' ? '红方' : '蓝方') + '</span>' +
        '</li>';
    });
    el.playerList.innerHTML = html;

    var isHost = S.role === 'host';
    /* v8 修复：原来这里把 hostControls 在联赛模式下整体隐藏，导致房主看不到「开始联赛」按钮。
       现在容器只按「是否房主」显隐，具体按钮再按模式分别显隐。 */
    el.hostControls.hidden = !isHost;
    el.btnStartSingle.hidden = S.mode !== 'single';
    el.btnStartLeague.hidden = S.mode !== 'league';
    el.leagueControls.hidden = !(isHost && S.mode === 'league') || !(S.league && S.league.pending);
    syncStartLeagueState();
  }

  /* v8 需求 1②：房主「开始联赛」按钮的可用性 = 房间已建 + 赛事模板已加载 + 联赛尚未开始 */
  function syncStartLeagueState() {
    if (!el.btnStartLeague) return;
    var tpl = el.leagueTemplate && el.leagueTemplate.value ? el.leagueTemplate.value : '';
    var canStart = !!(S.room && S.role === 'host' && tpl && !(S.league && S.league.teams && S.league.teams.length));
    el.btnStartLeague.disabled = !canStart;
    el.btnStartLeague.title = tpl
      ? (canStart ? '按模板「' + tpl + '」开启完整联赛赛程' : '联赛已经开始，可直接「进行我的下一场」')
      : '请先选择赛事模板';
    if (S.mode === 'league' && S.role === 'host') {
      console.info('[friend] 开始联赛按钮状态：模板=' + (tpl || '(未选)') + ' 可点击=' + canStart);
    }
  }

  function slotsHTML(names, total) {
    var html = '';
    for (var i = 0; i < total; i++) {
      var n = names[i];
      if (n) html += '<span class="v4-slot is-filled" title="' + esc(n) + '">' + esc(n) + '</span>';
      else html += '<span class="v4-slot">·</span>';
    }
    return html;
  }

  /* v9 需求 1：BP 面板统一走共享渲染器（与 KPL 年总模式 /arena 同源） */
  var boardInst = null;

  /** v17：一键 BP —— 把本小局剩余 ban/pick 交给内置 AI 走完
   *  v19：仅当对手是内置 AI 时可用（真人对战隐藏按钮 + 服务端也会 409 拒绝） */
  function autoCompleteBp() {
    if (!S.base || !S.pid) { toast('还没有连接房间'); return; }
    if (S.busy) return;
    if (S.match && S.match.opponent_is_ai !== true) {
      toast('真人对战不能一键 BP，请自己完成 BP');
      return;
    }
    S.busy = true;
    if (el.btnAutoBp) el.btnAutoBp.disabled = true;
    log('一键 BP：请求服务端 AI 走完剩余 BP');
    KPL.api.post(S.base + '/api/room/autocomplete', { pid: S.pid }).then(function (res) {
      S.busy = false;
      if (el.btnAutoBp) el.btnAutoBp.disabled = false;
      if (res && res.state) applyState(res.state);
      toast('本次 BP 已由 AI 完成');
    }).catch(function (e) {
      S.busy = false;
      if (el.btnAutoBp) el.btnAutoBp.disabled = false;
      toast('一键 BP 失败：' + (e && e.message ? e.message : e));
    });
  }

  function bootBoard() {
    if (!el.bpHost || !window.KPLBpBoard) {
      console.warn('[friend] 共享 BP 渲染器未就绪，BP 面板不可用');
      return;
    }
    boardInst = window.KPLBpBoard.mount(el.bpHost, {
      label: 'friend',
      onSubmit: function (ename) {
        var m = S.match || {};
        // v9 调试日志：好友对战 BP 渲染/提交
        window.__bpSubmits = window.__bpSubmits || [];
        window.__bpSubmits.push({ ename: ename, step: m.step, phase: m.phase, action: m.action, busy: S.busy, t: Date.now() });
        console.info('[friend] BP 提交 →', ename, 'step=' + m.step, 'action=' + m.action, 'busy=' + S.busy);
        var action = m.action === 'ban' ? 'ban' : 'pick';
        var hero = heroOf(ename) || {};
        act('/action', { ename: parseInt(ename, 10), action: action },
          (action === 'ban' ? 'Ban ' : 'Pick ') + (hero.cname || ename));
      }
    });
    el.bpHost.hidden = false;
    // 载入英雄字典：共享面板用它渲染头像与名称兜底
    var loadHeroes = (window.KPL && KPL.api && KPL.api.heroes) || null;
    if (loadHeroes) {
      var lid = (window.KPL && KPL.getLeagueId && KPL.getLeagueId()) || '';
      loadHeroes(lid).then(function (res) {
        var list = (res && (res.heroes || res)) || [];
        if (!Array.isArray(list)) return;
        window.KPLBpBoard.setHeroes(list);
        console.info('[friend] 英雄字典已载入：' + list.length + ' 个（用于 BP 头像兜底）');
        if (S.match) renderMatch(S.match);
      }).catch(function (err) {
        console.warn('[friend] 英雄字典载入失败：' + (err && err.message ? err.message : String(err)));
      });
    }
    console.info('[friend] BP 面板已挂载（共享渲染器 bp_board.js）');
  }

  function renderMatch(m) {
    S.match = m || null;
    if (!m) {
      el.bpCard.hidden = true;
      return;
    }
    el.bpCard.hidden = false;

    var blueName = (m.blue_team && (m.blue_team.name || m.blue_team)) || '蓝方';
    var redName = (m.red_team && (m.red_team.name || m.red_team)) || '红方';
    var sc = m.score || {};

    if (el.bpCaption) {
      el.bpCaption.textContent = blueName + ' vs ' + redName +
        ' · BO' + (m.bo || 1) + ' · 大比分 ' + (sc.blue || 0) + ':' + (sc.red || 0);
    }

    // v20：巅峰对决盲选阶段（tiebreak_pick）双方同时可操作，没有 m.action
    var tiePick = m.phase === 'tiebreak_pick';
    var canPick = !!(m.my_turn && (m.phase === 'bp' || tiePick) && !m.finished);
    if (boardInst) {
      try {
        boardInst.update(m, { canAct: canPick });
      } catch (err) {
        console.error('[friend] BP 面板渲染异常：' + (err && err.stack ? err.stack : String(err)));
        toast('BP 面板渲染失败：' + (err && err.message ? err.message : String(err)));
      }
    }

    // 选边 / 下一局 / 结算（与共享面板并存的少量 v4 控件）
    var sc2 = m.side_choice || {};
    var needSide = m.phase === 'side_choice' && sc2.required === true && sc2.resolved !== true;
    var showResult = m.finished || m.phase === 'result' || m.series_over;
    // v17：BP 进行中显示「一键 BP」；v19：只有对手是内置 AI 时才给（真人对战隐藏）
    // v20：巅峰对决盲选阶段同样算「BP 进行中」
    var inBp = (m.phase === 'bp' || m.phase === 'tiebreak_pick') && !m.finished && !m.series_over;
    // 后端 m.opponent_is_ai 为 v19 新增；老房间无该字段时按「有 my_turn 且 mode 非 free」保守判断
    var aiOpponent = m.opponent_is_ai === true;
    var canAutoBp = inBp && aiOpponent;
    if (el.bpActions) {
      el.bpActions.hidden = !(needSide || showResult || inBp);
      el.btnSideBlue.hidden = !needSide;
      el.btnSideRed.hidden = !needSide;
      if (el.btnAutoBp) {
        el.btnAutoBp.hidden = !canAutoBp;
        el.btnAutoBp.title = canAutoBp
          ? '本局剩余 BP 交给内置 AI 走完'
          : '真人对战不能一键 BP';
      }
      el.btnNextGame.hidden = !(m.phase === 'result' && !m.series_over) && !m.series_over;
    }

    if (m.finished && m.final) {
      el.bpResult.hidden = false;
      var w = m.final.winner || m.result || '';
      var wText = w === 'blue' ? ('蓝方胜利（' + blueName + '）')
        : w === 'red' ? ('红方胜利（' + redName + '）')
          : w === 'draw' ? '平局' : '本局结束';
      el.bpResult.innerHTML = '<b>' + esc(wText) + '</b><br><span>BO' + (m.bo || 1) +
        ' · 第 ' + (m.game_index || 1) + ' 局 · 大比分 ' + (sc.blue || 0) + ':' + (sc.red || 0) + '</span>';
    } else {
      el.bpResult.hidden = true;
    }
  }

  /* v9：英雄字典来自对局数据（room match view 的 hero_board / available），不再依赖旧网格 DOM */
  function board() {
    var m = S.match || {};
    if (Array.isArray(m.hero_board) && m.hero_board.length) return m.hero_board;
    if (Array.isArray(m.available) && m.available.length) return m.available;
    return [];
  }

  function heroOf(ename) {
    var list = board();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].ename) === String(ename)) return list[i];
    }
    return null;
  }

  /* v9：board()/renderLaneFilters()/renderGrid() 已由共享面板 bp_board.js 取代 */

  /* v9 补：真实赛制视图（阶段进度 + 抽签分组 / SAB 组别 + 分组积分榜） */
  var STAGE_LABEL = {
    r1: '第一轮循环赛', r2: '第二轮循环赛', cards: '卡位赛',
    r3: '第三轮循环赛', playoffs: '季后赛',
    qualifier: '32 → 8 单败淘汰', playoff: '8 队双败淘汰', final: '总决赛',
    arena: '擂台赛', breakout: '突围赛'
  };

  function renderStage(league) {
    if (!el.stageCard) return;
    var groups = league.groups || [];
    var plan = league.plan || [];
    if (!groups.length && !plan.length) { el.stageCard.hidden = true; return; }
    el.stageCard.hidden = false;
    if (el.stageEvent) {
      el.stageEvent.textContent = (league.event_name || '') +
        (league.champion ? ' · 冠军：' + league.champion : '');
    }
    // 阶段条
    var strip = '';
    plan.forEach(function (p, i) {
      var cls = 'v4-stage';
      if (p.status === 'done') cls += ' is-done';
      else if (p.status === 'current') cls += ' is-current';
      strip += '<span class="' + cls + '">' + (p.status === 'done' ? '✓ ' : '') +
        esc(p.label || STAGE_LABEL[p.key] || p.key) +
        ' <em>BO' + esc(p.bo || 5) + '</em></span>';
      if (i < plan.length - 1) strip += '<span class="v4-stage-arrow">›</span>';
    });
    if (el.stageStrip) el.stageStrip.innerHTML = strip;
    if (el.stageRules) el.stageRules.textContent = league.stage_rules || '';

    // 分组：第一轮是抽签组 G1/G2/G3，之后是 S/A/B 组
    var g = '';
    groups.forEach(function (grp) {
      var rows = grp.rows || [];
      g += '<div class="v4-group"><h4>' + esc(grp.name || grp.key) + '</h4><ul>';
      rows.forEach(function (r) {
        g += '<li class="' + (r.is_mine ? 'is-mine' : '') + '">' +
          '<span class="v4-group-rank">' + esc(r.rank) + '</span>' +
          '<span class="v4-group-team">' + esc(r.team) + (r.is_mine ? ' <em>你</em>' : '') + '</span>' +
          '<span class="v4-group-pts">' + esc(r.points == null ? 0 : r.points) + ' 分</span>' +
          '</li>';
      });
      g += '</ul></div>';
    });
    if (el.groupGrid) el.groupGrid.innerHTML = g;
  }

  /* v13：淘汰赛对阵流程图（联机页实时刷新，房主和朋友都能看到进度） */
  function renderBracket(league) {
    if (!el.bracketCard || !el.bracketHost) return;
    var view = (league && league.bracket) || null;
    if (!view || !(view.rounds && view.rounds.length)) {
      el.bracketCard.hidden = true;
      return;
    }
    el.bracketCard.hidden = false;
    if (el.bracketCaption) {
      el.bracketCaption.textContent = (league.stage_label || view.stage_label || '') +
        (view.champion ? ' · 冠军 ' + view.champion : '');
    }
    var books = view.available || [];
    if (el.bracketStageChips) {
      if (books.length > 1) {
        el.bracketStageChips.hidden = false;
        el.bracketStageChips.innerHTML = books.map(function (b) {
          var act = (view.stage || '') === b.stage ? ' is-active' : '';
          return '<button type="button" class="c-day-chip' + act + '" data-rstage="' +
            esc(b.stage) + '">' + esc(b.label || b.stage) + '</button>';
        }).join('');
        [].forEach.call(el.bracketStageChips.querySelectorAll('[data-rstage]'), function (btn) {
          btn.addEventListener('click', function () {
            loadBracketStage(btn.getAttribute('data-rstage'));
          });
        });
      } else {
        el.bracketStageChips.hidden = true;
        el.bracketStageChips.innerHTML = '';
      }
    }
    if (!window.KPLBracket) {
      el.bracketHost.innerHTML = '<div class="bk-empty">对阵图组件未加载</div>';
      return;
    }
    try {
      KPLBracket.render(el.bracketHost, view, { myTeam: S.myTeamName || null });
      console.info('[friend] 对阵图渲染', view.stage_label || view.stage,
        '轮次', (view.rounds || []).length);
    } catch (err) {
      console.error('[friend] 对阵图渲染失败：', err);
      el.bracketHost.innerHTML = '<div class="bk-empty">对阵图渲染失败</div>';
    }
  }

  function loadBracketStage(stage) {
    if (!S.room) return;
    KPL.api.post(S.base + '/api/room/bracket', { pid: S.pid, stage: stage })
      .then(function (res) {
        if (res && res.bracket) {
          S.league = S.league || {};
          S.league.bracket = res.bracket;
          renderBracket(S.league);
        }
      })
      .catch(function (err) {
        toast('对阵图加载失败：' + (err && err.message ? err.message : err));
      });
  }

  function renderLeague(league) {
    S.league = league || null;
    if (!league) {
      el.leagueCard.hidden = true;
      if (el.stageCard) el.stageCard.hidden = true;
      if (el.bracketCard) el.bracketCard.hidden = true;
      return;
    }
    renderStage(league);
    renderBracket(league);
    el.leagueCard.hidden = false;
    var stageName = league.stage_label || '';
    el.leagueBo.textContent = (stageName ? stageName + ' · ' : '') +
      'BO' + (league.bo || 3) + ' · ' + (league.size || 0) + ' 队';
    var rows = league.standings || [];
    var groups = league.groups || [];
    // 分组积分榜（真实赛制：三个组各一张表）；没有分组信息时退回平铺
    if (el.leagueTables) {
      if (groups.length) {
        var html0 = '';
        groups.forEach(function (grp) {
          html0 += '<div class="v4-table-wrap"><h4 class="v4-group-title">' +
            esc(grp.name || grp.key) + '</h4>' +
            '<table class="v4-league-table"><thead><tr><th>#</th><th>战队</th><th>场次</th>' +
            '<th>胜</th><th>负</th><th>净胜局</th></tr></thead><tbody>';
          (grp.rows || []).forEach(function (r) {
            html0 += '<tr class="' + (r.is_mine ? 'is-mine' : '') + '">' +
              '<td>' + esc(r.rank) + '</td>' +
              '<td>' + esc(r.team) + (r.is_mine ? ' <em>你</em>' : '') + '</td>' +
              '<td>' + esc(r.played) + '</td><td>' + esc(r.wins) + '</td>' +
              '<td>' + esc(r.losses) + '</td>' +
              '<td>' + ((r.game_diff > 0 ? '+' : '') + esc(r.game_diff)) + '</td></tr>';
          });
          html0 += '</tbody></table></div>';
        });
        el.leagueTables.innerHTML = html0;
        el.leagueTables.hidden = false;
        if (el.leagueFlatWrap) el.leagueFlatWrap.hidden = true;
      } else {
        el.leagueTables.innerHTML = '';
        el.leagueTables.hidden = true;
        if (el.leagueFlatWrap) el.leagueFlatWrap.hidden = false;
      }
    }
    var html = '';
    rows.forEach(function (r) {
      html += '<tr class="' + (r.is_mine ? 'is-mine' : '') + '">' +
        '<td>' + esc(r.rank) + '</td>' +
        '<td>' + esc(r.team) + (r.is_mine ? ' <em>你</em>' : '') + '</td>' +
        '<td>' + esc(r.played) + '</td>' +
        '<td>' + esc(r.wins) + '</td>' +
        '<td>' + esc(r.losses) + '</td>' +
        '<td>' + (r.game_diff > 0 ? '+' : '') + esc(r.game_diff) + '</td>' +
        '<td>' + (r.win_rate == null ? '—' : Number(r.win_rate).toFixed(1) + '%') + '</td>' +
        '</tr>';
    });
    el.leagueBody.innerHTML = html || '<tr><td colspan="7" class="v4-empty">联赛尚未开始</td></tr>';
    if (el.leagueControls) {
      el.leagueControls.hidden = !(S.role === 'host' && S.mode === 'league') || !league.pending;
    }
  }

  function applyState(res) {
    if (!res) return;
    var room = res.room || null;
    S.room = room;
    renderPlayers(room);
    renderSaveInfo();
    renderMatch(res.match || null);
    renderLeague(res.league || null);
    var key = (res.version || '') + '|' + ((res.match && res.match.step) || '') + '|' +
      ((res.match && res.match.game_index) || '');
    if (key !== S.lastKey) {
      S.lastKey = key;
      if (room) setBadge('已连接 · ' + (room.room_id || ''), 'is-ok');
    }
    maybeAuto();
  }

  /* ------------------- v6：服务端一键推进（AI 不再靠前端空转） ------------------- */
  function maybeAuto() {
    var m = S.match;
    var league = S.league || (S.room && S.room.league) || null;
    if (!S.base || !S.pid) return;
    if (m) {
      if (m.series_over || m.finished) return;        // 等用户点“进入下一局 / 下一场”
      if (m.phase === 'result') return;
      if (m.my_turn) return;                          // 轮到本人
      if (m.phase === 'bp' || m.phase === 'side_choice') autoDrive();
      return;
    }
    if (league && !league.finished) autoDrive();      // 联赛里没有进行中的对局 → 取下一场
  }

  function autoDrive() {
    if (S.autoTimer) return;
    S.autoTimer = setTimeout(function () {
      S.autoTimer = null;
      if (!S.base || !S.pid || S.busy) return;
      S.busy = true;
      request('/auto', { method: 'POST', body: { pid: S.pid } }, 20000).then(function (res) {
        S.busy = false;
        if (res && res.stalled) {
          log('服务端推进停滞，已暂停自动推进');
          toast('自动推进受阻，请手动点“进行下一场”');
          return;
        }
        if (res && res.state) applyState(res.state);
        else poll();
      }).catch(function (err) {
        S.busy = false;
        log('自动推进失败：' + describeError(err));
      });
    }, 150);
  }

  /* --------------------------- 网格交互 ---------------------------- */
  /* onGridClick / confirmPick 已由共享面板 bp_board.js 内部处理（v9） */

  function chooseSide(side) {
    act('/side_choice', { side: side }, '选择' + (side === 'blue' ? '蓝色方' : '红色方'));
  }

  /* --------------------------- 赛事模板 --------------------------- */
  /* --------------------- v9 需求 2：联机联赛存档 --------------------- */
  function loadFriendSaves() {
    if (!el.resumeList) return;
    fetch('/api/tourney/saves?mode=friend', { method: 'GET' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var rows = (res && res.saves) || [];
        console.info('[friend] 联机存档读取 → ' + rows.length + ' 条');
        renderSaves(rows);
      })
      .catch(function (err) {
        console.warn('[friend] 联机存档读取失败', err);
        if (el.resumeStatus) {
          el.resumeStatus.hidden = false;
          el.resumeStatus.textContent = '存档列表读取失败：' + (err && err.message ? err.message : String(err));
        }
      });
  }

  function renderSaves(rows) {
    if (el.resumeCount) el.resumeCount.textContent = rows.length ? rows.length + ' 个' : '暂无';
    if (!rows.length) {
      el.resumeList.innerHTML = '<li class="v4-hint">还没有联机联赛存档：开局一次联赛后会自动生成。</li>';
      return;
    }
    var html = '';
    rows.forEach(function (row) {
      var room = row.room || {};
      var port = room.port ? '端口 ' + esc(room.port) : '';
      var humans = (row.humans || []).length;
      var tag = (row.status === 'done') ? '已结束' : (row.stage_label || '');
      html += '<li class="v4-save">' +
        '<div class="v4-save-main">' +
        '<strong>' + esc(row.save_name || row.save_id) + '</strong>' +
        '<span class="v4-hint">' + esc((row.event && row.event.name) || row.template || '') +
        ' · ' + esc(tag) + ' · 已打 ' + esc(row.matches_done == null ? 0 : row.matches_done) +
        ' / ' + esc(row.matches_total == null ? 0 : row.matches_total) + ' 场 · 阶段进度 ' +
        esc(row.progress == null ? 0 : row.progress) + '%' +
        (humans ? ' · 真人 ' + humans : '') + (port ? ' · ' + port : '') + '</span>' +
        '</div>' +
        '<button type="button" class="btn btn-primary btn-sm" data-resume="' + esc(row.save_id) + '"' +
        (row.status === 'done' ? ' disabled' : '') + '>读档继续</button>' +
        '</li>';
    });
    el.resumeList.innerHTML = html;
    Array.prototype.forEach.call(el.resumeList.querySelectorAll('[data-resume]'), function (btn) {
      btn.addEventListener('click', function () { resumeLeague(btn.getAttribute('data-resume'), btn); });
    });
  }

  function resumeLeague(saveId, btn) {
    if (S.role && S.role !== 'host') { toast('只有房主可以读档'); return; }
    console.info('[friend] 读档继续联赛 → ' + saveId);
    if (btn) { btn.disabled = true; btn.textContent = '正在开局…'; }
    var restore = function () { if (btn) { btn.disabled = false; btn.textContent = '读档继续'; } };
    var resume = function () {
      act('/resume_league', { save_id: saveId }, '读档继续').then(restore, restore);
    };
    if (S.room) { resume(); return; }

    // v41：房主没有房间时，一步完成「建房 + 读档」，不用先手动点「创建房间」
    var port = parseInt(el.hostPort && el.hostPort.value, 10) || 5001;
    var name = ((el.hostName && el.hostName.value) || '').trim() || '房主';
    var team = name;
    if (!name) { toast('请先填写你的名字（昵称 = 战队名）'); restore(); return; }
    log('读档前自动建房：端口 ' + port + ' 昵称 ' + name);
    fetch('/api/room/host/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port: port, mode: 'league', league_id: S.leagueId || '', name: name, team: team })
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.data || res.data.ok === false) {
          throw new Error((res.data && (res.data.error || res.data.message)) || '建房失败');
        }
        S.role = 'host';
        S.base = 'http://127.0.0.1:' + res.data.port;
        S.pid = res.data.pid;
        // 房间刚建好，直接读档；/resume_league 的响应里带完整 state，不用再补一次 /state
        resume();
      })
      .catch(function (err) {
        restore();
        var msg = err && err.message ? err.message : String(err);
        toast('读档失败：' + msg);
        log('读档前建房失败：' + msg);
      });
  }

  /* v32：届次下拉 —— 和年总模式同一套 editions（哪个年份、哪一届、官方站名） */
  function currentEditions() {
    var list = S.templates || [];
    var key = el.leagueTemplate ? el.leagueTemplate.value : '';
    var row = list.filter(function (t) { return t.key === key; })[0];
    return (row && row.editions) || [];
  }

  function fillEditions() {
    if (!el.leagueEdition) return;
    var eds = currentEditions();
    if (!eds.length) {
      el.leagueEdition.innerHTML = '<option value="">（该模板暂无届次数据）</option>';
      return;
    }
    el.leagueEdition.innerHTML = eds.map(function (e) {
      var stations = (e.stations || []).map(function (s) { return s.label; }).filter(Boolean);
      var text = e.name || e.edition_id;
      if (stations.length) text += '（' + stations.join(' → ') + '）';
      return '<option value="' + esc(e.edition_id) + '">' + esc(text) + '</option>';
    }).join('');
    console.info('[friend] 届次 ' + eds.length + ' 个');
  }

  function loadTemplates() {
    if (!el.leagueTemplate) return;
    fetch('/api/tourney/templates', { method: 'GET' }).then(function (r) { return r.json(); })
      .then(function (res) {
        var list = (res && res.templates) || [];
        if (!list.length) throw new Error('模板为空');
        var opts = list.map(function (t) {
          return '<option value="' + esc(t.key) + '">' + esc(t.name) +
            '（' + esc((t.category === 'cup' ? '杯赛' : (t.category === 'package' ? '赛程包' : '联赛'))) +
            ' · ' + (t.team_count || 18) + ' 支战队）</option>';
        }).join('');
        el.leagueTemplate.innerHTML = opts;
        S.templates = list;
        // 默认选单届联赛模板，和年总模式保持一致
        var prefer = list.filter(function (t) { return t.key === 'spring'; })[0] || list[0];
        if (prefer) el.leagueTemplate.value = prefer.key;
        fillEditions();
        console.info('[friend] 已加载 ' + list.length + ' 个赛事模板');
      })
      .catch(function (err) {
        el.leagueTemplate.innerHTML = '<option value="">模板加载失败，可稍后重试</option>';
        log('赛事模板加载失败：' + (err && err.message ? err.message : String(err)));
      });
  }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ---------------------- 联机服务 / 端口占用（v7 需求 2） ---------------------- */
  function renderPorts(data) {
    var rows = (data && data.ports) || [];
    if (el.portsCount) el.portsCount.textContent = String(rows.length);
    if (!el.portList) return;
    if (!rows.length) {
      el.portList.innerHTML =
        '<li class="v4-player"><span class="v4-player-name">当前没有本程序占用的联机端口</span></li>';
      if (el.portsNote) el.portsNote.textContent = '没有运行中的房间；点「创建房间」会占用一个新端口。';
      return;
    }
    var html = '';
    rows.forEach(function (row) {
      var inUse = '';
      (data.probe || []).forEach(function (p) {
        if (p.port === row.port) inUse = p.in_use ? '监听中' : '已释放';
      });
      html += '<li class="v4-player">' +
        '<span class="v4-dot' + (inUse === '监听中' ? ' is-on' : '') + '"></span>' +
        '<span class="v4-player-name">端口 ' + row.port + ' · ' + esc(row.room_id) + '</span>' +
        '<span class="v4-player-team">' + esc(row.mode === 'league' ? '联赛 PK' : '单局 PK') +
          (row.template ? ' · ' + esc(row.template) : '') + '</span>' +
        '<span class="v4-player-side is-blue">' + row.online + '/' + row.players + ' 人</span>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-close-port="' + row.port + '">关闭</button>' +
        '</li>';
    });
    el.portList.innerHTML = html;
    Array.prototype.forEach.call(el.portList.querySelectorAll('[data-close-port]'), function (btn) {
      btn.addEventListener('click', function () {
        closeRoomPort(parseInt(btn.getAttribute('data-close-port'), 10), btn);
      });
    });
    if (el.portsNote) {
      el.portsNote.textContent = (data.note || '') || '端口被占用会导致朋友连不上，可以在这里主动释放。';
    }
  }

  function loadPorts() {
    fetch('/api/room/ports', { method: 'GET' }).then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res || res.ok === false) throw new Error((res && res.error) || '端口接口失败');
        renderPorts(res);
      })
      .catch(function (err) {
        if (el.portList) {
          el.portList.innerHTML = '<li class="v4-player"><span class="v4-player-name">端口状态读取失败：' +
            esc(err && err.message ? err.message : String(err)) + '</span></li>';
        }
      });
  }

  function closeRoomPort(port, btn) {
    if (!port) return;
    if (btn) { btn.disabled = true; btn.textContent = '关闭中…'; }
    console.info('[friend] 请求关闭端口 ' + port);
    fetch('/api/room/ports/close', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port: port })
    }).then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res || res.ok === false) throw new Error((res && res.error) || '关闭失败');
        console.info('[friend] 端口 ' + port + ' 关闭结果：closed=' + res.closed + ' in_use=' + res.in_use);
        toast(res.closed ? '已关闭联机服务，端口 ' + port + ' 已释放' : '房间已不在运行，端口 ' + port + ' 已清理');
        log('联机端口 ' + port + ' 已关闭');
        S.room = null;
        poll();
        loadPorts();
      })
      .catch(function (err) {
        var msg = err && err.message ? err.message : String(err);
        if (btn) { btn.disabled = false; btn.textContent = '关闭'; }
        toast('关闭失败：' + msg);
        log('关闭端口失败：' + msg);
      });
  }

  /* ------------------------------ 绑定 ------------------------------ */
  function bindEvents() {
    $$('#modeSeg .v4-seg-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        S.mode = btn.getAttribute('data-mode');
        $$('#modeSeg .v4-seg-btn').forEach(function (b) {
          b.classList.toggle('is-active', b === btn);
        });
        el.modeHint.textContent = S.mode === 'single'
          ? '两人打一局完整 BP（BO1），房主执蓝方、加入者执红方，轮流操作。'
          : '所有联机玩家各带一支战队（队名自拟，不能用 KPL 战队名），按所选赛事模板的官方赛制打完整赛季（SAB 三阶段 / 卡位赛 / 季后赛）；参赛队数按模板规模（联赛 18 / 挑战者杯 32 / 年度总决赛 12），真人不足的部分由内置 AI 补齐。';
        if (el.leagueTemplateWrap) el.leagueTemplateWrap.hidden = S.mode !== 'league';
        if (el.leagueEditionWrap) el.leagueEditionWrap.hidden = S.mode !== 'league';
        // v41：读档卡片以前永远带 hidden（模板里写的、代码从没取消），玩家看不到「继续上次联赛」
        if (el.resumeCard) el.resumeCard.hidden = S.mode !== 'league';
        if (el.singleBoWrap) el.singleBoWrap.hidden = S.mode !== 'single';
        if (S.room) renderPlayers(S.room);
      });
    });

    el.btnHostStart.addEventListener('click', hostStart);
    el.btnHostStop.addEventListener('click', hostStop);
    el.btnJoin.addEventListener('click', join);
    el.btnCopyAccess.addEventListener('click', function () {
      var text = el.roomAccess.textContent || '';
      if (navigator.clipboard && text) {
        navigator.clipboard.writeText(text).then(function () { toast('已复制'); },
          function () { toast('复制失败，请手动选中'); });
      } else {
        toast('请手动选中复制');
      }
    });

    el.btnStartSingle.addEventListener('click', function () {
      // v47：单局 PK 也可选 BO1/3/5/7/9
      var bo = el.singleBo ? parseInt(el.singleBo.value, 10) || 1 : 1;
      act('/start_single', { bo: bo }, '开始单局（BO' + bo + '）');
    });
    if (el.leagueTemplate) {
      el.leagueTemplate.addEventListener('change', function () {
        console.info('[friend] 赛事模板切换 → ' + (el.leagueTemplate.value || '(空)'));
        fillEditions();
        syncStartLeagueState();
      });
    }
    el.btnStartLeague.addEventListener('click', function () {
      var tpl = el.leagueTemplate && el.leagueTemplate.value ? el.leagueTemplate.value : '';
      if (!tpl) { toast('请先选择赛事模板'); return; }
      if (!S.room) { toast('请先创建房间'); return; }
      console.info('[friend] 房主开始联赛：模板=' + tpl + ' 房间=' + (S.room.id || S.room.room_id || '?'));
      log('开始联赛：模板=' + tpl);
      el.btnStartLeague.disabled = true;
      var sname = el.saveName && el.saveName.value ? el.saveName.value.trim() : '';
      if (sname) { console.info('[friend] 自定义存档名 → ' + sname); }
      var editionId = el.leagueEdition && el.leagueEdition.value ? el.leagueEdition.value : '';
      if (editionId) { console.info('[friend] 届次 → ' + editionId); }
      act('/start_league', { template: tpl, save_name: sname, edition_id: editionId }, '开始联赛');
    });
    el.btnLeagueNext.addEventListener('click', function () { act('/league_next', {}, '进行下一场'); });
    if (el.btnRefreshSaves) el.btnRefreshSaves.addEventListener('click', function () {
      console.info('[friend] 刷新联机存档列表');
      loadFriendSaves();
    });
    if (el.btnRefreshPorts) el.btnRefreshPorts.addEventListener('click', function () {
      console.info('[friend] 刷新端口状态');
      loadPorts();
    });
    if (el.btnClosePorts) el.btnClosePorts.addEventListener('click', function () {
      fetch('/api/room/ports', { method: 'GET' }).then(function (r) { return r.json(); })
        .then(function (res) {
          var rows = (res && res.ports) || [];
          if (!rows.length) { toast('当前没有运行中的联机服务'); return; }
          rows.forEach(function (row) { closeRoomPort(row.port, null); });
        })
        .catch(function (err) { toast('读取端口状态失败：' + (err && err.message ? err.message : String(err))); });
    });

    el.btnSideBlue.addEventListener('click', function () { chooseSide('blue'); });
    el.btnSideRed.addEventListener('click', function () { chooseSide('red'); });
    el.btnNextGame.addEventListener('click', function () { act('/next_game', {}, '进入下一局'); });
    if (el.btnAutoBp) el.btnAutoBp.addEventListener('click', autoCompleteBp);

    window.addEventListener('beforeunload', function () { stopLoops(); });
  }

  /* ------------------------------ 启动 ------------------------------ */
  function loadLeagues() {
    var fn = KPL.api && KPL.api.leagues;
    if (typeof fn !== 'function') return;
    fn().then(function (res) {
      var rows = (res && (res.leagues || res)) || [];
      if (!Array.isArray(rows)) return;
      var html = '';
      rows.forEach(function (r) {
        html += '<option value="' + esc(r.league_id) + '">' + esc(r.name) + '</option>';
      });
      el.hostLeague.innerHTML = html;
      var def = (KPL.getLeagueId && KPL.getLeagueId()) || '';
      if (def) el.hostLeague.value = def;
    }).catch(function () {});
  }

  function boot() {
    bindEvents();
    bootBoard();
    // v9 调试入口：控制台可用 __friend() 查看联机状态（不会泄露密钥）
    window.__friend = function () {
      return {
        base: S.base, pid: S.pid, role: S.role, mode: S.mode, busy: S.busy,
        hasRoom: !!S.room, hasMatch: !!S.match,
        match: S.match ? { step: S.match.step, phase: S.match.phase, my_turn: S.match.my_turn, side: S.match.side } : null
      };
    };
    loadLeagues();
    loadTemplates();
    loadFriendSaves();
    if (el.leagueTemplateWrap) el.leagueTemplateWrap.hidden = S.mode !== 'league';
    if (el.leagueEditionWrap) el.leagueEditionWrap.hidden = S.mode !== 'league';
    if (el.resumeCard) el.resumeCard.hidden = S.mode !== 'league';
    loadPorts();
    log('朋友 PK 已就绪：选择「创建房间」或「加入房间」');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  window.KPL_FRIEND = { state: S, poll: poll };
})();
