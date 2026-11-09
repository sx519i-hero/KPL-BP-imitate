# -*- coding: utf-8 -*-
"""
KPL BP 小游戏 —— 数据加载层（``game/data_loader.py``）。

职责
====
1. 从 ``kpl_bp_game/data/`` 读取爬虫产出的 JSON（唯一事实来源见
   ``docs/DATA_CONTRACT.md``）。
2. 做**派生统计**（胜率归一化区间、英雄两两共现、战队英雄偏好等），统一封装
   在 :class:`LeagueContext` 里，供 ``ai_coach`` / ``scorer`` / ``bp_engine`` 使用。
3. 容错：``data/`` 目录不存在、文件缺失、JSON 损坏、字段为 ``null`` 时**一律不抛
   异常**，退化为空结构（空列表 / 空字典 / ``None``）。**任何 KPL 数值都不硬编码**，
   全部来自真实文件；缺失即 ``None``，由上层把对应权重降为 0 并转移。

缓存
====
* 每个 JSON 文件按 ``(st_mtime_ns, st_size)`` 做签名缓存，文件被爬虫重写后自动失效。
* :class:`LeagueContext` 按 ``league_id`` 缓存，并用“参与构建的文件签名元组”做校验，
  数据更新后自动重建。多线程下用 ``RLock`` 保护（Flask 默认多线程）。
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "DATA_DIR",
    "LANES",
    "LeagueContext",
    "clear_cache",
    "co_pick_rate",
    "get_default_league_id",
    "hero_index",
    "hero_api_item",
    "hero_list",
    "league_context",
    "league_meta",
    "leagues_payload",
    "load_bp_history",
    "load_hero_stats",
    "load_heroes",
    "load_leagues",
    "load_player_stats",
    "load_team_meta",
    "load_team_stats",
    "team_objects",
]

#: 契约 §1.2 —— ``lane`` / ``role`` 的合法取值，顺序固定（用于分路覆盖评分）。
LANES: Tuple[str, ...] = ("对抗路", "打野", "中路", "发育路", "游走")

from . import paths  # v10：打包后数据目录在 exe 同级

_PKG_DIR: Path = Path(__file__).resolve().parent.parent  # kpl_bp_game/
#: ``data`` 根目录（打包后指向 exe 同级的 data）。
DATA_DIR: Path = Path(paths.data_dir())

# 各子目录（契约 §1 目录结构）
_HERO_STATS_DIR = "hero_stats_by_league"
_PLAYER_STATS_DIR = "player_stats_by_league"
_TEAM_STATS_DIR = "team_stats_by_league"
_BP_HISTORY_DIR = "bp_history_by_league"

_SIDES: Tuple[str, str] = ("blue", "red")

# --------------------------------------------------------------------------- #
# 内部：带签名缓存的 JSON 读取
# --------------------------------------------------------------------------- #

_LOCK = threading.RLock()
# v52：内存优化 —— 原来的文件缓存是无上限的，切 8 个赛季会把 8 份
# bp_history（每个 1.7MB JSON、解析后十几 MB）全部留在内存里。
# 现在改成 LRU（只留最近用的几个），并且超过阈值的大文件不缓存（只在构建上下文时读一次）。
_FILE_CACHE: "OrderedDict[str, Tuple[Tuple[int, int], Any]]" = OrderedDict()
_FILE_CACHE_MAX = 10                    # 最多缓存 10 个文件
_FILE_CACHE_SKIP_BYTES = 400 * 1024     # 大于 400KB 的文件（主要是 bp_history）不驻留内存


def _sig(path: Path) -> Tuple[int, int]:
    """文件签名 ``(mtime_ns, size)``；不存在时返回 ``(0, 0)``。"""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON 对象（dict）。任何异常都返回 ``default``，绝不抛出。"""
    sig = _sig(path)
    key = str(path)
    with _LOCK:
        hit = _FILE_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    data: Any = None
    if sig != (0, 0):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
    if not isinstance(data, dict):
        data = default
    if sig[1] <= _FILE_CACHE_SKIP_BYTES:
        with _LOCK:
            _FILE_CACHE[key] = (sig, data)
            _FILE_CACHE.move_to_end(key)
            while len(_FILE_CACHE) > _FILE_CACHE_MAX:
                _FILE_CACHE.popitem(last=False)
    return data


def clear_cache() -> None:
    """清空所有文件缓存与 LeagueContext 缓存（数据被替换后可手动调用）。"""
    with _LOCK:
        _FILE_CACHE.clear()
        _CTX_CACHE.clear()


# --------------------------------------------------------------------------- #
# 内部：类型清洗工具（源数据可能给 null / 字符串 / 缺字段）
# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> Optional[int]:
    """尽力转 ``int``，失败返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _as_float(value: Any) -> Optional[float]:
    """尽力转 ``float``，失败返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _dicts(value: Any) -> List[Dict[str, Any]]:
    """只保留 list 中的 dict 元素。"""
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _percentile(values: List[float], pct: float) -> float:
    """线性插值百分位（``values`` 会被就地排序）。"""
    if not values:
        return 0.0
    values.sort()
    if len(values) == 1:
        return float(values[0])
    pos = (len(values) - 1) * (pct / 100.0)
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    if low == high:
        return float(values[low])
    return float(values[low] + (values[high] - values[low]) * (pos - low))


def _other_side(side: str) -> str:
    return "red" if side == "blue" else "blue"


# --------------------------------------------------------------------------- #
# 契约 §1 的文件读取函数
# --------------------------------------------------------------------------- #


def load_leagues() -> Dict[str, Any]:
    """``data/leagues.json``。

    文件缺失时返回 ``{"leagues": [], "default_league_id": None}``（契约要求）。
    """
    data = _read_json(DATA_DIR / "leagues.json", default=None)
    if not isinstance(data, dict):
        return {"leagues": [], "default_league_id": None}
    leagues = _dicts(data.get("leagues"))
    payload: Dict[str, Any] = {
        "leagues": leagues,
        "default_league_id": _as_str(data.get("default_league_id")),
    }
    if data.get("_fetched_at") is not None:
        payload["_fetched_at"] = data.get("_fetched_at")
    return payload


def _derive_default_league_id(leagues: List[Dict[str, Any]]) -> Optional[str]:
    """契约 §1.1：最后一个 ``status=="ended"``；若都没有则取最后一个。"""
    if not leagues:
        return None
    ended = [lg for lg in leagues if _as_str(lg.get("status")) == "ended"]
    chosen = ended[-1] if ended else leagues[-1]
    return _as_str(chosen.get("league_id"))


def get_default_league_id() -> Optional[str]:
    """默认赛季 id；连 ``leagues.json`` 都没有时返回 ``None``。"""
    payload = load_leagues()
    lid = payload.get("default_league_id")
    if lid:
        return lid
    return _derive_default_league_id(payload.get("leagues") or [])


def _resolve_league(league_id: Optional[str]) -> Tuple[Optional[str], Dict[str, Any]]:
    """把 ``league_id`` 归一到 ``(id, league_dict)``，缺失时回落到默认赛季。"""
    leagues = load_leagues().get("leagues") or []
    target = _as_str(league_id) or get_default_league_id()
    for league in leagues:
        if _as_str(league.get("league_id")) == target:
            return target, league
    if leagues and target is None:
        return _as_str(leagues[-1].get("league_id")), leagues[-1]
    return target, {}


def load_heroes() -> List[Dict[str, Any]]:
    """``data/heroes.json`` 的 ``heroes`` 列表（原始项，未清洗）。"""
    data = _read_json(DATA_DIR / "heroes.json", default=None)
    if not isinstance(data, dict):
        return []
    return _dicts(data.get("heroes"))


def load_team_meta() -> Dict[str, Dict[str, Any]]:
    """``data/team_meta.json`` → ``{team_id: {...}}``（按 ``team_id`` 索引）。"""
    data = _read_json(DATA_DIR / "team_meta.json", default=None)
    if not isinstance(data, dict):
        return {}
    index: Dict[str, Dict[str, Any]] = {}
    for team in _dicts(data.get("teams")):
        tid = _as_str(team.get("team_id"))
        if tid:
            index[tid] = team
    return index


def _load_league_file(subdir: str, league_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """读取 ``data/{subdir}/{league_id}.json``；``league_id`` 为 ``None`` 时不读。"""
    if not league_id:
        return None
    lid = _as_str(league_id)
    if not lid:
        return None
    # 防路径穿越：league_id 只允许数字/字母/下划线/连字符
    safe = "".join(ch for ch in lid if ch.isalnum() or ch in "_-")
    if not safe:
        return None
    return _read_json(DATA_DIR / subdir / f"{safe}.json", default=None)


def load_hero_stats(league_id: Optional[str] = None) -> Dict[str, Any]:
    """``data/hero_stats_by_league/{league_id}.json``。

    缺失时返回 ``{"league_id": id, "league_name": None, "heroes": []}``。
    """
    lid, league = _resolve_league(league_id)
    data = _load_league_file(_HERO_STATS_DIR, lid)
    if not isinstance(data, dict):
        return {
            "league_id": lid,
            "league_name": _as_str(league.get("name")),
            "heroes": [],
        }
    payload = dict(data)
    payload["league_id"] = _as_str(data.get("league_id")) or lid
    payload["league_name"] = _as_str(data.get("league_name")) or _as_str(league.get("name"))
    payload["heroes"] = _dicts(data.get("heroes"))
    return payload


def load_player_stats(league_id: Optional[str] = None) -> Dict[str, Any]:
    """``data/player_stats_by_league/{league_id}.json``（缺失时 ``players == []``）。"""
    lid, league = _resolve_league(league_id)
    data = _load_league_file(_PLAYER_STATS_DIR, lid)
    if not isinstance(data, dict):
        return {"league_id": lid, "league_name": _as_str(league.get("name")), "players": []}
    payload = dict(data)
    payload["league_id"] = _as_str(data.get("league_id")) or lid
    payload["league_name"] = _as_str(data.get("league_name")) or _as_str(league.get("name"))
    payload["players"] = _dicts(data.get("players"))
    return payload


def load_team_stats(league_id: Optional[str] = None) -> Dict[str, Any]:
    """``data/team_stats_by_league/{league_id}.json``（缺失时 ``teams == []``）。"""
    lid, league = _resolve_league(league_id)
    data = _load_league_file(_TEAM_STATS_DIR, lid)
    if not isinstance(data, dict):
        return {"league_id": lid, "league_name": _as_str(league.get("name")), "teams": []}
    payload = dict(data)
    payload["league_id"] = _as_str(data.get("league_id")) or lid
    payload["league_name"] = _as_str(data.get("league_name")) or _as_str(league.get("name"))
    payload["teams"] = _dicts(data.get("teams"))
    return payload


def load_bp_history(league_id: Optional[str] = None) -> Dict[str, Any]:
    """``data/bp_history_by_league/{league_id}.json``。

    缺失时返回 ``{"matches": [], "_missing": True}``（契约 §1.7）。
    """
    lid, league = _resolve_league(league_id)
    data = _load_league_file(_BP_HISTORY_DIR, lid)
    if not isinstance(data, dict):
        return {
            "league_id": lid,
            "league_name": _as_str(league.get("name")),
            "matches": [],
            "_missing": True,
        }
    payload = dict(data)
    payload["league_id"] = _as_str(data.get("league_id")) or lid
    payload["league_name"] = _as_str(data.get("league_name")) or _as_str(league.get("name"))
    payload["matches"] = _dicts(data.get("matches"))
    payload["_missing"] = bool(data.get("_missing")) or not payload["matches"]
    return payload


# --------------------------------------------------------------------------- #
# 英雄基础信息合并（heroes.json ⊕ hero_stats_by_league）
# --------------------------------------------------------------------------- #

_HERO_DEFAULTS: Tuple[Tuple[str, Any], ...] = (
    ("cname", ""),
    ("title", None),
    ("lane", None),
    ("lane_list", []),
    ("lane_games", {}),
    ("lane_source", "kpl_battle_position"),
    ("lane_missing", True),
    ("role", []),
    ("avatar_path", None),
    ("avatar_ok", False),
    ("win_rate", None),
    ("pick_rate", None),
    ("ban_rate", None),
    ("pick_count", None),
    ("ban_count", None),
    ("kda", None),
    ("has_stats", False),
)


def _lane_ordered(lanes: Any) -> List[str]:
    """把任意分路集合规整成 ``list[str]``。

    v2 契约 §1.1/§1.2：``lane_list`` 的内容由 scraper 按“总出场次数降序、再按
    :data:`LANES` 固定序”给出，这里只做**去重 + 过滤非法值**，不重排（保留 scraper
    的频次序），但非法分路（不在 :data:`LANES` 里的）会被丢弃，避免前端出现脏标签。
    """
    ordered: List[str] = []
    for item in _as_list(lanes):
        lane = _as_str(item)
        if lane and lane in LANES and lane not in ordered:
            ordered.append(lane)
    return ordered


def _lane_games_map(raw: Any) -> Dict[str, int]:
    """``{分路: 局数}``；只保留合法分路与非负整数，非法项丢弃。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for key, value in raw.items():
        lane = _as_str(key)
        count = _as_int(value)
        if lane in LANES and count is not None and count >= 0:
            out[lane] = count
    return out


def _clean_hero_base(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 ``heroes.json`` 的一项清洗成内部结构；``ename`` 非法时返回 ``None``。

    v2 契约 §2.1 / §5：**``lane`` / ``lane_list`` 一律不得由 ``hero_type`` / ``role``
    推导**（herolist 的定位标签与 KPL 实战分路常不一致）。这里只读 ``heroes.json``
    自带的 ``lane_list``（全量索引），真正的赛季分路由 :func:`_merge_heroes` 覆盖。
    """
    ename = _as_int(raw.get("ename"))
    if ename is None:
        return None
    roles = [_as_str(item) for item in _as_list(raw.get("role"))]
    roles = [item for item in roles if item]
    # v2 契约 §5：只用真实分路数据，绝不回退 role / hero_type。
    # heroes.json 的 lane_list 是「全部赛季」的全量索引（scraper 按 position_desc 统计），
    # 这里只放进 lane_list_all 备用；内部 hero 的 lane_list / lane 一律由该赛季
    # hero_stats 覆盖（见 _merge_heroes）。旧数据没有 lane_list 时留空，不做任何推导。
    lane_list_all = _lane_ordered(raw.get("lane_list"))
    avatar_path = _as_str(raw.get("avatar_path"))
    avatar_ok = raw.get("avatar_ok")
    if not isinstance(avatar_ok, bool):
        # 契约里 avatar_ok 是布尔；缺失时按“有头像路径即认为可用”推断，避免 UI 全变文字块
        avatar_ok = bool(avatar_path)
    return {
        "ename": ename,
        "cname": _as_str(raw.get("cname")) or str(ename),
        "title": _as_str(raw.get("title")),
        "id_name": _as_str(raw.get("id_name")),
        "hero_type": raw.get("hero_type"),
        "lane": None,
        "lane_list": [],
        "lane_games": {},
        "lane_list_all": lane_list_all,
        # v43：官方分路（王者荣耀官网 herolist.json 的 roles + extra_cold_lane）
        "lane_official": _lane_ordered(raw.get("lane_official")),
        "lane_leagues": _as_int(raw.get("lane_leagues")),
        "lane_source": "kpl_battle_position",
        "lane_missing": True,
        "role": roles,
        "avatar_path": avatar_path,
        "avatar_ok": avatar_ok,
        "win_rate": None,
        "pick_rate": None,
        "ban_rate": None,
        "pick_count": None,
        "ban_count": None,
        "win_count": None,
        "game_count": None,
        "kda": None,
        "avg_kill": None,
        "avg_death": None,
        "avg_assist": None,
        "has_stats": False,
    }


def _clean_hero_stat(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """清洗 ``hero_stats.heroes`` 的一项；``ename`` 非法时返回 ``None``。

    v2 契约 §1.2：赛季 hero_stats 里的 ``lane_list`` / ``lane_games`` 是**权威**分路
    数据；该赛季无数据时为空数组 / 空 dict，**绝不填 herolist 的定位**。
    """
    ename = _as_int(raw.get("ename"))
    if ename is None:
        return None
    return {
        "ename": ename,
        "cname": _as_str(raw.get("cname")),
        "pick_count": _as_int(raw.get("pick_count")),
        "ban_count": _as_int(raw.get("ban_count")),
        "pick_rate": _as_float(raw.get("pick_rate")),
        "ban_rate": _as_float(raw.get("ban_rate")),
        "win_count": _as_int(raw.get("win_count")),
        "game_count": _as_int(raw.get("game_count")),
        "win_rate": _as_float(raw.get("win_rate")),
        "kda": _as_float(raw.get("kda")),
        "avg_kill": _as_float(raw.get("avg_kill")),
        "avg_death": _as_float(raw.get("avg_death")),
        "avg_assist": _as_float(raw.get("avg_assist")),
        "lane_list": _lane_ordered(raw.get("lane_list")),
        "lane_games": _lane_games_map(raw.get("lane_games")),
    }


def _merge_heroes(
    heroes_raw: List[Dict[str, Any]],
    hero_stats_payload: Dict[str, Any],
) -> Dict[int, Dict[str, Any]]:
    """``heroes.json`` 基础信息与赛季 hero_stats 合并 → ``{ename: hero}``。

    * 只在 hero_stats 出现、heroes.json 里没有的英雄也会被收录（``lane=None``），
      否则 BP 时会出现“数据里有但选不到”的裂缝。
    * v2 契约 §2.1：**该赛季**的 ``lane_list`` / ``lane_games`` 覆盖全量索引；
      该赛季无分路数据时留空数组，**不回退到全量或 hero_type**（§5 禁止事项）。
    """
    merged: Dict[int, Dict[str, Any]] = {}
    for raw in heroes_raw:
        hero = _clean_hero_base(raw)
        if hero is not None:
            merged[hero["ename"]] = hero

    for raw in hero_stats_payload.get("heroes") or []:
        stat = _clean_hero_stat(raw)
        if stat is None:
            continue
        hero = merged.get(stat["ename"])
        if hero is None:
            hero = _clean_hero_base({"ename": stat["ename"], "cname": stat.get("cname")})
            if hero is None:
                continue
            merged[stat["ename"]] = hero
        if not hero.get("cname") or hero["cname"] == str(stat["ename"]):
            hero["cname"] = stat.get("cname") or hero["cname"]
        hero["has_stats"] = True
        for key in (
            "pick_count",
            "ban_count",
            "pick_rate",
            "ban_rate",
            "win_count",
            "game_count",
            "win_rate",
            "kda",
            "avg_kill",
            "avg_death",
            "avg_assist",
        ):
            hero[key] = stat.get(key)
        # v2：赛季分路是该赛季唯一权威来源
        season_lanes = stat.get("lane_list") or []
        hero["lane_list"] = list(season_lanes)
        hero["lane_games"] = dict(stat.get("lane_games") or {})
        hero["lane"] = season_lanes[0] if season_lanes else None
        hero["lane_source"] = "kpl_battle_position"
        hero["lane_missing"] = not season_lanes
    return merged


def hero_index(league_id: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
    """``{ename: hero_dict}`` —— heroes.json 基础信息 ⊕ 该赛季 hero_stats。"""
    lid, _league = _resolve_league(league_id)
    hero_stats = load_hero_stats(lid) if lid else {"heroes": []}
    return _merge_heroes(load_heroes(), hero_stats)


def _hero_sort_key(hero: Dict[str, Any]) -> Tuple[int, float, int]:
    """契约 §2：``has_stats`` 优先 → ``win_rate`` 降序 → 无数据项 ``ename`` 升序。"""
    has_stats = bool(hero.get("has_stats"))
    if has_stats:
        win_rate = hero.get("win_rate")
        return (0, -(win_rate if win_rate is not None else -1e9), hero["ename"])
    return (1, 0.0, hero["ename"])


def _hero_api_item(hero: Dict[str, Any]) -> Dict[str, Any]:
    """契约 §2 ``/api/heroes`` 的元素形状（不含 ``top_score``）。

    v2 契约 §2.1：``lane_list`` / ``lane_games`` / ``lane_source`` / ``lane_missing``
    四个字段随之带出，前端据此显示分路标签或「暂无该赛季分路数据」。
    """
    item = {key: hero.get(key) for key, _default in _HERO_DEFAULTS}
    item["ename"] = hero["ename"]
    item["cname"] = hero.get("cname") or str(hero["ename"])
    item["role"] = list(hero.get("role") or [])
    lane_list = [lane for lane in _as_list(hero.get("lane_list")) if lane in LANES]
    item["lane_list"] = lane_list
    item["lane"] = lane_list[0] if lane_list else None
    item["lane_games"] = _lane_games_map(hero.get("lane_games"))
    # v43：分路可能来自 KPL 实战，也可能回退王者荣耀官网的官方分路
    item["lane_source"] = hero.get("lane_source") or (
        "kpl_battle_position" if lane_list else "missing"
    )
    item["lane_official"] = [ln for ln in _as_list(hero.get("lane_official")) if ln in LANES]
    item["lane_missing"] = not lane_list
    item["has_stats"] = bool(hero.get("has_stats"))
    return item


def hero_api_item(hero: Dict[str, Any], top_score: Optional[float] = None) -> Dict[str, Any]:
    """把内部 hero dict 转成前端/契约形状；``top_score`` 给出时一并带上。

    ``bp_engine`` 用它构造 ``match["available"]`` 的元素。
    """
    item = _hero_api_item(hero)
    if top_score is not None:
        item["top_score"] = round(float(top_score), 1)
    return item


# --------------------------------------------------------------------------- #
# LeagueContext：一次性装载 + 派生统计
# --------------------------------------------------------------------------- #


class LeagueContext:
    """单个赛季的全部数据与派生统计（构造后只读，可安全跨线程共享）。

    支持 ``ctx.heroes`` 与 ``ctx["heroes"]`` 两种访问方式，方便调用方少写代码。

    主要属性
    --------
    ``league_id`` / ``league`` / ``league_name`` / ``status`` / ``start`` / ``end``
    ``heroes`` : ``{ename: hero}``（基础信息 ⊕ 赛季统计）
    ``hero_stats`` : ``{ename: 原始 hero_stats 项}``
    ``teams`` : ``[team]``（按 ``win_rate`` 降序，含 ``rank`` / ``logo_path``）
    ``team_by_name`` : ``{队名: team}``
    ``players`` : ``[player]``
    ``bp_matches`` : ``[bp_history.match]``
    ``bp_missing`` : bp_history 为空或 ``_missing``
    ``win_rate_stats`` : ``{ename: win_rate}``；``win_rate_min`` / ``win_rate_max``
    ``synergy`` : ``{(ename_a, ename_b): 共现次数}``（同队同小局 pick 两两共现）
    ``team_preference`` : ``{队名: {ename: pick 次数}}``
    ``counter_available`` : 恒为 ``False``（暂无真实克制数据）
    """

    __slots__ = (
        "league_id",
        "league",
        "league_name",
        "status",
        "start",
        "end",
        "fetched_at",
        "heroes",
        "hero_stats",
        "teams",
        "team_by_name",
        "players",
        "bp_matches",
        "bp_missing",
        "bp_step_count",
        "win_rate_stats",
        "win_rate_min",
        "win_rate_max",
        "max_pick_rate",
        "max_ban_rate",
        "max_contested",
        "synergy",
        "synergy_p95",
        "synergy_max",
        "team_preference",
        "counter_available",
        "lane_missing",
        "total_bp_games",
        "games_picked",
        "history_pick_count",
        "history_ban_count",
        "league_preference",
        "_hero_list_cache",
        "_hero_list_sorted",
    )

    def __init__(
        self,
        league_id: Optional[str],
        league: Dict[str, Any],
        heroes: Dict[int, Dict[str, Any]],
        hero_stats: Dict[int, Dict[str, Any]],
        teams: List[Dict[str, Any]],
        players: List[Dict[str, Any]],
        bp_matches: List[Dict[str, Any]],
        bp_missing: bool,
        bp_step_count: Optional[int],
        synergy: Dict[Tuple[int, int], int],
        team_preference: Dict[str, Dict[int, int]],
        games_picked: Dict[int, int],
        history_pick_count: Dict[int, int],
        history_ban_count: Dict[int, int],
        league_preference: Dict[int, int],
        fetched_at: Optional[str],
    ) -> None:
        self.league_id = league_id
        self.league = league
        self.league_name = _as_str(league.get("name"))
        self.status = _as_str(league.get("status"))
        self.start = _as_str(league.get("start"))
        self.end = _as_str(league.get("end"))
        self.fetched_at = fetched_at
        self.heroes = heroes
        self.hero_stats = hero_stats
        self.teams = teams
        self.team_by_name = {
            (_as_str(team.get("name")) or ""): team for team in teams if _as_str(team.get("name"))
        }
        self.players = players
        self.bp_matches = bp_matches
        self.bp_missing = bool(bp_missing)
        self.bp_step_count = bp_step_count
        self.synergy = synergy
        self.team_preference = team_preference
        # v43：官方克制数据（pvp.qq.com 英雄详情页）已入库时，counter 项才真正参与评分
        try:
            from . import scorer as _scorer

            self.counter_available = bool(_scorer.counter_map())
        except Exception:  # noqa: BLE001
            self.counter_available = False
        # v2 契约 §2.2：整季无分路数据（所有英雄 lane_list 都为空）
        self.lane_missing = not any(
            hero.get("lane_list") for hero in heroes.values()
        )
        self.games_picked = games_picked
        self.history_pick_count = history_pick_count
        self.history_ban_count = history_ban_count
        self.league_preference = league_preference
        self.total_bp_games = len(bp_matches)

        # --- 胜率归一化区间（用于 version 项 / AI 的 version 分量）---
        win_rates = [
            hero["win_rate"]
            for hero in heroes.values()
            if isinstance(hero.get("win_rate"), float)
        ]
        self.win_rate_stats: Dict[int, float] = {
            hero["ename"]: hero["win_rate"]
            for hero in heroes.values()
            if isinstance(hero.get("win_rate"), float)
        }
        self.win_rate_min = min(win_rates) if win_rates else None
        self.win_rate_max = max(win_rates) if win_rates else None

        # --- 出场率 / ban 率归一化参考值（供 AI 的 version 分量使用）---
        pick_rates = [
            hero["pick_rate"] for hero in heroes.values() if hero.get("pick_rate") is not None
        ]
        ban_rates = [
            hero["ban_rate"] for hero in heroes.values() if hero.get("ban_rate") is not None
        ]
        contested = [
            (hero.get("pick_rate") or 0.0) + (hero.get("ban_rate") or 0.0)
            for hero in heroes.values()
            if hero.get("pick_rate") is not None or hero.get("ban_rate") is not None
        ]
        self.max_pick_rate = max(pick_rates) if pick_rates else None
        self.max_ban_rate = max(ban_rates) if ban_rates else None
        self.max_contested = max(contested) if contested else None

        # --- 协同归一化参考值 ---
        pair_counts = [count for count in synergy.values() if count > 0]
        self.synergy_max = max(pair_counts) if pair_counts else 0
        self.synergy_p95 = _percentile([float(c) for c in pair_counts], 95.0) if pair_counts else 0.0

        self._hero_list_cache: Optional[List[Dict[str, Any]]] = None
        self._hero_list_sorted: Optional[List[Dict[str, Any]]] = None

    # ---- dict 式访问 ---------------------------------------------------- #
    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:  # pragma: no cover - 仅防御性分支
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)

    @property
    def keys(self) -> List[str]:
        """属性名列表（仅供调试；``ctx.get(name)`` 是推荐的通用读取方式）。"""
        return list(self.__slots__)

    # ---- 英雄查询 ------------------------------------------------------- #
    def hero(self, ename: Any) -> Dict[str, Any]:
        """取英雄 dict；不存在时返回一个安全占位（``cname`` 用 ename 字符串）。"""
        key = _as_int(ename)
        hero = self.heroes.get(key) if key is not None else None
        if hero is not None:
            return hero
        return {
            "ename": key if key is not None else ename,
            "cname": str(ename),
            "title": None,
            "lane": None,
            "lane_list": [],
            "lane_games": {},
            "lane_source": "kpl_battle_position",
            "lane_missing": True,
            "role": [],
            "avatar_path": None,
            "avatar_ok": False,
            "win_rate": None,
            "pick_rate": None,
            "ban_rate": None,
            "pick_count": None,
            "ban_count": None,
            "kda": None,
            "has_stats": False,
        }

    def cname(self, ename: Any) -> str:
        hero = self.hero(ename)
        return _as_str(hero.get("cname")) or str(ename)

    def lane(self, ename: Any) -> Optional[str]:
        """v2 契约 §2.2：返回 ``lane_list[0]``；无该赛季分路数据时为 ``None``。

        **不再回退**到 ``hero_type`` / ``role``（§5 禁止事项）。
        """
        lanes = self.lanes(ename)
        return lanes[0] if lanes else None

    def lanes(self, ename: Any) -> List[str]:
        """v2 契约 §2.2：该赛季该英雄真实分路列表；无数据返回 ``[]``。"""
        raw = self.hero(ename).get("lane_list")
        return [lane for lane in _as_list(raw) if lane in LANES]

    def lane_games(self, ename: Any) -> Dict[str, int]:
        """v2 契约 §2.2：``{分路: 该赛季出现在该分路的局数}``；无数据返回 ``{}``。"""
        return _lane_games_map(self.hero(ename).get("lane_games"))

    def hero_list(self) -> List[Dict[str, Any]]:
        """按契约 §2 排序的英雄列表（已缓存；调用方请勿原地修改返回元素）。"""
        if self._hero_list_cache is None:
            ordered = sorted(self.heroes.values(), key=_hero_sort_key)
            self._hero_list_cache = [_hero_api_item(hero) for hero in ordered]
        return self._hero_list_cache

    def hero_names(self) -> Dict[str, int]:
        """``{cname: ename}``，用于解析 bp_history 里只有英雄名没有 ename 的情况。"""
        if self._hero_list_sorted is not None:
            return self._hero_list_sorted
        index: Dict[str, int] = {}
        for hero in self.heroes.values():
            name = _as_str(hero.get("cname"))
            if name:
                index.setdefault(name, hero["ename"])
        return index

    # ---- 派生统计查询 --------------------------------------------------- #
    def win_rate_ratio(self, ename: Any) -> Optional[float]:
        """该赛季胜率线性归一化到 ``0..1``；无数据返回 ``None``。

        区间退化为 ``min == max``（例如只有 1 个英雄有数据）时，只要该英雄有胜率
        就返回 ``0.5``（无区分度），避免除零。
        """
        key = _as_int(ename)
        if key is None:
            return None
        win_rate = self.win_rate_stats.get(key)
        if win_rate is None:
            return None
        low, high = self.win_rate_min, self.win_rate_max
        if low is None or high is None or high <= low:
            return 0.5
        ratio = (win_rate - low) / (high - low)
        return max(0.0, min(1.0, ratio))

    def pick_count(self, ename: Any) -> int:
        """该赛季 pick 次数：优先 hero_stats，缺失时回落到 bp_history 统计。"""
        key = _as_int(ename)
        if key is None:
            return 0
        hero = self.heroes.get(key) or {}
        count = hero.get("pick_count")
        if isinstance(count, int):
            return count
        return int(self.history_pick_count.get(key, 0))

    def ban_count(self, ename: Any) -> int:
        """该赛季 ban 次数：优先 hero_stats，缺失时回落到 bp_history 统计。"""
        key = _as_int(ename)
        if key is None:
            return 0
        hero = self.heroes.get(key) or {}
        count = hero.get("ban_count")
        if isinstance(count, int):
            return count
        return int(self.history_ban_count.get(key, 0))

    def co_pick_rate(self, ename: Any) -> float:
        """``0..1``：该英雄在 bp_history 小局中的“出场率”（出现于任一方 pick）。

        含义是“被真实 BP 选中并进入阵容的频率”，用于 UI/文案；AI 的两两协同请用
        :meth:`synergy_rate`。
        """
        key = _as_int(ename)
        if key is None or self.total_bp_games <= 0:
            return 0.0
        return min(1.0, self.games_picked.get(key, 0) / float(self.total_bp_games))

    def synergy_count(self, ename_a: Any, ename_b: Any) -> int:
        """两英雄同队同小局的共现次数（无序）。"""
        a, b = _as_int(ename_a), _as_int(ename_b)
        if a is None or b is None or a == b:
            return 0
        if a > b:
            a, b = b, a
        return int(self.synergy.get((a, b), 0))

    def synergy_rate(self, ename_a: Any, ename_b: Any) -> float:
        """``0..1`` 条件共现率 = 共现次数 / min(两者出场小局数)。

        即“给定 a 出场，b 也出现的概率”的保守估计；无数据返回 ``0.0``。
        """
        count = self.synergy_count(ename_a, ename_b)
        if count <= 0:
            return 0.0
        a, b = _as_int(ename_a), _as_int(ename_b)
        games_a = self.games_picked.get(a, 0) if a is not None else 0
        games_b = self.games_picked.get(b, 0) if b is not None else 0
        denom = min(games_a, games_b)
        if denom <= 0:
            return 0.0
        return max(0.0, min(1.0, count / float(denom)))

    def team_pref_count(self, team_name: Optional[str], ename: Any) -> int:
        """该战队在该赛季 bp_history 中 pick 某英雄的次数。"""
        key = _as_int(ename)
        name = _as_str(team_name)
        if key is None or not name:
            return 0
        return int((self.team_preference.get(name) or {}).get(key, 0))

    def team_pref_ratio(self, team_name: Optional[str], ename: Any) -> float:
        """``0..1``：该战队对该英雄的偏好强度（相对该队最常用英雄）。

        该队无历史记录时回落到“全联盟 pick 频次相对最大值”，仍无数据返回 ``0.0``。
        """
        key = _as_int(ename)
        if key is None:
            return 0.0
        prefs = self.team_preference.get(_as_str(team_name) or "") or {}
        if prefs:
            peak = max(prefs.values()) if prefs else 0
            if peak <= 0:
                return 0.0
            return max(0.0, min(1.0, prefs.get(key, 0) / float(peak)))
        if self.league_preference:
            peak = max(self.league_preference.values())
            if peak > 0:
                return max(0.0, min(1.0, self.league_preference.get(key, 0) / float(peak)))
        return 0.0

    # ---- 展示用元信息 --------------------------------------------------- #
    def counts(self) -> Dict[str, int]:
        return {
            "heroes": len(self.heroes),
            "heroes_with_stats": len(self.win_rate_stats),
            "teams": len(self.teams),
            "players": len(self.players),
            "bp_matches": len(self.bp_matches),
        }

    def has_any_data(self) -> bool:
        return bool(self.heroes or self.teams or self.players or self.bp_matches)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        counts = self.counts()
        return (
            f"<LeagueContext {self.league_id!r} heroes={counts['heroes']} "
            f"teams={counts['teams']} bp={counts['bp_matches']} missing={self.bp_missing}>"
        )


# --------------------------------------------------------------------------- #
# 派生统计构建
# --------------------------------------------------------------------------- #


def _clean_team(raw: Dict[str, Any], meta: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """清洗/合并 team_stats 与 team_meta 的一项。"""
    name = _as_str(raw.get("name"))
    if not name:
        return None
    team_id = _as_str(raw.get("team_id"))
    meta_item = meta.get(team_id or "") or {}
    abbreviation = _as_str(raw.get("abbreviation")) or _as_str(meta_item.get("abbreviation"))
    if not abbreviation:
        abbreviation = name[:4]
    logo_path = _as_str(meta_item.get("logo_path")) or _as_str(raw.get("logo_path"))
    logo_ok = meta_item.get("logo_ok")
    if not isinstance(logo_ok, bool):
        logo_ok = bool(logo_path)
    return {
        "team_id": team_id,
        "name": name,
        "abbreviation": abbreviation,
        "logo_path": logo_path,
        "logo_ok": logo_ok,
        "matches": _as_int(raw.get("matches")),
        "wins": _as_int(raw.get("wins")),
        "losses": _as_int(raw.get("losses")),
        "game_diff": _as_int(raw.get("game_diff")),
        "win_rate": _as_float(raw.get("win_rate")),
        "blue_win_rate": _as_float(raw.get("blue_win_rate")),
        "red_win_rate": _as_float(raw.get("red_win_rate")),
        "avg_kda": _as_float(raw.get("avg_kda")),
        "avg_kill": _as_float(raw.get("avg_kill")),
        "avg_death": _as_float(raw.get("avg_death")),
        "rank": _as_int(raw.get("rank")),
    }


def _clean_player(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = _as_str(raw.get("name"))
    if not name:
        return None
    return {
        "name": name,
        "team": _as_str(raw.get("team")),
        "team_id": _as_str(raw.get("team_id")),
        "position": _as_str(raw.get("position")),
        "matches": _as_int(raw.get("matches")),
        "wins": _as_int(raw.get("wins")),
        "losses": _as_int(raw.get("losses")),
        "win_rate": _as_float(raw.get("win_rate")),
        "kda": _as_float(raw.get("kda")),
        "gpm": _as_float(raw.get("gpm")),
        "gold_rate": _as_float(raw.get("gold_rate")),
        "damage_share": _as_float(raw.get("damage_share")),
        "participation_rate": _as_float(raw.get("participation_rate")),
    }


def _sequences_of_game(
    match: Dict[str, Any],
    name_index: Dict[str, int],
) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
    """从一场 BP 记录里抽取 ``(picks_by_side, bans_by_side)``（``ename`` 去重保序）。

    优先用 ``sequence``（带 ``ename``，最可信）；缺失时回落到 ``blue_picks`` /
    ``red_picks`` / ``blue_bans`` / ``red_bans`` 的英雄**名称**，经 ``cname→ename`` 反查。
    """
    picks: Dict[str, List[int]] = {"blue": [], "red": []}
    bans: Dict[str, List[int]] = {"blue": [], "red": []}
    has_sequence = False
    for item in _dicts(match.get("sequence")):
        side = _as_str(item.get("side"))
        action = _as_str(item.get("action"))
        if side not in _SIDES or action not in ("pick", "ban"):
            continue
        has_sequence = True
        ename = _as_int(item.get("ename"))
        if ename is None:
            ename = name_index.get(_as_str(item.get("hero")) or "")
        if ename is None:
            continue
        bucket = picks if action == "pick" else bans
        if ename not in bucket[side]:
            bucket[side].append(ename)

    if not has_sequence:
        for side in _SIDES:
            for raw_name in _as_list(match.get(f"{side}_picks")):
                ename = _as_int(raw_name) or name_index.get(_as_str(raw_name) or "")
                if ename is not None and ename not in picks[side]:
                    picks[side].append(ename)
            for raw_name in _as_list(match.get(f"{side}_bans")):
                ename = _as_int(raw_name) or name_index.get(_as_str(raw_name) or "")
                if ename is not None and ename not in bans[side]:
                    bans[side].append(ename)

    return picks, bans


def _mode_int(values: Iterable[Any]) -> Optional[int]:
    """众数；空集合返回 ``None``。"""
    counts: Dict[int, int] = {}
    for value in values:
        key = _as_int(value)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


# v52：只保留最近用过的 2 个赛季上下文（切赛季会释放旧的，2GB 小内存服务器很关键）
_CTX_CACHE: "OrderedDict[Optional[str], Tuple[Tuple[str, ...], LeagueContext]]" = OrderedDict()
_CTX_CACHE_MAX = 2


def _context_signature(league_id: Optional[str]) -> Tuple[str, ...]:
    """参与构建 context 的文件签名（用于缓存失效）。"""
    lid = _as_str(league_id)
    paths = [
        DATA_DIR / "leagues.json",
        DATA_DIR / "heroes.json",
        DATA_DIR / "team_meta.json",
    ]
    if lid:
        safe = "".join(ch for ch in lid if ch.isalnum() or ch in "_-")
        paths.extend(
            [
                DATA_DIR / _HERO_STATS_DIR / f"{safe}.json",
                DATA_DIR / _PLAYER_STATS_DIR / f"{safe}.json",
                DATA_DIR / _TEAM_STATS_DIR / f"{safe}.json",
                DATA_DIR / _BP_HISTORY_DIR / f"{safe}.json",
            ]
        )
    return tuple(f"{path}|{_sig(path)[0]}|{_sig(path)[1]}" for path in paths)


def _build_context(league_id: Optional[str]) -> LeagueContext:
    lid, league = _resolve_league(league_id)
    heroes_payload_raw = load_heroes()
    hero_stats_payload = load_hero_stats(lid) if lid else {"heroes": []}
    heroes = _merge_heroes(heroes_payload_raw, hero_stats_payload)
    hero_stats = {
        stat["ename"]: stat
        for stat in (_clean_hero_stat(raw) for raw in hero_stats_payload.get("heroes") or [])
        if stat is not None
    }

    # ---- 战队（team_stats ⊕ team_meta），按 win_rate 降序并补 rank ----
    meta = load_team_meta()
    team_stats_payload = load_team_stats(lid) if lid else {"teams": []}
    teams: List[Dict[str, Any]] = []
    seen_names: set = set()
    for raw in team_stats_payload.get("teams") or []:
        team = _clean_team(raw, meta)
        if team is None or team["name"] in seen_names:
            continue
        seen_names.add(team["name"])
        teams.append(team)
    teams.sort(
        key=lambda t: (
            0 if t.get("win_rate") is not None else 1,
            -(t["win_rate"] if t.get("win_rate") is not None else 0.0),
            t["name"],
        )
    )
    for position, team in enumerate(teams, start=1):
        if team.get("rank") is None:
            team["rank"] = position

    # ---- 选手 ----
    player_payload = load_player_stats(lid) if lid else {"players": []}
    players = [p for p in (_clean_player(raw) for raw in player_payload.get("players") or []) if p]

    # ---- BP 历史 + 派生统计 ----
    bp_payload = load_bp_history(lid) if lid else {"matches": [], "_missing": True}
    bp_matches = _dicts(bp_payload.get("matches"))
    bp_missing = bool(bp_payload.get("_missing")) or not bp_matches
    bp_step_count = _as_int(bp_payload.get("_step_count"))
    if bp_step_count is None:
        bp_step_count = _mode_int(
            len(_dicts(match.get("sequence"))) for match in bp_matches
        )

    name_index: Dict[str, int] = {}
    for hero in heroes.values():
        cname = _as_str(hero.get("cname"))
        if cname:
            name_index.setdefault(cname, hero["ename"])

    synergy: Dict[Tuple[int, int], int] = {}
    team_preference: Dict[str, Dict[int, int]] = {}
    games_picked: Dict[int, int] = {}
    history_pick_count: Dict[int, int] = {}
    history_ban_count: Dict[int, int] = {}
    league_preference: Dict[int, int] = {}

    for match in bp_matches:
        picks, bans = _sequences_of_game(match, name_index)
        for side in _SIDES:
            side_picks = picks[side]
            for ename in side_picks:
                history_pick_count[ename] = history_pick_count.get(ename, 0) + 1
                games_picked[ename] = games_picked.get(ename, 0) + 1
                league_preference[ename] = league_preference.get(ename, 0) + 1
            for ename in bans[side]:
                history_ban_count[ename] = history_ban_count.get(ename, 0) + 1
            # 同队同小局 5 个 pick 的两两共现
            unique_picks = sorted(set(side_picks))
            for idx_a in range(len(unique_picks)):
                for idx_b in range(idx_a + 1, len(unique_picks)):
                    pair = (unique_picks[idx_a], unique_picks[idx_b])
                    synergy[pair] = synergy.get(pair, 0) + 1
            team_name = _as_str(match.get(f"{side}_team"))
            if team_name:
                prefs = team_preference.setdefault(team_name, {})
                for ename in side_picks:
                    prefs[ename] = prefs.get(ename, 0) + 1

    fetched_at = None
    leagues_payload_raw = load_leagues()
    for payload in (
        hero_stats_payload,
        team_stats_payload,
        player_payload,
        bp_payload,
        leagues_payload_raw,
    ):
        if isinstance(payload, dict) and payload.get("_fetched_at"):
            fetched_at = _as_str(payload.get("_fetched_at"))
            break

    return LeagueContext(
        league_id=lid,
        league=league,
        heroes=heroes,
        hero_stats=hero_stats,
        teams=teams,
        players=players,
        bp_matches=bp_matches,
        bp_missing=bp_missing,
        bp_step_count=bp_step_count,
        synergy=synergy,
        team_preference=team_preference,
        games_picked=games_picked,
        history_pick_count=history_pick_count,
        history_ban_count=history_ban_count,
        league_preference=league_preference,
        fetched_at=fetched_at,
    )


def league_context(league_id: Optional[str] = None) -> LeagueContext:
    """一次性装好该赛季全部数据并做派生统计（带缓存）。

    ``league_id`` 为 ``None`` 时使用默认赛季；无任何数据时返回空 context，不抛异常。
    """
    lid, _league = _resolve_league(league_id)
    signature = _context_signature(lid)
    with _LOCK:
        hit = _CTX_CACHE.get(lid)
        if hit is not None and hit[0] == signature:
            _CTX_CACHE.move_to_end(lid)
            return hit[1]
    context = _build_context(lid)
    with _LOCK:
        _CTX_CACHE[lid] = (signature, context)
        _CTX_CACHE.move_to_end(lid)
        while len(_CTX_CACHE) > _CTX_CACHE_MAX:
            _CTX_CACHE.popitem(last=False)
    return context


# --------------------------------------------------------------------------- #
# 给 app.py 的便捷封装（避免它重复读文件 / 重复写排序逻辑）
# --------------------------------------------------------------------------- #


_STATION_KEYWORDS = (
    ("spring", ("春季赛",)),
    ("challenger", ("挑战者杯",)),
    ("autumn", ("夏季赛", "秋季赛")),
    ("annual_finals", ("年度总决赛",)),
)


def package_stations(league_id: str) -> List[Dict[str, Any]]:
    """按某个赛季所属年份，推出「春季赛 → 挑战者杯 → 夏季赛 → 年度总决赛」四站。

    v31：好友 PK / 大模型联赛这类「只传了模板没传届次」的老调用方，
    以前会拿到中性的「春季赛 / 挑战者杯 / …」站名；这里用真实联赛名兜底。
    任何一步找不到就返回已找到的部分（调用方会退回中性名）。
    """
    target = str(league_id or "")
    try:
        rows = leagues_payload().get("leagues") or []
    except Exception:                                       # noqa: BLE001
        return []
    if not target or not rows:
        return []

    def _year(value: str) -> str:
        if value and len(value) >= 4 and value[:4].isdigit():
            return value[:4]
        return ""

    want_year = _year(target)
    if not want_year:
        for row in rows:
            if _as_str(row.get("league_id")) == target:
                want_year = _year(_as_str(row.get("start"))) or ""
                break
    return _package_stations_by_year(want_year)


def package_stations_of_year(year: str) -> List[Dict[str, Any]]:
    """v32：按年份直接取「春季赛 → 挑战者杯 → 夏季赛 → 年度总决赛」四站。

    好友 PK 的「届次」是 ``annual_package:<年份>``，没有具体 league_id，用这个函数。
    """
    want = str(year or "").strip()[:4]
    if not want.isdigit():
        return []
    return _package_stations_by_year(want)


def _package_stations_by_year(want_year: str) -> List[Dict[str, Any]]:
    def _year(value: str) -> str:
        if value and len(value) >= 4 and value[:4].isdigit():
            return value[:4]
        return ""

    try:
        rows = leagues_payload().get("leagues") or []
    except Exception:                                       # noqa: BLE001
        return []
    if not rows:
        return []
    if want_year:
        # 只认该年份的赛事，避免把 2026 的站名塞进 2025 的包
        rows = [r for r in rows if _year(_as_str(r.get("league_id"))) == want_year] or rows

    out: List[Dict[str, Any]] = []
    for key, words in _STATION_KEYWORDS:
        same = []
        any_year = []
        for row in rows:
            name = _as_str(row.get("name"))
            lid = _as_str(row.get("league_id"))
            if not name or not lid:
                continue
            if not any(word in name for word in words):
                continue
            if want_year and _year(lid) == want_year:
                same.append(row)
            else:
                any_year.append(row)
        pick = (same or any_year or [None])[0]
        if not pick:
            continue
        out.append({"key": key, "label": _as_str(pick.get("name")),
                    "league_id": _as_str(pick.get("league_id"))})
    if len(out) < 3:
        return []
    return out


def leagues_payload() -> Dict[str, Any]:
    """``/api/leagues`` 的响应体（不含 ``ok``）。"""
    payload = load_leagues()
    leagues = []
    for league in payload.get("leagues") or []:
        leagues.append(
            {
                "league_id": _as_str(league.get("league_id")),
                "name": _as_str(league.get("name")),
                "status": _as_str(league.get("status")),
                "start": _as_str(league.get("start")),
                "end": _as_str(league.get("end")),
                "match_count": _as_int(league.get("match_count")),
                "year": _as_int(league.get("year")),
                "season": _as_int(league.get("season")),
            }
        )
    return {
        "default_league_id": payload.get("default_league_id") or _derive_default_league_id(leagues),
        "leagues": leagues,
    }


def hero_list(league_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """``/api/heroes`` 的 ``heroes`` 数组（已按契约 §2 排序）。"""
    return list(league_context(league_id).hero_list())


def team_objects(league_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``{队名: {team_id, name, abbreviation, logo_path, logo_ok, ...}}``。"""
    ctx = league_context(league_id)
    return {team["name"]: dict(team) for team in ctx.teams}


def league_meta(league_id: Optional[str] = None) -> Dict[str, Any]:
    """``/api/league_meta`` 的响应体（不含 ``ok``）。"""
    ctx = league_context(league_id)
    counts = ctx.counts()
    return {
        "league_id": ctx.league_id,
        "league_name": ctx.league_name,
        "status": ctx.status,
        "start": ctx.start,
        "end": ctx.end,
        "counts": counts,
        "has_bp_history": not ctx.bp_missing,
        "real_bp_step_count": ctx.bp_step_count,
    }
