# -*- coding: utf-8 -*-
"""
KPL BP 小游戏 —— 阵容评分（``game/scorer.py``）。

契约 §6 的实现
=============
``score_lineup(picks, enemy_picks, league_ctx)`` →
``{"score": int, "breakdown": {...}, "notes": [...], "missing": [...]}``

五项基准权重：``system 30 / counter 25 / version 20 / lane 15 / weakness 10``。

权重转移
--------
契约 §6 明确：**无真实克制数据时 ``counter`` 记 0，其 25 分权重按
30:20:15:10 的比例转移给其余四项**（转移后 ``system`` 上限为 40，属正常）。
契约 §2 又要求“某项无真实数据支撑时该项为 0，且其权重按 §6 规则转移”，因此这里把
转移规则做成通用形式：

    有效权重_y = 基准权重_y × 100 / Σ(有数据支撑项的基准权重)

只转移 ``counter`` 时即 100/75 ≈ 1.3333 倍，与契约 §6 的 30:20:15:10 比例完全一致。

五项如何从**真实数据**推导（不硬编码任何 KPL 数值）
------------------------------------------------
* ``system``（体系完整度）：分路覆盖是否齐全 ＋ 有真实赛季数据的英雄占比。
* ``counter``（克制）：暂无真实克制数据 → 恒 0，权重转移。
* ``version``（版本强度）：该赛季 ``win_rate`` 的 min–max 线性映射（契约 §6）。
* ``lane``（分路契合）：把 5 个 pick 与该赛季真实 ``lane_list`` 做最优匹配，命中主分路
  记满分，命中副分路打折，完全不匹配记 0。**``lane_list`` 为空的英雄不参与匹配**；
  双方全部 pick 都没有分路数据时该项判为无数据，其权重按现有比例转移。
* ``weakness``（阵容短板）：``1 - 短板程度``，短板来自“分路缺口”与“所选英雄胜率显著
  低于同分路中位数”。

``score == round(sum(breakdown.values()))`` 且 ``0 <= score <= 100``。
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import ai_coach, data_loader
from .data_loader import LANES

__all__ = [
    "BASE_WEIGHTS",
    "recommend",
    "recommend_candidates",
    "score_lineup",
    "score_match",
]

#: 契约 §6 基准权重（五项，总和 100）。
BASE_WEIGHTS: Dict[str, int] = {
    "system": 30,
    "counter": 25,
    "version": 20,
    "lane": 15,
    "weakness": 10,
}

#: 分路匹配质量：主分路命中 / 仅副分路命中。
_LANE_PRIMARY = 1.0
_LANE_SECONDARY = 0.75
#: 副分路命中在「分路契合」里的折算（主分路 1.0）。
_SECONDARY_DISCOUNT = 0.85
#: 短板判定：所选英雄胜率低于同分路中位数超过该绝对值（百分点）即算短板。
_WEAKNESS_WIN_RATE_GAP = 1.5


# --------------------------------------------------------------------------- #
# 权重转移
# --------------------------------------------------------------------------- #


def effective_weights(active: Iterable[str]) -> Dict[str, int]:
    """按契约 §6 规则把“无数据项”的权重按比例转移给有数据的项。

    ``active`` 是有真实数据支撑的分项名集合；返回 ``{分项: 有效权重}``，
    未激活的分项权重为 0，激活项权重之和为 100（整数化，余数补给 ``system``
    以保证总和恰为 100）。
    """
    active_set = {name for name in active if name in BASE_WEIGHTS}
    if not active_set:
        return {name: 0 for name in BASE_WEIGHTS}
    base_total = sum(BASE_WEIGHTS[name] for name in active_set)
    if base_total <= 0:
        return {name: 0 for name in BASE_WEIGHTS}
    result = {name: 0 for name in BASE_WEIGHTS}
    assigned = 0
    for name in BASE_WEIGHTS:
        if name in active_set:
            value = int(round(BASE_WEIGHTS[name] * 100.0 / base_total))
            result[name] = value
            assigned += value
    # 整数化余数补给权重最大的激活项（通常是 system），保证总和 100
    if assigned != 100 and active_set:
        pivot = max(active_set, key=lambda key: (BASE_WEIGHTS[key], key))
        result[pivot] += 100 - assigned
    return result


# --------------------------------------------------------------------------- #
# 派生参考量
# --------------------------------------------------------------------------- #


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _lane_win_rate_median(league_ctx: Any) -> Dict[str, float]:
    """每个分路的赛季胜率中位数（来自真实 hero_stats）。

    v2：分路归属只取该赛季 ``lane_list``；无分路数据的英雄不计入任何分路桶。
    """
    buckets: Dict[str, List[float]] = {}
    for hero in (getattr(league_ctx, "heroes", {}) or {}).values():
        win_rate = hero.get("win_rate")
        if win_rate is None:
            continue
        _primary, lanes = _hero_lanes(hero)
        for lane in lanes:
            buckets.setdefault(lane, []).append(float(win_rate))
    result: Dict[str, float] = {}
    for lane, values in buckets.items():
        median = _median(values)
        if median is not None:
            result[lane] = median
    return result


def _hero_lanes(hero: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """返回 ``(主分路, 全部分路)``。

    v2 契约 §2.3：**只用 ``lane_list``（该赛季 KPL 实战分路）**，不再读 ``role`` /
    ``hero_type``。``lane_list`` 为空 → ``(None, [])``，表示该英雄无分路数据、不参与
    分路匹配。
    """
    raw = hero.get("lane_list")
    lanes: List[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            lane = str(item) if item else ""
            if lane in LANES and lane not in lanes:
                lanes.append(lane)
    if not lanes:
        return None, []
    return lanes[0], lanes


def _match_lanes_to_slots(
    picks: Sequence[int],
    league_ctx: Any,
) -> Tuple[float, List[str], List[str]]:
    """把 ``picks`` 与 5 条分路做贪心最优匹配。

    返回 ``(质量 0..1, 命中的分路列表, 未覆盖的分路列表)``。质量按“每个 pick 的最优
    可用分路契合度”求和后除以其目标数量。

    v2 契约 §2.3：只使用该赛季 ``lane_list``；``lane_list`` 为空的英雄**不参与**
    分路匹配，也不计入目标数量（避免“无分路数据”被重复惩罚）。若所有 pick 都没有
    分路数据，返回 ``(0.0, [], 全部 5 条分路)``，调用方据此把 ``lane`` 项判为无数据。
    """
    if not picks:
        return 0.0, [], list(LANES)
    result = _assign_slots(picks, league_ctx)
    return result["quality"], result["matched"], result["uncovered"]


def _assign_slots(
    picks: Sequence[int],
    league_ctx: Any,
) -> Dict[str, Any]:
    """贪心把 picks 分配到 5 条分路，返回（质量 / 命中分路 / 缺口 / 分路→pick 序号）。

    v49：抽出来给「小分详情」用 —— 结算界面要按 对抗路→打野→中路→发育路→游走
    逐条对比双方，需要知道每个英雄站的是哪条路。
    """
    heroes = [league_ctx.hero(ename) for ename in picks]
    options: List[Tuple[float, int, Optional[str]]] = []  # (质量, pick 序号, 分路)
    lane_ready_picks = 0
    for index, hero in enumerate(heroes):
        primary, lanes = _hero_lanes(hero)
        if not lanes:
            continue
        lane_ready_picks += 1
        for lane in lanes:
            if lane not in LANES:
                continue
            quality = _LANE_PRIMARY
            if lane != primary:
                quality = _LANE_SECONDARY * _SECONDARY_DISCOUNT
            options.append((quality, index, lane))
    if lane_ready_picks <= 0:
        # 全员无该赛季分路数据 → 无法判断分路覆盖，交由调用方转移权重
        return {"quality": 0.0, "matched": [], "uncovered": list(LANES), "slots": {}}
    # 贪心：先安排质量高的（同质量按 pick 顺序、分路顺序稳定）
    options.sort(key=lambda item: (-item[0], item[1], item[2] or ""))

    used_picks: set = set()
    used_lanes: set = set()
    slots: Dict[str, int] = {}
    total_quality = 0.0
    matched_lanes: List[str] = []
    for quality, index, lane in options:
        if index in used_picks or lane in used_lanes or lane is None:
            continue
        used_picks.add(index)
        used_lanes.add(lane)
        total_quality += quality
        matched_lanes.append(lane)
        slots[lane] = index

    target = min(len(LANES), lane_ready_picks)
    if target <= 0:
        return {"quality": 0.0, "matched": matched_lanes, "uncovered": list(LANES), "slots": slots}
    ratio = max(0.0, min(1.0, total_quality / float(target)))
    uncovered = [lane for lane in LANES if lane not in used_lanes]
    return {"quality": ratio, "matched": matched_lanes, "uncovered": uncovered, "slots": slots}


# --------------------------------------------------------------------------- #
# 官方对位克制数据（v43）
# --------------------------------------------------------------------------- #
# 数据源：王者荣耀官网英雄详情页「英雄关系」里的 压制英雄 / 被压制英雄，
# 由 scraper.py 的 step_hero_relations() 抓成 data/hero_relations.json。
# 抓不到的英雄（新英雄还没有详情页）在这里就是缺席 —— 缺数据时 counter 项不激活。
_RELATIONS_CACHE: Dict[str, Any] = {"mtime": None, "map": {}}


def _relations_path() -> str:
    try:
        from . import paths as _paths

        return os.path.join(_paths.data_dir(), "hero_relations.json")
    except Exception:  # noqa: BLE001
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(here, "data", "hero_relations.json")


def counter_map() -> Dict[int, Dict[str, Any]]:
    """``{ename: {"cname": str, "counters": set, "countered_by": set}}``（带 mtime 缓存）。"""
    path = _relations_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime is not None and _RELATIONS_CACHE.get("mtime") == mtime:
        return _RELATIONS_CACHE["map"]
    mapping: Dict[int, Dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        for row in payload.get("heroes") or []:
            ename = row.get("ename")
            if not isinstance(ename, int):
                continue
            mapping[ename] = {
                "cname": row.get("cname"),
                "counters": set(row.get("counters_enames") or []),
                "countered_by": set(row.get("countered_by_enames") or []),
            }
    except Exception:  # noqa: BLE001
        mapping = {}
    _RELATIONS_CACHE["mtime"] = mtime
    _RELATIONS_CACHE["map"] = mapping
    return mapping


def counter_ratio_for(
    picks: Sequence[int], enemy_picks: Sequence[int]
) -> Optional[float]:
    """把「双方阵容互相压制」换算成 0~1 的比值；没有官方数据时返回 None。

    规则（全部来自官网真实关系，不含任何手填系数）：
      本方英雄 A 压制 敌方英雄 B → +1；反之（B 压制 A）→ -1；
      逐对累加后除以「双方组合数」，再放大 3 倍（官方每英雄最多 2 条压制关系，
      实际对局里净优势通常很小，放大后才有区分度），最后夹到 -1~1。
    """
    mine = [e for e in (picks or []) if isinstance(e, int)]
    theirs = [e for e in (enemy_picks or []) if isinstance(e, int)]
    if not mine or not theirs:
        return None
    mapping = counter_map()
    if not mapping:
        return None
    known = sum(1 for e in mine if e in mapping)
    if known == 0:
        return None
    total = 0
    net = 0
    for a in mine:
        mine_row = mapping.get(a) or {}
        mine_counters = mine_row.get("counters") or set()
        for b in theirs:
            enemy_row = mapping.get(b) or {}
            enemy_counters = enemy_row.get("counters") or set()
            if b in mine_counters:
                net += 1
            if a in enemy_counters:
                net -= 1
            total += 1
    if total <= 0:
        return None
    raw = net / float(total)
    scaled = max(-1.0, min(1.0, raw * 3.0))
    return 0.5 + 0.5 * scaled


# --------------------------------------------------------------------------- #
# 单阵容评分
# --------------------------------------------------------------------------- #


def score_lineup(
    picks: Sequence[int],
    enemy_picks: Sequence[int],
    league_ctx: Any,
) -> Dict[str, Any]:
    """给一套阵容打分（契约 §6）。

    :param picks: 本方 pick 的 ``ename`` 列表（0–5 个；不足 5 个也能算，只是分路覆盖
        与体系分偏低）。
    :param enemy_picks: 敌方 pick 的 ``ename`` 列表（当前仅 ``counter`` 项会用到，
        而该项无真实数据支撑、恒为 0）。
    :param league_ctx: :class:`game.data_loader.LeagueContext`。

    返回 ``{"score": int, "breakdown": {五项: int}, "notes": [..], "missing": [..]}``。
    """
    pick_list = [ename for ename in picks if isinstance(ename, int)]
    enemy_list = [ename for ename in (enemy_picks or []) if isinstance(ename, int)]
    heroes = [league_ctx.hero(ename) for ename in pick_list]

    notes: List[str] = []
    missing: List[str] = []

    # ---- system：分路覆盖 + 真实数据覆盖 ----
    lane_ratio, matched_lanes, uncovered_lanes = _match_lanes_to_slots(pick_list, league_ctx)
    lane_ready = sum(1 for hero in heroes if _hero_lanes(hero)[1])
    stats_count = sum(1 for hero in heroes if hero.get("has_stats"))
    stats_ratio = stats_count / float(len(heroes)) if heroes else 0.0
    coverage_ratio = (
        len(matched_lanes) / float(min(len(LANES), lane_ready)) if lane_ready else 0.0
    )
    coverage_ratio = max(0.0, min(1.0, coverage_ratio))
    system_ratio = 0.5 * coverage_ratio + 0.3 * lane_ratio + 0.2 * stats_ratio
    # 有 pick 就算激活：分路数据缺失只让分路相关的两个子项贡献 0，
    # 不能把整个 system 项判为“无数据”（否则会连带把 system 权重也转移掉）。
    system_active = bool(pick_list)

    # ---- version：该赛季 win_rate min–max 线性映射 ----
    ratios: List[float] = []
    for hero in heroes:
        ratio = league_ctx.win_rate_ratio(hero.get("ename"))
        if ratio is not None:
            ratios.append(ratio)
    version_ratio = sum(ratios) / float(len(ratios)) if ratios else None

    # ---- lane：分路契合（独立于 system 的匹配质量）----
    lane_active = bool(pick_list) and bool(matched_lanes)
    lane_ratio_effective = lane_ratio

    # ---- weakness：1 - 短板程度 ----
    median_by_lane = _lane_win_rate_median(league_ctx)
    weakness_penalties: List[float] = []
    weak_heroes: List[str] = []
    for hero in heroes:
        _primary, lanes = _hero_lanes(hero)
        win_rate = hero.get("win_rate")
        if win_rate is None or not lanes:
            continue
        medians = [
            median_by_lane[lane] for lane in lanes if lane in median_by_lane
        ]
        if not medians:
            continue
        # 多分路英雄以其最优分路的中位数作基准，避免被次分路无谓扣分
        median = max(medians)
        gap = float(median) - float(win_rate)
        if gap > _WEAKNESS_WIN_RATE_GAP:
            weakness_penalties.append(min(1.0, gap / 10.0))
            weak_heroes.append(str(hero.get("cname") or hero.get("ename")))
    # 分路缺口：只有在“有分路数据可判断”时才计入，否则视为无数据而非短板
    gap_ratio = (len(uncovered_lanes) / float(len(LANES))) if lane_ready else 0.0
    weakness_ratio: Optional[float] = None
    if pick_list and (median_by_lane or (lane_ready and uncovered_lanes)):
        penalty = 0.0
        if weakness_penalties:
            penalty += 0.6 * (sum(weakness_penalties) / float(len(pick_list)))
        penalty += 0.4 * gap_ratio
        weakness_ratio = max(0.0, min(1.0, 1.0 - penalty))
        if not weakness_penalties and not (lane_ready and uncovered_lanes):
            notes.append("无明显短板：分路覆盖齐全、所选英雄均不弱于同分路平均水平")
        for name in weak_heroes[:2]:
            missing.append(f"{name} 该赛季胜率低于同分路中位数")
        if lane_ready and uncovered_lanes:
            missing.append("缺少 " + "、".join(uncovered_lanes) + " 位英雄")

    # ---- counter：官方「压制 / 被压制」关系（v43）----
    counter_ratio: Optional[float] = counter_ratio_for(pick_list, enemy_list)
    counter_active = counter_ratio is not None

    ratios_map: Dict[str, Optional[float]] = {
        "system": system_ratio if system_active else None,
        "counter": counter_ratio,
        "version": version_ratio,
        "lane": lane_ratio_effective if lane_active else None,
        "weakness": weakness_ratio,
    }
    active = [name for name, value in ratios_map.items() if value is not None]
    weights = effective_weights(active)
    counter_adv: Optional[int] = None
    if counter_active:
        mapping = counter_map()
        adv = 0
        for a in pick_list[:5]:
            row = mapping.get(a) or {}
            for b in enemy_list[:5]:
                if b in (row.get("counters") or set()):
                    adv += 1
                if a in ((mapping.get(b) or {}).get("counters") or set()):
                    adv -= 1
        counter_adv = adv
        if adv > 0:
            notes.append("对位上占优：我方英雄压制关系多 %d 条（官方官网克制数据）" % adv)
        elif adv < 0:
            missing.append("对位上略吃亏：对方压制关系多 %d 条" % (-adv))
        else:
            notes.append("对位克制基本持平（官方官网克制数据）")
    elif enemy_list:
        missing.append("所选英雄暂无官方克制数据，克制分记 0（权重已按比例转移到其余四项）")
    else:
        missing.append("对手还没选人，克制分暂不计算（权重已按比例转移到其余四项）")

    breakdown: Dict[str, int] = {}
    for name in BASE_WEIGHTS:
        value = ratios_map.get(name)
        if value is None or weights.get(name, 0) <= 0:
            breakdown[name] = 0
        else:
            breakdown[name] = int(
                max(0, min(weights[name], int(round(weights[name] * float(value)))))
            )
    score = int(round(sum(breakdown.values())))
    score = max(0, min(100, score))

    # ---- notes：定性文案（只引用真实字段，不编造数值）----
    if not pick_list:
        notes.append("尚未选择英雄")
    else:
        if stats_ratio >= 1.0:
            notes.append("所选英雄均有该赛季真实数据支撑")
        elif stats_ratio < 1.0:
            notes.append("部分所选英雄无该赛季数据，相关分项已按规则折算")
        if len(matched_lanes) == len(LANES) and len(pick_list) >= len(LANES):
            notes.append("分路覆盖齐全")
        if version_ratio is not None:
            if version_ratio >= 0.66:
                notes.append("版本强度处于该赛季上游")
            elif version_ratio <= 0.33:
                notes.append("版本强度处于该赛季下游")
        if lane_ratio_effective >= 0.9 and lane_active:
            notes.append("英雄分路契合度高")

    # 去重但保持顺序
    def _dedup(items: List[str]) -> List[str]:
        seen: set = set()
        output: List[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                output.append(item)
        return output

    # 实际生效上限：权重按比例转移后，各分项的上限会变（如只剩 system+weakness 时
    # weakness 上限可达 25）。前端必须用这份 caps 渲染 "x/上限"，不能硬编码。
    caps: Dict[str, int] = {}
    for name in BASE_WEIGHTS:
        value = ratios_map.get(name)
        weight = weights.get(name, 0)
        caps[name] = 0 if (value is None or weight <= 0) else int(round(weight))

    return {
        "score": score,
        "breakdown": breakdown,
        "caps": caps,
        "notes": _dedup(notes),
        "missing": _dedup(missing),
        # v49：内部统计，供 score_detail() 生成「小分详情」文案用（前端不直接读）
        "_stats": {
            "picks": list(pick_list),
            "enemy": list(enemy_list),
            "matched_lanes": list(matched_lanes),
            "uncovered_lanes": list(uncovered_lanes),
            "lane_ratio": lane_ratio_effective,
            "stats_count": stats_count,
            "stats_ratio": stats_ratio,
            "version_ratio": version_ratio,
            "weak_heroes": list(weak_heroes),
            "weakness_ratio": weakness_ratio,
            "counter_adv": counter_adv,
            "counter_ratio": counter_ratio,
            "weights": dict(weights),
        },
    }


# --------------------------------------------------------------------------- #
# 推荐
# --------------------------------------------------------------------------- #


def _reason_for(entry: Dict[str, Any], league_ctx: Any, match: Dict[str, Any], side: str) -> str:
    """把候选的分数构成翻译成定性中文文案（不编造任何数值，除真实字段）。"""
    hero = league_ctx.hero(entry["ename"])
    cname = str(hero.get("cname") or entry["ename"])
    action = entry.get("action") or "pick"
    parts = entry.get("parts") or {}
    weights = entry.get("weights") or {}

    own_picks = (match.get("picks") or {}).get(side) or []
    _, _, uncovered = _match_lanes_to_slots(own_picks, league_ctx)

    reasons: List[str] = []
    _primary, hero_lanes = _hero_lanes(hero)
    if action == "pick" and parts.get("lane_need") == 1.0:
        hit = [lane for lane in hero_lanes if lane in uncovered]
        if hit:
            reasons.append(f"补足{hit[0]}")
    if parts.get("synergy") is not None and (parts.get("synergy") or 0) >= 0.06:
        reasons.append("与已选英雄协同高" if action == "pick" else "可拆散对手体系")
    if parts.get("team_pref") is not None and (parts.get("team_pref") or 0) >= 0.4:
        reasons.append("该队常用英雄" if action == "pick" else "对手常用英雄")
    version_ratio = parts.get("version")
    if version_ratio is not None and weights.get("version", 0) > 0:
        win_rate = hero.get("win_rate")
        if version_ratio >= 0.6:
            if win_rate is not None:
                reasons.append(f"增强版本强度（该赛季胜率 {win_rate:.1f}%）")
            else:
                reasons.append("增强版本强度")
        elif version_ratio <= 0.3:
            reasons.append("版本强度偏弱")

    if not reasons and hero_lanes:
        reasons.append(f"{hero_lanes[0]}位可用英雄")
    if not reasons:
        reasons.append(f"当前可用的{cname}" if action == "pick" else "针对性禁用")
    return "；".join(reasons[:2])


def recommend_candidates(
    match: Dict[str, Any],
    league_ctx: Any,
    n: int = 5,
) -> List[Dict[str, Any]]:
    """给当前行动方推荐最多 ``n`` 个候选（``match["available"]`` 内）。

    返回 ``[{"ename": int, "cname": str, "score": float(1 位小数), "reason": str}]``，
    按 ``score`` 降序。``available`` 为空或无任何真实数据支撑时返回 ``[]``。
    """
    available = match.get("available") or []
    if not available:
        return []
    if not bool(getattr(league_ctx, "heroes", None)):
        return []
    side = str(match.get("side") or "blue")
    if side not in ("blue", "red"):
        side = "blue"
    action = str(match.get("action") or "pick")

    scored = ai_coach.evaluate_candidates(match, side, league_ctx, action)
    limit = max(0, int(n))
    output: List[Dict[str, Any]] = []
    for entry in scored[:limit]:
        hero = league_ctx.hero(entry["ename"])
        output.append(
            {
                "ename": entry["ename"],
                "cname": str(hero.get("cname") or entry["ename"]),
                "score": round(float(entry["score"]), 1),
                "reason": _reason_for(entry, league_ctx, match, side),
            }
        )
    return output


def recommend(match: Dict[str, Any], league_ctx: Any, limit: int = 5) -> Optional[Dict[str, Any]]:
    """``/api/bp/score`` 响应体里的 ``recommend`` 段；无候选时返回 ``None``。"""
    candidates = recommend_candidates(match, league_ctx, n=limit)
    if not candidates:
        return None
    side = str(match.get("side") or "blue")
    if side not in ("blue", "red"):
        side = "blue"
    action = str(match.get("action") or "pick")
    return {"side": side, "action": action, "candidates": candidates}


# --------------------------------------------------------------------------- #
# 整局评分
# --------------------------------------------------------------------------- #


def score_match(match: Dict[str, Any], league_ctx: Any) -> Dict[str, Any]:
    """契约 §2 ``/api/bp/score`` 的完整响应体（**不含** ``ok``）。

    返回::

        {"match_id": str,
         "blue": {"score": int, "breakdown": {...}, "notes": [...], "missing": [...]},
         "red":  {...},
         "delta": int,
         "recommend": {"side","action","candidates"} | None}
    """
    picks = match.get("picks") or {}
    blue_picks = [item for item in (picks.get("blue") or []) if isinstance(item, int)]
    red_picks = [item for item in (picks.get("red") or []) if isinstance(item, int)]

    blue = score_lineup(blue_picks, red_picks, league_ctx)
    red = score_lineup(red_picks, blue_picks, league_ctx)

    return {
        "match_id": match.get("match_id"),
        "blue": blue,
        "red": red,
        "delta": int(blue["score"]) - int(red["score"]),
        "recommend": recommend(match, league_ctx),
    }

# --------------------------------------------------------------------------- #
# 可调评分细则（v42）
# --------------------------------------------------------------------------- #
# 参数存在 data/scoring_rules.json，由 game/scoring_rules.py 负责读写与校验。
# 模块加载时应用一次；玩家在「设置 → 评分细则」里改完会立刻再次 apply_rules()，
# 所以这里不做成常量闭包，全部保留模块级变量，保证热更新生效。
try:  # pragma: no cover - 仅在导入期执行
    from . import scoring_rules as _scoring_rules

    _scoring_rules.apply_rules()
except Exception as _exc:  # noqa: BLE001
    print("[scoring] 评分细则加载失败（用内置默认值）：%s" % _exc)


# --------------------------------------------------------------------------- #
# v49：小分详情（结算界面用）
# --------------------------------------------------------------------------- #
# 把「这一侧为什么得这么多分」翻译成玩家看得懂的内容：
#   * components：五项分项各自的得分/上限/一句原因（数值全部来自上一行统计，不编造）
#   * lanes：按 对抗路→打野→中路→发育路→游走 逐条对比双方英雄与该分路的表现
COMPONENT_LABELS = {
    "system": "体系完整度",
    "counter": "对位克制",
    "version": "版本强度",
    "lane": "分路覆盖",
    "weakness": "阵容短板",
}


def _hero_row(hero: Any) -> Dict[str, Any]:
    """英雄的一行展示数据（只取真实字段，缺就是 None）。"""
    hero = hero if isinstance(hero, dict) else {}
    return {
        "ename": hero.get("ename"),
        "cname": hero.get("cname"),
        "win_rate": hero.get("win_rate"),
        "pick_rate": hero.get("pick_rate"),
        "ban_rate": hero.get("ban_rate"),
        "lane_list": list(_hero_lanes(hero)[1]),
        "has_stats": bool(hero.get("has_stats")),
    }


def _counter_between(a: int, b: int, mapping: Dict[int, Dict[str, Any]]) -> Optional[str]:
    """a 与 b 的官方克制关系：``"advantage"`` / ``"disadvantage"`` / ``"even"``；无数据 None。"""
    if not mapping:
        return None
    row_a = mapping.get(a) or {}
    row_b = mapping.get(b) or {}
    if not row_a and not row_b:
        return None
    a_counters = row_a.get("counters") or set()
    b_counters = row_b.get("counters") or set()
    if b in a_counters and a not in b_counters:
        return "advantage"
    if a in b_counters and b not in a_counters:
        return "disadvantage"
    if a in b_counters and b in a_counters:
        return "even"
    return "even" if (a_counters or b_counters) else None


def score_detail(
    picks: Sequence[int],
    enemy_picks: Sequence[int],
    league_ctx: Any,
) -> Dict[str, Any]:
    """生成某一侧的「小分详情」：分项构成 + 五条分路的双方对比。"""
    raw = score_lineup(picks, enemy_picks, league_ctx)
    stats = raw.get("_stats") or {}
    breakdown = raw.get("breakdown") or {}
    caps = raw.get("caps") or {}
    pick_list = [e for e in (picks or []) if isinstance(e, int)]
    enemy_list = [e for e in (enemy_picks or []) if isinstance(e, int)]

    matched = list(stats.get("matched_lanes") or [])
    uncovered = list(stats.get("uncovered_lanes") or [])
    lane_ratio = float(stats.get("lane_ratio") or 0.0)
    stats_count = int(stats.get("stats_count") or 0)
    version_ratio = stats.get("version_ratio")
    weak = list(stats.get("weak_heroes") or [])
    counter_adv = stats.get("counter_adv")
    weights = stats.get("weights") or {}

    components: List[Dict[str, Any]] = []
    for key in ("system", "counter", "version", "lane", "weakness"):
        score = int(breakdown.get(key) or 0)
        cap = int(caps.get(key) or 0)
        ratio = None
        reason = ""
        if key == "system":
            ratio = stats.get("stats_ratio")
            reason = "分路覆盖 %d/5，平均契合 %.0f%%，%d/%d 个英雄有该赛季数据" % (
                len(matched), lane_ratio * 100, stats_count, len(pick_list))
        elif key == "counter":
            if counter_adv is None:
                reason = "所选英雄暂无官方克制数据，该项不参与评分（权重已转移）"
            elif counter_adv > 0:
                reason = "官方克制关系占优：我方压制多 %d 条" % counter_adv
            elif counter_adv < 0:
                reason = "官方克制关系吃亏：对方压制多 %d 条" % (-counter_adv)
            else:
                reason = "官方克制关系基本持平"
            if cap > 0:
                ratio = float(stats.get("counter_ratio") or 0.0)
        elif key == "version":
            ratio = version_ratio
            if version_ratio is None:
                reason = "所选英雄暂无该赛季胜率数据，该项不参与评分（权重已转移）"
            else:
                reason = "阵容平均胜率强度 %.0f%%（该赛季横向比较：0%% 最弱、100%% 最强）" % (
                    float(version_ratio) * 100)
        elif key == "lane":
            ratio = lane_ratio if matched else None
            if not matched:
                reason = "所选英雄没有可用的分路数据"
            else:
                reason = "%d/5 名英雄打在本命分路" % len(matched)
                if uncovered:
                    reason += "，缺 " + "、".join(uncovered)
        else:  # weakness
            ratio = stats.get("weakness_ratio")
            if ratio is None:
                reason = "缺少可判断的分路/胜率数据"
            elif weak:
                reason = "短板：%s 的赛季胜率低于同分路中位数" % "、".join(weak[:2])
            elif uncovered:
                reason = "分路缺口：没有英雄能打 " + "、".join(uncovered)
            else:
                reason = "分路覆盖齐全，所选英雄都不弱于同分路平均水平"
        components.append({
            "key": key,
            "label": COMPONENT_LABELS.get(key, key),
            "score": score,
            "cap": cap,
            "weight": int(round(float(weights.get(key) or 0))),
            "ratio": None if ratio is None else round(float(ratio), 4),
            "reason": reason,
        })

    # ---- 逐分路对比 ----
    mine_slots = _assign_slots(pick_list, league_ctx).get("slots") or {}
    their_slots = _assign_slots(enemy_list, league_ctx).get("slots") or {}
    mapping = counter_map()
    lanes: List[Dict[str, Any]] = []
    for lane in LANES:
        mine_en = pick_list[mine_slots[lane]] if lane in mine_slots else None
        their_en = enemy_list[their_slots[lane]] if lane in their_slots else None
        relation = None
        if isinstance(mine_en, int) and isinstance(their_en, int):
            relation = _counter_between(mine_en, their_en, mapping)
        lanes.append({
            "lane": lane,
            "mine": _hero_row(league_ctx.hero(mine_en)) if isinstance(mine_en, int) else None,
            "theirs": _hero_row(league_ctx.hero(their_en)) if isinstance(their_en, int) else None,
            "relation": relation,
        })

    return {
        "score": int(raw.get("score") or 0),
        "components": components,
        "lanes": lanes,
        "notes": raw.get("notes") or [],
        "missing": raw.get("missing") or [],
    }
