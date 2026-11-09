# -*- coding: utf-8 -*-
"""KPL 赛制规则加载器（规则与应用分离）。

设计原则
--------
1. **所有赛制数值都在 `data/kpl_rules.json` 里**，本模块只做读取 + 派生计算，
   代码里不出现任何「积分 = 100」这类硬编码常量，方便后续直接导入官方细则。
2. 文件按 mtime 缓存：改完 JSON 保存即生效，不用重启进程（下次读取自动重载）。
3. 读不到 / 解析失败时返回内置兜底值并记录日志，**绝不让页面卡死**。

附：KPL 完整赛制速查（与 kpl_rules.json 一一对应，方便人工核对）
---------------------------------------------------------------
年度积分（名次 → 积分）
  春季赛 : 1=100  2=80  3-4=60  5-6=40  7-8=20  9-10=10  11-18=0
  挑战者杯: 1=85   2=60  3=40   4=20    5-6=10 7 及以后=0
  夏季赛 : 1=120  2=100 3-4=80  5-6=50  7-8=30  9-10=20  11-18=0
  年总入围：总分前 12；1-6 进大师组，7-12 进精英组。
  年度积分同分优先级：夏季赛名次 → 挑战者杯名次 → 春季赛名次
                      → 三项合计净胜小局 → 相互交手胜负 → 加赛。

联赛（春 / 秋）SAB 三阶段
  第一轮：18 队抽 3 组，组内 BO5 单循环，胜 1 分；各组 1-2 → S、3-4 → A、5-6 → B。
  第二轮：S/A/B 组内 BO5 单循环。
  卡位赛：S5-A2、S6-A1、A5-B2、A6-B1，BO7；胜者升组，B 组败者淘汰。
  第三轮：S、A 组内 BO5 单循环（第二轮积分清空）；
          S 前 4 → 季后赛胜者组，S5-6 与 A 前 4 → 败者组，A5-6 淘汰。
  季后赛：BO7 双败淘汰，胜者组败者掉败者组，败者组败者淘汰。

挑战者杯
  32 队 32 进 8 单败（BO5），春季赛冠亚军为 1/2 号种子固定半区；
  8 队双败淘汰（BO7）；总决赛 BO9（4:4 巅峰对决）。

年度总决赛
  12 队（大师组 1-6、精英组 7-12）；擂台赛分组 BO5 单循环；
  大师前 4 + 精英第 1 直接进淘汰赛；大师 5-6、精英 2-5 进突围赛（BO7 单败）；
  精英第 6 淘汰；淘汰赛 8 队 BO7 双败。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("kpl.rules")

from . import paths  # v10：打包后数据目录在 exe 同级

BASE_DIR = paths.resource_dir()
RULES_PATH = paths.data_path("kpl_rules.json")

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"mtime": None, "rules": None}

# 兜底规则：仅在 kpl_rules.json 缺失/损坏时使用，保证引擎仍能跑起来。
_FALLBACK: Dict[str, Any] = {
    "bo": {"regular": 5, "card": 7, "playoff": 7, "challenger_qualifier": 5,
           "challenger_playoff": 7, "challenger_final": 9,
           "annual_arena": 5, "annual_breakout": 7, "annual_playoff": 7},
    "annual_points": {
        "spring": {"default": 0, "brackets": [
            {"from": 1, "to": 1, "points": 100}, {"from": 2, "to": 2, "points": 80},
            {"from": 3, "to": 4, "points": 60}, {"from": 5, "to": 6, "points": 40},
            {"from": 7, "to": 8, "points": 20}, {"from": 9, "to": 10, "points": 10}]},
        "challenger": {"default": 0, "brackets": [
            {"from": 1, "to": 1, "points": 85}, {"from": 2, "to": 2, "points": 60},
            {"from": 3, "to": 3, "points": 40}, {"from": 4, "to": 4, "points": 20},
            {"from": 5, "to": 6, "points": 10}]},
        "autumn": {"default": 0, "brackets": [
            {"from": 1, "to": 1, "points": 120}, {"from": 2, "to": 2, "points": 100},
            {"from": 3, "to": 4, "points": 80}, {"from": 5, "to": 6, "points": 50},
            {"from": 7, "to": 8, "points": 30}, {"from": 9, "to": 10, "points": 20}]},
    },
    "league_sab": {"team_count": 18, "groups_round1": 3},
    "challenger_cup": {"team_count": 32},
    "annual_finals": {"team_count": 12},
    "templates": [
        {"key": "spring", "name": "2026 春季赛", "category": "league",
         "short": "春季赛", "pipeline": ["league_sab"], "team_count": 18,
         "annual_slot": "spring", "desc": "18 支 · SAB 三阶段 + 季后赛"},
        {"key": "autumn", "name": "2026 夏季赛", "category": "league",
         "short": "夏季赛", "pipeline": ["league_sab"], "team_count": 18,
         "annual_slot": "autumn", "desc": "18 支 · SAB 三阶段 + 季后赛"},
        {"key": "challenger", "name": "2026 挑战者杯", "category": "cup",
         "short": "挑战者杯", "pipeline": ["challenger_cup"], "team_count": 32,
         "annual_slot": "challenger", "desc": "32 支 · 单败进 8 + 双败 + BO9 决赛"},
        {"key": "annual_finals", "name": "2026 年度总决赛", "category": "cup",
         "short": "年度总决赛", "pipeline": ["annual_finals"], "team_count": 12,
         "annual_slot": None, "desc": "12 支 · 擂台赛 + 突围赛 + 淘汰赛"},
        {"key": "annual_package", "name": "2026 年度赛程包", "category": "package",
         "short": "年度赛程包",
         "pipeline": ["league_sab", "challenger_cup", "league_sab", "annual_finals"],
         "team_count": 18, "annual_slot": None, "desc": "春 → 挑杯 → 夏 → 年总"},
    ],
}


def _load_raw() -> Dict[str, Any]:
    """读规则文件（按 mtime 缓存）。任何异常都回退到内置规则，并写日志。"""
    with _LOCK:
        try:
            mtime = os.path.getmtime(RULES_PATH)
        except OSError:
            if _CACHE["rules"] is None:
                LOGGER.warning("[rules] 找不到 %s，使用内置兜底规则", RULES_PATH)
                _CACHE["rules"] = _FALLBACK
            return _CACHE["rules"]
        if _CACHE["rules"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["rules"]
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("规则文件根节点必须是对象")
            _CACHE["rules"] = data
            _CACHE["mtime"] = mtime
            LOGGER.info("[rules] 已加载 %s（%d 个模板）", os.path.basename(RULES_PATH),
                        len(data.get("templates") or []))
            return data
        except Exception as exc:                       # noqa: BLE001
            LOGGER.warning("[rules] 解析 %s 失败，使用内置兜底规则：%s", RULES_PATH, exc)
            if _CACHE["rules"] is None:
                _CACHE["rules"] = _FALLBACK
            return _CACHE["rules"]


def rules() -> Dict[str, Any]:
    """当前生效的完整规则（只读，请勿原地修改）。"""
    return _load_raw()


def reload_rules() -> Dict[str, Any]:
    """强制重读（设置页「重载规则」按钮用）。"""
    with _LOCK:
        _CACHE["mtime"] = None
    return _load_raw()


# --------------------------------------------------------------------------- #
# 模板 / BO / 阶段
# --------------------------------------------------------------------------- #
def templates() -> List[Dict[str, Any]]:
    return list(rules().get("templates") or [])


def template(key: str) -> Optional[Dict[str, Any]]:
    for row in templates():
        if row.get("key") == key:
            return dict(row)
    return None


def template_teams(key: str) -> int:
    row = template(key) or {}
    count = row.get("team_count")
    if count:
        return int(count)
    return int(rules().get("league_sab", {}).get("team_count") or 18)


def schedule_cfg() -> Dict[str, Any]:
    """赛程排期配置（每个比赛日最多几场、同队一天只打一场）。"""
    return dict(rules().get("schedule") or {})


def matches_per_day(default: int = 3) -> int:
    """每个比赛日最多安排的 BO 系列赛数量；<=0 表示不限制。"""
    value = schedule_cfg().get("matches_per_day", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bo_of(name: str, default: int = 5) -> int:
    """取某种对局的 BO 数，例如 ``bo_of('card')`` → 7。"""
    table = rules().get("bo") or {}
    try:
        return int(table.get(name) or default)
    except (TypeError, ValueError):
        return default


def annual_points(slot: str, rank: Optional[int]) -> int:
    """名次 → 年度积分。``slot`` ∈ spring / challenger / autumn。"""
    table = (rules().get("annual_points") or {}).get(slot) or {}
    if not rank:
        return 0
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return 0
    for row in table.get("brackets") or []:
        try:
            lo = int(row.get("from"))
            hi = int(row.get("to"))
        except (TypeError, ValueError):
            continue
        if lo <= rank <= hi:
            return int(row.get("points") or 0)
    return int(table.get("default") or 0)


def annual_points_table() -> List[Dict[str, Any]]:
    """给前端展示用的年度积分对照表。"""
    out: List[Dict[str, Any]] = []
    table = rules().get("annual_points") or {}
    for slot, label in (("spring", "春季赛"), ("challenger", "挑战者杯"), ("autumn", "夏季赛")):
        rows = (table.get(slot) or {}).get("brackets") or []
        out.append({
            "slot": slot,
            "label": label,
            "brackets": [{"from": r.get("from"), "to": r.get("to"),
                          "points": r.get("points")} for r in rows],
        })
    return out


def tiebreak_rules(kind: str = "in_season") -> List[Dict[str, Any]]:
    node = (rules().get("tiebreak") or {}).get(kind) or []
    return [dict(row) for row in node]


def league_cfg() -> Dict[str, Any]:
    return dict(rules().get("league_sab") or {})


def challenger_cfg() -> Dict[str, Any]:
    return dict(rules().get("challenger_cup") or {})


def annual_cfg() -> Dict[str, Any]:
    return dict(rules().get("annual_finals") or {})


def rule_tables_for_comment() -> Dict[str, Any]:
    """给「设置 → 赛制规则」页面直接展示的结构化对照表。"""
    return {
        "annual_points": annual_points_table(),
        "bo": rules().get("bo") or {},
        "tiebreak": {
            "in_season": tiebreak_rules("in_season"),
            "in_season_extra": (rules().get("tiebreak") or {}).get("in_season_extra"),
            "annual": tiebreak_rules("annual"),
            "annual_extra": (rules().get("tiebreak") or {}).get("annual_extra"),
        },
        "league_sab": league_cfg(),
        "challenger_cup": challenger_cfg(),
        "annual_finals": annual_cfg(),
        "templates": templates(),
    }
