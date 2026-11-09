/* ==========================================================================
 * KPL 模拟 BP 教练 —— 前端共享基础层（api.js）
 * 原生 JS，无框架、无构建。挂载 window.KPL，供所有页面复用。
 * 契约见 docs/DATA_CONTRACT.md（v1）与 docs/DATA_CONTRACT_V2.md（v2 增补）
 * --------------------------------------------------------------------------
 * 导出概览：
 *   KPL.api.*                后端接口封装（全部返回 Promise<object>）
 *                            v2 新增 api.bpSideChoice(mid, side) 败方选边
 *   KPL.getLeagueId() / setLeagueId(id) / onLeague(fn)   全局赛季状态
 *   KPL.fmtPct / fmtNum / fmtInt                          数值格式化（null → '—'）
 *   KPL.heroImg(h, opts) / teamLogo(t, opts)              返回 HTML 字符串
 *   KPL.toast(msg)                                        轻提示
 *   KPL.profile.get() / save(patch)                       本地教练档案
 * 额外便捷项（非契约，可用可不用）：
 *   KPL.DASH, KPL.util.esc/noop, KPL.resolveLeagueId(), KPL.getLeagueName(id), KPL.imgFallback(img)
 * ========================================================================== */
(function (global) {
  'use strict';

  var document = global.document;
  var LEAGUE_KEY = 'kpl.league_id';
  var PROFILE_KEY = 'kpl.profile';
  var DASH = '—';

  var PROFILE_DEFAULT = {
    nickname: '教练',
    total_games: 0,
    wins: 0,
    losses: 0,
    hero_usage: {},
    last_played: null
  };

  /* ------------------------------------------------------------------ *
   * 小工具
   * ------------------------------------------------------------------ */
  function noop() {}

  function lsGet(key) {
    try {
      return global.localStorage ? global.localStorage.getItem(key) : null;
    } catch (e) {
      return null; // 隐私模式 / 被禁用
    }
  }

  function lsSet(key, value) {
    try {
      if (!global.localStorage) return false;
      global.localStorage.setItem(key, value);
      return true;
    } catch (e) {
      return false;
    }
  }

  function isNil(v) {
    return v === null || v === undefined || v === '';
  }

  /** 转数值；无法转换 / 非有限数 → null（不是 0！） */
  function toNum(v) {
    if (isNil(v)) return null;
    if (typeof v === 'number') return isFinite(v) ? v : null;
    var n = parseFloat(String(v));
    return isFinite(n) ? n : null;
  }

  var ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function esc(s) {
    if (isNil(s)) return '';
    return String(s).replace(/[&<>"']/g, function (c) { return ESC_MAP[c]; });
  }

  /** 取首个「字」（中文/emoji 安全） */
  function firstChar(name) {
    var s = String(isNil(name) ? '' : name).trim();
    if (!s) return '?';
    if (typeof Array.from === 'function') {
      var arr = Array.from(s);
      return arr[0] || '?';
    }
    return s.charAt(0) || '?';
  }

  function debounce(fn, wait) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      if (t) clearTimeout(t);
      t = setTimeout(function () { t = null; fn.apply(self, args); }, wait || 120);
    };
  }

  /* ------------------------------------------------------------------ *
   * 数值格式化（契约 §7：null 一律 '—'，不得用 0 冒充）
   * ------------------------------------------------------------------ */
  function fmtPct(v, digits) {
    var n = toNum(v);
    if (n === null) return DASH;
    var d = (digits === undefined || digits === null) ? 1 : digits;
    return n.toFixed(d) + '%';
  }

  function fmtNum(v, digits) {
    var n = toNum(v);
    if (n === null) return DASH;
    var d = (digits === undefined || digits === null) ? 1 : digits;
    return n.toFixed(d);
  }

  function fmtInt(v) {
    var n = toNum(v);
    if (n === null) return DASH;
    return String(Math.round(n));
  }

  /* ------------------------------------------------------------------ *
   * HTTP
   * ------------------------------------------------------------------ */
  function buildQuery(params) {
    if (!params) return '';
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null) return; // 自动丢弃 undefined / null
      if (typeof v === 'object') return;
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function parseResponse(res) {
    return res.text().then(function (text) {
      var data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (e) {
          throw new Error('服务返回了非 JSON 内容（HTTP ' + res.status + '）');
        }
      } else {
        data = {};
      }
      if (!res.ok) {
        var msg = (data && (data.error || data.message)) || ('请求失败（HTTP ' + res.status + '）');
        throw new Error(msg);
      }
      if (data && data.ok === false) {
        throw new Error(data.error || data.message || '接口返回失败');
      }
      return data;
    });
  }

  /* ------------------------------------------------------------------ *
   * v23：全局「正在处理」细进度条
   * 请求超过 120ms 才显示（快请求不闪），让每次点击都有即时反馈。
   * ------------------------------------------------------------------ */
  var busyCount = 0;
  var busyTimer = null;
  var busyBar = null;

  function busyEl() {
    if (busyBar && busyBar.parentNode) return busyBar;
    if (!document || !document.body) return null;
    busyBar = document.createElement('div');
    busyBar.className = 'kpl-busy-bar';
    document.body.appendChild(busyBar);
    void busyBar.offsetWidth;   // 触发一次重排，后面的过渡才生效
    return busyBar;
  }

  function busyStart() {
    busyCount += 1;
    if (busyTimer || busyCount !== 1) return;
    busyTimer = setTimeout(function () {
      busyTimer = null;
      var bar = busyEl();
      if (bar) bar.classList.add('is-on');
    }, 120);
  }

  function busyEnd() {
    busyCount = Math.max(0, busyCount - 1);
    if (busyCount > 0) return;
    if (busyTimer) { clearTimeout(busyTimer); busyTimer = null; }
    var bar = busyEl();
    if (bar) bar.classList.remove('is-on');
  }

  /** 统一入口：loading 计数 + JSON 解析 + 错误抛出 */
  function request(url, init) {
    busyStart();
    var p;
    try {
      p = global.fetch(url, init);
    } catch (e) {
      busyEnd();
      return Promise.reject(e);
    }
    return p.then(function (res) {
      return parseResponse(res);
    }).then(function (data) {
      busyEnd();
      return data;
    }, function (err) {
      busyEnd();
      throw err;
    });
  }

  /* ------------------------------------------------------------------ *
   * v30：只读接口的内存缓存（英雄表 / 战队表 / 赛季表 / 赛事模板）
   * 这些数据在一次运行期间不会变，省掉每次进页面都重新拉一遍。
   * ------------------------------------------------------------------ */
  var MEMO = {};

  function memoGet(path, params, key) {
    var k = key || (path + buildQuery(params));
    if (Object.prototype.hasOwnProperty.call(MEMO, k)) {
      return Promise.resolve(MEMO[k]);
    }
    return get(path, params).then(function (data) {
      if (data && data.ok !== false) MEMO[k] = data;
      return data;
    });
  }

  function get(path, params) {
    var url = path + buildQuery(params);
    return request(url, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin'
    });
  }

  function post(path, body) {
    return request(path, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
      },
      credentials: 'same-origin',
      body: JSON.stringify(body || {})
    });
  }

  var api = {
    get: get,
    post: post,

    leagues: function () {
      return memoGet('/api/leagues', null, 'leagues').then(function (res) {
        // 顺带缓存一份赛季列表，供 getLeagueName() 同步取名字
        if (res && res.leagues) leagueCacheList = res.leagues;
        return res;
      });
    },
    leagueMeta: function (id) {
      return get('/api/league_meta', { league_id: id });
    },
    heroes: function (id) {
      return memoGet('/api/heroes', { league_id: id }, 'heroes|' + (id || ''));
    },
    teams: function (id) {
      return memoGet('/api/teams', { league_id: id }, 'teams|' + (id || ''));
    },
    players: function (id) {
      return get('/api/players', { league_id: id });
    },
    bpHistory: function (id, limit) {
      return get('/api/bp/history', { league_id: id, limit: limit });
    },

    bpNew: function (opts) {
      return post('/api/bp/new', opts || {});
    },
    bpState: function (mid) {
      return get('/api/bp/state', { match_id: mid });
    },
    bpAction: function (mid, ename, action, side) {
      var body = { match_id: mid, ename: ename, action: action };
      // v20：巅峰对决盲选时双方同时可落子，需要显式带上本方阵营
      if (side === 'blue' || side === 'red') body.side = side;
      return post('/api/bp/action', body);
    },
    bpAutoComplete: function (mid) {
      // v17：一键 BP —— 本小局剩余 ban/pick 全部由内置 AI 走完
      return post('/api/bp/auto_complete', { match_id: mid });
    },
    bpAuto: function (mid) {
      return post('/api/bp/auto', { match_id: mid });
    },
    bpNextGame: function (mid) {
      return post('/api/bp/next_game', { match_id: mid });
    },
    bpScore: function (mid) {
      return post('/api/bp/score', { match_id: mid });
    },
    /* v2：败方选边（契约 v2 §3.3）。仅在 phase === 'side_choice' 时可用，否则后端 409。 */
    bpSideChoice: function (mid, side) {
      return post('/api/bp/side_choice', { match_id: mid, side: side });
    },

    /* ---------------------------------------------------------------------
     * v3 年总模式（契约 v3 §4）。全部返回后端原始 JSON（{ok:true, ...}），
     * 由各页面自行取值，api.js 不做二次包装。
     * ------------------------------------------------------------------- */
    careerEvents: function () {
      return memoGet('/api/tourney/templates', null, 'tourney|templates');
    },
    careerTeams: function (leagueId) {
      return get('/api/career/teams', { league_id: leagueId });
    },
    careerSaves: function () {
      return get('/api/tourney/saves');
    },
    careerLeaderboard: function () {
      return get('/api/tourney/leaderboard');
    },
    careerNew: function (opts) {
      return post('/api/tourney/new', opts || {});
    },
    careerState: function (saveId) {
      return get('/api/tourney/state', { save_id: saveId });
    },
    careerSchedule: function (saveId, day, onlyMine) {
      return get('/api/tourney/schedule', {
        save_id: saveId,
        day: (day === null || day === undefined || day === '') ? undefined : day,
        only_mine: onlyMine ? 1 : undefined
      });
    },
    careerStandings: function (saveId) {
      return get('/api/tourney/standings', { save_id: saveId });
    },
    careerBracket: function (saveId) {
      return get('/api/tourney/bracket', { save_id: saveId });
    },
    careerSimulate: function (saveId, opts) {
      var body = { save_id: saveId };
      var extra = opts || {};
      Object.keys(extra).forEach(function (k) {
        if (k === 'save_id') return;
        body[k] = extra[k];
      });
      return post('/api/tourney/simulate', body);
    },
    careerNextStage: function (saveId) {
      return Promise.resolve({ ok: true, advanced: false, note: '阶段自动进阶' });
    },
    /* v28：玩家点「开启下一赛程 / 开始淘汰赛抽签」→ 真正开下一站或进淘汰赛 */
    tourneyDrawCommit: function (saveId) {
      return post('/api/tourney/draw_commit', { save_id: saveId });
    },
    /* v31：年总突围赛「选对手」 */
    tourneyBreakout: function (saveId) {
      return get('/api/tourney/breakout', { save_id: saveId });
    },
    tourneyBreakoutPick: function (saveId, opponent) {
      return post('/api/tourney/breakout_pick', { save_id: saveId, opponent: opponent || null });
    },
    tourneyBreakoutCommit: function (saveId) {
      return post('/api/tourney/breakout_commit', { save_id: saveId });
    },
    careerResult: function (opts) {
      return post('/api/tourney/result', opts || {});
    },
    /* v6：标准 KPL 赛制引擎（与 v3 生涯接口同形，页面无需改动） */
    tourneyTemplates: function () {
      return get('/api/tourney/templates');
    },
    tourneyRules: function () {
      return get('/api/tourney/rules');
    },
    tourneyAnnual: function (saveId) {
      return get('/api/tourney/annual', { save_id: saveId });
    },
    tourneyNextMatch: function (saveId) {
      return get('/api/tourney/next_match', { save_id: saveId });
    },
    tourneyRename: function (saveId, name) {
      return post('/api/tourney/rename', { save_id: saveId, save_name: name });
    },
    careerDelete: function (saveId) {
      return post('/api/tourney/delete', { save_id: saveId });
    },

    /* ---------------------------------------------------------------------
     * v19 历史回看（只读）。这两个接口都不写存档，看完随时可以继续打。
     *   tourneyHistory(saveId)                → 所有系列赛 × 所有阶段索引
     *   tourneyHistoryStage(saveId, s, stage) → 某站某阶段明细（赛程/积分榜/对阵图）
     * ------------------------------------------------------------------- */
    tourneyHistory: function (saveId) {
      return get('/api/tourney/history', { save_id: saveId });
    },
    tourneyHistoryStage: function (saveId, series, stage) {
      return get('/api/tourney/history_stage', {
        save_id: saveId,
        series: (series === null || series === undefined || series === '') ? undefined : series,
        stage: stage
      });
    },

    /* ---------------------------------------------------------------------
     * v4 设置 / 大模型 / 朋友 PK 房间（房主端）
     * ------------------------------------------------------------------- */
    settings: function () {
      return get('/api/settings');
    },
    scoringSave: function (patch) {
      return post('/api/settings/scoring', patch || {});
    },
    scoringReset: function () {
      return post('/api/settings/scoring/reset', {});
    },

    saveSettings: function (patch) {
      return post('/api/settings', patch || {});
    },
    llmTest: function (patch) {
      return post('/api/settings/llm_test', patch || {});
    },
    llmStatus: function () {
      return get('/api/llm/status');
    },
    roomLocalIps: function () {
      return get('/api/room/local_ips');
    },
    roomHostStart: function (opts) {
      return post('/api/room/host/start', opts || {});
    },
    roomHostStop: function () {
      return post('/api/room/host/stop', {});
    },
    roomHostStatus: function () {
      return get('/api/room/host/status');
    }
  };

  /* ------------------------------------------------------------------ *
   * 全局赛季状态（localStorage['kpl.league_id']）
   * ------------------------------------------------------------------ */
  var leagueSubs = [];
  var leagueCacheDefault = null; // /api/leagues 拉到的 default_league_id 缓存
  var leagueCacheList = null;    // /api/leagues 拉到的赛季列表缓存

  function getLeagueId() {
    var raw = lsGet(LEAGUE_KEY);
    if (raw === null || raw === undefined) return null;
    raw = String(raw).trim();
    return raw === '' ? null : raw;
  }

  function setLeagueId(id) {
    if (isNil(id)) return null;
    id = String(id);
    var prev = getLeagueId();
    lsSet(LEAGUE_KEY, id);
    if (prev !== id) notifyLeague(id);
    return id;
  }

  function notifyLeague(id) {
    leagueSubs.slice().forEach(function (fn) {
      try {
        fn(id);
      } catch (e) {
        if (global.console) console.error('[KPL] onLeague 回调异常：', e);
      }
    });
  }

  /** 注册赛季变更回调，返回取消函数 */
  function onLeague(fn) {
    if (typeof fn !== 'function') return noop;
    leagueSubs.push(fn);
    return function off() {
      var i = leagueSubs.indexOf(fn);
      if (i >= 0) leagueSubs.splice(i, 1);
    };
  }

  /** 便捷：拿到一个可用 league_id（本地已有就直接用，否则用 /api/leagues 的默认值） */
  function resolveLeagueId() {
    var id = getLeagueId();
    if (id) return Promise.resolve(id);
    if (leagueCacheDefault) return Promise.resolve(leagueCacheDefault);
    return api.leagues().then(function (res) {
      var leagues = (res && res.leagues) || [];
      var def = res && res.default_league_id;
      if (!def && leagues.length) def = leagues[leagues.length - 1].league_id;
      if (def) {
        leagueCacheDefault = String(def);
        setLeagueId(leagueCacheDefault);
        return leagueCacheDefault;
      }
      return null;
    }).catch(function () { return null; });
  }

  /**
   * 同步取当前赛季中文名（供队友渲染用）。
   * 需要先调用过 api.leagues()（index/data 页都会调），否则返回 '—'。
   * 异步场景请直接用 /api/bp/state 里的 match.league_name。
   */
  function getLeagueName(id) {
    var want = isNil(id) ? getLeagueId() : String(id);
    if (!want || !leagueCacheList) return DASH;
    for (var i = 0; i < leagueCacheList.length; i++) {
      if (String(leagueCacheList[i].league_id) === want) {
        return leagueCacheList[i].name || want;
      }
    }
    return DASH;
  }

  /* ------------------------------------------------------------------ *
   * 头像 / Logo（返回 HTML 字符串）
   *   - 正常：真实 <img loading="lazy">
   *   - h 缺失 / avatar_ok === false / 图片 onerror  → 文字首字兜底
   *   onerror 兜底完全内联在字符串里，不依赖外部事件绑定。
   * ------------------------------------------------------------------ */
  var ONERROR_JS =
    "var p=this.parentNode;" +
    "if(p){this.style.display='none';" +
    "var f=p.querySelector('.avatar-fallback');" +
    "if(f){f.hidden=false;f.removeAttribute('hidden');}}";

  var IMG_STYLE = 'display:block;width:100%;height:100%;object-fit:cover;';

  function sizeStyle(size) {
    var n = toNum(size);
    if (n === null || n <= 0) return '';
    return ' style="width:' + n + 'px;height:' + n + 'px"';
  }

  function avatarMarkup(extraClass, name, src, opts) {
    opts = opts || {};
    var cls = extraClass + (opts.cls ? ' ' + opts.cls : '');
    var st = sizeStyle(opts.size);
    var label = esc(name);

    if (isNil(src)) {
      // 没有图：直接用兜底块
      return '<span class="avatar avatar-fallback ' + extraClass + (opts.cls ? ' ' + opts.cls : '') + '"' +
        st + ' title="' + label + '" aria-label="' + label + '">' + esc(firstChar(name)) + '</span>';
    }

    return '<span class="avatar ' + cls + '"' + st + '>' +
      '<img src="' + esc(src) + '" alt="' + label + '" loading="lazy" decoding="async" ' +
      'style="' + IMG_STYLE + '" onerror="' + ONERROR_JS + '">' +
      '<span class="avatar-fallback" hidden>' + esc(firstChar(name)) + '</span>' +
      '</span>';
  }

  function heroImg(h, opts) {
    opts = opts || {};
    var name = h ? (h.cname || h.hero || h.name || '') : '';
    var src = null;
    if (h && h.avatar_ok !== false && !isNil(h.avatar_path)) src = h.avatar_path;
    return avatarMarkup('hero-avatar', name, src, opts);
  }

  function teamLogo(t, opts) {
    opts = opts || {};
    var name = t ? (t.name || t.abbreviation || '') : '';
    var src = null;
    if (t && t.logo_ok !== false && !isNil(t.logo_path)) src = t.logo_path;
    // logo 兜底优先用缩写（更短更清晰）
    var fallbackName = (t && t.abbreviation && String(t.abbreviation).length <= 4)
      ? t.abbreviation
      : name;
    if (!isNil(src)) {
      // 有图时兜底文字也用缩写
      var cls = 'team-logo' + (opts.cls ? ' ' + opts.cls : '');
      var st = sizeStyle(opts.size);
      var label = esc(name || fallbackName);
      return '<span class="avatar ' + cls + '"' + st + '>' +
        '<img src="' + esc(src) + '" alt="' + label + '" loading="lazy" decoding="async" ' +
        'style="' + IMG_STYLE + '" onerror="' + ONERROR_JS + '">' +
        '<span class="avatar-fallback" hidden>' + esc(fallbackName) + '</span>' +
        '</span>';
    }
    return '<span class="avatar avatar-fallback team-logo' + (opts.cls ? ' ' + opts.cls : '') + '"' +
      sizeStyle(opts.size) + ' title="' + esc(name) + '">' +
      esc(String(fallbackName || firstChar(name)).slice(0, 4)) + '</span>';
  }

  /** 兜底全局钩子（页面自己写了 onerror="KPL.imgFallback(this)" 时可用） */
  function imgFallback(img) {
    if (!img || !img.parentNode) return;
    img.style.display = 'none';
    var f = img.parentNode.querySelector('.avatar-fallback');
    if (f) {
      f.hidden = false;
      f.removeAttribute('hidden');
    }
  }

  /* ------------------------------------------------------------------ *
   * 轻提示
   * ------------------------------------------------------------------ */
  var toastTimer = null;

  function toast(msg) {
    if (!document || !document.body) return;
    var box = document.getElementById('kpl-toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'kpl-toast';
      box.className = 'toast';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    box.textContent = String(isNil(msg) ? '' : msg);
    // 强制重排，保证连续 toast 也能重新播放动画
    void box.offsetHeight;
    box.classList.add('toast-show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastTimer = null;
      box.classList.remove('toast-show');
    }, 2200);
  }

  /* ------------------------------------------------------------------ *
   * 本地教练档案（契约 §8）
   * ------------------------------------------------------------------ */
  function readProfile() {
    var raw = lsGet(PROFILE_KEY);
    var base = {};
    Object.keys(PROFILE_DEFAULT).forEach(function (k) { base[k] = PROFILE_DEFAULT[k]; });
    if (!raw) return base;
    try {
      var obj = JSON.parse(raw);
      if (!obj || typeof obj !== 'object') return base;
      Object.keys(base).forEach(function (k) {
        if (obj[k] !== undefined && obj[k] !== null) base[k] = obj[k];
      });
      if (!base.hero_usage || typeof base.hero_usage !== 'object') base.hero_usage = {};
      return base;
    } catch (e) {
      return base;
    }
  }

  var profile = {
    get: readProfile,
    save: function (patch) {
      var cur = readProfile();
      if (patch && typeof patch === 'object') {
        Object.keys(patch).forEach(function (k) {
          if (patch[k] !== undefined) cur[k] = patch[k];
        });
      }
      if (!cur.hero_usage || typeof cur.hero_usage !== 'object') cur.hero_usage = {};
      lsSet(PROFILE_KEY, JSON.stringify(cur));
      return cur;
    }
  };

  /* ------------------------------------------------------------------ *
   * 导出
   * ------------------------------------------------------------------ */
  var KPL = {
    version: '4.0.0',
    DASH: DASH,

    api: api,

    getLeagueId: getLeagueId,
    setLeagueId: setLeagueId,
    onLeague: onLeague,
    resolveLeagueId: resolveLeagueId,
    getLeagueName: getLeagueName,

    fmtPct: fmtPct,
    fmtNum: fmtNum,
    fmtInt: fmtInt,

    heroImg: heroImg,
    teamLogo: teamLogo,
    imgFallback: imgFallback,

    toast: toast,
    clearCache: function (prefix) {
      Object.keys(MEMO).forEach(function (k) {
        if (!prefix || k.indexOf(prefix) === 0) delete MEMO[k];
      });
    },
    profile: profile,

    util: {
      esc: esc,
      firstChar: firstChar,
      toNum: toNum,
      isNil: isNil,
      debounce: debounce,
      buildQuery: buildQuery,
      noop: noop
    }
  };

  global.KPL = KPL;
})(window);
