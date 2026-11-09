/* v31（用户 m18614）：年总突围赛「选对手」页面
 *
 * 官方流程：擂台赛结束后，大师组第 5、第 6 + 精英组第 2~5 共 6 队互相选对手。
 *   1) 大师 5 先选一支对手（精英组，或大师组第 6）；
 *   2) 大师 6 在剩下的队里再选一支；
 *   3) 最后剩下的两支自动成为对手。
 * 我的队正好是选对手的那一方时由玩家点选，否则 AI 自动选（页面照样逐手演示）。
 */
(function () {
  'use strict';

  var saveId = new URLSearchParams(location.search).get('save_id') || '';
  var el = {
    title: document.getElementById('boTitle'),
    sub: document.getElementById('boSub'),
    masters: document.getElementById('boMasters'),
    elite: document.getElementById('boElite'),
    pairs: document.getElementById('boPairs'),
    turnCard: document.getElementById('boTurnCard'),
    turnTitle: document.getElementById('boTurnTitle'),
    turnSub: document.getElementById('boTurnSub'),
    start: document.getElementById('btnBoStart'),
    error: document.getElementById('boError'),
    errorText: document.getElementById('boErrorText')
  };

  var state = { pick: null, busy: false, timer: null, started: false };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fail(msg) {
    if (el.error) {
      el.error.hidden = false;
      el.errorText.textContent = msg;
    }
    if (window.KPL && KPL.toast) KPL.toast(msg);
  }

  function stopTimer() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
  }

  function chip(name, opts) {
    opts = opts || {};
    var cls = 'bo-team-chip';
    if (opts.picker) cls += ' is-picker';
    if (opts.current) cls += ' is-current';
    if (opts.used) cls += ' is-used';
    if (opts.pickable) cls += ' is-pickable';
    if (opts.mine) cls += ' is-mine';
    var tag = opts.tag ? '<span class="bo-chip-tag">' + esc(opts.tag) + '</span>' : '';
    var mine = opts.mine ? '<span class="bo-chip-mine">我</span>' : '';
    return '<button type="button" class="' + cls + '"' +
      (opts.pickable ? ' data-pick="' + esc(name) + '"' : ' disabled') + '>' +
      tag + '<span class="bo-chip-name">' + esc(name) + '</span>' + mine + '</button>';
  }

  function render() {
    var p = state.pick || {};
    var masters = p.masters || [];
    var teams = p.teams || [];
    var allElite = teams.slice(masters.length);
    var pool = p.pool || [];
    var pairs = p.pairs || [];
    var used = {};
    for (var i = 0; i < pairs.length; i++) used[pairs[i][1]] = true;

    var myTurn = !!p.my_turn;
    var current = p.current || '';

    el.masters.innerHTML = masters.map(function (team, idx) {
      return chip(team, {
        picker: true,
        tag: '第 ' + (idx + 5) + ' 顺位',
        current: !p.complete && team === current,
        mine: team === p.my_team,
        pickable: false
      });
    }).join('');

    /* v32：可选对手 = 还没配对的队（含大师组第 6），不再只有精英组 */
    var candidates = pool.slice();
    if (!candidates.length && !p.complete) candidates = allElite.filter(function (t) {
      return pairs.every(function (pair) { return pair[1] !== t; });
    });
    el.elite.innerHTML = candidates.map(function (team) {
      var isMaster = masters.indexOf(team) >= 0;
      return chip(team, {
        tag: isMaster ? '大师组' : '',
        mine: team === p.my_team,
        pickable: myTurn
      });
    }).join('') || '<div class="c-empty">没有可选的对手了</div>';

    if (pairs.length) {
      el.pairs.innerHTML = pairs.map(function (pair, i) {
        return '<div class="bo-pair">' +
          '<span class="bo-pair-no">第 ' + (i + 1) + ' 场</span>' +
          '<span class="bo-pair-team' + (pair[0] === p.my_team ? ' is-mine' : '') + '">' + esc(pair[0]) + '</span>' +
          '<span class="bo-pair-vs">VS</span>' +
          '<span class="bo-pair-team' + (pair[1] === p.my_team ? ' is-mine' : '') + '">' + esc(pair[1]) + '</span>' +
          '</div>';
      }).join('');
    } else {
      el.pairs.innerHTML = '<div class="c-empty">还没有确定的对阵</div>';
    }

    if (p.complete) {
      el.turnTitle.textContent = '对阵已确定';
      el.turnSub.textContent = '3 场 BO7 单败，胜者 3 队进入淘汰赛。点击下方按钮开始突围赛。';
    } else if (myTurn) {
      el.turnTitle.textContent = '轮到你（' + (p.my_team || '我的队') + '）选对手';
      el.turnSub.textContent = '请在下方「可选对手」里点一支战队（大师 6 也可以选）。';
    } else {
      el.turnTitle.textContent = '「' + (current || '—') + '」正在选对手…';
      el.turnSub.textContent = '大师 5 先选，大师 6 再选，剩下两支自动配对。';
    }

    var mineIsEliteLeft = !p.complete && !myTurn && (p.masters || []).indexOf(p.my_team) < 0;
    if (mineIsEliteLeft) el.turnSub.textContent += '（你的战队在精英组，等两队选完看有没有被挑中。）';

    if (el.start) el.start.hidden = !p.complete;
  }

  function applyPick(pick) {
    state.pick = pick || {};
    render();
    scheduleNext();
  }

  function scheduleNext() {
    stopTimer();
    var p = state.pick || {};
    if (!p.active) {
      /* 没有进行中的选对手（可能已经完成）→ 回赛季页 */
      location.href = '/career/season?save_id=' + encodeURIComponent(saveId);
      return;
    }
    if (p.complete || p.my_turn || state.busy) return;
    state.timer = setTimeout(function () {
      autoPick();
    }, 900);
  }

  function autoPick() {
    if (state.busy) return;
    state.busy = true;
    KPL.api.tourneyBreakoutPick(saveId).then(function (res) {
      state.busy = false;
      if (!res || res.ok === false) throw new Error((res && res.error) || '选对手失败');
      applyPick(res.pick);
    }).catch(function (err) {
      state.busy = false;
      fail('选对手失败：' + (err && err.message ? err.message : err));
    });
  }

  function humanPick(team) {
    if (state.busy) return;
    state.busy = true;
    KPL.api.tourneyBreakoutPick(saveId, team).then(function (res) {
      state.busy = false;
      if (!res || res.ok === false) throw new Error((res && res.error) || '选对手失败');
      applyPick(res.pick);
    }).catch(function (err) {
      state.busy = false;
      fail('选对手失败：' + (err && err.message ? err.message : err));
    });
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('[data-pick]') : null;
    if (!btn) return;
    var team = btn.getAttribute('data-pick');
    if (team) humanPick(team);
  });

  if (el.start) {
    el.start.addEventListener('click', function () {
      if (state.busy) return;
      state.busy = true;
      el.start.disabled = true;
      KPL.api.tourneyBreakoutCommit(saveId).then(function (res) {
        state.busy = false;
        if (!res || res.ok === false) throw new Error((res && res.error) || '开打失败');
        location.href = '/career/season?save_id=' + encodeURIComponent(saveId);
      }).catch(function (err) {
        state.busy = false;
        el.start.disabled = false;
        fail('开始突围赛失败：' + (err && err.message ? err.message : err));
      });
    });
  }

  function boot() {
    if (!saveId) { fail('缺少存档 ID'); return; }
    KPL.api.tourneyBreakout(saveId).then(function (res) {
      if (!res || res.ok === false) throw new Error((res && res.error) || '读取失败');
      state.pick = res.pick || {};
      render();
      scheduleNext();
    }).catch(function (err) {
      fail('读取突围赛数据失败：' + (err && err.message ? err.message : err));
    });
  }

  boot();
})();
