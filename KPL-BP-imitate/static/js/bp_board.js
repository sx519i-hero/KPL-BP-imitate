/* ============================================================================
 * bp_board.js —— 共享 BP 面板渲染器（v9 需求 1）
 *
 * 目标：好友 PK（联机）与 KPL 年总模式（/arena）使用**同一套 BP 面板**：
 *   · 同样的布局与 CSS 类（/static/css/arena.css），不再各写一套 v4 面板；
 *   · 真实英雄头像（avatar_path / 官方 CDN 拼接兜底）、Ban/Pick 卡槽带头像；
 *   · 顶部展示 BO 系列大比分 + 当前小局比分 / 局号；
 *   · BP 面板下方展示「全局 BP 状态」：双方本系列赛已选用英雄 + 本局已用/已 ban；
 *   · 全局 BP 置灰、悬浮提示 reason、巅峰对决（无 Ban / 双方盲选）规则完全同源，
 *     数据都来自后端 match.hero_board 的 disabled / reason。
 *
 * 用法：
 *   var inst = KPLBpBoard.mount(document.getElementById('bpHost'), {
 *     label: 'friend',                      // 日志前缀
 *     onSubmit: function (ename) { ... }    // 玩家点【确认选择】时回调
 *   });
 *   KPLBpBoard.setHeroes(list);             // /api/heroes 列表（可选，做头像兜底）
 *   inst.update(match, { canAct: bool });   // 每次拿到房间/对局状态后调用
 * ========================================================================== */
(function (global) {
  'use strict';

  var LANES = ['对抗路', '打野', '中路', '发育路', '游走'];
  var HERO_MAP = {};          // ename -> hero（/api/heroes 兜底字典）
  var LOG_PREFIX = '[bp-board]';

  /* ------------------------------ 工具 ------------------------------ */
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function dash(v) {
    return (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) ? '—' : v;
  }
  function fmtPct(v) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '—';
    return Number(v).toFixed(1) + '%';
  }
  function log() {
    try {
      var args = Array.prototype.slice.call(arguments);
      args.unshift(LOG_PREFIX);
      console.info.apply(console, args);
    } catch (e) { /* 忽略 */ }
  }

  function setHeroes(list) {
    (list || []).forEach(function (h) {
      if (h && h.ename !== null && h.ename !== undefined) HERO_MAP[String(h.ename)] = h;
    });
    log('英雄字典已载入', Object.keys(HERO_MAP).length, '个');
  }

  /** 头像路径：优先后端 avatar_path，其次官方 CDN，最后文字兜底 */
  function heroImgHTML(hero, fbCls) {
    var cname = (hero && (hero.cname || hero.name)) || '';
    if (hero && hero.avatar_ok === false) return fbBlock(cname, fbCls);
    var src = (hero && hero.avatar_path) || '';
    if (!src && hero && hero.ename !== null && hero.ename !== undefined) {
      src = 'https://game.gtimg.cn/images/yxzj/img201606/heroimg/' + hero.ename + '/' + hero.ename + '.jpg';
    }
    if (!src) return fbBlock(cname, fbCls);
    return '<img src="' + esc(src) + '" alt="' + esc(cname) + '" loading="lazy" decoding="async">';
  }
  function fbBlock(cname, cls) {
    var t = cname ? String(cname).charAt(0) : '?';
    return '<span class="' + (cls || 'slot-fb') + '">' + esc(t) + '</span>';
  }

  /** 头像加载失败 → 首字兜底（避免出现裂图/空白块） */
  function installImgFallback() {
    if (global.__bpBoardImgFallback) return;
    global.__bpBoardImgFallback = true;
    document.addEventListener('error', function (ev) {
      var t = ev.target;
      if (!t || t.tagName !== 'IMG') return;
      var box = t.parentNode;
      if (!box) return;
      if (!/slot-avatar|hc-avatar|gbp-avatar|bb-logo/.test(box.className || '')) return;
      var cls = /hc-avatar/.test(box.className) ? 'hc-fb'
        : (/gbp-avatar/.test(box.className) ? 'gbp-fb' : 'slot-fb');
      var span = document.createElement('span');
      span.className = cls;
      span.textContent = (t.getAttribute('alt') || '?').charAt(0) || '?';
      box.replaceChild(span, t);
    }, true);
  }

  /* ------------------------------ 数据工具 ------------------------------ */
  function enameOf(v) {
    if (v === null || v === undefined) return null;
    if (typeof v === 'number') return v;
    if (typeof v === 'object') return (v.ename !== null && v.ename !== undefined) ? v.ename : null;
    if (typeof v === 'string') {
      var n = parseInt(v, 10);
      return isNaN(n) ? v : n;
    }
    return null;
  }
  function normArr(a) { return Array.isArray(a) ? a : []; }

  /** 对局里的 picks/bans：v9 起后端会给 *_enames，老数据回退名字数组 */
  function sideList(match, kind, side) {
    var boxed = match[kind + '_enames'];
    if (boxed && Array.isArray(boxed[side])) return boxed[side].map(enameOf);
    var flat = match[side === 'blue' ? 'blue_' + kind : 'red_' + kind];
    return normArr(flat).map(enameOf);
  }

  function heroOf(ename, board) {
    if (ename === null || ename === undefined) return null;
    if (board) {
      for (var i = 0; i < board.length; i++) {
        if (String(board[i].ename) === String(ename)) return board[i];
      }
    }
    return HERO_MAP[String(ename)] || null;
  }
  function heroCname(ename, board) {
    var h = heroOf(ename, board);
    return (h && (h.cname || h.name)) || (ename === null || ename === undefined ? '—' : String(ename));
  }
  function mainLane(h) {
    if (h && typeof h.lane_main === 'string' && h.lane_main) return h.lane_main;
    if (h && Array.isArray(h.lane_list) && h.lane_list.length) return h.lane_list[0];
    return '';
  }

  /* ------------------------------ 卡槽 ------------------------------ */
  /** v20：巅峰对决盲选时对手已选必须不可见，只显示「已选 N」占位 */
  function maskSlotHTML(idx, filled) {
    if (!filled) {
      return '<div class="slot empty-slot pick-empty" data-kind="pick" data-idx="' + idx + '">' +
        '<div class="empty-ph"><span class="ph-ico">+</span><span>盲选 ' + (idx + 1) + '</span></div></div>';
    }
    return '<div class="slot slot-mask" data-kind="pick" data-idx="' + idx + '">' +
      '<div class="slot-avatar"><span class="mask-q">?</span></div>' +
      '<span class="slot-name">已选</span>' +
      '<span class="slot-sub">待公布</span></div>';
  }

  function maskSlotsHTML(count) {
    var out = '';
    for (var i = 0; i < 5; i++) out += maskSlotHTML(i, i < count);
    return out;
  }

  /** 巅峰对决揭晓塔：5 行 {lane, blue, red}，按 对抗路 → 游走 顺序 */
  function revealHtml(m, board) {
    var rows = (m && Array.isArray(m.tiebreak_reveal)) ? m.tiebreak_reveal : [];
    if (!rows.length) return '';
    var cellHtml = function (cell) {
      if (!cell) return '<span class="tb-cell is-empty"><span class="tb-fb">—</span><span class="tb-name">—</span></span>';
      var h = heroOf(cell.ename, board) || cell;
      var nm = (h && (h.cname || h.name)) || '—';
      return '<span class="tb-cell" title="' + esc(nm) + '">' +
        '<span class="tb-avatar">' + heroImgHTML(h || cell, 'tb-fb') + '</span>' +
        '<span class="tb-name">' + esc(nm) + '</span></span>';
    };
    var LANE_ORDER = ['对抗路', '打野', '中路', '发育路', '游走'];
    var body = LANE_ORDER.map(function (lane, i) {
      var row = null;
      for (var k = 0; k < rows.length; k++) {
        if (rows[k] && rows[k].lane === lane) { row = rows[k]; break; }
      }
      if (!row) row = rows[i] || {};
      return '<div class="tb-row">' +
        '<span class="tb-lane">' + esc(row.lane || lane) + '</span>' +
        '<span class="tb-side is-blue">' + cellHtml(row.blue) + '</span>' +
        '<span class="tb-vs">vs</span>' +
        '<span class="tb-side is-red">' + cellHtml(row.red) + '</span></div>';
    }).join('');
    return '<div class="tb-head"><strong>巅峰对决 · 分路公布</strong>' +
      '<span class="tb-head-tip">对抗路 → 打野 → 中路 → 发育路 → 游走</span></div>' + body;
  }

  function banSlotHTML(ename, idx, board) {
    if (ename === null || ename === undefined) {
      return '<div class="slot empty-slot ban-empty" data-kind="ban" data-idx="' + idx + '">' +
        '<div class="empty-ph"><span class="ph-ico">&#10005;</span><span>Ban ' + (idx + 1) + '</span></div></div>';
    }
    var h = heroOf(ename, board);
    var nm = heroCname(ename, board);
    return '<div class="slot slot-ban' + (idx % 2 ? ' even' : '') + '" data-kind="ban" data-idx="' + idx +
      '" title="' + esc(nm) + '">' +
      '<span class="slot-no">' + (idx + 1) + '</span>' +
      '<span class="ban-x" aria-hidden="true">&#10005;</span>' +
      '<div class="slot-avatar">' + heroImgHTML(h || { ename: ename, cname: nm }, 'slot-fb') + '</div>' +
      '<span class="slot-name">' + esc(nm) + '</span>' +
      '</div>';
  }

  function pickSlotHTML(ename, idx, board) {
    if (ename === null || ename === undefined) {
      return '<div class="slot empty-slot pick-empty" data-kind="pick" data-idx="' + idx + '">' +
        '<div class="empty-ph"><span class="ph-ico">+</span><span>Pick ' + (idx + 1) + '</span></div></div>';
    }
    var h = heroOf(ename, board);
    var nm = heroCname(ename, board);
    var wr = h ? dash(h.win_rate) : '—';
    return '<div class="slot slot-pick" data-kind="pick" data-idx="' + idx + '" title="' + esc(nm) + '">' +
      '<div class="slot-avatar">' + heroImgHTML(h || { ename: ename, cname: nm }, 'slot-fb') + '</div>' +
      '<span class="slot-name">' + esc(nm) + '</span>' +
      '<span class="slot-sub">' + (wr === '—' ? '—' : fmtPct(wr)) + '</span>' +
      '</div>';
  }

  function renderSlots(arr, total, fn, board) {
    var out = '';
    for (var i = 0; i < total; i++) out += fn(arr[i] === undefined ? null : arr[i], i, board);
    return out;
  }

  /* ------------------------------ 骨架 ------------------------------ */
  var SKELETON = [
    '<div class="bp-board">',

    '  <div class="bb-head">',
    '    <div class="bb-team is-blue"><span class="bb-logo" data-el="logoBlue"></span>',
    '      <span class="bb-name" data-el="nameBlue">蓝方</span></div>',
    '    <div class="bb-score">',
    '      <span class="bb-bo" data-el="boLabel">BO —</span>',
    '      <span class="bb-series" data-el="seriesScore">0 : 0</span>',
    '      <span class="bb-game" data-el="gameScore">第 1 局 · 0 : 0</span>',
    '    </div>',
    '    <div class="bb-team is-red"><span class="bb-name" data-el="nameRed">红方</span>',
    '      <span class="bb-logo" data-el="logoRed"></span></div>',
    '  </div>',

    '  <div class="tiebreak-banner" data-el="tiebreak" hidden>',
    '    <span class="tb-ico" aria-hidden="true">&#9889;</span>',
    '    <span class="tb-text"><strong>巅峰对决</strong> · 本局无 Ban，双方盲选；选完按 对抗路 → 游走 逐路公布（全局 BP 限制解除）</span>',
    '  </div>',

    '  <section class="bp-panel card" data-el="bpPanel" aria-label="BP 面板">',
    '    <div class="bp-side" data-side="blue">',
    '      <div class="bp-side-head"><span class="side-pill side-blue">蓝方</span>',
    '        <span class="side-team" data-el="bpTeamBlue">—</span>',
    '        <span class="side-count" data-el="countBlue"></span></div>',
    '      <div class="slot-row ban-row" data-el="banBlue"></div>',
    '      <div class="slot-row pick-row" data-el="pickBlue"></div>',
    '    </div>',
    '    <div class="turn-band" data-el="turnBand" data-side="">',
    '      <div class="turn-main"><span class="turn-dot" aria-hidden="true"></span>',
    '        <span class="turn-text" data-el="turnText">等待开始</span></div>',
    '      <div class="turn-prog"><div class="prog" aria-hidden="true"><i data-el="stepBar"></i></div>',
    '        <span class="prog-text" data-el="stepText">—</span></div>',
    '    </div>',
    '    <div class="bp-side" data-side="red">',
    '      <div class="bp-side-head"><span class="side-pill side-red">红方</span>',
    '        <span class="side-team" data-el="bpTeamRed">—</span>',
    '        <span class="side-count" data-el="countRed"></span></div>',
    '      <div class="slot-row ban-row" data-el="banRed"></div>',
    '      <div class="slot-row pick-row" data-el="pickRed"></div>',
    '    </div>',
    '  </section>',

    /* v20：巅峰对决 · 分路公布 */
    '  <section class="tb-reveal card" data-el="tbReveal" hidden></section>',

    /* v9 需求 1②：全局 BP 状态 */
    '  <section class="gbp-strip card" data-el="gbpStrip" aria-label="全局 BP 状态">',
    '    <div class="gbp-head"><b>全局 BP 状态</b>',
    '      <span class="gbp-note" data-el="gbpNote"></span></div>',
    '    <div class="gbp-row"><span class="gbp-side is-blue">蓝方本系列赛已选</span>',
    '      <div class="gbp-chips" data-el="gbpBlue"></div></div>',
    '    <div class="gbp-row"><span class="gbp-side is-red">红方本系列赛已选</span>',
    '      <div class="gbp-chips" data-el="gbpRed"></div></div>',
    '  </section>',

    '  <section class="confirm-bar card" data-el="confirmBar" hidden aria-live="polite">',
    '    <div class="cb-hero"><span class="cb-avatar" data-el="cbAvatar" aria-hidden="true"></span>',
    '      <span class="cb-meta"><strong class="cb-name" data-el="cbName">未选择英雄</strong>',
    '        <span class="cb-hint" data-el="cbHint">点击下方英雄头像预选，再点确认锁定</span></span></div>',
    '    <div class="cb-actions">',
    '      <button class="btn btn-ghost btn-sm" type="button" data-el="btnCancel" disabled>取消预选</button>',
    '      <button class="btn btn-primary btn-sm" type="button" data-el="btnConfirm" disabled>确认选择</button>',
    '    </div>',
    '  </section>',

    '  <section class="hero-dock card" data-el="heroDock" aria-label="可选英雄">',
    '    <div class="dock-head">',
    '      <div class="lane-chips" data-el="laneChips" role="group" aria-label="按位置筛选"></div>',
    '      <div class="dock-tools"><span class="dock-count" data-el="dockCount"></span>',
    '        <input class="hero-search" data-el="heroSearch" type="search" placeholder="搜索英雄"',
    '               autocomplete="off" spellcheck="false" aria-label="按名字搜索英雄"></div>',
    '    </div>',
    '    <div class="hero-grid" data-el="heroGrid"></div>',
    '    <div class="empty grid-empty" data-el="gridEmpty" hidden></div>',
    '  </section>',

    '</div>'
  ].join('\n');

  /* ------------------------------ 实例 ------------------------------ */
  function mount(container, options) {
    if (!container) throw new Error('KPLBpBoard.mount: 容器不存在');
    var opts = options || {};
    var prefix = opts.label || 'bp';
    container.innerHTML = SKELETON;
    installImgFallback();

    var refs = {};
    Array.prototype.forEach.call(container.querySelectorAll('[data-el]'), function (node) {
      refs[node.getAttribute('data-el')] = node;
    });

    var state = {
      match: null,
      canAct: false,
      pending: null,
      lane: 'all',
      query: '',
      submit: opts.onSubmit || null
    };

    /* ---- 筛选 chips ---- */
    function renderChips() {
      var html = '<button type="button" class="chip' + (state.lane === 'all' ? ' active' : '') +
        '" data-lane="all">全部</button>';
      LANES.forEach(function (l) {
        html += '<button type="button" class="chip' + (state.lane === l ? ' active' : '') +
          '" data-lane="' + esc(l) + '">' + esc(l) + '</button>';
      });
      refs.laneChips.innerHTML = html;
    }

    refs.laneChips.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.chip');
      if (!btn) return;
      state.lane = btn.getAttribute('data-lane');
      renderChips();
      renderGrid();
      log('分路筛选', state.lane);
    });
    refs.heroSearch.addEventListener('input', function () {
      state.query = String(refs.heroSearch.value || '').trim();
      renderGrid();
    });

    /* ---- 英雄网格 ---- */
    function boardList() {
      var m = state.match || {};
      if (Array.isArray(m.hero_board) && m.hero_board.length) return m.hero_board;
      return Array.isArray(m.available) ? m.available : [];
    }

    function filtered() {
      var out = boardList().filter(function (h) {
        if (state.lane !== 'all' && mainLane(h) !== state.lane) return false;
        if (state.query) {
          var nm = String((h && (h.cname || h.name)) || '');
          if (nm.toLowerCase().indexOf(state.query.toLowerCase()) < 0) return false;
        }
        return true;
      });
      out.sort(function (a, b) {
        var la = LANES.indexOf(mainLane(a)), lb = LANES.indexOf(mainLane(b));
        if (la !== lb) return (la < 0 ? 99 : la) - (lb < 0 ? 99 : lb);
        return (Number(a.ename) || 0) - (Number(b.ename) || 0);
      });
      return out;
    }

    function heroCellHTML(h) {
      var ename = h.ename;
      var nm = h.cname || h.name || String(ename);
      var locked = h.disabled === true;
      var pending = !locked && state.pending !== null && String(state.pending) === String(ename);
      var dim = locked || !state.canAct;
      var reason = locked ? String(h.reason || '当前不可选') : '';
      var tip = nm + '｜主分路 ' + (mainLane(h) || '暂无') +
        (h.win_rate != null ? '｜胜率 ' + fmtPct(h.win_rate) : '') +
        (h.ban_rate != null ? '｜Ban 率 ' + fmtPct(h.ban_rate) : '') +
        (reason ? '｜' + reason : '');
      return '<button type="button" class="hero-cell' + (dim ? ' is-dim' : '') +
        (locked ? ' is-locked' : '') + (pending ? ' is-pending' : '') +
        '" data-ename="' + esc(ename) + '"' +
        (locked ? ' aria-disabled="true" tabindex="-1"' : '') +
        (pending ? ' aria-pressed="true"' : '') +
        (reason ? ' title="' + esc(reason) + '"' : '') +
        ' aria-label="' + esc(tip) + '">' +
        (locked ? '<span class="hc-lock" aria-hidden="true">🚫</span>' : '') +
        '<span class="hc-avatar">' + heroImgHTML(h, 'hc-fb') + '</span>' +
        '<span class="hc-name">' + esc(nm) + '</span>' +
        '<span class="hc-lanes"><span class="hc-lane">' + esc(mainLane(h) || '暂无分路') + '</span></span>' +
        '<span class="hc-badges">' +
          '<span class="hc-badge wr">胜 ' + fmtPct(h.win_rate) + '</span>' +
          '<span class="hc-badge br">禁 ' + fmtPct(h.ban_rate) + '</span>' +
        '</span>' +
        '</button>';
    }

    function renderGrid() {
      var list = filtered();
      if (!list.length) {
        refs.heroGrid.innerHTML = '';
        refs.gridEmpty.hidden = false;
        refs.gridEmpty.textContent = boardList().length
          ? '没有符合筛选条件的英雄，换个位置或清空搜索试试。'
          : '对局尚未开始，暂时没有可选英雄。';
      } else {
        refs.gridEmpty.hidden = true;
        var buf = [];
        var lastLane = null;
        list.forEach(function (h) {
          var lane = mainLane(h) || '其他';
          if (!state.lane || state.lane === 'all') {
            if (lane !== lastLane) {
              lastLane = lane;
              buf.push('<div class="hc-group" role="presentation">' + esc(lane) +
                '<span class="hc-group-n">' + (h.lane_main_official === false ? '官方定位' : 'KPL 实战') +
                '</span></div>');
            }
          }
          buf.push(heroCellHTML(h));
        });
        refs.heroGrid.innerHTML = buf.join('');
      }
      var legal = 0;
      boardList().forEach(function (h) { if (h && h.disabled !== true) legal += 1; });
      refs.dockCount.textContent = boardList().length
        ? (list.length + ' / ' + boardList().length + ' 展示 · ' + legal + ' 可选')
        : '—';
      refs.heroDock.classList.toggle('locked', !state.canAct);
    }

    refs.heroGrid.addEventListener('click', function (ev) {
      var cell = ev.target.closest('.hero-cell');
      if (!cell) return;
      if (cell.getAttribute('aria-disabled') === 'true') {
        var why = cell.getAttribute('title') || '当前不可选';
        if (global.KPL && typeof global.KPL.toast === 'function') global.KPL.toast(why);
        return;
      }
      if (!state.canAct) {
        if (global.KPL && typeof global.KPL.toast === 'function') global.KPL.toast('现在还没轮到你操作');
        return;
      }
      var ename = cell.getAttribute('data-ename');
      state.pending = (String(state.pending) === String(ename)) ? null : ename;
      log('预选英雄', ename, state.pending ? '' : '（已取消）');
      renderGrid();
      renderConfirm();
    });

    /* ---- 预选确认条 ---- */
    function renderConfirm() {
      var m = state.match || {};
      // v20：巅峰对决盲选阶段没有 action 字段（双方都是 pick），需要单独判断
      var tiePick = m.phase === 'tiebreak_pick';
      if (state.pending === null || (!m.action && !tiePick)) {
        refs.confirmBar.hidden = true;
        return;
      }
      var h = heroOf(state.pending, boardList());
      refs.confirmBar.hidden = false;
      refs.cbName.textContent = (h ? (h.cname || h.name) : '英雄') +
        ' · ' + (tiePick ? '盲选' : (m.action === 'ban' ? '禁用' : '选用'));
      refs.cbHint.textContent = tiePick
        ? '盲选阶段点【确认选择】即锁定，对手看不到你选了谁'
        : '预选已高亮，点【确认选择】才会锁定并进入下一步';
      refs.cbAvatar.innerHTML = heroImgHTML(h || { ename: state.pending }, 'hc-fb');
      refs.btnConfirm.disabled = false;
      refs.btnCancel.disabled = false;
    }

    refs.btnCancel.addEventListener('click', function () {
      state.pending = null;
      renderGrid();
      renderConfirm();
    });
    refs.btnConfirm.addEventListener('click', function () {
      if (state.pending === null || typeof state.submit !== 'function') return;
      var ename = state.pending;
      state.pending = null;
      refs.btnConfirm.disabled = true;
      refs.btnCancel.disabled = true;
      refs.confirmBar.hidden = true;
      log('确认提交', ename);
      try {
        state.submit(ename);
      } catch (e) {
        log('提交异常', e && e.message);
      }
    });

    /* ---- 顶部：系列大比分 / 小局比分 ---- */
    function renderHead(m) {
      var bt = m.blue_team || {}, rt = m.red_team || {};
      refs.nameBlue.textContent = bt.name || '蓝方';
      refs.nameRed.textContent = rt.name || '红方';
      refs.bpTeamBlue.textContent = bt.name || '蓝方';
      refs.bpTeamRed.textContent = rt.name || '红方';
      refs.logoBlue.innerHTML = logoHTML(bt);
      refs.logoRed.innerHTML = logoHTML(rt);

      var bo = m.bo || 1;
      refs.boLabel.textContent = 'BO' + bo;
      var sc = m.score || {};
      var sb = Number(sc.blue || 0), sr = Number(sc.red || 0);
      refs.seriesScore.textContent = sb + ' : ' + sr;

      /* v9 需求 1③：小局比分 + 局号，一眼看出 BO 进度 */
      var gi = m.game_index || 1;
      var gs = m.game_score;
      if (!gs || (gs.blue == null && gs.red == null)) {
        var fs = m.final && m.final.score;
        gs = (fs && typeof fs === 'object') ? fs : null;
      }
      var need = Math.floor(bo / 2) + 1;
      var gameText = '第 ' + gi + ' / ' + bo + ' 局';
      if (m.series_over) gameText = '系列赛结束 · ' + (sb > sr ? '蓝方' : '红方') + ' ' + Math.max(sb, sr) + ':' + Math.min(sb, sr);
      else gameText += ' · 抢 ' + need + ' 胜';
      refs.gameScore.textContent = gameText;
    }

    function logoHTML(team) {
      var t = team || {};
      if (t.logo_ok === false) return '<span class="logo-fb">' + esc((t.abbreviation || t.name || '?').slice(0, 3)) + '</span>';
      if (t.logo_path) return '<img src="' + esc(t.logo_path) + '" alt="' + esc(t.name || '') + '" loading="lazy">';
      return '<span class="logo-fb">' + esc((t.abbreviation || t.name || '?').slice(0, 3)) + '</span>';
    }

    /* ---- BP 面板 ---- */
    function renderPanel(m) {
      var board = boardList();
      var tie = m.is_tiebreak === true;
      var hasReveal = tie && Array.isArray(m.tiebreak_reveal) && m.tiebreak_reveal.length > 0;
      refs.bpPanel.classList.toggle('no-ban', tie);
      refs.bpPanel.classList.toggle('is-tiebreak', tie);

      var bansB = sideList(m, 'bans', 'blue'), bansR = sideList(m, 'bans', 'red');
      var picksB = sideList(m, 'picks', 'blue'), picksR = sideList(m, 'picks', 'red');

      // v20：盲选过程中遮住对手已选（联机时用 my_camp 判断自己哪一方；
      // 单机自由 BP 两个 my_camp 都没有 → 不遮）
      var maskSide = '';
      if (tie && !hasReveal && (m.my_camp === 'blue' || m.my_camp === 'red')) {
        maskSide = (m.my_camp === 'blue') ? 'red' : 'blue';
      }

      refs.banBlue.innerHTML = tie ? '' : renderSlots(bansB, 5, banSlotHTML, board);
      refs.banRed.innerHTML = tie ? '' : renderSlots(bansR, 5, banSlotHTML, board);
      refs.pickBlue.innerHTML = (maskSide === 'blue')
        ? maskSlotsHTML(picksB.length)
        : renderSlots(picksB, 5, pickSlotHTML, board);
      refs.pickRed.innerHTML = (maskSide === 'red')
        ? maskSlotsHTML(picksR.length)
        : renderSlots(picksR, 5, pickSlotHTML, board);

      refs.countBlue.textContent = tie
        ? (picksB.length + '/5 · 盲选')
        : ('Ban ' + bansB.length + '/5 · Pick ' + picksB.length + '/5');
      refs.countRed.textContent = tie
        ? (picksR.length + '/5 · 盲选')
        : ('Ban ' + bansR.length + '/5 · Pick ' + picksR.length + '/5');

      if (refs.tbReveal) {
        if (hasReveal) {
          refs.tbReveal.innerHTML = revealHtml(m, board);
          refs.tbReveal.hidden = false;
        } else {
          refs.tbReveal.innerHTML = '';
          refs.tbReveal.hidden = true;
        }
      }
    }

    /* ---- 回合带 ---- */
    function renderTurn(m) {
      var tiePick = m.phase === 'tiebreak_pick';
      var live = (m.phase === 'bp' || tiePick) && !m.finished;
      refs.turnBand.classList.toggle('is-active', live);
      refs.turnBand.classList.toggle('is-finished', !live);
      refs.turnBand.classList.toggle('b-red', m.side === 'red');
      refs.turnBand.dataset.side = m.side || '';

      var label;
      if (m.series_over) label = '系列赛已结束';
      else if (m.phase === 'side_choice') label = (m.side_choice && m.side_choice.note) || '等待上局败方选边';
      else if (m.phase === 'result') label = '本小局结束，等待进入下一局';
      else if (tiePick) label = m.turn_label || '→ 巅峰对决盲选（双方同时选 5 人）';
      else if (m.turn_label) label = m.turn_label;
      else label = '→ ' + (m.side === 'red' ? '红方' : '蓝方') + ' ' + (m.action === 'ban' ? 'Ban' : 'Pick');
      if (live && m.my_turn && !tiePick) label = '轮到你 · ' + label;
      refs.turnText.textContent = label;

      if (tiePick) {
        // 盲选没有「第 N/M 步」，按我方已选人数显示
        var mine = sideList(m, 'picks', m.my_camp || m.side || 'blue').length;
        refs.stepBar.style.width = Math.min(100, (mine / 5) * 100).toFixed(2) + '%';
        refs.stepText.textContent = '已盲选 ' + mine + ' / 5 · 选完按分路公布';
        return;
      }

      var total = m.total_steps || 0, step = m.step || 0;
      var pct = total > 0 ? Math.max(0, Math.min(100, (step / total) * 100)) : 0;
      if (m.finished && total > 0) pct = 100;
      refs.stepBar.style.width = pct.toFixed(2) + '%';
      if (!total || step <= 0) refs.stepText.textContent = '—';
      else {
        var remain = Math.max(0, total - step + (m.finished ? 0 : 1));
        refs.stepText.textContent = '第 ' + step + ' / ' + total + ' 步 · 剩 ' + remain + ' 步';
      }
    }

    /* ---- 全局 BP 状态 ---- */
    function gbpChipsHtml(enames, board) {
      if (!enames || !enames.length) return '<span class="gbp-empty">暂无</span>';
      return enames.map(function (ename) {
        var h = heroOf(ename, board) || { ename: ename };
        var nm = heroCname(ename, board);
        return '<span class="gbp-chip" title="' + esc(nm) + '">' +
          '<span class="gbp-avatar">' + heroImgHTML(h, 'gbp-fb') + '</span>' + esc(nm) + '</span>';
      }).join('');
    }

    function renderGlobalBp(m) {
      var board = boardList();
      var used = m.used_global_enames || {};
      var tie = m.is_tiebreak === true;
      refs.gbpBlue.innerHTML = gbpChipsHtml(used.blue || [], board);
      refs.gbpRed.innerHTML = gbpChipsHtml(used.red || [], board);
      var nb = (used.blue || []).length, nr = (used.red || []).length;
      refs.gbpNote.textContent = tie
        ? '巅峰对决：本局无 Ban，双方同时盲选 5 人（可撞英雄），选完按 对抗路 → 打野 → 中路 → 发育路 → 游走 逐路公布，全局 BP 限制已解除'
        : ('一局内同一英雄只能被选一次；本系列赛（BO' + (m.bo || 1) + '）内已选用过的英雄本队不可再选。蓝方已用 ' +
           nb + ' 个 · 红方已用 ' + nr + ' 个');
      refs.gbpStrip.classList.toggle('is-tiebreak', tie);
    }

    /* ---- 对外 update ---- */
    function update(match, flags) {
      var f = flags || {};
      state.match = match || null;
      state.canAct = !!(match && f.canAct);
      if (!match || (match.phase !== 'bp' && match.phase !== 'tiebreak_pick')) state.pending = null;
      if (!match) {
        container.hidden = true;
        return;
      }
      container.hidden = false;

      refs.tiebreak.hidden = match.is_tiebreak !== true;
      renderHead(match);
      renderPanel(match);
      renderTurn(match);
      renderGlobalBp(match);
      renderChips();
      renderGrid();
      renderConfirm();

      log('渲染完成', 'BO' + (match.bo || 1), '第' + (match.game_index || 1) + '局',
        '步' + (match.step || 0) + '/' + (match.total_steps || 0),
        'phase=' + (match.phase || '-'), 'myTurn=' + !!match.my_turn,
        '可用=' + boardList().filter(function (h) { return h && h.disabled !== true; }).length,
        '英雄总数=' + boardList().length);
    }

    renderChips();
    return { update: update, refs: refs, state: state };
  }

  global.KPLBpBoard = { mount: mount, setHeroes: setHeroes, LANES: LANES };
})(window);
