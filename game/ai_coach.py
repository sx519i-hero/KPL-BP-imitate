# -*- coding: utf-8 -*-
"""
KPL BP 小游戏 —— AI 教练（``game/ai_coach.py``）。

契约 §5（v1）+ 契约 v2 §2.4 的实现
==================================
``choose_action(match, side, league_ctx) -> int``（返回 ``ename``）

打分公式（权重全部来自真实 JSON，不硬编码任何 KPL 数值）::

    score = 0.20*version + 0.30*synergy + 0.25*counter + 0.10*team_pref
          + 0.15*lane_need

v2 在 v1 的 4 分量基础上新增第 5 分量 ``lane_need``（补位），其 0.15 权重
**从 ``version`` 中扣除**（``version`` 0.35 → 0.20），权重总和仍为 1.0。

权重转移（契约原文）
------------------
* ``bp_history`` 为 ``_missing`` 或空 → ``synergy`` 与 ``team_pref`` 的权重并入
  ``version``（v2 下 ``version`` → ``0.60``）。
* 无真实克制数据（``league_ctx.counter_available is False``）→ ``counter`` 权重并入
  ``version``。
* 该赛季完全无分路数据（``league_ctx.lane_missing``）→ ``lane_need`` 权重归 0，
  按比例转移给其余项（契约 v2 §2.4）。

两者叠加时 ``version`` 权重为 ``1.0``，即完全按赛季表现强度选人。实现上用
:func:`effective_weights` 统一计算，保证权重始终归一化到 1。

``lane_need`` 分量（契约 v2 §2.4）
--------------------------------
* 只在 ``action == "pick"`` 时生效；``ban`` 恒为 ``None``（ban 不补位）。
* ``need = set(LANES) - 我方已覆盖分路``；候选 ``lane_list`` 与 ``need`` 有交集 → ``1.0``，
  否则 ``0.0``。
* 候选自身 ``lane_list == []`` → ``None``（不参与归一化，避免无数据英雄被系统性歧视）。
* ``need`` 为空 → ``None``。

ban 与 pick 的语义差异
--------------------
* ``action == "pick"``：``synergy`` 取“候选英雄与我方已选英雄”的协同；
  ``team_pref`` 取**我方**战队对该英雄的偏好。
* ``action == "ban"``：同样的分量反过来理解 —— ``synergy`` 取“候选英雄与敌方已选
  英雄”的协同（拆散对手体系），``team_pref`` 取**敌方**战队对该英雄的偏好（拿走对手
  擅长位）。二者都是同一份真实数据的对立使用，不引入新数据源。

难度（契约 §5）
--------------
* ``rookie``：30% 概率在候选上游随机（“瞎 BP”），其余走公式。
* ``pro``：纯公式取最大分。
* ``boss``：在 ``pro`` 之上，若当前动作是 ``ban``，优先 ban 掉敌方该赛季
  ``bp_history`` 里 pick 次数最多、且当前可 ban 的英雄。

耗时
----
契约要求“每次决策前 ``sleep(0.8~1.5s)``”。:func:`choose_action` 每次调用都会 sleep
该区间内的随机时长（:func:`decide` 可显式关闭，仅供内部批量评估使用）。
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import data_loader

__all__ = [
    "AICoachError",
    "BASE_WEIGHTS",
    "choose_action",
    "choose_side",
    "effective_weights",
    "evaluate_candidate",
    "evaluate_candidates",
    "score_candidate",
]

#: 契约 §5 基准权重（v1）。
_V1_WEIGHTS: Dict[str, float] = {
    "version": 0.35,
    "synergy": 0.30,
    "counter": 0.25,
    "team_pref": 0.10,
}

#: 契约 v2 §2.4 基准权重：新增 ``lane_need``，其 0.15 从 ``version`` 扣除。
BASE_WEIGHTS: Dict[str, float] = {
    "version": 0.20,
    "synergy": 0.30,
    "counter": 0.25,
    "team_pref": 0.10,
    "lane_need": 0.15,
}

_DIFFICULTIES = ("rookie", "pro", "boss")
#: v23：AI「思考」时长从 0.8~1.5s 降到 0.18~0.35s。
#: 原来的拟人延迟会让玩家每次确认后都要干等一秒多（AI 连走两手就是两秒多），
#: 是「点完要等好半天」的主因；现在保留一点停顿感，但体感接近即时响应。
_SLEEP_MIN = 0.05
_SLEEP_MAX = 0.12


def think_pause() -> None:
    """一次「AI 思考」停顿（v23 新增）。

    批量代走（AI 连走两手 / 一键 BP）时只在开头停一次，
    否则每手都睡会让玩家觉得程序卡住。
    """
    time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))


_ROOKIE_RANDOM_P = 0.30
#: rookie 的“上游候选池”比例（在可用英雄里按分数排前 30% 中随机）。
_ROOKIE_POOL = 0.30


class AICoachError(Exception):
    """AI 无法决策（无可用英雄 / 参数非法）时抛出。"""


# --------------------------------------------------------------------------- #
# 权重转移
# --------------------------------------------------------------------------- #


def effective_weights(league_ctx: Any) -> Dict[str, float]:
    """按契约 §5 + v2 §2.4 计算转移后的实际权重（保证总和为 1）。

    返回 ``{"version", "synergy", "counter", "team_pref", "lane_need"}``。

    转移顺序（与契约一致）：
    1. ``bp_history`` 缺失 → ``synergy + team_pref`` 并入 ``version``；
    2. 无克制数据 → ``counter`` 并入 ``version``；
    3. 整季无分路数据（``lane_missing``）→ ``lane_need`` 按比例转移给其余非零项。

    步骤 3 的“按比例转移”实现为：先算前面几步归一化后的权重，再把 ``lane_need``
    的份额按其余项的相对大小分摊，等价于把 ``lane_need`` 的份额置 0 后重新归一化。
    """
    weights = dict(BASE_WEIGHTS)
    # 1) bp_history 缺失 → synergy + team_pref 并入 version
    if bool(getattr(league_ctx, "bp_missing", True)):
        weights["version"] += weights["synergy"] + weights["team_pref"]
        weights["synergy"] = 0.0
        weights["team_pref"] = 0.0
    # 2) 无克制数据 → counter 并入 version
    if not bool(getattr(league_ctx, "counter_available", False)):
        weights["version"] += weights["counter"]
        weights["counter"] = 0.0
    # 3) 整季无分路数据 → lane_need 归 0，其余项按比例分摊（重新归一化即等价）
    if bool(getattr(league_ctx, "lane_missing", True)):
        weights["lane_need"] = 0.0
    total = sum(weights.values())
    if total <= 0:
        # 理论上不会发生；兜底成纯 version，避免除零
        return {
            "version": 1.0,
            "synergy": 0.0,
            "counter": 0.0,
            "team_pref": 0.0,
            "lane_need": 0.0,
        }
    return {key: value / total for key, value in weights.items()}


# --------------------------------------------------------------------------- #
# 各分量（全部返回 0..1 或 None；None 表示“无真实数据支撑”）
# --------------------------------------------------------------------------- #


def _version_ratio(league_ctx: Any, ename: int) -> Optional[float]:
    """版本强度：赛季胜率（归一化）与出场/禁用热度（对半加权）。

    * 胜率来自 ``hero_stats_by_league`` 的 ``win_rate``，用它在该赛季的 min–max
      线性映射到 ``0..1``。
    * 热度 = ``(pick_rate + ban_rate) / 该赛季最大值``。
    * 两者都缺失时返回 ``None``（调用方会把该分量权重让渡出去）。
    """
    # v52b：与评分器统一口径 —— 胜率先按场次收缩，再叠加热度（方案 B）。
    # 这样 AI 选人和结算评分不会「两套标准」（以前 1 场 100% 的英雄 AI 也当神抢）。
    try:
        from . import scorer as _scorer

        strength = _scorer.version_strength_for(league_ctx, ename)
    except Exception:                                   # noqa: BLE001
        strength = None
    if strength is not None:
        return strength
    win_ratio = league_ctx.win_rate_ratio(ename)
    hero = league_ctx.hero(ename)
    pick_rate = hero.get("pick_rate")
    ban_rate = hero.get("ban_rate")
    heat_ratio: Optional[float] = None
    max_contested = getattr(league_ctx, "max_contested", None)
    if (pick_rate is not None or ban_rate is not None) and max_contested:
        heat = (pick_rate or 0.0) + (ban_rate or 0.0)
        heat_ratio = max(0.0, min(1.0, heat / float(max_contested)))
    if win_ratio is None and heat_ratio is None:
        return None
    if win_ratio is None:
        return heat_ratio
    if heat_ratio is None:
        return win_ratio
    return 0.6 * win_ratio + 0.4 * heat_ratio


def _synergy_ratio(league_ctx: Any, ename: int, partners: Sequence[int]) -> Optional[float]:
    """``0..1``：候选英雄与 ``partners``（同队已选）的平均条件共现率。"""
    if bool(getattr(league_ctx, "bp_missing", True)):
        return None
    partners = [p for p in partners if isinstance(p, int) and p != ename]
    if not partners:
        # 还没有队友：用该英雄在真实 BP 里的出场率作为“体系兼容度”的弱先验
        rate = league_ctx.co_pick_rate(ename)
        return rate if rate > 0 else None
    rates = [league_ctx.synergy_rate(ename, partner) for partner in partners]
    useful = [rate for rate in rates if rate > 0]
    if not useful:
        return 0.0
    return max(0.0, min(1.0, sum(useful) / float(len(partners))))


def _team_pref_ratio(league_ctx: Any, team_name: Optional[str], ename: int) -> Optional[float]:
    """``0..1``：战队偏好；该赛季完全没有 bp_history 时返回 ``None``。"""
    if bool(getattr(league_ctx, "bp_missing", True)):
        return None
    return league_ctx.team_pref_ratio(team_name, ename)


def _counter_ratio(league_ctx: Any, ename: int, enemy_picks: Sequence[int]) -> Optional[float]:
    """克制分：用王者荣耀官网「压制 / 被压制」关系算候选英雄对当前敌方阵容的净优势。

    没有官方数据（该英雄还没详情页 / 敌方没选人）→ 返回 ``None``，权重按比例转移。
    """
    if not bool(getattr(league_ctx, "counter_available", False)):
        return None
    enemies = [e for e in (enemy_picks or []) if isinstance(e, int)]
    if not enemies:
        return None
    try:
        from . import scorer as _scorer

        return _scorer.counter_ratio_for([ename], enemies)
    except Exception:  # noqa: BLE001
        return None


def _lanes_of(league_ctx: Any, ename: int) -> List[str]:
    """取该赛季该英雄的真实分路列表（无数据返回 ``[]``，绝不推导）。"""
    lanes_getter = getattr(league_ctx, "lanes", None)
    if callable(lanes_getter):
        lanes = lanes_getter(ename)
        if lanes:
            return [str(lane) for lane in lanes]
        return []
    # 兼容极旧 context：直接读 hero dict 的 lane_list
    hero = league_ctx.hero(ename) if hasattr(league_ctx, "hero") else {}
    raw = hero.get("lane_list") if isinstance(hero, dict) else None
    return [str(lane) for lane in raw] if isinstance(raw, (list, tuple)) else []


def _covered_lanes(league_ctx: Any, picks: Sequence[int]) -> set:
    """我方已选英雄覆盖到的分路集合（只统计有真实分路数据的英雄）。"""
    covered: set = set()
    for ename in picks:
        if isinstance(ename, int):
            covered.update(_lanes_of(league_ctx, ename))
    return covered


def _lane_need_ratio(
    league_ctx: Any,
    ename: int,
    own_picks: Sequence[int],
    action: str,
) -> Optional[float]:
    """契约 v2 §2.4 的第 5 分量：补位需求命中度。

    * ``action == "ban"`` → ``None``（ban 不补位）；
    * 整季无分路数据（``lane_missing``）→ ``None``；
    * 候选自身 ``lane_list == []`` → ``None``（避免无数据英雄被系统性歧视）；
    * ``need = set(LANES) - 我方已覆盖分路``，候选分路与 ``need`` 有交集 → ``1.0``；
      否则 ``0.0``。``need`` 为空（已选满 5 条分路）→ ``None``。
    """
    if action != "pick":
        return None
    if bool(getattr(league_ctx, "lane_missing", True)):
        return None
    candidate_lanes = _lanes_of(league_ctx, ename)
    if not candidate_lanes:
        return None
    all_lanes = set(data_loader.LANES)
    own = [item for item in own_picks if isinstance(item, int)]
    if len(own) >= len(all_lanes):
        # 已选 5 人及以上，理论上不会发生
        return None
    need = all_lanes - _covered_lanes(league_ctx, own)
    if not need:
        return None
    return 1.0 if need.intersection(candidate_lanes) else 0.0


# --------------------------------------------------------------------------- #
# 候选评估
# --------------------------------------------------------------------------- #


def _side_team_name(match: Dict[str, Any], side: str) -> Optional[str]:
    team = match.get(f"{side}_team") or {}
    if isinstance(team, dict):
        name = team.get("name")
        if name:
            return str(name)
    return None


def _picks_of(match: Dict[str, Any], side: str) -> List[int]:
    picks = match.get("picks") or {}
    value = picks.get(side) or []
    return [item for item in value if isinstance(item, int)]


def _available_enames(match: Dict[str, Any]) -> List[int]:
    result: List[int] = []
    for item in match.get("available") or []:
        if isinstance(item, dict):
            ename = item.get("ename")
        else:
            ename = item
        if isinstance(ename, int) and ename not in result:
            result.append(ename)
    return result


def evaluate_candidate(
    match: Dict[str, Any],
    side: str,
    ename: int,
    league_ctx: Any,
    action: Optional[str] = None,
) -> Dict[str, Any]:
    """评估单个候选英雄，返回各分量与加权得分（供 AI 与评分推荐共用）。

    返回::

        {"ename": int, "score": float(0..100), "action": str,
         "parts": {"version": float|None, "synergy": float|None,
                   "counter": float|None, "team_pref": float|None,
                   "lane_need": float|None},
         "weights": {...}}

    ``lane_need`` 是 v2 契约 §2.4 新增的第 5 分量。
    """
    action = action or str(match.get("action") or "pick")
    enemy_side = "red" if side == "blue" else "blue"
    own_picks = _picks_of(match, side)
    enemy_picks = _picks_of(match, enemy_side)

    if action == "ban":
        # ban：拆对手体系 + 拿走对手擅长位
        partner_picks = enemy_picks
        pref_team = _side_team_name(match, enemy_side)
    else:
        partner_picks = own_picks
        pref_team = _side_team_name(match, side)

    parts: Dict[str, Optional[float]] = {
        "version": _version_ratio(league_ctx, ename),
        "synergy": _synergy_ratio(league_ctx, ename, partner_picks),
        "counter": _counter_ratio(league_ctx, ename, enemy_picks),
        "team_pref": _team_pref_ratio(league_ctx, pref_team, ename),
        "lane_need": _lane_need_ratio(league_ctx, ename, own_picks, action),
    }
    weights = effective_weights(league_ctx)

    # 分量缺失 → 其权重按剩余有数据分量等比转移（等价于在该候选上重新归一化）
    available_weight = 0.0
    for key, value in parts.items():
        if value is not None and weights.get(key, 0.0) > 0:
            available_weight += weights[key]
    if available_weight <= 0:
        score = 0.0
    else:
        acc = 0.0
        for key, value in parts.items():
            if value is None:
                continue
            acc += weights[key] * value
        score = 100.0 * acc / available_weight

    return {
        "ename": ename,
        "score": max(0.0, min(100.0, score)),
        "action": action,
        "parts": parts,
        "weights": weights,
    }


def evaluate_candidates(
    match: Dict[str, Any],
    side: str,
    league_ctx: Any,
    action: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """对 ``match["available"]`` 全部候选打分，按分数降序返回。"""
    result = [
        evaluate_candidate(match, side, ename, league_ctx, action)
        for ename in _available_enames(match)
    ]
    result.sort(key=lambda item: (-item["score"], item["ename"]))
    return result


def score_candidate(
    match: Dict[str, Any],
    side: str,
    ename: int,
    league_ctx: Any,
    action: Optional[str] = None,
) -> float:
    """单候选得分（``0..100`` float，未四舍五入）。"""
    return evaluate_candidate(match, side, ename, league_ctx, action)["score"]


# --------------------------------------------------------------------------- #
# 难度策略
# --------------------------------------------------------------------------- #


def _boss_ban_target(match: Dict[str, Any], side: str, league_ctx: Any) -> Optional[int]:
    """boss：敌方该赛季 bp_history 里 pick 最多、且当前可 ban 的英雄。"""
    if str(match.get("action")) != "ban":
        return None
    if bool(getattr(league_ctx, "bp_missing", True)):
        return None
    enemy_side = "red" if side == "blue" else "blue"
    enemy_team = _side_team_name(match, enemy_side)
    prefs = getattr(league_ctx, "team_preference", {}) or {}
    table = prefs.get(enemy_team or "") or {}
    if not table:
        return None
    available = set(_available_enames(match))
    ranked = sorted(table.items(), key=lambda kv: (-kv[1], kv[0]))
    for ename, count in ranked:
        if count > 0 and ename in available:
            return ename
    return None


def _rookie_pick(ranked: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rookie：30% 概率在上游候选池里随机；否则取第一名。"""
    if not ranked:
        raise AICoachError("没有可选英雄")
    if random.random() < _ROOKIE_RANDOM_P and len(ranked) > 1:
        pool_size = max(1, int(round(len(ranked) * _ROOKIE_POOL)))
        pool_size = min(pool_size, len(ranked))
        return random.choice(ranked[:pool_size])
    return ranked[0]


# --------------------------------------------------------------------------- #
# 败方选边（契约 v2 §3.3「AI 自动选边的决策依据」）
# --------------------------------------------------------------------------- #


def _as_rate(value: Any) -> Optional[float]:
    """把 ``blue_win_rate`` / ``red_win_rate`` 清洗成 ``float`` 或 ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def choose_side(
    league_ctx: Any,
    team_name: Optional[str],
    *,
    sleep: bool = True,
) -> Dict[str, Any]:
    """AI 选边（契约 v2 §3.3）：在该赛季 ``team_stats`` 里比较蓝/红胜率，选更高的一方。

    真实数据决定一切：
    * 两侧都有 ``blue_win_rate`` / ``red_win_rate`` → 取胜率高的一方；相等取蓝色方。
    * 只有一侧有数据 → 取有数据的一侧（另一侧无从比较）。
    * 两侧都没有 → 默认 ``"blue"``，并在 ``note`` 里写明「该赛季无蓝红胜率数据，默认蓝色方」。

    返回::

        {"side": "blue"|"red",
         "blue_win_rate": float|None, "red_win_rate": float|None,
         "note": str, "source": "team_stats"|"fallback"|"single_side"}

    ``sleep=True`` 时会 ``sleep(0.8~1.5s)``（契约要求 AI 思考时间）。
    """
    if sleep:
        time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

    team: Dict[str, Any] = {}
    if team_name:
        by_name = getattr(league_ctx, "team_by_name", None) or {}
        candidate = by_name.get(str(team_name))
        if isinstance(candidate, dict):
            team = candidate
    blue_rate = _as_rate(team.get("blue_win_rate"))
    red_rate = _as_rate(team.get("red_win_rate"))

    if blue_rate is None and red_rate is None:
        return {
            "side": "blue",
            "blue_win_rate": None,
            "red_win_rate": None,
            "note": "该赛季无蓝红胜率数据，默认蓝色方",
            "source": "fallback",
        }
    if red_rate is None:
        return {
            "side": "blue",
            "blue_win_rate": blue_rate,
            "red_win_rate": None,
            "note": "该战队该赛季仅有蓝色方胜率数据，选择蓝色方",
            "source": "single_side",
        }
    if blue_rate is None:
        return {
            "side": "red",
            "blue_win_rate": None,
            "red_win_rate": red_rate,
            "note": "该战队该赛季仅有红色方胜率数据，选择红色方",
            "source": "single_side",
        }
    if blue_rate >= red_rate:
        return {
            "side": "blue",
            "blue_win_rate": blue_rate,
            "red_win_rate": red_rate,
            "note": "蓝色方胜率 %.2f%% 高于红色方 %.2f%%，选择蓝色方" % (blue_rate, red_rate),
            "source": "team_stats",
        }
    return {
        "side": "red",
        "blue_win_rate": blue_rate,
        "red_win_rate": red_rate,
        "note": "红色方胜率 %.2f%% 高于蓝色方 %.2f%%，选择红色方" % (red_rate, blue_rate),
        "source": "team_stats",
    }


def choose_action(
    match: Dict[str, Any],
    side: str,
    league_ctx: Any,
    *,
    action: Optional[str] = None,
) -> int:
    """AI 选择一手（返回 ``ename``）。契约 §5。

    每次调用都会 ``sleep(0.8~1.5s)``（契约硬要求，前端有 loading 兜底）。
    ``action`` 缺省取 ``match["action"]``（当前步要求的动作）。

    无可用英雄时抛 :class:`AICoachError`。
    """
    time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))
    return decide(match, side, league_ctx, action=action)


def decide(
    match: Dict[str, Any],
    side: str,
    league_ctx: Any,
    *,
    action: Optional[str] = None,
) -> int:
    """不做 sleep 的决策内核（:func:`choose_action` 的同步睡眠包装）。

    单独暴露给 ``bp_engine.auto_advance`` 与测试脚本做批量评估；对外行为与
    :func:`choose_action` 一致（同一套公式与难度策略）。
    """
    if side not in ("blue", "red"):
        raise AICoachError(f"非法阵营: {side!r}")
    action = action or str(match.get("action") or "pick")
    if action not in ("ban", "pick"):
        raise AICoachError(f"非法动作: {action!r}")

    ranked = evaluate_candidates(match, side, league_ctx, action)
    if not ranked:
        raise AICoachError("没有可选英雄（available 为空）")

    difficulty = str(match.get("difficulty") or "pro").lower()
    if difficulty not in _DIFFICULTIES:
        difficulty = "pro"

    if difficulty == "boss":
        target = _boss_ban_target(match, side, league_ctx)
        if target is not None:
            return target
        return ranked[0]["ename"]
    if difficulty == "rookie":
        return _rookie_pick(ranked)["ename"]
    return ranked[0]["ename"]
