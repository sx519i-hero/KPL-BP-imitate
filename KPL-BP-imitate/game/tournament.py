# -*- coding: utf-8 -*-
"""KPL 标准赛制引擎（v6）——生涯 / 好友 PK 联赛 / 大模型 AI 联赛三方共用。

和旧 ``career_engine`` 的区别
-----------------------------
* 旧引擎是「8 人 BO3 瑞士轮 + 双败」的简化模型，只够演示。
* 本引擎按用户提供的 KPL 官方细则实现：**年度积分 → 春/秋 SAB 三阶段（含卡位赛升降级）
  → 季后赛双败 → 挑战者杯 32 强单败 + 双败 + BO9 决赛 → 年度总决赛擂台/突围/淘汰**。
* 所有赛制数值（BO、积分、同分优先级、卡位赛对阵）都从 ``game/kpl_rules.py`` 读取，
  代码里不硬编码，方便后续直接导入官方细则文档。

统一规则速查（亦见 kpl_rules.py 顶部注释）
------------------------------------------
* 全局 BP：同一 BO 系列内，本队已选英雄后续小局不可再选；被 ban 英雄本局双方都不能选。
* 巅峰对决：BO7 打到 3:3、BO9 打到 4:4 才触发；BO5 永远没有；无 ban、清空全局 BP、双方盲选。
* 选边权：循环赛首轮由抽签/高顺位决定；每小局打完败方获得下一局选边权。

存档结构（``data/tourney/<save_id>.json``，schema v4）
-----------------------------------------------------
::

    {
      "save_id": "ts_ab12cd34", "version": 4, "kind": "tournament",
      "mode": "career|friend|llm", "template": "spring", "category": "league",
      "save_name": "我的 2026 春季赛", "created_at": "...", "updated_at": "...",
      "seed": 123456, "status": "running|done", "champion": null,
      "event": {"template","name","short","category","desc","league_id"},
      "coach": {"name","team_name","team_is_custom","team_logo","team_id"},
      "participants": [{"name","team_id","logo_path","win_rate","blue_win_rate",
                        "red_win_rate","role": "player|llm|ai","rank":1}],
      "pool": {"队名": {...}},
      "series": [{"key","slot","name","pipeline","status","ranking","points"}],
      "series_index": 0,
      "stage": "r1", "stage_label": "第一轮循环赛", "stage_kind": "group_round_robin",
      "stage_done": false,
      "groups": {"G1": ["队", ...], "S": [...], "A": [...], "B": [...]},
      "draw_order": [...], "cards": [...],
      "schedule": [match...], "bracket": {...} | null,
      "annual": {"队名": {"spring":0,"challenger":0,"autumn":0,"total":0,"ranks":{}}},
      "stats": {"matched","wins","losses","championship"},
      "log": [{"at","text"}]
    }
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import kpl_rules as R

LOGGER = logging.getLogger("kpl.tourney")

SAVE_VERSION = 4
MAX_LOG = 60

STAGE_DEFS: Dict[str, List[Tuple[str, str, str]]] = {
    "league_sab": [
        ("r1", "第一轮循环赛", "group_round_robin"),
        ("r2", "第二轮循环赛", "group_round_robin"),
        ("cards", "卡位赛", "card_matches"),
        ("r3", "第三轮循环赛", "group_round_robin"),
        ("playoffs", "季后赛", "split_double_elim"),
    ],
    "challenger_cup": [
        ("qualifier", "32 进 8 单败淘汰赛", "single_elim"),
        ("playoff", "双败淘汰赛", "double_elim"),
        ("final", "总决赛", "final"),
    ],
    "annual_finals": [
        ("arena", "擂台赛", "group_round_robin"),
        ("breakout", "突围赛", "single_elim"),
        ("playoff", "淘汰赛", "double_elim"),
    ],
}

# 每个阶段使用的 BO 规则名（→ kpl_rules.bo_of）
BO_KEY: Dict[Tuple[str, str], str] = {
    ("league_sab", "r1"): "regular",
    ("league_sab", "r2"): "regular",
    ("league_sab", "cards"): "card",
    ("league_sab", "r3"): "regular",
    ("league_sab", "playoffs"): "playoff",
    ("challenger_cup", "qualifier"): "challenger_qualifier",
    ("challenger_cup", "playoff"): "challenger_playoff",
    ("challenger_cup", "final"): "challenger_final",
    ("annual_finals", "arena"): "annual_arena",
    ("annual_finals", "breakout"): "annual_breakout",
    ("annual_finals", "playoff"): "annual_playoff",
}


class TourneyError(Exception):
    """业务错误：由 app.py 转成 4xx。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _need_wins(bo: int) -> int:
    return int(bo) // 2 + 1


def _next_pow2(n: int) -> int:
    return 1 << max(0, int(math.ceil(math.log2(max(2, n)))))


def _log(save: Dict[str, Any], text: str, extra: Optional[Dict[str, Any]] = None) -> None:
    import datetime

    entry = {"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "text": text}
    if extra:
        entry.update(extra)
    log = save.setdefault("log", [])
    log.append(entry)
    del log[:-MAX_LOG]
    LOGGER.info("[tourney] %s", text)


def _rng(save: Dict[str, Any], salt: str = ""):
    import random

    seed = _as_int(save.get("seed"), 0) or 0
    return random.Random((seed * 1000003) ^ (zlib.crc32(str(salt).encode("utf-8")) & 0xFFFFFFFF))


def _team_names(save: Dict[str, Any], teams: Optional[Sequence[Any]] = None) -> List[str]:
    if teams:
        out = []
        for item in teams:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("name"):
                out.append(item["name"])
        if out:
            return out
    return [p["name"] for p in save.get("participants") or [] if p.get("name")]


def _short(text: str) -> str:
    text = str(text or "")
    match = re.match(r"^(\d{4})\s*年?\s*(?:KPL)?\s*(?:王者荣耀)?\s*(.+)$", text)
    if match:
        return "%s %s" % (match.group(1), match.group(2).strip())
    return text


# --------------------------------------------------------------------------- #
# 模板 / 赛程包
# --------------------------------------------------------------------------- #
# 年度赛程包的四站（中性名，具体年份/官方全名由 editions 传入 stations 覆盖）
PACKAGE_STATIONS: List[Dict[str, Any]] = [
    {"key": "spring", "slot": "spring", "name": "春季赛",
     "pipeline": "league_sab", "label": "春季赛"},
    {"key": "challenger", "slot": "challenger", "name": "挑战者杯",
     "pipeline": "challenger_cup", "label": "挑战者杯"},
    {"key": "autumn", "slot": "autumn", "name": "夏季赛",
     "pipeline": "league_sab", "label": "夏季赛"},
    {"key": "annual_finals", "slot": None, "name": "年度总决赛",
     "pipeline": "annual_finals", "label": "年度总决赛"},
]


def _merge_stations(stations: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """把外部传入的站点与默认站点合并（按 key 补齐 pipeline/slot）。"""
    base = {row["key"]: dict(row) for row in PACKAGE_STATIONS}
    out: List[Dict[str, Any]] = []
    for st in stations or []:
        key = (st or {}).get("key")
        row = dict(base.get(key) or {})
        row.update({k: v for k, v in (st or {}).items() if v not in (None, "")})
        if not row.get("pipeline"):
            raise TourneyError("赛程包站点缺少 pipeline：%s" % (key or st))
        row.setdefault("label", row.get("name") or key)
        out.append(row)
    return out or [dict(row) for row in PACKAGE_STATIONS]


def template_series(template_key: str,
                    stations: Optional[Sequence[Dict[str, Any]]] = None
                    ) -> List[Dict[str, Any]]:
    """把模板展开成「系列赛」列表（赛程包会有 4 站）。

    ``stations`` 用于年度赛程包：外部传入每一站的官方名称 / league_id，
    这样「2025 年度赛程包」的四站就是 2025 年真实的四个赛事名。
    """
    tpl = R.template(template_key)
    if not tpl:
        raise TourneyError("未知赛事模板：%s" % template_key)
    pipeline = list(tpl.get("pipeline") or [])
    names = {
        "league_sab": tpl.get("short") or "联赛",
    }
    series: List[Dict[str, Any]] = []
    if template_key == "annual_package":
        series = _merge_stations(stations)
    else:
        for idx, pipe in enumerate(pipeline):
            row = {
                "key": "%s_%d" % (template_key, idx),
                "slot": tpl.get("annual_slot"),
                "name": names.get(pipe) or tpl.get("short") or template_key,
                "pipeline": pipe,
                "label": tpl.get("name") or template_key,
            }
            # v29：单赛事也支持「届次」——外部传入该届的官方名称 / league_id
            if stations:
                st = None
                for cand in stations:
                    if (cand or {}).get("key") in (template_key, row["key"]):
                        st = cand
                        break
                if st is None and idx == 0 and len(stations) == 1:
                    st = stations[0]
                if st:
                    label = st.get("label") or st.get("name")
                    if label:
                        row["label"] = label
                        row["name"] = label
                    if st.get("league_id"):
                        row["league_id"] = st["league_id"]
            series.append(row)
    for row in series:
        row.update({"status": "pending", "ranking": [], "points": {}})
        row["stages"] = [k for k, _l, _k2 in STAGE_DEFS.get(row["pipeline"], [])]
    return series


def stage_plan(save: Dict[str, Any]) -> List[Dict[str, Any]]:
    """当前系列赛的阶段清单（供前端展示进度）。"""
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[min(idx, len(save.get("series") or [{}]) - 1)]
    pipe = series.get("pipeline") or "league_sab"
    plan = []
    for key, label, kind in STAGE_DEFS.get(pipe, []):
        plan.append({
            "key": key,
            "label": label,
            "kind": kind,
            "bo": R.bo_of(BO_KEY.get((pipe, key), "regular"), 5),
            "status": ("done" if _stage_index(save, key) < _stage_index(save, save.get("stage") or "")
                       else ("current" if key == (save.get("stage") or "") else "pending")),
        })
    return plan


def _stage_index(save: Dict[str, Any], stage_key: str) -> int:
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[min(idx, len(save.get("series") or [{}]) - 1)]
    stages = [k for k, _l, _k2 in STAGE_DEFS.get(series.get("pipeline") or "league_sab", [])]
    try:
        return stages.index(stage_key)
    except ValueError:
        return -1


# --------------------------------------------------------------------------- #
# 建档
# --------------------------------------------------------------------------- #
def new_save(template_key: str, teams: Sequence[Dict[str, Any]],
             coach: Dict[str, Any], *, mode: str = "career",
             save_name: Optional[str] = None, seed: Optional[int] = None,
             league_id: Optional[str] = None, event_name: Optional[str] = None,
             llm_team: Optional[str] = None,
             stations: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    import datetime
    import secrets

    tpl = R.template(template_key)
    if not tpl:
        raise TourneyError("未知赛事模板：%s" % template_key)
    rows = [dict(t) for t in teams if t.get("name")]
    if len(rows) < 4:
        raise TourneyError("参赛战队不足（需要至少 4 支，收到 %d 支）" % len(rows))
    team_name = (coach or {}).get("team_name")
    if team_name and (coach or {}).get("team_is_custom"):
        # 自建战队顶替排名最后的一支，保持参赛规模
        if not any(r["name"] == team_name for r in rows):
            # v21：年度赛程包的参赛池会同时带 18 支联赛队 + 挑战者杯补足队，
            # 自建队必须落在「联赛段」里，否则联赛阶段按前 18 队截断时会被裁掉。
            anchor = min(len(rows) - 1, max(0, R.template_teams(template_key) - 1))
            rows[anchor] = {"name": team_name, "team_id": None,
                            "logo_path": (coach or {}).get("team_logo"),
                            "win_rate": None, "blue_win_rate": None, "red_win_rate": None,
                            "is_custom": True}
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save: Dict[str, Any] = {
        "save_id": "ts_" + secrets.token_hex(6),
        "version": SAVE_VERSION,
        "kind": "tournament",
        "mode": mode,
        "template": template_key,
        "category": tpl.get("category") or "league",
        "save_name": (save_name or tpl.get("name") or "新赛程").strip()[:24],
        "created_at": now,
        "updated_at": now,
        "seed": _as_int(seed, None) if seed is not None else secrets.randbelow(1 << 30),
        "status": "running",
        "champion": None,
        "event": {
            "template": template_key,
            "name": event_name or tpl.get("name"),
            "short": _short(event_name) if event_name else (tpl.get("short") or ""),
            "category": tpl.get("category"),
            "desc": tpl.get("desc"),
            "league_id": league_id,
            # v21：年度赛程包需要记住「联赛段几支队」，才能把杯赛补位队挡在常规赛之外
            "league_size": (R.league_cfg().get("team_count") or 18)
            if template_key == "annual_package" else None,
        },
        "coach": {
            "name": (coach or {}).get("name") or "教练",
            "team_name": team_name,
            "team_is_custom": bool((coach or {}).get("team_is_custom")),
            "team_logo": (coach or {}).get("team_logo"),
            "team_id": (coach or {}).get("team_id"),
        },
        "participants": [],
        "pool": {},
        "series": template_series(template_key, stations),
        "series_index": 0,
        "stage": "",
        "stage_label": "",
        "stage_kind": "",
        "stage_done": False,
        "groups": {},
        "draw_order": [],
        "cards": [],
        "schedule": [],
        "bracket": None,
        "annual": {},
        "stats": {"matched": 0, "wins": 0, "losses": 0, "championship": 0},
        "log": [],
        "_match_seq": 0,
    }
    for idx, row in enumerate(rows, start=1):
        role = "ai"
        if row.get("name") == team_name:
            role = "player"
        elif llm_team and row.get("name") == llm_team:
            role = "llm"
        save["participants"].append({
            "name": row.get("name"),
            "team_id": row.get("team_id"),
            "logo_path": row.get("logo_path"),
            "abbreviation": row.get("abbreviation"),
            "win_rate": row.get("win_rate"),
            "blue_win_rate": row.get("blue_win_rate"),
            "red_win_rate": row.get("red_win_rate"),
            "rank": _as_int(row.get("rank"), idx),
            "role": role,
            "is_custom": bool(row.get("is_custom")),
        })
        save["pool"][row.get("name")] = save["participants"][-1]
        save["annual"][row.get("name")] = {"spring": 0, "challenger": 0,
                                           "autumn": 0, "total": 0, "ranks": {}}
    if template_key == "annual_package":
        save["event"]["stations"] = [
            {"key": row.get("key"), "label": row.get("label"),
             "league_id": row.get("league_id")}
            for row in save["series"]
        ]
    _log(save, "创建赛程「%s」：模板 %s，%d 支战队，模式 %s"
         % (save["save_name"], template_key, len(save["participants"]), mode))
    start_series(save)
    return save


def start_series(save: Dict[str, Any]) -> None:
    """准备当前系列赛（抽签 + 第一个阶段）。"""
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    pipe = series.get("pipeline")
    series["status"] = "running"
    save["groups"] = {}
    save["bracket"] = None
    save["cards"] = []
    save["stage_done"] = False
    save["stage"] = ""
    save["schedule"] = [m for m in save.get("schedule") or []]
    teams = _series_teams(save, series)
    save["draw_order"] = _draw_order(save, teams)
    _log(save, "「%s」抽签完成：%d 支战队参与" % (series.get("label") or series.get("name"), len(teams)))
    _enter_stage(save, STAGE_DEFS[pipe][0][0])


def _series_teams(save: Dict[str, Any], series: Dict[str, Any]) -> List[str]:
    """当前系列赛的参赛队：年度总决赛取年度积分前 12。"""
    if series.get("pipeline") == "annual_finals":
        ranked = annual_ranking(save)
        return [row["team"] for row in ranked[:R.annual_cfg().get("team_count", 12)]]
    names = [p["name"] for p in save.get("participants") or []]
    want = R.template_teams(save.get("template") or "spring")
    if series.get("pipeline") == "challenger_cup":
        want = R.challenger_cfg().get("team_count", 32) or 32
    if save.get("template") == "annual_package" and series.get("pipeline") == "league_sab":
        # v21：年度赛程包的参赛池同时装着「联赛 18 队 + 挑战者杯补足队」，
        # 联赛段只取排在前面的联赛队，避免杯赛补位队混进常规赛。
        want = min(want, _package_league_size(save))
    if len(names) > want:
        names = names[:want]
    return names


def _package_league_size(save: Dict[str, Any]) -> int:
    """年度赛程包里「联赛段」的队数（= 联赛模板的 team_count，通常 18）。"""
    size = (save.get("event") or {}).get("league_size")
    try:
        size = _as_int(size, None)
    except Exception:                                       # noqa: BLE001
        size = None
    if size and size >= 4:
        return int(size)
    return int(R.league_cfg().get("team_count") or 18)


def _draw_order(save: Dict[str, Any], teams: Sequence[str]) -> List[str]:
    order = list(teams)
    _rng(save, "draw:" + str(save.get("series_index"))).shuffle(order)
    return order


# --------------------------------------------------------------------------- #
# 比赛对象
# --------------------------------------------------------------------------- #
def _mk_match(save: Dict[str, Any], *, stage: str, label: str, kind: str, bo: int,
              blue: str, red: str, group: Optional[str] = None,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    seq = (_as_int(save.get("_match_seq"), 0) or 0) + 1
    save["_match_seq"] = seq
    coach_team = (save.get("coach") or {}).get("team_name")
    match = {
        "match_key": "m%04d" % seq,
        "day": 1,
        "stage": stage,
        "stage_label": label,
        "series": _as_int(save.get("series_index"), 0) or 0,
        "group": group,
        "kind": kind,
        "bo": int(bo),
        "blue_team": blue,
        "red_team": red,
        "status": "pending",
        "score": None,
        "winner": None,
        "winner_team": None,
        "loser_team": None,
        "is_mine": bool(coach_team in (blue, red)),
        "player_side": ("blue" if coach_team == blue else ("red" if coach_team == red else None)),
        "bp_match_id": None,
        "result_source": None,
        "slot_ref": None,
    }
    if extra:
        match.update(extra)
    return match


def _assign_days(matches: Sequence[Dict[str, Any]], start_day: int = 1,
                 per_day: Optional[int] = None) -> int:
    """把一批对局排进比赛日。

    v11 修复（用户反馈）：
      * 每个比赛日最多 ``per_day`` 场（默认取 ``data/kpl_rules.json`` 的
        ``schedule.matches_per_day``，对齐 KPL 真实排期一天 2~3 场），
        以前会把「当天所有不冲突的比赛」全塞进第一天（18 队 → 9 场/天）；
      * 同一支战队一天只会出现一次；
      * 排完后按 (比赛日, 原始顺序) 重排列表，让赛程与模拟都严格按时间从上到下推进。
    """
    if per_day is None:
        per_day = R.matches_per_day()
    per_day = int(per_day or 0)
    day = int(start_day)
    remaining = list(matches)
    order = {id(m): i for i, m in enumerate(matches)}
    while remaining:
        today: set = set()
        rest: List[Dict[str, Any]] = []
        count = 0
        for match in remaining:
            blue, red = match.get("blue_team"), match.get("red_team")
            if per_day > 0 and count >= per_day:
                rest.append(match)
                continue
            if blue in today or red in today:
                rest.append(match)
                continue
            today.add(blue)
            today.add(red)
            match["day"] = day
            count += 1
        if count == 0:
            # 兜底：万一全被上限/冲突挡住，强制排一场，避免死循环
            fallen = rest.pop(0) if rest else remaining[0]
            if fallen in remaining:
                remaining.remove(fallen)
            fallen["day"] = day
            rest = [m for m in remaining if m is not fallen]
        remaining = rest
        day += 1
    # 稳定排序：先比赛日，再原始顺序
    matches.sort(key=lambda m: (_as_int(m.get("day"), 1) or 1, order.get(id(m), 0)))
    return day - 1


def _refresh_days(save: Dict[str, Any]) -> None:
    schedule = save.get("schedule") or []
    if not schedule:
        save["total_days"] = 0
        save["current_day"] = 0
        return
    save["total_days"] = max(_as_int(m.get("day"), 1) or 1 for m in schedule)
    pending = [m for m in schedule if m.get("status") == "pending"]
    save["current_day"] = min((_as_int(m.get("day"), 1) or 1) for m in pending) if pending else save["total_days"]


# v22（用户 m13373）：季后赛 / 淘汰赛「一天只安排一场」，不再是循环赛的 3 场/天。
# 循环赛（分组循环、擂台赛）仍然读 schedule.matches_per_day（默认 3 场/天）。
# 卡位赛是升降级赛、不是淘汰赛，官方一天 2 场，单独给 2。
KNOCKOUT_KINDS = {"single_elim", "double_elim", "split_double_elim", "final", "breakout"}
STAGE_DAY_LIMIT: Dict[str, int] = {
    "single_elim": 1,
    "double_elim": 1,
    "split_double_elim": 1,
    "final": 1,
    "breakout": 1,
    "card_matches": 2,
}


def _stage_kind_of(save: Dict[str, Any], stage: Optional[str]) -> Optional[str]:
    if not stage:
        return None
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [])
    if 0 <= idx < len(series):
        for key, _label, kind in STAGE_DEFS.get(series[idx].get("pipeline"), []):
            if key == stage:
                return kind
    if stage == save.get("stage"):
        return save.get("stage_kind")
    return None


# 按阶段覆盖「每天几场」（官方赛程：挑战者杯 32 进 8 一天 3 场）
STAGE_DAY_BY_STAGE: Dict[str, int] = {"qualifier": 3}


def _stage_per_day(save: Dict[str, Any], stage: Optional[str]) -> Optional[int]:
    """该阶段每个比赛日最多几场；``None`` 表示沿用全局配置（循环赛 3 场/天）。"""
    if stage and stage in STAGE_DAY_BY_STAGE:
        return STAGE_DAY_BY_STAGE[stage]
    return STAGE_DAY_LIMIT.get(_stage_kind_of(save, stage) or "")


def _append(save: Dict[str, Any], matches: Iterable[Dict[str, Any]]) -> None:
    rows = [m for m in matches if m]
    if not rows:
        return
    schedule = save.setdefault("schedule", [])
    start = max([_as_int(m.get("day"), 1) or 1 for m in schedule] or [0]) + 1
    _assign_days(rows, start, _stage_per_day(save, rows[0].get("stage")))
    schedule.extend(rows)
    _refresh_days(save)


# --------------------------------------------------------------------------- #
# 循环赛 / 分组
# --------------------------------------------------------------------------- #
def _round_robin_pairs(teams: Sequence[str]) -> List[Tuple[str, str]]:
    names = [t for t in teams if t]
    pairs: List[Tuple[str, str]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            pairs.append((names[i], names[j]))
    return pairs


def _round_robin_rounds(teams: Sequence[str]) -> List[List[Tuple[str, str]]]:
    """把单循环拆成「轮次」：每轮内所有队伍各打一场（圆桌法）。

    v22（用户 m13373）：一个比赛日要从**不同分组**各取一场，所以必须先知道
    「每个组的第几轮」而不是一条按队伍嵌套的扁平列表。
    6 队 → 5 轮 × 3 场。
    """
    names = [t for t in teams if t]
    if len(names) < 2:
        return []
    roster: List[Optional[str]] = list(names)
    if len(roster) % 2:
        roster.append(None)
    size = len(roster)
    rounds: List[List[Tuple[str, str]]] = []
    for r in range(size - 1):
        pairs: List[Tuple[str, str]] = []
        for i in range(size // 2):
            a, b = roster[i], roster[size - 1 - i]
            if a and b:
                pairs.append((a, b) if r % 2 == 0 else (b, a))
        rounds.append(pairs)
        roster = [roster[0], roster[-1]] + roster[1:-1]
    return rounds


def _snake_groups(order: Sequence[str], count: int) -> List[List[str]]:
    buckets: List[List[str]] = [[] for _ in range(max(1, count))]
    for idx, name in enumerate(order):
        round_no, pos = divmod(idx, len(buckets))
        buckets[pos if round_no % 2 == 0 else len(buckets) - 1 - pos].append(name)
    return buckets


def _make_group_stage(save: Dict[str, Any], stage: str, label: str,
                      groups: List[Tuple[str, List[str]]], bo: int) -> List[Dict[str, Any]]:
    """分组循环赛：按「轮次」交错生成，让每天的比赛来自不同分组。

    v22（用户 m13373）：旧实现是「G1 全部对局 → G2 全部 → G3 全部」，
    于是 `_assign_days` 每天取 3 场时全落在同一个组，出现「一个组打完才轮到下一个组」。
    现在按「第 1 轮 G1 第 1 场 → G2 第 1 场 → G3 第 1 场 → G1 第 2 场 …」交错，
    配合 `_assign_days(per_day=3)` 就是「每天三个组各一场」；
    第三轮只有 S/A 两组时，每天也能同时安排两个分组的比赛。
    """
    planned: List[List[List[Tuple[str, str]]]] = []
    for _gkey, teams in groups:
        planned.append(_round_robin_rounds(teams))
    max_in_round = 0
    for rounds in planned:
        for pairs in rounds:
            max_in_round = max(max_in_round, len(pairs))
    matches: List[Dict[str, Any]] = []
    total_rounds = max([len(rounds) for rounds in planned] or [0])
    for round_no in range(total_rounds):
        for match_no in range(max_in_round):
            for (gkey, _teams), rounds in zip(groups, planned):
                if round_no >= len(rounds) or match_no >= len(rounds[round_no]):
                    continue
                blue, red = rounds[round_no][match_no]
                matches.append(_mk_match(save, stage=stage, label=label,
                                         kind="group_round_robin", bo=bo,
                                         blue=blue, red=red, group=gkey))
    return matches


def build_stage(save: Dict[str, Any]) -> List[Dict[str, Any]]:
    """生成当前阶段的比赛（幂等：已有本阶段比赛就不再生成）。"""
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    pipe = series.get("pipeline")
    stage = save.get("stage") or ""
    if not stage:
        return []
    existing = [m for m in save.get("schedule") or [] if m.get("series") == idx and m.get("stage") == stage]
    if existing:
        return []
    label = save.get("stage_label") or stage
    bo = R.bo_of(BO_KEY.get((pipe, stage), "regular"), 5)
    created: List[Dict[str, Any]] = []

    if pipe == "league_sab":
        if stage == "r1":
            teams = _series_teams(save, series)
            buckets = _snake_groups(save.get("draw_order") or teams,
                                    int(R.league_cfg().get("groups_round1") or 3))
            save["groups"] = {"G%d" % (i + 1): list(b) for i, b in enumerate(buckets)}
            created = _make_group_stage(save, stage, label,
                                        [("G%d" % (i + 1), b) for i, b in enumerate(buckets)], bo)
        elif stage == "r2":
            groups = [("S", save["groups"].get("S") or []),
                      ("A", save["groups"].get("A") or []),
                      ("B", save["groups"].get("B") or [])]
            created = _make_group_stage(save, stage, label, groups, bo)
        elif stage == "cards":
            created = _make_card_stage(save, stage, label)
        elif stage == "r3":
            groups = [("S", save["groups"].get("S") or []),
                      ("A", save["groups"].get("A") or [])]
            created = _make_group_stage(save, stage, label, groups, bo)
        elif stage == "playoffs":
            created = _make_league_playoffs(save, stage, label)
    elif pipe == "challenger_cup":
        if stage == "qualifier":
            created = _make_single_elim(save, stage, label, _series_teams(save, series),
                                        bo, advance_to=int(R.challenger_cfg().get("qualifier", {}).get("advance_to") or 8))
        elif stage == "playoff":
            names = save["groups"].get("CUP8") or []
            # v28：8 支出线队重新抽签决定上下半区，不再沿用资格赛的顺序
            names = _shuffle_seeds(save, names, "cup_playoff")
            created = _make_double_elim(save, stage, label, names, bo)
        elif stage == "final":
            names = save["groups"].get("CUP_FINAL") or []
            if len(names) == 2:
                created = [_mk_match(save, stage=stage, label=label, kind="final", bo=bo,
                                     blue=names[0], red=names[1])]
    elif pipe == "annual_finals":
        if stage == "arena":
            created = _make_arena_stage(save, stage, label, bo)
        elif stage == "breakout":
            created = _make_breakout_stage(save, stage, label, bo)
        elif stage == "playoff":
            names = save["groups"].get("ANNUAL8") or []
            seeded = _annual_playoff_seed(save, names)
            created = _make_double_elim(save, stage, label, seeded, bo, with_final=True)

    _append(save, created)
    # v13：每个阶段的淘汰赛对阵图都留档，切到下一阶段后仍能回看上一张流程图
    if save.get("bracket"):
        save.setdefault("brackets", {})[stage] = save["bracket"]
        # v19：按「站|阶段」再存一份，避免年总包里多站同名阶段（playoffs/playoff）互相覆盖
        save.setdefault("brackets_by_series", {})[_history_key(save.get("series_index"), stage)] = save["bracket"]
    LOGGER.info("[tourney] %s/%s 生成 %d 场比赛", pipe, stage, len(created))
    return created


def _make_card_stage(save: Dict[str, Any], stage: str, label: str) -> List[Dict[str, Any]]:
    cfg = R.league_cfg().get("cards") or {}
    bo = R.bo_of("card", int(cfg.get("bo") or 7))
    tables = save.get("_last_tables") or {}
    created: List[Dict[str, Any]] = []
    cards: List[Dict[str, Any]] = []
    for pair in cfg.get("pairs") or []:
        high_key, low_key = pair.get("high_group"), pair.get("low_group")
        high_rank, low_rank = _as_int(pair.get("high_rank")), _as_int(pair.get("low_rank"))
        high_rows = tables.get(high_key) or []
        low_rows = tables.get(low_key) or []
        if len(high_rows) < (high_rank or 0) or len(low_rows) < (low_rank or 0):
            continue
        high_team = high_rows[(high_rank or 1) - 1]["team"]
        low_team = low_rows[(low_rank or 1) - 1]["team"]
        match = _mk_match(save, stage=stage, label=label, kind="card", bo=bo,
                          blue=high_team, red=low_team,
                          group="%s%d vs %s%d" % (high_key, high_rank, low_key, low_rank),
                          extra={"card": {"high_group": high_key, "high_rank": high_rank,
                                          "low_group": low_key, "low_rank": low_rank}})
        created.append(match)
        cards.append({"match_key": match["match_key"], "high_team": high_team,
                      "low_team": low_team, "high_group": high_key,
                      "low_group": low_key, "winner": None})
    save["cards"] = cards
    return created


def _make_arena_stage(save: Dict[str, Any], stage: str, label: str, bo: int) -> List[Dict[str, Any]]:
    """年总擂台赛：**组外 BO5 单循环**（官方修正版）。

    * 大师组（年度积分 1-6）、精英组（7-12）；
    * 大师组所有队伍只与精英组所有队伍交手 —— 大师组内部不互打、精英组内部不互打；
    * 6 × 6 = 36 场 BO5，两组各自独立积分榜排名。
    """
    ranked = annual_ranking(save)
    cfg = R.annual_cfg()
    masters_cfg = cfg.get("masters") or {}
    masters_range = masters_cfg.get("rank") or [1, 6]
    masters_n = len(range(int(masters_range[0]), int(masters_range[1]) + 1))
    names = [row["team"] for row in ranked[:12]]
    masters = names[:masters_n]
    elite = names[masters_n:]
    save["groups"] = dict(save.get("groups") or {}, master=masters, elite=elite)

    arena_cfg = cfg.get("arena") or {}
    pairs_cfg = arena_cfg.get("cross_groups") or [["master", "elite"]]
    books = {"master": masters, "elite": elite}
    matches: List[Dict[str, Any]] = []
    for pair in pairs_cfg:
        left_key, right_key = pair[0], pair[1]
        left, right = books.get(left_key) or [], books.get(right_key) or []
        if not left or not right:
            continue
        # 按「轮次」生成（第 r 轮：left[i] vs right[(i+r) % n]），这样每天都能排满，
        # 不会出现贪心配对在排期末尾扎堆残留（原实现尾部会出现 2/2/1/1 的不均匀比赛日）
        rounds = max(len(left), len(right))
        for r in range(rounds):
            for i, blue in enumerate(left):
                red = right[(i + r) % len(right)]
                matches.append(_mk_match(save, stage=stage, label=label,
                                         kind="cross_round_robin", bo=bo,
                                         blue=blue, red=red, group="cross"))
    _log(save, "擂台赛（组外单循环）大师 %d 队 × 精英 %d 队 = %d 场 BO%d"
         % (len(masters), len(elite), len(matches), bo))
    return matches


def _shuffle_seeds(save: Dict[str, Any], names: Sequence[str], salt: str) -> List[str]:
    """淘汰赛签位抽签（v28）：整体洗牌，重新决定双败对阵的上/下半区。"""
    teams = [str(t) for t in (names or []) if t]
    if len(teams) < 2:
        return teams
    order = list(teams)
    _rng(save, salt).shuffle(order)
    _log(save, "淘汰赛抽签（%s）：%s" % (salt, order))
    return order


def _annual_playoff_seed(save: Dict[str, Any], names: Sequence[str]) -> List[str]:
    """年总淘汰赛 8 队签位。

    默认 8 队全部「抽签」（用存档 seed 做确定性洗牌，便于复盘）；
    ``kpl_rules.annual_finals.playoff.seed_master_top2.enabled`` 为真时，
    把大师组前 2 名分别钉在上下半区首位，保证不提前相遇，其余 6 队抽签落位。
    """
    teams = [t for t in names if t]
    cfg = (R.annual_cfg().get("playoff") or {}).get("seed_master_top2") or {}
    rnd = _rng(save, "annual_playoff")
    rest = list(teams)
    fixed: Dict[int, str] = {}
    if cfg.get("enabled") and len(teams) == 8:
        tables = save.get("_arena_tables") or save.get("_last_tables") or {}
        masters = [r["team"] for r in (tables.get("master") or [])]
        top2 = [t for t in masters[:2] if t in rest]
        slots = [0, 4]        # 8 队双败：0-3 上半区，4-7 下半区
        for slot, name in zip(slots, top2):
            fixed[slot] = name
            rest.remove(name)
        _log(save, "淘汰赛种子签位：大师组前 2（%s）分处上下半区" % " / ".join(top2))
    rnd.shuffle(rest)
    out: List[str] = []
    for idx in range(len(teams)):
        if idx in fixed:
            out.append(fixed[idx])
        else:
            out.append(rest.pop(0) if rest else "")
    _log(save, "淘汰赛签位（抽签）：%s" % out)
    return out


# v27（用户 m16873）：年总突围赛对阵图（官方流程图三列：突围赛 → 胜者 → 淘汰赛名额）。
# `brk_r1` 是 3 场真实 BO7；`brk_win` / `brk_need` 是纯展示槽（`placeholder`），
# 队名由 `_fill_bracket_from_sources()` 按 `src_a` 回填，不会被 `_propagate()` /
# `_sync_bracket()` 当成真实比赛（否则会被判 walkover、还会去建不存在的小局）。
_BREAKOUT_KIND = "breakout"
BRK_R1 = "brk_r1"
BRK_WIN = "brk_win"
BRK_NEED = "brk_need"
BRK_HOLDER_KEYS = (BRK_WIN, BRK_NEED)


def _holder_slot(bo: int, slot_no: int, src, *, kind: str, label: str) -> Dict[str, Any]:
    """对阵图里的「展示槽」：不建比赛，只按来源回填队名。"""
    slot = _slot(bo)
    slot.update({
        "slot": slot_no,
        "src_a": src,
        "src_b": None,
        "placeholder": kind,
        "placeholder_label": label,
    })
    return slot


def _breakout_bracket_from_rows(save: Dict[str, Any], stage: str,
                                rows: List[Dict[str, Any]],
                                label: Optional[str] = None) -> Dict[str, Any]:
    """用实时 schedule 行重建突围赛图（v27 之前的老存档没有存过 bracket）。"""
    bo = 0
    r1_slots: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        ref = str(row.get("slot_ref") or "")
        no = 0
        if "#" in ref:
            try:
                no = int(ref.rsplit("#", 1)[-1])
            except (TypeError, ValueError):
                no = 0
        if not no:
            no = len(r1_slots) + 1
        if no in seen:
            continue
        seen.add(no)
        bo = int(row.get("bo") or bo or 7)
        slot = _slot(bo)
        slot.update({"slot": no, "team": row.get("blue_team"), "team_b": row.get("red_team"),
                     "bo": bo, "status": "pending", "match_key": row.get("match_key")})
        r1_slots.append(slot)
    r1_slots.sort(key=lambda x: int(x.get("slot") or 0))
    bracket: Dict[str, Any] = {
        "kind": _BREAKOUT_KIND,
        "stage": stage,
        "stage_label": label or "突围赛",
        "bo": bo or 7,
        "rounds": [{"key": BRK_R1, "name": "擂台赛", "side": "winners",
                    "slots": r1_slots}],
        "seeds": [],
        "has_final": False,
        "bracket_columns": ["擂台赛", "突围赛", "淘汰赛"],
    }
    _breakout_extra_rounds(bracket)
    return bracket


def _breakout_extra_rounds(bracket: Dict[str, Any]) -> None:
    """给突围赛图补上官方流程图的后两列：晋级卡 + 淘汰赛名额卡（幂等）。"""
    rounds = [r for r in (bracket.get("rounds") or [])
              if r.get("key") not in BRK_HOLDER_KEYS]
    r1 = next((r for r in rounds if r.get("key") == BRK_R1), None)
    if r1 is None:
        bracket["rounds"] = rounds
        return
    slots = sorted(r1.get("slots") or [], key=lambda s: int(s.get("slot") or 0))
    bo = int(bracket.get("bo") or 7)
    win_slots: List[Dict[str, Any]] = []
    need_slots: List[Dict[str, Any]] = []
    for i, slot in enumerate(slots, start=1):
        no = int(slot.get("slot") or i)
        win_slots.append(_holder_slot(bo, i, (BRK_R1, no, "win"),
                                      kind="winner", label="晋级"))
        need_slots.append(_holder_slot(bo, i, (BRK_WIN, i, "win"),
                                       kind="slot", label="淘汰赛"))
    if win_slots:
        rounds.append({"key": BRK_WIN, "name": "突围赛", "side": "winners",
                       "slots": win_slots})
        rounds.append({"key": BRK_NEED, "name": "淘汰赛", "side": "grand",
                       "slots": need_slots})
        bracket["rounds"] = rounds
        bracket["has_final"] = False
        bracket["bracket_columns"] = ["擂台赛", "突围赛", "淘汰赛"]
    else:
        bracket["rounds"] = rounds


def _make_breakout_stage(save: Dict[str, Any], stage: str, label: str, bo: int) -> List[Dict[str, Any]]:
    """年总突围赛：6 队 BO7 单败，3 场决出 3 个淘汰赛名额（官方修正版）。

    参赛：大师组第 5、第 6 + 精英组第 2~5。
    选对手顺位（``kpl_rules.annual_finals.breakout.pick_order``）：
    大师 5 先挑 → 大师 6 再挑 → 剩余两支自动配对。
    模拟里高顺位优先挑「顺位最低的对手」，与官方实战选择习惯一致。
    """
    # v31：玩家在「突围赛 · 选对手」页面已经选好对阵时直接沿用
    preset: List[Tuple[str, str]] = []
    for item in ((save.get("groups") or {}).get("breakout_pairs") or []):
        if isinstance(item, (list, tuple)) and len(item) == 2 and all(item):
            preset.append((str(item[0]), str(item[1])))
    if len(preset) >= 3:
        pairs: List[Tuple[str, str]] = preset[:3]
        for blue, red in pairs:
            _log(save, "突围赛对阵（玩家/顺位选定）：%s vs %s" % (blue, red))
    else:
        tables = save.get("_last_tables") or save.get("_arena_tables") or {}
        masters = [r["team"] for r in (tables.get("master") or [])]
        elite = [r["team"] for r in (tables.get("elite") or [])]
        high = masters[4:6]          # 大师 5、6
        pool = list(elite[1:5])      # 精英 2~5
        pairs = []
        pickers = [masters[4], masters[5]] if len(high) >= 2 else high[:]
        for picker in pickers:
            if not pool:
                break
            target = pool.pop()      # 顺位最低的对手
            pairs.append((picker, target))
            _log(save, "突围赛选对手：%s 选择 %s" % (picker, target))
        if len(pool) >= 2:
            pairs.append((pool[0], pool[1]))
            _log(save, "突围赛剩余自动配对：%s vs %s" % (pool[0], pool[1]))
        if not pairs:
            raise TourneyError("突围赛参赛队不足（大师 %d / 精英 %d）" % (len(high), len(elite)))
    save["groups"] = dict(save.get("groups") or {}, breakout_pairs=[list(p) for p in pairs])
    _log(save, "突围赛对阵：%s（%d 场 BO%d，胜者 3 队进淘汰赛）" % (pairs, len(pairs), bo))
    # v27（用户 m16873）：官方流程图是三列「擂台赛 → 突围赛 → 淘汰赛」，
    # 突围赛这一列再拆成「3 场 BO7」→「3 个胜者」→「3 个淘汰赛名额」。
    # 这里同时建 bracket：brk_r1 是真实比赛（带 slot_ref），后两列是展示槽。
    series_index = _as_int(save.get("series_index"), 0) or 0
    r1_slots: List[Dict[str, Any]] = []
    created: List[Dict[str, Any]] = []
    for i, (blue, red) in enumerate(pairs, start=1):
        ref = "%d|%s|%s#%d" % (series_index, stage, BRK_R1, i)
        match = _mk_match(save, stage=stage, label=label, kind="breakout", bo=bo,
                          blue=blue, red=red,
                          extra={"slot_ref": ref, "bracket_round": BRK_R1,
                                 "bracket_round_name": "突围赛", "bracket_side": "winners"})
        created.append(match)
        slot = _slot(bo)
        slot.update({"slot": i, "team": blue, "team_b": red, "bo": bo,
                     "status": "pending", "match_key": match.get("match_key")})
        r1_slots.append(slot)
    bracket: Dict[str, Any] = {
        "kind": _BREAKOUT_KIND,
        "stage": stage,
        "stage_label": label,
        "bo": bo,
        "rounds": [{"key": BRK_R1, "name": "擂台赛", "side": "winners",
                    "slots": r1_slots}],
        "seeds": [t for pair in pairs for t in pair],
        "winners_champion": None,
        "losers_champion": None,
        "has_final": False,
        "bracket_columns": ["擂台赛", "突围赛", "淘汰赛"],
    }
    _breakout_extra_rounds(bracket)
    save["bracket"] = bracket
    return created


def _slot(bo: int) -> Dict[str, Any]:
    return {"slot": None, "team": None, "team_b": None, "bo": int(bo),
            "status": "pending", "winner": None, "loser": None,
            "winner_team": None, "loser_team": None, "score": None,
            "match_key": None, "src_a": None, "src_b": None, "walkover": False}


def _make_single_elim(save: Dict[str, Any], stage: str, label: str,
                      teams: Sequence[str], bo: int, *, advance_to: Optional[int] = None
                      ) -> List[Dict[str, Any]]:
    """单败淘汰。``advance_to=8`` 表示只打到剩 8 支（挑战者杯 32 进 8 打 2 轮）。"""
    names = [t for t in teams if t]
    if len(names) < 2:
        raise TourneyError("%s 可用战队不足" % label)
    size = _next_pow2(len(names))
    total_rounds = int(round(math.log2(size)))
    if advance_to:
        keep_rounds = int(round(math.log2(max(2, int(advance_to)))))
        play_rounds = max(1, total_rounds - keep_rounds)
    else:
        play_rounds = total_rounds
    padded: List[Optional[str]] = list(names) + [None] * (size - len(names))
    rounds: List[Dict[str, Any]] = []
    cur: List[Optional[str]] = padded
    for r in range(1, play_rounds + 1):
        slots = []
        for i in range(0, len(cur), 2):
            row = _slot(bo)
            row["slot"] = i // 2 + 1
            row["team"] = cur[i]
            row["team_b"] = cur[i + 1]
            slots.append(row)
        # 截断赛制（挑战者杯 32 进 8）按「N 进 M」命名，避免沿用完整赛程的「半决赛/决赛」
        if advance_to:
            name = "%d 进 %d" % (len([t for t in cur if t]) or len(cur), max(2, len(cur) // 2))
        else:
            name = _round_name(r, play_rounds)
        rounds.append({"key": "s_r%d" % r, "name": name,
                       "side": "single", "slots": slots})
        cur = [None] * (len(cur) // 2)
    bracket = {"kind": "single_elim", "stage": stage, "stage_label": label,
               "bo": bo, "rounds": rounds, "seeds": list(names)}
    for i in range(1, len(rounds)):
        for slot in rounds[i]["slots"]:
            index = slot["slot"] - 1
            slot["src_a"] = ("s_r%d" % i, index * 2 + 1, "win")
            slot["src_b"] = ("s_r%d" % i, index * 2 + 2, "win")
    save["bracket"] = bracket
    return _sync_bracket(save, bracket)


def _round_name(round_no: int, total: int) -> str:
    idx = total - round_no + 1
    names = {1: "决赛", 2: "半决赛", 3: "八强", 4: "十六强", 5: "三十二强"}
    return names.get(idx, "第 %d 轮" % round_no)


def _make_double_elim(save: Dict[str, Any], stage: str, label: str,
                      teams: Sequence[str], bo: int,
                      with_final: bool = False) -> List[Dict[str, Any]]:
    names = [t for t in teams if t]
    if len(names) < 4:
        raise TourneyError("%s 可用战队不足（%d 支）" % (label, len(names)))
    size = _next_pow2(len(names))
    padded: List[Optional[str]] = list(names) + [None] * (size - len(names))
    k = int(math.log2(size))
    rounds: List[Dict[str, Any]] = []
    cur = padded
    for r in range(1, k + 1):
        slots = []
        for i in range(0, len(cur), 2):
            row = _slot(bo)
            row["slot"] = i // 2 + 1
            row["team"] = cur[i]
            row["team_b"] = cur[i + 1]
            slots.append(row)
        rounds.append({"key": "w_r%d" % r, "name": _wb_name(r, k), "side": "winners", "slots": slots})
        cur = [None] * (len(cur) // 2)
    lb_total = 2 * (k - 1)
    for j in range(1, lb_total + 1):
        count = size // (1 << ((j + 1) // 2 + 1))
        slots = [_slot(bo) for _ in range(max(1, count))]
        for idx, row in enumerate(slots, start=1):
            row["slot"] = idx
        rounds.append({"key": "l_r%d" % j, "name": _lb_name(j, lb_total),
                       "side": "losers", "slots": slots})
    bracket = {"kind": "double_elim", "stage": stage, "stage_label": label,
               "bo": bo, "rounds": rounds, "seeds": list(names)}
    _wire_standard_double_elim(bracket, k)
    if with_final:
        # v15：标准双败必须补一场「总决赛」——胜者组冠军 vs 败者组冠军。
        # 挑战者杯不传 with_final，它的总决赛是独立的下一阶段（单场 BO9），
        # 由 bracket_view 的 _final_stage_round() 自动补进图里。
        gf = _slot(bo)
        gf["slot"] = 1
        rounds.append({"key": "gf", "name": "总决赛", "side": "grand", "slots": [gf]})
        gf["src_a"] = ("w_r%d" % k, 1, "win")
        gf["src_b"] = ("l_r%d" % (2 * (k - 1)), 1, "win")
        bracket["has_final"] = True
    save["bracket"] = bracket
    return _sync_bracket(save, bracket)


def _wb_name(round_no: int, k: int) -> str:
    """胜者组轮次名称：按「本轮参赛队数」命名（2 队=决赛、4 队=半决赛、8 队=八强赛…）。

    旧实现用 ``k - round_no + 1`` 当键，8 队时第 1 轮会被叫成「胜者组决赛」。
    """
    teams = 1 << (k - round_no + 1)
    return {2: "胜者组决赛", 4: "胜者组半决赛", 8: "胜者组八强赛",
            16: "胜者组十六强赛"}.get(teams, "胜者组第 %d 轮" % round_no)


def _lb_name(j: int, total: int) -> str:
    return {1: "败者组第一轮", 2: "败者组第二轮", 3: "败者组第三轮",
            4: "败者组决赛"}.get(j, "败者组第 %d 轮" % j)


def _wire_standard_double_elim(bracket: Dict[str, Any], k: int) -> None:
    """标准双败连线（size = 2^k，全部队伍从胜者组出发）。

    胜者组：w_r1..w_rk。
    败者组：l_r1 吃 w_r1 的两两败者；之后「奇数轮合并上一轮两个胜者、偶数轮对上胜者组同序败者」。
    """
    index = {(r["key"], s_["slot"]) for r in bracket["rounds"] for s_ in r["slots"]}

    def ref(round_key: str, slot_no: int, kind: str):
        return (round_key, slot_no, kind) if (round_key, slot_no) in index else None

    # 胜者组内部推进
    for i in range(2, k + 1):
        for round_ in bracket["rounds"]:
            if round_["key"] != "w_r%d" % i:
                continue
            for slot in round_["slots"]:
                idx = slot["slot"]
                slot["src_a"] = ref("w_r%d" % (i - 1), idx * 2 - 1, "win")
                slot["src_b"] = ref("w_r%d" % (i - 1), idx * 2, "win")

    lb_rounds = [r for r in bracket["rounds"] if r.get("side") == "losers"]
    for j, round_ in enumerate(lb_rounds, start=1):
        for slot in round_["slots"]:
            idx = slot["slot"]
            if j == 1:
                slot["src_a"] = ref("w_r1", idx * 2 - 1, "lose")
                slot["src_b"] = ref("w_r1", idx * 2, "lose")
            elif j % 2 == 0:
                slot["src_a"] = ref("l_r%d" % (j - 1), idx, "win")
                slot["src_b"] = ref("w_r%d" % (j // 2 + 1), idx, "lose")
            else:
                slot["src_a"] = ref("l_r%d" % (j - 1), idx * 2 - 1, "win")
                slot["src_b"] = ref("l_r%d" % (j - 1), idx * 2, "win")
    bracket["winners_rounds"] = k
    bracket["losers_rounds"] = len(lb_rounds)


def _make_league_playoffs(save: Dict[str, Any], stage: str, label: str) -> List[Dict[str, Any]]:
    """联赛季后赛：10 队双败（KPL 官方 2025 赛程图，共 12 场）。

    参赛资格（``league_sab.round3.playoff_seeds``）：
        **S 组 1-6 名 + A 组 1-4 名 = 10 队**；A 组 5-6 名淘汰。

    对阵（``league_sab.playoffs.pairings``，完全按第三轮排名，不抽签）::

        胜者组  #3  S1 vs S4        #4  S2 vs S3
                #10 #3胜者 vs #4胜者（胜者组决赛）→ 胜者直进总决赛，败者掉 #11
        败者组  #1  A1 vs A4        #2  A2 vs A3
                #5  #1胜者 vs S5    #6  #2胜者 vs S6
                #7  #5胜者 vs #3败者
                #8  #6胜者 vs #4败者
                #9  #7胜者 vs #8胜者
                #11 #9胜者 vs #10败者（败者组决赛）
        总决赛  #12 #10胜者 vs #11胜者
    """
    tables = save.get("_last_tables") or {}
    cfg = R.league_cfg().get("playoffs") or {}
    s_rows = tables.get("S") or []
    a_rows = tables.get("A") or []
    if len(s_rows) < 6 or len(a_rows) < 4:
        raise TourneyError("季后赛需要 S 组 6 队、A 组至少 4 队（实际 %d / %d）" % (len(s_rows), len(a_rows)))
    s = [r["team"] for r in s_rows[:6]]
    a = [r["team"] for r in a_rows[:4]]
    seeded = bool((cfg.get("seed_s12") or {}).get("enabled", True))
    if seeded:
        wb_pairs = [(s[0], s[3]), (s[1], s[2])]
        lb_pairs = [(a[0], a[3]), (a[1], a[2])]
    else:
        rng = _rng(save, "league_playoffs")
        sw = list(s[:4]); rng.shuffle(sw)
        sa = list(a); rng.shuffle(sa)
        wb_pairs = [(sw[0], sw[1]), (sw[2], sw[3])]
        lb_pairs = [(sa[0], sa[1]), (sa[2], sa[3])]
    save["groups"] = dict(save.get("groups") or {},
                          playoff_entrants=list(s) + list(a),
                          playoff_wb_pairs=[list(p) for p in wb_pairs],
                          playoff_lb_pairs=[list(p) for p in lb_pairs])
    _log(save, "季后赛对阵（%s）：胜者组 %s %s；败者组 %s %s；S5=%s S6=%s"
         % ("种子位固定" if seeded else "随机抽签",
            wb_pairs[0], wb_pairs[1], lb_pairs[0], lb_pairs[1], s[4], s[5]))
    return _make_split_double_elim(save, stage, label, wb_pairs, lb_pairs, s[4], s[5],
                                   R.bo_of("playoff", 7))


def _playoff_draw(save: Dict[str, Any], entrants: Sequence[str],
                  seeds: Sequence[str], *, salt: str) -> List[str]:
    """（保留兼容）8 队抽签排位：种子分处上下半区，其余随机填入剩余位。"""
    names = [t for t in entrants if t]
    rng = _rng(save, salt)
    slots: List[Optional[str]] = [None] * len(names)
    seed_list = [t for t in seeds if t in names][:2]
    if len(seed_list) >= 1:
        slots[0] = seed_list[0]
    if len(seed_list) >= 2:
        slots[min(2, len(names) - 1)] = seed_list[1]
    rest = [t for t in names if t not in seed_list]
    rng.shuffle(rest)
    for i, name in enumerate(slots):
        if name is None and rest:
            slots[i] = rest.pop(0)
    return [t for t in slots if t]


def _make_split_double_elim(save: Dict[str, Any], stage: str, label: str,
                            wb_pairs: Sequence[Sequence[str]],
                            lb_pairs: Sequence[Sequence[str]],
                            s5: Optional[str] = None, s6: Optional[str] = None,
                            bo: int = 7) -> List[Dict[str, Any]]:
    """联赛季后赛 10 队双败（12 场）：4 队胜者组 + 6 队败者组。

    结构见 :func:`_make_league_playoffs` 的说明。
    """
    rounds: List[Dict[str, Any]] = []

    def mk(key: str, name: str, side: str, count: int) -> List[Dict[str, Any]]:
        slots = []
        for slot_no in range(1, count + 1):
            row = _slot(bo)
            row["slot"] = slot_no
            slots.append(row)
        rounds.append({"key": key, "name": name, "side": side, "slots": slots})
        return slots

    wb1 = mk("w_r1", "胜者组第一轮", "winners", 2)
    for row, pair in zip(wb1, list(wb_pairs)[:2]):
        row.update({"team": pair[0], "team_b": pair[1]})
    wb2 = mk("w_r2", "胜者组决赛", "winners", 1)
    lb1 = mk("l_r1", "败者组第一轮", "losers", 2)
    for row, pair in zip(lb1, list(lb_pairs)[:2]):
        row.update({"team": pair[0], "team_b": pair[1]})
    lb2 = mk("l_r2", "败者组第二轮", "losers", 2)
    lb2[0]["team_b"] = s5
    lb2[1]["team_b"] = s6
    lb3 = mk("l_r3", "败者组第三轮", "losers", 2)
    lb4 = mk("l_r4", "败者组第四轮", "losers", 1)
    lb5 = mk("l_r5", "败者组决赛", "losers", 1)
    gf = mk("gf", "总决赛", "grand", 1)

    seeds = [p[0] for p in wb_pairs[:2]] + [p[1] for p in wb_pairs[:2]]
    seeds += [p[0] for p in lb_pairs[:2]] + [p[1] for p in lb_pairs[:2]]
    seeds += [t for t in (s5, s6) if t]
    bracket = {"kind": "split_double_elim", "stage": stage, "stage_label": label,
               "bo": bo, "rounds": rounds, "seeds": seeds,
               "winners_champion": None, "losers_champion": None}
    index = {(r["key"], s_["slot"]): s_ for r in rounds for s_ in r["slots"]}

    def set_src(key: str, slot_no: int, a, b) -> None:
        target = index.get((key, slot_no))
        if target is not None:
            target["src_a"] = a
            target["src_b"] = b

    set_src("w_r2", 1, ("w_r1", 1, "win"), ("w_r1", 2, "win"))
    set_src("l_r2", 1, ("l_r1", 1, "win"), None)      # #1胜者 vs S5（S5 为固定参赛队）
    set_src("l_r2", 2, ("l_r1", 2, "win"), None)      # #2胜者 vs S6
    set_src("l_r3", 1, ("l_r2", 1, "win"), ("w_r1", 1, "lose"))
    set_src("l_r3", 2, ("l_r2", 2, "win"), ("w_r1", 2, "lose"))
    set_src("l_r4", 1, ("l_r3", 1, "win"), ("l_r3", 2, "win"))
    set_src("l_r5", 1, ("l_r4", 1, "win"), ("w_r2", 1, "lose"))
    set_src("gf", 1, ("w_r2", 1, "win"), ("l_r5", 1, "win"))
    save["bracket"] = bracket
    return _sync_bracket(save, bracket)


def _slot_rest(bo: int) -> Dict[str, Any]:
    return {"status": "pending", "winner": None, "loser": None, "winner_team": None,
            "loser_team": None, "score": None, "match_key": None,
            "src_a": None, "src_b": None, "walkover": False}


def _clean(slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in slots:
        base = _slot(int(row.get("bo") or 5))
        base.update({k: v for k, v in row.items() if k != "bo"})
        base["bo"] = int(row.get("bo") or 5)
        out.append(base)
    return out


def _bracket_index(bracket: Dict[str, Any]) -> Dict[Tuple[str, int], Dict[str, Any]]:
    return {(r["key"], s["slot"]): s for r in bracket.get("rounds") or [] for s in r["slots"]}


def _resolve(bracket: Dict[str, Any], ref) -> Tuple[Optional[str], bool]:
    if not ref:
        return None, False
    round_key, slot_no, kind = ref
    slot = _bracket_index(bracket).get((round_key, slot_no))
    if slot is None:
        return None, False
    if slot.get("status") != "done":
        return None, False
    return (slot.get("winner") if kind == "win" else slot.get("loser")), True


def _propagate(bracket: Dict[str, Any]) -> None:
    for _ in range(8):
        changed = False
        for round_ in bracket.get("rounds") or []:
            for slot in round_.get("slots") or []:
                if slot.get("status") == "done":
                    continue
                # v27：展示槽（突围赛的「胜者 / 淘汰赛名额」）不参与自动晋级，
                # 否则单来源会被当成 walkover 判成 done，并去建不存在的比赛。
                if slot.get("placeholder"):
                    continue
                if slot.get("src_a") is None:
                    a, a_ok = slot.get("team"), True
                else:
                    a, a_ok = _resolve(bracket, slot.get("src_a"))
                if slot.get("src_b") is None:
                    b, b_ok = slot.get("team_b"), True
                else:
                    b, b_ok = _resolve(bracket, slot.get("src_b"))
                if a_ok and b_ok and not a and not b:
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
                        slot["status"] = "done"
                        slot["walkover"] = True
                        slot["winner"] = a or b
                        slot["loser"] = b or a
                        slot["winner_team"] = a or b
                        slot["loser_team"] = b or a
                        slot["score"] = {"blue": 0, "red": 0}
                        changed = True
        if not changed:
            break


def _sync_bracket(save: Dict[str, Any], bracket: Dict[str, Any]) -> List[Dict[str, Any]]:
    _propagate(bracket)
    existing = {(m.get("slot_ref")): m for m in save.get("schedule") or [] if m.get("slot_ref")}
    created: List[Dict[str, Any]] = []
    rounds_by_key = {r.get("key"): r for r in bracket.get("rounds") or []}
    # v22（用户 m13373）：按官方观赛顺序逐轮放出比赛——比赛日是按「创建顺序」排的，
    # 所以这里必须让同一批可打的比赛按真实轮次排序
    # （联赛：W1 → L1 → L2 → W 决赛 → L3 → L4 → L 决赛 → GF；
    #   杯赛/年总：W 八强 → L1 → W 半决赛 → L2 → W 决赛 → L3 → L 决赛 → 总决赛）。
    # 例：胜者组第一轮打完时，败者组第一轮与胜者组决赛同时可打，
    # 排序保证先排「败者组第一轮」，与官方赛程一致。
    rounds_by_key = {r.get("key"): r for r in bracket.get("rounds") or []}
    blocked = False
    for round_key in _round_play_order(bracket):
        if blocked:
            break
        round_ = rounds_by_key.get(round_key)
        if not round_:
            continue
        for slot in round_.get("slots") or []:
            if slot.get("status") != "pending":
                continue
            if slot.get("placeholder"):
                continue
            if not (slot.get("team") and slot.get("team_b")):
                continue
            ref = "%d|%s|%s#%d" % (_as_int(save.get("series_index"), 0) or 0,
                                   bracket.get("stage"), round_.get("key"), slot.get("slot"))
            if ref in existing:
                continue
            kind = {"single": "single_elim", "winners": "double_elim",
                    "losers": "double_elim", "grand": "final"}.get(round_.get("side"), "double_elim")
            match = _mk_match(save, stage=bracket.get("stage"), label=bracket.get("stage_label"),
                              kind=kind, bo=int(slot.get("bo") or bracket.get("bo") or 5),
                              blue=slot.get("team"), red=slot.get("team_b"),
                              extra={"slot_ref": ref, "bracket_round": round_.get("key"),
                                     "bracket_round_name": round_.get("name"),
                                     "bracket_side": round_.get("side")})
            slot["match_key"] = match.get("match_key")
            created.append(match)
        # v22：逐轮放出——本轮还有没打完的比赛，就不提前放出后面的轮次。
        # 这样比赛日顺序才与官方赛程一致（不会出现「胜者组决赛」和「败者组第二轮」同一天）。
        if any(s.get("status") != "done" for s in round_.get("slots") or []):
            blocked = True
    return created


# v22（用户 m13373）：淘汰赛真实观赛顺序（不是生成顺序）。
_SPLIT_PLAY_ORDER: List[str] = ["w_r1", "l_r1", "l_r2", "w_r2",
                                "l_r3", "l_r4", "l_r5", "gf"]
_STD_PLAY_ORDER: Dict[int, List[str]] = {
    2: ["w_r1", "l_r1", "w_r2", "l_r2", "gf"],
    3: ["w_r1", "l_r1", "w_r2", "l_r2", "w_r3", "l_r3", "l_r4", "gf"],
    4: ["w_r1", "l_r1", "w_r2", "l_r2", "w_r3", "l_r3", "l_r4",
        "w_r4", "l_r5", "l_r6", "gf"],
}


def _round_play_order(bracket: Dict[str, Any]) -> List[str]:
    """返回该对阵图的轮次开打顺序（键列表，始终覆盖全部轮次）。"""
    keys = [r.get("key") for r in bracket.get("rounds") or []]
    kind = bracket.get("kind")
    preset: Optional[List[str]] = None
    if kind == "split_double_elim":
        preset = _SPLIT_PLAY_ORDER
    elif kind == "double_elim":
        try:
            k = int(bracket.get("winners_rounds") or 0)
        except (TypeError, ValueError):
            k = 0
        preset = _STD_PLAY_ORDER.get(k)
    if preset:
        order = [key for key in preset if key in keys]
        order += [key for key in keys if key not in order]
        return order
    return [key for key in keys if key]


# --------------------------------------------------------------------------- #
# 结果 / 推进
# --------------------------------------------------------------------------- #
def _need_score(bo: int) -> int:
    return _need_wins(bo)


def _simulate_score(save: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, int]:
    bo = _as_int(match.get("bo"), 1) or 1
    need = _need_score(bo)
    blue = save["pool"].get(match.get("blue_team")) or {}
    red = save["pool"].get(match.get("red_team")) or {}
    bw = blue.get("win_rate") if isinstance(blue.get("win_rate"), (int, float)) else 50.0
    rw = red.get("win_rate") if isinstance(red.get("win_rate"), (int, float)) else 50.0
    p_blue = min(0.85, max(0.15, 0.5 + (float(bw) - float(rw)) / 120.0))
    rng = _rng(save, "sim:" + str(match.get("match_key")))
    if rng.random() < p_blue:
        return {"blue": need, "red": rng.randint(0, need - 1)}
    return {"blue": rng.randint(0, need - 1), "red": need}


def find_match(save: Dict[str, Any], match_key: str) -> Optional[Dict[str, Any]]:
    for match in save.get("schedule") or []:
        if match.get("match_key") == match_key:
            return match
    return None


def apply_result(save: Dict[str, Any], match_key: str, winner_side: Optional[str] = None,
                 score: Optional[Dict[str, int]] = None, *, source: str = "sim",
                 bp_match_id: Optional[str] = None,
                 bp_record: Optional[Dict[str, Any]] = None,
                 winner_team: Optional[str] = None) -> Dict[str, Any]:
    match = find_match(save, match_key)
    if match is None:
        raise TourneyError("找不到比赛 %s" % match_key)
    if match.get("status") == "done":
        return match
    # v18：胜负以**战队名**为准（阵营随时会因选边互换，队名不会）。
    # 只要传了 winner_team 且能在本场两队里找到，就据此推导 winner_side，
    # 前端传什么 side 都不再影响结果。
    if winner_team:
        team = str(winner_team).strip()
        if team and team == str(match.get("blue_team") or ""):
            winner_side = "blue"
        elif team and team == str(match.get("red_team") or ""):
            winner_side = "red"
        else:
            LOGGER.warning("[tourney] winner_team=%r 不在本场对阵（blue=%r red=%r），回退 winner_side",
                           team, match.get("blue_team"), match.get("red_team"))
    if winner_side not in ("blue", "red"):
        raise TourneyError("winner_side 只接受 blue / red")
    bo = _as_int(match.get("bo"), 1) or 1
    need = _need_score(bo)
    if not score:
        score = {"blue": need, "red": 0} if winner_side == "blue" else {"blue": 0, "red": need}
    score = {"blue": _as_int(score.get("blue"), 0) or 0, "red": _as_int(score.get("red"), 0) or 0}
    match.update({
        "status": "done",
        "score": score,
        "winner": winner_side,
        "winner_team": match.get("blue_team") if winner_side == "blue" else match.get("red_team"),
        "loser_team": match.get("red_team") if winner_side == "blue" else match.get("blue_team"),
        "result_source": source,
        "bp_match_id": bp_match_id or match.get("bp_match_id"),
    })
    # v9 需求 2②：把真实 BP 记录（每手 ban/pick + 双方阵容 + 各小局比分）写进存档，
    # 存档即可离线复盘，不依赖内存里的 BP 对局（它在 TTL 后会回收）。
    if bp_record:
        match["bp_record"] = bp_record
    coach_team = (save.get("coach") or {}).get("team_name")
    if coach_team and coach_team in (match.get("blue_team"), match.get("red_team")):
        stats = save.setdefault("stats", {"matched": 0, "wins": 0, "losses": 0, "championship": 0})
        stats["matched"] = (_as_int(stats.get("matched"), 0) or 0) + 1
        if match.get("winner_team") == coach_team:
            stats["wins"] = (_as_int(stats.get("wins"), 0) or 0) + 1
        else:
            stats["losses"] = (_as_int(stats.get("losses"), 0) or 0) + 1
    _sync_bracket_after_result(save, match)
    _refresh_days(save)
    return match


def _sync_bracket_after_result(save: Dict[str, Any], match: Dict[str, Any]) -> None:
    bracket = save.get("bracket")
    ref = match.get("slot_ref")
    if not bracket or not ref:
        return
    parts = ref.split("|")
    if len(parts) == 3:
        _series, stage, rest = parts
    else:
        stage, rest = parts[0], parts[1]
    if stage != bracket.get("stage"):
        return
    round_key, slot_no = rest.split("#", 1)
    slot = _bracket_index(bracket).get((round_key, int(slot_no)))
    if slot is None:
        return
    slot.update({
        "status": "done",
        "winner": match.get("winner_team"),
        "loser": match.get("loser_team"),
        "winner_team": match.get("winner_team"),
        "loser_team": match.get("loser_team"),
        "score": match.get("score"),
        "match_key": match.get("match_key"),
    })
    _propagate(bracket)
    created = _sync_bracket(save, bracket)
    if created:
        _append(save, created)


def stage_matches(save: Dict[str, Any], stage: Optional[str] = None) -> List[Dict[str, Any]]:
    idx = _as_int(save.get("series_index"), 0) or 0
    stage = stage or save.get("stage")
    return [m for m in save.get("schedule") or []
            if m.get("series") == idx and m.get("stage") == stage]


def stage_complete(save: Dict[str, Any]) -> bool:
    matches = stage_matches(save)
    return bool(matches) and all(m.get("status") == "done" for m in matches)


def _table_rows(save: Dict[str, Any], teams: Sequence[str],
                matches: Sequence[Dict[str, Any]], mix: bool = False) -> List[Dict[str, Any]]:
    """积分榜行。

    ``mix=True`` 用于年总擂台赛的「组外单循环」：一场比赛可能只有一方在本组，
    此时只给在本组的那一方计分（大师组内部不互打，各看自己那张榜）。
    """
    totals: Dict[str, Dict[str, Any]] = {}
    for name in teams:
        totals[name] = {"team": name, "played": 0, "wins": 0, "losses": 0,
                        "game_wins": 0, "game_losses": 0, "game_diff": 0,
                        "points": 0, "win_rate": 0.0}
    h2h: Dict[str, Dict[str, List[int]]] = {}

    def _h2h(a: str, b: str) -> List[int]:
        key = "%s|%s" % (a, b)
        return h2h.setdefault(key, [0, 0])

    for match in matches:
        blue, red = match.get("blue_team"), match.get("red_team")
        if match.get("status") != "done":
            continue
        if not mix and (blue not in totals or red not in totals):
            continue
        if blue not in totals and red not in totals:
            continue
        score = match.get("score") or {}
        sb = _as_int(score.get("blue"), 0) or 0
        sr = _as_int(score.get("red"), 0) or 0
        winner, loser = match.get("winner_team"), match.get("loser_team")
        for name, gw, gl in ((blue, sb, sr), (red, sr, sb)):
            if name not in totals:
                continue
            row = totals[name]
            row["played"] += 1
            row["game_wins"] += gw
            row["game_losses"] += gl
            row["game_diff"] = row["game_wins"] - row["game_losses"]
            if name == winner:
                row["wins"] += 1
                row["points"] += 1
            else:
                row["losses"] += 1
        if winner and loser and winner in totals and loser in totals:
            stat = _h2h(winner, loser)
            stat[0] += 1
            _h2h(loser, winner)[1] += 1
    rows = list(totals.values())
    for row in rows:
        row["win_rate"] = round(100.0 * row["wins"] / row["played"], 1) if row["played"] else 0.0
    rows.sort(key=lambda r: (-r["points"], -r["game_diff"], -r["wins"], r["team"]))
    # 同分优先级（kpl_rules.tiebreak.in_season）：积分 → 净胜局 → 直接交手 → 交手净胜分
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    _resolve_h2h_ties(rows, h2h)
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


def _resolve_h2h_ties(rows: List[Dict[str, Any]], h2h: Dict[str, List[int]]) -> None:
    """对「积分 + 净胜局」都相同的相邻分组，用相互交手战绩再排。"""
    i = 0
    while i < len(rows):
        j = i + 1
        while (j < len(rows) and rows[j]["points"] == rows[i]["points"]
               and rows[j]["game_diff"] == rows[i]["game_diff"]):
            j += 1
        if j - i > 1:
            block = rows[i:j]

            def _score(row, peers=block):
                wins = 0
                for peer in peers:
                    if peer is row:
                        continue
                    wins += _h2h(row["team"], peer["team"])[0]
                return -wins

            block.sort(key=lambda r: (_score(r), r["team"]))
            rows[i:j] = block
        i = j


def _tables_for_stage(save: Dict[str, Any], stage: str) -> Dict[str, List[Dict[str, Any]]]:
    matches = stage_matches(save, stage)
    groups = save.get("groups") or {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    if stage in ("r1",):
        for key, teams in groups.items():
            if key.startswith("G"):
                out[key] = _table_rows(save, teams, [m for m in matches if m.get("group") == key])
    elif stage in ("r2", "r3"):
        for key in ("S", "A", "B"):
            teams = groups.get(key) or []
            if not teams:
                continue
            out[key] = _table_rows(save, teams, [m for m in matches if m.get("group") == key])
    elif stage == "arena":
        # 组外单循环：两组各自统计「本方参与的比赛」，见 _table_rows(mix=True)
        for key in ("master", "elite"):
            teams = groups.get(key) or []
            if teams:
                out[key] = _table_rows(save, teams, matches, mix=True)
    return out


def _stage_ranking(save: Dict[str, Any], stage: str) -> List[Dict[str, Any]]:
    """阶段结束后的整体名次（供发积分/晋级）。"""
    tables = _tables_for_stage(save, stage) or save.get("_last_tables") or {}
    matches = stage_matches(save, stage)
    if stage == "r1":
        rows: List[Dict[str, Any]] = []
        for key in sorted(tables):
            for row in tables[key]:
                rows.append(dict(row, group=key))
        # 各组同名次合并排序（1-2 名进 S …）
        rows.sort(key=lambda r: (r.get("rank") or 99, -r["points"], -r["game_diff"], r["team"]))
        for idx, row in enumerate(rows, start=1):
            row["rank"] = idx
        return rows
    if stage in ("r2", "r3"):
        return [dict(row, group=key) for key in ("S", "A", "B")
                for row in (tables.get(key) or [])]
    if stage == "arena":
        return [dict(row, group=key) for key in ("master", "elite")
                for row in (tables.get(key) or [])]
    if stage in ("cards", "breakout"):
        return []
    if stage == "final":
        match = matches[0] if matches else {}
        if not match:
            return []
        return [{"team": match.get("winner_team"), "rank": 1},
                {"team": match.get("loser_team"), "rank": 2}]
    return []


# --------------------------------------------------------------------------- #
# v28（用户 m17745）：赛程闸门
#   · 年度赛程包一站打完 → 停在总决赛结果页，等玩家点「开启下一赛程」；
#   · 挑战者杯 / 年度总决赛进入淘汰赛前 → 8 队重新抽签分上下半区，等玩家点
#     「开始淘汰赛抽签」。
# 只有单人 career 模式设闸门，联机 / 大模型联赛仍然全自动推进。
# --------------------------------------------------------------------------- #
# 自测脚本会把 KPL_NO_DRAW_GATE 设成 1，跳过闸门直接跑完整赛程
_GATE_DISABLED = (os.environ.get("KPL_NO_DRAW_GATE") or "").strip() in ("1", "true", "yes")


def _career_gate_mode(save: Dict[str, Any]) -> bool:
    if _GATE_DISABLED:
        return False
    return (save.get("mode") or "") == "career"


def _needs_stage_draw(save: Dict[str, Any], stage_key: str) -> bool:
    """挑战者杯 / 年度总决赛的淘汰赛：8 队必须重新抽签，不能沿用上一阶段的顺序。"""
    if not _career_gate_mode(save) or stage_key != "playoff":
        return False
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[idx]
    return (series.get("pipeline") or "") in ("challenger_cup", "annual_finals")


def _gate_stage_teams(save: Dict[str, Any]) -> List[str]:
    groups = save.get("groups") or {}
    for key in ("CUP8", "ANNUAL8"):
        names = groups.get(key)
        if names:
            return [str(n) for n in names if n]
    return []


def _arm_gate(save: Dict[str, Any], kind: str, stage_key: Optional[str] = None) -> Dict[str, Any]:
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[idx]
    gate: Dict[str, Any] = {"kind": kind, "series_index": idx, "stage": stage_key}
    if kind == "series":
        nxt_idx = idx + 1
        row = (save.get("series") or [])[nxt_idx]
        gate.update({
            "series_index": nxt_idx,
            "label": row.get("label") or row.get("name") or "下一站",
            "button": "开启下一赛程",
            "needs_draw": (row.get("pipeline") or "") != "annual_finals",
            "teams": _series_teams(save, row),
        })
    elif kind == "stage":
        gate.update({
            "label": series.get("label") or series.get("name") or "",
            "stage_label": save.get("stage_label") or "",
            "button": "开始淘汰赛抽签",
            "needs_draw": True,
            "teams": _gate_stage_teams(save),
        })
    else:
        plan = _new_breakout_pick(save)
        gate.update({
            "label": series.get("label") or series.get("name") or "",
            "stage_label": "突围赛 · 选对手",
            "button": "开始突围赛选对手",
            "needs_draw": True,
            "teams": plan.get("teams") or [],
        })
        save["breakout_pick"] = plan
    save["draw_gate"] = gate
    _log(save, "赛程闸门：%s（%s）等待玩家点击「%s」"
         % (gate.get("label"), kind, gate.get("button")))
    return gate


def _real_group_keys(groups: Dict[str, Any]) -> List[str]:
    """``save["groups"]`` 里混着分组名单和别的状态（cards_outcome / playoff_entrants …）。

    只把真正的分组键挑出来，避免前端把 ``cards_outcome`` 之类当成一个小组渲染。
    """
    out: List[str] = []
    for key in groups or {}:
        if not isinstance(key, str):
            continue
        if key in ("S", "A", "B", "master", "elite", "CUP_FINAL") or (
                len(key) >= 2 and key[0] in "GMC" and key[1:].isdigit()):
            out.append(key)
    return out


def _draw_payload_series(save: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    groups = save.get("groups") or {}
    group_of: Dict[str, str] = {}
    group_list: List[Dict[str, Any]] = []
    if isinstance(groups, dict):
        for key in _real_group_keys(groups):
            teams = groups.get(key) or []
            names = []
            for team in teams:
                name = team.get("name") if isinstance(team, dict) else team
                if name:
                    names.append(str(name))
                    group_of[str(name)] = key
            group_list.append({"key": key, "name": key, "teams": names})
    if not group_list:
        # 挑战者杯没有小组，抽签直接决定「32 进 8」首轮对阵 → 用首轮槽位当分组展示
        bracket = save.get("bracket") or {}
        rounds = bracket.get("rounds") or []
        first = rounds[0] if rounds else {}
        for i, slot in enumerate(first.get("slots") or [], start=1):
            pair = [str(t) for t in (slot.get("team"), slot.get("team_b")) if t]
            if len(pair) == 2:
                group_list.append({"key": "第 %d 组" % i, "name": "第 %d 组" % i, "teams": pair})
    if not group_of:
        for g in group_list:
            for name in g.get("teams") or []:
                group_of[name] = g.get("key")
    order = [{"team": str(t), "group": group_of.get(str(t), "")}
             for t in (save.get("draw_order") or [])]
    label = row.get("label") or row.get("name") or ""
    return {"kind": "series", "title": "%s · 分组抽签" % label, "label": label,
            "order": order, "groups": group_list, "needs_draw": True}


def _draw_payload_stage(save: Dict[str, Any], stage_key: str) -> Dict[str, Any]:
    bracket = save.get("bracket") or {}
    seeds = [str(t) for t in (bracket.get("seeds") or []) if t]
    half = len(seeds) // 2
    order = [{"team": team,
              "group": ("上半区" if i < half else "下半区"),
              "label": "第 %d 签" % (i + 1)}
             for i, team in enumerate(seeds)]
    groups = [{"key": "upper", "name": "上半区", "teams": seeds[:half]},
              {"key": "lower", "name": "下半区", "teams": seeds[half:]}]
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[idx]
    label = series.get("label") or series.get("name") or ""
    return {"kind": "stage", "title": "%s · 淘汰赛抽签" % label, "label": label,
            "order": order, "groups": groups, "needs_draw": True}


def commit_draw_gate(save: Dict[str, Any]) -> Dict[str, Any]:
    """玩家点「开启下一赛程 / 开始淘汰赛抽签」后执行：真正开下一站或进淘汰赛。"""
    gate = save.get("draw_gate")
    if not isinstance(gate, dict) or not gate:
        raise TourneyError("当前没有待开启的赛程")
    kind = gate.get("kind")
    if kind == "series":
        nxt = _as_int(gate.get("series_index"), None)
        if nxt is None or not (0 <= nxt < len(save.get("series") or [])):
            raise TourneyError("赛程索引无效")
        save["series_index"] = nxt
        start_series(save)
        build_stage(save)
        row = (save.get("series") or [])[nxt]
        payload = _draw_payload_series(save, row)
        if (row.get("pipeline") or "") == "annual_finals":
            payload["needs_draw"] = False
            payload["order"] = []
            payload["groups"] = []
    elif kind == "stage":
        stage_key = gate.get("stage") or ""
        if not stage_key:
            raise TourneyError("缺少阶段信息")
        _enter_stage(save, stage_key)
        payload = _draw_payload_stage(save, stage_key)
    elif kind == "breakout":
        # v31：突围赛选对手有独立页面；这里兜底——没选完的就让 AI 按顺位补齐
        for _ in range(8):
            plan = save.get("breakout_pick") or {}
            if plan.get("complete"):
                break
            apply_breakout_pick(save)
        payload = commit_breakout_gate(save)
        return payload
    else:
        raise TourneyError("未知的赛程闸门类型：%s" % kind)
    save["draw_gate"] = None
    if payload.get("needs_draw"):
        save["draw_result"] = payload
    else:
        save.pop("draw_result", None)
    _log(save, "玩家开启：%s" % payload.get("title"))
    save["stage_done"] = bool(save.get("stage")) and stage_complete(save)
    return payload


def _breakout_participants(save: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """突围赛 6 支参赛队：大师组第 5、第 6（选对手顺位），精英组第 2~5。

    索引可在 ``kpl_rules.json`` 的 ``annual_finals.breakout.participants`` 里改。
    """
    tables = save.get("_last_tables") or save.get("_arena_tables") or {}
    masters = [r["team"] for r in (tables.get("master") or [])]
    elite = [r["team"] for r in (tables.get("elite") or [])]
    cfg = R.annual_cfg().get("breakout") or {}
    part = cfg.get("participants") or {}
    m_pos = [int(x) for x in (part.get("master") or [5, 6])]
    e_pos = [int(x) for x in (part.get("elite") or [2, 3, 4, 5])]
    pickers = [masters[i - 1] for i in m_pos if 0 <= i - 1 < len(masters)]
    pool = [elite[i - 1] for i in e_pos if 0 <= i - 1 < len(elite)]
    return pickers, pool


def _needs_breakout_pick(save: Dict[str, Any], stage_key: str) -> bool:
    """年总突围赛：玩家（生涯模式）要自己一个对一个地选对手。"""
    if not _career_gate_mode(save) or stage_key != "breakout":
        return False
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[idx]
    return (series.get("pipeline") or "") == "annual_finals"


def _breakout_available(plan: Dict[str, Any]) -> List[str]:
    """当前这一手可以挑的对手：所有还没配对的队里去掉当前选人的那支。

    v32：大师 5 也可以选大师 6，所以候选池包含另一位大师。
    """
    if plan.get("complete"):
        return []
    teams = [str(t) for t in (plan.get("teams") or [])]
    paired: List[str] = []
    for pair in plan.get("pairs") or []:
        for team in pair:
            if team:
                paired.append(str(team))
    current = _breakout_current(plan)
    return [t for t in teams if t not in paired and t != current]


def _breakout_current(plan: Dict[str, Any]) -> Optional[str]:
    """下一位该选对手的战队：按大师顺位取，已经被配走的跳过。"""
    if plan.get("complete"):
        return None
    paired: List[str] = []
    for pair in plan.get("pairs") or []:
        for team in pair:
            if team:
                paired.append(str(team))
    for picker in plan.get("pickers") or []:
        if str(picker) not in paired:
            return str(picker)
    return None


def _new_breakout_pick(save: Dict[str, Any]) -> Dict[str, Any]:
    pickers, pool = _breakout_participants(save)
    if len(pickers) < 2 or len(pool) < 4:
        raise TourneyError("突围赛参赛队不足（大师 %d / 精英 %d）" % (len(pickers), len(pool)))
    masters = [str(t) for t in pickers]
    elite = [str(t) for t in pool]
    return {"teams": masters + elite, "pickers": masters, "masters": masters,
            "elite": elite, "pairs": [], "step": 0, "complete": False,
            "allow_master_opponent": True}


def _breakout_pick_view(save: Dict[str, Any]) -> Dict[str, Any]:
    plan = save.get("breakout_pick") or {}
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[idx]
    my_team = str(((save.get("coach") or {}).get("team_name")) or "")
    pickers = [str(t) for t in (plan.get("pickers") or [])]
    elite_all = [str(t) for t in (plan.get("elite") or [])]
    step = _as_int(plan.get("step"), 0) or 0
    complete = bool(plan.get("complete"))
    current = _breakout_current(plan)
    available = _breakout_available(plan)
    return {
        "active": bool(plan),
        "teams": [str(t) for t in (plan.get("teams") or [])],
        "masters": pickers,
        "elite": elite_all,
        "pickers": pickers,
        "pool": available,
        "allow_master_opponent": bool(plan.get("allow_master_opponent")),
        "pairs": [list(p) for p in (plan.get("pairs") or [])],
        "step": step,
        "complete": complete,
        "current": current,
        "my_team": my_team,
        "my_turn": bool(current and current == my_team),
        "label": save.get("stage_label") or "突围赛",
        "series_label": series.get("label") or series.get("name") or "",
    }


def apply_breakout_pick(save: Dict[str, Any], opponent: Optional[str] = None) -> Dict[str, Any]:
    """当前选对手的战队挑一支对手（``opponent`` 为空时由 AI 挑顺位最低的）。"""
    plan = save.get("breakout_pick")
    if not isinstance(plan, dict) or not plan:
        raise TourneyError("当前没有进行中的突围赛选对手")
    if plan.get("complete"):
        return _breakout_pick_view(save)
    picker = _breakout_current(plan)
    if not picker:
        raise TourneyError("选对手轮次已结束")
    available = _breakout_available(plan)
    if opponent:
        opponent = str(opponent)
        if opponent not in available:
            raise TourneyError("「%s」不在可选对手里" % opponent)
    else:
        # AI 偏好：优先挑顺位最低的精英队；只剩大师可挑时挑大师
        elites = [t for t in available if t not in (plan.get("pickers") or [])]
        opponent = (elites or available)[-1]
    pairs = [list(p) for p in (plan.get("pairs") or [])]
    pairs.append([picker, opponent])
    plan["pairs"] = pairs
    plan["step"] = (_as_int(plan.get("step"), 0) or 0) + 1
    _log(save, "突围赛选对手：%s 选择 %s" % (picker, opponent))
    # 剩下 2 支自动配对；若大师 5 选了大师 6（选人顺位都用完）则剩 4 支配成 2 场
    paired_all = [x for pair in pairs for x in pair]
    remaining = [t for t in (plan.get("teams") or []) if t not in paired_all]
    pickers_left = [p for p in (plan.get("pickers") or []) if str(p) not in paired_all]
    if len(remaining) == 2:
        pairs.append([remaining[0], remaining[1]])
        plan["pairs"] = pairs
        plan["complete"] = True
        _log(save, "突围赛剩余自动配对：%s vs %s" % (remaining[0], remaining[1]))
    elif len(remaining) == 4 and not pickers_left:
        pairs.append([remaining[0], remaining[1]])
        pairs.append([remaining[2], remaining[3]])
        plan["pairs"] = pairs
        plan["complete"] = True
        _log(save, "突围赛剩余自动配对：%s vs %s / %s vs %s"
             % (remaining[0], remaining[1], remaining[2], remaining[3]))
    save["breakout_pick"] = plan
    return _breakout_pick_view(save)


def commit_breakout_gate(save: Dict[str, Any]) -> Dict[str, Any]:
    """选完对手后真正开打突围赛。"""
    plan = save.get("breakout_pick")
    gate = save.get("draw_gate") or {}
    if not isinstance(plan, dict) or not plan:
        raise TourneyError("当前没有进行中的突围赛选对手")
    if not plan.get("complete"):
        raise TourneyError("还有战队没有选择对手")
    pairs = [list(p) for p in (plan.get("pairs") or [])]
    save["groups"] = dict(save.get("groups") or {}, breakout_pairs=pairs)
    save["draw_gate"] = None
    save.pop("breakout_pick", None)
    _enter_stage(save, "breakout")
    save["stage_done"] = bool(save.get("stage")) and stage_complete(save)
    _log(save, "玩家完成突围赛选对手：%s" % pairs)
    return {"kind": "breakout",
            "title": "%s · 突围赛对阵" % (gate.get("label") or ""),
            "pairs": pairs, "needs_draw": False}


def advance(save: Dict[str, Any]) -> bool:
    """若当前阶段结束，结算并进入下一阶段 / 下一站。返回是否推进过。

    v28：遇到「赛程闸门」就停下——等玩家点按钮（``commit_draw_gate``）再继续。
    """
    guard = 0
    moved = False
    while guard < 12 and save.get("status") != "done":
        guard += 1
        if save.get("draw_gate"):
            break
        stage = save.get("stage") or ""
        if not stage or not stage_complete(save):
            break
        if save.get("stage_done"):
            break
        _finish_stage(save, stage)
        nxt = _next_stage_key(save)
        if nxt:
            if _needs_breakout_pick(save, nxt):
                _arm_gate(save, "breakout", nxt)
                moved = True
                break
            if _needs_stage_draw(save, nxt):
                _arm_gate(save, "stage", nxt)
                moved = True
                break
            _enter_stage(save, nxt)
            moved = True
            continue
        # 当前系列赛结束
        _finish_series(save)
        if save.get("status") == "done":
            break
        cur_idx = _as_int(save.get("series_index"), 0) or 0
        if _career_gate_mode(save) and cur_idx + 1 < len(save.get("series") or []):
            _arm_gate(save, "series")
            moved = True
            break
        save["series_index"] = cur_idx + 1
        start_series(save)
        stage = save.get("stage") or ""
        build_stage(save)
        moved = True
        continue
    save["stage_done"] = bool(save.get("stage")) and stage_complete(save)
    return moved


def _next_stage_key(save: Dict[str, Any]) -> Optional[str]:
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    stages = [k for k, _l, _k2 in STAGE_DEFS.get(series.get("pipeline"), [])]
    cur = save.get("stage") or ""
    try:
        pos = stages.index(cur)
    except ValueError:
        return stages[0] if stages else None
    return stages[pos + 1] if pos + 1 < len(stages) else None


def _enter_stage(save: Dict[str, Any], stage_key: str) -> None:
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    pipe = series.get("pipeline")
    labels = {k: (l, kd) for k, l, kd in STAGE_DEFS.get(pipe, [])}
    label, kind = labels.get(stage_key, (stage_key, "group_round_robin"))
    save["stage"] = stage_key
    save["stage_label"] = label
    save["stage_kind"] = kind
    save["stage_done"] = False
    if stage_key != "playoffs" and stage_key != "playoff":
        save["bracket"] = None
    _log(save, "进入阶段：%s › %s（%s）" % (series.get("label") or series.get("name"), label, kind))
    build_stage(save)


def _finish_stage(save: Dict[str, Any], stage: str) -> None:
    tables = _tables_for_stage(save, stage)
    if tables:
        save["_last_tables"] = tables
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    pipe = series.get("pipeline")

    if pipe == "league_sab":
        if stage == "r1":
            save["groups"] = dict(save.get("groups") or {})
            for key in ("G1", "G2", "G3"):
                rows = tables.get(key) or []
                if not rows:
                    continue
                save["groups"]["S"] = (save["groups"].get("S") or []) + [r["team"] for r in rows[:2]]
                save["groups"]["A"] = (save["groups"].get("A") or []) + [r["team"] for r in rows[2:4]]
                save["groups"]["B"] = (save["groups"].get("B") or []) + [r["team"] for r in rows[4:6]]
            _log(save, "第一轮分组结果：S=%s / A=%s / B=%s"
                 % (save["groups"].get("S"), save["groups"].get("A"), save["groups"].get("B")))
        elif stage == "r2":
            save["_r2_tables"] = tables
        elif stage == "cards":
            # v46：卡位赛没有小组积分榜，直接用 save["groups"] + 比赛结果升降级
            _apply_cards(save)
        elif stage == "r3":
            save["_r3_tables"] = tables
        elif stage == "playoffs":
            bracket = save.get("bracket") or {}
            champion = _bracket_champion(bracket)
            series["ranking"] = _final_ranking(save, series)
            series["status"] = "done"
            _crown(save, champion)
    elif pipe == "challenger_cup":
        if stage == "qualifier":
            bracket = save.get("bracket") or {}
            want = int(R.challenger_cfg().get("qualifier", {}).get("advance_to") or 8)
            winners = _bracket_survivors(bracket, want)
            save["groups"] = dict(save.get("groups") or {}, CUP8=winners)
            save["_qualifier_order"] = [row["team"] for row in _bracket_ranking(bracket)
                                        if row["team"] not in winners]
            _log(save, "挑战者杯 32 进 8 晋级：%s" % winners)
        elif stage == "playoff":
            bracket = save.get("bracket") or {}
            champion = _bracket_champion(bracket)
            finalists = _double_elim_finalists(bracket)
            save["groups"] = dict(save.get("groups") or {}, CUP_FINAL=finalists)
            save["_cup_playoff_champion"] = champion
            _log(save, "挑战者杯双败结束，决赛对阵：%s" % finalists)
        elif stage == "final":
            ranking = _final_ranking(save, series)
            # v21：挑战者杯也要把整站名次落档，否则 _finish_series 结算年度积分时拿不到名次，
            # 挑杯一栏永远 0 分（年总包里所有队都是 0）。
            series["ranking"] = ranking
            series["status"] = "done"
            if ranking:
                _crown(save, ranking[0]["team"])
    elif pipe == "annual_finals":
        if stage == "arena":
            save["_arena_tables"] = tables
            cfg = R.annual_cfg()
            masters = tables.get("master") or []
            elite = tables.get("elite") or []
            direct = [r["team"] for r in masters[:4]] + ([elite[0]["team"]] if elite else [])
            to_break = [r["team"] for r in masters[4:6]] + [r["team"] for r in elite[1:5]]
            out = ([elite[5]["team"]] if len(elite) >= 6 else [])
            save["groups"] = dict(save.get("groups") or {}, annual_direct=direct,
                                  annual_breakout=to_break, annual_out=out)
            _log(save, "擂台赛结束：直进淘汰赛 %s；进突围赛 %s；淘汰 %s" % (direct, to_break, out))
        elif stage == "breakout":
            winners = [m.get("winner_team") for m in stage_matches(save, "breakout")]
            direct = (save.get("groups") or {}).get("annual_direct") or []
            playoff = direct + [w for w in winners if w]
            save["groups"] = dict(save.get("groups") or {}, ANNUAL8=playoff)
            _log(save, "突围赛晋级：%s；淘汰赛 8 队：%s" % (winners, playoff))
        elif stage == "playoff":
            bracket = save.get("bracket") or {}
            champion = _bracket_champion(bracket)
            series["ranking"] = _final_ranking(save, series)
            series["status"] = "done"
            _crown(save, champion)
    # v19：结榜快照（供历史回看使用）。跨站后 start_series 会清空 groups，
    # 届时第一轮的 G1/G2/G3 与年总的大师/精英分组只能靠这份快照还原。
    if tables:
        save.setdefault("stage_snapshots", {})[_history_key(idx, stage)] = {
            "tables": tables,
            "label": save.get("stage_label"),
            "kind": save.get("stage_kind"),
        }
    save["stage_done"] = True


def _apply_cards(save: Dict[str, Any]) -> None:
    """卡位赛结束 → 按官方升降级规则改写 S / A 分组。

    v46 修 bug（用户反馈「卡位赛赢得队伍没有去到该去的分组」）：
    以前是 ``_apply_cards(save, tables)``，而 ``_tables_for_stage(save, "cards")`` 对 cards 阶段
    返回空字典（卡位赛是 4 场独立对阵，没有小组积分榜），于是「S/A/B 各 6 队」的前置判断永远
    不满足，直接 log「卡位赛结算数据不足」就 return —— **升降级从来没生效过**。

    现在改为读 ``save["_r2_tables"]``（第二轮结束时的 S/A/B 排名表，按名次排好序）：

      * S1-S4 直接留在 S；A3-A4 直接留在 A；B3-B6 留在 B（被淘汰区）。
      * S5 vs A2、S6 vs A1：胜者进 S，败者进 A；
      * A5 vs B2、A6 vs B1：胜者进 A，败者淘汰（进 B 名单）。
    """
    groups = save.setdefault("groups", {})
    tables = save.get("_r2_tables")
    if not isinstance(tables, dict) or not tables:
        tables = _tables_for_stage(save, "r2") or {}
    s_rows = [r.get("team") for r in (tables.get("S") or []) if r.get("team")]
    a_rows = [r.get("team") for r in (tables.get("A") or []) if r.get("team")]
    b_rows = [r.get("team") for r in (tables.get("B") or []) if r.get("team")]
    if len(s_rows) < 6 or len(a_rows) < 6 or len(b_rows) < 6:
        # 极端兜底：排名表缺失时退回 groups 的原始顺序（不再直接跳过，避免整段不生效）
        s_rows = s_rows or list(groups.get("S") or [])
        a_rows = a_rows or list(groups.get("A") or [])
        b_rows = b_rows or list(groups.get("B") or [])
    if len(s_rows) < 6 or len(a_rows) < 6 or len(b_rows) < 6:
        _log(save, "卡位赛结算数据不足，跳过升降级（S=%d A=%d B=%d）"
             % (len(s_rows), len(a_rows), len(b_rows)))
        return

    next_s: List[str] = list(s_rows[0:4])
    next_a: List[str] = [a_rows[2], a_rows[3]]
    next_b: List[str] = list(b_rows[2:])
    played = 0
    for match in stage_matches(save, "cards"):
        winner = match.get("winner_team")
        loser = match.get("loser_team")
        if not winner or not loser:
            continue
        played += 1
        high_group = (match.get("card") or {}).get("high_group")
        if high_group == "S":
            next_s.append(winner)
            next_a.append(loser)
        elif high_group == "A":
            next_a.append(winner)
            next_b.append(loser)

    def _uniq(rows: List[str]) -> List[str]:
        seen = set()
        out = []
        for name in rows:
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    groups["S"] = _uniq(next_s)[:6]
    groups["A"] = _uniq(next_a)[:6]
    groups["B"] = _uniq(next_b)[:6]
    groups["cards_outcome"] = {
        "next_S": list(groups["S"]), "next_A": list(groups["A"]),
        "eliminated": list(groups["B"]), "played": played,
    }
    _log(save, "卡位赛升降级完成（%d 场）：S=%s / A=%s / 淘汰=%s"
         % (played, groups["S"], groups["A"], groups["B"]))


def _bracket_ranking(bracket: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按「赢的场次 + 被淘汰的轮次」给淘汰赛所有参赛队排名（champion 第 1）。"""
    if not bracket:
        return []
    rounds = bracket.get("rounds") or []
    order = {r.get("key"): i for i, r in enumerate(rounds)}
    wins: Dict[str, int] = {}
    last_loss: Dict[str, int] = {}
    played: List[str] = []
    for round_ in rounds:
        pos = order.get(round_.get("key"), 0)
        for slot in round_.get("slots") or []:
            if slot.get("status") != "done" or slot.get("walkover"):
                continue
            winner, loser = slot.get("winner"), slot.get("loser")
            if winner:
                wins[winner] = wins.get(winner, 0) + 1
                if winner not in played:
                    played.append(winner)
            if loser:
                last_loss[loser] = max(last_loss.get(loser, -1), pos)
                if loser not in played:
                    played.append(loser)
    for name in bracket.get("seeds") or []:
        if name and name not in played:
            played.append(name)
    champion = _bracket_champion(bracket)
    rest = [t for t in played if t and t != champion]
    rest.sort(key=lambda t: (-wins.get(t, 0), -last_loss.get(t, -1), t))
    rows: List[Dict[str, Any]] = []
    if champion:
        rows.append({"team": champion, "rank": 1})
    for idx, name in enumerate(rest, start=2):
        rows.append({"team": name, "rank": idx})
    return rows


def _final_ranking(save: Dict[str, Any], series: Dict[str, Any]) -> List[Dict[str, Any]]:
    """整个系列赛的最终名次：淘汰赛排名 + 未进淘汰赛的队伍按最后阶段成绩补齐。"""
    pipe = series.get("pipeline")
    bracket = save.get("bracket") or {}
    if not bracket and pipe == "challenger_cup":
        # v21：挑战者杯的「总决赛」是独立阶段，进到 final 时 save["bracket"] 已被清空，
        # 真正的双败对阵图存在 brackets_by_series 里，这里取回来算季后赛名次。
        idx = _as_int(save.get("series_index"), 0) or 0
        bracket = ((save.get("brackets_by_series") or {}).get(_history_key(idx, "playoff"))
                   or (save.get("brackets") or {}).get("playoff") or {})
    rows = _bracket_ranking(bracket)
    if pipe == "challenger_cup":
        # v21：挑战者杯总冠军由独立的 BO9 总决赛决定，双败对阵图的胜者只是决赛选手，
        # 这里把总决赛的胜/负方顶到第 1/2 名，其余队按双败名次顺延。
        idx = _as_int(save.get("series_index"), 0) or 0
        for match in save.get("schedule") or []:
            if match.get("series") != idx or match.get("stage") != "final":
                continue
            if match.get("status") != "done":
                continue
            win_team = match.get("winner_team") or match.get("winner")
            lose_team = match.get("loser_team") or match.get("loser")
            if win_team:
                rows = [{"team": win_team, "rank": 1}]
                if lose_team:
                    rows.append({"team": lose_team, "rank": 2})
                rows = rows + [r for r in _bracket_ranking(bracket)
                               if r["team"] not in (win_team, lose_team)]
                break
    seen = {r["team"] for r in rows}
    fallback: List[str] = []
    if pipe == "league_sab":
        # 季后赛 10 队（S1-6 + A1-4）+ A 组 5-6 名淘汰 + 卡位赛被淘汰的队伍（补齐到 18 队）
        tables = save.get("_r3_tables") or {}
        for row in (tables.get("A") or [])[4:6]:
            fallback.append(row["team"])
        cards = save.get("_r2_tables") or {}
        for key in ("S", "A", "B"):
            for row in cards.get(key) or []:
                fallback.append(row["team"])
    elif pipe == "challenger_cup":
        for name in save.get("_qualifier_order") or []:
            fallback.append(name)
    elif pipe == "annual_finals":
        groups = save.get("groups") or {}
        for name in groups.get("annual_out") or []:
            fallback.append(name)
        for match in stage_matches(save, "breakout"):
            if match.get("loser_team"):
                fallback.append(match["loser_team"])
    for name in fallback:
        if name and name not in seen:
            rows.append({"team": name, "rank": len(rows) + 1})
            seen.add(name)
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


def _bracket_champion(bracket: Dict[str, Any]) -> Optional[str]:
    if not bracket:
        return None
    rounds = bracket.get("rounds") or []
    # 有总决赛槽位就只看总决赛：没打完就不能算冠军（中间轮次的胜者不是冠军）
    for round_ in rounds:
        if round_.get("side") == "grand":
            slot = (round_.get("slots") or [{}])[0]
            return slot.get("winner") if slot.get("status") == "done" else None
    # 没有总决赛槽位（单败淘汰）时，要求最后一轮全部结束才取胜者
    last = rounds[-1] if rounds else None
    if last and last.get("slots") and all(
            (sl.get("status") == "done") for sl in last.get("slots") or []):
        return (last["slots"][0] or {}).get("winner")
    return None


def _bracket_survivors(bracket: Dict[str, Any], want: int) -> List[str]:
    """单败淘汰打到剩 want 支时的晋级名单。"""
    if not bracket:
        return []
    losses: Dict[str, int] = {}
    order: List[str] = []
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            if slot.get("status") != "done":
                continue
            loser = slot.get("loser")
            if loser:
                losses[loser] = losses.get(loser, 0) + 1
    for name in bracket.get("seeds") or []:
        if name and losses.get(name, 0) == 0:
            order.append(name)
    return order[:want]


def _double_elim_finalists(bracket: Dict[str, Any]) -> List[str]:
    if not bracket:
        return []
    w_key, l_key = None, None
    for round_ in bracket.get("rounds") or []:
        if round_.get("side") == "winners":
            w_key = round_
        if round_.get("side") == "losers":
            l_key = round_
    out = []
    if w_key and w_key.get("slots"):
        out.append((w_key["slots"][0] or {}).get("winner"))
    if l_key and l_key.get("slots"):
        out.append((l_key["slots"][0] or {}).get("winner"))
    return [t for t in out if t]


def _playoff_ranking(save: Dict[str, Any], bracket: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not bracket:
        return []
    champion = _bracket_champion(bracket)
    loser = None
    for round_ in bracket.get("rounds") or []:
        if round_.get("side") in ("grand",):
            slot = (round_.get("slots") or [{}])[0]
            if slot.get("status") == "done":
                loser = slot.get("loser")
    rows = [{"team": champion, "rank": 1}]
    if loser:
        rows.append({"team": loser, "rank": 2})
    return rows


def _crown(save: Dict[str, Any], champion: Optional[str]) -> None:
    if not champion:
        return
    save["champion"] = champion
    coach_team = (save.get("coach") or {}).get("team_name")
    if coach_team == champion:
        stats = save.setdefault("stats", {"matched": 0, "wins": 0, "losses": 0, "championship": 0})
        stats["championship"] = (_as_int(stats.get("championship"), 0) or 0) + 1
    _log(save, "🏆 冠军产生：%s" % champion)


def _finish_series(save: Dict[str, Any]) -> None:
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    series["status"] = "done"
    ranking = series.get("ranking") or []
    slot = series.get("slot")
    if slot:
        series["points"] = {}
        for row in ranking:
            team = row.get("team")
            rank = _as_int(row.get("rank"), None)
            if not team or not rank:
                continue
            pts = R.annual_points(slot, rank)
            entry = save["annual"].setdefault(team, {"spring": 0, "challenger": 0,
                                                    "autumn": 0, "total": 0, "ranks": {}})
            entry[slot] = pts
            entry.setdefault("ranks", {})[slot] = rank
            series["points"][team] = pts
    for team, entry in save["annual"].items():
        entry["total"] = sum(_as_int(entry.get(k), 0) or 0 for k in ("spring", "challenger", "autumn"))
    _log(save, "「%s」结束，年度积分已结算：%s"
         % (series.get("label") or series.get("name"),
            {k: v for k, v in list(series.get("points", {}).items())[:6]}))
    if idx + 1 >= len(save.get("series") or []):
        save["status"] = "done"


def annual_ranking(save: Dict[str, Any]) -> List[Dict[str, Any]]:
    """年度积分榜（含官方同分优先级）。"""
    rows = []
    for team, entry in (save.get("annual") or {}).items():
        ranks = entry.get("ranks") or {}
        rows.append({
            "team": team,
            "spring": _as_int(entry.get("spring"), 0) or 0,
            "challenger": _as_int(entry.get("challenger"), 0) or 0,
            "autumn": _as_int(entry.get("autumn"), 0) or 0,
            "total": _as_int(entry.get("total"), 0) or 0,
            "ranks": ranks,
            "autumn_rank": _as_int(ranks.get("autumn"), 99) or 99,
            "challenger_rank": _as_int(ranks.get("challenger"), 99) or 99,
            "spring_rank": _as_int(ranks.get("spring"), 99) or 99,
        })
    totals = _total_game_diff(save)
    for row in rows:
        row["game_diff"] = totals.get(row["team"], 0)
    rows.sort(key=lambda r: (-r["total"], r["autumn_rank"], r["challenger_rank"],
                             r["spring_rank"], -r["game_diff"], r["team"]))
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


def _total_game_diff(save: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for match in save.get("schedule") or []:
        if match.get("status") != "done":
            continue
        score = match.get("score") or {}
        sb = _as_int(score.get("blue"), 0) or 0
        sr = _as_int(score.get("red"), 0) or 0
        blue, red = match.get("blue_team"), match.get("red_team")
        if blue:
            out[blue] = out.get(blue, 0) + (sb - sr)
        if red:
            out[red] = out.get(red, 0) + (sr - sb)
    return out


# --------------------------------------------------------------------------- #
# 视图
# --------------------------------------------------------------------------- #
def simulate(save: Dict[str, Any], *, count: int = 1, until_mine: bool = False,
             include_mine: bool = False) -> int:
    """模拟 AI 场次。

    v9 补丁（用户反馈）：``count`` 必须是**硬上限**——以前循环跑 ``count*200`` 次，
    于是点一次「模拟下一场比赛」（count=1）会把本阶段所有 AI 对局一次性算完，
    玩家只看到"剩下自己的比赛"。现在：
      * ``until_mine=False``：最多模拟 ``count`` 场就停（真正一场一场出结果）；
      * ``until_mine=True``：把「我下一场比赛」之前的所有场次全部模拟掉
        （v24 修正：以前只走一场就停，和「模拟下一场」没区别）。
    """
    played = 0
    limit = max(1, int(count))
    # 每次 advance 跨阶段最多消耗几十次循环，这里给足余量但不再无限模拟
    iterations = limit * 400 if until_mine else limit * 4
    for _ in range(iterations):
        advance(save)
        if save.get("draw_gate"):            # v28：赛程闸门 —— 等玩家点按钮，停止模拟
            break
        pending = [m for m in stage_matches(save) if m.get("status") == "pending"]
        if not pending:
            if stage_complete(save):
                advance(save)
                continue
            break
        # v11：模拟必须按「比赛日 → 当天场次顺序」从上到下推进
        pending.sort(key=lambda m: (_as_int(m.get("day"), 1) or 1,
                                    _as_int(str(m.get("match_key") or "").lstrip("m"), 0) or 0))
        target = None
        for match in pending:
            if match.get("is_mine"):
                # v24（用户 m16286）：until_mine 的语义是「把我这场比赛之前的场次全部模拟掉」。
                # 旧实现写成「只要场上还存在我的待打比赛就 break」，而我的比赛从阶段生成起就是
                # pending，于是这个循环永远只走一场 —— 表现和「模拟下一场」一模一样。
                if include_mine:
                    target = match
                break
            target = match
            break
        if target is None:
            break
        score = _simulate_score(save, target)
        side = "blue" if score["blue"] > score["red"] else "red"
        apply_result(save, target["match_key"], side, score, source="sim")
        played += 1
        if played >= limit and not until_mine:
            break
    advance(save)
    return played


def next_player_match(save: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    coach_team = (save.get("coach") or {}).get("team_name")
    for match in stage_matches(save):
        if match.get("status") == "pending" and coach_team in (match.get("blue_team"), match.get("red_team")):
            return match
    return None


def schedule_view(save: Dict[str, Any], *, day: Optional[int] = None,
                  only_mine: bool = False) -> Dict[str, Any]:
    matches = stage_matches(save)
    days: Dict[int, Dict[str, Any]] = {}
    for match in matches:
        d = _as_int(match.get("day"), 1) or 1
        row = days.setdefault(d, {"day": d, "label": "第%d日" % d, "total": 0,
                                  "done": 0, "mine": 0})
        row["total"] += 1
        if match.get("status") == "done":
            row["done"] += 1
        if match.get("is_mine"):
            row["mine"] += 1
    rows = []
    for match in matches:
        if day and (_as_int(match.get("day"), 1) or 1) != int(day):
            continue
        if only_mine and not match.get("is_mine"):
            continue
        blue = save["pool"].get(match.get("blue_team")) or {}
        red = save["pool"].get(match.get("red_team")) or {}
        rows.append({
            "match_key": match.get("match_key"),
            "day": match.get("day"),
            "stage": match.get("stage"),
            "stage_label": match.get("stage_label"),
            "group": match.get("group"),
            "kind": match.get("kind"),
            "bo": match.get("bo"),
            "blue_team": match.get("blue_team"),
            "red_team": match.get("red_team"),
            "blue_logo": blue.get("logo_path"),
            "red_logo": red.get("logo_path"),
            "status": match.get("status"),
            "score": match.get("score"),
            "winner": match.get("winner"),
            "winner_team": match.get("winner_team"),
            "is_mine": match.get("is_mine"),
            "my_turn": bool(match.get("is_mine") and match.get("status") == "pending"),
            "result_source": match.get("result_source"),
            "bp_match_id": match.get("bp_match_id"),
            "bracket_round_name": match.get("bracket_round_name"),
        })
    return {
        "days": [days[k] for k in sorted(days)],
        "matches": rows,
        "current_day": save.get("current_day"),
        "total_days": save.get("total_days"),
        "current_stage": save.get("stage"),
        "current_stage_label": save.get("stage_label"),
    }


def standings(save: Dict[str, Any]) -> Dict[str, Any]:
    stage = save.get("stage") or ""
    idx = _as_int(save.get("series_index"), 0) or 0
    series = save["series"][idx]
    pipe = series.get("pipeline")
    tables = _tables_for_stage(save, stage)
    groups = []
    for key in ("G1", "G2", "G3", "S", "A", "B", "master", "elite"):
        rows = tables.get(key)
        if not rows:
            continue
        label = {"master": "大师组", "elite": "精英组"}.get(key, key)
        groups.append({"key": key, "name": label, "rows": rows})
    out = {
        "stage": stage,
        "label": save.get("stage_label"),
        "kind": save.get("stage_kind"),
        "groups": groups,
        "rules": _stage_rules(pipe, stage),
        "annual": annual_ranking(save) if save.get("annual") else [],
        "cards": save.get("cards") or [],
    }
    bracket = save.get("bracket")
    if bracket:
        out["bracket"] = bracket_view(save)
    return out


def _stage_rules(pipe: str, stage: str) -> str:
    cfg = R.rules()
    try:
        if pipe == "league_sab":
            node = (cfg.get("league_sab") or {}).get({"r1": "round1", "r2": "round2",
                                                      "cards": "cards", "r3": "round3"}.get(stage, ""), {})
            if stage == "playoffs":
                node = (cfg.get("league_sab") or {}).get("playoffs") or {}
            return node.get("rules_text") or ""
        if pipe == "challenger_cup":
            key = {"qualifier": "qualifier", "playoff": "playoff", "final": "final"}.get(stage, "")
            return ((cfg.get("challenger_cup") or {}).get(key) or {}).get("rules_text") or ""
        if pipe == "annual_finals":
            key = {"arena": "arena", "breakout": "breakout", "playoff": "playoff"}.get(stage, "")
            node = (cfg.get("annual_finals") or {}).get(key) or {}
            return node.get("rules_text") or ""
    except Exception:                                  # noqa: BLE001
        return ""
    return ""


def _src_ref(src: Any) -> Optional[str]:
    """把内部连线元组 ``(round_key, slot_no, kind)`` 序列化成 "w_r1#2.lose"。"""
    if not src:
        return None
    try:
        round_key, slot_no, kind = src
    except (TypeError, ValueError):
        return None
    return "%s#%s.%s" % (round_key, slot_no, kind or "win")


def _refresh_bracket_from_schedule(save: Dict[str, Any], bracket: Dict[str, Any],
                                   series_index: Optional[int] = None) -> Dict[str, Any]:
    """用 schedule 里的真实赛果刷新对阵图快照。

    早先阶段的图在进入下一阶段后就不再被 ``_sync_bracket`` 更新了，
    这里按 ``match_key`` 回填 score/winner，保证回看旧图时比分是真的。

    v25：``series_index`` 可显式指定要取哪一站的比赛。历史回看别人那一站时必须传，
    否则会拿「当前站的 series_index」去过滤，一张图都填不上（表现为对阵图没有队伍）。
    """
    rows = {}
    by_slot = {}
    # v22（用户 m13373）：只认本系列赛本阶段的比赛。
    # 年总包里春季赛/夏季赛都有 ``playoffs``、挑战者杯/年总都有 ``playoff``，
    # 旧实现只按「|阶段| + 轮次 + 槽位」匹配 slot_ref，会把春季赛的比赛
    # 填进夏季赛的图（「夏季赛流程图上面有着春季赛的数据」）。
    if series_index is None:
        cur_series = _as_int(save.get("series_index"), 0) or 0
    else:
        cur_series = _as_int(series_index, 0) or 0
    series_prefix = "%d|" % cur_series
    for row in save.get("schedule") or []:
        if (_as_int(row.get("series"), 0) or 0) != cur_series:
            continue
        key = row.get("match_key")
        if key:
            rows[key] = row
        ref = row.get("slot_ref")
        if ref:
            by_slot[str(ref)] = row
    # 老存档里的轮次名可能是旧算法写下的（8 队时第 1 轮叫成「胜者组决赛」），
    # 展示时按当前算法重算一遍名称，避免旧图名字颠倒。
    if bracket.get("kind") == "double_elim":
        w_rounds = [r for r in (bracket.get("rounds") or []) if str(r.get("key", "")).startswith("w_r")]
        k = len(w_rounds)
        for idx, round_ in enumerate(w_rounds, start=1):
            round_["name"] = _wb_name(idx, k)
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            key = slot.get("match_key")
            row = rows.get(key) if key else None
            if row is None:
                # 快照里下一轮槽位可能还没生成 match_key，用 slot_ref 反查
                for ref, cand in by_slot.items():
                    if ref.endswith("#%d" % int(slot.get("slot") or 0)) and ("|%s|" % bracket.get("stage")) in ref                             and ref.rsplit("|", 1)[-1].split("#")[0] == round_.get("key"):
                        row = cand
                        slot["match_key"] = cand.get("match_key")
                        break
            if not row or row.get("status") != "done":
                continue
            slot["status"] = "done"
            slot["score"] = row.get("score")
            slot["winner"] = row.get("winner_team")
            slot["loser"] = row.get("loser_team")
            slot["team"] = row.get("blue_team")
            slot["team_b"] = row.get("red_team")
            slot["result_source"] = row.get("result_source")
    # 把已结束比赛的胜者回填到下一轮（快照里下一轮可能还是 None）
    changed = True
    guard = 0
    while changed and guard < 40:
        changed = False
        guard += 1
        for round_ in bracket.get("rounds") or []:
            for slot in round_.get("slots") or []:
                for field in ("team", "team_b"):
                    if slot.get(field):
                        continue
                    src = slot.get("src_a" if field == "team" else "src_b")
                    if not src:
                        continue
                    _rk, _no, _kind = src
                    for r2 in bracket.get("rounds") or []:
                        if r2.get("key") != _rk:
                            continue
                        for s2 in r2.get("slots") or []:
                            if int(s2.get("slot") or 0) != int(_no):
                                continue
                            # v27：展示槽没有 status="done"，但它已经按来源填了队名，
                            # 下一列（「淘汰赛名额」）要能顺着它继续往右传递。
                            if s2.get("status") != "done" and not (s2.get("placeholder") and s2.get("team")):
                                continue
                            name = (s2.get("loser_team") or s2.get("loser")) if _kind == "lose"                                 else (s2.get("winner_team") or s2.get("winner") or s2.get("team"))
                            if name:
                                slot[field] = name
                                # v27：展示卡同时带上来源那场的比分（官方流程图中间列是「胜者 + 比分」）
                                if slot.get("placeholder") and _kind == "win" and s2.get("score"):
                                    slot["score"] = s2.get("score")
                                changed = True
    return bracket


def _is_last_stage(save: Dict[str, Any], stage: Optional[str]) -> bool:
    """该阶段是否为本系列赛的最后一站（决定是否展示冠军/亚军）。"""
    if not stage:
        return False
    order = _stage_order(save)
    if order:
        return order[-1] == stage
    return True


def _stage_order(save: Dict[str, Any]) -> List[str]:
    """当前系列赛各阶段的顺序（用于对阵图回退与切换 chips 排序）。"""
    try:
        idx = int(save.get("series_index") or 0)
    except (TypeError, ValueError):
        idx = 0
    series = save.get("series") or []
    if 0 <= idx < len(series):
        return list((series[idx] or {}).get("stages") or [])
    return list(STAGE_DEFS.get(((save.get("event") or {}).get("pipeline") or ["league_sab"])[0], []))


def _final_stage_round(save: Dict[str, Any], bracket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """挑战者杯这类「8 强双败之后再来一场 BO9 总决赛」的赛制：
    把下一步的单场总决赛也画进淘汰赛图里，否则图上看不到最后那一场（v14 修复）。"""
    stage = bracket.get("stage")
    order = _stage_order(save)
    if not stage or stage not in order:
        return None
    idx = order.index(stage)
    if idx + 1 >= len(order):
        return None
    next_stage = order[idx + 1]
    matches = [m for m in (save.get("schedule") or []) if m.get("stage") == next_stage]
    if len(matches) != 1:
        return None
    m = matches[0]
    rounds = bracket.get("rounds") or []
    winners = [r for r in rounds if r.get("side") == "winners"]
    losers = [r for r in rounds if r.get("side") == "losers"]

    def _tail_ref(rounds_list):
        if not rounds_list:
            return None
        last = rounds_list[-1]
        slots = last.get("slots") or []
        if not slots:
            return None
        return (last.get("key"), slots[-1].get("slot"), "win")

    src_a = _tail_ref(winners)
    src_b = _tail_ref(losers)
    team = m.get("blue_team")
    team_b = m.get("red_team")
    score = m.get("score") or {}
    winner = None
    loser = None
    if str(m.get("status")) == "done":
        wteam = m.get("winner_team")
        if wteam and wteam == team:
            winner, loser = team, team_b
        elif wteam and wteam == team_b:
            winner, loser = team_b, team
    return {
        "key": "gf",
        "name": "总决赛（BO%s）" % (m.get("bo") or 9),
        "side": "grand",
        "slots": [{
            "slot": 1,
            "team": team,
            "team_b": team_b,
            "bo": m.get("bo"),
            "status": m.get("status"),
            "winner": winner,
            "loser": loser,
            "score": {"blue": (score or {}).get("blue"), "red": (score or {}).get("red")},
            "match_key": m.get("match_key"),
            "walkover": False,
            "src_a": src_a,
            "src_b": src_b,
        }],
    }


def _bracket_book(save: Dict[str, Any]) -> Dict[str, Any]:
    """「本系列赛」可回看的对阵图，键为阶段名。

    v22（用户 m13373）：年总包里春季赛/夏季赛都有 ``playoffs``、挑战者杯/年总都有
    ``playoff``，而 ``save["brackets"]`` 只按阶段名存 → 后一站会覆盖前一站，
    于是「夏季赛季后赛」显示成春季赛的图、「年总淘汰赛」显示成挑杯的图。
    这里优先从 ``brackets_by_series``（v19 起按 ``"<站>|<阶段>"`` 存）里挑本系列赛的图；
    只有完全没有按站存档（v19 之前的老存档）时才退回旧的 ``brackets``。
    """
    idx = _as_int(save.get("series_index"), 0) or 0
    by_series = save.get("brackets_by_series") or {}
    prefix = "%d|" % idx
    picked = {k[len(prefix):]: v for k, v in by_series.items()
              if isinstance(k, str) and k.startswith(prefix) and v}
    if picked:
        return picked
    legacy = save.get("brackets") or {}
    if legacy and not by_series:
        return dict(legacy)
    return {}


def bracket_view(save: Dict[str, Any], stage: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """淘汰赛对阵图数据（v13：支持按阶段回看 + 带连线信息）。"""
    books = _bracket_book(save)
    bracket = None
    if stage and stage in books:
        bracket = books[stage]
    else:
        # v22：只认本系列赛自己的图，绝不回退到其它站（春季赛/挑杯）的同名阶段。
        bracket = save.get("bracket") or books.get(save.get("stage") or "")
        if bracket is None and books:
            order = _stage_order(save)
            for key in reversed(order):
                if key in books:
                    bracket = books[key]
                    break
            if bracket is None:
                bracket = books[sorted(books)[-1]]
    if not bracket and stage == "breakout":
        # v27 之前建的存档没有存过突围赛图，这里用实时赛果重建，老存档也能看图。
        rows = stage_matches(save, "breakout")
        if rows:
            bracket = _breakout_bracket_from_rows(save, "breakout", rows)
    if not bracket:
        return None
    # v27（用户 m16873）：年总突围赛没有真正的「下一阶段只有 1 场」的决赛，
    # 后两列（胜者 / 淘汰赛名额）是展示槽，靠这里补齐，
    # 这样 v27 之前创建的老存档也能直接看到官方三列流程图。
    if bracket.get("kind") == _BREAKOUT_KIND:
        _breakout_extra_rounds(bracket)
    bracket = _refresh_bracket_from_schedule(save, bracket)
    extra_final = _final_stage_round(save, bracket)
    rounds = []
    for round_ in bracket.get("rounds") or []:
        slots = []
        for slot in round_.get("slots") or []:
            blue = save["pool"].get(slot.get("team")) or {}
            red = save["pool"].get(slot.get("team_b")) or {}
            slots.append({
                "slot": slot.get("slot"),
                "team": slot.get("team"),
                "team_b": slot.get("team_b"),
                "team_logo": blue.get("logo_path"),
                "team_b_logo": red.get("logo_path"),
                "bo": slot.get("bo"),
                "status": slot.get("status"),
                "winner": slot.get("winner"),
                "loser": slot.get("loser"),
                "score": slot.get("score"),
                "match_key": slot.get("match_key"),
                "walkover": slot.get("walkover"),
                "src_a": _src_ref(slot.get("src_a")),
                "src_b": _src_ref(slot.get("src_b")),
                # v27：展示槽（突围赛「胜者」/「淘汰赛名额」）前端渲染成单行卡片。
                "placeholder": slot.get("placeholder"),
                "placeholder_label": slot.get("placeholder_label"),
            })
        rounds.append({"key": round_.get("key"), "name": round_.get("name"),
                       "side": round_.get("side"), "slots": slots})
    if extra_final:
        rounds.append({
            "key": extra_final.get("key"),
            "name": extra_final.get("name"),
            "side": extra_final.get("side"),
            "slots": extra_final.get("slots"),
        })
    grand = [r for r in rounds if r.get("side") == "grand"]
    champ = runner = None
    if grand and grand[-1].get("slots"):
        first = grand[-1]["slots"][0]
        if first.get("winner"):
            champ = first.get("winner")
            runner = first.get("loser")
    if champ is None and _is_last_stage(save, bracket.get("stage")):
        champ = _bracket_champion(bracket)
        runner = _bracket_runner_up(bracket)
    return {
        "kind": bracket.get("kind"),
        "stage": bracket.get("stage"),
        "stage_label": bracket.get("stage_label"),
        "bo": bracket.get("bo"),
        "rounds": rounds,
        "winners": [r for r in rounds if r["side"] == "winners"],
        "losers": [r for r in rounds if r["side"] == "losers"],
        "final": [r for r in rounds if r["side"] == "grand"],
        # 只有本系列赛最后一个阶段才显示「冠军/亚军」：
        # 挑战者杯 32 进 8、淘汰赛中途的图显示冠亚军会误导（系列赛还没打完）。
        "champion": champ,
        "runner_up": runner,
        # v27：突围赛图的三列标题（突围赛 / 胜者 / 淘汰赛），其它图用默认。
        "columns": bracket.get("bracket_columns") or [],
        "available": [{"stage": k, "label": (books[k].get("stage_label") or k),
                       "kind": books[k].get("kind")}
                      for k in sorted(books, key=lambda x: (_stage_order(save).index(x)
                                                             if x in _stage_order(save) else 99))],
    }


def _bracket_runner_up(bracket: Dict[str, Any]) -> Optional[str]:
    """亚军：优先取总决赛（grand/single 最后一轮）的败者，没有就取已结束的最后一场。"""
    rounds = bracket.get("rounds") or []
    for round_ in rounds:
        if round_.get("side") in ("grand", "single"):
            slot = (round_.get("slots") or [{}])[0]
            if slot.get("status") != "done":
                return None
            return slot.get("loser_team") or slot.get("loser")
    last = rounds[-1] if rounds else None
    if last and last.get("slots") and all(sl.get("status") == "done" for sl in last["slots"]):
        return (last["slots"][0] or {}).get("loser_team") or (last["slots"][0] or {}).get("loser")
    return None


def summary(save: Dict[str, Any]) -> Dict[str, Any]:
    idx = _as_int(save.get("series_index"), 0) or 0
    series = (save.get("series") or [{}])[min(idx, len(save.get("series") or [{}]) - 1)]
    stats = save.get("stats") or {}
    matched = _as_int(stats.get("matched"), 0) or 0
    wins = _as_int(stats.get("wins"), 0) or 0
    return {
        "save_id": save.get("save_id"),
        "kind": save.get("kind") or "tournament",
        "version": save.get("version"),
        "mode": save.get("mode"),
        "template": save.get("template"),
        "category": save.get("category"),
        "save_name": save.get("save_name"),
        "status": save.get("status"),
        "champion": save.get("champion"),
        "event": save.get("event"),
        "coach": save.get("coach"),
        "series_index": idx,
        "series_total": len(save.get("series") or []),
        "series_label": series.get("label") or series.get("name"),
        "series_status": series.get("status"),
        "stage": save.get("stage"),
        "stage_label": save.get("stage_label"),
        "stage_kind": save.get("stage_kind"),
        "stage_done": save.get("stage_done"),
        "plan": stage_plan(save),
        "current_day": save.get("current_day"),
        "total_days": save.get("total_days"),
        "stats": {
            "matched": matched, "wins": wins,
            "losses": _as_int(stats.get("losses"), 0) or 0,
            "championship": _as_int(stats.get("championship"), 0) or 0,
            "win_rate": round(100.0 * wins / matched, 1) if matched else 0.0,
        },
        "annual": annual_ranking(save),
        # v6：三类主体（玩家 / 外部大模型 / 内置 AI）要让前端能看见，用于决定每场由谁决策
        "participants": [{"name": p.get("name"), "role": p.get("role") or "ai",
                          "rank": p.get("rank")}
                         for p in (save.get("participants") or [])],
        "llm_team": next((p.get("name") for p in (save.get("participants") or [])
                          if (p.get("role") or "ai") == "llm"), None),
        "created_at": save.get("created_at"),
        "updated_at": save.get("updated_at"),
        "log": (save.get("log") or [])[-12:],
    }


# ======================================================================
# v19：历史回看（只读视图）
# ----------------------------------------------------------------------
# 需求（用户 m11169）：「打完之后看不见曾经的记录，春季赛打完只能看到季后赛
# 结果，看不到常规赛；年总包打完春季赛直接到挑杯，想看春季赛结果都看不了」。
#
# 设计原则：**只读，不改动任何既有函数的行为与返回值**。
#   * 赛程数据全部来自 save["schedule"]（跨站不清空，按 (series, stage) 过滤即可）；
#   * 积分榜优先读结榜快照 save["stage_snapshots"]["<站>|<阶段>"]（v19 起写入），
#     旧存档没有快照时按比赛里的 group 字段反推分组重算；年总擂台赛的分组
#     （大师/精英）在旧存档里已随 start_series 清空，此时合并成一张表并标注
#     merged=True，绝不编造分组；
#   * 对阵图优先读 save["brackets_by_series"]["<站>|<阶段>"]（v19 起写入），
#     回退旧的 save["brackets"][stage]，并用 match_key 交叉校验归属，避免年总
#     包里多站同名阶段（playoffs / playoff）互相串图。
# ======================================================================

_HISTORY_GROUP_LABEL = {"master": "大师组", "elite": "精英组"}
_HISTORY_TABLE_ORDER = ("G1", "G2", "G3", "S", "A", "B", "master", "elite")


def _history_key(series_index, stage):
    return "%d|%s" % (_as_int(series_index, 0) or 0, stage or "")


def _history_group_label(key):
    return _HISTORY_GROUP_LABEL.get(key, key)


def _series_stage_matches(save, series_index, stage):
    """按「站 + 阶段」取比赛（跨站历史数据都在 save["schedule"] 里）。"""
    idx = _as_int(series_index, 0) or 0
    return [m for m in (save.get("schedule") or [])
            if (_as_int(m.get("series"), 0) or 0) == idx and m.get("stage") == stage]


def _history_stage_status(matches):
    total = len(matches)
    if not total:
        return "none", 0
    done = sum(1 for m in matches if m.get("status") == "done")
    if done >= total:
        return "done", done
    if done <= 0:
        return "pending", 0
    return "running", done


def _history_groups(save, series_index, stage, matches):
    """重建某站某阶段的分组名单。

    优先用结榜快照（v19 起写入，含 r1 的 G1/G2/G3、年总的 master/elite）；
    没有快照就按比赛自带的 ``group`` 字段反推（r1 → G1..G3，r2/r3 → S/A/B）。
    """
    snap = (save.get("stage_snapshots") or {}).get(_history_key(series_index, stage)) or {}
    snap_tables = snap.get("tables") or {}
    if snap_tables:
        out = {}
        for key, rows in snap_tables.items():
            out[key] = [r.get("team") for r in rows if r.get("team")]
        return out
    groups = {}
    for match in matches:
        key = match.get("group")
        if not key or key == "cross":
            continue
        bucket = groups.setdefault(key, [])
        for name in (match.get("blue_team"), match.get("red_team")):
            if name and name not in bucket:
                bucket.append(name)
    return groups


def _history_tables(save, series_index, stage, matches):
    """某站某阶段的积分榜（列表，元素 {key, name, rows, merged?}）。"""
    snap = (save.get("stage_snapshots") or {}).get(_history_key(series_index, stage)) or {}
    snap_tables = snap.get("tables") or {}
    out = []
    if snap_tables:
        for key in _HISTORY_TABLE_ORDER:
            rows = snap_tables.get(key)
            if rows:
                out.append({"key": key, "name": _history_group_label(key),
                            "rows": [dict(r) for r in rows]})
        if out:
            return out
    groups = _history_groups(save, series_index, stage, matches)
    if stage == "arena" and not (groups.get("master") or groups.get("elite")):
        # 旧存档：大师/精英分组已随 start_series 清空 → 合并统计（数值真实，不编造分组）
        teams = []
        for match in matches:
            for name in (match.get("blue_team"), match.get("red_team")):
                if name and name not in teams:
                    teams.append(name)
        if teams:
            out.append({"key": "all", "name": "大师组 + 精英组（旧存档分组已清理，合并统计）",
                        "rows": _table_rows(save, teams, matches, False), "merged": True})
        return out
    mix = (stage == "arena")
    for key in _HISTORY_TABLE_ORDER:
        teams = groups.get(key) or []
        if not teams:
            continue
        if mix:
            rows = _table_rows(save, teams, matches, True)
        else:
            rows = _table_rows(save, teams, [m for m in matches if m.get("group") == key])
        if rows:
            out.append({"key": key, "name": _history_group_label(key), "rows": rows})
    return out


def _history_ranking(stage, tables):
    """阶段整体名次（循环赛阶段由积分榜合并得出；淘汰赛阶段由对阵图给出）。"""
    if stage in ("cards", "breakout"):
        return []
    rows = []
    for group in tables:
        for row in group.get("rows") or []:
            rows.append(dict(row, group=group.get("key")))
    if not rows:
        return []
    if stage == "r1":
        # 第一轮：各组同名次合并（1-2 名进 S、3-4 名进 A、5-6 名进 B）
        rows.sort(key=lambda r: (r.get("rank") or 99, -(r.get("points") or 0),
                                 -(r.get("game_diff") or 0), r.get("team") or ""))
        for pos, row in enumerate(rows, start=1):
            row["rank"] = pos
    return rows


def _history_ranking_from_matches(stage, matches):
    """v19 兼容：没有对阵图快照的旧存档，从**比赛记录本身**反推淘汰赛名次。

    只用于历史回看（只读展示），不写回存档、不参与 `_final_ranking` 等既有逻辑。
    排序规则与引擎既有的 `_bracket_ranking()` 完全一致，保证「旧存档回看的名次」
    和「新存档回看的名次」不会互相矛盾：

    * 冠军 = 总决赛（``gf``）胜者；没有总决赛时取最后一轮胜者（该轮须全部结束）；
    * 其余队伍按 `(-胜场, -最后被淘汰的轮次位置, 队名)` 排序。

    数据全部来自真实赛果（``winner_team`` / ``loser_team`` / ``bracket_round``），
    不做任何推断性编造。
    """
    done = [m for m in matches
            if m.get("status") == "done" and m.get("winner_team")]
    if not done:
        return []

    def round_key(match):
        key = str(match.get("bracket_round") or "")
        if key:
            return key
        ref = str(match.get("slot_ref") or "")
        return ref.split("|")[-1].split("#")[0] if "|" in ref else ref

    # 轮次先后：与 _make_double_elim / _make_split_double_elim 的定义顺序一致
    # —— 先胜者组 w_r1..w_rk，再败者组 l_r1..，最后总决赛 gf。
    def round_order(key):
        if key == "gf":
            return (2, 0)
        if key.startswith("w_r"):
            return (0, _as_int(key[3:], 0) or 0)
        if key.startswith("l_r"):
            return (1, _as_int(key[3:], 0) or 0)
        if key.startswith("s_r"):
            return (0, _as_int(key[3:], 0) or 0)
        return (3, 0)

    keys = sorted({round_key(m) for m in done}, key=round_order)
    pos_of = {k: i for i, k in enumerate(keys)}
    wins, last_loss, seen = {}, {}, []

    def touch(name):
        if name and name not in seen:
            seen.append(name)

    for match in done:
        pos = pos_of.get(round_key(match), 0)
        winner, loser = match.get("winner_team"), match.get("loser_team")
        touch(winner)
        touch(loser)
        if winner:
            wins[winner] = wins.get(winner, 0) + 1
        if loser:
            last_loss[loser] = max(last_loss.get(loser, -1), pos)

    # 冠军：优先总决赛胜者；否则要求最后一轮全部结束
    champion = None
    grands = [m for m in done if round_key(m) == "gf"]
    if grands:
        champion = grands[-1].get("winner_team")
    elif keys:
        last = keys[-1]
        same = [m for m in done if round_key(m) == last]
        all_slots = [m for m in matches if round_key(m) == last]
        if same and len(same) == len(all_slots):
            champion = same[0].get("winner_team")

    rows = []
    if champion:
        rows.append({"team": champion, "rank": 1})
    rest = [t for t in seen if t != champion]
    rest.sort(key=lambda t: (-wins.get(t, 0), -last_loss.get(t, -1), t))
    for pos, name in enumerate(rest, start=len(rows) + 1):
        rows.append({"team": name, "rank": pos})
    return rows


_KO_ROUND_PREFIX_SIDE = (("w_r", "winners"), ("l_r", "losers"), ("s_r", "single"))


def _round_side_of(key, fallback="winners"):
    for prefix, side in _KO_ROUND_PREFIX_SIDE:
        if key.startswith(prefix):
            return side
    if key == "gf":
        return "grand"
    return fallback


def _ko_play_order(seen_keys, kind):
    """按官方播放顺序给「真实出现过的轮次」排序（构造历史对阵图用）。"""
    seen = list(seen_keys)
    k = len([key for key in seen if key.startswith("w_r")])
    if kind == "split_double_elim":
        order = list(_SPLIT_PLAY_ORDER)
    elif kind == "double_elim":
        order = list(_STD_PLAY_ORDER.get(k) or [])
    else:
        order = []
    ordered = [key for key in order if key in seen]
    for key in seen:
        if key not in ordered:
            ordered.append(key)
    return ordered


def _wire_split_double_elim(bracket):
    """10 队双败（联赛）的连线：与 `_make_split_double_elim` 内的 set_src 完全一致。"""
    index = {(r["key"], s["slot"]) for r in bracket["rounds"] for s in r["slots"]}

    def ref(round_key, slot_no, kind_):
        return (round_key, slot_no, kind_) if (round_key, slot_no) in index else None

    def set_src(key, slot_no, a, b):
        for round_ in bracket["rounds"]:
            if round_["key"] != key:
                continue
            for slot in round_["slots"]:
                if slot["slot"] == slot_no:
                    slot["src_a"] = a
                    slot["src_b"] = b

    set_src("w_r2", 1, ref("w_r1", 1, "win"), ref("w_r1", 2, "win"))
    set_src("l_r2", 1, ref("l_r1", 1, "win"), None)
    set_src("l_r2", 2, ref("l_r1", 2, "win"), None)
    set_src("l_r3", 1, ref("l_r2", 1, "win"), ref("w_r1", 1, "lose"))
    set_src("l_r3", 2, ref("l_r2", 2, "win"), ref("w_r1", 2, "lose"))
    set_src("l_r4", 1, ref("l_r3", 1, "win"), ref("l_r3", 2, "win"))
    set_src("l_r5", 1, ref("l_r4", 1, "win"), ref("w_r2", 1, "lose"))
    set_src("gf", 1, ref("w_r2", 1, "win"), ref("l_r5", 1, "win"))


def _history_bracket_from_matches(save, series_index, stage, matches):
    """v25（用户 m16430）：快照缺失时，按真实比赛记录重建对阵图。

    旧存档（v19 之前没有 ``brackets_by_series``）或者阶段被跳过的存档，回看淘汰赛
    只能看到一句「快照未留档」+ 名次表，用户会觉得「流程图没有队伍」。这里直接用
    schedule 里每场比赛自带的 ``bracket_round`` / ``slot_ref`` / 比分 / 胜负重建，
    保证任何存档的历史回看都能看到有队伍的流程图。
    """
    if not matches:
        return None
    kind_by_stage = {"playoffs": "split_double_elim", "playoff": "double_elim",
                     "qualifier": "single_elim"}
    kind = kind_by_stage.get(stage)
    if kind is None:
        # 兼容：用比赛自己的 kind 字段
        kinds = {str(m.get("kind") or "") for m in matches}
        for cand in ("split_double_elim", "double_elim", "single_elim"):
            if cand in kinds:
                kind = cand
                break
    if kind is None:
        return None

    grouped = {}
    names = {}
    for match in matches:
        round_key = str(match.get("bracket_round") or "")
        if not round_key:
            ref = str(match.get("slot_ref") or "")
            if "|" in ref:
                round_key = ref.rsplit("|", 1)[-1].split("#")[0]
        if not round_key:
            continue
        slot_no = 1
        ref = str(match.get("slot_ref") or "")
        if "#" in ref:
            try:
                slot_no = int(ref.rsplit("#", 1)[-1]) or 1
            except (TypeError, ValueError):
                slot_no = 1
        side = str(match.get("bracket_side") or _round_side_of(round_key))
        rname = match.get("bracket_round_name") or round_key
        names.setdefault(round_key, rname)
        items = grouped.setdefault((side, round_key), {})
        items[slot_no] = match
    if not grouped:
        return None

    seen_keys = []
    for (_side, round_key) in grouped:
        if round_key not in seen_keys:
            seen_keys.append(round_key)
    ordered = _ko_play_order(seen_keys, kind)

    rounds = []
    for round_key in ordered:
        pair = next(((side, rk) for (side, rk) in grouped if rk == round_key), None)
        if pair is None:
            continue
        side, _rk = pair
        items = grouped[pair]
        slots = []
        for slot_no in sorted(items):
            match = items[slot_no]
            score = match.get("score") or {}
            done = str(match.get("status")) == "done"
            winner = match.get("winner_team") if done else None
            loser = match.get("loser_team") if done else None
            slot = _slot_rest(int(match.get("bo") or 7))
            slot.update({
                "slot": slot_no,
                "team": match.get("blue_team"),
                "team_b": match.get("red_team"),
                "bo": int(match.get("bo") or 7),
                "status": "done" if done else "pending",
                "winner": winner,
                "loser": loser,
                "score": {"blue": score.get("blue"), "red": score.get("red")} if done else None,
                "match_key": match.get("match_key"),
                "walkover": False,
            })
            slots.append(slot)
        rounds.append({"key": round_key, "name": names.get(round_key) or round_key,
                       "side": side, "slots": slots})
    if not rounds:
        return None

    bracket = {
        "kind": kind,
        "stage": stage,
        "stage_label": matches[0].get("stage_label") or stage,
        "bo": int(matches[0].get("bo") or 7),
        "rounds": rounds,
        "seeds": [],
        "winners_champion": None,
        "losers_champion": None,
        "has_final": any(r["key"] == "gf" for r in rounds),
        "rebuilt": True,
    }
    try:
        if kind == "split_double_elim":
            _wire_split_double_elim(bracket)
        elif kind == "double_elim":
            k = len([r for r in rounds if r["key"].startswith("w_r")])
            if k >= 2:
                _wire_standard_double_elim(bracket, k)
        else:
            index = {(r["key"], s["slot"]) for r in rounds for s in r["slots"]}
            for round_ in rounds:
                if not round_["key"].startswith("s_r"):
                    continue
                n = int(round_["key"][3:] or 1)
                if n <= 1:
                    continue
                for slot in round_["slots"]:
                    i = slot["slot"]
                    prev_a = ("s_r%d" % (n - 1), i * 2 - 1)
                    prev_b = ("s_r%d" % (n - 1), i * 2)
                    if prev_a in index:
                        slot["src_a"] = (prev_a[0], prev_a[1], "win")
                    if prev_b in index:
                        slot["src_b"] = (prev_b[0], prev_b[1], "win")
    except Exception as exc:                                # noqa: BLE001
        LOGGER.warning("[tourney] 重建历史对阵图连线失败：%s", exc)
    return bracket


def _bracket_touches(bracket, match_keys):
    """对阵图快照是否属于这批比赛（用 match_key 交叉校验，避免多站同名阶段串图）。"""
    if not bracket or not match_keys:
        return False
    for round_ in bracket.get("rounds") or []:
        for slot in round_.get("slots") or []:
            key = slot.get("match_key")
            if key and key in match_keys:
                return True
    return False


def _find_bracket(save, series_index, stage, matches):
    """定位某站某阶段的淘汰赛图快照（只做查找，不刷新内容）。"""
    key = _history_key(series_index, stage)
    cand = (save.get("brackets_by_series") or {}).get(key)
    if cand:
        return cand
    books = save.get("brackets") or {}
    want = {m.get("match_key") for m in matches if m.get("match_key")}
    fallback = books.get(stage)
    if fallback and _bracket_touches(fallback, want):
        return fallback
    for other in books.values():
        if _bracket_touches(other, want):
            return other
    return None


def _history_final_round(save, series_index, bracket):
    """挑战者杯这类「双败之后单独一场 BO9 总决赛」：把下一阶段那唯一一场也画进图里。

    与 ``_final_stage_round`` 的区别是严格按「同一站」过滤比赛（历史回看时其它
    站的总决赛不会被误抓进来）。
    """
    stage = bracket.get("stage")
    series_list = save.get("series") or []
    idx = _as_int(series_index, 0) or 0
    if not (0 <= idx < len(series_list)):
        return None
    order = [k for k, _l, _kind in STAGE_DEFS.get(series_list[idx].get("pipeline"), [])]
    if not stage or stage not in order:
        return None
    pos = order.index(stage)
    if pos + 1 >= len(order):
        return None
    matches = _series_stage_matches(save, idx, order[pos + 1])
    if len(matches) != 1:
        return None
    match = matches[0]
    rounds = bracket.get("rounds") or []
    winners = [r for r in rounds if r.get("side") == "winners"]
    losers = [r for r in rounds if r.get("side") == "losers"]

    def _tail_ref(rounds_list):
        if not rounds_list:
            return None
        last = rounds_list[-1]
        slots = last.get("slots") or []
        if not slots:
            return None
        return (last.get("key"), slots[-1].get("slot"), "win")

    team = match.get("blue_team")
    team_b = match.get("red_team")
    winner = loser = None
    if str(match.get("status")) == "done":
        wteam = match.get("winner_team")
        if wteam and wteam == team:
            winner, loser = team, team_b
        elif wteam and wteam == team_b:
            winner, loser = team_b, team
    score = match.get("score") or {}
    return {
        "key": "gf",
        "name": "总决赛（BO%s）" % (match.get("bo") or 9),
        "side": "grand",
        "slots": [{
            "slot": 1,
            "team": team,
            "team_b": team_b,
            "bo": match.get("bo"),
            "status": match.get("status"),
            "winner": winner,
            "loser": loser,
            "score": {"blue": score.get("blue"), "red": score.get("red")},
            "match_key": match.get("match_key"),
            "walkover": False,
            "src_a": _tail_ref(winners),
            "src_b": _tail_ref(losers),
        }],
    }


def _bracket_payload(save, bracket, series_index, extra_final=None):
    """把对阵图快照整理成前端 ``KPLBracket`` 需要的结构（与 bracket_view 输出同形）。"""
    rounds = []
    for round_ in bracket.get("rounds") or []:
        slots = []
        for slot in round_.get("slots") or []:
            blue = (save.get("pool") or {}).get(slot.get("team")) or {}
            red = (save.get("pool") or {}).get(slot.get("team_b")) or {}
            slots.append({
                "slot": slot.get("slot"),
                "team": slot.get("team"),
                "team_b": slot.get("team_b"),
                "team_logo": blue.get("logo_path"),
                "team_b_logo": red.get("logo_path"),
                "bo": slot.get("bo"),
                "status": slot.get("status"),
                "winner": slot.get("winner"),
                "loser": slot.get("loser"),
                "score": slot.get("score"),
                "match_key": slot.get("match_key"),
                "walkover": slot.get("walkover"),
                "src_a": _src_ref(slot.get("src_a")),
                "src_b": _src_ref(slot.get("src_b")),
                # v27：展示槽（突围赛「胜者」/「淘汰赛名额」）前端渲染成单行卡片。
                "placeholder": slot.get("placeholder"),
                "placeholder_label": slot.get("placeholder_label"),
            })
        rounds.append({"key": round_.get("key"), "name": round_.get("name"),
                       "side": round_.get("side"), "slots": slots})
    if extra_final:
        rounds.append({"key": extra_final.get("key"), "name": extra_final.get("name"),
                       "side": extra_final.get("side"), "slots": extra_final.get("slots")})
    grand = [r for r in rounds if r.get("side") == "grand"]
    champion = runner_up = None
    if grand and grand[-1].get("slots"):
        first = grand[-1]["slots"][0]
        if first.get("winner"):
            champion = first.get("winner")
            runner_up = first.get("loser")
    series_list = save.get("series") or []
    idx = _as_int(series_index, 0) or 0
    last_stage = None
    if 0 <= idx < len(series_list):
        order = [k for k, _l, _kind in STAGE_DEFS.get(series_list[idx].get("pipeline"), [])]
        last_stage = order[-1] if order else None
    if champion is None and last_stage and bracket.get("stage") == last_stage:
        champion = _bracket_champion(bracket)
        runner_up = _bracket_runner_up(bracket)
    return {
        "kind": bracket.get("kind"),
        "stage": bracket.get("stage"),
        "stage_label": bracket.get("stage_label"),
        "bo": bracket.get("bo"),
        "rounds": rounds,
        "winners": [r for r in rounds if r["side"] == "winners"],
        "losers": [r for r in rounds if r["side"] == "losers"],
        "final": [r for r in rounds if r["side"] == "grand"],
        "champion": champion,
        "runner_up": runner_up,
        "columns": bracket.get("bracket_columns") or [],
    }


def _history_match_row(save, match):
    """历史赛程行（字段与 schedule_view 的行保持一致，另加 series 便于前端分组）。"""
    pool = save.get("pool") or {}
    blue = pool.get(match.get("blue_team")) or {}
    red = pool.get(match.get("red_team")) or {}
    return {
        "match_key": match.get("match_key"),
        "series": _as_int(match.get("series"), 0) or 0,
        "day": match.get("day"),
        "stage": match.get("stage"),
        "stage_label": match.get("stage_label"),
        "group": match.get("group"),
        "kind": match.get("kind"),
        "bo": match.get("bo"),
        "blue_team": match.get("blue_team"),
        "red_team": match.get("red_team"),
        "blue_logo": blue.get("logo_path"),
        "red_logo": red.get("logo_path"),
        "status": match.get("status"),
        "score": match.get("score"),
        "winner": match.get("winner"),
        "winner_team": match.get("winner_team"),
        "loser_team": match.get("loser_team"),
        "is_mine": match.get("is_mine"),
        "result_source": match.get("result_source"),
        "bp_match_id": match.get("bp_match_id"),
        "bracket_round_name": match.get("bracket_round_name"),
    }


def _history_days(rows):
    days = {}
    for row in rows:
        day = _as_int(row.get("day"), 1) or 1
        item = days.setdefault(day, {"day": day, "label": "第%d日" % day,
                                     "total": 0, "done": 0, "mine": 0})
        item["total"] += 1
        if row.get("status") == "done":
            item["done"] += 1
        if row.get("is_mine"):
            item["mine"] += 1
    return [days[k] for k in sorted(days)]


def history_index(save: Dict[str, Any]) -> Dict[str, Any]:
    """历史回看索引：所有系列赛 × 所有阶段（不含逐场明细，明细见 history_stage）。

    纯只读，不修改 ``save``。
    """
    series_list = save.get("series") or []
    cur_idx = _as_int(save.get("series_index"), 0) or 0
    cur_stage = save.get("stage") or ""
    coach_team = (save.get("coach") or {}).get("team_name")
    out_series: List[Dict[str, Any]] = []
    for pos, series in enumerate(series_list):
        pipe = series.get("pipeline")
        stages: List[Dict[str, Any]] = []
        for key, label, kind in STAGE_DEFS.get(pipe, []):
            matches = _series_stage_matches(save, pos, key)
            status, done = _history_stage_status(matches)
            ranking = series.get("ranking") or []
            stages.append({
                "key": key,
                "label": label,
                "kind": kind,
                "bo": R.bo_of(BO_KEY.get((pipe, key), "regular"), 5),
                "status": status,
                "total": len(matches),
                "done": done,
                "mine": sum(1 for m in matches if m.get("is_mine")),
                "started": bool(matches),
                "is_current": bool(pos == cur_idx and key == cur_stage),
                "bracket_available": _find_bracket(save, pos, key, matches) is not None,
            })
        ranking = series.get("ranking") or []
        champion = None
        if ranking:
            top = sorted(ranking, key=lambda r: _as_int(r.get("rank"), 99) or 99)
            champion = (top[0] or {}).get("team") if top else None
        if not ranking and (series.get("status") or "") == "done":
            # 挑战者杯的冠军写在最后一场 BO9 总决赛里（不写 series["ranking"]），
            # 这里按真实赛果补一个「冠 / 亚」两行名次，绝不推断别的队伍。
            last_key = None
            for key, _label, _kind in STAGE_DEFS.get(pipe, []):
                if _series_stage_matches(save, pos, key):
                    last_key = key
            finals = _series_stage_matches(save, pos, last_key) if last_key else []
            if len(finals) == 1 and finals[0].get("status") == "done" and finals[0].get("winner_team"):
                winner = finals[0]["winner_team"]
                loser = finals[0].get("loser_team")
                champion = winner
                ranking = [{"team": winner, "rank": 1}] + ([{"team": loser, "rank": 2}] if loser else [])
        out_series.append({
            "index": pos,
            "key": series.get("key"),
            "slot": series.get("slot"),
            "name": series.get("name"),
            "label": series.get("label") or series.get("name"),
            "pipeline": pipe,
            "status": series.get("status") or "pending",
            "is_current": pos == cur_idx,
            "champion": champion,
            "ranking": ranking,
            "points": series.get("points") or {},
            "stages": stages,
            "total": sum(1 for m in (save.get("schedule") or [])
                         if (_as_int(m.get("series"), 0) or 0) == pos),
            "done": sum(1 for m in (save.get("schedule") or [])
                        if (_as_int(m.get("series"), 0) or 0) == pos
                        and m.get("status") == "done"),
        })
    return {
        "save_id": save.get("save_id"),
        "save_name": save.get("save_name"),
        "template": save.get("template"),
        "category": save.get("category"),
        "status": save.get("status"),
        "champion": save.get("champion"),
        "coach_team": coach_team,
        "current": {"series_index": cur_idx, "stage": cur_stage},
        "series": out_series,
        "annual": annual_ranking(save) if save.get("annual") else [],
    }


def history_stage(save: Dict[str, Any], series_index: int, stage: str) -> Dict[str, Any]:
    """历史回看明细：某一站某一阶段的赛程 / 积分榜 / 阶段名次 / 对阵图。

    纯只读，不修改 ``save``（对阵图刷新只在内存副本上做）。
    """
    idx = _as_int(series_index, 0) or 0
    series_list = save.get("series") or []
    if not (0 <= idx < len(series_list)):
        raise TourneyError("系列赛序号超出范围：%s" % series_index)
    series = series_list[idx]
    pipe = series.get("pipeline")
    defs = STAGE_DEFS.get(pipe, [])
    labels = {k: (l, kd) for k, l, kd in defs}
    if stage not in labels:
        raise TourneyError("阶段不存在：%s（%s）" % (stage, pipe))
    label, kind = labels[stage]
    matches = _series_stage_matches(save, idx, stage)
    rows = sorted((_history_match_row(save, m) for m in matches),
                  key=lambda r: ((_as_int(r.get("day"), 1) or 1), r.get("match_key") or ""))
    tables = _history_tables(save, idx, stage, matches)
    ranking = _history_ranking(stage, tables)
    bracket_raw = _find_bracket(save, idx, stage, matches)
    if bracket_raw is None and stage == "breakout" and matches:
        # v27：v27 之前的老存档没存过突围赛图，用真实赛果重建（回看也能看到三列图）。
        bracket_raw = _breakout_bracket_from_rows(save, stage, matches, label)
    bracket = None
    if bracket_raw:
        # 历史回看是只读的：在深拷贝上刷新比分，绝不改动存档里的原始快照
        refreshed = _refresh_bracket_from_schedule(save, copy.deepcopy(bracket_raw), idx)
        extra_final = _history_final_round(save, idx, refreshed)
        bracket = _bracket_payload(save, refreshed, idx, extra_final)
        if not ranking:
            ranking = _bracket_ranking(refreshed)
    if bracket is None:
        # v25（用户 m16430）：没有任何快照（v19 之前的老存档 / 阶段被跳过）时，
        # 直接用真实比赛记录重建一张对阵图，保证历史回看能看到队伍。
        rebuilt = _history_bracket_from_matches(save, idx, stage, matches)
        if rebuilt:
            extra_final = _history_final_round(save, idx, rebuilt)
            bracket = _bracket_payload(save, rebuilt, idx, extra_final)
            if not ranking:
                ranking = _bracket_ranking(rebuilt)
    if not ranking and kind == "final" and len(matches) == 1:
        # 单场总决赛（挑战者杯 BO9）：名次直接取真实赛果，不推断其它队伍
        match = matches[0]
        if match.get("status") == "done" and match.get("winner_team"):
            ranking = [{"team": match["winner_team"], "rank": 1}]
            if match.get("loser_team"):
                ranking.append({"team": match["loser_team"], "rank": 2})
    if not ranking and kind in ("single_elim", "double_elim", "split_double_elim", "final"):
        # v19 兼容：旧存档没有对阵图快照（`brackets_by_series` 是 v19 才有的），
        # 此时按真实比赛记录反推名次，避免「打得完却看不到结果」。
        ranking = _history_ranking_from_matches(stage, matches)
    status, done = _history_stage_status(matches)
    rules = ""
    try:
        rules = _stage_rules(pipe, stage)
    except Exception:                                       # noqa: BLE001
        rules = ""
    return {
        "series_index": idx,
        "series_key": series.get("key"),
        "series_label": series.get("label") or series.get("name"),
        "series_status": series.get("status") or "pending",
        "stage": stage,
        "stage_label": label,
        "stage_kind": kind,
        "bo": R.bo_of(BO_KEY.get((pipe, stage), "regular"), 5),
        "status": status,
        "total": len(matches),
        "done": done,
        "is_current": bool(idx == (_as_int(save.get("series_index"), 0) or 0)
                           and stage == (save.get("stage") or "")),
        "rules": rules,
        "days": _history_days(rows),
        "matches": rows,
        "tables": tables,
        "ranking": ranking,
        "bracket": bracket,
        "bracket_available": bool(bracket),
    }
