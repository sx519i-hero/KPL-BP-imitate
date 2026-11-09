/* ============================================================
   bp.js — KPL 对战页（BP 模拟）
   唯一状态源：后端返回的 match 对象；前端不推演 BP 步骤表。
   依赖（fe-core 提供，签名冻结）：
     KPL.api.bpNew / bpState / bpAction / bpAuto / bpNextGame / bpScore
     KPL.api.heroes(id)
     KPL.getLeagueId / KPL.toast / KPL.profile.get|save
     KPL.fmtPct / fmtNum / fmtInt / heroImg / teamLogo
   上述任一缺失时本文件内部都有降级实现，页面不崩。
   ============================================================ */
(function () {
  'use strict';

  var KPL = window.KPL || (window.KPL = {});

  /* ---------------- 常量 ---------------- */
  var LANES = ['全部', '对抗路', '打野', '中路', '发育路', '游走'];
  var AI_MAX_STEPS = 80;              // 防御性循环上限（20 步制远小于此值）
  var REC_TOP = 3;                    // 网格「推荐」角标数量
  var REC_SHOW = 3;                   // 评分浮层推荐条数
  var LANE_MAX_TAGS = 2;              // 网格每人最多显示 2 个分路标签（契约 v2 §4-1）
  var SIDE_CHOICE_PATH = '/api/bp/side_choice';
  var PHASE_SIDE_CHOICE = 'side_choice';   // 契约 v2 §3.2 新增阶段
  // v20：巅峰对决官方流程 —— 双方同时盲选 5 人（互不可见、可撞英雄），
  // 选完再按「对抗路 → 打野 → 中路 → 发育路 → 游走」逐路公布。
  var PHASE_TIEBREAK_PICK = 'tiebreak_pick';
  var PHASE_TIEBREAK_REVEAL = 'tiebreak_reveal';
  var TIEBREAK_PICKS = 5;
  var TIEBREAK_LANES = ['对抗路', '打野', '中路', '发育路', '游走'];
  var KNOWN_PHASES = { bp: 1, result: 1, side_choice: 1, done: 1, tiebreak_pick: 1, tiebreak_reveal: 1 };
  var PICK_SECONDS = 30;              // 契约 v3 §6：玩家回合倒计时，超时由 AI 代选
  var TIMER_STEP_MS = 200;            // 倒计时刷新间隔

  /* ---------------- 状态 ---------------- */
  var S = {
    match: null,
    leagueId: null,
    heroByEname: {},                   // 全量英雄字典（补 ban/pick 位头像）
    heroesReady: false,
    heroCache: {},                     // 按赛季分桶的英雄字典，换赛季不会串（契约 v2 §4-3）
    heroLeagueId: null,                // 当前字典属于哪个赛季
    lane: '全部',
    keyword: '',
    recSet: {},                        // 网格推荐角标 ename 集合
    aiBusy: false,
    autoBusy: false,                    // 一键 BP 请求进行中（v17）
    acting: false,
    sideBusy: false,                   // 选边请求进行中
    scoreOpen: false,
    resultKey: '',                     // 防止同一小局重复弹结算
    profileDone: {},                   // 已记账的小局
    opts: null,                        // 开局参数，用于「再来一局」
    started: false,
    pending: null,                     // 预选中的 ename（未确认，契约 v3 §6）
    timerId: null,                     // 倒计时 interval
    timerDeadline: 0,                  // 倒计时截止时间戳
    turnKey: '',                       // 当前回合标识，用于判断是否需要重置倒计时
    timerFired: false,                 // 本回合是否已触发超时代选
    /* --- v3 年总模式回写（URL 带 save_id + match_key 时生效） --- */
    career: null,                      // {saveId, matchKey, careerBlueTeam, reported}
    careerBusy: false
  };

  var el = {};

  /* ============================================================
     小工具
     ============================================================ */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function isFn(f) { return typeof f === 'function'; }

  /** null/undefined/NaN/'' → '—'，绝不把缺失显示成 0 */
  function dash(v) { return (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) ? '—' : v; }

  function fmtPct(v) {
    if (isFn(KPL.fmtPct)) { try { return KPL.fmtPct(v); } catch (e) { /* fallthrough */ } }
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) return '—';
    return Number(v).toFixed(1) + '%';
  }

  function fmtNum(v, digits) {
    if (isFn(KPL.fmtNum)) { try { return KPL.fmtNum(v); } catch (e) { /* fallthrough */ } }
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) return '—';
    return String(Number(v).toFixed(digits == null ? 1 : digits));
  }

  function fmtInt(v) {
    if (isFn(KPL.fmtInt)) { try { return KPL.fmtInt(v); } catch (e) { /* fallthrough */ } }
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) return '—';
    return String(Math.round(Number(v)));
  }

  function toast(msg) {
    if (isFn(KPL.toast)) { try { KPL.toast(msg); return; } catch (e) { /* fallthrough */ } }
    if (el.toast) return;              // 已在显示
    var d = document.createElement('div');
    d.textContent = msg;
    d.style.cssText = 'position:fixed;left:50%;bottom:calc(64px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);' +
      'background:rgba(46,42,38,.92);color:#fff;font-size:13px;line-height:1.4;padding:9px 14px;border-radius:10px;z-index:200;' +
      'max-width:80vw;text-align:center;box-shadow:0 6px 18px rgba(0,0,0,.18)';
    document.body.appendChild(d);
    el.toast = d;
    setTimeout(function () { if (d.parentNode) d.parentNode.removeChild(d); el.toast = null; }, 2200);
  }

  function errMsg(e) {
    if (!e) return '未知错误';
    if (typeof e === 'string') return e;
    return e.message || String(e);
  }

  /* ---------------- 后端调用统一入口 ---------------- */
  function raw(name, args) {
    var api = KPL.api || {};
    var f = api[name];
    if (!isFn(f)) return Promise.reject(new Error('接口不可用：KPL.api.' + name));
    try {
      return Promise.resolve(f.apply(api, args || []));
    } catch (e) {
      return Promise.reject(e);
    }
  }

  /** 解析响应：ok===false 或缺少 match 时抛错，交由调用方 toast */
  function call(name, args, requireMatch) {
    return raw(name, args).then(function (res) {
      if (!res || res.ok === false) {
        throw new Error((res && res.error) || (name + ' 调用失败'));
      }
      if (requireMatch && !res.match) throw new Error(name + ' 未返回对局数据');
      return res;
    });
  }

  function parseQuery() {
    var q = {};
    try {
      var s = window.location.search || '';
      if (s.charAt(0) === '?') s = s.slice(1);
      s.split('&').forEach(function (kv) {
        if (!kv) return;
        var i = kv.indexOf('=');
        var k = i < 0 ? kv : kv.slice(0, i);
        var v = i < 0 ? '' : kv.slice(i + 1);
        try { k = decodeURIComponent(k.replace(/\+/g, ' ')); v = decodeURIComponent(v.replace(/\+/g, ' ')); } catch (e) { /* keep raw */ }
        if (k) q[k] = v;
      });
    } catch (e) { /* 忽略 */ }
    return q;
  }

  function numOrNull(v, allowed) {
    if (v === null || v === undefined || v === '') return null;
    var n = parseInt(v, 10);
    if (isNaN(n)) return null;
    if (allowed && allowed.indexOf(n) < 0) return null;
    return n;
  }

  /* ============================================================
     DOM 缓存
     ============================================================ */
  function cacheDom() {
    var ids = ['seasonName', 'logoBlue', 'logoRed', 'nameBlue', 'nameRed', 'youBlue', 'youRed',
      'boLabel', 'boScore', 'boSub', 'tiebreakBanner', 'sideNote',
      'bpPanel', 'banBlue', 'pickBlue', 'banRed', 'pickRed', 'tbReveal',
      'bpTeamBlue', 'bpTeamRed', 'countBlue', 'countRed',
      'turnBand', 'turnText', 'stepBar', 'stepText',
      'laneChips', 'heroGrid', 'gridEmpty', 'dockCount', 'heroSearch', 'heroDock',
      'scoreFab', 'scoreMask', 'scoreSheet', 'scoreClose', 'scoreBody', 'autoBpFab',
      'resultMask', 'resultModal', 'resultTitle', 'resultSub', 'resultBody', 'btnAgain',
      'tbRevealMask', 'tbRevealModal', 'tbRevealStage', 'tbRevealLog', 'btnTbRevealNext',
      'btnBackHome', 'btnBackSeason',
      'sideChoiceMask', 'sideChoiceModal', 'sideChoiceKicker', 'sideChoiceTitle',
      'sideChoiceSub', 'sideChoiceHist', 'btnPickBlue', 'btnPickRed',
      'aiMask', 'aiText', 'fatalBox', 'arenaTop', 'linkReset',
      'confirmBar', 'cbAvatar', 'cbName', 'cbHint', 'cbTimer',
      'btnCancelPick', 'btnConfirmPick'];
    ids.forEach(function (id) { el[id] = document.getElementById(id); });
  }

  /* ============================================================
     头像 / 队徽
     ============================================================ */
  function fbBlock(hero, cls) {
    var t = (hero && (hero.cname || hero.hero)) ? String(hero.cname || hero.hero).charAt(0) : '?';
    return '<span class="' + (cls || 'slot-fb') + '">' + esc(t) + '</span>';
  }

  /** 英雄头像：优先 KPL.heroImg，其次 avatar_path，最后文字兜底 */
  function heroImgHTML(hero, fbCls) {
    if (!hero) return fbBlock(null, fbCls);
    if (hero.avatar_ok === false) return fbBlock(hero, fbCls);
    if (isFn(KPL.heroImg)) {
      try {
        var h = KPL.heroImg(hero, {});
        if (h) return h;
      } catch (e) { /* 降级 */ }
    }
    if (hero.avatar_path) {
      return '<img src="' + esc(hero.avatar_path) + '" alt="' + esc(hero.cname || '') +
        '" loading="lazy" decoding="async">';
    }
    return fbBlock(hero, fbCls);
  }

  /** 队徽：优先 KPL.teamLogo，其次 logo_path */
  function teamLogoHTML(team) {
    var t = team || {};
    if (t.logo_ok === false) return '<span class="logo-fb">' + esc((t.abbreviation || t.name || '?').slice(0, 3)) + '</span>';
    if (isFn(KPL.teamLogo)) {
      try {
        var h = KPL.teamLogo(t, {});
        if (h) return h;
      } catch (e) { /* 降级 */ }
    }
    if (t.logo_path) {
      return '<img src="' + esc(t.logo_path) + '" alt="' + esc(t.name || '') + '" loading="lazy" decoding="async">';
    }
    return '<span class="logo-fb">' + esc((t.abbreviation || t.name || '?').slice(0, 3)) + '</span>';
  }

  /** 图片加载失败 → 文字兜底（捕获阶段统一处理） */
  function installImgFallback() {
    document.addEventListener('error', function (ev) {
      var t = ev.target;
      if (!t || !t.tagName || t.tagName !== 'IMG') return;
      var box = t.parentNode;
      if (!box) return;
      if (!/slot-avatar|hc-avatar|rec-avatar|vs-logo|res-pick/.test(box.className || '')) return;
      var cls = /vs-logo/.test(box.className) ? 'logo-fb' : (/hc-avatar/.test(box.className) ? 'hc-fb' : 'slot-fb');
      var txt = (t.getAttribute('alt') || '?').charAt(0) || '?';
      var span = document.createElement('span');
      span.className = cls;
      span.textContent = txt;
      box.replaceChild(span, t);
    }, true);
  }

  /* ============================================================
     英雄字典
     ============================================================ */
  function heroOf(ename) {
    if (ename === null || ename === undefined) return null;
    var h = S.heroByEname[String(ename)];
    if (h) return h;
    // 用 history / ai_moves 里的英雄名兜底（无头像数据）
    var m = S.match;
    if (m) {
      var pools = [m.history || [], m.ai_moves || []];
      for (var i = 0; i < pools.length; i++) {
        for (var j = 0; j < pools[i].length; j++) {
          var it = pools[i][j];
          if (it && it.ename === ename && it.hero) return { ename: ename, cname: it.hero, avatar_ok: false };
        }
      }
    }
    return { ename: ename, cname: String(ename), avatar_ok: false };
  }

  /**
   * 拉取英雄字典。契约 v2 §4-3：`lane_list` 随赛季变化，
   * 所以按 league_id 分桶缓存，换赛季必须重新请求，避免串数据。
   */
  function loadHeroes(leagueId) {
    var key = (leagueId === null || leagueId === undefined || leagueId === '') ? '__default__' : String(leagueId);
    if (S.heroCache[key]) {
      S.heroByEname = S.heroCache[key];
      S.heroLeagueId = leagueId == null ? null : String(leagueId);
      S.heroesReady = true;
      return Promise.resolve(null);
    }
    return call('heroes', [leagueId], false).then(function (res) {
      var list = (res && res.heroes) || [];
      var map = {};
      list.forEach(function (h) { if (h && h.ename != null) map[String(h.ename)] = h; });
      // 仅在拿到有效数据时替换，避免空响应清空已有字典
      if (list.length) {
        S.heroCache[key] = map;
        // 只在该赛季仍是当前赛季时才设为活动字典（防止并发请求串赛季）
        var cur = S.match && S.match.league_id;
        if (cur == null || String(cur) === String(leagueId)) {
          S.heroByEname = map;
          S.heroLeagueId = leagueId == null ? null : String(leagueId);
        }
      }
      S.heroesReady = true;
      return list;
    }).catch(function () {
      S.heroesReady = true;                // 失败也放行，槽位退回文字兜底
      return [];
    });
  }

  /**
   * 契约 v2 §4-3：赛季变了必须重新拉 /api/heroes?league_id=
   * 返回 Promise，供 startMatch / resumeMatch / 选边后串行等待。
   */
  function ensureHeroesFor(leagueId) {
    var want = (leagueId === null || leagueId === undefined || leagueId === '') ? null : String(leagueId);
    if (want === S.heroLeagueId && S.heroesReady) return Promise.resolve(null);
    return loadHeroes(leagueId);
  }

  /* ============================================================
     渲染：顶部信息条
     ============================================================ */
  function renderTop() {
    var m = S.match;
    if (!m) return;
    var blue = m.blue_team || {};
    var red = m.red_team || {};

    el.seasonName.textContent = m.league_name || KPL.getLeagueName ? (m.league_name || '') : (m.league_name || '');
    if (!m.league_name) el.seasonName.textContent = '—';

    el.logoBlue.innerHTML = teamLogoHTML(blue);
    el.logoRed.innerHTML = teamLogoHTML(red);
    el.nameBlue.textContent = blue.name || '蓝方';
    el.nameRed.textContent = red.name || '红方';
    el.nameBlue.title = blue.name || '';
    el.nameRed.title = red.name || '';

    var ps = m.player_side;
    el.youBlue.hidden = ps !== 'blue';
    el.youRed.hidden = ps !== 'red';

    var gi = m.game_index;
    el.boLabel.textContent = 'BO' + dash(m.bo) + (gi ? ' · 第 ' + fmtInt(gi) + ' 局' : '');

    // v14：大比分必须按「战队」显示。m.score 是按当前阵营统计的，每局败方选边后
    // 同一支队伍的胜场会被拆到蓝/红两侧，看起来像「赢了却不算」。
    var tscore = boTeamScore(m);
    var hasTw = !!(m.team_wins && (tscore.mine || tscore.opp));
    if (hasTw) {
      el.boScore.textContent = fmtInt(tscore.mine) + ' : ' + fmtInt(tscore.opp);
    } else {
      var sc0 = m.score || {};
      var b0 = (sc0.blue != null) ? sc0.blue : null;
      var r0 = (sc0.red != null) ? sc0.red : null;
      el.boScore.textContent = (b0 === null && r0 === null) ? '— : —' : (fmtInt(b0) + ' : ' + fmtInt(r0));
    }

    var bits = [];
    if (hasTw) {
      bits.push('大比分看战队：我方 ' + tscore.mine + ' · 对手 ' + tscore.opp + ' · BO' + dash(m.bo) + ' 抢 ' + tscore.need + ' 胜');
    }
    if (m.mode === 'free') bits.push('自由 BP');
    else if (m.mode === 'ai') bits.push('人机对战');
    if (m.difficulty && m.mode === 'ai') {
      // 难度文案与首页 index.js:332 保持一致（入门 / 职业 / 魔王）
      bits.push({ rookie: '入门', pro: '职业', boss: '魔王' }[m.difficulty] || m.difficulty);
    }
    if (m.is_tiebreak) bits.push('决胜局');
    el.boSub.textContent = bits.join(' · ');

    // 决胜局顶部醒目提示条
    if (el.tiebreakBanner) el.tiebreakBanner.hidden = m.is_tiebreak !== true;

    renderSideNote(m);
  }

  /**
   * 契约 v2 §3.4-2：首局 / 单局不弹选边面板，只在顶部用小字说明。
   * `reason` 可能是枚举（first_game / single_game）也可能是后端直接给的中文说明，
   * 两种都要能显示；都没有就不显示。
   */
  function renderSideNote(m) {
    if (!el.sideNote) return;
    var sa = (m && m.side_assignment) || {};
    var sc = (m && m.side_choice) || {};
    var text = '';

    // 1) 明确是「需要选边」的场合不在顶部占用空间（有面板）
    if (sc.required === true && sc.resolved !== true) {
      el.sideNote.hidden = true;
      el.sideNote.textContent = '';
      return;
    }

    // 2) side_assignment.reason：枚举或中文说明
    text = reasonText(sa.reason);
    // 3) 退回 side_choice.reason / note
    if (!text) text = reasonText(sc.reason);
    if (!text && sc.note) text = String(sc.note);

    el.sideNote.hidden = !text;
    el.sideNote.textContent = text;
  }

  /** 把选边 reason 归一成展示文案（枚举映射 + 中文原样透传） */
  function reasonText(reason) {
    if (reason === null || reason === undefined || reason === '') return '';
    var r = String(reason).trim();
    if (!r) return '';
    var KNOWN = {
      first_game: '首局无历史对局，默认分配阵营',
      single_game: '单局模式（BO1），阵营由开局选择决定',
      loser_choice: '本局由上局败方选边',
      series_over: '系列赛已结束'
    };
    if (KNOWN[r]) return KNOWN[r];
    // 中文/自定义说明：直接展示（后端可能已经给成可读文案）
    return r;
  }

  /* ============================================================
     渲染：槽位
     ============================================================ */
  function banSlotHTML(side, idx, ename) {
    if (ename === null || ename === undefined) {
      return '<div class="slot empty-slot ban-empty" data-kind="ban" data-idx="' + idx + '">' +
        '<div class="empty-ph"><span class="ph-ico">&#10005;</span><span>Ban ' + (idx + 1) + '</span></div></div>';
    }
    var h = heroOf(ename);
    return '<div class="slot slot-ban' + (idx % 2 ? ' even' : '') + '" data-kind="ban" data-idx="' + idx + '" title="' + esc(h && h.cname) + '">' +
      '<span class="slot-no">' + (idx + 1) + '</span>' +
      '<span class="ban-x" aria-hidden="true">&#10005;</span>' +
      '<div class="slot-avatar">' + heroImgHTML(h, 'slot-fb') + '</div>' +
      '<span class="slot-name">' + esc(h ? dash(h.cname) : '—') + '</span>' +
      '</div>';
  }

  function pickSlotHTML(side, idx, ename) {
    if (ename === null || ename === undefined) {
      return '<div class="slot empty-slot pick-empty" data-kind="pick" data-idx="' + idx + '">' +
        '<div class="empty-ph"><span class="ph-ico">+</span><span>Pick ' + (idx + 1) + '</span></div></div>';
    }
    var h = heroOf(ename);
    var wr = h ? dash(h.win_rate) : null;
    return '<div class="slot slot-pick" data-kind="pick" data-idx="' + idx + '" title="' + esc(h && h.cname) + '">' +
      '<div class="slot-avatar">' + heroImgHTML(h, 'slot-fb') + '</div>' +
      '<span class="slot-name">' + esc(h ? dash(h.cname) : '—') + '</span>' +
      '<span class="slot-sub">' + (wr === null ? '—' : fmtPct(wr)) + '</span>' +
      '</div>';
  }

  function normArr(a) { return Array.isArray(a) ? a : []; }

  /** picks/bans 可能是 ename 数组，也可能被后端包成对象，统一取 ename */
  function enameOf(v) {
    if (v === null || v === undefined) return null;
    if (typeof v === 'number') return v;
    if (typeof v === 'string') { var n = parseInt(v, 10); return isNaN(n) ? v : n; }
    if (typeof v === 'object') return (v.ename != null ? v.ename : null);
    return null;
  }

  /** 把对局里的 hero_board 合并进英雄字典（补头像/名称兜底） */
  function absorbBoardHeroes(m) {
    var board = m && m.hero_board;
    if (!Array.isArray(board) || !board.length) return;
    for (var i = 0; i < board.length; i++) {
      var b = board[i];
      if (!b || b.ename == null) continue;
      var key = String(b.ename);
      if (S.heroByEname[key]) continue;
      S.heroByEname[key] = {
        ename: b.ename,
        cname: b.cname,
        title: b.title,
        avatar_path: b.avatar_path,
        avatar_ok: b.avatar_ok,
        lane_main: b.lane_main,
        lane_list: b.lane_list || (b.lane_main ? [b.lane_main] : []),
        win_rate: b.win_rate,
        ban_rate: b.ban_rate,
        pick_count: b.pick_count
      };
    }
  }

  function renderPanel() {
    var m = S.match;
    if (!m) return;

    // v14：对局里自带的全量 hero_board 就是最可靠的英雄字典。
    // 之前只靠 /api/heroes 填 S.heroByEname，一旦那次请求的赛季和当前对局不一致，
    // 结算弹窗、ban/pick 槽位就会没有头像（显示「?」）。
    absorbBoardHeroes(m);

    var bansB = normArr((m.bans || {}).blue).map(enameOf);
    var bansR = normArr((m.bans || {}).red).map(enameOf);
    var picksB = normArr((m.picks || {}).blue).map(enameOf);
    var picksR = normArr((m.picks || {}).red).map(enameOf);

    var tie = m.is_tiebreak === true;
    var reveal = tiebreakReveal(m);
    el.bpPanel.classList.toggle('no-ban', tie);
    el.bpPanel.classList.toggle('is-tiebreak', tie);

    // v20：盲选过程中对手已选必须不可见（否则「盲选」形同虚设）；
    // 只有自己那一方（自由 BP 下则是当前操作方）显示真实头像，对手只显示「已选 N/5」。
    var maskSide = tie && !reveal.length ? tiebreakMaskedSide(m) : '';

    el.banBlue.innerHTML = tie ? '' : renderSlots(bansB, 5, banSlotHTML, 'blue');
    el.banRed.innerHTML = tie ? '' : renderSlots(bansR, 5, banSlotHTML, 'red');
    el.pickBlue.innerHTML = (maskSide === 'blue')
      ? renderMaskSlots(picksB.length)
      : renderSlots(picksB, TIEBREAK_PICKS, tie ? tiebreakSlotHTML : pickSlotHTML, 'blue');
    el.pickRed.innerHTML = (maskSide === 'red')
      ? renderMaskSlots(picksR.length)
      : renderSlots(picksR, TIEBREAK_PICKS, tie ? tiebreakSlotHTML : pickSlotHTML, 'red');

    var bt = m.blue_team || {}, rt = m.red_team || {};
    el.bpTeamBlue.textContent = bt.name || '蓝方';
    el.bpTeamRed.textContent = rt.name || '红方';
    el.countBlue.textContent = tie ? (picksB.length + '/' + TIEBREAK_PICKS + ' · 盲选') : ('Ban ' + bansB.length + '/5 · Pick ' + picksB.length + '/5');
    el.countRed.textContent = tie ? (picksR.length + '/' + TIEBREAK_PICKS + ' · 盲选') : ('Ban ' + bansR.length + '/5 · Pick ' + picksR.length + '/5');

    // 当前动作方高亮对应面板
    var bSide = el.bpPanel.querySelector('.bp-side[data-side="blue"]');
    var rSide = el.bpPanel.querySelector('.bp-side[data-side="red"]');
    var live = !m.finished && (m.phase === 'bp' || m.phase === PHASE_TIEBREAK_PICK);
    if (bSide) bSide.classList.toggle('ai-think', live && m.side === 'red' && !isPlayerTurn());
    if (rSide) rSide.classList.toggle('ai-think', live && m.side === 'blue' && !isPlayerTurn());

    renderTiebreakReveal(m);
  }

  /* ============================================================
     v20：巅峰对决盲选 + 分路揭晓
     ------------------------------------------------------------
     官方流程：无 ban；双方同时盲选 5 人（互不可见、可以撞英雄）；
     选完按 对抗路 → 打野 → 中路 → 发育路 → 游走 逐路公布。
     ============================================================ */
  function tiebreakReveal(m) {
    var r = m && m.tiebreak_reveal;
    return Array.isArray(r) ? r : [];
  }

  /** 盲选过程中要遮住哪一方（空串 = 不遮） */
  function tiebreakMaskedSide(m) {
    if (!m) return '';
    var me = m.player_side;
    if (me !== 'blue' && me !== 'red') return '';   // 自由 BP：两边都是自己操作，不遮
    return me === 'blue' ? 'red' : 'blue';
  }

  /** 对手盲选槽：只显示「已选 N」占位，不泄露英雄 */
  function renderMaskSlots(count) {
    var out = '';
    for (var i = 0; i < TIEBREAK_PICKS; i++) {
      if (i < count) {
        out += '<div class="slot slot-mask" data-kind="pick" data-idx="' + i + '">' +
          '<div class="slot-avatar"><span class="mask-q">?</span></div>' +
          '<span class="slot-name">已选</span>' +
          '<span class="slot-sub">待公布</span></div>';
      } else {
        out += '<div class="slot empty-slot pick-empty" data-kind="pick" data-idx="' + i + '">' +
          '<div class="empty-ph"><span class="ph-ico">+</span><span>盲选 ' + (i + 1) + '</span></div></div>';
      }
    }
    return out;
  }

  function tiebreakSlotHTML(side, idx, ename) {
    if (ename === null || ename === undefined) {
      return '<div class="slot empty-slot pick-empty" data-kind="pick" data-idx="' + idx + '">' +
        '<div class="empty-ph"><span class="ph-ico">+</span><span>盲选 ' + (idx + 1) + '</span></div></div>';
    }
    var h = heroOf(ename);
    var wr = h ? dash(h.win_rate) : null;
    return '<div class="slot slot-pick" data-kind="pick" data-idx="' + idx + '" title="' + esc(h && h.cname) + '">' +
      '<div class="slot-avatar">' + heroImgHTML(h, 'slot-fb') + '</div>' +
      '<span class="slot-name">' + esc(h ? dash(h.cname) : '—') + '</span>' +
      '<span class="slot-sub">' + (wr === null ? '—' : fmtPct(wr)) + '</span>' +
      '</div>';
  }

  function revealCellHTML(cell) {
    var h = cell && cell.ename != null ? heroOf(cell.ename) : (cell || null);
    if (!h && cell && cell.cname) h = cell;
    if (!h) {
      return '<span class="tb-cell is-empty"><span class="tb-fb">—</span><span class="tb-name">—</span></span>';
    }
    return '<span class="tb-cell" title="' + esc(h.cname || '') + '">' +
      '<span class="tb-avatar">' + heroImgHTML(h, 'tb-fb') + '</span>' +
      '<span class="tb-name">' + esc(dash(h.cname)) + '</span>' +
      '</span>';
  }

  /** 按分路逐行公布双方阵容（对抗路 → 打野 → 中路 → 发育路 → 游走） */
  function renderTiebreakReveal(m) {
    var host = el.tbReveal;
    if (!host) return;
    var reveal = tiebreakReveal(m);
    if (!m || m.is_tiebreak !== true || !reveal.length) {
      host.hidden = true;
      host.innerHTML = '';
      return;
    }
    var rows = TIEBREAK_LANES.map(function (lane, i) {
      var row = null;
      for (var k = 0; k < reveal.length; k++) {
        if (reveal[k] && reveal[k].lane === lane) { row = reveal[k]; break; }
      }
      if (!row) row = reveal[i] || {};
      return '<div class="tb-row">' +
        '<span class="tb-lane">' + esc(row.lane || lane) + '</span>' +
        '<span class="tb-side is-blue">' + revealCellHTML(row.blue) + '</span>' +
        '<span class="tb-vs">vs</span>' +
        '<span class="tb-side is-red">' + revealCellHTML(row.red) + '</span>' +
        '</div>';
    }).join('');
    host.innerHTML = '<div class="tb-head"><strong>巅峰对决 · 分路公布</strong>' +
      '<span class="tb-head-tip">对抗路 → 打野 → 中路 → 发育路 → 游走</span></div>' + rows;
    host.hidden = false;
  }

  function renderSlots(arr, total, fn, side) {
    var out = '';
    for (var i = 0; i < total; i++) out += fn(side, i, arr[i] === undefined ? null : arr[i]);
    return out;
  }

  /* ============================================================
     渲染：状态带 / 进度
     ============================================================ */
  function isPlayerTurn() {
    var m = S.match;
    if (!m) return false;
    if (!m.player_side || m.mode === 'free') return true;   // 自由 BP 或后端未给边：双方可点
    // v20：巅峰对决盲选阶段双方同时可落子，只要我方还没选满 5 人就算「轮到我」
    if (m.phase === PHASE_TIEBREAK_PICK && m.is_tiebreak === true) {
      var mine = normArr(((m.picks || {})[m.player_side])).length;
      return mine < TIEBREAK_PICKS;
    }
    return m.side === m.player_side;
  }

  function renderTurn() {
    var m = S.match;
    if (!m) return;
    var band = el.turnBand;
    if (!band) return;

    var tiePick = m.phase === PHASE_TIEBREAK_PICK;
    var tieReveal = m.phase === PHASE_TIEBREAK_REVEAL || (m.is_tiebreak === true && tiebreakReveal(m).length > 0 && !m.finished);
    var live = (m.phase === 'bp' && !m.finished) || m.phase === 'side_choice' || (tiePick && !m.finished);
    band.classList.toggle('is-active', (m.phase === 'bp' || tiePick) && !m.finished);
    band.classList.toggle('is-finished', !live);
    band.classList.toggle('b-red', m.side === 'red');
    band.dataset.side = m.side || '';

    var label;
    if (m.phase === 'side_choice') {
      // 契约 v2 §3.4-2：选边阶段既不算进行中、也还没开始，文案要单独给，
      // 不能落到下面的「BP 已结束」分支（本局根本还没开始）。
      label = m.turn_label || '等待上局败方选边';
    } else if (tiePick) {
      label = m.turn_label || ('→ 巅峰对决盲选（' + normArr(((m.picks || {})[(m.player_side || 'blue')])).length + '/' + TIEBREAK_PICKS + '）');
    } else if (tieReveal) {
      label = '巅峰对决 · 公布阵容（对抗路 → 游走）';
    } else if (m.finished || m.phase !== 'bp') {
      label = 'BP 已结束';
    } else if (m.turn_label) {
      label = m.turn_label;
    } else {
      label = '→ ' + (m.side === 'red' ? '红方' : '蓝方') + ' ' + (m.action === 'ban' ? 'Ban' : 'Pick');
    }
    if (live && m.phase === 'bp' && !isPlayerTurn()) label += ' · AI';
    el.turnText.textContent = label;

    var total = m.total_steps || 0;
    var step = m.step || 0;
    if (tiePick) {
      // 盲选没有「第 N/M 步」的概念，按我方已选人数显示进度
      var myKey = m.player_side || 'blue';
      var mineN = normArr(((m.picks || {})[myKey])).length;
      el.stepBar.style.width = Math.min(100, (mineN / TIEBREAK_PICKS) * 100).toFixed(2) + '%';
      el.stepText.textContent = '已盲选 ' + fmtInt(mineN) + ' / ' + TIEBREAK_PICKS + ' · 选完按分路公布';
      return;
    }
    if (tieReveal && !m.finished) {
      el.stepBar.style.width = '100%';
      el.stepText.textContent = '双方各 5 人已锁定';
      return;
    }
    var pct = total > 0 ? Math.max(0, Math.min(100, (step / total) * 100)) : 0;
    if (m.finished && total > 0) pct = 100;
    el.stepBar.style.width = pct.toFixed(2) + '%';

    if (!total || m.phase === 'side_choice' || step <= 0) {
      // step=0 出现在选边阶段（本局尚未开始），此时算「剩 N 步」会得出 19 这种荒谬值。
      el.stepText.textContent = '—';
    } else {
      var remain = Math.max(0, total - step + (m.finished ? 0 : 1));
      el.stepText.textContent = '第 ' + fmtInt(step) + ' / ' + fmtInt(total) + ' 步 · 剩 ' + fmtInt(remain) + ' 步';
    }
  }

  /* ============================================================
     渲染：英雄网格
     ============================================================ */
  function availableList() {
    var m = S.match;
    return (m && Array.isArray(m.available)) ? m.available : [];
  }

  /**
   * v5 需求 6/7：**全量英雄面板**。
   * 后端 match.hero_board 返回全部英雄，每项带 lane_main / disabled / reason。
   * 老后端没有 hero_board 时回退到 available（保证不会白屏）。
   */
  function boardList() {
    var m = S.match;
    if (m && Array.isArray(m.hero_board) && m.hero_board.length) return m.hero_board;
    return availableList();
  }

  /** 主分路（后端给了就用，没给就退到 lane_list[0]） */
  function mainLane(h) {
    if (h && typeof h.lane_main === 'string' && h.lane_main) return h.lane_main;
    var lanes = heroLanes(h);
    return lanes.length ? lanes[0] : '';
  }

  function isLocked(h) {
    return !!(h && h.disabled === true);
  }

  /**
   * 推荐集合：仅取 has_stats 为 true 的英雄按 win_rate 降序。
   * 注意：只用于打「推荐」角标，不改动网格渲染顺序（保持后端排序）。
   */
  function computeRecSet() {
    S.recSet = {};
    var list = availableList().filter(function (h) { return h && h.has_stats === true && h.win_rate != null; });
    list.slice().sort(function (a, b) { return (Number(b.win_rate) || 0) - (Number(a.win_rate) || 0); })
      .slice(0, REC_TOP)
      .forEach(function (h) { S.recSet[String(h.ename)] = true; });
  }

  /**
   * 契约 v2 §2.1 / §4-2：分路以该赛季 `lane_list` 为唯一依据。
   * 严禁回退到 hero.lane 单值或 hero.role（§5 禁止项），取不到就当空数组。
   */
  function heroLanes(h) {
    if (!h) return [];
    var src = h;
    var list = src.lane_list;
    if (!Array.isArray(list)) {
      var full = h.ename != null ? S.heroByEname[String(h.ename)] : null;
      list = full && Array.isArray(full.lane_list) ? full.lane_list : [];
    }
    if (!Array.isArray(list)) return [];
    return list.filter(function (l) { return typeof l === 'string' && l; });
  }

  function heroCname(h) {
    if (!h) return '—';
    if (h.cname) return h.cname;
    var full = S.heroByEname[String(h.ename)];
    return (full && full.cname) || '—';
  }

  /**
   * v5 需求 6：**不再按「KPL 出场率 + 分路」排序**，改成严格按王者官方主分路分组：
   * 对抗路 → 打野 → 中路 → 发育路 → 游走。组内保持后端原始顺序（稳定、不跳动）。
   * 「全部」时把 5 组按顺序拼起来，并带分组标题。
   */
  function laneRank(h) {
    var i = LANES.indexOf(mainLane(h));
    return i < 0 ? LANES.length : i;
  }

  function filteredHeroes() {
    var list = boardList();
    var lane = S.lane;
    var kw = (S.keyword || '').trim().toLowerCase();
    var out = list.filter(function (h) {
      if (!h) return false;
      if (lane && lane !== '全部' && mainLane(h) !== lane) return false;
      if (kw) {
        var nm = String(heroCname(h)).toLowerCase();
        if (nm.indexOf(kw) < 0 && String(h.ename).indexOf(kw) < 0) return false;
      }
      return true;
    });
    // 稳定排序：先按主分路官方顺序，再按 ename，保证刷新后位置不乱跳
    var idx = {};
    for (var i = 0; i < out.length; i++) idx[String(out[i].ename)] = i;
    out.sort(function (a, b) {
      var d = laneRank(a) - laneRank(b);
      if (d) return d;
      return (Number(a.ename) || 0) - (Number(b.ename) || 0);
    });
    return out;
  }

  /** 契约 v2 §4-1：最多 2 个分路标签，超出显示 +N；无数据 → 灰字提示 */
  function heroLanesHTML(h) {
    var lanes = heroLanes(h);
    if (!lanes.length) return '<span class="hc-lanes"><span class="hc-lane-none">暂无该赛季分路数据</span></span>';
    var shown = lanes.slice(0, LANE_MAX_TAGS);
    var rest = lanes.length - shown.length;
    var buf = ['<span class="hc-lanes">'];
    shown.forEach(function (l) { buf.push('<span class="hc-lane">' + esc(l) + '</span>'); });
    if (rest > 0) buf.push('<span class="hc-lane more">+' + rest + '</span>');
    buf.push('</span>');
    return buf.join('');
  }

  function heroCellHTML(h) {
    var ename = h.ename;
    var nm = heroCname(h);
    var wr = (h.win_rate === undefined ? null : h.win_rate);
    var br = (h.ban_rate === undefined ? null : h.ban_rate);
    var rec = (S.recSet[String(ename)] && !isLocked(h)) ? '<span class="hc-rec">荐</span>' : '';
    var locked = isLocked(h);
    var disabled = locked || !canAct();
    var pending = !locked && S.pending != null && String(S.pending) === String(ename);
    var reason = locked ? String(h.reason || '当前不可选') : '';
    var tip = nm + '｜主分路 ' + (mainLane(h) || '暂无') +
      (wr != null ? '｜胜率 ' + fmtPct(wr) : '') +
      (reason ? '｜' + reason : '');
    return '<button type="button" class="hero-cell' + (disabled ? ' is-dim' : '') +
      (locked ? ' is-locked' : '') +
      (pending ? ' is-pending' : '') + '" data-ename="' + esc(ename) + '"' +
      (locked ? ' aria-disabled="true" tabindex="-1"' : '') +
      (pending ? ' aria-pressed="true"' : '') +
      (reason ? ' title="' + esc(reason) + '"' : '') +
      ' aria-label="' + esc(tip) + '">' +
      rec +
      (locked ? '<span class="hc-lock" aria-hidden="true">🚫</span>' : '') +
      '<span class="hc-avatar">' + heroImgHTML(h, 'hc-fb') + '</span>' +
      '<span class="hc-name">' + esc(nm) + '</span>' +
      heroLanesHTML(h) +
      '<span class="hc-badges">' +
        '<span class="hc-badge wr">胜 ' + fmtPct(wr) + '</span>' +
        '<span class="hc-badge br">禁 ' + fmtPct(br) + '</span>' +
      '</span>' +
      '</button>';
  }

  /* v30：英雄网格按「外观签名」增量渲染 —— 状态没变就不重建 133 个单元格（也就不会重建 <img>） */
  var GRID_SIG = null;
  var GRID_LIST_SIG = '';

  function gridSignature(list) {
    var parts = [S.lane || '', S.search || '', canAct() ? '1' : '0',
                 S.pending == null ? '' : String(S.pending), list.length];
    for (var i = 0; i < list.length; i++) {
      var h = list[i];
      if (!h) { parts.push('-'); continue; }
      var locked = isLocked(h) ? 1 : 0;
      parts.push(h.ename + ':' + locked + ':' +
        ((S.recSet[String(h.ename)] && !locked) ? 1 : 0));
    }
    return parts.join(',');
  }

  function renderGridCells(list) {
    if (!list.length) {
      el.heroGrid.innerHTML = '';
      el.gridEmpty.hidden = false;
      el.gridEmpty.textContent = availableList().length
        ? '没有符合筛选条件的英雄，换个位置或清空搜索试试。'
        : '当前没有可选英雄。';
      return;
    }
    el.gridEmpty.hidden = true;
    var buf = [];
    var groupTitle = (S.lane && S.lane !== '全部') ? S.lane : null;
    var lastLane = null;
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      if (!groupTitle) {
        var lane = mainLane(item) || '其他';
        if (lane !== lastLane) {
          lastLane = lane;
          buf.push('<div class="hc-group" role="presentation">' + esc(lane) +
            '<span class="hc-group-n">' +
            (item.lane_main_official === false ? '官方定位' : 'KPL 实战') + '</span></div>');
        }
      }
      buf.push(heroCellHTML(item));
    }
    el.heroGrid.innerHTML = buf.join('');
  }

  /* v38：只改状态（被 ban / 推荐 / 轮到谁）时不重建 133 个格子，
     直接就地改 class，桌面版（QtWebEngine）里能省掉 200~400ms 的重排。 */
  function gridListSignature(list) {
    var parts = [S.lane || '', S.search || '', list.length];
    for (var i = 0; i < list.length; i++) {
      parts.push(list[i] ? list[i].ename : '-');
    }
    return parts.join(',');
  }

  function patchGridCells(list) {
    var nodes = el.heroGrid.querySelectorAll('.hero-cell');
    if (nodes.length !== list.length) return false;
    var i, h;
    for (i = 0; i < list.length; i++) {
      h = list[i];
      if (!h || String(nodes[i].getAttribute('data-ename')) !== String(h.ename)) return false;
    }
    var act = canAct();
    for (i = 0; i < list.length; i++) {
      h = list[i];
      var n = nodes[i];
      var locked = isLocked(h);
      var disabled = locked || !act;
      var pending = !locked && S.pending != null && String(S.pending) === String(h.ename);
      var cls = 'hero-cell' + (disabled ? ' is-dim' : '') +
        (locked ? ' is-locked' : '') + (pending ? ' is-pending' : '');
      if (n.className !== cls) n.className = cls;
      if (locked) {
        if (!n.hasAttribute('aria-disabled')) {
          n.setAttribute('aria-disabled', 'true');
          n.setAttribute('tabindex', '-1');
        }
      } else if (n.hasAttribute('aria-disabled')) {
        n.removeAttribute('aria-disabled');
        n.removeAttribute('tabindex');
      }
      if (pending) {
        if (n.getAttribute('aria-pressed') !== 'true') n.setAttribute('aria-pressed', 'true');
      } else if (n.hasAttribute('aria-pressed')) {
        n.removeAttribute('aria-pressed');
      }
      var reason = locked ? String(h.reason || '当前不可选') : '';
      if (reason) {
        if (n.getAttribute('title') !== reason) n.setAttribute('title', reason);
      } else if (n.hasAttribute('title')) {
        n.removeAttribute('title');
      }
      var wantRec = !!(S.recSet[String(h.ename)] && !locked);
      var recNode = n.querySelector('.hc-rec');
      if (wantRec && !recNode) {
        var r = document.createElement('span');
        r.className = 'hc-rec';
        r.textContent = '荐';
        n.appendChild(r);
      } else if (!wantRec && recNode && recNode.parentNode) {
        recNode.parentNode.removeChild(recNode);
      }
      var lockNode = n.querySelector('.hc-lock');
      if (locked && !lockNode) {
        var l = document.createElement('span');
        l.className = 'hc-lock';
        l.setAttribute('aria-hidden', 'true');
        l.textContent = '🚫';
        n.appendChild(l);
      } else if (!locked && lockNode && lockNode.parentNode) {
        lockNode.parentNode.removeChild(lockNode);
      }
    }
    return true;
  }

  function renderGrid() {
    if (!el.heroGrid) return;
    var list = filteredHeroes();
    var sig = gridSignature(list);
    if (sig !== GRID_SIG) {
      var listSig = gridListSignature(list);
      if (listSig === GRID_LIST_SIG && patchGridCells(list)) {
        GRID_SIG = sig;
      } else {
        GRID_SIG = sig;
        GRID_LIST_SIG = listSig;
        renderGridCells(list);
      }
    }
    if (el.dockCount) {
      var legal = availableList().length;
      var total = boardList().length;
      el.dockCount.textContent = total
        ? (list.length + ' / ' + total + ' 展示 · ' + legal + ' 可选')
        : '—';
    }
    el.heroDock.classList.toggle('locked', !canAct());
  }

  function renderChips() {
    if (!el.laneChips) return;
    el.laneChips.innerHTML = LANES.map(function (l) {
      return '<button type="button" class="chip' + (S.lane === l ? ' active' : '') + '" data-lane="' + esc(l) +
        '" aria-pressed="' + (S.lane === l) + '">' + esc(l) + '</button>';
    }).join('');
  }

  function canAct() {
    var m = S.match;
    if (!m) return false;
    if (m.finished || m.phase === 'done' || m.phase === 'result') return false;
    if (m.phase === PHASE_SIDE_CHOICE) return false;      // 选边阶段禁止 BP 动作
    if (m.phase === PHASE_TIEBREAK_REVEAL) return false;  // 盲选已结束，只做揭晓，不再落子
    if (S.aiBusy || S.acting || S.sideBusy) return false;
    return isPlayerTurn();
  }

  /* ============================================================
     统一渲染入口
     ============================================================ */
  function renderAll() {
    if (!S.match) return;
    renderTop();
    renderPanel();
    renderTurn();
    computeRecSet();
    renderGrid();
  }

  /* ============================================================
     match 应用 / 结算
     ============================================================ */
  function applyMatch(match) {
    if (!match) return;
    S.match = match;
    S.fatalShown = false;
    if (el.fatalBox) el.fatalBox.hidden = true;
    rememberCareerBlue(match);

    // 未知 phase 兜底：非 bp/result/side_choice/done 也不要白屏（契约 v2 §3.4-7）
    var ph = match.phase;
    if (ph != null && !KNOWN_PHASES[ph]) {
      match.phase = 'bp';
    }

    syncUrl(match);
    renderAll();

    // 选边阶段：弹面板、盖住 BP 区；其余阶段确保面板关闭（自愈，防止卡在遮罩）
    if (wantSideChoice(match)) {
      if (chooserIsPlayer(match)) {
        showSideChoice(match);
      } else {
        // 契约 §3.3：chooser_role==ai 时后端应已自动选边；若仍停在此处，
        // 前端代为选边以防卡死（用户明确禁止项）
        showSideChoice(match);
        autoSideChoiceForAi(match);
      }
    } else if (match.phase === PHASE_SIDE_CHOICE) {
      // 处在选边阶段但 required/resolved 组合异常 → 自愈，避免 UI 卡住
      hideSideChoice();
      selfHealSideChoice(match);
    } else {
      hideSideChoice();
    }

    if (match.finished) maybeFinish();
    syncPendingTimer();
  }

  /** AI 替选：不阻塞用户，短暂提示后自动提交 */
  function autoSideChoiceForAi(m) {
    if (S.sideBusy) return;
    if (el.sideChoiceSub) {
      el.sideChoiceSub.textContent = (el.sideChoiceSub.textContent || '') + ' · AI 战队正在选边…';
    }
    setTimeout(function () {
      if (!S.match || S.match.match_id !== m.match_id) return;
      if (!wantSideChoice(S.match)) return;
      onSideChoice('blue');
    }, 900);
  }

  /** phase=side_choice 但无法判定需要玩家选边时，刷新一次状态自愈（只做一次） */
  function selfHealSideChoice(m) {
    if (S.sideHealKey === String(m.match_id)) return;
    S.sideHealKey = String(m.match_id);
    call('bpState', [m.match_id], false).then(function (res) {
      if (res && res.match) applyMatch(res.match);
    }).catch(function () { /* 保持当前画面，不白屏 */ });
  }

  /* ============================================================
     败方选边面板（契约 v2 §3.2 / §3.4）
     ============================================================ */
  /** 需要玩家手动选边：phase == side_choice 且 required == true 且还没 resolved */
  function wantSideChoice(m) {
    if (!m) return false;
    if (m.phase !== PHASE_SIDE_CHOICE) return false;
    var sc = m.side_choice || {};
    if (sc.required !== true) return false;
    if (sc.resolved === true) return false;
    return true;
  }

  /** 选边方是不是玩家自己（chooser_role: player|ai|human） */
  function chooserIsPlayer(m) {
    var sc = (m && m.side_choice) || {};
    var role = sc.chooser_role;
    if (role === 'player') return true;
    if (role === 'ai') return false;
    // 缺省时按 team 名对比玩家队伍
    var pt = (m && m.player_team) || {};
    if (sc.chooser_team && pt.name) return sc.chooser_team === pt.name;
    return true;
  }

  function sideHistHTML(m) {
    var games = Array.isArray(m.series_games) ? m.series_games : [];
    if (!games.length) return '';
    var rows = [];
    games.slice(-3).forEach(function (g) {
      var gi = g.game_index != null ? ('第 ' + fmtInt(g.game_index) + ' 局') : '上一局';
      var wt = g.winner_team || (g.winner_side === 'blue' ? ((m.blue_team || {}).name || '蓝方')
        : g.winner_side === 'red' ? ((m.red_team || {}).name || '红方') : '—');
      var lt = g.loser_team || '';
      var sc = (g.blue_score != null || g.red_score != null)
        ? (fmtInt(g.blue_score) + ' : ' + fmtInt(g.red_score)) : '';
      rows.push('<div class="side-hist-game">' +
        '<span class="gi">' + esc(gi) + '</span>' +
        '<span class="who w">' + esc(wt) + ' 胜</span>' +
        (lt ? '<span class="who">' + esc(lt) + ' 负</span>' : '') +
        (sc ? '<span class="sc">' + esc(sc) + '</span>' : '') +
        '</div>');
    });
    return '<div class="side-hist-rows">' + rows.join('') + '</div>';
  }

  function showSideChoice(m) {
    var sc = m.side_choice || {};
    var ss = m.score || {};
    var chooserTeam = sc.chooser_team || '';

    if (el.sideChoiceKicker) {
      el.sideChoiceKicker.textContent = sc.chooser_role === 'ai' ? 'AI 战队选边' : '上局败方选边';
    }
    // 标题必须逐字含「本局由 XX 战队（上局败方）选边」
    if (el.sideChoiceTitle) {
      el.sideChoiceTitle.textContent = '本局由 ' + (chooserTeam || '—') + ' 战队（上局败方）选边';
    }
    if (el.sideChoiceSub) {
      var bits = [];
      var tsc = boTeamScore(m);
      if (m.team_wins && (tsc.mine || tsc.opp)) {
        bits.push('大比分 我方 ' + tsc.mine + ' : ' + tsc.opp + ' 对手 · BO' + dash(m.bo) + ' 抢 ' + tsc.need + ' 胜');
      } else {
        bits.push('大比分 ' + fmtInt(ss.blue) + ' : ' + fmtInt(ss.red) + ' · BO' + dash(m.bo));
      }
      var last = (Array.isArray(m.series_games) && m.series_games.length)
        ? m.series_games[m.series_games.length - 1] : null;
      if (last) {
        var wt = last.winner_team || '—';
        var lt = last.loser_team || '—';
        bits.push('上一局 ' + wt + ' 胜 ' + lt);
      }
      el.sideChoiceSub.textContent = bits.join(' · ');
    }
    if (el.sideChoiceHist) {
      var hist = sideHistHTML(m);
      el.sideChoiceHist.innerHTML = hist;
      el.sideChoiceHist.hidden = !hist;
    }
    var busy = !!S.sideBusy;
    if (el.sideChoiceModal) el.sideChoiceModal.classList.toggle('is-busy', busy);
    if (el.btnPickBlue) el.btnPickBlue.disabled = busy;
    if (el.btnPickRed) el.btnPickRed.disabled = busy;
    if (el.sideChoiceMask) el.sideChoiceMask.hidden = false;
    // 盖住 BP 区时锁住网格
    setDockLocked(true);
  }

  function hideSideChoice() {
    if (el.sideChoiceMask) el.sideChoiceMask.hidden = true;
    if (el.sideChoiceModal) el.sideChoiceModal.classList.remove('is-busy');
    if (el.btnPickBlue) el.btnPickBlue.disabled = false;
    if (el.btnPickRed) el.btnPickRed.disabled = false;
  }

  /** 契约 v2 §3.3：POST /api/bp/side_choice {match_id, side} */
  function onSideChoice(side) {
    if (side !== 'blue' && side !== 'red') return;
    var m = S.match;
    if (!m || !wantSideChoice(m)) { hideSideChoice(); return; }
    if (S.sideBusy) return;

    S.sideBusy = true;
    if (el.btnPickBlue) el.btnPickBlue.disabled = true;
    if (el.btnPickRed) el.btnPickRed.disabled = true;
    if (el.sideChoiceModal) el.sideChoiceModal.classList.add('is-busy');

    // api.js 是 fe-core 的文件，不能改；直接用已有的 KPL.api.post
    var p;
    try {
      if (!KPL.api || !isFn(KPL.api.post)) throw new Error('缺少 KPL.api.post');
      p = KPL.api.post(SIDE_CHOICE_PATH, { match_id: m.match_id, side: side });
    } catch (e) {
      p = Promise.reject(e);
    }

    return Promise.resolve(p).then(function (res) {
      var next = (res && res.match) || (res && res.data && res.data.match);
      if (!next) throw new Error('选边未返回 match');
      S.sideBusy = false;
      hideSideChoice();
      applyMatch(next);                          // 全量重渲染：队名/logo/「你」徽章跟着换
      return ensureHeroesFor(next.league_id || S.leagueId).then(function () {
        // 选边后从第 1 步重新开始，照常进入 AI 轮次
        if (S.match) renderAll();
        return runAiTurn();
      }).then(function () {
        if (S.match) renderAll();
        setDockLocked(!canAct());
      });
    }).catch(function (e) {
      S.sideBusy = false;
      if (el.sideChoiceModal) el.sideChoiceModal.classList.remove('is-busy');
      // 自愈：重新拉一次对局状态，避免卡在遮罩上
      call('bpState', [m.match_id], false).then(function (res) {
        if (res && res.match) applyMatch(res.match);
      }).catch(function () {
        if (el.btnPickBlue) el.btnPickBlue.disabled = false;
        if (el.btnPickRed) el.btnPickRed.disabled = false;
      });
      toast('选边失败：' + errMsg(e));
    });
  }

  /** 把 match_id 写回地址栏，刷新后可原样接管（不新增历史记录） */
  function syncUrl(m) {
    if (!m || !m.match_id || !window.history || !isFn(window.history.replaceState)) return;
    try {
      var q = parseQuery();
      if (q.match_id === String(m.match_id)) return;
      q.match_id = String(m.match_id);
      var parts = [];
      Object.keys(q).forEach(function (k) {
        if (q[k] === null || q[k] === undefined || q[k] === '') return;
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(q[k]));
      });
      window.history.replaceState(null, '', window.location.pathname + (parts.length ? '?' + parts.join('&') : ''));
    } catch (e) { /* 忽略：地址栏同步失败不影响功能 */ }
  }

  function maybeFinish() {
    var m = S.match;
    if (!m || !m.finished) return;
    updateProfileOnce(m);
    showResult(m);
  }

  /* ---------------- 战绩记账 ---------------- */
  function updateProfileOnce(m) {
    var key = String(m.match_id) + '#' + String(m.game_index);
    if (S.profileDone[key]) return;
    S.profileDone[key] = true;

    var prof = null;
    try {
      if (KPL.profile && isFn(KPL.profile.get)) prof = KPL.profile.get();
    } catch (e) { prof = null; }
    if (!prof || typeof prof !== 'object') return;

    var f = m.final || {};
    var ps = m.player_side;
    var winner = winnerOf(m);

    prof.total_games = (Number(prof.total_games) || 0) + 1;

    // 玩家执边由 match.player_side 决定；与 final.winner 比对。
    // 契约要求「赢则 wins+1，否则 losses+1」，平局算作非胜（保证 wins+losses==total_games）。
    if (ps && (winner === 'blue' || winner === 'red' || winner === 'draw')) {
      if (winner === ps) prof.wins = (Number(prof.wins) || 0) + 1;
      else prof.losses = (Number(prof.losses) || 0) + 1;
    }

    // 玩家所选英雄使用次数
    if (!prof.hero_usage || typeof prof.hero_usage !== 'object') prof.hero_usage = {};
    var mine = [];
    if (ps && f[ps] && Array.isArray(f[ps].picks)) mine = f[ps].picks;
    else if (ps && m.picks && Array.isArray(m.picks[ps])) mine = m.picks[ps];
    mine.forEach(function (p) {
      var en = enameOf(p);
      if (en === null || en === undefined || en === '') return;
      var k = String(en);
      prof.hero_usage[k] = (Number(prof.hero_usage[k]) || 0) + 1;
    });

    try {
      if (KPL.profile && isFn(KPL.profile.save)) KPL.profile.save(prof);
    } catch (e) { /* 存档失败不影响对局 */ }
  }

  /* ============================================================
     结算弹窗
     ============================================================ */
  function boNeed(m) {
    var bo = Number(m.bo) || 1;
    return Math.floor(bo / 2) + 1;
  }

  // v14：系列赛是否结束必须以「战队胜场」判断。
  // 之前这里用 score.blue / score.red（按当前阵营统计），而每局打完败方选边会换边，
  // 同一支队伍的胜场被拆到蓝/红两侧，导致 BO5 要打满 5 局、BO7 要打满 7 局才结算。
  function boOver(m) {
    if (!m) return false;
    if (m.series_over === true) return true;
    var need = boNeed(m);
    var tw = m.team_wins || {};
    var best = 0;
    for (var key in tw) {
      if (!Object.prototype.hasOwnProperty.call(tw, key)) continue;
      var v = Number(tw[key]);
      if (!isNaN(v) && v > best) best = v;
    }
    if (best >= need) return true;
    var f = m.final || {};
    var ba = f.bo_after || null;
    if (ba) {
      var b = Number(ba.blue), r = Number(ba.red);
      if (!isNaN(b) && b >= need) return true;
      if (!isNaN(r) && r >= need) return true;
    }
    if (Number(m.bo) <= 1) return true;
    if (m.game_index != null && Number(m.game_index) >= Number(m.bo)) return true;
    return false;
  }

  function teamLabelOf(v) {
    if (!v) return '';
    if (typeof v === 'object') return String(v.name || '');
    return String(v);
  }

  // 大比分按「战队」统计（换边不影响），用于结算弹窗文案。
  function boTeamScore(m) {
    var tw = m.team_wins || {};
    /* v18：先按「我方战队名」定位（player_team 跟着队走，不随换边变），
       只有拿不到队名时才退回 player_side 推阵营。 */
    var ptName = (m.player_team && (m.player_team.name || m.player_team)) || '';
    ptName = ptName ? String(ptName) : '';
    var blueName = teamLabelOf(m.blue_team);
    var redName = teamLabelOf(m.red_team);
    var myName, oppName;
    if (ptName && ptName === blueName) { myName = blueName; oppName = redName; }
    else if (ptName && ptName === redName) { myName = redName; oppName = blueName; }
    else {
      var mySide = m.player_side === 'red' ? 'red' : 'blue';
      myName = teamLabelOf(mySide === 'blue' ? m.blue_team : m.red_team);
      oppName = teamLabelOf(mySide === 'blue' ? m.red_team : m.blue_team);
    }
    return {
      mine: Number(tw[myName]) || 0,
      opp: Number(tw[oppName]) || 0,
      myName: myName,
      oppName: oppName,
      need: boNeed(m)
    };
  }

  function picksOf(side) {
    var m = S.match;
    var f = m && m.final;
    if (f && f[side] && Array.isArray(f[side].picks)) return f[side].picks;
    if (m && m.picks && Array.isArray(m.picks[side])) return m.picks[side];
    return [];
  }

  function renderResultBody(m) {
    var f = m.final || {};
    var parts = [];

    // v49b：结算弹窗顺序按用户要求排 —— ①大比分 ②再来一局/返回首页 ③双方阵容 ④评分对比 ⑤小分详情
    parts.push(boAfterHTML(m));

    // 按钮占位：innerHTML 写完后再把 .modal-foot 整段挪进来（保留原有事件绑定）
    parts.push('<div class="res-actions" id="resActions"></div>');

    // 双方最终阵容（v50：按 对抗路 → 打野 → 中路 → 发育路 → 游走 排列，不再按 BP 手序）
    parts.push('<div class="res-lineups">');
    ['blue', 'red'].forEach(function (side) {
      var team = (side === 'blue' ? m.blue_team : m.red_team) || {};
      var picks = orderedPicks(m, side);
      var cells = picks.map(function (en) {
        var h = heroOf(en);
        return '<div class="res-pick">' +
          '<div class="hc-avatar">' + heroImgHTML(h, 'hc-fb') + '</div>' +
          '<span class="res-pick-name">' + esc(h ? dash(h.cname) : '—') + '</span>' +
          '</div>';
      }).join('');
      for (var i = picks.length; i < 5; i++) {
        cells += '<div class="res-pick"><div class="hc-avatar"><span class="hc-fb">—</span></div>' +
          '<span class="res-pick-name">—</span></div>';
      }
      parts.push('<div class="res-side">' +
        '<div class="res-side-head">' +
        '<span class="side-pill side-' + side + '">' + (side === 'blue' ? '蓝方' : '红方') + '</span>' +
        '<span>' + esc(team.name || '—') + '</span>' +
        '</div>' +
        '<div class="res-picks">' + cells + '</div>' +
        '</div>');
    });
    parts.push('</div>');

    // v20：巅峰对决 —— 按分路公布双方阵容（对抗路 → 打野 → 中路 → 发育路 → 游走）
    parts.push(tiebreakRevealHTML(m));

    // 评分对比
    var bs = (f.blue && f.blue.score != null) ? f.blue.score : null;
    var rs = (f.red && f.red.score != null) ? f.red.score : null;
    if (bs != null || rs != null) {
      parts.push('<div class="res-scores">' + scoreRowHTML('蓝方', bs, 'blue') + scoreRowHTML('红方', rs, 'red') + '</div>');
      // v49：小分详情 —— 这 80 分是怎么构成的、为什么，按分路逐条对比
      parts.push(scoreDetailHTML(f));
    }

    // v50：按钮区整段节点会被内层 innerHTML 清掉，所以先把它摘下来（节点与事件都留着），
    // 渲染完再插回去 —— 否则第 2、3 局的结算弹窗就再也没有按钮了。
    var foot = resultFoot();
    if (foot && foot.parentNode) foot.parentNode.removeChild(foot);

    el.resultBody.innerHTML = parts.join('');

    var slot = el.resultBody.querySelector('#resActions');
    if (foot && slot) slot.appendChild(foot);
  }

  /* v50：结算弹窗底部按钮区（缓存节点；每局结束都复用同一份，避免被 innerHTML 清掉） */
  var RESULT_FOOT = null;
  function resultFoot() {
    if (!RESULT_FOOT || !RESULT_FOOT.parentNode) {
      RESULT_FOOT = document.querySelector('#resultModal .modal-foot') || RESULT_FOOT;
    }
    return RESULT_FOOT;
  }

  /* v50：把双方 5 个 pick 按 对抗路 → 打野 → 中路 → 发育路 → 游走 排序 */
  var LANE_ORDER = ['对抗路', '打野', '中路', '发育路', '游走'];
  function orderedPicks(m, side) {
    var raw = picksOf(side).map(enameOf);
    var f = (m && m.final) || {};
    var detail = f[side] && f[side].detail;
    var out = [];
    var rows = (detail && detail.lanes) || [];
    LANE_ORDER.forEach(function (lane) {
      var row = null;
      for (var i = 0; i < rows.length; i++) {
        if (rows[i] && rows[i].lane === lane) { row = rows[i]; break; }
      }
      var en = row && row.mine ? row.mine.ename : null;
      if (en != null && out.indexOf(en) < 0) out.push(en);
    });
    raw.forEach(function (en) { if (out.indexOf(en) < 0) out.push(en); });
    // 完全没有分路信息时，退回按英雄自带的 lane_list[0] 排
    if (!rows.length) {
      out.sort(function (a, b) { return laneRank(a) - laneRank(b); });
    }
    return out;
  }
  function laneRank(en) {
    var h = heroOf(en) || {};
    var lane = (h.lane_list && h.lane_list[0]) || h.lane || '';
    var i = LANE_ORDER.indexOf(lane);
    return i < 0 ? LANE_ORDER.length : i;
  }

  /* 大比分：v14 起按战队统计，换边后不会出现「我方 3 胜却显示 2:1」的错乱 */
  function boAfterHTML(m) {
    var f = m.final || {};
    var tm = boTeamScore(m);
    var hasTeamWins = !!(m.team_wins && (tm.mine || tm.opp));
    if (hasTeamWins) {
      return '<div class="bo-after">大比分 <b>' + tm.mine + ' : ' + tm.opp + '</b>' +
        ' <span class="bo-hint">（我方 / 对手 · BO' + dash(m.bo) + ' 抢 ' + tm.need + ' 胜）</span>' +
        (boOver(m) ? ' · 本 BO 已结束' : '') + '</div>';
    }
    var ba = f.bo_after || m.score || {};
    if (ba.blue != null || ba.red != null) {
      return '<div class="bo-after">大比分 <b>' + fmtInt(ba.blue) + ' : ' + fmtInt(ba.red) + '</b>' +
        (boOver(m) ? ' · 本 BO 已结束' : (' · BO' + dash(m.bo))) + '</div>';
    }
    return '';
  }

  /** v20：结算弹窗里的「按分路公布」区块（非巅峰对决返回空串） */
  /* v50：巅峰对决第一个结算页 —— 上半逐路放大展示（蓝方 | 分路 | 红方），
     展示 1s 后收进下半的累计列表，五路走完后出现「下一步」。 */
  function findRevealRow(reveal, lane, index) {
    for (var i = 0; i < reveal.length; i++) {
      if (reveal[i] && reveal[i].lane === lane) return reveal[i];
    }
    return reveal[index] || {};
  }

  function tbStageHTML(row, lane) {
    return '<div class="tb-stage-inner">' +
      '<div class="tb-stage-side is-blue">' + revealCellHTML(row.blue) + '</div>' +
      '<div class="tb-stage-lane">' + esc(lane) + '</div>' +
      '<div class="tb-stage-side is-red">' + revealCellHTML(row.red) + '</div>' +
      '</div>';
  }

  function tbLogRowHTML(row, lane) {
    return '<div class="tb-row tb-log-row">' +
      '<span class="tb-lane">' + esc(lane) + '</span>' +
      '<span class="tb-side is-blue">' + revealCellHTML(row.blue) + '</span>' +
      '<span class="tb-vs">vs</span>' +
      '<span class="tb-side is-red">' + revealCellHTML(row.red) + '</span>' +
      '</div>';
  }

  function playTiebreakReveal(m, onDone) {
    var reveal = tiebreakReveal(m);
    var finished = false;
    var finish = function () {
      if (finished) return;
      finished = true;
      if (el.tbRevealMask) el.tbRevealMask.hidden = true;
      onDone();
    };
    if (!el.tbRevealMask || !reveal.length) { finish(); return; }
    if (el.tbRevealLog) el.tbRevealLog.innerHTML = '';
    if (el.tbRevealStage) el.tbRevealStage.innerHTML = '';
    if (el.btnTbRevealNext) {
      el.btnTbRevealNext.hidden = true;
      el.btnTbRevealNext.onclick = finish;
    }
    el.tbRevealMask.hidden = false;

    var i = 0;
    var step = function () {
      if (i >= TIEBREAK_LANES.length) {
        if (el.tbRevealStage) {
          el.tbRevealStage.innerHTML = '<div class="tb-stage-done">五路阵容公布完毕</div>';
        }
        if (el.btnTbRevealNext) el.btnTbRevealNext.hidden = false;
        return;
      }
      var lane = TIEBREAK_LANES[i];
      var row = findRevealRow(reveal, lane, i);
      if (el.tbRevealStage) el.tbRevealStage.innerHTML = tbStageHTML(row, lane);
      setTimeout(function () {
        if (el.tbRevealLog) el.tbRevealLog.insertAdjacentHTML('beforeend', tbLogRowHTML(row, lane));
        if (el.tbRevealStage) el.tbRevealStage.innerHTML = '';
        i += 1;
        step();
      }, 1000);
    };
    step();
  }

  function tiebreakRevealHTML(m) {
    var reveal = tiebreakReveal(m);
    if (!m || m.is_tiebreak !== true || !reveal.length) return '';
    var rows = TIEBREAK_LANES.map(function (lane, i) {
      var row = null;
      for (var k = 0; k < reveal.length; k++) {
        if (reveal[k] && reveal[k].lane === lane) { row = reveal[k]; break; }
      }
      if (!row) row = reveal[i] || {};
      return '<div class="tb-row">' +
        '<span class="tb-lane">' + esc(row.lane || lane) + '</span>' +
        '<span class="tb-side is-blue">' + revealCellHTML(row.blue) + '</span>' +
        '<span class="tb-vs">vs</span>' +
        '<span class="tb-side is-red">' + revealCellHTML(row.red) + '</span>' +
        '</div>';
    }).join('');
    return '<div class="res-tiebreak">' +
      '<div class="res-tiebreak-head"><strong>巅峰对决 · 分路公布</strong>' +
      '<span>对抗路 → 打野 → 中路 → 发育路 → 游走</span></div>' +
      rows + '</div>';
  }

  /* ============================================================
     v49：小分详情
     —— 五项构成（得分/上限/为什么）+ 按 对抗路→打野→中路→发育路→游走 对比双方
     ============================================================ */
  var RELATION_TEXT = {
    advantage: { txt: '蓝方压制', cls: 'is-blue' },
    disadvantage: { txt: '红方压制', cls: 'is-red' },
    even: { txt: '持平', cls: 'is-even' }
  };

  function detailHeroHTML(hero) {
    if (!hero || !hero.cname) {
      return '<span class="rd-hero is-none">—</span>';
    }
    var h = heroOf(hero.ename) || hero;
    var wr = (hero.win_rate === null || hero.win_rate === undefined)
      ? '<span class="rd-wr is-na">无赛季数据</span>'
      : '<span class="rd-wr">胜率 ' + fmtPct(hero.win_rate) + '</span>';
    return '<span class="rd-hero">' +
      '<span class="rd-ava">' + heroImgHTML(h, 'hc-fb') + '</span>' +
      '<span class="rd-name">' + esc(hero.cname) + '</span>' + wr + '</span>';
  }

  function compBarHTML(item, side) {
    if (!item) return '<span class="rd-comp-cell is-none">—</span>';
    if (!item.cap) return '<span class="rd-comp-cell is-none">不计入</span>';
    var pct = Math.max(0, Math.min(100, (Number(item.score) || 0) / item.cap * 100));
    return '<span class="rd-comp-cell">' +
      '<span class="rd-comp-num is-' + side + '">' + fmtInt(item.score) + '</span>' +
      '<span class="rd-comp-cap">/' + fmtInt(item.cap) + '</span>' +
      '<span class="rd-comp-bar"><i class="is-' + side + '" style="width:' + pct + '%"></i></span>' +
      '</span>';
  }

  function scoreDetailHTML(f) {
    var bd = (f && f.blue && f.blue.detail) || null;
    var rd = (f && f.red && f.red.detail) || null;
    if (!bd && !rd) return '';

    var order = ['system', 'counter', 'version', 'lane', 'weakness'];
    var bMap = {}, rMap = {};
    normArr((bd && bd.components) || []).forEach(function (c) { bMap[c.key] = c; });
    normArr((rd && rd.components) || []).forEach(function (c) { rMap[c.key] = c; });

    var compRows = order.map(function (key) {
      var b = bMap[key], r = rMap[key];
      var label = (b && b.label) || (r && r.label) || key;
      var reason = (b && b.reason) || (r && r.reason) || '';
      return '<div class="rd-comp-row">' +
        '<span class="rd-comp-k">' + esc(label) + '</span>' +
        compBarHTML(b, 'blue') + compBarHTML(r, 'red') +
        '<span class="rd-comp-reason">' + (reason ? '为什么：' + esc(reason) : '') + '</span>' +
        '</div>';
    }).join('');

    var base = bd || rd || {};
    var laneRows = normArr(base.lanes).map(function (row) {
      var bLane = bd ? findLane(bd, row.lane) : null;
      var rLane = rd ? findLane(rd, row.lane) : null;
      // 蓝方看自己那条路（mine），红方同理；对位关系取蓝方视角（advantage=蓝方压制）
      var rel = (bLane && bLane.relation) || null;
      var relInfo = RELATION_TEXT[rel] || { txt: '—', cls: 'is-even' };
      return '<div class="tb-row rd-lane-row">' +
        '<span class="tb-lane">' + esc(row.lane) + '</span>' +
        '<span class="tb-side is-blue">' + detailHeroHTML((bLane || row).mine) + '</span>' +
        '<span class="tb-vs rd-rel ' + relInfo.cls + '">' + esc(relInfo.txt) + '</span>' +
        '<span class="tb-side is-red">' + detailHeroHTML((rLane || row).mine) + '</span>' +
        '</div>';
    }).join('');

    return '<div class="res-detail">' +
      '<div class="res-detail-head"><strong>小分详情</strong>' +
      '<span>每一项得分 = 该赛季真实数据 × 评分细则权重（可在设置里调整）</span></div>' +
      '<div class="rd-comp">' +
      '<div class="rd-comp-row rd-comp-title"><span class="rd-comp-k">评分构成</span>' +
      '<span class="rd-comp-cell is-blue">蓝方得分 / 上限</span>' +
      '<span class="rd-comp-cell is-red">红方得分 / 上限</span></div>' +
      compRows + '</div>' +
      (laneRows ? '<div class="rd-lanes">' +
        '<div class="tb-row rd-comp-title"><span class="tb-lane">分路</span>' +
        '<span class="tb-side is-blue">蓝方</span><span class="tb-vs">对位</span>' +
        '<span class="tb-side is-red">红方</span></div>' + laneRows + '</div>' : '') +
      '<div class="rd-foot">对位关系来自王者荣耀官网「英雄关系」，其余数值来自该赛季 KPL 数据</div>' +
      '</div>';
  }

  function findLane(detail, lane) {
    var rows = normArr((detail && detail.lanes) || []);
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].lane === lane) return rows[i];
    }
    return null;
  }

  function scoreRowHTML(name, val, side) {    var null_ = (val === null || val === undefined);
    var v = null_ ? null : Math.max(0, Math.min(100, Number(val)));
    return '<div class="sc-row">' +
      '<div class="sc-head"><span class="sc-name">' + esc(name) + '</span>' +
      '<span class="sc-val ' + (null_ ? 'is-null' : (side === 'blue' ? 'is-blue' : 'is-red')) + '">' +
      (null_ ? '—' : fmtInt(v)) + '</span></div>' +
      '<div class="sc-bar"><i class="is-' + side + '" style="width:' + (null_ ? 0 : v) + '%"></i></div>' +
      '</div>';
  }

  /**
   * 胜方判定：优先 final.winner（"blue"|"red"|"draw"），
   * 缺失时回退 match.result（"blue_win"|"red_win"|"draw"）。
   * 绝不用 if(blue) else(red) 的写法——平局不能被当成红方胜。
   */
  function winnerOf(m) {
    var f = (m && m.final) || {};
    var w = f.winner;
    if (w === 'blue' || w === 'red' || w === 'draw') return w;
    var res = m && m.result;
    if (res === 'blue_win') return 'blue';
    if (res === 'red_win') return 'red';
    if (res === 'draw') return 'draw';
    return null;
  }

  /* ============================================================
     v3 年总模式回写（契约 v3 §4 /career/result）
     从生涯赛季页进来的对局，URL 带 save_id + match_key。
     系列赛结束（boOver）时把胜负写回存档，并把结算按钮换成「返回赛季」。
     ============================================================ */
  function initCareerFromQuery(q) {
    var saveId = q && q.save_id ? String(q.save_id) : '';
    var matchKey = q && q.match_key ? String(q.match_key) : '';
    if (!saveId || !matchKey) { S.career = null; return; }
    S.career = {
      saveId: saveId,
      matchKey: matchKey,
      careerBlueTeam: '',   // 首局蓝方 = 生涯对局的蓝方战队（换边后仍用它判定归属）
      reported: false
    };
  }

  function rememberCareerBlue(m) {
    if (!S.career || S.career.careerBlueTeam) return;
    var n = (m && m.blue_team && m.blue_team.name) || '';
    if (n) S.career.careerBlueTeam = String(n);
  }

  /** 返回生涯对局的 winner_side（blue / red），失败返回 null */
  function careerWinnerSide(m) {
    var w = winnerOf(m);
    if (w !== 'blue' && w !== 'red') return null;
    var winTeam = w === 'blue'
      ? ((m.blue_team || {}).name || '')
      : ((m.red_team || {}).name || '');
    var base = (S.career && S.career.careerBlueTeam) || '';
    if (!winTeam || !base) return w;      // 拿不到队名时退回本局阵营
    return (String(winTeam) === String(base)) ? 'blue' : 'red';
  }

  /** v18：胜方**战队名**（数据绑队名，不绑阵营） */
  function careerWinnerTeam(m) {
    var w = winnerOf(m);
    if (w !== 'blue' && w !== 'red') return '';
    return teamLabelOf(w === 'blue' ? m.blue_team : m.red_team);
  }

  function reportCareer(m) {
    if (!S.career || S.career.reported || S.careerBusy) return;
    if (!boOver(m)) return;
    var side = careerWinnerSide(m);
    var winTeam = careerWinnerTeam(m);
    if (!side && !winTeam) { toast('本局为平局，未写入生涯战绩'); S.career.reported = true; return; }

    S.careerBusy = true;
    /* score = BO 局分（m.score 是系列赛比分，不是阵容评分） */
    var score = null;
    try {
      if (m.score && m.score.blue !== undefined && m.score.red !== undefined) {
        score = { blue: Number(m.score.blue) || 0, red: Number(m.score.red) || 0 };
      }
    } catch (e) { score = null; }

    Promise.resolve(KPL.api && KPL.api.careerResult ? KPL.api.careerResult({
      save_id: S.career.saveId,
      match_key: S.career.matchKey,
      winner_side: side || null,
      winner_team: winTeam || null,
      score: score,
      bp_match_id: m.match_id
    }) : Promise.reject(new Error('生涯接口不可用'))).then(function (r) {
      S.career.reported = true;
      S.careerBusy = false;
      if (r && r.ok === false) throw new Error(r.error || '回写失败');
      toast('生涯战绩已记录');
    }).catch(function (e) {
      S.careerBusy = false;
      S.career.reported = true;   // 失败也不再重试，避免结算弹窗反复请求
      toast('生涯战绩回写失败：' + errMsg(e));
    });
  }

  function applyCareerButtons(m) {
    if (!S.career) return;
    var href = '/career/season?save_id=' + encodeURIComponent(S.career.saveId);
    var over = boOver(m);
    if (el.btnBackSeason) {
      el.btnBackSeason.setAttribute('href', href);
      el.btnBackSeason.hidden = !over;      // 只有系列赛结束才给「返回赛季」
    }
    if (el.btnBackHome) el.btnBackHome.hidden = over;
    if (el.btnAgain) el.btnAgain.hidden = over;   // 系列赛已结束，不能再重开
  }

  /* v50：巅峰对决先把「分路公布」当作第一个结算页，逐路 1s 展示，点「下一步」再看完整结算 */
  var TB_REVEAL_SEEN = '';

  function showResult(m) {
    if (!el.resultMask) return;
    var key = String(m && m.match_id) + '#' + String(m && m.game_index);
    if (m && m.is_tiebreak === true && tiebreakReveal(m).length && TB_REVEAL_SEEN !== key) {
      TB_REVEAL_SEEN = key;
      playTiebreakReveal(m, function () { showResultModal(m); });
      return;
    }
    showResultModal(m);
  }

  function showResultModal(m) {
    if (!el.resultMask) return;
    var winner = winnerOf(m);
    var ps = m.player_side;

    var title, cls;
    if (winner === 'blue') { title = '蓝方胜利'; cls = 'win'; }
    else if (winner === 'red') { title = '红方胜利'; cls = 'win'; }
    else if (winner === 'draw') { title = '平局'; cls = 'draw'; }
    else { title = '本局结束'; cls = 'draw'; }

    var isWin = ps && winner && winner !== 'draw' && winner === ps;
    if (ps && winner && winner !== 'draw') { cls = isWin ? 'win' : 'lose'; }

    el.resultTitle.textContent = title;
    el.resultTitle.className = 'result-title ' + cls;

    var bt = (m.blue_team || {}).name || '—';
    var rt = (m.red_team || {}).name || '—';
    var sub = bt + ' vs ' + rt;
    if (ps) sub += ' · 你执' + (ps === 'blue' ? '蓝方' : '红方') + (isWin ? '（获胜）' : (winner === 'draw' ? '' : '（落败）'));
    el.resultSub.textContent = sub;

    renderResultBody(m);

    if (el.btnAgain) el.btnAgain.textContent = boOver(m) ? '再来一局' : '进入下一局';
    el.resultMask.hidden = false;
    S.resultKey = String(m.match_id) + '#' + String(m.game_index);

    /* v3 生涯：系列赛结束时回写胜负 + 换按钮 */
    rememberCareerBlue(m);
    if (S.career) {
      applyCareerButtons(m);
      reportCareer(m);
    }
  }

  function hideResult() { if (el.resultMask) el.resultMask.hidden = true; }

  /* ============================================================
     评分浮层
     ============================================================ */
  /**
   * 一键 BP（v17）：把本小局剩余的 ban/pick 全部交给内置 AI 走完。
   * 成功后直接刷新面板 / 结算弹窗，不需要玩家再点确认。
   * v20：巅峰对决盲选阶段同样可用（AI 帮我方补满 5 人）。
   */
  function runAutoComplete() {
    var m = S.match;
    if (!m || !m.match_id) return;
    if (m.finished || (m.phase && m.phase !== 'bp' && m.phase !== PHASE_SIDE_CHOICE && m.phase !== PHASE_TIEBREAK_PICK)) {
      toast('本小局已经结束了');
      return;
    }
    if (S.autoBusy || S.acting || S.aiBusy) return;
    S.autoBusy = true;
    S.pendingEname = null;
    if (el.autoBpFab) el.autoBpFab.disabled = true;
    toast('一键 BP：剩余 BP 交给内置 AI…');
    call('bpAutoComplete', [m.match_id], true).then(function (res) {
      S.autoBusy = false;
      if (el.autoBpFab) el.autoBpFab.disabled = false;
      var next = (res && res.match) ? res.match : res;
      if (next && next.match_id) applyMatch(next);
      toast('本次 BP 已由 AI 完成，点「进入下一局」继续');
    }).catch(function (e) {
      S.autoBusy = false;
      if (el.autoBpFab) el.autoBpFab.disabled = false;
      toast('一键 BP 失败：' + errMsg(e));
    });
  }

  function openScoreSheet() {
    var m = S.match;
    if (!m) { toast('对局尚未开始'); return; }
    el.scoreMask.hidden = false;
    el.scoreSheet.hidden = false;
    S.scoreOpen = true;
    el.scoreBody.innerHTML = '<div class="empty" style="padding:24px 0;text-align:center">评分计算中…</div>';

    call('bpScore', [m.match_id], false).then(function (res) {
      if (S.scoreOpen) renderScoreSheet(res);
    }).catch(function (e) {
      if (S.scoreOpen) {
        el.scoreBody.innerHTML = '<div class="empty" style="padding:24px 0;text-align:center">评分获取失败：' +
          esc(errMsg(e)) + '</div>';
      }
      toast('阵容评分失败：' + errMsg(e));
    });
  }

  function closeScoreSheet() {
    S.scoreOpen = false;
    el.scoreMask.hidden = true;
    el.scoreSheet.hidden = true;
  }

  function deltaText(delta) {
    if (delta === null || delta === undefined || isNaN(Number(delta))) return null;
    var d = Number(delta);
    var ad = Math.abs(d);
    var lead = d > 0 ? '蓝方' : (d < 0 ? '红方' : null);
    if (ad <= 3) return { txt: '双方阵容势均力敌（差值 ' + fmtInt(ad) + '）', cls: 'is-even' };
    if (ad <= 10) return { txt: lead + '略占优势（差值 ' + fmtInt(ad) + '）', cls: '' };
    return { txt: lead + '优势明显（差值 ' + fmtInt(ad) + '）', cls: 'is-strong' };
  }

  /**
   * 分数条宽度：总分恒为 0–100，直接按 score 取百分比即可，不会溢出。
   * 注意 breakdown 子项**不是** 0–100 口径（实测 system 可达 40），
   * 若将来要绘制子项条，必须用 BD_CAPS 里的实际生效上限，不能一律除以 100。
   */
  var BD_CAPS = { system: 40, counter: 25, version: 27, lane: 20, weakness: 14 };
  var BD_LABELS = { system: '体系', counter: '克制', version: '版本', lane: '分路', weakness: '弱项' };

  /**
   * 分项 chip。上限优先用后端下发的 `caps`（权重转移后上限会变，见契约 v2 §2.3），
   * 取不到时才回退 BD_CAPS。`caps[k] === 0` 表示该分项当前无数据、权重已转移走，
   * 此时不渲染该 chip（渲染成 "克制 0/25" 是误导 —— 25 已经不在这一项了）。
   */
  function breakdownHTML(d) {
    var bd = d && d.breakdown;
    if (!bd || typeof bd !== 'object') return '';
    var caps = (d && d.caps && typeof d.caps === 'object') ? d.caps : BD_CAPS;
    var chips = [];
    Object.keys(BD_CAPS).forEach(function (k) {
      var v = bd[k];
      if (v === null || v === undefined || isNaN(Number(v))) return;
      var cap = caps[k];
      if (cap === null || cap === undefined || isNaN(Number(cap))) cap = BD_CAPS[k];
      cap = Number(cap);
      if (cap <= 0) return;                       // 该分项已无权重，不展示
      if (Number(v) > cap) v = cap;               // 防御：不出现 x > 上限
      chips.push('<span class="sc-bd"><i>' + BD_LABELS[k] + '</i>' + fmtNum(v, 0) +
        '<em>/' + cap + '</em></span>');
    });
    return chips.length ? '<div class="sc-bds">' + chips.join('') + '</div>' : '';
  }

  function sideBlock(res, side) {
    var d = res[side] || {};
    var name = side === 'blue' ? '蓝方' : '红方';
    var val = (d.score === undefined ? null : d.score);
    var notes = normArr(d.notes).filter(Boolean);
    var missing = normArr(d.missing).filter(Boolean);

    var tags = [];
    notes.forEach(function (t) { tags.push('<span class="sc-tag">' + esc(t) + '</span>'); });
    missing.forEach(function (t) { tags.push('<span class="sc-tag miss">缺 ' + esc(t) + '</span>'); });

    return '<div class="sc-row">' +
      '<div class="sc-head"><span class="sc-name">' + name + '</span>' +
      '<span class="sc-val ' + (val === null ? 'is-null' : (side === 'blue' ? 'is-blue' : 'is-red')) + '">' +
      (val === null ? '—' : fmtInt(val)) + '</span></div>' +
      '<div class="sc-bar"><i class="is-' + side + '" style="width:' + (val === null ? 0 : Math.max(0, Math.min(100, Number(val)))) + '%"></i></div>' +
      breakdownHTML(d) +
      (tags.length ? '<div class="sc-tags">' + tags.join('') + '</div>'
        : '<div class="sc-none">暂无定性评价</div>') +
      '</div>';
  }

  function renderScoreSheet(res) {
    var parts = [];
    parts.push(sideBlock(res, 'blue'));
    parts.push(sideBlock(res, 'red'));

    var dt = deltaText(res.delta);
    if (dt) parts.push('<div class="sc-delta ' + dt.cls + '">' + esc(dt.txt) + '</div>');

    var rec = res.recommend || {};
    var cands = normArr(rec.candidates).slice(0, REC_SHOW);
    if (cands.length) {
      var rs = rec.side === 'red' ? 'red' : 'blue';
      var actTxt = rec.action === 'ban' ? 'Ban' : 'Pick';
      var items = cands.map(function (c) {
        var h = heroOf(c.ename) || { ename: c.ename, cname: c.cname, avatar_ok: false };
        if (!h.cname && c.cname) h = { ename: c.ename, cname: c.cname, avatar_ok: false };
        var nm = c.cname || h.cname || '—';
        var reason = c.reason || '';
        return '<button type="button" class="rec-item" data-ename="' + esc(c.ename) + '"' +
          ' aria-label="选 ' + esc(nm) + '">' +
          '<span class="rec-avatar">' + heroImgHTML(h, 'slot-fb') + '</span>' +
          '<span class="rec-main">' +
          '<span class="rec-name">' + esc(nm) + '</span>' +
          '<span class="rec-reason">' + esc(reason || '—') + '</span>' +
          '</span>' +
          '<span class="rec-score">' + (c.score == null ? '—' : fmtNum(c.score, 1)) + '</span>' +
          '</button>';
      }).join('');

      parts.push('<div class="rec-head">推荐下一手' +
        '<span class="rec-side is-' + rs + '">' + (rs === 'red' ? '红方' : '蓝方') + ' ' + esc(actTxt) + '</span>' +
        '</div><div class="rec-list">' + items + '</div>');
    } else {
      parts.push('<div class="rec-head">推荐下一手</div>' +
        '<div class="sc-none">暂无推荐（BP 已结束或数据不足）</div>');
    }

    el.scoreBody.innerHTML = parts.join('');
  }

  /* ============================================================
     行动
     ============================================================ */
  /* ============================================================
     预选 + 确认（契约 v3 §6）
     ------------------------------------------------------------
     规则要点：
       * 点击英雄只是「预选高亮」，不锁定、不进槽位、不推进时序；
       * 必须点【确认选择】才真正提交 BP 动作；【取消预选】可清空重选；
       * 未确认前不发任何 BP 请求 → AI 不会自动行动、步骤不会跳；
       * 倒计时结束仍未确认 → 判本回合弃权，由 AI 代选（取引擎推荐分最高的英雄）。
     ============================================================ */
  function playerTurnKey(m) {
    if (!m) return '';
    return [m.match_id, m.game_index, m.step, m.side, m.action].join('|');
  }

  function heroOf(ename) {
    var key = String(ename);
    if (S.heroByEname && S.heroByEname[key]) return S.heroByEname[key];
    var list = availableList();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].ename) === key) return list[i];
    }
    return null;
  }

  function renderConfirmBar() {
    if (!el.confirmBar) return;
    var active = canAct();
    var ename = S.pending;
    var hero = ename == null ? null : heroOf(ename);

    el.confirmBar.hidden = !active;
    if (!active) return;

    var action = (S.match && S.match.action === 'ban') ? 'ban' : 'pick';
    var verb = action === 'ban' ? '禁用' : '选用';

    if (hero) {
      if (el.cbAvatar) el.cbAvatar.innerHTML = heroImgHTML(hero, 'cb-fb');
      if (el.cbName) el.cbName.textContent = heroCname(hero) + ' · ' + verb;
      if (el.cbHint) {
        el.cbHint.textContent = '预选已高亮，点【确认选择】才会锁定并进入下一步';
      }
    } else {
      if (el.cbAvatar) el.cbAvatar.innerHTML = '';
      if (el.cbName) el.cbName.textContent = '未选择英雄';
      if (el.cbHint) {
        el.cbHint.textContent = '点击下方头像预选（本回合' + (action === 'ban' ? '禁用' : '选择') + '）';
      }
    }
    if (el.btnConfirmPick) el.btnConfirmPick.disabled = !hero;
    if (el.btnCancelPick) el.btnCancelPick.disabled = !hero;
    renderTimerLabel();
  }

  function renderTimerLabel() {
    if (!el.cbTimer) return;
    if (!S.timerDeadline || !canAct()) {
      el.cbTimer.hidden = true;
      return;
    }
    var left = Math.max(0, Math.ceil((S.timerDeadline - Date.now()) / 1000));
    el.cbTimer.hidden = false;
    el.cbTimer.textContent = left + 's';
    el.cbTimer.classList.toggle('is-urgent', left <= 10);
  }

  function stopCountdown() {
    if (S.timerId) {
      clearInterval(S.timerId);
      S.timerId = null;
    }
    S.timerDeadline = 0;
    renderTimerLabel();
  }

  function startCountdown() {
    stopCountdown();
    if (!canAct()) return;
    S.turnKey = playerTurnKey(S.match);
    S.timerFired = false;
    S.timerDeadline = Date.now() + PICK_SECONDS * 1000;
    renderTimerLabel();
    S.timerId = setInterval(function () {
      if (!canAct()) { stopCountdown(); return; }
      renderTimerLabel();
      if (Date.now() >= S.timerDeadline) {
        stopCountdown();
        onTimeout();
      }
    }, TIMER_STEP_MS);
  }

  /** 超时弃权：由 AI 代选（取 available 里引擎推荐分最高的英雄） */
  function onTimeout() {
    if (S.timerFired) return;
    if (!canAct()) return;
    S.timerFired = true;
    var pick = bestAvailable();
    if (!pick) { toast('倒计时结束，但没有可代选的英雄'); return; }
    toast('倒计时结束，本回合判弃权，AI 代选：' + heroCname(pick));
    clearPending();
    doAction(pick.ename);
  }

  function bestAvailable() {
    var list = availableList();
    if (!list.length) return null;
    var best = list[0];
    var bestScore = scoreOf(best);
    for (var i = 1; i < list.length; i++) {
      var sc = scoreOf(list[i]);
      if (sc > bestScore) { best = list[i]; bestScore = sc; }
    }
    return best;
  }

  function scoreOf(h) {
    var v = h && h.top_score;
    if (v === undefined || v === null) return -1;
    var n = Number(v);
    return isFinite(n) ? n : -1;
  }

  function setPending(ename) {
    if (!canAct()) return;
    if (ename == null) return;
    var ok = availableList().some(function (h) { return String(h.ename) === String(ename); });
    if (!ok) { toast('该英雄当前不可选'); return; }
    if (S.pending != null && String(S.pending) === String(ename)) return;
    S.pending = ename;
    renderConfirmBar();
    renderGrid();
  }

  function clearPending() {
    if (S.pending == null) return;
    S.pending = null;
    renderConfirmBar();
    renderGrid();
  }

  function confirmPending() {
    if (S.pending == null) { toast('请先预选一个英雄'); return; }
    if (!canAct()) { toast('当前不能行动'); return; }
    var ename = S.pending;
    clearPending();
    doAction(ename);
  }

  /** 每次 match 变化后同步：清掉失效预选、按回合重置倒计时 */
  function syncPendingTimer() {
    var m = S.match;
    if (!m || !canAct()) {
      if (S.pending != null) { S.pending = null; }
      stopCountdown();
      renderConfirmBar();
      return;
    }
    // 预选可能因对手 ban/pick 而失效（理论上不会，防御）
    if (S.pending != null) {
      var still = availableList().some(function (h) {
        return String(h.ename) === String(S.pending);
      });
      if (!still) S.pending = null;
    }
    var key = playerTurnKey(m);
    if (key !== S.turnKey || !S.timerId) {
      startCountdown();
    }
    renderConfirmBar();
  }

  function doAction(ename) {
    var m = S.match;
    if (!m) return;
    if (S.acting || S.aiBusy) { toast('正在处理上一步，请稍候'); return; }
    var tiePick = (m.phase === PHASE_TIEBREAK_PICK) || (m.is_tiebreak === true && m.phase === 'bp');
    if (m.finished || (m.phase !== 'bp' && !tiePick)) { toast('本局 BP 已结束'); return; }
    if (!isPlayerTurn()) { toast('当前是对方回合'); return; }

    var action = tiePick ? 'pick' : m.action;
    if (action !== 'ban' && action !== 'pick') action = 'pick';

    // 前端预校验：只能点 available 里的英雄（后端仍会兜底校验）
    var ok = availableList().some(function (h) { return String(h.ename) === String(ename); });
    if (!ok) { toast('该英雄当前不可选'); return; }

    S.acting = true;
    setDockLocked(true);
    flashCell(ename);

    // v20：盲选阶段双方同时可落子，必须显式带上本方阵营
    var side = tiePick ? (m.player_side || (m.mode === 'free' ? m.side : null)) : null;
    call('bpAction', [m.match_id, ename, action, side], true).then(function (res) {
      applyMatch(res.match);
    }).catch(function (e) {
      toast('行动失败：' + errMsg(e));
    }).then(function () {
      S.acting = false;
      return runAiTurn();
    }).then(function () {
      setDockLocked(!canAct());
      renderGrid();
      // 回合推进后重置倒计时（新回合重新计时）
      S.turnKey = '';
      syncPendingTimer();
    });
  }

  function flashCell(ename) {
    if (!el.heroGrid) return;
    var cell = el.heroGrid.querySelector('.hero-cell[data-ename="' + String(ename).replace(/"/g, '') + '"]');
    if (!cell) return;
    cell.classList.remove('flash');
    void cell.offsetWidth;
    cell.classList.add('flash');
  }

  function setDockLocked(locked) {
    if (el.heroDock) el.heroDock.classList.toggle('locked', !!locked);
  }

  function showAi(on, text) {
    S.aiBusy = !!on;
    if (el.aiMask) el.aiMask.hidden = !on;
    if (on && el.aiText && text) el.aiText.textContent = text;
    if (on) setDockLocked(true);
  }

  /**
   * AI 回合：反复调用 bpAuto 直到回到玩家回合 / 对局结束。
   * 后端可能「一次走完所有手」，也可能「一次一手」，此循环对两种情况都成立。
   */
  function runAiTurn() {
    var m0 = S.match;
    if (!m0) return Promise.resolve();
    if (m0.mode === 'free') return Promise.resolve();
    if (m0.finished || m0.phase !== 'bp') return Promise.resolve();
    if (isPlayerTurn()) return Promise.resolve();
    if (S.aiBusy) return Promise.resolve();

    showAi(true, 'AI 教练思考中…');
    renderPanel();
    renderTurn();

    var guard = 0;
    var loop = function () {
      var m = S.match;
      if (!m || m.finished || m.phase !== 'bp') return Promise.resolve();
      if (isPlayerTurn()) return Promise.resolve();
      if (guard++ > AI_MAX_STEPS) return Promise.resolve();
      return call('bpAuto', [m.match_id], true).then(function (res) {
        applyMatch(res.match);
        return loop();
      });
    };

    return loop().catch(function (e) {
      toast('AI 行动失败：' + errMsg(e));
    }).then(function () {
      showAi(false);
      renderAll();
      setDockLocked(!canAct());
    });
  }

  /* ============================================================
     开局 / 再来一局
     ============================================================ */
  function buildOpts(q) {
    var leagueId = q.league_id || null;
    if (!leagueId && isFn(KPL.getLeagueId)) {
      try { leagueId = KPL.getLeagueId(); } catch (e) { leagueId = null; }
    }
    var opts = {
      mode: (q.mode === 'free' || q.mode === 'ai') ? q.mode : 'ai',
      side: (q.side === 'red' || q.side === 'blue') ? q.side : 'blue',
      bo: numOrNull(q.bo, [1, 3, 5, 7, 9]) || 7,
      difficulty: (['rookie', 'pro', 'boss'].indexOf(q.difficulty) >= 0) ? q.difficulty : 'pro'
    };
    if (leagueId) opts.league_id = String(leagueId);
    if (q.blue) opts.blue_team = q.blue;
    if (q.red) opts.red_team = q.red;
    return opts;
  }

  function startMatch(opts) {
    S.started = true;
    showFatal(false);
    S.match = null;
    return call('bpNew', [opts], true).then(function (res) {
      var m = res.match;
      if (!opts.league_id && m.league_id) opts.league_id = m.league_id;
      S.opts = opts;
      S.leagueId = m.league_id || S.leagueId;
      // 先渲染一版（英雄字典可能未就绪，槽位用文字兜底），随后补全
      applyMatch(m);
      return ensureHeroesFor(S.leagueId).then(function () {
        if (S.match) renderAll();
        return runAiTurn();
      }).then(function () {
        if (S.match) renderAll();
        setDockLocked(!canAct());
      });
    }).catch(function (e) {
      showFatal(true, '开局失败：' + errMsg(e));
      toast('开局失败：' + errMsg(e));
    });
  }

  function showFatal(on, msg) {
    if (!el.fatalBox) return;
    el.fatalBox.hidden = !on;
    if (on) {
      el.fatalBox.innerHTML = '<div style="font-size:13px;color:#A8483A;font-weight:600">' + esc(msg || '加载失败') + '</div>' +
        '<button class="btn btn-primary btn-sm" type="button" id="btnRetry">重试</button>';
      var b = document.getElementById('btnRetry');
      if (b) b.addEventListener('click', function () { retry(); });
    }
  }

  function retry() {
    var q = parseQuery();
    var opts = S.opts || buildOpts(q);
    startMatch(opts);
  }

  function onAgain() {
    var m = S.match;
    if (!m) { retry(); return; }
    if (el.btnAgain) el.btnAgain.disabled = true;

    var done = function () { if (el.btnAgain) el.btnAgain.disabled = false; };

    if (m.finished && !boOver(m)) {
      // 未打完 BO：进入下一小局
      call('bpNextGame', [m.match_id], true).then(function (res) {
        hideResult();
        var next = res.match;
        applyMatch(next);                            // phase==side_choice 时这里会弹选边面板
        if (wantSideChoice(next)) return null;       // 等用户选边，别自动跑 AI
        return runAiTurn();
      }).then(function () {
        if (S.match) renderAll();
        setDockLocked(!canAct());
      }).catch(function (e) {
        toast('进入下一局失败：' + errMsg(e));
      }).then(done);
    } else {
      // 整个 BO 已结束：开新 BO（沿用原参数，team/bo/side 不变）
      hideResult();
      var opts = S.opts || buildOpts(parseQuery());
      startMatch(opts).then(done, done);
    }
  }

  /* ============================================================
     事件绑定
     ============================================================ */
  function bindEvents() {
    // 英雄网格：事件委托，避免上百个独立监听
    if (el.heroGrid) {
      el.heroGrid.addEventListener('click', function (ev) {
        var cell = ev.target && ev.target.closest ? ev.target.closest('.hero-cell') : null;
        if (!cell) return;
        var ename = cell.getAttribute('data-ename');
        var m = S.match;
        if (!m) return;
        // v5 需求 7：全局 BP 受限英雄置灰且禁止点击
        if (cell.classList.contains('is-locked') || cell.getAttribute('aria-disabled') === 'true') {
          toast(cell.getAttribute('title') || '该英雄当前不可选');
          return;
        }
        if (S.aiBusy) { toast('AI 教练思考中，请稍候'); return; }
        if (S.acting) { toast('正在处理上一步'); return; }
        if (S.sideBusy) { toast('正在提交选边…'); return; }
        // 选边阶段：遮罩盖住网格，即使漏点也不发请求（契约 v2 §3.4）
        if (m.phase === PHASE_SIDE_CHOICE) { toast('请先选择本局阵营'); return; }
        // v20：巅峰对决盲选阶段允许继续点选
        if (m.finished || (m.phase !== 'bp' && m.phase !== PHASE_TIEBREAK_PICK)) { toast('本局 BP 已结束'); return; }
        if (!isPlayerTurn()) { toast('当前是对方回合，请等待 AI 出手'); return; }
        var n = parseInt(ename, 10);
        setPending(isNaN(n) ? ename : n);
      });
    }

    // 位置筛选
    if (el.laneChips) {
      el.laneChips.addEventListener('click', function (ev) {
        var chip = ev.target && ev.target.closest ? ev.target.closest('.chip') : null;
        if (!chip) return;
        var lane = chip.getAttribute('data-lane');
        if (!lane || lane === S.lane) return;
        S.lane = lane;
        renderChips();
        renderGrid();
      });
    }

    // 搜索（防抖，避免每次按键全量重绘）
    if (el.heroSearch) {
      var timer = null;
      el.heroSearch.addEventListener('input', function () {
        var v = el.heroSearch.value || '';
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          S.keyword = v;
          renderGrid();
        }, 120);
      });
    }

    // 一键 BP（v17）：剩余 ban/pick 全交给内置 AI，直接结束本小局
    if (el.autoBpFab) el.autoBpFab.addEventListener('click', function () {
      runAutoComplete();
    });

    // 评分浮层
    if (el.scoreFab) el.scoreFab.addEventListener('click', openScoreSheet);
    if (el.scoreClose) el.scoreClose.addEventListener('click', closeScoreSheet);
    if (el.scoreMask) el.scoreMask.addEventListener('click', closeScoreSheet);
    if (el.scoreBody) {
      el.scoreBody.addEventListener('click', function (ev) {
        var it = ev.target && ev.target.closest ? ev.target.closest('.rec-item') : null;
        if (!it) return;
        var ename = parseInt(it.getAttribute('data-ename'), 10);
        if (isNaN(ename)) return;
        var m = S.match;
        if (!m) return;
        if (m.finished || (m.phase !== 'bp' && m.phase !== PHASE_TIEBREAK_PICK)) { toast('本局 BP 已结束'); return; }
        if (!isPlayerTurn()) { toast('当前是对方回合，无法选中'); return; }
        closeScoreSheet();
        setPending(ename);
      });
    }

    // 预选确认条（契约 v3 §6）
    if (el.btnConfirmPick) el.btnConfirmPick.addEventListener('click', confirmPending);
    if (el.btnCancelPick) el.btnCancelPick.addEventListener('click', function () {
      if (S.pending == null) return;
      clearPending();
      toast('已取消预选');
    });

    // 结算按钮
    if (el.btnAgain) el.btnAgain.addEventListener('click', onAgain);

    // 选边按钮（契约 v2 §3.4-1）
    if (el.btnPickBlue) el.btnPickBlue.addEventListener('click', function () { onSideChoice('blue'); });
    if (el.btnPickRed) el.btnPickRed.addEventListener('click', function () { onSideChoice('red'); });
    // 选边遮罩不响应点击穿透关闭（必须选边才能继续）
    if (el.sideChoiceMask) {
      el.sideChoiceMask.addEventListener('click', function (ev) {
        if (ev.target === el.sideChoiceMask) toast('请选择本局阵营后继续');
      });
    }

    // ESC 关闭浮层
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' || ev.key === 'Esc') {
        if (S.scoreOpen) closeScoreSheet();
      }
    });
  }

  /* ============================================================
     启动
     ============================================================ */
  function boot() {
    cacheDom();
    if (!document.getElementById('arenaPage')) return;   // 非对战页
    installImgFallback();
    renderChips();
    bindEvents();

    var q = parseQuery();
    initCareerFromQuery(q);
    var opts = buildOpts(q);
    S.leagueId = opts.league_id || null;

    // URL 带 match_id 时接管既有对局（刷新 / 分享链接不丢进度）
    if (q.match_id) {
      resumeMatch(q.match_id);
      return;
    }
    startMatch(opts);
  }

  /** 按 match_id 接管既有对局 */
  function resumeMatch(matchId) {
    S.started = true;
    showFatal(false);
    S.match = null;
    return call('bpState', [matchId], true).then(function (res) {
      var m = res.match;
      S.opts = buildOpts(parseQuery());
      S.leagueId = m.league_id || S.leagueId;
      applyMatch(m);
      return ensureHeroesFor(S.leagueId).then(function () {
        if (S.match) renderAll();
        return runAiTurn();
      }).then(function () {
        if (S.match) renderAll();
        setDockLocked(!canAct());
      });
    }).catch(function (e) {
      // 接管失败就退回按 URL 参数重新开局，页面不能死
      toast('未能接管对局：' + errMsg(e) + '，已重新开局');
      return startMatch(S.opts || buildOpts(parseQuery()));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 供调试 / 后端联调时手动刷新
  window.KPL_ARENA = {
    refresh: function () {
      var m = S.match;
      if (!m) return Promise.resolve();
      return call('bpState', [m.match_id], true).then(function (res) {
        applyMatch(res.match);
        return runAiTurn();
      }).catch(function (e) { toast('刷新失败：' + errMsg(e)); });
    },
    state: function () { return S; }
  };
})();
