# -*- coding: utf-8 -*-
"""KPL 生涯模式赛制引擎（v3 契约 §1 / §3）。

纯标准库实现，**不 import flask**。所有函数只操作 ``dict``，不做任何文件 IO
（存档读写由 ``app.py`` 负责）。

三种赛制（契约 v3 §1.2）：

* ``league``（KPL 春/夏季赛）
  ``round1`` 分组单循环 BO5 → ``round2`` 重分组单循环 BO5 →
  ``playoff`` 双败淘汰 BO7 → ``final`` 总决赛 BO7
* ``challenger``（挑战者杯）
  ``swiss`` 瑞士轮 BO3 → ``playoff`` 双败淘汰 BO5 → ``final`` 总决赛 BO7
* ``annual``（年度总决赛）
  ``arena`` 擂台赛单循环 BO5 → ``breakout`` 突围赛双败 BO5（取前 4）→
  ``playoff`` 双败淘汰 BO7 → ``final`` 总决赛 BO9

设计要点
--------
* 一切「真实数据」只来自调用方传入的 ``teams``（``data/team_stats_by_league``）。
  本模块**不硬编码**任何队伍数、胜率、赛事 ID。
* 模拟比分按双方该赛季**真实 win_rate** 加权随机生成，属于程序模拟，
  不是真实战绩 —— 结构里保留 ``_sim_note`` 说明。
* 赛程 / 抽签 / 比分全部由本模块产生，前端不得自行生成。
* 双败淘汰赛是**真双败**：任一队输两场即淘汰，胜者组冠军与败者组冠军
  在 ``playoff`` 结束时产生，再由 ``final`` 阶段打一场总决赛。
"""

from __future__ import annotations

import itertools
import math
import random
import re
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CareerError",
    "FORMATS",
    "CATEGORY_OF_FORMAT",
    "STAGE_PLAN",
    "STAGE_KIND_LABEL",
    "SWISS_MAX_ROUNDS",
    "SWISS_WIN_TO_ADVANCE",
    "SWISS_LOSS_TO_ELIMINATE",
    "PLAYOFF_SIZE",
    "TIERS",
    "classify_league",
    "stage_plan",
    "bo_for",
    "event_meta",
    "create_save",
    "draw_groups",
    "build_stage_schedule",
    "stage_complete",
    "next_stage",
    "simulate",
    "apply_result",
    "standings",
    "schedule_view",
    "next_player_match",
    "bracket_view",
    "summary",
]


class CareerError(Exception):
    """生涯模式业务错误（API 层转 409）。"""


# --------------------------------------------------------------------------- #
# 常量（契约 v3 §1.3）
# --------------------------------------------------------------------------- #

FORMATS: Tuple[str, ...] = ("league", "challenger", "annual")

CATEGORY_OF_FORMAT: Dict[str, str] = {
    "league": "league",
    "challenger": "cup",
    "annual": "cup",
}

#: (stage_key, label, kind, bo) —— BO 规格严格按用户需求。
STAGE_PLAN: Dict[str, List[Tuple[str, str, str, int]]] = {
    "league": [
        ("round1", "第一轮循环赛", "group_round_robin", 5),
        ("round2", "第二轮循环赛", "group_round_robin", 5),
        ("playoff", "季后赛", "double_elim", 7),
        ("final", "总决赛", "single", 7),
    ],
    "challenger": [
        ("swiss", "瑞士轮", "swiss", 3),
        ("playoff", "淘汰赛", "double_elim", 5),
        ("final", "总决赛", "single", 7),
    ],
    "annual": [
        ("arena", "擂台赛", "round_robin", 5),
        ("breakout", "突围赛", "double_elim", 5),
        ("playoff", "双败淘汰赛", "double_elim", 7),
        ("final", "总决赛", "single", 9),
    ],
}

STAGE_KIND_LABEL: Dict[str, str] = {
    "group_round_robin": "组内单循环",
    "round_robin": "单循环",
    "swiss": "瑞士轮",
    "double_elim": "双败淘汰",
    "single": "单场决胜",
}

SWISS_MAX_ROUNDS = 5
SWISS_WIN_TO_ADVANCE = 3
SWISS_LOSS_TO_ELIMINATE = 3
PLAYOFF_SIZE = 8

#: 教练等级阈值（契约 v3 §4.3）。按**累计冠军数**从高到低匹配。
TIERS: Tuple[Tuple[int, str], ...] = (
    (10, "传奇教头"),
    (6, "金牌教练"),
    (3, "名帅"),
    (1, "初露锋芒"),
    (0, "无名之辈"),
)

#: 契约 v3 §2.1 要求保留的模拟说明。
SIM_NOTE = "模拟比赛的比分由程序按战队赛季胜率模拟生成，非真实战绩"

#: 联赛分组数量（round1）。
LEAGUE_GROUP_COUNT = 3


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return default
    return default


def _as_num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        if not text:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    return None


def _team_name(team: Any) -> str:
    if isinstance(team, dict):
        name = team.get("name")
        return str(name).strip() if name else ""
    if isinstance(team, str):
        return team.strip()
    return ""


def _uniq(seq: Iterable[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in seq:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _pow2_floor(n: int) -> int:
    """不大于 ``n`` 的最大的 2 的幂（最小 2）。"""
    if n < 2:
        return 0
    return 1 << (int(n).bit_length() - 1)


def _next_pow2(n: int) -> int:
    if n <= 2:
        return 2
    return 1 << (n - 1).bit_length()


def _rng(save: Dict[str, Any]) -> random.Random:
    """抽签/洗牌用的确定性随机源（由 save["seed"] 决定）。"""
    seed = _as_int(save.get("seed"), 0) or 0
    return random.Random(seed)


def _match_rng(save: Dict[str, Any], match_key: str) -> random.Random:
    """每场比赛独立的确定性随机源 —— 与模拟顺序无关，便于复现。"""
    seed = _as_int(save.get("seed"), 0) or 0
    crc = zlib.crc32(str(match_key).encode("utf-8"))
    return random.Random((seed * 1000003) ^ (crc & 0xFFFFFFFF))


def _need_wins(bo: int) -> int:
    return max(1, int(bo) // 2 + 1)


def tier_of(championships: int) -> str:
    """冠军数 → 教练等级（契约 v3 §4.3）。"""
    count = max(0, int(championships or 0))
    for threshold, label in TIERS:
        if count >= threshold:
            return label
    return TIERS[-1][1]


# --------------------------------------------------------------------------- #
# 分类与元信息（契约 v3 §1.1）
# --------------------------------------------------------------------------- #


def classify_league(name: str) -> Tuple[str, str]:
    """按赛事名判断 ``(category, format)``。

    ``data/leagues.json`` 的 ``league_type_name`` 全是 ``"kpl"``，不能用来分类；
    这里按名称关键字判断，顺序即优先级。
    """
    text = str(name or "")
    if "挑战者杯" in text:
        return "cup", "challenger"
    if "年度总决赛" in text:
        return "cup", "annual"
    if "春季赛" in text or "夏季赛" in text:
        return "league", "league"
    return "cup", "challenger"


def stage_plan(fmt: str) -> List[Tuple[str, str, str, int]]:
    plan = STAGE_PLAN.get(fmt)
    if not plan:
        raise CareerError("未知赛制 format=%r（可用：%s）" % (fmt, "/".join(FORMATS)))
    return list(plan)


def bo_for(fmt: str, stage_key: str) -> int:
    for key, _label, _kind, bo in stage_plan(fmt):
        if key == stage_key:
            return int(bo)
    raise CareerError("赛制 %s 没有阶段 %r" % (fmt, stage_key))


def _stage_label(fmt: str, stage_key: str) -> str:
    for key, label, _kind, _bo in stage_plan(fmt):
        if key == stage_key:
            return label
    return ""


def _stage_kind(fmt: str, stage_key: str) -> str:
    for key, _label, kind, _bo in stage_plan(fmt):
        if key == stage_key:
            return kind
    return ""


def short_name_of(name: str) -> str:
    """``2025年KPL夏季赛`` → ``2025 KPL 夏季赛``（契约 v3 §2.2）。"""
    text = str(name or "").strip()
    if not text:
        return ""
    has_kpl = "KPL" in text.upper()
    match = re.match(r"^(\d{4})\s*[年]?\s*(?:KPL)?\s*(?:王者荣耀)?\s*(.+)$", text, re.I)
    if match:
        year, rest = match.group(1), match.group(2).strip()
        rest = rest.replace("年年度", "年度").strip()
        parts = [year]
        if has_kpl and not rest.upper().startswith("KPL"):
            parts.append("KPL")
        if rest:
            parts.append(rest)
        return " ".join(parts)
    cleaned = text.replace("KPL", " KPL ").replace("王者荣耀", " ")
    parts = [p for p in cleaned.split() if p]
    return " ".join(parts) if parts else text


def format_desc(fmt: str, team_count: int) -> str:
    plan = stage_plan(fmt)
    pieces = ["%d 支战队" % team_count]
    for key, label, _kind, bo in plan:
        pieces.append("%s BO%d" % (label, bo))
    return " · ".join(pieces)


def event_meta(league: Dict[str, Any], team_count: int) -> Dict[str, Any]:
    """由 ``leagues.json`` 的一条记录生成 ``save["event"]``。"""
    name = str(league.get("name") or "").strip()
    category, fmt = classify_league(name)
    note = None
    if not ("挑战者杯" in name or "年度总决赛" in name
            or "春季赛" in name or "夏季赛" in name):
        note = "赛事名无法识别，赛制按挑战者杯处理"
    return {
        "league_id": str(league.get("league_id") or "").strip(),
        "name": name,
        "category": category,
        "format": fmt,
        "short_name": short_name_of(name),
        "team_count": int(team_count),
        "format_note": note,
    }


# --------------------------------------------------------------------------- #
# 创建存档（契约 v3 §3 create_save）
# --------------------------------------------------------------------------- #


def _normalize_pool(teams: Sequence[Any]) -> List[Dict[str, Any]]:
    pool: List[Dict[str, Any]] = []
    for team in teams or []:
        name = _team_name(team)
        if not name:
            continue
        item = {
            "name": name,
            "team_id": (str(team.get("team_id")) if isinstance(team, dict)
                        and team.get("team_id") is not None else None),
            "rank": _as_int(team.get("rank")) if isinstance(team, dict) else None,
            "win_rate": _as_num(team.get("win_rate")) if isinstance(team, dict) else None,
            "blue_win_rate": (_as_num(team.get("blue_win_rate"))
                              if isinstance(team, dict) else None),
            "red_win_rate": (_as_num(team.get("red_win_rate"))
                             if isinstance(team, dict) else None),
            "logo_path": (team.get("logo_path") if isinstance(team, dict) else None),
            "abbreviation": (team.get("abbreviation") if isinstance(team, dict) else None),
            "matches": _as_int(team.get("matches")) if isinstance(team, dict) else None,
            "wins": _as_int(team.get("wins")) if isinstance(team, dict) else None,
            "losses": _as_int(team.get("losses")) if isinstance(team, dict) else None,
        }
        pool.append(item)
    # 稳定排序：真实 rank 升序，缺 rank 的按名字排在后面
    pool.sort(key=lambda t: (t.get("rank") is None, t.get("rank") or 0, t.get("name") or ""))
    return pool


def _coach_dict(coach: Any) -> Dict[str, Any]:
    data = coach if isinstance(coach, dict) else {}
    name = str(data.get("team_name") or "").strip()
    if not name:
        raise CareerError("缺少战队名 team_name")
    if len(name) > 8:
        raise CareerError("战队名最多 8 个字符，收到 %d 个：%s" % (len(name), name))
    return {
        "name": str(data.get("name") or "教练").strip() or "教练",
        "team_name": name,
        "team_is_custom": bool(data.get("team_is_custom")),
        "team_logo": data.get("team_logo"),
        "team_id": (str(data.get("team_id")) if data.get("team_id") is not None else None),
    }


def create_save(
    league: Dict[str, Any],
    teams: Sequence[Any],
    coach: Any,
    *,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """创建生涯存档骨架（尚未抽签）。结构见契约 v3 §2.1。"""
    pool = _normalize_pool(teams)
    if len(pool) < 4:
        raise CareerError("该赛事战队数不足（%d 支），无法开启生涯" % len(pool))

    coach_dict = _coach_dict(coach)
    event = event_meta(league if isinstance(league, dict) else {}, len(pool))
    if not event["league_id"]:
        raise CareerError("缺少 league_id，无法开启生涯")

    fmt = event["format"]
    plan = stage_plan(fmt)
    first_key, first_label, first_kind, _bo = plan[0]

    if seed is None:
        seed = int.from_bytes(zlib.crc32((event["league_id"] + coach_dict["team_name"]).encode()) and
                              zlib.crc32((event["league_id"] + coach_dict["team_name"]).encode()).to_bytes(4, "big"),
                              "big")

    save: Dict[str, Any] = {
        "save_id": None,  # 由 API 层生成
        "version": 3,
        "created_at": None,
        "updated_at": None,
        "seed": int(seed),
        "event": event,
        "coach": coach_dict,
        "pool": pool,
        "stage": first_key,
        "stage_index": 0,
        "stage_label": first_label,
        "stage_kind": first_kind,
        "stage_done": False,
        "status": "running",
        "groups": [],
        "draw_order": [],
        "swiss": None,
        "bracket": None,
        "schedule": [],
        "current_day": 1,
        "total_days": 0,
        "stats": {
            "matched": 0,
            "wins": 0,
            "losses": 0,
            "game_wins": 0,
            "game_losses": 0,
            "championship": 0,
        },
        "_match_seq": 0,
        "_sim_note": SIM_NOTE,
        "history": [],
    }
    return save


def _pool_of(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    """取战队池：显式传入优先，否则用存档里冻结的 pool。"""
    if teams:
        pool = _normalize_pool(teams)
        if pool:
            return pool
    return list(save.get("pool") or [])


def _pool_names(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None) -> List[str]:
    return [t["name"] for t in _pool_of(save, teams)]


def _team_meta(save: Dict[str, Any], name: str) -> Dict[str, Any]:
    for team in save.get("pool") or []:
        if team.get("name") == name:
            return team
    return {"name": name}


# --------------------------------------------------------------------------- #
# 赛程：比赛对象 & 比赛日排期（契约 v3 §2.3）
# --------------------------------------------------------------------------- #


def _new_match_key(save: Dict[str, Any]) -> str:
    seq = _as_int(save.get("_match_seq"), 0) or 0
    seq += 1
    save["_match_seq"] = seq
    return "m%04d" % seq


def _make_match(
    save: Dict[str, Any],
    *,
    stage_key: str,
    stage_label: str,
    kind: str,
    bo: int,
    blue_team: str,
    red_team: str,
    group: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    coach_team = (save.get("coach") or {}).get("team_name")
    match = {
        "match_key": _new_match_key(save),
        "day": 1,
        "stage": stage_key,
        "stage_label": stage_label,
        "group": group,
        "kind": kind,
        "bo": int(bo),
        "blue_team": blue_team,
        "red_team": red_team,
        "status": "pending",
        "score": None,
        "winner": None,
        "winner_team": None,
        "is_mine": bool(coach_team in (blue_team, red_team)),
        "bp_match_id": None,
        "result_source": None,
        "player_side": ("blue" if coach_team == blue_team else
                        ("red" if coach_team == red_team else None)),
    }
    if extra:
        match.update(extra)
    return match


def _assign_days(matches: Sequence[Dict[str, Any]], start_day: int = 1) -> int:
    """贪心排期：同一队一天最多打一场（契约 v3 §2.3）。返回最后一天。"""
    day = int(start_day)
    remaining = list(matches)
    while remaining:
        today: set = set()
        rest: List[Dict[str, Any]] = []
        for match in remaining:
            blue = match.get("blue_team")
            red = match.get("red_team")
            if blue in today or red in today:
                rest.append(match)
                continue
            today.add(blue)
            today.add(red)
            match["day"] = day
        if not today:  # 理论上不可达（第一条比赛总能排进空的一天）
            fallen = rest.pop(0)
            fallen["day"] = day
            today.add(fallen.get("blue_team"))
            today.add(fallen.get("red_team"))
        remaining = rest
        day += 1
    return day - 1


def _refresh_days(save: Dict[str, Any]) -> None:
    """重算 schedule 的 day / total_days / current_day（契约 v3 §2.3 / §2.4）。"""
    schedule = save.get("schedule") or []
    if not schedule:
        save["total_days"] = 0
        save["current_day"] = 0
        return
    save["total_days"] = max(_as_int(m.get("day"), 1) or 1 for m in schedule)
    pending = [m for m in schedule if m.get("status") == "pending"]
    if pending:
        save["current_day"] = min(_as_int(m.get("day"), 1) or 1 for m in pending)
    else:
        save["current_day"] = save["total_days"]


def _append_matches(save: Dict[str, Any], matches: Sequence[Dict[str, Any]]) -> None:
    """把新比赛按「同队一天最多一场」追加排期，并保持既有比赛的日子不变。"""
    if not matches:
        return
    schedule = save.setdefault("schedule", [])
    last_day = max((_as_int(m.get("day"), 1) or 1) for m in schedule) if schedule else 0
    new_last = _assign_days(matches, start_day=last_day + 1 if schedule else 1)
    for match in matches:
        # 阶段内相对日子压平到「紧接着已有赛程」的连续区间
        match["day"] = match["day"] - (last_day + 1 if schedule else 1) + (last_day + 1)
        schedule.append(match)
    del new_last
    _refresh_days(save)


# --------------------------------------------------------------------------- #
# 抽签
# --------------------------------------------------------------------------- #


def _snake_groups(names: Sequence[str], count: int) -> List[List[str]]:
    """蛇形分组（1→N→N→1）：按真实排名轮流放，保证各组实力均衡。

    18 支 / 3 组 → 每组 6 支；各组人数最多相差 1。
    """
    count = max(1, int(count))
    buckets: List[List[str]] = [[] for _ in range(count)]
    for idx, name in enumerate(names):
        round_no = idx // count
        pos = idx % count
        if round_no % 2 == 0:
            buckets[pos].append(name)
        else:
            buckets[count - 1 - pos].append(name)
    return buckets


def draw_groups(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None,
                *, seeded: bool = True) -> Dict[str, Any]:
    """抽签并生成本阶段赛程（契约 v3 §3 draw_groups）。

    * ``league``：按真实 rank 蛇形分 3 组，生成 ``groups`` + ``draw_order``；
    * 其它赛制：无分组，``draw_order`` 为种子序（用于抽签页展示）。
    """
    if save.get("status") != "running":
        raise CareerError("该生涯已结束，无法抽签")
    if save.get("schedule"):
        raise CareerError("该存档已完成抽签")

    fmt = (save.get("event") or {}).get("format")
    plan = stage_plan(fmt)
    stage_key = save.get("stage") or plan[0][0]
    if stage_key != plan[0][0]:
        raise CareerError("只能在第一个阶段抽签（当前阶段 %s）" % stage_key)

    names = _pool_names(save, teams)
    if len(names) < 4:
        raise CareerError("战队数不足，无法抽签")

    rng = _rng(save)
    ranked = list(names) if seeded else list(names)
    if not seeded:
        rng.shuffle(ranked)

    groups: List[Dict[str, Any]] = []
    draw_order: List[Dict[str, Any]] = []
    if fmt == "league":
        buckets = _snake_groups(ranked, LEAGUE_GROUP_COUNT)
        group_names = ["GROUP %d" % (i + 1) for i in range(LEAGUE_GROUP_COUNT)]
        for idx, bucket in enumerate(buckets):
            groups.append({
                "key": "G%d" % (idx + 1),
                "name": group_names[idx],
                "teams": list(bucket),
            })
        # 抽签揭示顺序：随机打散，但记录其所属组
        flat = [(group_names[i], name) for i, bucket in enumerate(buckets) for name in bucket]
        rng.shuffle(flat)
        draw_order = [{"team": name, "group": group_name, "label": group_name}
                      for group_name, name in flat]
    else:
        draw_order = [{"team": name, "group": None, "label": "种子 %d" % (i + 1)}
                      for i, name in enumerate(ranked)]

    save["groups"] = groups
    save["draw_order"] = draw_order

    build_stage_schedule(save, teams)
    return save


# --------------------------------------------------------------------------- #
# 阶段赛程生成
# --------------------------------------------------------------------------- #


def build_stage_schedule(save: Dict[str, Any],
                         teams: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """为当前阶段生成比赛并追加到 schedule（契约 v3 §3）。"""
    fmt = (save.get("event") or {}).get("format")
    stage_key = save.get("stage")
    plan = stage_plan(fmt)
    if stage_key not in [p[0] for p in plan]:
        raise CareerError("当前阶段 %r 不属于赛制 %s" % (stage_key, fmt))

    label = _stage_label(fmt, stage_key)
    kind = _stage_kind(fmt, stage_key)
    bo = bo_for(fmt, stage_key)

    if kind == "group_round_robin":
        matches = _build_group_round_robin(save, stage_key, label, bo)
    elif kind == "round_robin":
        matches = _build_round_robin(save, teams, stage_key, label, bo)
    elif kind == "swiss":
        _init_swiss(save, teams)
        matches = _swiss_open_round(save)
    elif kind == "double_elim":
        matches = _build_double_elim_stage(save, teams, stage_key, label, kind, bo)
    elif kind == "single":
        matches = _build_final_stage(save, stage_key, label, bo)
    else:  # pragma: no cover - 防御
        raise CareerError("未知阶段类型 %r" % kind)

    _append_matches(save, matches)
    save["stage_kind"] = kind
    save["stage_label"] = label
    return save


def _build_group_round_robin(save: Dict[str, Any], stage_key: str,
                             label: str, bo: int) -> List[Dict[str, Any]]:
    groups = save.get("groups") or []
    if not groups:
        raise CareerError("还没有分组，无法生成 %s 赛程" % label)
    matches: List[Dict[str, Any]] = []
    for group in groups:
        teams = list(group.get("teams") or [])
        for a, b in itertools.combinations(teams, 2):
            matches.append(_make_match(
                save, stage_key=stage_key, stage_label=label,
                kind="group_round_robin", bo=bo,
                blue_team=a, red_team=b, group=group.get("key"),
            ))
    return matches


def _build_round_robin(save: Dict[str, Any], teams: Optional[Sequence[Any]],
                       stage_key: str, label: str, bo: int) -> List[Dict[str, Any]]:
    names = _pool_names(save, teams)
    matches = [
        _make_match(save, stage_key=stage_key, stage_label=label,
                    kind="round_robin", bo=bo, blue_team=a, red_team=b)
        for a, b in itertools.combinations(names, 2)
    ]
    return matches


# --------------------------------------------------------------------------- #
# 瑞士轮（契约 v3 §1.2 challenger）
# --------------------------------------------------------------------------- #


def _init_swiss(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None) -> None:
    swiss = save.get("swiss")
    if isinstance(swiss, dict) and swiss.get("records"):
        return
    names = _pool_names(save, teams)
    save["swiss"] = {
        "round": 0,
        "max_rounds": SWISS_MAX_ROUNDS,
        "records": {name: {"wins": 0, "losses": 0, "bye": 0} for name in names},
        "pair_history": [],
        "advancers": [],
        "eliminated": [],
        "round_matches": {},  # 轮次 → [match_key]
    }


def _swiss_alive(save: Dict[str, Any]) -> List[str]:
    swiss = save.get("swiss") or {}
    records = swiss.get("records") or {}
    adv = set(swiss.get("advancers") or [])
    elim = set(swiss.get("eliminated") or [])
    alive = []
    for name, rec in records.items():
        if name in adv or name in elim:
            continue
        wins = _as_int(rec.get("wins"), 0) or 0
        losses = _as_int(rec.get("losses"), 0) or 0
        if wins < SWISS_WIN_TO_ADVANCE and losses < SWISS_LOSS_TO_ELIMINATE:
            alive.append(name)
    return alive


def _swiss_buckets(alive: Sequence[str], records: Dict[str, Any]) -> Dict[Tuple[int, int], List[str]]:
    buckets: Dict[Tuple[int, int], List[str]] = {}
    for name in alive:
        rec = records.get(name) or {}
        key = (_as_int(rec.get("wins"), 0) or 0, _as_int(rec.get("losses"), 0) or 0)
        buckets.setdefault(key, []).append(name)
    return buckets


def _pair_score(save: Dict[str, Any]) -> Dict[str, int]:
    pool = {t["name"]: (_as_int(t.get("rank")) or 999) for t in save.get("pool") or []}
    return pool


def _swiss_pair_bucket(save: Dict[str, Any], names: Sequence[str]) -> List[Tuple[str, str]]:
    """同分桶内配对，尽量避开已交手组合。"""
    swiss = save.get("swiss") or {}
    history = swiss.get("pair_history") or []
    played = {tuple(sorted(pair)) for pair in history if isinstance(pair, (list, tuple))}
    remaining = list(names)
    pairs: List[Tuple[str, str]] = []
    while len(remaining) >= 2:
        first = remaining.pop(0)
        partner = None
        for idx, cand in enumerate(remaining):
            key = tuple(sorted((first, cand)))
            if key not in played:
                partner = cand
                remaining.pop(idx)
                break
        if partner is None:  # 全部打过 → 允许重复，取第一个
            partner = remaining.pop(0)
        pairs.append((first, partner))
    return pairs


def _swiss_open_round(save: Dict[str, Any]) -> List[Dict[str, Any]]:
    """生成瑞士轮下一轮的比赛（若该轮已生成则只返回新增部分）。"""
    swiss = save.get("swiss") or {}
    if not swiss:
        raise CareerError("瑞士轮尚未初始化")
    fmt = (save.get("event") or {}).get("format")
    label = _stage_label(fmt, "swiss")
    bo = bo_for(fmt, "swiss")
    records = swiss.get("records") or {}
    rng = _rng(save)

    alive = _swiss_alive(save)
    if not alive:
        return []

    # 出线名额已够 → 不再开新一轮
    if len(swiss.get("advancers") or []) >= PLAYOFF_SIZE:
        return []
    if (swiss.get("round") or 0) >= SWISS_MAX_ROUNDS:
        return []

    swiss["round"] = (_as_int(swiss.get("round"), 0) or 0) + 1
    round_no = int(swiss["round"])

    ordered_alive = list(alive)
    rng.shuffle(ordered_alive)

    # 同分优先匹配：先按 (wins, losses) 分桶，逐桶配对；
    # 桶内人数为奇数时让一人顺延到下一桶（"carry-down"）。
    buckets = _swiss_buckets(ordered_alive, records)
    keys = sorted(buckets.keys(), key=lambda k: (-k[1], -k[0]))
    carry: Optional[str] = None
    pairs: List[Tuple[str, str]] = []
    byes: List[str] = []

    for key in keys:
        group = list(buckets[key])
        rng.shuffle(group)
        if carry:
            group.insert(0, carry)
            carry = None
        pairs.extend(_swiss_pair_bucket(save, group))
        leftover = _pair_score(save)  # noqa: F841  (占位，保持结构清晰)
        # _swiss_pair_bucket 已经消费偶数个，剩余奇数个由内部保留最后一人
        consumed = len(pairs) * 2
        del consumed, leftover

    # 重新计算谁还没配对
    paired: set = set()
    for a, b in pairs:
        paired.add(a)
        paired.add(b)
    unpaired = [name for name in ordered_alive if name not in paired]
    if len(unpaired) == 1:
        byes.append(unpaired[0])
    elif len(unpaired) > 1:
        pairs.extend(_swiss_pair_bucket(save, unpaired))

    matches: List[Dict[str, Any]] = []
    round_keys: List[str] = []
    for a, b in pairs:
        if a == b:
            continue
        history = swiss.setdefault("pair_history", [])
        history.append([a, b])
        match = _make_match(
            save, stage_key="swiss", stage_label=label, kind="swiss", bo=bo,
            blue_team=a, red_team=b,
            extra={"swiss_round": round_no},
        )
        matches.append(match)
        round_keys.append(match["match_key"])

    for name in byes:
        rec = records.setdefault(name, {"wins": 0, "losses": 0, "bye": 0})
        rec["bye"] = (_as_int(rec.get("bye"), 0) or 0) + 1
        rec["wins"] = (_as_int(rec.get("wins"), 0) or 0) + 1

    swiss.setdefault("round_matches", {})[str(round_no)] = round_keys

    # BYE 可能直接让人达标
    for name in byes:
        _swiss_check_team(save, name)

    return matches


def _swiss_check_team(save: Dict[str, Any], name: str) -> None:
    swiss = save.setdefault("swiss", {})
    records = swiss.get("records") or {}
    rec = records.get(name)
    if not rec:
        return
    wins = _as_int(rec.get("wins"), 0) or 0
    losses = _as_int(rec.get("losses"), 0) or 0
    adv = swiss.setdefault("advancers", [])
    elim = swiss.setdefault("eliminated", [])
    if wins >= SWISS_WIN_TO_ADVANCE and name not in adv:
        adv.append(name)
    elif losses >= SWISS_LOSS_TO_ELIMINATE and name not in elim:
        elim.append(name)


def _swiss_record_result(save: Dict[str, Any], match: Dict[str, Any]) -> None:
    swiss = save.get("swiss") or {}
    records = swiss.get("records") or {}
    winner_team = match.get("winner_team")
    loser_team = (match.get("red_team") if winner_team == match.get("blue_team")
                  else match.get("blue_team"))
    for name, delta in ((winner_team, 1), (loser_team, 0)):
        rec = records.setdefault(name, {"wins": 0, "losses": 0, "bye": 0})
        if delta:
            rec["wins"] = (_as_int(rec.get("wins"), 0) or 0) + 1
        else:
            rec["losses"] = (_as_int(rec.get("losses"), 0) or 0) + 1
    if winner_team:
        _swiss_check_team(save, winner_team)
    if loser_team:
        _swiss_check_team(save, loser_team)


# --------------------------------------------------------------------------- #
# 双败淘汰赛（契约 v3 §1.2 / §2.1）
# --------------------------------------------------------------------------- #

_WB_NAME = {
    1: "胜者组第一轮",
    2: "胜者组八强",
    3: "胜者组半决赛",
    4: "胜者组决赛",
    5: "胜者组第五轮",
}


def _wb_round_name(i: int, k: int) -> str:
    """胜者组轮次名：按剩余队数命名（16强/8强/半决赛/决赛…）。"""
    if i == k:
        return "胜者组决赛"
    if i == k - 1:
        return "胜者组半决赛"
    remaining = 1 << (k - i + 1)
    return "胜者组 %d 强" % remaining


def _lb_round_name(j: int, total: int) -> str:
    if j == total:
        return "败者组决赛"
    if j == total - 1:
        return "败者组半决赛"
    return "败者组第 %d 轮" % j


def _slot_ref(round_key: str, slot: int, part: str) -> str:
    """``src`` 引用格式：``"w_r1#3:win"`` / ``"w_r2#1:lose"``。"""
    return "%s#%d:%s" % (round_key, slot, part)


def _slot_key(bracket: Dict[str, Any], round_key: str, slot: int) -> str:
    """赛程里的 ``slot_ref``：带阶段前缀，保证跨阶段唯一。"""
    stage = bracket.get("stage") or "?"
    return "%s|%s#%d" % (stage, round_key, slot)


def _build_bracket(
    save: Dict[str, Any],
    teams: Sequence[str],
    *,
    stage_key: str,
    stage_label: str,
    bo: int,
    stop_remaining: Optional[int] = None,
    name_prefix: str = "",
) -> Dict[str, Any]:
    """构造双败淘汰赛骨架（不产生比赛；比赛由 ``_sync_bracket`` 按可打状态生成）。"""
    seeds = [t for t in teams if t]
    n = len(seeds)
    if n < 2:
        raise CareerError("双败淘汰赛至少需要 2 支战队，收到 %d" % n)
    size = _next_pow2(n)
    k = int(math.log2(size))

    rng = _rng(save)
    if n < size:
        # 补齐轮空位（None），按种子序 1..n + None
        padded: List[Optional[str]] = list(seeds) + [None] * (size - n)
    else:
        padded = list(seeds)
    del rng

    rounds: List[Dict[str, Any]] = []

    # --- 胜者组 ---
    for i in range(1, k + 1):
        count = size // (1 << i)
        slots = []
        for s in range(1, count + 1):
            slots.append({
                "slot": s,
                "team": None,
                "team_b": None,
                "bo": bo,
                "match_key": None,
                "winner": None,
                "loser": None,
                "score": None,
                "status": "pending",
                "src_a": None,
                "src_b": None,
                "walkover": False,
            })
        rounds.append({
            "key": "w_r%d" % i,
            "name": _wb_round_name(i, k),
            "side": "winners",
            "windex": i,
            "slots": slots,
        })

    # --- 败者组 ---
    lb_total = 2 * (k - 1)
    for j in range(1, lb_total + 1):
        # 败者组第 j 轮场次数 = size / 2^(floor((j+1)/2) + 1)
        # 8 队 → 2,2,1,1（共 6 场）；16 队 → 4,4,2,2,1,1（共 14 场）
        count = size // (1 << ((j + 1) // 2 + 1))
        slots = []
        for s in range(1, count + 1):
            slots.append({
                "slot": s,
                "team": None,
                "team_b": None,
                "bo": bo,
                "match_key": None,
                "winner": None,
                "loser": None,
                "score": None,
                "status": "pending",
                "src_a": None,
                "src_b": None,
                "walkover": False,
            })
        rounds.append({
            "key": "l_r%d" % j,
            "name": _lb_round_name(j, lb_total),
            "side": "losers",
            "lindex": j,
            "slots": slots,
        })

    by_key = {r["key"]: r for r in rounds}

    # --- 胜者组第一轮：种子配对 + 轮空 ---
    w1 = by_key["w_r1"]["slots"]
    for idx in range(len(w1)):
        a = padded[idx]
        b = padded[size - 1 - idx]
        w1[idx]["team"] = a
        w1[idx]["team_b"] = b

    # --- 胜者组后续轮：winner 进下一轮 ---
    for i in range(1, k):
        cur = by_key["w_r%d" % i]["slots"]
        nxt = by_key["w_r%d" % (i + 1)]["slots"]
        for idx, slot in enumerate(cur):
            target = nxt[idx // 2]
            slot["next_win"] = _slot_ref("w_r%d" % (i + 1), target["slot"], "win")
            if idx % 2 == 0:
                target["src_a"] = _slot_ref("w_r%d" % i, slot["slot"], "win")
            else:
                target["src_b"] = _slot_ref("w_r%d" % i, slot["slot"], "win")

    # 胜者组决赛 winner → 胜者组冠军
    wf = by_key["w_r%d" % k]["slots"][0]
    wf["next_win"] = "winners_champion"

    # --- 败者组第一轮：胜者组第一轮败者两两配对 ---
    l1 = by_key["l_r1"]["slots"]
    for idx, slot in enumerate(l1):
        slot["src_a"] = _slot_ref("w_r1", idx * 2 + 1, "lose")
        slot["src_b"] = _slot_ref("w_r1", idx * 2 + 2, "lose")

    # --- 败者组后续轮 ---
    for j in range(2, lb_total + 1):
        cur = by_key["l_r%d" % j]["slots"]
        prev = by_key["l_r%d" % (j - 1)]["slots"]
        if j % 2 == 0:
            wb_round = j // 2 + 1
            wb_slots = by_key["w_r%d" % wb_round]["slots"]
            for idx, slot in enumerate(cur):
                slot["src_a"] = _slot_ref("l_r%d" % (j - 1), prev[idx]["slot"], "win")
                slot["src_b"] = _slot_ref("w_r%d" % wb_round, wb_slots[idx]["slot"], "lose")
        else:
            for idx, slot in enumerate(cur):
                slot["src_a"] = _slot_ref("l_r%d" % (j - 1), idx * 2 + 1, "win")
                slot["src_b"] = _slot_ref("l_r%d" % (j - 1), idx * 2 + 2, "win")

    # 败者组下一轮 winner 指针
    for j in range(1, lb_total):
        cur = by_key["l_r%d" % j]["slots"]
        nxt = by_key["l_r%d" % (j + 1)]["slots"]
        for idx, slot in enumerate(cur):
            # 奇数轮(1,3,5…)1:1 进下一轮；偶数轮(2,4,6…)两场胜者合并进一个槽位
            if j % 2 == 1:
                target = nxt[idx]
            else:
                target = nxt[idx // 2]
            slot["next_win"] = _slot_ref("l_r%d" % (j + 1), target["slot"], "win")

    lf = by_key["l_r%d" % lb_total]["slots"][0]
    lf["next_win"] = "losers_champion"

    bracket = {
        "kind": "double_elim",
        "stage": stage_key,
        "stage_label": stage_label,
        "bo": bo,
        "size": size,
        "seeds": padded,
        "rounds": rounds,
        "eliminated": [],
        "winners_champion": None,
        "losers_champion": None,
        "champion": None,
        "runner_up": None,
        "qualifiers": [],
        "stop_remaining": stop_remaining,
        "name_prefix": name_prefix,
    }
    return bracket


def _all_slots(bracket: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            yield slot


def _slot_index(bracket: Dict[str, Any]) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    """``"w_r1#2"`` → (round, slot)。"""
    index: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            index["%s#%d" % (round_["key"], slot["slot"])] = (round_, slot)
    return index


def _resolve_ref(index: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]],
                 ref: Optional[str]) -> Tuple[Optional[str], bool]:
    """解析 src 引用 → (队名, 是否已确定)。未打完返回 (None, False)。"""
    if not ref:
        return None, True
    if ref == "bye":
        return None, True
    if ":" not in ref:
        return None, False
    target, part = ref.rsplit(":", 1)
    pair = index.get(target)
    if not pair:
        return None, False
    _round, slot = pair
    if slot.get("walkover") and slot.get("status") == "done":
        value = slot.get("winner") if part == "win" else slot.get("loser")
        return value, True
    if slot.get("status") != "done":
        return None, False
    value = slot.get("winner") if part == "win" else slot.get("loser")
    return value, True


def _propagate_bracket(bracket: Dict[str, Any]) -> None:
    """把已决出的胜负向前传播，并把轮空自动判胜。"""
    index = _slot_index(bracket)
    for _ in range(8):  # 轮空链式传播，迭代到稳定
        changed = False
        for round_ in bracket.get("rounds") or []:
            for slot in round_.get("slots") or []:
                if slot.get("status") == "done":
                    continue
                a, a_ok = _resolve_ref(index, slot.get("src_a"))
                b, b_ok = _resolve_ref(index, slot.get("src_b"))
                if slot.get("src_a") is None and slot.get("team") is not None:
                    a, a_ok = slot.get("team"), True
                if slot.get("src_b") is None and slot.get("team_b") is not None:
                    b, b_ok = slot.get("team_b"), True
                if a_ok and b_ok and not a and not b:
                    if slot.get("status") != "done":
                        slot["status"] = "done"
                        slot["walkover"] = True
                        changed = True
                    continue
                if a_ok and b_ok and (a or b):
                    if slot.get("team") != a:
                        slot["team"] = a
                        changed = True
                    if slot.get("team_b") != b:
                        slot["team_b"] = b
                        changed = True
                    if (a and not b) or (b and not a):
                        # 轮空：直接判胜，不产生比赛
                        slot["status"] = "done"
                        slot["walkover"] = True
                        slot["winner"] = a or b
                        slot["loser"] = b or a
                        slot["score"] = {"blue": 0, "red": 0}
                        slot["winner_team"] = a or b
                        slot["loser_team"] = b or a
                        changed = True
        if not changed:
            break

    # 冠军指针
    wf = _slot_by_ref(index, "w_r%d" % _wb_round_count(bracket))
    if wf is not None and wf.get("status") == "done" and not bracket.get("winners_champion"):
        bracket["winners_champion"] = wf.get("winner")
    lf = _slot_by_ref(index, "l_r%d" % _lb_round_count(bracket))
    if lf is not None and lf.get("status") == "done" and not bracket.get("losers_champion"):
        bracket["losers_champion"] = lf.get("winner")

    # 淘汰名单：双败语义 = 累计输掉 2 场的队伍出局
    losses: Dict[str, int] = {}
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            if slot.get("status") != "done":
                continue
            loser = slot.get("loser")
            if loser:
                losses[loser] = losses.get(loser, 0) + 1
    bracket["losses"] = losses
    bracket["eliminated"] = [name for name, cnt in losses.items() if cnt >= 2]


def _slot_by_ref(index: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]],
                 round_key: str) -> Optional[Dict[str, Any]]:
    for key, (_round, slot) in index.items():
        if key.startswith(round_key + "#"):
            return slot
    return None


def _wb_round_count(bracket: Dict[str, Any]) -> int:
    return sum(1 for r in bracket.get("rounds") or [] if r.get("side") == "winners")


def _lb_round_count(bracket: Dict[str, Any]) -> int:
    return sum(1 for r in bracket.get("rounds") or [] if r.get("side") == "losers")


def _remaining_teams(bracket: Dict[str, Any]) -> int:
    total = len([t for t in (bracket.get("seeds") or []) if t])
    return total - len(bracket.get("eliminated") or [])


def _bracket_ready_slots(bracket: Dict[str, Any]) -> List[Dict[str, Any]]:
    """可打且还没生成比赛 / 还没打完的槽位。"""
    _propagate_bracket(bracket)
    stop = bracket.get("stop_remaining")
    if isinstance(stop, int) and _remaining_teams(bracket) <= stop:
        return []
    ready = []
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            if slot.get("status") != "pending":
                continue
            if slot.get("walkover"):
                continue
            if slot.get("team") and slot.get("team_b"):
                ready.append(slot)
    return ready


def _build_double_elim_stage(save: Dict[str, Any], teams: Optional[Sequence[Any]],
                             stage_key: str, label: str, kind: str,
                             bo: int) -> List[Dict[str, Any]]:
    if stage_key == "playoff":
        names = _playoff_participants(save, teams)
        stop = None
    elif stage_key == "breakout":
        names = _breakout_participants(save, teams)
        stop = 4
    else:  # pragma: no cover
        names = _pool_names(save, teams)
        stop = None

    if len(names) < 2:
        raise CareerError("%s 可用战队不足（%d 支）" % (label, len(names)))

    # 双败淘汰赛的参赛队数必须是 2 的幂（否则轮空会让「两败淘汰」语义失真）。
    # 实际数据里季后赛恒为 8 支（PLAYOFF_SIZE），这里做一次兜底收敛。
    limit = _pow2_floor(len(names))
    if limit < 2:
        raise CareerError("%s 可用战队不足（%d 支）" % (label, len(names)))
    if limit < len(names):
        names = list(names)[:limit]

    bracket = _build_bracket(
        save, names, stage_key=stage_key, stage_label=label, bo=bo,
        stop_remaining=stop,
    )
    save["bracket"] = bracket
    return _sync_bracket_matches(save, bracket, append=False)


def _sync_bracket_matches(save: Dict[str, Any],
                          bracket: Dict[str, Any],
                          *, append: bool = True) -> List[Dict[str, Any]]:
    """把「双方已确定但还没生成比赛」的槽位转成 schedule 里的比赛。

    ``append=False`` 时只创建比赛对象、不写进 schedule（由调用方统一追加），
    避免 ``build_stage_schedule`` 里重复排期。
    """
    created: List[Dict[str, Any]] = []
    existing = {(m.get("slot_ref")): m for m in save.get("schedule") or []
                if m.get("slot_ref")}
    for slot in _bracket_ready_slots(bracket):
        ref = _slot_key(bracket, _round_key_of(bracket, slot), slot["slot"])
        if ref in existing:
            continue
        round_ = _round_of(bracket, slot)
        match = _make_match(
            save,
            stage_key=bracket.get("stage") or save.get("stage"),
            stage_label=bracket.get("stage_label") or save.get("stage_label"),
            kind="double_elim",
            bo=int(slot.get("bo") or bracket.get("bo") or 5),
            blue_team=slot["team"],
            red_team=slot["team_b"],
            extra={
                "slot_ref": ref,
                "bracket_round": round_.get("key"),
                "bracket_round_name": round_.get("name"),
                "bracket_side": round_.get("side"),
            },
        )
        slot["match_key"] = match["match_key"]
        created.append(match)
    if created and append:
        _append_matches(save, created)
    return created


def _round_of(bracket: Dict[str, Any], slot: Dict[str, Any]) -> Dict[str, Any]:
    for round_ in bracket.get("rounds") or []:
        if slot in (round_.get("slots") or []):
            return round_
    return {}


def _round_key_of(bracket: Dict[str, Any], slot: Dict[str, Any]) -> str:
    return _round_of(bracket, slot).get("key") or "?"


def _playoff_participants(save: Dict[str, Any],
                          teams: Optional[Sequence[Any]]) -> List[str]:
    """按上一阶段结果算出 playoff 的参赛队（契约 v3 §1.2）。"""
    fmt = (save.get("event") or {}).get("format")
    names: List[str] = []
    if fmt == "league":
        rows = _round2_qualified(save)
        names = rows
    elif fmt == "challenger":
        swiss = save.get("swiss") or {}
        adv = list(swiss.get("advancers") or [])
        records_all = swiss.get("records") or {}
        adv.sort(key=lambda n: (-(_as_int((records_all.get(n) or {}).get("wins"), 0) or 0),
                                (_as_int((records_all.get(n) or {}).get("losses"), 0) or 0),
                                _rank_of(save, n)))
        if len(adv) < PLAYOFF_SIZE:
            # 用瑞士轮战绩补足
            records = swiss.get("records") or {}
            alive = [n for n in records if n not in adv]
            alive.sort(key=lambda n: (
                -(_as_int((records.get(n) or {}).get("wins"), 0) or 0),
                (_as_int((records.get(n) or {}).get("losses"), 0) or 0),
                _rank_of(save, n),
            ))
            adv = adv + alive[:max(0, PLAYOFF_SIZE - len(adv))]
        names = adv[:PLAYOFF_SIZE]
    elif fmt == "annual":
        arena_top = _arena_top(save, 4)
        qualifiers = list((save.get("bracket") or {}).get("qualifiers") or [])
        names = arena_top + [n for n in qualifiers if n not in arena_top]
    if not names:
        names = _pool_names(save, teams)[:PLAYOFF_SIZE]
    return names[:PLAYOFF_SIZE]


def _breakout_participants(save: Dict[str, Any],
                           teams: Optional[Sequence[Any]]) -> List[str]:
    """突围赛参赛队 = 擂台赛第 5 名及以后（上限 8 队）。"""
    ranked = _arena_ranking(save)
    if not ranked:
        ranked = _pool_names(save, teams)
    rest = ranked[4:]
    return rest[:PLAYOFF_SIZE]


def _rank_of(save: Dict[str, Any], name: str) -> int:
    team = _team_meta(save, name)
    return _as_int(team.get("rank")) or 999


# --------------------------------------------------------------------------- #
# 联赛 / 擂台赛排名（standings）
# --------------------------------------------------------------------------- #


def _match_rows(save: Dict[str, Any], *, stage_key: Optional[str] = None,
                group: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = []
    for match in save.get("schedule") or []:
        if stage_key and match.get("stage") != stage_key:
            continue
        if group and match.get("group") != group:
            continue
        rows.append(match)
    return rows


def _group_table(save: Dict[str, Any], teams: Sequence[str],
                 matches: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for name in teams:
        rows[name] = {
            "team": name,
            "played": 0, "wins": 0, "losses": 0,
            "game_wins": 0, "game_losses": 0, "game_diff": 0,
            "points": 0, "win_rate": None, "rank": 0,
        }
    for match in matches:
        if match.get("status") != "done":
            continue
        blue = match.get("blue_team")
        red = match.get("red_team")
        score = match.get("score") or {}
        bw = _as_int(score.get("blue"), 0) or 0
        rw = _as_int(score.get("red"), 0) or 0
        winner = match.get("winner_team")
        for name in (blue, red):
            if name not in rows:
                continue
            row = rows[name]
            row["played"] += 1
            if name == blue:
                row["game_wins"] += bw
                row["game_losses"] += rw
            else:
                row["game_wins"] += rw
                row["game_losses"] += bw
            if winner == name:
                row["wins"] += 1
                row["points"] += 1
            elif winner:
                row["losses"] += 1
    table = []
    for name, row in rows.items():
        row["game_diff"] = row["game_wins"] - row["game_losses"]
        row["win_rate"] = (round(row["wins"] * 100.0 / row["played"], 2)
                           if row["played"] else None)
        table.append(row)
    table.sort(key=lambda r: (-r["points"], -r["game_diff"], -r["wins"], _rank_of(save, r["team"])))
    for idx, row in enumerate(table):
        row["rank"] = idx + 1
    return table


def _round2_qualified(save: Dict[str, Any]) -> List[str]:
    """round2 结束后：S 组前 4 + A 组前 4 进季后赛（不足则顺延）。"""
    groups = save.get("groups") or []
    stage_key = "round2" if (save.get("event") or {}).get("format") == "league" else save.get("stage")
    qualified: List[str] = []
    for group in groups:
        key = group.get("key")
        if key not in ("S", "A"):
            continue
        table = _group_table(save, group.get("teams") or [],
                             _match_rows(save, stage_key=stage_key, group=key))
        qualified.extend(row["team"] for row in table[:4])
    if not qualified:
        # 兜底：按当前阶段总排名
        table = _group_table(save, _pool_names(save), _match_rows(save, stage_key=stage_key))
        qualified = [row["team"] for row in table[:PLAYOFF_SIZE]]
    return qualified[:PLAYOFF_SIZE]


def _arena_ranking(save: Dict[str, Any]) -> List[str]:
    table = _group_table(save, _pool_names(save), _match_rows(save, stage_key="arena"))
    return [row["team"] for row in table]


def _arena_top(save: Dict[str, Any], count: int) -> List[str]:
    return _arena_ranking(save)[:count]


# --------------------------------------------------------------------------- #
# 单场决赛（final）
# --------------------------------------------------------------------------- #


def _build_final_stage(save: Dict[str, Any], stage_key: str, label: str,
                       bo: int) -> List[Dict[str, Any]]:
    bracket = save.get("bracket") or {}
    wc = bracket.get("winners_champion")
    lc = bracket.get("losers_champion")
    if not wc or not lc:
        # 兜底：用上一阶段排名前两名
        fmt = (save.get("event") or {}).get("format")
        names = _pool_names(save)
        prev = [p[0] for p in stage_plan(fmt)]
        idx = prev.index(stage_key) - 1 if stage_key in prev else 0
        prev_key = prev[max(0, idx)]
        table = _group_table(save, names, _match_rows(save, stage_key=prev_key))
        ranked = [row["team"] for row in table]
        wc = wc or (ranked[0] if ranked else names[0])
        lc = lc or (ranked[1] if len(ranked) > 1 else names[-1])
    match = _make_match(
        save, stage_key=stage_key, stage_label=label, kind="single", bo=bo,
        blue_team=wc, red_team=lc,
        extra={"final_of": "double_elim"},
    )
    return [match]


# --------------------------------------------------------------------------- #
# 模拟与结果回写
# --------------------------------------------------------------------------- #


def _simulate_score(save: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, Any]:
    """按双方该赛季真实 win_rate 加权随机生成系列赛比分（程序模拟，非真实战绩）。"""
    bo = _as_int(match.get("bo"), 1) or 1
    need = _need_wins(bo)
    blue = match.get("blue_team")
    red = match.get("red_team")

    bw = _as_num(_team_meta(save, blue).get("win_rate")) or 50.0
    rw = _as_num(_team_meta(save, red).get("win_rate")) or 50.0
    if bw <= 0 and rw <= 0:
        p_blue = 0.5
    else:
        total = max(1e-6, bw + rw)
        p_blue = max(0.05, min(0.95, bw / total))

    rng = _match_rng(save, str(match.get("match_key")))
    blue_wins = 0
    red_wins = 0
    while blue_wins < need and red_wins < need:
        if rng.random() < p_blue:
            blue_wins += 1
        else:
            red_wins += 1
        if blue_wins + red_wins >= bo:
            break
    # 兜底保证有人达到 need
    if blue_wins < need and red_wins < need:
        if blue_wins > red_wins:
            blue_wins = need
        else:
            red_wins = need
    return {"blue": blue_wins, "red": red_wins}


def _record_result(save: Dict[str, Any], match: Dict[str, Any],
                   winner_side: str, score: Dict[str, int],
                   *, source: str, bp_match_id: Optional[str] = None) -> None:
    if winner_side not in ("blue", "red"):
        raise CareerError("winner_side 只接受 blue / red，收到：%r" % (winner_side))
    match["status"] = "done"
    match["score"] = {"blue": int(score.get("blue", 0)), "red": int(score.get("red", 0))}
    match["winner"] = winner_side
    match["winner_team"] = match.get("blue_team") if winner_side == "blue" \
        else match.get("red_team")
    match["loser_team"] = match.get("red_team") if winner_side == "blue" \
        else match.get("blue_team")
    match["result_source"] = source
    if bp_match_id:
        match["bp_match_id"] = bp_match_id

    # 统计
    coach_team = (save.get("coach") or {}).get("team_name")
    stats = save.setdefault("stats", {})
    if coach_team in (match.get("blue_team"), match.get("red_team")):
        stats["matched"] = (_as_int(stats.get("matched"), 0) or 0) + 1
        won = match.get("winner_team") == coach_team
        if won:
            stats["wins"] = (_as_int(stats.get("wins"), 0) or 0) + 1
        else:
            stats["losses"] = (_as_int(stats.get("losses"), 0) or 0) + 1
        if match.get("blue_team") == coach_team:
            stats["game_wins"] = (_as_int(stats.get("game_wins"), 0) or 0) + int(score.get("blue", 0))
            stats["game_losses"] = (_as_int(stats.get("game_losses"), 0) or 0) + int(score.get("red", 0))
        else:
            stats["game_wins"] = (_as_int(stats.get("game_wins"), 0) or 0) + int(score.get("red", 0))
            stats["game_losses"] = (_as_int(stats.get("game_losses"), 0) or 0) + int(score.get("blue", 0))

    # 瑞士轮记分
    if match.get("kind") == "swiss":
        _swiss_record_result(save, match)

    # 双败槽位
    bracket = save.get("bracket")
    if isinstance(bracket, dict) and match.get("slot_ref"):
        index = _slot_index(bracket)
        raw_ref = str(match.get("slot_ref"))
        pair = index.get(raw_ref) or index.get(raw_ref.split("|", 1)[-1])
        if pair:
            _round, slot = pair
            slot["status"] = "done"
            slot["winner"] = match.get("winner_team")
            slot["loser"] = match.get("loser_team")
            slot["score"] = match.get("score")
            slot["winner_team"] = match.get("winner_team")
            slot["loser_team"] = match.get("loser_team")
        _propagate_bracket(bracket)

    save.setdefault("history", []).append({
        "match_key": match.get("match_key"),
        "stage": match.get("stage"),
        "day": match.get("day"),
        "blue_team": match.get("blue_team"),
        "red_team": match.get("red_team"),
        "score": match.get("score"),
        "winner_team": match.get("winner_team"),
        "source": source,
    })

    _refresh_days(save)


def _play_simulated(save: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, Any]:
    score = _simulate_score(save, match)
    side = "blue" if score["blue"] > score["red"] else "red"
    _record_result(save, match, side, score, source="sim")
    # 瑞士轮一轮打完 → 开下一轮
    _maybe_open_next_swiss_round(save)
    # 双败：把新可打的槽位生成比赛
    bracket = save.get("bracket")
    if isinstance(bracket, dict):
        _sync_bracket_matches(save, bracket)
        maybe_finish_double_elim(save)
    return match


def _maybe_open_next_swiss_round(save: Dict[str, Any]) -> None:
    if (save.get("stage_kind") or _stage_kind(
            (save.get("event") or {}).get("format") or "league", save.get("stage") or "")) != "swiss":
        return
    pending = [m for m in save.get("schedule") or []
               if m.get("stage") == save.get("stage") and m.get("status") == "pending"]
    if pending:
        return
    if len((save.get("swiss") or {}).get("advancers") or []) >= PLAYOFF_SIZE:
        return
    if not _swiss_alive(save):
        return
    new_matches = _swiss_open_round(save)
    if new_matches:
        _append_matches(save, new_matches)


def _sync_swiss(save: Dict[str, Any]) -> None:
    _maybe_open_next_swiss_round(save)


def maybe_finish_double_elim(save: Dict[str, Any]) -> None:
    """把双败阶段打完的赛果落成 champions / qualifiers。"""
    bracket = save.get("bracket")
    if not isinstance(bracket, dict):
        return
    _propagate_bracket(bracket)
    stage_key = bracket.get("stage") or save.get("stage")
    stop = bracket.get("stop_remaining")
    if isinstance(stop, int):
        # 突围赛：剩余队即晋级队
        if _remaining_teams(bracket) <= stop:
            alive: List[str] = []
            for round_ in bracket.get("rounds") or []:
                for slot in round_.get("slots") or []:
                    if slot.get("status") != "done":
                        for name in (slot.get("team"), slot.get("team_b")):
                            if name:
                                alive.append(name)
                    else:
                        for name in (slot.get("winner"), slot.get("team"), slot.get("team_b")):
                            if name:
                                alive.append(name)
            eliminated = set(bracket.get("eliminated") or [])
            candidates = [n for n in _uniq(alive) if n not in eliminated]
            bracket["qualifiers"] = candidates[:stop]
        return

    wc = bracket.get("winners_champion")
    lc = bracket.get("losers_champion")
    if wc and lc:
        bracket["champion"] = None  # 冠军由 final 阶段产生
        bracket["finalists"] = [wc, lc]


def simulate(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None,
             *, count: int = 1, until_mine: bool = False,
             include_mine: bool = False) -> Dict[str, Any]:
    """模拟 pending 比赛（契约 v3 §3）。

    * ``until_mine=True``：按赛程顺序模拟，遇到自己的比赛就停；
    * ``include_mine=True``：连自己的比赛也模拟（测试 / 快速推进用）。
    """
    if save.get("status") != "running":
        raise CareerError("该生涯已结束（status=%s）" % save.get("status"))
    if not save.get("schedule"):
        raise CareerError("还没有赛程，请先抽签（POST /draw）")

    budget = max(1, int(count or 1)) if not until_mine else 10 ** 6
    simulated: List[Dict[str, Any]] = []
    guard = 0
    while len(simulated) < budget and guard < 2000:
        guard += 1
        pending = [m for m in save.get("schedule") or [] if m.get("status") == "pending"]
        if not pending:
            # 瑞士轮可能还有下一轮
            _maybe_open_next_swiss_round(save)
            pending = [m for m in save.get("schedule") or [] if m.get("status") == "pending"]
            if not pending:
                break
        target = None
        for match in pending:
            if match.get("is_mine") and not include_mine:
                if until_mine:
                    target = None
                    break
                continue
            target = match
            break
        if target is None:
            break
        _play_simulated(save, target)
        simulated.append({
            "match_key": target.get("match_key"),
            "day": target.get("day"),
            "stage": target.get("stage"),
            "blue_team": target.get("blue_team"),
            "red_team": target.get("red_team"),
            "score": target.get("score"),
            "winner_team": target.get("winner_team"),
        })
        if until_mine:
            remaining = [m for m in save.get("schedule") or [] if m.get("status") == "pending"]
            if remaining and remaining[0].get("is_mine"):
                break

    save["_last_simulated"] = [
        {k: v for k, v in item.items()} for item in simulated
    ]
    _refresh_days(save)
    return save


def apply_result(save: Dict[str, Any], match_key: str, winner_side: Optional[str] = None,
                 *, score: Optional[Dict[str, int]] = None,
                 bp_match_id: Optional[str] = None,
                 source: str = "bp",
                 winner_team: Optional[str] = None) -> Dict[str, Any]:
    """玩家真正打完 BP 后回写结果（契约 v3 §3）。"""
    if save.get("status") != "running":
        raise CareerError("该生涯已结束（status=%s）" % save.get("status"))
    match = None
    for item in save.get("schedule") or []:
        if item.get("match_key") == match_key:
            match = item
            break
    if match is None:
        raise CareerError("赛程里没有 match_key=%r 的比赛" % (match_key,))
    if winner_team:
        team = str(winner_team).strip()
        if team and team == str(match.get("blue_team") or ""):
            winner_side = "blue"
        elif team and team == str(match.get("red_team") or ""):
            winner_side = "red"
    if match.get("status") == "done":
        raise CareerError("该场比赛已经结束（%s vs %s）"
                          % (match.get("blue_team"), match.get("red_team")))
    if winner_side not in ("blue", "red"):
        raise CareerError("winner_side 只接受 blue / red，收到：%r" % (winner_side,))

    bo = _as_int(match.get("bo"), 1) or 1
    need = _need_wins(bo)
    if not isinstance(score, dict) or score.get("blue") is None:
        # 未给比分时按 BO 满局推算（赢家 need 局，输家 need-1 局）
        score = ({"blue": need, "red": need - 1} if winner_side == "blue"
                 else {"blue": need - 1, "red": need})
    _record_result(save, match, winner_side, score, source=source,
                   bp_match_id=bp_match_id)
    _maybe_open_next_swiss_round(save)
    bracket = save.get("bracket")
    if isinstance(bracket, dict):
        _sync_bracket_matches(save, bracket)
        maybe_finish_double_elim(save)
    return save


# --------------------------------------------------------------------------- #
# 阶段推进
# --------------------------------------------------------------------------- #


def stage_complete(save: Dict[str, Any]) -> bool:
    stage_key = save.get("stage")
    pending = [m for m in save.get("schedule") or []
               if m.get("stage") == stage_key and m.get("status") == "pending"]
    if pending:
        return False
    if (save.get("stage_kind") or "") == "swiss":
        bracket_ready = bool(_swiss_alive(save)) and \
            len((save.get("swiss") or {}).get("advancers") or []) < PLAYOFF_SIZE and \
            (_as_int((save.get("swiss") or {}).get("round"), 0) or 0) < SWISS_MAX_ROUNDS
        if bracket_ready:
            return False
    return True


def next_stage(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """结算当前阶段并进入下一阶段（契约 v3 §3）。"""
    if save.get("status") != "running":
        raise CareerError("该生涯已结束（status=%s）" % save.get("status"))
    if not save.get("schedule"):
        raise CareerError("还没有赛程，请先抽签（POST /draw）")
    if not stage_complete(save):
        pending = [m for m in save.get("schedule") or []
                   if m.get("stage") == save.get("stage") and m.get("status") == "pending"]
        raise CareerError("当前阶段还有 %d 场比赛未完成" % len(pending))

    fmt = (save.get("event") or {}).get("format")
    plan = stage_plan(fmt)
    index = _as_int(save.get("stage_index"), 0) or 0
    finished_key = plan[index][0] if index < len(plan) else save.get("stage")

    save.setdefault("stage_history", []).append({
        "stage": finished_key,
        "label": _stage_label(fmt, finished_key),
    })

    # 联赛 round1 → 重分组 S/A/B
    if finished_key == "round1" and fmt == "league":
        _rebuild_round2_groups(save)

    # 双败阶段结束 → 落 champions / qualifiers
    if _stage_kind(fmt, finished_key) == "double_elim":
        maybe_finish_double_elim(save)
        bracket = save.get("bracket") or {}
        if finished_key == "playoff":
            bracket["champion"] = None
            bracket["finalists"] = [bracket.get("winners_champion"),
                                    bracket.get("losers_champion")]

    # 最后阶段打完 → 生涯结束
    if index + 1 >= len(plan):
        _finish_career(save, finished_key)
        return save

    next_key, next_label, next_kind, _bo = plan[index + 1]
    save["stage"] = next_key
    save["stage_index"] = index + 1
    save["stage_label"] = next_label
    save["stage_kind"] = next_kind
    save["stage_done"] = False

    build_stage_schedule(save, teams)
    return save


def _rebuild_round2_groups(save: Dict[str, Any]) -> None:
    """round1 → round2：各组 1-2 名 → S 组，3-4 → A 组，5-6 → B 组。"""
    groups = save.get("groups") or []
    buckets: Dict[str, List[str]] = {"S": [], "A": [], "B": []}
    for group in groups:
        table = _group_table(save, group.get("teams") or [],
                             _match_rows(save, stage_key="round1", group=group.get("key")))
        ranked = [row["team"] for row in table]
        for idx, name in enumerate(ranked):
            if idx < 2:
                buckets["S"].append(name)
            elif idx < 4:
                buckets["A"].append(name)
            else:
                buckets["B"].append(name)
    new_groups = []
    for key, label in (("S", "S组"), ("A", "A组"), ("B", "B组")):
        if buckets[key]:
            new_groups.append({"key": key, "name": label, "teams": buckets[key]})
    save["groups"] = new_groups


def _finish_career(save: Dict[str, Any], final_stage: str) -> None:
    """最后阶段结束：定冠军、写 stats.championship。"""
    bracket = save.get("bracket") or {}
    champion = None
    last_matches = [m for m in save.get("schedule") or []
                    if m.get("stage") == final_stage and m.get("status") == "done"]
    if last_matches:
        champion = last_matches[-1].get("winner_team")
        runner_up = last_matches[-1].get("loser_team")
        bracket["champion"] = champion
        bracket["runner_up"] = runner_up
        bracket["finalists"] = [last_matches[-1].get("blue_team"),
                                last_matches[-1].get("red_team")]
    save["bracket"] = bracket
    save["status"] = "done"
    save["stage_done"] = True
    coach_team = (save.get("coach") or {}).get("team_name")
    stats = save.setdefault("stats", {})
    if champion and coach_team and champion == coach_team:
        stats["championship"] = (_as_int(stats.get("championship"), 0) or 0) + 1
    stats["champion_team"] = champion
    _refresh_days(save)


# --------------------------------------------------------------------------- #
# 只读视图
# --------------------------------------------------------------------------- #


def standings(save: Dict[str, Any]) -> Dict[str, Any]:
    """当前阶段的积分榜（契约 v3 §3 standings）。"""
    fmt = (save.get("event") or {}).get("format")
    stage_key = save.get("stage")
    kind = _stage_kind(fmt, stage_key)
    matches_all = save.get("schedule") or []

    if kind == "group_round_robin":
        groups = save.get("groups") or []
        out = []
        for group in groups:
            rows = _group_table(
                save, group.get("teams") or [],
                [m for m in matches_all
                 if m.get("stage") == stage_key and m.get("group") == group.get("key")],
            )
            out.append({"key": group.get("key"), "name": group.get("name"), "rows": rows})
        return {"stage": stage_key, "label": save.get("stage_label"), "kind": kind,
                "groups": out, "rules": _stage_rules(fmt, stage_key)}

    if kind == "round_robin":
        rows = _group_table(save, _pool_names(save),
                            [m for m in matches_all if m.get("stage") == stage_key])
        return {"stage": stage_key, "label": save.get("stage_label"), "kind": kind,
                "groups": [{"key": "ALL", "name": "总积分榜", "rows": rows}],
                "rules": _stage_rules(fmt, stage_key)}

    if kind == "swiss":
        swiss = save.get("swiss") or {}
        records = swiss.get("records") or {}
        adv = set(swiss.get("advancers") or [])
        elim = set(swiss.get("eliminated") or [])
        rows = []
        for name, rec in records.items():
            wins = _as_int(rec.get("wins"), 0) or 0
            losses = _as_int(rec.get("losses"), 0) or 0
            if name in adv:
                state = "已晋级"
            elif name in elim:
                state = "已淘汰"
            else:
                state = "进行中"
            rows.append({
                "team": name, "wins": wins, "losses": losses,
                "bye": _as_int(rec.get("bye"), 0) or 0,
                "game_diff": 0, "points": wins, "state": state,
                "played": wins + losses,
            })
        rows.sort(key=lambda r: (-r["wins"], r["losses"], _rank_of(save, r["team"])))
        for idx, row in enumerate(rows):
            row["rank"] = idx + 1
        return {"stage": stage_key, "label": save.get("stage_label"), "kind": kind,
                "groups": [{"key": "ALL", "name": "瑞士轮积分", "rows": rows}],
                "rules": _stage_rules(fmt, stage_key),
                "swiss_round": swiss.get("round")}

    if kind == "double_elim":
        view = bracket_view(save) or {}
        return {"stage": stage_key, "label": save.get("stage_label"), "kind": kind,
                "groups": [], "bracket": view, "rules": _stage_rules(fmt, stage_key)}

    if kind == "single":
        rows = []
        for match in matches_all:
            if match.get("stage") != stage_key:
                continue
            rows.append({
                "team": match.get("blue_team"), "opponent": match.get("red_team"),
                "status": match.get("status"), "winner_team": match.get("winner_team"),
                "score": match.get("score"),
            })
        return {"stage": stage_key, "label": save.get("stage_label"), "kind": kind,
                "groups": [], "final": rows, "rules": _stage_rules(fmt, stage_key)}

    return {"stage": stage_key, "label": save.get("stage_label"), "kind": kind,
            "groups": [], "rules": None}


def _stage_rules(fmt: str, stage_key: str) -> str:
    if fmt == "league" and stage_key == "round1":
        return "组内单循环 BO5，1-2 名进 S 组、3-4 名进 A 组、5-6 名进 B 组"
    if fmt == "league" and stage_key == "round2":
        return "组内单循环 BO5，S 组前 4 与 A 组前 4 进季后赛（双败 BO7）"
    if fmt == "league" and stage_key == "playoff":
        return "双败淘汰 BO7，胜者组冠军与败者组冠军进总决赛"
    if fmt == "challenger" and stage_key == "swiss":
        return "瑞士轮 BO3，累计 3 胜晋级、3 负淘汰，同分按胜负匹配（不重复对阵）"
    if fmt == "challenger" and stage_key == "playoff":
        return "双败淘汰 BO5，胜者组冠军与败者组冠军进总决赛"
    if fmt == "annual" and stage_key == "arena":
        return "擂台赛单循环 BO5，按胜场与净胜局排名，前 4 名直接进正赛"
    if fmt == "annual" and stage_key == "breakout":
        return "突围赛双败淘汰 BO5，前 4 名晋级正赛"
    if fmt == "annual" and stage_key == "playoff":
        return "双败淘汰 BO7，擂台赛前 4 + 突围赛前 4 参赛"
    if stage_key == "final":
        bo = bo_for(fmt, "final")
        return "总决赛 BO%d，胜者组冠军 vs 败者组冠军" % bo
    return ""


def schedule_view(save: Dict[str, Any], *, day: Optional[int] = None,
                  only_mine: bool = False) -> Dict[str, Any]:
    """赛程视图（契约 v3 §3 schedule_view）。"""
    matches_all = list(save.get("schedule") or [])
    coach_team = (save.get("coach") or {}).get("team_name")

    days_map: Dict[int, Dict[str, Any]] = {}
    for match in matches_all:
        d = _as_int(match.get("day"), 1) or 1
        entry = days_map.setdefault(d, {"day": d, "label": "第%d日" % d,
                                        "total": 0, "done": 0, "mine": 0})
        entry["total"] += 1
        if match.get("status") == "done":
            entry["done"] += 1
        if coach_team in (match.get("blue_team"), match.get("red_team")):
            entry["mine"] += 1
    days = [days_map[k] for k in sorted(days_map)]

    pool = {t["name"]: t for t in save.get("pool") or []}
    current_stage = save.get("stage")

    rows = []
    for match in matches_all:
        if only_mine and coach_team not in (match.get("blue_team"), match.get("red_team")):
            continue
        if day is not None and (_as_int(match.get("day"), 1) or 1) != int(day):
            continue
        blue = match.get("blue_team")
        red = match.get("red_team")
        my_turn = (match.get("status") == "pending"
                   and coach_team in (blue, red)
                   and match.get("stage") == current_stage
                   and save.get("status") == "running")
        rows.append({
            "match_key": match.get("match_key"),
            "day": match.get("day"),
            "stage": match.get("stage"),
            "stage_label": match.get("stage_label"),
            "group": match.get("group"),
            "kind": match.get("kind"),
            "bo": match.get("bo"),
            "blue_team": blue,
            "red_team": red,
            "blue_logo": (pool.get(blue) or {}).get("logo_path"),
            "red_logo": (pool.get(red) or {}).get("logo_path"),
            "status": match.get("status"),
            "score": match.get("score"),
            "winner": match.get("winner"),
            "winner_team": match.get("winner_team"),
            "is_mine": bool(coach_team in (blue, red)),
            "my_turn": bool(my_turn),
            "result_source": match.get("result_source"),
            "bracket_round_name": match.get("bracket_round_name"),
            "bp_match_id": match.get("bp_match_id"),
        })

    return {
        "days": days,
        "matches": rows,
        "current_day": save.get("current_day"),
        "total_days": save.get("total_days"),
        "current_stage": current_stage,
        "current_stage_label": save.get("stage_label"),
    }


def next_player_match(save: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """下一场「我的比赛」（契约 v3 §3）。"""
    if save.get("status") != "running":
        return None
    stage_key = save.get("stage")
    coach_team = (save.get("coach") or {}).get("team_name")
    for match in save.get("schedule") or []:
        if match.get("status") != "pending":
            continue
        if match.get("stage") != stage_key:
            continue
        if coach_team in (match.get("blue_team"), match.get("red_team")):
            pool = {t["name"]: t for t in save.get("pool") or []}
            blue = match.get("blue_team")
            red = match.get("red_team")
            return {
                "match_key": match.get("match_key"),
                "day": match.get("day"),
                "stage": match.get("stage"),
                "stage_label": match.get("stage_label"),
                "group": match.get("group"),
                "bo": match.get("bo"),
                "blue_team": blue,
                "red_team": red,
                "blue_logo": (pool.get(blue) or {}).get("logo_path"),
                "red_logo": (pool.get(red) or {}).get("logo_path"),
                "player_side": match.get("player_side"),
                "bp_match_id": match.get("bp_match_id"),
            }
    return None


def bracket_view(save: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """双败淘汰赛视图（契约 v3 §3 bracket_view）。"""
    bracket = save.get("bracket")
    if not isinstance(bracket, dict):
        return None
    pool = {t["name"]: t for t in save.get("pool") or []}

    def _slot_view(slot: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "slot": slot.get("slot"),
            "team": slot.get("team"),
            "team_b": slot.get("team_b"),
            "team_logo": (pool.get(slot.get("team")) or {}).get("logo_path"),
            "team_b_logo": (pool.get(slot.get("team_b")) or {}).get("logo_path"),
            "bo": slot.get("bo"),
            "status": slot.get("status"),
            "winner": slot.get("winner"),
            "loser": slot.get("loser"),
            "score": slot.get("score"),
            "walkover": bool(slot.get("walkover")),
            "match_key": slot.get("match_key"),
        }

    winners, losers = [], []
    for round_ in bracket.get("rounds") or []:
        entry = {
            "key": round_.get("key"),
            "name": round_.get("name"),
            "side": round_.get("side"),
            "slots": [_slot_view(s) for s in (round_.get("slots") or [])],
        }
        (winners if round_.get("side") == "winners" else losers).append(entry)

    return {
        "kind": bracket.get("kind"),
        "stage": bracket.get("stage"),
        "stage_label": bracket.get("stage_label"),
        "bo": bracket.get("bo"),
        "winners": winners,
        "losers": losers,
        "winners_champion": bracket.get("winners_champion"),
        "losers_champion": bracket.get("losers_champion"),
        "champion": bracket.get("champion"),
        "runner_up": bracket.get("runner_up"),
        "qualifiers": bracket.get("qualifiers") or [],
        "eliminated": bracket.get("eliminated") or [],
        "stop_remaining": bracket.get("stop_remaining"),
    }


def summary(save: Dict[str, Any]) -> Dict[str, Any]:
    """生涯概览（契约 v3 §3 summary）。"""
    fmt = (save.get("event") or {}).get("format")
    plan = stage_plan(fmt) if fmt else []
    index = _as_int(save.get("stage_index"), 0) or 0
    schedule = save.get("schedule") or []
    done = len([m for m in schedule if m.get("status") == "done"])
    total = len(schedule)
    stats = save.get("stats") or {}
    matched = _as_int(stats.get("matched"), 0) or 0
    wins = _as_int(stats.get("wins"), 0) or 0
    return {
        "save_id": save.get("save_id"),
        "event": save.get("event"),
        "coach": save.get("coach"),
        "stage": save.get("stage"),
        "stage_label": save.get("stage_label"),
        "stage_kind": save.get("stage_kind"),
        "stage_index": index,
        "stage_total": len(plan),
        "stage_done": bool(save.get("stage_done")),
        "stage_complete": stage_complete(save) if schedule else False,
        "status": save.get("status"),
        "progress": round(done * 1.0 / total, 4) if total else 0.0,
        "current_day": save.get("current_day"),
        "total_days": save.get("total_days"),
        "stats": {
            "matched": matched,
            "wins": wins,
            "losses": _as_int(stats.get("losses"), 0) or 0,
            "game_wins": _as_int(stats.get("game_wins"), 0) or 0,
            "game_losses": _as_int(stats.get("game_losses"), 0) or 0,
            "championship": _as_int(stats.get("championship"), 0) or 0,
            "win_rate": (round(wins * 100.0 / matched, 2) if matched else None),
        },
        "next_match": next_player_match(save),
        "standings_kind": save.get("stage_kind"),
        "champion": (save.get("bracket") or {}).get("champion"),
    }
