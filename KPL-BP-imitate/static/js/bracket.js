/*!
 * bracket.js —— 淘汰赛对阵流程图（v15 重绘，对齐 KPL 官方赛程图观感）
 *
 * 联赛季后赛 / 挑战者杯淘汰赛 / 年总淘汰赛共用同一个组件：
 *   KPLBracket.render(container, view, opts)
 *
 * 布局：左上是「胜者组」，左下是「败者组」，最右侧是「总决赛」；
 *       每场对阵是一张两队上下排列（队徽 + 队名 + 比分）的卡片，
 *       卡与卡之间用「右 → 竖 → 右」折线相连，同列卡片按来源居中。
 *
 * view 来自后端 game/tournament.py 的 bracket_view()：
 *   { kind, stage, stage_label, bo, rounds:[{key,name,side,slots:[...]}],
 *     winners, losers, final, champion, runner_up, available }
 * 每个 slot 含：slot/team/team_b/bo/status/winner/loser/score/src_a/src_b,
 * src_a / src_b 形如 "w_r1#2.lose"（来源轮次#槽位号.胜者/败者），用于连线与居中。
 *
 * opts：{ myTeam, label, emptyText }
 */
(function (global) {
  'use strict';

  var CW = 186;      // 卡片宽（配合 HG 让桌面端不用横向滚动就能看到总决赛）
  var CH = 62;       // 卡片高（两行队伍）
  var HG = 52;       // 列间距
  var VG = 12;       // 同列卡片间距
  var SEC_GAP = 46;  // 胜者组 / 败者组 之间的间隙
  var TAB = 26;      // 轮次名占位高度
  var PAD = 12;
  var LABEL_W = 30;  // 左侧竖排区块标签宽度

  function log() {
    try { console.info.apply(console, ['[bracket]'].concat([].slice.call(arguments))); } catch (e) {}
  }

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /** "w_r1#2.lose" -> { round:'w_r1', slot:2, kind:'lose' } */
  function parseRef(ref) {
    if (!ref || typeof ref !== 'string') return null;
    var m = ref.match(/^([A-Za-z0-9_]+)#(\d+)\.(win|lose)$/);
    if (!m) return null;
    return { round: m[1], slot: parseInt(m[2], 10), kind: m[3] };
  }

  function slotScore(slot) {
    var sc = slot && slot.score;
    if (!sc) return [null, null];
    if (Array.isArray(sc)) return [sc[0], sc[1]];
    return [sc.blue, sc.red];
  }

  function teamOf(slot, which) {
    var v = which === 'b' ? slot.team_b : slot.team;
    return v == null || v === '' ? null : String(v);
  }

  /* ---------------- 几何布局 ---------------- */

  function buildLayout(rounds) {
    var win = [], lose = [], grand = [];
    (rounds || []).forEach(function (r) {
      var side = r.side || 'winners';
      if (side === 'grand' || side === 'single') grand.push(r);
      else if (side === 'losers') lose.push(r);
      else win.push(r);
    });

    var pos = {};

    function place(round, colIndex, baseY, section, xBase) {
      var x = xBase + colIndex * (CW + HG);
      round.slots.forEach(function (slot, i) {
        var no = slot.slot || (i + 1);
        var centerY;
        if (colIndex === 0 || section === 'grand') {
          centerY = baseY + TAB + CH / 2 + i * (CH + VG);
        } else {
          var centers = [];
          [slot.src_a, slot.src_b].forEach(function (ref) {
            var p = parseRef(ref);
            if (!p) return;
            var src = pos[p.round + '#' + p.slot];
            // 只跟随同一区块的来源，避免把卡片拉进另一个区块
            if (src && src.section === section) centers.push(src.centerY);
          });
          centerY = centers.length
            ? centers.reduce(function (a, b) { return a + b; }, 0) / centers.length
            : baseY + TAB + CH / 2 + i * (CH + VG);
        }
        pos[round.key + '#' + no] = {
          x: x, top: centerY - CH / 2, centerY: centerY, w: CW, h: CH,
          section: section, round: round.key, slot: no
        };
      });
    }

    var col = 0;
    win.forEach(function (r) { place(r, col, PAD, 'win', PAD + LABEL_W); col += 1; });

    var winFirst = win.length ? win[0].slots.length : 0;
    var winBlockH = Math.max(CH, winFirst * (CH + VG) - VG);
    var loseBase = PAD + TAB + winBlockH + SEC_GAP;
    var lcol = 0;
    lose.forEach(function (r) { place(r, lcol, loseBase, 'lose', PAD + LABEL_W); lcol += 1; });

    // 总决赛：列在最右，纵向落在「胜者组决赛 / 败者组决赛」之间
    var grandX = PAD + LABEL_W + Math.max(win.length, lose.length) * (CW + HG);
    var anchorY = null;
    var lastWin = win.length ? win[win.length - 1] : null;
    var lastLose = lose.length ? lose[lose.length - 1] : null;
    var pa = lastWin ? pos[lastWin.key + '#1'] : null;
    var pb = lastLose ? pos[lastLose.key + '#1'] : null;
    if (pa && pb) anchorY = (pa.centerY + pb.centerY) / 2;
    else if (pa) anchorY = pa.centerY;
    else if (pb) anchorY = pb.centerY;

    grand.forEach(function (r, gi) {
      var x = grandX + gi * (CW + HG);
      r.slots.forEach(function (slot, i) {
        var no = slot.slot || (i + 1);
        // v27：最右列如果写了来源（年总突围赛的「淘汰赛名额」卡来自中列胜者卡），
        // 就跟来源对齐，而不是从固定基准点往下堆——否则三列对不齐。
        var centers = [];
        [slot.src_a, slot.src_b].forEach(function (ref) {
          var p = parseRef(ref);
          if (!p) return;
          var src = pos[p.round + '#' + p.slot];
          if (src) centers.push(src.centerY);
        });
        var centerY = centers.length
          ? centers.reduce(function (a, b) { return a + b; }, 0) / centers.length
          : ((anchorY == null ? PAD + CH / 2 : anchorY) + i * (CH + VG));
        pos[r.key + '#' + no] = {
          x: x, top: centerY - CH / 2, centerY: centerY, w: CW, h: CH,
          section: 'grand', round: r.key, slot: no
        };
      });
    });

    var maxX = 0, maxY = 0;
    Object.keys(pos).forEach(function (k) {
      maxX = Math.max(maxX, pos[k].x + CW);
      maxY = Math.max(maxY, pos[k].top + CH);
    });

    return {
      win: win, lose: lose, grand: grand, pos: pos,
      width: maxX + PAD, height: maxY + PAD,
      winBase: PAD, loseBase: loseBase, grandX: grandX,
      winBlockH: winBlockH,
      loseBlockH: Math.max(CH, (lose.length ? lose[0].slots.length : 0) * (CH + VG) - VG),
      hasLosers: lose.length > 0
    };
  }

  /* ---------------- 卡片 / 连线 ---------------- */

  /* 队徽：一次性拉 /api/teams 建「队名 -> logo_path」缓存，取不到的用首字兜底 */
  var LOGO_MAP = null;

  var LOGO_STORAGE_KEY = 'kpl.logoMap.v2';

  function readStoredLogos() {
    try {
      var raw = global.sessionStorage && global.sessionStorage.getItem(LOGO_STORAGE_KEY);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      return (obj && typeof obj === 'object' && Object.keys(obj).length) ? obj : null;
    } catch (e) {
      return null;
    }
  }

  function storeLogos(map) {
    try {
      if (global.sessionStorage) {
        global.sessionStorage.setItem(LOGO_STORAGE_KEY, JSON.stringify(map));
      }
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function ensureLogos(cb) {
    if (LOGO_MAP) return;
    /* v30：队徽映射不随赛季变化，先吃 sessionStorage，命中就不用发请求、也不用重绘 */
    var stored = readStoredLogos();
    if (stored) { LOGO_MAP = stored; return; }
    var api = global.KPL && global.KPL.api;
    if (!api || typeof api.teams !== 'function') { LOGO_MAP = {}; return; }
    Promise.resolve(api.teams()).then(function (res) {
      var list = (res && (res.teams || res.list)) || [];
      var map = {};
      list.forEach(function (t) {
        if (t && t.name && t.logo_path) map[String(t.name)] = t.logo_path;
      });
      LOGO_MAP = map;
      if (Object.keys(map).length) storeLogos(map);
      log('队徽缓存就绪', Object.keys(map).length, '支战队');
      if (typeof cb === 'function') cb();
    }).catch(function () {
      LOGO_MAP = {};
      log('队徽加载失败，改用首字兜底');
    });
  }

  function logoHtml(name, direct) {
    if (!name) return '<span class="bk-logo bk-logo-empty"></span>';
    var url = direct || (LOGO_MAP ? LOGO_MAP[name] : null);
    if (url) {
      return '<img class="bk-logo" src="' + esc(url) + '" alt="" loading="lazy" ' +
        'onerror="this.style.visibility=\'hidden\'">';
    }
    return '<span class="bk-logo bk-logo-fb">' + esc(String(name).slice(0, 1)) + '</span>';
  }

  function rowHtml(name, score, side, slot, opts) {
    var isWin = !!(slot.winner && name && slot.winner === name);
    var isMine = !!(opts.myTeam && name && opts.myTeam === name);
    var cls = 'bk-row' + (isWin ? ' is-win' : '') + (isMine ? ' is-mine' : '');
    // 后端 bracket_view 已经带上了队徽，优先用它（自定义/外卡战队也能出图）
    var direct = side === 'b' ? slot.team_b_logo : slot.team_logo;
    return '<div class="' + cls + '">' + logoHtml(name, direct) +
      '<span class="bk-name">' + (name ? esc(name) : 'TBD') + '</span>' +
      '<span class="bk-score">' + (score == null ? '' : esc(score)) + '</span></div>';
  }

  /* v27：展示卡——年总突围赛流程图里的「胜者」「淘汰赛」占位框（不生成比赛）。 */
  function phCardHtml(slot, ref, opts) {
    var name = teamOf(slot, 'a');
    var isMine = !!(opts.myTeam && name && opts.myTeam === name);
    var label = slot.placeholder_label || '';
    // 官方流程图中间列是「胜者 + 比分」，这里把来源那场的比分一起显示出来
    var sc = slotScore(slot);
    var scoreTxt = '';
    // 只有中间列（晋级卡）带比分；最后一列「淘汰赛」名额卡只显示队名
    if (slot.placeholder === 'winner' && name && sc[0] != null && sc[1] != null) {
      scoreTxt = '<span class="bk-score">' + esc(Math.max(sc[0], sc[1])) + ':' +
        esc(Math.min(sc[0], sc[1])) + '</span>';
    }
    var cls = 'bk-match is-ph is-ph-' + (slot.placeholder || 'slot') +
      (name ? ' is-filled' : '') + (isMine ? ' has-mine' : '');
    return '<div class="' + cls + '" data-ref="' + esc(ref) + '">' +
      (name
        ? '<div class="bk-ph-row">' + logoHtml(name, slot.team_logo) +
          '<span class="bk-name">' + esc(name) + '</span>' + scoreTxt + '</div>'
        : '') +
      '<div class="bk-ph-label">' + esc(label) + '</div>' +
      '</div>';
  }

  function cardHtml(slot, ref, opts) {
    if (slot && slot.placeholder) return phCardHtml(slot, ref, opts);
    var sc = slotScore(slot);
    var a = teamOf(slot, 'a');
    var b = teamOf(slot, 'b');
    var done = slot.status === 'done';
    var tag = slot.bo ? ('BO' + slot.bo) : '';
    var state = done ? '' : '<span class="bk-state">' + (slot.team || slot.team_b ? '待打' : '未定') + '</span>';
    var mineCls = (opts.myTeam && (a === opts.myTeam || b === opts.myTeam)) ? ' has-mine' : '';
    return '<div class="bk-match' + (done ? ' is-done' : '') + mineCls + '" data-ref="' + esc(ref) + '">' +
      '<div class="bk-tag">' + esc(tag) + state + '</div>' +
      rowHtml(a, sc[0], 'a', slot, opts) +
      rowHtml(b, sc[1], 'b', slot, opts) +
      '</div>';
  }

  function pathFor(from, to) {
    var x1 = from.x + from.w, y1 = from.centerY;
    var x2 = to.x, y2 = to.centerY;
    // 折线拐点固定在「源卡片右侧的列间空隙」，避免竖线压到别的卡片上
    var gapCenter = x1 + Math.round(HG / 2);
    var midX = Math.min(gapCenter, x2 - Math.round(HG / 2));
    if (midX <= x1) midX = x1 + 12;
    if (Math.abs(y1 - y2) < 0.5) return 'M' + x1 + ',' + y1 + 'L' + x2 + ',' + y2;
    return 'M' + x1 + ',' + y1 + 'L' + midX + ',' + y1 +
      'L' + midX + ',' + y2 + 'L' + x2 + ',' + y2;
  }

  function drawLinks(svg, layout) {
    var byKey = {};
    layout.win.concat(layout.lose, layout.grand).forEach(function (r) {
      r.slots.forEach(function (s, i) { byKey[r.key + '#' + (s.slot || i + 1)] = s; });
    });
    var out = [];
    Object.keys(layout.pos).forEach(function (key) {
      var target = layout.pos[key];
      var slot = byKey[key];
      if (!slot) return;
      [slot.src_a, slot.src_b].forEach(function (ref) {
        var p = parseRef(ref);
        if (!p) return;
        var src = layout.pos[p.round + '#' + p.slot];
        if (!src) return;
        var cls = 'bk-link' + (p.kind === 'lose' ? ' is-lose' : '');
        if (target.section === 'lose') cls += ' is-loser';
        if (target.section === 'grand') cls += ' is-final';
        out.push('<path class="' + cls + '" d="' + pathFor(src, target) + '"/>');
      });
    });
    svg.innerHTML = out.join('');
    return out.length;
  }

  function render(container, view, opts) {
    opts = opts || {};
    if (!container) return null;
    view = view || {};
    var rounds = view.rounds || [];
    if (!rounds.length) {
      container.innerHTML = '<div class="bk-empty">' + esc(opts.emptyText || '该阶段还没有淘汰赛对阵') + '</div>';
      return null;
    }
    var layout = buildLayout(rounds);
    var html = ['<div class="bk-canvas" style="width:' + layout.width + 'px;height:' + layout.height + 'px">',
      '<svg class="bk-links" width="' + layout.width + '" height="' + layout.height + '"></svg>'];

    if (layout.hasLosers) {
      html.push('<div class="bk-sec" style="top:' + layout.winBase + 'px;height:' + layout.winBlockH + 'px">' +
        '<span>胜者组</span></div>');
      html.push('<div class="bk-sec is-loser" style="top:' + layout.loseBase + 'px;height:' + layout.loseBlockH + 'px">' +
        '<span>败者组</span></div>');
    }

    layout.win.concat(layout.lose, layout.grand).forEach(function (r) {
      var first = layout.pos[r.key + '#1'];
      if (first) {
        html.push('<div class="bk-round" style="left:' + first.x + 'px;top:' + (first.top - 21) + 'px;width:' + CW + 'px">' +
          esc(r.name || '') + '</div>');
      }
      r.slots.forEach(function (slot, i) {
        var no = slot.slot || (i + 1);
        var p = layout.pos[r.key + '#' + no];
        if (!p) return;
        html.push('<div class="bk-cell" style="left:' + p.x + 'px;top:' + p.top + 'px">' +
          cardHtml(slot, r.key + '#' + no, opts) + '</div>');
      });
    });

    if (layout.grand.length && !(view.columns && view.columns.length)) {
      var g = layout.pos[layout.grand[0].key + '#1'];
      html.push('<div class="bk-final" style="left:' + layout.grandX + 'px;top:' +
        ((g ? g.top : 0) - 46) + 'px;width:' + CW + 'px">总决赛</div>');
    }

    html.push('</div>');
    container.innerHTML = html.join('');

    var svg = container.querySelector('svg.bk-links');
    var links = svg ? drawLinks(svg, layout) : 0;
    log('渲染完成', opts.label || '', '轮次', rounds.length, '卡片', Object.keys(layout.pos).length, '连线', links);

    // 队徽异步到达后原地重绘一次（首次用首字兜底，避免空白）
    ensureLogos(function () {
      if (container.isConnected) render(container, view, opts);
    });
    return { layout: layout, links: links };
  }

  global.KPLBracket = { render: render, _parseRef: parseRef, _layout: buildLayout };
})(window);
