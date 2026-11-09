/* ============================================================
   career.js — 「我的」生涯页
   数据源：localStorage['kpl.profile']（契约 §8），无后端接口。
   优先用 KPL.profile.get/save，缺失则自己读写 localStorage。
   ============================================================ */
(function () {
  'use strict';

  var KPL = (window.KPL = window.KPL || {});
  var LS_KEY = 'kpl.profile';
  var LEAGUE_KEY = 'kpl.league_id';

  var DEFAULT_PROFILE = {
    nickname: '教练',
    total_games: 0,
    wins: 0,
    losses: 0,
    hero_usage: {},
    last_played: null
  };

  /* ---------------- 工具 ---------------- */
  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function num(v) {
    if (typeof KPL.fmtNum === 'function') { try { return KPL.fmtNum(v); } catch (e) {} }
    return isNum(v) ? String(Math.round(v * 100) / 100) : '—';
  }
  function int(v) {
    if (typeof KPL.fmtInt === 'function') { try { return KPL.fmtInt(v); } catch (e) {} }
    return isNum(v) ? String(Math.round(v)) : '—';
  }
  function pct(v) {
    if (typeof KPL.fmtPct === 'function') { try { return KPL.fmtPct(v); } catch (e) {} }
    return isNum(v) ? v.toFixed(2) + '%' : '—';
  }
  function toast(msg) {
    if (typeof KPL.toast === 'function') { try { return KPL.toast(msg); } catch (e) {} }
    if (window.console && console.warn) console.warn('[kpl]', msg);
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ---------------- 头像（与数据页同一套兜底） ---------------- */
  if (typeof window.__kplAvtFallback !== 'function') {
    window.__kplAvtFallback = function (img) {
      try {
        var box = img.parentNode;
        if (!box) return;
        box.classList.add('avt-fallback');
        var txt = box.querySelector('.avt-txt');
        if (!txt) {
          txt = document.createElement('span');
          txt.className = 'avt-txt';
          txt.textContent = img.getAttribute('data-fb') || '?';
          box.appendChild(txt);
        }
        box.removeChild(img);
      } catch (e) { /* noop */ }
    };
  }

  function avatarHtml(path, label, sizeCls) {
    var fb = esc(String(label || '?').slice(0, 1));
    if (path) {
      return '<span class="avt ' + sizeCls + '">' +
        '<img src="' + esc(path) + '" alt="' + esc(label) + '" loading="lazy" data-fb="' + fb +
        '" onerror="window.__kplAvtFallback&&window.__kplAvtFallback(this)">' +
        '<span class="avt-txt">' + fb + '</span></span>';
    }
    return '<span class="avt ' + sizeCls + ' avt-fallback"><span class="avt-txt">' + fb + '</span></span>';
  }

  /* ---------------- profile 读写（双保险） ---------------- */
  function sanitizeProfile(raw) {
    var p = {};
    var src = raw && typeof raw === 'object' ? raw : {};
    p.nickname = (typeof src.nickname === 'string' && src.nickname.trim()) ? src.nickname.trim() : DEFAULT_PROFILE.nickname;
    p.total_games = isNum(src.total_games) ? Math.max(0, Math.round(src.total_games)) : 0;
    p.wins = isNum(src.wins) ? Math.max(0, Math.round(src.wins)) : 0;
    p.losses = isNum(src.losses)
      ? Math.max(0, Math.round(src.losses))
      : Math.max(0, p.total_games - p.wins);
    p.hero_usage = {};
    var hu = src.hero_usage;
    if (hu && typeof hu === 'object') {
      Object.keys(hu).forEach(function (k) {
        var v = Number(hu[k]);
        if (isFinite(v) && v > 0) p.hero_usage[String(k)] = v;
      });
    }
    p.last_played = typeof src.last_played === 'string' ? src.last_played : null;
    return p;
  }

  function readLocal() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) { return null; }
  }
  function writeLocal(obj) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(obj)); return true; }
    catch (e) { return false; }
  }

  var profileApi = (KPL.profile && typeof KPL.profile.get === 'function') ? KPL.profile : null;

  function loadProfile() {
    var raw = null;
    if (profileApi) {
      try { raw = profileApi.get(); } catch (e) { raw = null; }
    }
    if (raw == null) raw = readLocal();
    return sanitizeProfile(raw);
  }

  function saveProfile(patch) {
    var current = loadProfile();
    var merged = {};
    Object.keys(current).forEach(function (k) { merged[k] = current[k]; });
    Object.keys(patch || {}).forEach(function (k) { merged[k] = patch[k]; });
    merged = sanitizeProfile(merged);

    if (profileApi && typeof profileApi.save === 'function') {
      try {
        profileApi.save(merged);
        // 校验是否真的写进去了；失败再走本地兜底
        var check = null;
        try { check = profileApi.get(); } catch (e) { check = null; }
        if (check && typeof check === 'object') {
          var c = sanitizeProfile(check);
          if (c.nickname === merged.nickname) return merged;
        }
      } catch (e) { /* 落到本地兜底 */ }
    }
    writeLocal(merged);
    return merged;
  }

  /* ---------------- DOM ---------------- */
  function byId(id) { return document.getElementById(id); }

  var el = {
    /* --- 生涯（v3）：教练卡 / 统计 / 当前生涯 / 排行榜 --- */
    coachNo: byId('coachNo'),
    tierGlobal: byId('tierGlobal'),
    statRows: byId('statRows'),



    saveFilter: byId('saveFilter'),
    currentWrap: byId('currentWrap'),
    btnNewCareer: byId('btnNewCareer'),
    careerTemplate: byId('careerTemplate'),
    tplDesc: byId('tplDesc'),
    tplStatus: byId('tplStatus'),
    btnCoachRank: byId('btnCoachRank'),
    btnTeamRank: byId('btnTeamRank'),
    rankNote: byId('rankNote'),
    rankModal: byId('rankModal'),
    rankModalTitle: byId('rankModalTitle'),
    rankModalBody: byId('rankModalBody'),
    rankModalNote: byId('rankModalNote'),
    rankModalClose: byId('rankModalClose'),
    /* --- 我的战绩（本地 BP 记录） --- */
    nick: byId('careerNick'),
    meta: byId('careerMeta'),
    nickForm: byId('nickForm'),
    nickInput: byId('nickInput'),
    nickCancel: byId('nickCancel'),
    nickBtn: byId('btnEditNick'),
    csTotal: byId('csTotal'),
    csWins: byId('csWins'),
    csLosses: byId('csLosses'),
    csWinRate: byId('csWinRate'),
    usageList: byId('usageList'),
    usageHint: byId('usageHint'),
    badgeGrid: byId('badgeGrid'),
    badgeHint: byId('badgeHint'),
    disclaimer: byId('careerDisclaimer')
  };

  if (!el.nick) return; // 不在生涯页

  var state = { profile: null, heroes: null, index: null, leaderboard: null, saveFilter: 'all' };

  /* ---------------- 档案卡 ---------------- */
  function winRateOf(p) {
    if (!p || !isNum(p.total_games) || p.total_games <= 0) return null;
    return (p.wins / p.total_games) * 100;
  }

  function renderProfile() {
    var p = state.profile;

    el.nick.textContent = p.nickname;

    el.csTotal.textContent = int(p.total_games);
    el.csWins.textContent = int(p.wins);
    el.csLosses.textContent = int(p.losses);
    var wr = winRateOf(p);
    el.csWinRate.textContent = wr == null ? '—' : pct(wr);

    var bits = [];
    if (p.total_games > 0) bits.push('已执教 ' + p.total_games + ' 局');
    else bits.push('还没有对战记录');
    if (p.last_played) bits.push('最近 ' + p.last_played);
    el.meta.textContent = bits.join(' · ');

    el.nickInput.value = p.nickname;
  }

  /* 昵称编辑 */
  el.nickBtn.addEventListener('click', function () {
    openNickForm();
  });

  function openNickForm() {
    el.nickForm.hidden = false;
    el.nickInput.value = state.profile.nickname;
    el.nickInput.focus();
    try { el.nickInput.select(); } catch (e) {}
  }
  function closeNickForm() {
    el.nickForm.hidden = true;
  }

  el.nickCancel.addEventListener('click', closeNickForm);

  el.nickForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var v = String(el.nickInput.value || '').trim().slice(0, 16);
    if (!v) { toast('昵称不能为空'); el.nickInput.focus(); return; }
    state.profile = saveProfile({ nickname: v });
    renderProfile();
    closeNickForm();
    toast('昵称已保存');
  });

  el.nickInput.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); closeNickForm(); }
  });

  /* ---------------- 常用英雄 TOP5 ---------------- */
  function leagueId() {
    if (typeof KPL.getLeagueId === 'function') {
      try { var v = KPL.getLeagueId(); if (v) return String(v); } catch (e) {}
    }
    try { return localStorage.getItem(LEAGUE_KEY) || null; } catch (e) { return null; }
  }

  function fetchHeroes() {
    var id = leagueId();
    // 优先 KPL.api.heroes；缺失时用直连 fetch 兜底
    var p;
    if (KPL.api && typeof KPL.api.heroes === 'function') {
      try { p = Promise.resolve(KPL.api.heroes(id)); } catch (e) { p = Promise.reject(e); }
    } else {
      p = fetch('/api/heroes' + (id ? '?league_id=' + encodeURIComponent(id) : ''), { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
    }
    return p.then(function (d) {
      if (!d || d.ok === false) throw new Error((d && d.error) || '英雄接口失败');
      var map = {};
      (d.heroes || []).forEach(function (h) { map[String(h.ename)] = h; });
      return map;
    });
  }

  function topUsage(p) {
    var hu = p.hero_usage || {};
    var arr = Object.keys(hu).map(function (k) {
      return { ename: k, count: Number(hu[k]) || 0 };
    }).filter(function (x) { return x.count > 0; });
    arr.sort(function (a, b) {
      if (b.count !== a.count) return b.count - a.count;
      var na = Number(a.ename), nb = Number(b.ename);
      if (isFinite(na) && isFinite(nb)) return na - nb;
      return String(a.ename) < String(b.ename) ? -1 : 1;
    });
    return arr.slice(0, 5);
  }

  /* v2 §2.1 / §4：分路只看 lane_list（该赛季 KPL 实战分路）。
     缺失 / 空 → 「暂无该赛季分路数据」，不回退 role / lane / hero_type。 */
  var LANES = ['对抗路', '打野', '中路', '发育路', '游走'];
  var LANE_NONE = '暂无该赛季分路数据';

  function laneList(h) {
    var v = h && h.lane_list;
    if (!Array.isArray(v)) return [];
    var out = [];
    for (var i = 0; i < v.length; i++) {
      var s = v[i] == null ? '' : String(v[i]).trim();
      if (s && out.indexOf(s) < 0) out.push(s);
    }
    return out;
  }

  function laneChipsHtml(list) {
    if (!list || !list.length) return '<span class="lane-none">' + esc(LANE_NONE) + '</span>';
    var html = '<span class="lane-chips">';
    for (var i = 0; i < list.length; i++) {
      html += '<span class="lane-chip">' + esc(list[i]) + '</span>';
    }
    return html + '</span>';
  }

  function renderUsage(heroMap, errMsg) {
    var p = state.profile;
    var top = topUsage(p);

    if (!top.length) {
      el.usageHint.textContent = '按使用次数';
      el.usageList.innerHTML = '<div class="d-empty">还没有对战记录，去打一局吧</div>';
      return;
    }

    el.usageHint.textContent = 'TOP ' + top.length + ' · 按使用次数';

    var max = 1;
    top.forEach(function (t) { if (t.count > max) max = t.count; });

    var rows = [];
    top.forEach(function (t, i) {
      var h = (heroMap && heroMap[String(t.ename)]) || null;
      var name = h && h.cname ? h.cname : ('英雄 #' + t.ename);
      var lanes = laneList(h);
      var w = Math.max(6, Math.round((t.count / max) * 100));

      rows.push(
        '<div class="usage-item">' +
          '<span class="usage-rank">' + (i + 1) + '</span>' +
          avatarHtml(h ? h.avatar_path : null, name, 'avt-md') +
          '<span class="usage-main">' +
            '<span class="usage-name">' + esc(name) + laneChipsHtml(lanes) + '</span>' +
            '<span class="usage-bar"><span style="width:' + w + '%"></span></span>' +
          '</span>' +
          '<span class="usage-count">' + int(t.count) + '<i>次</i></span>' +
        '</div>'
      );
    });

    el.usageList.innerHTML = rows.join('') +
      (errMsg ? '<div class="d-modal-note">英雄头像暂不可用：' + esc(errMsg) + '（使用次数为本地记录）</div>' : '');
  }

  function loadUsage() {
    el.usageList.innerHTML = '<div class="d-loading"><span class="spinner"></span><span>加载中…</span></div>';
    fetchHeroes().then(function (map) {
      state.heroes = map;
      renderUsage(map, null);
    }).catch(function (err) {
      // 英雄接口挂了也要显示使用次数（本地数据），头像用文字兜底
      renderUsage({}, err && err.message ? err.message : String(err));
      toast('英雄信息加载失败，已用文字兜底');
    });
  }

  /* ---------------- 成就徽章（基于 profile 真实统计） ---------------- */
  function achievements(p) {
    var g = isNum(p.total_games) ? p.total_games : 0;
    var w = isNum(p.wins) ? p.wins : 0;
    var wr = winRateOf(p);
    var heroesUsed = Object.keys(p.hero_usage || {}).length;

    return [
      { key: 'first', icon: '⚔', name: '首战', desc: '完成第 1 场 BP 对战', got: g >= 1 },
      { key: 'veteran', icon: '🎖', name: '老练', desc: '累计完成 10 场对战', got: g >= 10 },
      { key: 'mind', icon: '🧠', name: '教练之心', desc: '累计取得 5 场胜利', got: w >= 5 },
      { key: 'deep', icon: '📚', name: '英雄池', desc: '使用过 5 名不同英雄', got: heroesUsed >= 5 },
      { key: 'sharp', icon: '🎯', name: '六十胜率', desc: '胜率达到 60%（≥10 场）', got: wr != null && g >= 10 && wr >= 60 },
      { key: 'hundred', icon: '🏅', name: '百战', desc: '累计完成 100 场对战', got: g >= 100 }
    ];
  }

  function renderBadges() {
    var p = state.profile;
    var list = achievements(p);
    var got = 0;
    var rows = [];
    list.forEach(function (b) {
      if (b.got) got++;
      rows.push(
        '<div class="badge ' + (b.got ? 'is-unlocked' : 'is-locked') + '" title="' +
          esc(b.name + '：' + b.desc) + '">' +
          '<span class="badge-icon">' + (b.got ? b.icon : '🔒') + '</span>' +
          '<span class="badge-body">' +
            '<span class="badge-name">' + esc(b.name) + '</span>' +
            '<span class="badge-desc">' + esc(b.got ? b.desc : '未解锁 · ' + b.desc) + '</span>' +
          '</span>' +
        '</div>'
      );
    });
    el.badgeGrid.innerHTML = rows.join('');
    el.badgeHint.textContent = '已解锁 ' + got + ' / ' + list.length;
  }

  /* ============================================================
   * v3 年总模式（契约 v3 §4/§5）
   * 数据全部来自真实后端：/api/career/saves、/api/career/leaderboard。
   * 严禁编造任何数字：接口缺失或为 null 时一律显示「—」。
   * ============================================================ */
  function careerApi() {
    return (KPL.api && typeof KPL.api.careerSaves === 'function') ? KPL.api : null;
  }

  function pctOrDash(v) {
    if (!isNum(v)) return '—';
    return pct(v);
  }

  function fmtDate(s) {
    if (!s) return '';
    return String(s).replace('T', ' ').slice(0, 16);
  }

  /* 每次刷新给一个稳定的「NO.2026-xx」，取存档数量派生，不编造具体编号含义 */
  function renderCoachNo(count) {
    if (!el.coachNo) return;
    el.coachNo.textContent = 'NO.2026-' + String(count).padStart(2, '0');
  }

  /* v51：按 春季赛 / 挑战者杯 / 夏季赛 / 年度总决赛 四类分别统计（数据全部来自后端 stats4） */
  var STAT4_ORDER = ['spring', 'challenger', 'summer', 'annual'];
  var STAT4_LABEL = {
    spring: '春季赛', challenger: '挑战者杯', summer: '夏季赛', annual: '年度总决赛'
  };

  function stat4RowHTML(key, bucket) {
    var b = bucket || {};
    var matched = int(b.matched || 0);
    var rate = matched ? pctOrDash(b.win_rate) : '0%';
    return '<div class="c-stat-block c-stat-row" data-bucket="' + key + '">' +
      '<div class="c-stat-head"><strong>' + esc(STAT4_LABEL[key] || key) + '</strong>' +
      '<span class="c-stat-sub">' + int(b.saves || 0) + ' 个存档</span></div>' +
      '<div class="c-stat-grid">' +
      '<div class="c-stat-cell"><span class="c-stat-num">' + matched + '</span><span class="c-stat-label">执教场次</span></div>' +
      '<div class="c-stat-cell"><span class="c-stat-num">' + int(b.wins || 0) + '</span><span class="c-stat-label">胜场</span></div>' +
      '<div class="c-stat-cell"><span class="c-stat-num">' + int(b.championship || 0) + '</span><span class="c-stat-label">冠军</span></div>' +
      '<div class="c-stat-cell"><span class="c-stat-num">' + rate + '</span><span class="c-stat-label">胜率</span></div>' +
      '</div></div>';
  }

  function renderCareerStats(payload) {
    var coach = (payload && payload.coach) || {};
    var stats4 = (payload && payload.stats4) || null;

    if (el.tierGlobal) el.tierGlobal.textContent = coach.tier || '无名之辈';

    if (!el.statRows) return;
    var rows;
    if (stats4) {
      rows = STAT4_ORDER.map(function (key) {
        return stat4RowHTML(key, stats4[key] || {});
      }).join('');
    } else {
      /* 后端没给 stats4（旧存档 / 接口降级）时，四行都显示 0，绝不拿别的分类凑数 */
      rows = STAT4_ORDER.map(function (key) {
        return stat4RowHTML(key, {});
      }).join('');
    }
    el.statRows.innerHTML = rows;
  }

  function saveCardHtml(save) {
    var ev = save.event || {};
    var st = save.stats || {};
    var done = save.status === 'done';
    var stageLabel = save.stage_label || '未开始';
    var progress = isNum(save.progress) ? Math.max(0, Math.min(100, save.progress)) : 0;
    var dayText = (isNum(save.current_day) && isNum(save.total_days))
      ? (save.current_day + '/' + save.total_days + ' 比赛日') : '';
    var teamName = (save.coach && save.coach.team_name) || '未命名战队';
    var recText = '战绩 ' + int(st.wins || 0) + '胜' + int(st.losses || 0) + '负';
    var target = '/career/season?save_id=' + encodeURIComponent(save.save_id);
    var title = done
      ? (save.champion ? '🏆 ' + esc(save.champion) + ' 夺冠' : '赛季结束')
      : '继续执教';
    if (!save.drawn) {
      target = '/career/draw?save_id=' + encodeURIComponent(save.save_id);
      title = '去抽签';
    }

    var CAT_LABEL = { league: '联赛', cup: '杯赛', package: '赛程包' };
    var catLabel = CAT_LABEL[ev.category] || '联赛';
    var saveName = esc(save.save_name || (ev.short_name || ev.name || '未命名存档'));
    /* v19：已结束的存档额外给一个「结果回看」入口，直接打开历史回看面板 */
    var historyBtn = done
      ? '<a class="c-btn c-btn-ghost c-btn-sm" href="' +
        ('/career/season?save_id=' + encodeURIComponent(save.save_id) + '&history=1') +
        '">📜 结果回看</a>'
      : '';

    return '' +
      '<section class="c-card" data-save="' + esc(save.save_id) + '">' +
        '<div class="c-current-head">' +
          '<h2 class="c-card-title" style="margin:0;">' + catLabel + ' · ' + saveName + '</h2>' +
          '<span class="c-link">' + esc(dayText) + '</span>' +
        '</div>' +
        '<h3 class="c-current-name">' + esc(teamName) + '</h3>' +
        '<div style="margin-bottom:8px;">' +
          '<span class="c-tag">' + esc(stageLabel) + '</span>' +
        '</div>' +
        '<p class="c-current-league">｜' + esc(ev.short_name || ev.name || '') + '</p>' +
        '<div class="c-progress"><i style="width:' + progress + '%"></i></div>' +
        '<div class="c-current-foot">' +
          '<span class="c-record">' + esc(recText) + '</span>' +
          historyBtn +
          '<a class="c-btn c-btn-primary c-btn-sm" href="' + target + '">' + title + ' ›</a>' +
        '</div>' +
        '<div class="c-save-actions">' +
          '<button type="button" class="c-btn c-btn-ghost c-btn-sm" data-act="rename" data-save="' + esc(save.save_id) + '">重命名</button>' +
          '<button type="button" class="c-btn c-btn-ghost c-btn-sm is-danger" data-act="delete" data-save="' + esc(save.save_id) + '">删除存档</button>' +
        '</div>' +
      '</section>';
  }

  function renderSaves(payload) {
    if (!el.currentWrap) return;
    var saves = (payload && payload.saves) || [];
    if (!saves.length) {
      el.currentWrap.innerHTML =
        '<section class="c-card"><div class="c-empty">' +
          '还没有生涯存档。<br>点下面【去选赛事，开启新的年总赛程】，或从首页「KPL 年总模式」进入选赛事页。' +
        '</div></section>';
      return;
    }
    var cat = state.saveFilter || 'all';
    var pool = saves.filter(function (s) {
      var c = (s.event || {}).category || 'league';
      return cat === 'all' || c === cat;
    });
    if (!pool.length) {
      el.currentWrap.innerHTML = '<section class="c-card"><div class="c-empty">' +
        (cat === 'all' ? '还没有生涯存档。' : '该分类下还没有存档。') +
        '<br>点下面【去选赛事，开启新的年总赛程】开始新赛程。</div></section>';
      return;
    }
    /* 运行中的排前面，其余按 updated_at 倒序（后端已排好） */
    var running = pool.filter(function (s) { return s.status !== 'done'; });
    var done = pool.filter(function (s) { return s.status === 'done'; });
    el.currentWrap.innerHTML = running.concat(done).map(saveCardHtml).join('');
  }

  function loadCareerIndex() {
    var api = careerApi();
    /* v47 修复：以前这里要求 el.currentWrap 存在才发请求 —— 但「我的」页已经不再显示存档列表
       （存档搬到 /saves），于是整段统计（联赛/杯赛场次、胜率、冠军）都拿不到数据，全部显示 0。
       现在只要接口可用就去取，列表容器不存在时只跳过存档列表渲染。 */
    if (!api) return Promise.resolve(null);
    return Promise.resolve(api.careerSaves()).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '生涯接口失败');
      state.index = res;
      renderCoachNo(((res.saves || []).length) || 0);
      renderCareerStats(res);
      if (el.currentWrap) renderSaves(res);
      return res;
    }).catch(function (err) {
      if (el.currentWrap) {
        el.currentWrap.innerHTML =
          '<section class="c-card"><div class="c-empty">生涯数据读取失败：' +
          esc(err && err.message ? err.message : String(err)) + '</div></section>';
      }
      renderCareerStats(null);
      return null;
    });
  }

  /* ---------- 排行榜弹层 ---------- */
  function openRankModal(kind) {
    if (!el.rankModal || !el.rankModalBody) return;
    el.rankModal.hidden = false;
    el.rankModalTitle.textContent = kind === 'coach' ? '十大教练冠军榜' : 'KPL 战队冠军榜（杯赛）';
    el.rankModalBody.innerHTML = '<div class="c-skel"></div>';
    el.rankModalNote.textContent = '';

    var api = careerApi();
    var p = (api && typeof api.careerLeaderboard === 'function')
      ? Promise.resolve(api.careerLeaderboard())
      : Promise.reject(new Error('排行榜接口不可用'));

    p.then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '排行榜失败');
      state.leaderboard = res;
      var rows = (kind === 'coach') ? (res.coaches || []) : (res.teams || []);
      if (!rows.length) {
        el.rankModalBody.innerHTML = '<div class="c-empty">还没有冠军记录。<br>带队夺冠后这里才会出现名字。</div>';
      } else {
        var html = '<ul class="c-list-plain">';
        rows.slice(0, 10).forEach(function (r, i) {
          var val;
          if (kind === 'coach') {
            val = '联赛 ' + int(r.league || 0) + ' · 杯赛 ' + int(r.cup || 0) + ' · 合计 ' + int(r.total || 0);
          } else {
            val = '联赛 ' + int(r.league || 0) + ' · 挑杯 ' + int(r.challenger || 0) +
                  ' · 年总 ' + int(r.annual || 0) + ' · 合计 ' + int(r.total || 0);
          }
          html += '<li>' +
            '<span class="c-lp-rank' + (i < 3 ? ' is-top' : '') + '">' + (i + 1) + '</span>' +
            '<span class="c-lp-name">' + esc(r.name) + '</span>' +
            '<span class="c-lp-val">' + val + '</span>' +
          '</li>';
        });
        html += '</ul>';
        el.rankModalBody.innerHTML = html;
      }
      el.rankModalNote.textContent = res.note || '';
    }).catch(function (err) {
      el.rankModalBody.innerHTML = '<div class="c-empty">排行榜读取失败：' +
        esc(err && err.message ? err.message : String(err)) + '</div>';
    });
  }

  function closeRankModal() {
    if (el.rankModal) el.rankModal.hidden = true;
  }

  if (el.btnCoachRank) el.btnCoachRank.addEventListener('click', function () { openRankModal('coach'); });
  if (el.btnTeamRank) el.btnTeamRank.addEventListener('click', function () { openRankModal('team'); });
  if (el.rankModalClose) el.rankModalClose.addEventListener('click', closeRankModal);
  if (el.rankModal) {
    el.rankModal.addEventListener('click', function (e) {
      if (e.target === el.rankModal) closeRankModal();
    });
  }
  /* ---------------- v7 需求 1：赛事选择下拉（默认年度赛程包） ---------------- */
  var TEMPLATE_ORDER = ['annual_package', 'spring', 'challenger', 'autumn'];
  var TEMPLATE_LABEL = {
    annual_package: '年度赛程包（春季赛 → 挑战者杯 → 夏季赛 → 年度总决赛）',
    spring: '春季赛（单赛季 · SAB 三阶段 + 季后赛）',
    challenger: '挑战者杯（单杯赛 · 32 队）',
    autumn: '夏季赛（单赛季 · SAB 三阶段 + 季后赛）'
  };
  var TEMPLATE_DESC = {
    annual_package: '年度赛程包：春季赛 → 挑战者杯 → 夏季赛 → 年度总决赛，逐站结算累加年度积分，前 12 名进入年总。',
    spring: '单赛季：18 队 SAB 三阶段 → 卡位赛升降级 → 双败季后赛，赛季结束发放年度积分。',
    challenger: '单杯赛：32 队单败 BO5 → 8 队双败 BO7 → 总决赛 BO9。',
    autumn: '单赛季：与春季赛同赛制（18 队 SAB 三阶段 + 卡位赛 + 双败季后赛），年度积分更高。'
  };
  var TPL_STORE = 'kpl.careerTemplate';

  function readTemplate() {
    try { return window.localStorage.getItem(TPL_STORE) || 'annual_package'; } catch (e) { return 'annual_package'; }
  }
  function writeTemplate(key) {
    try { window.localStorage.setItem(TPL_STORE, key); } catch (e) {}
  }
  function renderTplDesc(key) {
    if (el.tplDesc) el.tplDesc.textContent = TEMPLATE_DESC[key] || TEMPLATE_DESC.annual_package;
  }

  function loadCareerTemplates() {
    if (!el.careerTemplate) return;
    var api = KPL.api || null;
    if (!api || typeof api.tourneyTemplates !== 'function') {
      el.careerTemplate.innerHTML = '<option value="annual_package">年度赛程包</option>';
      return;
    }
    if (el.tplStatus) el.tplStatus.textContent = '（加载中…）';
    api.tourneyTemplates().then(function (res) {
      var list = (res && res.templates) || [];
      var byKey = {};
      list.forEach(function (t) { byKey[t.key || t.template] = t; });
      var keys = TEMPLATE_ORDER.filter(function (k) { return byKey[k]; });
      if (!keys.length) keys = Object.keys(byKey);
      el.careerTemplate.innerHTML = keys.map(function (k) {
        var label = TEMPLATE_LABEL[k] || (byKey[k] && (byKey[k].name || k)) || k;
        return '<option value="' + k + '">' + label + '</option>';
      }).join('');
      var pick = readTemplate();
      if (keys.indexOf(pick) < 0) pick = keys.indexOf('annual_package') >= 0 ? 'annual_package' : keys[0];
      el.careerTemplate.value = pick;
      renderTplDesc(pick);
      if (el.tplStatus) el.tplStatus.textContent = '';
      if (window.console && console.info) {
        console.info('[career] 赛事模板下拉就绪：' + keys.join(',') + '，默认 ' + pick);
      }
    }).catch(function (err) {
      el.careerTemplate.innerHTML =
        '<option value="annual_package">年度赛程包（模板接口读取失败，用默认）</option>';
      if (el.tplStatus) el.tplStatus.textContent = '（模板列表读取失败：' + (err && err.message ? err.message : '网络错误') + '）';
      renderTplDesc('annual_package');
    });

    el.careerTemplate.addEventListener('change', function () {
      writeTemplate(el.careerTemplate.value);
      renderTplDesc(el.careerTemplate.value);
      if (window.console && console.info) {
        console.info('[career] 赛事模板切换 → ' + el.careerTemplate.value);
      }
    });
  }

  if (el.btnNewCareer) {
    el.btnNewCareer.addEventListener('click', function () {
      // v47：存档页的「去选赛事」直接进选赛事页（年总模式入口）
      if (el.careerTemplate === null || el.careerTemplate === undefined) {
        window.location.href = '/career/team';
        return;
      }
      var tpl = (el.careerTemplate && el.careerTemplate.value) || readTemplate() || 'annual_package';
      if (window.console && console.info) console.info('[career] 新建赛程 template=' + tpl);
      window.location.href = '/career/team?template=' + encodeURIComponent(tpl);
    });
  }

  /* ---------------- 免责声明去重 ---------------- */
  function dedupeDisclaimer() {
    if (!el.disclaimer) return;
    // 若 base.html 已自带同义声明，就不再重复展示
    var markers = ['.site-footer .disclaimer', 'footer .disclaimer', '#siteDisclaimer', '.app-disclaimer'];
    for (var i = 0; i < markers.length; i++) {
      var node = document.querySelector(markers[i]);
      if (node && node !== el.disclaimer) { el.disclaimer.hidden = true; return; }
    }
    var f = document.querySelector('footer');
    if (f && /免责|版权归|官方无关/.test(f.textContent || '') && !f.contains(el.disclaimer)) {
      el.disclaimer.hidden = true;
    }
  }

  /* ---------------- 启动 ---------------- */
  /* ---------- 存档分类筛选 / 重命名 / 删除 ---------- */
  function bindSaveActions() {
    if (el.saveFilter) {
      el.saveFilter.addEventListener('click', function (evt) {
        var btn = evt.target.closest ? evt.target.closest('.c-filter-btn') : null;
        if (!btn) return;
        var cat = btn.getAttribute('data-cat') || 'all';
        if (state.saveFilter === cat) return;
        state.saveFilter = cat;
        Array.prototype.forEach.call(el.saveFilter.querySelectorAll('.c-filter-btn'), function (b) {
          var on = b === btn;
          b.classList.toggle('is-active', on);
          b.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        console.info('[career] 存档筛选 →', cat);
        if (state.index) renderSaves(state.index);
      });
    }
    if (!el.currentWrap) return;
    el.currentWrap.addEventListener('click', function (evt) {
      var btn = evt.target.closest ? evt.target.closest('[data-act]') : null;
      if (!btn) return;
      var act = btn.getAttribute('data-act');
      var saveId = btn.getAttribute('data-save');
      if (!saveId) return;
      evt.preventDefault();
      if (act === 'rename') { renameSave(saveId); }
      else if (act === 'delete') { deleteSave(saveId); }
    });
  }

  function renameSave(saveId) {
    var api = careerApi();
    if (!api || !api.tourneyRename) { toast('存档接口不可用'); return; }
    var cur = null;
    ((state.index && state.index.saves) || []).forEach(function (s) {
      if (s.save_id === saveId) cur = s;
    });
    var oldName = (cur && cur.save_name) || '';
    var name = window.prompt('给这个存档起个名字（最多 24 字）：', oldName);
    if (name === null) return;
    name = String(name).trim();
    if (!name) { toast('存档名称不能为空'); return; }
    console.info('[career] 重命名存档', saveId, '→', name);
    Promise.resolve(api.tourneyRename(saveId, name)).then(function (res) {
      if (res && res.ok === false) throw new Error(res.error || '重命名失败');
      toast('存档已重命名');
      return loadCareerIndex();
    }).catch(function (err) {
      toast('重命名失败：' + (err && err.message ? err.message : String(err)));
    });
  }

  function deleteSave(saveId) {
    var api = careerApi();
    if (!api || !api.careerDelete) { toast('存档接口不可用'); return; }
    if (!window.confirm('删除后该赛程的抽签、比赛结果、年度积分都会丢失，确定删除？')) return;
    console.info('[career] 删除存档', saveId);
    Promise.resolve(api.careerDelete(saveId)).then(function (res) {
      if (res && res.ok === false) throw new Error(res.error || '删除失败');
      toast('存档已删除');
      return loadCareerIndex();
    }).catch(function (err) {
      toast('删除失败：' + (err && err.message ? err.message : String(err)));
    });
  }

  function boot() {
    bindSaveActions();
    state.profile = loadProfile();
    renderProfile();
    renderBadges();
    loadUsage();
    loadCareerTemplates();
    loadCareerIndex();
    dedupeDisclaimer();

    // 跨页面（其他标签页）同步
    window.addEventListener('storage', function (e) {
      if (e.key !== LS_KEY) return;
      state.profile = loadProfile();
      renderProfile();
      renderBadges();
      renderUsage(state.heroes || {}, null);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
