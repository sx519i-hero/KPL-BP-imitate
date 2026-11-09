# -*- coding: utf-8 -*-
"""
KPL BP 小游戏 —— BP 规则引擎（``game/bp_engine.py``）。

契约：``docs/DATA_CONTRACT.md`` §3（``match`` 对象）与 §4（BP 步骤表）。

本模块是**唯一**知道“BP 怎么做”的地方：步骤推进、合法性校验、全局 BP
（BO 内同一队伍已 pick 的英雄不能再次 pick，ban 位每小局重置）、决胜局规则、
AI 代走，以及把``match["available"]`` 每次都重算成“当前真正可选的英雄”。

对外接口
--------
``BP_STEPS``          常规小局 20 步（每方 5 ban / 5 pick）的 ``(side, action)`` 表
``TIEBREAK_STEPS``    决胜局 10 步的 ``(side, action)`` 表（全 pick）
``new_match(...)``    按契约 §3 造一个完整 ``match`` 对象
``apply_action(...)`` 执行一手（非法则 ``raise BPError``），原地推进
``legal_actions(...)`` 当前可执行动作列表
``auto_advance(...)`` AI 连续走完所有该它走的手，直到轮到玩家或本小局结束
``next_game(...)``    进入下一小局（系列赛结束时幂等返回）
``pick_default_teams`` / ``resolve_teams`` 球队兜底

不硬编码任何 KPL 数值：所有数据经 ``game.data_loader`` 读取；数据缺失时英雄池
退化为空列表，但引擎自身不会崩。
"""

from __future__ import annotations

import datetime
import secrets
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import ai_coach, data_loader, scorer

__all__ = [
    "ALL_ACTIONS",
    "ALLOWED_BO",
    "BPError",
    "BP_STEPS",
    "BANS_PER_SIDE",
    "FIRST_BAN_PHASE_STEPS",
    "SECOND_BAN_PHASE_STEPS",
    "MODES",
    "SIDES",
    "DIFFICULTIES",
    "TIEBREAK_STEPS",
    "apply_action",
    "auto_advance",
    "choose_side",
    "legal_actions",
    "new_match",
    "next_game",
    "pick_default_teams",
    "refresh_match",
    "resolve_teams",
    "team_brief",
    "team_wins",
]


# --------------------------------------------------------------------------- #
# 常量 / 异常
# --------------------------------------------------------------------------- #

#: v11：现行 KPL 常规小局 **20 步**，每方 **5 ban / 5 pick**。
#: ban 分两段：先各 ban 2 个（第 1-4 手），第一轮 pick 之后再各 ban 3 个（第 11-16 手）。
#: 与 ``data/bp_history_by_league/*.json`` 的真实对局（每局 20 手）一致。
BP_STEPS: Tuple[Tuple[str, str], ...] = (
    ("blue", "ban"),   # 1   第一段 ban：双方各 2 个
    ("red", "ban"),    # 2
    ("blue", "ban"),   # 3
    ("red", "ban"),    # 4
    ("blue", "pick"),  # 5
    ("red", "pick"),   # 6
    ("red", "pick"),   # 7
    ("blue", "pick"),  # 8
    ("blue", "pick"),  # 9
    ("red", "pick"),   # 10
    ("red", "ban"),    # 11  第二段 ban：双方各再 3 个（红方先）
    ("blue", "ban"),   # 12
    ("red", "ban"),    # 13
    ("blue", "ban"),   # 14
    ("red", "ban"),    # 15
    ("blue", "ban"),   # 16
    ("red", "pick"),   # 17
    ("blue", "pick"),  # 18
    ("blue", "pick"),  # 19
    ("red", "pick"),   # 20
)

#: 每方 ban 数（v11：5）；两段 ban 各自的步数（先 4 手 = 2 个/方，后 6 手 = 3 个/方）。
BANS_PER_SIDE: int = 5
FIRST_BAN_PHASE_STEPS: int = 4
SECOND_BAN_PHASE_STEPS: int = 6

#: v20：巅峰对决 = **真盲选**（用户 m11997）。
#:
#: 官方规则：决胜局没有 ban 位，双方**同时盲选 5 个英雄**（互不可见、允许撞同一个英雄），
#: 双方都选完之后，再按「对抗路 → 打野 → 中路 → 发育路 → 游走」的顺序逐路公布阵容。
#:
#: 旧的 ``TIEBREAK_STEPS``（10 步交替 pick）只是「把 ban 去掉、按 BP 顺序轮流出」，
#: 既不是盲选也没有分路公布，v20 起仅在「兼容老存档 / 计算总步数」时保留。
TIEBREAK_PICKS_PER_SIDE: int = 5
PHASE_TIEBREAK_PICK: str = "tiebreak_pick"
PHASE_TIEBREAK_REVEAL: str = "tiebreak_reveal"

#: 契约 §3 末段（v20 起仅用于 ``total_steps`` 口径与老存档兼容）：
#: 决胜局仍是 10 手（双方各 5 个 pick），但不再是「轮流出」，见上面的盲选说明。
TIEBREAK_STEPS: Tuple[Tuple[str, str], ...] = (
    ("blue", "pick"),  # 1
    ("red", "pick"),   # 2
    ("red", "pick"),   # 3
    ("blue", "pick"),  # 4
    ("blue", "pick"),  # 5
    ("red", "pick"),   # 6
    ("red", "pick"),   # 7
    ("blue", "pick"),  # 8
    ("blue", "pick"),  # 9
    ("red", "pick"),   # 10
)

#: 契约 §2 ``/api/bp/new`` 允许的赛制。
ALLOWED_BO: Tuple[int, ...] = (1, 3, 5, 7, 9)
MODES: Tuple[str, ...] = ("ai", "free")
SIDES: Tuple[str, str] = ("blue", "red")
DIFFICULTIES: Tuple[str, ...] = ("rookie", "pro", "boss")
ALL_ACTIONS: Tuple[str, ...] = ("ban", "pick")

#: 进入决胜局的赛制（契约 §3）。
# ---------------------------------------------------------------------------
# KPL 官方赛制对照表（v5 修正，2025/2026 KPL 规则）
# ---------------------------------------------------------------------------
#  | BO   | 胜场需求 | 巅峰对决触发条件        | 备注                     |
#  |------|----------|-------------------------|--------------------------|
#  | BO1  |   1 胜   | 无                      | 单局                     |
#  | BO3  |   2 胜   | 无                      | 2:2 不存在               |
#  | BO5  |   3 胜   | **永不触发**            | 2:2 时第 5 局为普通局     |
#  | BO7  |   4 胜   | 3:3 平后第 7 局         | 巅峰对决：盲选/无ban/清空全局BP |
#  | BO9  |   5 胜   | 4:4 平后第 9 局         | 巅峰对决：盲选/无ban/清空全局BP |
# ---------------------------------------------------------------------------
# **BO5 绝不能出现巅峰对决**（旧版把 5 写进 _TIEBREAK_BO 是错的，已修）。
_TIEBREAK_BO: Tuple[int, ...] = (7, 9)

_SIDE_CN: Dict[str, str] = {"blue": "蓝", "red": "红"}


class BPError(Exception):
    """BP 规则/状态错误。``app.py`` 会把它映射成 HTTP 409。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    """带本地时区偏移的 ISO 时间戳（契约 §3 ``created_at``）。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_match_id() -> str:
    """占位 ``match_id``（真正的 ID 由 ``app.py`` 生成并覆盖）。"""
    return "m_" + secrets.token_hex(4)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] == "-" and text[1:].isdigit())):
            return int(text)
    return None


def _side_list(match: Dict[str, Any], key: str) -> List[int]:
    """安全取出 ``match[key]["blue"]`` / ``["red"]``（保证是 ``list[int]``）。"""
    block = match.get(key)
    if not isinstance(block, dict):
        block = {"blue": [], "red": []}
        match[key] = block
    result: List[int] = []
    for side in SIDES:
        raw = block.get(side)
        if not isinstance(raw, list):
            raw = []
            block[side] = raw
        result.append(raw)  # type: ignore[arg-type]
    return result


def _ensure_side_lists(match: Dict[str, Any]) -> None:
    """把 ``bans`` / ``picks`` / ``used_global`` 规整成 ``{"blue": [], "red": []}``。"""
    for key in ("bans", "picks", "used_global"):
        _side_list(match, key)
    if not isinstance(match.get("history"), list):
        match["history"] = []
    if not isinstance(match.get("ai_moves"), list):
        match["ai_moves"] = []
    if not isinstance(match.get("available"), list):
        match["available"] = []
    score = match.get("score")
    if not isinstance(score, dict):
        match["score"] = {"blue": 0, "red": 0}
    else:
        for side in SIDES:
            value = _as_int(score.get(side))
            score[side] = value if value is not None else 0
    if not isinstance(match.get("series_games"), list):
        match["series_games"] = []
    if not isinstance(match.get("team_wins"), dict):
        match["team_wins"] = team_wins(match)


def _ctx(match: Dict[str, Any]) -> Any:
    """取该对局所属赛季的 :class:`~game.data_loader.LeagueContext`（带缓存）。"""
    return data_loader.league_context(match.get("league_id"))


def _is_tiebreak_game(game_index: int, bo: int, score: Optional[Dict[str, int]] = None) -> bool:
    """是否「巅峰对决」小局。

    KPL 官方赛制（也是用户 v5 需求第 5 条的硬要求）：
      · **只有 BO7 / BO9 才有巅峰对决，BO5 无论打成什么比分都不会有**；
      · BO7 必须战至 **3:3** 才进第 7 局的巅峰对决；
      · BO9 必须战至 **4:4** 才进第 9 局的巅峰对决；
      · 巅峰对决是盲选：没有 ban 位、双方可以选同一个英雄、
        并且**解除全局 BP 限制**（前面小局用过的英雄也能再选）。
    所以判定必须同时满足三件事：``bo in (7, 9)``、``game_index == bo``、
    并且当前大比分「双方都拿满 bo//2 分」（3:3 / 4:4）。
    ``score`` 不传时退化为只按 ``game_index == bo`` 判断（兼容旧调用点）。
    """
    try:
        bo_value = int(bo)
        index = int(game_index)
    except (TypeError, ValueError):
        return False
    if bo_value not in _TIEBREAK_BO or index != bo_value:
        return False
    if not isinstance(score, dict):
        return True
    half = bo_value // 2
    try:
        blue = int(score.get("blue") or 0)
        red = int(score.get("red") or 0)
    except (TypeError, ValueError):
        return True
    return blue == half and red == half


def _steps_for(match: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
    return TIEBREAK_STEPS if match.get("is_tiebreak") else BP_STEPS


def _need_wins(bo: int) -> int:
    """BO 赛制下先到几胜（``bo // 2 + 1``）。"""
    try:
        value = int(bo)
    except (TypeError, ValueError):
        value = 1
    return max(1, value // 2 + 1)


# --------------------------------------------------------------------------- #
# 阵营 / 战队归属（v2 契约 §3）
# --------------------------------------------------------------------------- #


def _team_name_of(team: Any) -> str:
    """从球队 dict / 字符串里取队名；取不到返回 ``""``。"""
    if isinstance(team, dict):
        name = team.get("name")
        if name:
            return str(name)
    elif isinstance(team, str):
        text = team.strip()
        if text:
            return text
    return ""


def _blue_name(match: Dict[str, Any]) -> str:
    return _team_name_of(match.get("blue_team"))


def _red_name(match: Dict[str, Any]) -> str:
    return _team_name_of(match.get("red_team"))


def _side_of_team(match: Dict[str, Any], team_name: Any) -> Optional[str]:
    """某战队**当前**所在阵营；不在场上返回 ``None``。"""
    name = _team_name_of(team_name)
    if not name:
        return None
    if name == _blue_name(match):
        return "blue"
    if name == _red_name(match):
        return "red"
    return None


def _opposite_side(side: str) -> str:
    return "red" if side == "blue" else "blue"


def team_wins(match: Dict[str, Any]) -> Dict[str, int]:
    """``{队名: 已赢小局数}``（按**战队**统计，换边不影响）。

    换边只改「战队 ↔ 阵营」，所以大比分必须以战队为键；``match["score"]`` 每次
    换边后用 :func:`_sync_score` 按当前阵营重算，保持契约 §3 的 ``score{blue,red}``
    语义（永远是“当前蓝方/当前红方的胜场”）。
    """
    wins = match.get("team_wins")
    if isinstance(wins, dict):
        cleaned = {}
        for key, value in wins.items():
            name = _team_name_of(key)
            count = _as_int(value)
            if name and count is not None:
                cleaned[name] = count
        if cleaned:
            return cleaned
    # 兜底：从 series_games 重建
    rebuilt: Dict[str, int] = {}
    for game in match.get("series_games") or []:
        if not isinstance(game, dict):
            continue
        name = _team_name_of(game.get("winner_team"))
        if name:
            rebuilt[name] = rebuilt.get(name, 0) + 1
    return rebuilt


def _sync_score(match: Dict[str, Any]) -> None:
    """按当前阵营归属重算 ``match["score"]``（契约 §3 ``{blue, red}``）。"""
    wins = team_wins(match)
    match["team_wins"] = wins
    match["score"] = {
        "blue": int(wins.get(_blue_name(match), 0)),
        "red": int(wins.get(_red_name(match), 0)),
    }


def _series_over(match: Dict[str, Any]) -> bool:
    """系列赛是否结束：任一方先到 ``need_wins``，或已打满 ``bo`` 局。"""
    bo = _as_int(match.get("bo")) or 1
    need = _need_wins(bo)
    game_index = _as_int(match.get("game_index")) or 1
    wins = team_wins(match)
    best = max(wins.values()) if wins else 0
    return best >= need or game_index >= bo


def _swap_camps(match: Dict[str, Any]) -> None:
    """交换两队阵营（v2 契约 §3.3）。

    同时交换 ``used_global`` 的两个桶 —— 全局 BP 是**按战队**约束的，阵营一换如果
    不跟着换，就会出现“同一战队换个边就能重复 pick”的漏洞。
    """
    blue = match.get("blue_team")
    red = match.get("red_team")
    match["blue_team"] = red
    match["red_team"] = blue
    used = match.get("used_global")
    if isinstance(used, dict):
        blue_used = used.get("blue") if isinstance(used.get("blue"), list) else []
        red_used = used.get("red") if isinstance(used.get("red"), list) else []
        used["blue"] = red_used
        used["red"] = blue_used
    # v17 修复：阵营一换，我操作的阵营也要跟着换，否则大比分（按战队统计）会显示反
    cur_side = str(match.get("player_side") or "")
    if cur_side in SIDES:
        match["player_side"] = _opposite_side(cur_side)
    _recompute_player_side(match)
    _sync_score(match)


def _recompute_player_side(match: Dict[str, Any]) -> None:
    """``player_side`` 重算（契约 v2 §3.2）：``ai`` 模式跟随 ``player_team``。"""
    # v18：不再按 mode 短路 —— 只要记了我方战队名，换边后就跟着队名重算 player_side。
    player_team = _team_name_of(match.get("player_team"))
    if not player_team:
        return
    side = _side_of_team(match, player_team)
    if side in SIDES:
        match["player_side"] = side


def _chooser_role(match: Dict[str, Any], chooser_team: str) -> str:
    """选边者的角色：``ai`` 模式用 ``player`` / ``ai``；``free`` 模式用 ``human``。"""
    mode = str(match.get("mode") or "ai").strip().lower()
    if mode != "ai":
        return "human"
    player_team = _team_name_of(match.get("player_team"))
    ai_team = _team_name_of(match.get("ai_team"))
    if chooser_team and chooser_team == player_team:
        return "player"
    if chooser_team and chooser_team == ai_team:
        return "ai"
    # 兜底：与 player_side 不同的那支队就是 AI
    return "ai" if _side_of_team(match, chooser_team) != match.get("player_side") else "player"


# --------------------------------------------------------------------------- #
# 球队
# --------------------------------------------------------------------------- #


def team_brief(team: Dict[str, Any]) -> Dict[str, Any]:
    """``data_loader`` 的 team dict → 契约 §3 的 ``{name, logo_path, abbreviation}``。"""
    name = team.get("name")
    name = str(name) if name else "未知战队"
    abbreviation = team.get("abbreviation") or name[:4]
    return {
        "name": name,
        "logo_path": team.get("logo_path"),
        "abbreviation": str(abbreviation),
        "team_id": team.get("team_id"),
    }


def _fallback_brief(label: str) -> Dict[str, Any]:
    return {
        "name": label,
        "logo_path": None,
        "abbreviation": label[:2],
        "team_id": None,
    }


def resolve_teams(
    league_id: Optional[str],
    blue_name: Any,
    red_name: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把队名解析成契约 §3 的球队对象（logo/缩写齐全），带兜底。

    规则：队名属于该赛季 ``team_stats`` 则采用；否则按契约用赛季前两名兜底；
    完全无球队数据时返回 ``蓝方`` / ``红方``。两方不会重名。
    """
    ctx = data_loader.league_context(league_id)
    teams = [team for team in (ctx.teams or []) if team.get("name")]
    by_name = {team["name"]: team for team in teams}

    def one(raw: Any, index: int, label: str) -> Dict[str, Any]:
        name: Optional[str] = None
        if isinstance(raw, str):
            name = raw.strip() or None
        elif isinstance(raw, dict):
            value = raw.get("name")
            name = str(value).strip() if value else None
        if name and name in by_name:
            return team_brief(by_name[name])
        # v18：显式传了队名（生涯/联机/AI 联赛里玩家自定义的战队）就原样保留，
        # 绝不拿赛季队伍顶替，否则「我自己起的名字」进了 BP 页面会变成别的战队。
        if name:
            return _fallback_brief(name)
        if index < len(teams):
            return team_brief(teams[index])
        return _fallback_brief(label)

    blue = one(blue_name, 0, "蓝方")
    red = one(red_name, 1, "红方")
    if blue["name"] == red["name"]:
        for team in teams:
            if team["name"] != blue["name"]:
                red = team_brief(team)
                break
        else:
            red = _fallback_brief("红方" if blue["name"] != "红方" else "蓝方")
    return blue, red


def pick_default_teams(league_id: Optional[str] = None) -> Tuple[str, str]:
    """该赛季默认双方队名（``team_stats`` 前两名）。

    无球队数据时返回 ``("蓝方", "红方")``（契约 §2：缺失用赛季前两名兜底）。
    """
    ctx = data_loader.league_context(league_id)
    names = [str(team["name"]) for team in (ctx.teams or []) if team.get("name")]
    blue = names[0] if len(names) > 0 else "蓝方"
    red = names[1] if len(names) > 1 else "红方"
    if blue == red:
        red = "红方" if blue != "红方" else "蓝方"
    return blue, red


# --------------------------------------------------------------------------- #
# available 计算
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 多形态英雄（元流之子）：一个英雄位、多个分路形态
# --------------------------------------------------------------------------- #
#
# KPL 规则（用户 m21337）：元流之子虽然在对局数据里是 581~585 五个 ename（坦克/法师/
# 刺客/射手/辅助），但**它是同一个英雄**——只要其中任意一个形态被 ban 或被选，
# 其余形态同样不能再选/再禁，否则等于一个队伍占了两个英雄位。
#
# 实现上不写死 ename：按 heroes.json 里的 cname 去掉括号后缀取「基础名」，
# 基础名相同的英雄自动归为同一家族（现在只有元流之子，以后官方再出类似英雄也自动生效）。
_FAMILY_CACHE: Dict[str, Dict[int, str]] = {}


def hero_family_map(ctx) -> Dict[int, str]:
    """``{ename: 家族基础名}``，只包含「同一基础名有多个形态」的英雄。"""
    key = str(getattr(ctx, "league_id", "") or "")
    cached = _FAMILY_CACHE.get(key)
    if cached is not None:
        return cached
    groups: Dict[str, List[int]] = {}
    for hero in ctx.hero_list():
        ename = _as_int(hero.get("ename"))
        if ename is None:
            continue
        cname = str(hero.get("cname") or "")
        base = cname.replace("（", "(").split("(")[0].strip() or cname
        groups.setdefault(base, []).append(ename)
    mapping: Dict[int, str] = {}
    for base, members in groups.items():
        if len(members) < 2:
            continue
        for ename in members:
            mapping[ename] = base
    _FAMILY_CACHE[key] = mapping
    return mapping


def hero_family_members(ctx, ename: Any) -> List[int]:
    """同一英雄家族的全部 ename（含自己）；单形态英雄返回空列表。"""
    target = _as_int(ename)
    if target is None:
        return []
    mapping = hero_family_map(ctx)
    base = mapping.get(target)
    if not base:
        return []
    return [one for one, owner in mapping.items() if owner == base]


def _expand_by_family(match: Dict[str, Any], excluded: set) -> set:
    """把「家族里任意一个形态被占用」扩展成「整个家族都不可用」。"""
    if not excluded:
        return excluded
    mapping = hero_family_map(_ctx(match))
    if not mapping:
        return excluded
    hit = {mapping[one] for one in excluded if one in mapping}
    if not hit:
        return excluded
    for one, base in mapping.items():
        if base in hit:
            excluded.add(one)
    return excluded


def _excluded_enames(match: Dict[str, Any], for_side: Optional[str] = None,
                     for_action: Optional[str] = None) -> set:
    """已被占用（不可再选/不可再禁）的英雄。

    v17 修正 —— **禁英雄的池子和选英雄的池子是分开的**（用户 m09741）：
    * 本小局内的 ``ban`` / ``pick``：一局之内一个英雄只能出现一次，双方共享排除；
    * ``pick``：排除「本队自己」本系列赛已选用过的英雄（全局 BP 按队各算各的）；
    * ``ban``：排除「**对方**」本系列赛已选用过的英雄 —— 禁人池 = 对方还能选的池子，
      所以我方用过的英雄照样可以禁（我方本来就选不了它，禁掉只是封对方），
      而对方用过的英雄我禁了没意义，因此从禁人池里剔除；
    * 巅峰对决是盲选：解除全局 BP 限制（``used_global`` 不参与排除），
      且允许双方撞同一个英雄（只排除本队本局已选）。
    """
    excluded: set = set()
    tiebreak = bool(match.get("is_tiebreak"))
    side = for_side or str(match.get("side") or "")
    action = str(for_action or match.get("action") or "pick").strip().lower()
    same_game_sides = (side,) if (tiebreak and side) else SIDES
    for key in ("bans", "picks"):
        block = match.get(key)
        if not isinstance(block, dict):
            continue
        for one_side in same_game_sides:
            for value in block.get(one_side) or []:
                ename = _as_int(value)
                if ename is not None:
                    excluded.add(ename)
    # 全局 BP：pick 看本队历史，ban 看对方历史（巅峰对决清空）
    if not tiebreak and side:
        pool_side = _opposite_side(side) if action == "ban" else side
        block = match.get("used_global")
        if isinstance(block, dict):
            for value in block.get(pool_side) or []:
                ename = _as_int(value)
                if ename is not None:
                    excluded.add(ename)
    # 元流之子等多形态英雄：一个形态被占用 → 整个家族都不可用
    return _expand_by_family(match, excluded)


def _build_available(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按契约 §3 重算 ``available``：剔除已 ban/pick + 全局已用，保持 §2 排序。

    每项带上 ``top_score``（AI 同一套公式给出的 0–100 分，仅作提示，不影响排序）。
    """
    ctx = _ctx(match)
    side = str(match.get("side") or "blue")
    action = str(match.get("action") or "pick")
    # v20：巅峰对决盲选时按「面板当前操作方」算可选池（双方各自只排自己选过的）
    if match.get("is_tiebreak"):
        side = tiebreak_focus_side(match)
        action = "pick"
    # v17：pick 排本队历史选用，ban 排对方历史选用（禁人池 ≠ 我方可选池）
    excluded = _excluded_enames(match, side, action)
    items: List[Dict[str, Any]] = []
    for hero in ctx.hero_list():
        ename = _as_int(hero.get("ename"))
        if ename is None or ename in excluded:
            continue
        top_score: Optional[float] = None
        try:
            top_score = float(
                ai_coach.evaluate_candidate(match, side, ename, ctx, action)["score"]
            )
        except Exception:  # pragma: no cover - AI 异常绝不能影响 BP 主流程
            top_score = None
        items.append(data_loader.hero_api_item(hero, top_score))
    return items


# --------------------------------------------------------------------------- #
# 英雄面板（v5 需求 6/7）：全量英雄 + 分路分组 + 置灰原因
# --------------------------------------------------------------------------- #

#: 官方主分路兜底表（仅当该英雄在所选赛季**没有**任何 KPL 实战分路数据时使用，
#: 保证「英雄全部加载展示」不会因为数据缺失而漏掉任何一个英雄）。
#: 数据优先级：hero["lane_list"]（KPL 实战分路，权威）> lane_official（官网官方分路）> hero_type 兜底。
_HERO_TYPE_LANE: Dict[int, str] = {
    1: "对抗路", 2: "中路", 3: "对抗路", 4: "打野", 5: "发育路", 6: "游走",
}


def hero_main_lane(hero: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """返回 ``(主分路, 是否来自 KPL 实战数据)``。

    ``lane_list`` 已按赛季出场局数降序排列，取第一个即「最常走的分路」。
    """
    lanes = hero.get("lane_list")
    if isinstance(lanes, list):
        for name in lanes:
            if isinstance(name, str) and name in data_loader.LANES:
                return name, True
    # 兜底 1：王者荣耀官网 herolist.json 的官方分路（roles + extra_cold_lane，
    # v43 才按正确 code 表解析；KPL 没出场过的英雄靠它拿到正确默认分路）
    official = hero.get("lane_official")
    if isinstance(official, list):
        for name in official:
            if isinstance(name, str) and name in data_loader.LANES:
                return name, False
    # 兜底 2：老数据里只有 role（内容同官方分路）
    roles = hero.get("role")
    if isinstance(roles, list):
        for name in roles:
            if isinstance(name, str) and name in data_loader.LANES:
                return name, False
    # 兜底 3：herolist.json 的 hero_type（战士/法师/坦克/刺客/射手/辅助）
    raw_type = hero.get("hero_type")
    htype = _as_int(raw_type)
    if htype is None:
        # herolist.json 的 hero_type 是字符串（"1".."6"），这里补一次转换
        try:
            htype = int(str(raw_type).strip())
        except (TypeError, ValueError):
            htype = None
    if htype is not None:
        name = _HERO_TYPE_LANE.get(htype)
        if name:
            return name, False
    return None, False


def _build_hero_board(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    """**全量**英雄面板：所有英雄都在，附 ``disabled`` / ``reason`` / 主分路。

    v5 需求 7：全局 BP 下被 ban、或本系列赛已选用、或本小局已用的英雄
    不再从面板消失，而是 ``disabled=true`` + 中文 ``reason``，由前端置灰。
    """
    ctx = _ctx(match)
    side = str(match.get("side") or "blue")
    action = str(match.get("action") or "pick")
    tiebreak = bool(match.get("is_tiebreak"))
    if tiebreak:
        # 盲选时面板代表「当前操作方」，只排该方自己选过的英雄
        side = tiebreak_focus_side(match)
        action = "pick"

    own_picks = set(_int_list((match.get("picks") or {}).get(side)))
    opp_side = "red" if side == "blue" else "blue"
    opp_picks = set(_int_list((match.get("picks") or {}).get(opp_side)))
    cur_bans = set(_int_list((match.get("bans") or {}).get("blue"))) | set(
        _int_list((match.get("bans") or {}).get("red"))
    )
    # 全局 BP：pick 排本队整个 BO 选过的英雄，ban 排对方选过的（巅峰对决解除）
    if tiebreak:
        own_used = set()
    else:
        pool_side = opp_side if action == "ban" else side
        own_used = set(_int_list((match.get("used_global") or {}).get(pool_side)))

    usable: set = set()
    if str(match.get("phase") or "bp") in ("bp", PHASE_TIEBREAK_PICK):
        for item in (match.get("available") or []):
            value = _as_int(item.get("ename")) if isinstance(item, dict) else _as_int(item)
            if value is not None:
                usable.add(value)

    family_map = hero_family_map(ctx)
    family_taken = set(own_picks if tiebreak else (own_picks | opp_picks | cur_bans | own_used))
    taken_families = {family_map[one] for one in family_taken if one in family_map}

    board: List[Dict[str, Any]] = []
    for hero in ctx.hero_list():
        ename = _as_int(hero.get("ename"))
        if ename is None:
            continue
        lane, official = hero_main_lane(hero)
        reason = ""
        if ename in own_picks:
            reason = "本局已选"
        elif ename in opp_picks and not tiebreak:
            # 同一小局英雄不重复：对方已选的英雄本队也不能再选（巅峰对决盲选除外）
            reason = "对方本局已选"
        elif not tiebreak and ename in cur_bans:
            reason = "本局已被 ban"
        elif not tiebreak and ename in own_used:
            reason = ("对方本系列赛已选用（全局 BP，禁了也没意义）" if action == "ban"
                      else "本队本系列赛已选用（全局 BP）")
        elif tiebreak and action == "pick" and ename in opp_picks:
            # 巅峰对决是盲选，允许与对方撞英雄 —— 不算受限
            reason = ""
        elif family_map.get(ename) in taken_families:
            # 元流之子：同一英雄的其他分路形态已被占用（本局 ban/pick 或本系列赛已用）
            reason = "%s的其他分路已被占用（同一英雄）" % family_map.get(ename)
        # 引擎口径兜底：available 是唯一的“可选”权威，面板绝不能比它更宽松
        if not reason and usable and ename not in usable:
            reason = "当前不可选（已被 ban 或已选择）"
        disabled = bool(reason)
        item = data_loader.hero_api_item(hero)
        item["lane_main"] = lane
        item["lane_main_official"] = official
        item["disabled"] = disabled
        item["reason"] = reason
        board.append(item)
    return board


# --------------------------------------------------------------------------- #
# 巅峰对决盲选（v20，用户 m11997）
# --------------------------------------------------------------------------- #
#
# KPL 官方巅峰对决流程：
#   1. 没有 ban 位，双方**同时**各盲选 5 个英雄，过程中谁也不知道对方选了什么；
#   2. 允许双方撞同一个英雄（盲选的必然结果，不算冲突）；
#   3. 双方都选完之后，再按「对抗路 → 打野 → 中路 → 发育路 → 游走」逐路公布阵容。
#
# 实现方式：``phase = "tiebreak_pick"`` 期间 ``apply_action`` 不校验「轮到谁」，
# 而是接受一个显式的 ``side``（没传就按玩家方 / 蓝方兜底），双方各自计数到 5；
# 双方都满 5 人时引擎先算出 ``match["tiebreak_reveal"]``（5 行，每行一个分路 +
# 双方对应英雄）再照常结算，前端负责按分路顺序逐行揭晓。


def is_tiebreak_match(match: Dict[str, Any]) -> bool:
    """本小局是否巅峰对决（BO7 3:3 / BO9 4:4 的决胜局）。"""
    return bool(isinstance(match, dict) and match.get("is_tiebreak"))


def tiebreak_picks(match: Dict[str, Any], side: str) -> List[int]:
    """某一方在盲选里已经选好的英雄（按选择先后）。"""
    picks = match.get("picks") or {}
    return _int_list(picks.get(side)) if isinstance(picks, dict) else []


def tiebreak_ready(match: Dict[str, Any]) -> bool:
    """双方是否都已完成 5 个盲选。"""
    return all(
        len(tiebreak_picks(match, side)) >= TIEBREAK_PICKS_PER_SIDE for side in SIDES
    )


def _tiebreak_sides_open(match: Dict[str, Any]) -> List[str]:
    """还没选满 5 个的一方（按 蓝→红 顺序）。"""
    return [
        side for side in SIDES
        if len(tiebreak_picks(match, side)) < TIEBREAK_PICKS_PER_SIDE
    ]


def tiebreak_focus_side(match: Dict[str, Any]) -> str:
    """盲选阶段「面板默认操作方」。

    * ``mode == "ai"``：永远是玩家方（AI 那一半由后端代选）；
    * 其它模式：蓝方优先，蓝方选满了就看红方（双人/自由 BP 时同一个人轮流选两边）。
    """
    mode = str(match.get("mode") or "ai").strip().lower()
    if mode == "ai":
        side = str(match.get("player_side") or "blue")
        if side not in SIDES:
            side = "blue"
        if len(tiebreak_picks(match, side)) < TIEBREAK_PICKS_PER_SIDE:
            return side
    for side in SIDES:
        if len(tiebreak_picks(match, side)) < TIEBREAK_PICKS_PER_SIDE:
            return side
    return str(match.get("player_side") or "blue")


def _hero_cell(ctx: Any, ename: Optional[int]) -> Optional[Dict[str, Any]]:
    """揭晓用的英雄信息（头像 + 名字 + 该赛季数据）。"""
    if ename is None:
        return None
    hero = {}
    try:
        hero = ctx.hero(ename) or {}
    except Exception:  # pragma: no cover - 数据缺失不该影响结算
        hero = {}
    item = data_loader.hero_api_item(hero) if hero else {}
    item["ename"] = ename
    if not item.get("cname"):
        try:
            item["cname"] = ctx.cname(ename)
        except Exception:  # pragma: no cover
            item["cname"] = str(ename)
    lane, _official = hero_main_lane(hero) if hero else (None, False)
    item["lane_main"] = lane
    return item


def _assign_lanes(picks: List[int], ctx: Any) -> Dict[str, Optional[int]]:
    """把一方的 5 个英雄按分路摆好（对抗路 → 打野 → 中路 → 发育路 → 游走）。

    优先级：正中该分路（``lane_list`` 首位命中）→ 能走该分路（其它位命中）→
    剩下没被任何分路认领的英雄按顺序补齐。绝不丢英雄、也不重复。
    """
    pool = [ename for ename in picks]
    result: Dict[str, Optional[int]] = {lane: None for lane in data_loader.LANES}
    # 第一轮：正中分路的先落位
    for lane in data_loader.LANES:
        for ename in list(pool):
            hero = {}
            try:
                hero = ctx.hero(ename) or {}
            except Exception:  # pragma: no cover
                hero = {}
            main, _official = hero_main_lane(hero) if hero else (None, False)
            if main == lane:
                result[lane] = ename
                pool.remove(ename)
                break
    # 第二轮：能走这条分路的补位
    for lane in data_loader.LANES:
        if result[lane] is not None:
            continue
        for ename in list(pool):
            hero = {}
            try:
                hero = ctx.hero(ename) or {}
            except Exception:  # pragma: no cover
                hero = {}
            lanes = hero.get("lane_list") if isinstance(hero.get("lane_list"), list) else []
            if lane in lanes:
                result[lane] = ename
                pool.remove(ename)
                break
    # 第三轮：剩下的人按顺序塞进还空着的分路
    for lane in data_loader.LANES:
        if result[lane] is None and pool:
            result[lane] = pool.pop(0)
    return result


def build_tiebreak_reveal(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按分路顺序生成揭晓数据：5 行，每行 ``{lane, blue, red}``。"""
    ctx = _ctx(match)
    blue_map = _assign_lanes(tiebreak_picks(match, "blue"), ctx)
    red_map = _assign_lanes(tiebreak_picks(match, "red"), ctx)
    rows: List[Dict[str, Any]] = []
    for lane in data_loader.LANES:
        rows.append(
            {
                "lane": lane,
                "blue": _hero_cell(ctx, blue_map.get(lane)),
                "red": _hero_cell(ctx, red_map.get(lane)),
            }
        )
    return rows


def _apply_tiebreak_pick(
    match: Dict[str, Any],
    ename: Any,
    action: Optional[str] = None,
    side: Optional[str] = None,
) -> Dict[str, Any]:
    """巅峰对决盲选：接受任意一方的一手 pick（不校验「轮到谁」）。"""
    if action is not None and str(action).strip().lower() not in ("", "pick"):
        raise BPError("巅峰对决是盲选，只能 pick，没有 ban")
    picks = match.get("picks")
    if not isinstance(picks, dict):
        picks = {"blue": [], "red": []}
        match["picks"] = picks
    acting = str(side or "").strip().lower()
    if acting not in SIDES:
        open_sides = _tiebreak_sides_open(match)
        if not open_sides:
            raise BPError("双方盲选都已完成")
        acting = tiebreak_focus_side(match)
        if acting not in open_sides:
            acting = open_sides[0]
    if len(tiebreak_picks(match, acting)) >= TIEBREAK_PICKS_PER_SIDE:
        raise BPError("%s方盲选已满 %d 人" % (_SIDE_CN.get(acting, acting), TIEBREAK_PICKS_PER_SIDE))
    target = _as_int(ename)
    if target is None:
        raise BPError("ename 必须是英雄数字 ID（整数），收到：%r" % (ename,))
    ctx = _ctx(match)
    exists = True
    try:
        exists = bool(ctx.hero(target))
    except Exception:  # pragma: no cover - 数据层异常不挡住主流程
        exists = True
    if not exists:
        raise BPError("英雄 %s 不存在" % target)
    own = set(tiebreak_picks(match, acting))
    if target in own:
        raise BPError("英雄 %s 本队已经选过了（盲选不能重复）" % ctx.cname(target))
    mates = [one for one in hero_family_members(ctx, target) if one != target]
    if any(one in own for one in mates):
        raise BPError("英雄 %s 的其他分路已经选过了（同一个英雄只能上一次）"
                      % hero_family_map(ctx).get(target))
    history = match.get("history")
    if not isinstance(history, list):
        history = []
        match["history"] = history
    history.append(
        {
            "step": len(history) + 1,
            "side": acting,
            "action": "pick",
            "ename": target,
            "hero": ctx.cname(target),
            "tiebreak": True,
        }
    )
    picks.setdefault(acting, []).append(target)
    used_global = match.get("used_global")
    if isinstance(used_global, dict):
        bucket = used_global.setdefault(acting, [])
        if target not in bucket:
            bucket.append(target)
    match["side"] = acting
    match["action"] = "pick"
    refresh_match(match)
    return match


def fill_tiebreak_side(match: Dict[str, Any], side: str, *, fast: bool = True) -> int:
    """把某一方的盲选补满 5 人（AI 代选 / 一键 BP 用），返回补了几手。"""
    if not is_tiebreak_match(match) or match.get("finished"):
        return 0
    side = str(side or "").strip().lower()
    if side not in SIDES:
        return 0
    ctx = _ctx(match)
    filled = 0
    guard = 0
    while (len(tiebreak_picks(match, side)) < TIEBREAK_PICKS_PER_SIDE
           and not match.get("finished") and guard < TIEBREAK_PICKS_PER_SIDE + 2):
        guard += 1
        # v20 修复：AI 打分每次都偏向同一个版本强势英雄，盲选里已经选过它时
        # 必须换下一个可选项，否则 fill 会一直 BPError 卡住（只剩 4/5）。
        choices = _tiebreak_candidates(match, side)
        if not choices:
            break
        ename = None
        try:
            if fast:
                ename = _as_int(ai_coach.decide(match, side, ctx, action="pick"))
            else:
                ename = _as_int(ai_coach.choose_action(match, side, ctx, action="pick"))
        except Exception:                       # noqa: BLE001 AI 异常时退回第一个可选
            ename = None
        if ename is None or ename not in choices:
            ename = choices[0]
        try:
            _apply_tiebreak_pick(match, ename, "pick", side=side)
        except BPError:
            # 仍然失败（理论上不会发生）→ 换一个没被选过的再试
            remaining = [e for e in choices if e != ename]
            if not remaining:
                break
            try:
                _apply_tiebreak_pick(match, remaining[0], "pick", side=side)
            except BPError:
                break
        filled += 1
    return filled


def _tiebreak_candidates(match: Dict[str, Any], side: str) -> List[int]:
    """某一方在盲选里还能选的英雄（只排自己选过的）。"""
    ctx = _ctx(match)
    excluded = set(tiebreak_picks(match, side))
    out: List[int] = []
    for hero in ctx.hero_list():
        ename = _as_int(hero.get("ename"))
        if ename is None or ename in excluded:
            continue
        out.append(ename)
    return out


def _int_list(values: Any) -> List[int]:
    out: List[int] = []
    if isinstance(values, (list, tuple)):
        for value in values:
            number = _as_int(value)
            if number is not None:
                out.append(number)
    return out


def _pick_number(match: Dict[str, Any], side: str) -> int:
    """该方本小局已经 pick 了几个（用于 ``N楼`` 文案）。"""
    picks = match.get("picks") or {}
    value = picks.get(side) if isinstance(picks, dict) else None
    return len(value) if isinstance(value, list) else 0


def _ban_number(match: Dict[str, Any], side: str) -> int:
    bans = match.get("bans") or {}
    value = bans.get(side) if isinstance(bans, dict) else None
    return len(value) if isinstance(value, list) else 0


def _turn_label(match: Dict[str, Any], side: str, action: str) -> str:
    """契约 §3 ``turn_label``，例如 ``→ 蓝方 Pick (1楼)`` / ``→ 红方 Ban (1禁)``。"""
    cn = _SIDE_CN.get(side, side)
    if action == "pick":
        return "→ %s方 Pick (%d楼)" % (cn, _pick_number(match, side) + 1)
    return "→ %s方 Ban (%d禁)" % (cn, _ban_number(match, side) + 1)


def _final_label(match: Dict[str, Any]) -> str:
    winner = (match.get("final") or {}).get("winner")
    if winner == "blue":
        return "蓝方胜"
    if winner == "red":
        return "红方胜"
    if winner == "draw":
        return "平局"
    return "BP 结束"


# --------------------------------------------------------------------------- #
# 结束本小局
# --------------------------------------------------------------------------- #


_TIE_BREAK_KEYS = ("version", "lane", "system", "weakness")


def _tie_break_winner(match: Dict[str, Any], blue: Dict[str, Any],
                      red: Dict[str, Any]) -> str:
    """阵容总分相同时的确定性判胜函数。

    依次比较：版本强度 → 分路覆盖 → 体系完整度 → 短板，全部相同则用
    ``match_id`` 的稳定哈希决定（同一场永远同一个结果，不用随机数）。
    """
    b = blue.get("breakdown") or {}
    r = red.get("breakdown") or {}
    for key in _TIE_BREAK_KEYS:
        bv = _as_int(b.get(key)) or 0
        rv = _as_int(r.get(key)) or 0
        if bv != rv:
            return "blue" if bv > rv else "red"
    seed = "%s|%s|%s" % (
        match.get("match_id") or "",
        ",".join(str(x) for x in (match.get("picks") or {}).get("blue") or []),
        ",".join(str(x) for x in (match.get("picks") or {}).get("red") or []),
    )
    return "blue" if (zlib.crc32(seed.encode("utf-8")) & 1) == 0 else "red"


def _finish_game(match: Dict[str, Any]) -> None:
    """本小局 20/10 步走完：算分、判胜负、记大比分、决定系列赛是否结束。

    v2 追加（契约 §3）：记录 ``series_games``，并给下一局写好 ``side_choice``
    （败方选边）与 ``side_assignment``。
    """
    ctx = _ctx(match)
    _ensure_side_lists(match)
    if is_tiebreak_match(match) and not isinstance(match.get("tiebreak_reveal"), list):
        # v20：巅峰对决无论从哪条路径结算，都先把「按分路公布」的数据准备好
        try:
            match["tiebreak_reveal"] = build_tiebreak_reveal(match)
        except Exception:  # pragma: no cover - 数据缺失不该挡住结算
            match["tiebreak_reveal"] = []
    blue_picks = list(match["picks"]["blue"])  # type: ignore[index]
    red_picks = list(match["picks"]["red"])  # type: ignore[index]

    blue = scorer.score_lineup(blue_picks, red_picks, ctx)
    red = scorer.score_lineup(red_picks, blue_picks, ctx)
    # v49：结算界面的「小分详情」——五项构成说明 + 按分路逐条对比双方
    blue_detail = red_detail = None
    detail_fn = getattr(scorer, "score_detail", None)
    if detail_fn is not None:
        try:
            blue_detail = detail_fn(blue_picks, red_picks, ctx)
            red_detail = detail_fn(red_picks, blue_picks, ctx)
        except Exception:  # noqa: BLE001 - 详情失败不能挡住结算
            blue_detail = red_detail = None

    blue_score = _as_int(blue.get("score")) or 0
    red_score = _as_int(red.get("score")) or 0
    if blue_score > red_score:
        winner = "blue"
    elif red_score > blue_score:
        winner = "red"
    else:
        # KPL 真实赛制里一个小局**一定有胜负**（没有平局）。
        # 阵容总分打平时按下面顺序逐级比，最后用 match_id 做稳定哈希兜底，
        # 保证同一场重算结果一致、且不会出现 0:0 的系列赛。
        winner = _tie_break_winner(match, blue, red)

    if winner == "draw":
        # 平局时 mvp_side 用体系分兜底，保证字段仍是 "blue" / "red"
        b_sys = (blue.get("breakdown") or {}).get("system") or 0
        r_sys = (red.get("breakdown") or {}).get("system") or 0
        mvp_side = "blue" if b_sys >= r_sys else "red"
    else:
        mvp_side = winner

    # 契约 v2 §3：平局没有“败方”，用 mvp_side（体系分高的一方）当作胜方，
    # 另一方即败方，保证选边链路永远有唯一 chooser。
    winner_team = _blue_name(match) if mvp_side == "blue" else _red_name(match)
    loser_side = _opposite_side(mvp_side)
    loser_team = _blue_name(match) if loser_side == "blue" else _red_name(match)

    wins = team_wins(match)
    if winner != "draw":
        wins[winner_team] = wins.get(winner_team, 0) + 1
    elif winner_team:
        # 平局不计入大比分（v1 语义），但仍记录谁被视作胜方用于选边
        pass
    match["team_wins"] = wins

    bo = _as_int(match.get("bo")) or 1
    game_index = _as_int(match.get("game_index")) or 1
    need = _need_wins(bo)
    series_over = _series_over(match)

    match["finished"] = True
    match["series_over"] = bool(series_over)
    match["need_wins"] = need
    match["result"] = "draw" if winner == "draw" else "%s_win" % winner
    # 契约 §3：final 的每一侧都带 picks / score / breakdown / notes / missing
    blue_block = dict(blue)
    blue_block["picks"] = list(blue_picks)
    blue_block["team"] = _blue_name(match)
    blue_block["detail"] = blue_detail
    red_block = dict(red)
    red_block["picks"] = list(red_picks)
    red_block["team"] = _red_name(match)
    red_block["detail"] = red_detail
    match["final"] = {
        "blue": blue_block,
        "red": red_block,
        "winner": winner,
        "mvp_side": mvp_side,
        "bo_after": {"blue": int(wins.get(_blue_name(match), 0)),
                     "red": int(wins.get(_red_name(match), 0))},
    }
    _sync_score(match)

    # 契约 v2 §3.2：series_games 记录（供前端显示大比分与前几局阵营）
    games = match.get("series_games")
    if not isinstance(games, list):
        games = []
        match["series_games"] = games
    games.append(
        {
            "game_index": game_index,
            "blue_team": _blue_name(match),
            "red_team": _red_name(match),
            "winner_side": mvp_side,
            "winner_team": winner_team,
            "loser_team": loser_team,
            "blue_score": blue_score,
            "red_score": red_score,
        }
    )

    match["available"] = []
    match["turn_label"] = _final_label(match)

    # 契约 v2 §3.2 phase 枚举：bp → result → side_choice → bp … → done
    if series_over:
        match["phase"] = "done"
        match["side_choice"] = {
            "required": False,
            "reason": "series_over",
            "chooser_team": None,
            "chooser_role": None,
            "chosen_side": None,
            "resolved": True,
            "note": "系列赛已结束，不会再选边",
        }
    else:
        match["phase"] = "result"
        _prepare_side_choice(match, loser_team)


def _prepare_side_choice(match: Dict[str, Any], loser_team: str) -> None:
    """给下一局写 ``side_choice`` / ``side_assignment`` 草案（契约 v2 §3.2）。

    真正生效（换边 + 进 BP）在 :func:`next_game` / :func:`choose_side`。
    """
    bo = _as_int(match.get("bo")) or 1
    next_game_index = (_as_int(match.get("game_index")) or 1) + 1
    if bo <= 1:
        match["side_choice"] = {
            "required": False,
            "reason": "single_game",
            "chooser_team": None,
            "chooser_role": None,
            "chosen_side": None,
            "resolved": True,
            "note": "单局模式，关闭败方选边",
        }
        return
    role = _chooser_role(match, loser_team)
    match["side_choice"] = {
        "required": True,
        "reason": "loser_choice",
        "chooser_team": loser_team,
        "chooser_role": role,
        "chosen_side": None,
        "resolved": False,
        "next_game_index": next_game_index,
        "note": "本局由 %s（上局败方）选边" % loser_team,
    }


# --------------------------------------------------------------------------- #
# 状态刷新
# --------------------------------------------------------------------------- #


def refresh_match(match: Dict[str, Any]) -> Dict[str, Any]:
    """按当前步重算 ``side`` / ``action`` / ``total_steps`` / ``available`` / ``turn_label``。

    ``app.py`` 从内存取回 match 后可以调用它做一次自愈（不是必须的）。

    v2：``phase == "side_choice"`` 时不重建 ``available``（契约 §3.3：``available=[]``），
    也不会把 phase 覆写成 ``"bp"``。
    """
    if not isinstance(match, dict):
        raise BPError("match 必须是 dict")
    _ensure_side_lists(match)
    steps = _steps_for(match)
    match["total_steps"] = len(steps)
    if match.get("finished"):
        match["available"] = []
        match["turn_label"] = _final_label(match)
        # v38：自愈时也把面板刷到最终状态（结算界面不再出现“还能选”的英雄）
        try:
            match["hero_board"] = _build_hero_board(match)
        except Exception:  # pragma: no cover
            pass
        return match
    phase = match.get("phase")
    if phase == "side_choice":
        # 选边阶段：不进入 BP，available 必须为空
        match["phase"] = "side_choice"
        match["step"] = 0
        match["available"] = []
        choice = match.get("side_choice") or {}
        chooser = _team_name_of(choice.get("chooser_team")) or "待定"
        match["turn_label"] = "→ %s 选边（蓝色方 / 红色方）" % chooser
        return match
    # v20：巅峰对决盲选（不按 steps 轮转，双方同时各选 5 人）
    if is_tiebreak_match(match):
        history = match.get("history")
        done = len(history) if isinstance(history, list) else 0
        if tiebreak_ready(match):
            # 双方都选满 5 人 → 先把「按分路公布」的数据算好留给前端逐行揭晓，
            # 再照常结算（前端拿到 tiebreak_reveal 后做公布动画）。
            if not isinstance(match.get("tiebreak_reveal"), list):
                match["tiebreak_reveal"] = build_tiebreak_reveal(match)
            match["step"] = done
            match["available"] = []
            _finish_game(match)
            return match
        side = tiebreak_focus_side(match)
        match["phase"] = PHASE_TIEBREAK_PICK
        match["step"] = done + 1
        match["side"] = side
        match["action"] = "pick"
        match["available"] = _build_available(match)
        match["hero_board"] = _build_hero_board(match)
        match["turn_label"] = "→ %s方 盲选（%d/%d）" % (
            _SIDE_CN.get(side, side),
            len(tiebreak_picks(match, side)),
            TIEBREAK_PICKS_PER_SIDE,
        )
        return match
    step = _as_int(match.get("step")) or 1
    if step < 1:
        step = 1
    if step > len(steps):
        # 防御：步数越界但未标记结束（外部改坏了状态）→ 直接结算
        match["step"] = len(steps)
        _finish_game(match)
        return match
    match["step"] = step
    side, action = steps[step - 1]
    match["side"] = side
    match["action"] = action
    match["phase"] = "bp"
    match["available"] = _build_available(match)
    match["hero_board"] = _build_hero_board(match)
    match["turn_label"] = _turn_label(match, side, action)
    return match


# --------------------------------------------------------------------------- #
# 创建对局
# --------------------------------------------------------------------------- #


def new_match(
    league_id: Optional[str] = None,
    blue_team: Any = None,
    red_team: Any = None,
    bo: int = 7,
    mode: str = "ai",
    side: str = "blue",
    difficulty: str = "pro",
) -> Dict[str, Any]:
    """按契约 §3 造一个可玩的对局。

    :param league_id: 赛季 ID；``None`` 时用默认赛季。
    :param blue_team: 蓝方队名（不属于该赛季时按契约兜底成前两名）。
    :param red_team: 红方队名（同上）。
    :param bo: 赛制，只接受 1/3/5/7。
    :param mode: ``"ai"``（玩家对战 AI）或 ``"free"``（双人对摆）。
    :param side: 玩家执边 ``"blue"`` / ``"red"``（``free`` 模式下仅用于文案）。
    :param difficulty: AI 难度 ``"rookie"`` / ``"pro"`` / ``"boss"``。

    ``mode == "ai"`` 且首手轮到 AI 时，返回前已把 AI 的手走完（``ai_moves`` 有记录）。
    """
    bo_value = _as_int(bo)
    if bo_value not in ALLOWED_BO:
        raise BPError("bo 只接受 %s，收到：%r" % ("/".join(str(v) for v in ALLOWED_BO), bo))
    mode_value = str(mode or "ai").strip().lower()
    if mode_value not in MODES:
        raise BPError("mode 只接受 %s，收到：%r" % ("/".join(MODES), mode))
    side_value = str(side or "blue").strip().lower()
    if side_value not in SIDES:
        raise BPError("side 只接受 %s，收到：%r" % ("/".join(SIDES), side))
    difficulty_value = str(difficulty or "pro").strip().lower()
    if difficulty_value not in DIFFICULTIES:
        raise BPError(
            "difficulty 只接受 %s，收到：%r" % ("/".join(DIFFICULTIES), difficulty)
        )

    ctx = data_loader.league_context(league_id)
    blue_brief, red_brief = resolve_teams(league_id, blue_team, red_team)

    game_index = 1
    # 首局大比分恒 0:0，不可能触发巅峰对决（传 {"blue":0,"red":0} 而非 match）
    is_tiebreak = _is_tiebreak_game(game_index, bo_value, {"blue": 0, "red": 0})
    steps = TIEBREAK_STEPS if is_tiebreak else BP_STEPS

    # ---- 契约 v2 §3.1：首局战队身份与默认阵营 ----
    blue_name = _team_name_of(blue_brief)
    red_name = _team_name_of(red_brief)
    if mode_value == "ai":
        # 契约 v2 §3.2：player_team / ai_team 是 {name, abbreviation, logo_path}
        # 契约 v2 §3.1：首局无历史 → 默认分配；玩家战队默认蓝方（side 入参为 blue 时），
        # 玩家显式选了 red 就把红方战队给玩家，AI 拿蓝方。整轮 BO 战队身份不变。
        if side_value == "blue":
            player_team = dict(blue_brief)
            ai_team = dict(red_brief)
        else:
            player_team = dict(red_brief)
            ai_team = dict(blue_brief)
    else:
        # v18：自由 BP / 生涯 / 联赛里「我方战队」同样要记下来（按队名，不按红蓝），
        # 否则换边后前端无法判断哪边是我们，胜负与比分就会颠倒。
        player_team = dict(blue_brief) if side_value == "blue" else dict(red_brief)
        ai_team = None

    match: Dict[str, Any] = {
        "match_id": _new_match_id(),
        "league_id": ctx.league_id,
        "league_name": ctx.league_name,
        "mode": mode_value,
        "difficulty": difficulty_value,
        "bo": bo_value,
        "player_side": side_value,
        "blue_team": blue_brief,
        "red_team": red_brief,
        "player_team": player_team,
        "ai_team": ai_team,
        "score": {"blue": 0, "red": 0},
        "team_wins": {},
        "series_games": [],
        "game_index": game_index,
        "phase": "bp",
        "step": 1,
        "action": steps[0][1],
        "side": steps[0][0],
        "total_steps": len(steps),
        "turn_label": "",
        "bans": {"blue": [], "red": []},
        "picks": {"blue": [], "red": []},
        "used_global": {"blue": [], "red": []},
        "hero_board": [],
        "available": [],
        "history": [],
        "ai_moves": [],
        "finished": False,
        "result": "in_progress",
        "final": None,
        "is_tiebreak": is_tiebreak,
        "created_at": _now_iso(),
        "need_wins": _need_wins(bo_value),
        "series_over": False,
        "side_choice": None,
        "side_assignment": None,
    }

    # 首局：无历史对局 → 默认分配阵营（契约 v2 §3.1 第 1 条）
    match["side_assignment"] = {
        "game_index": game_index,
        "mode": "default",
        "reason": "首局无历史对局，默认分配阵营",
    }
    if bo_value <= 1:
        match["side_choice"] = {
            "required": False,
            "reason": "single_game",
            "chooser_team": None,
            "chooser_role": None,
            "chosen_side": None,
            "resolved": True,
            "note": "单局模式，关闭败方选边",
        }
    else:
        match["side_choice"] = {
            "required": False,
            "reason": "first_game",
            "chooser_team": None,
            "chooser_role": None,
            "chosen_side": None,
            "resolved": True,
            "note": "首局无历史对局，默认分配阵营",
        }
    _recompute_player_side(match)

    refresh_match(match)

    # 契约：AI 先手时，创建完对局就应当轮到玩家
    if mode_value == "ai":
        auto_advance(match)
    return match


# --------------------------------------------------------------------------- #
# 执行一手
# --------------------------------------------------------------------------- #


def _available_enames(match: Dict[str, Any]) -> List[int]:
    result: List[int] = []
    for item in match.get("available") or []:
        value = item.get("ename") if isinstance(item, dict) else item
        ename = _as_int(value)
        if ename is not None:
            result.append(ename)
    return result


def apply_action(
    match: Dict[str, Any],
    ename: Any,
    action: Optional[str] = None,
    side: Optional[str] = None,
) -> Dict[str, Any]:
    """执行一手 BP（**原地修改并返回同一个 dict**）。

    非法情况 ``raise BPError``（中文消息，``app.py`` 映射成 409）：
    小局已结束 / 动作与当前步不符 / 英雄不在 ``available`` 内 / 参数非法。

    v20：巅峰对决走盲选分支 —— 不校验「轮到谁」，可由 ``side`` 显式指定哪一方
    落子（没传就按面板默认操作方）。
    """
    if not isinstance(match, dict):
        raise BPError("match 必须是 dict")
    _ensure_side_lists(match)

    if match.get("finished"):
        raise BPError("本小局已结束，请先进入下一小局（POST /api/bp/next_game）")
    phase = match.get("phase")
    if phase == PHASE_TIEBREAK_REVEAL:
        raise BPError("巅峰对决盲选已完成，请公布阵容后进入本小局")
    if is_tiebreak_match(match) and phase in (None, "bp", PHASE_TIEBREAK_PICK):
        return _apply_tiebreak_pick(match, ename, action, side)
    # 契约 v2 §3.3：选边阶段拒绝一切 BP 动作
    if phase == "side_choice":
        raise BPError("请先选择阵营（POST /api/bp/side_choice）")
    if phase not in (None, "bp"):
        raise BPError("当前阶段（%s）不接受 BP 动作" % phase)

    step = _as_int(match.get("step")) or 1
    steps = _steps_for(match)
    if step < 1 or step > len(steps):
        raise BPError("步数异常（step=%s），本小局共 %d 步" % (step, len(steps)))

    expected_side, expected_action = steps[step - 1]
    action_value = str(action or expected_action).strip().lower()
    if action_value not in ALL_ACTIONS:
        raise BPError("action 只接受 ban / pick，收到：%r" % (action,))
    if action_value != expected_action:
        raise BPError(
            "当前第 %d 步要求 %s（%s 方），收到 %s"
            % (step, expected_action, expected_side, action_value)
        )

    target = _as_int(ename)
    if target is None:
        raise BPError("ename 必须是英雄数字 ID（整数），收到：%r" % (ename,))
    if target not in _available_enames(match):
        raise BPError(
            "英雄 %s 当前不可%s（已被 ban/pick，或本队本赛制内已 pick 过）"
            % (data_loader.league_context(match.get("league_id")).cname(target), action_value)
        )

    ctx = data_loader.league_context(match.get("league_id"))
    match["history"].append(  # type: ignore[union-attr]
        {
            "step": step,
            "side": expected_side,
            "action": action_value,
            "ename": target,
            "hero": ctx.cname(target),
        }
    )
    if action_value == "ban":
        match["bans"][expected_side].append(target)  # type: ignore[index]
    else:
        match["picks"][expected_side].append(target)  # type: ignore[index]
        used = match["used_global"][expected_side]  # type: ignore[index]
        if target not in used:
            used.append(target)

    next_step = step + 1
    if next_step > len(steps):
        # 本小局已走完：step 停留在最后一手，避免出现 19/11 这种越界读数
        _finish_game(match)
        # v38：结算后按最终阵容重建一次面板，否则最后一手被选的英雄在结算界面上
        # 仍然显示为“可选”（hero_board 是上一步的快照）。
        try:
            match["hero_board"] = _build_hero_board(match)
        except Exception:  # pragma: no cover - 面板重建失败不影响结算
            pass
    else:
        match["step"] = next_step
        refresh_match(match)
    return match


# --------------------------------------------------------------------------- #
# 合法动作 / AI 代走
# --------------------------------------------------------------------------- #


def legal_actions(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    """当前可执行的 BP 动作（即 ``available`` 里每个英雄 + 当前动作）。"""
    if not isinstance(match, dict) or match.get("finished"):
        return []
    side = str(match.get("side") or "")
    action = str(match.get("action") or "")
    if side not in SIDES or action not in ALL_ACTIONS:
        return []
    result: List[Dict[str, Any]] = []
    for item in match.get("available") or []:
        if not isinstance(item, dict):
            continue
        ename = _as_int(item.get("ename"))
        if ename is None:
            continue
        top_score = item.get("top_score")
        result.append(
            {
                "ename": ename,
                "cname": item.get("cname") or str(ename),
                "lane": item.get("lane"),
                "avatar_path": item.get("avatar_path"),
                "avatar_ok": item.get("avatar_ok"),
                "win_rate": item.get("win_rate"),
                "pick_rate": item.get("pick_rate"),
                "ban_rate": item.get("ban_rate"),
                "has_stats": item.get("has_stats"),
                "top_score": top_score,
                "side": side,
                "action": action,
            }
        )
    return result


def _ai_should_move(match: Dict[str, Any], mode: str, start_side: Optional[str]) -> bool:
    """当前这一步是否该由本函数代走。"""
    side = match.get("side")
    if side not in SIDES:
        return False
    if mode == "ai":
        player_side = str(match.get("player_side") or "blue")
        if player_side not in SIDES:
            player_side = "blue"
        return side != player_side
    # free 模式：只代走“与调用时同一方”的连续若干手，绝不替对手方落子
    return side == start_side


def auto_complete(match: Dict[str, Any]) -> Dict[str, Any]:
    """一键 BP：把**本小局剩下所有 ban / pick**（双方都算）交给内置 AI 走完。

    与 :func:`auto_advance` 的区别：不区分「谁该走」，直接从当前步一路推到底，
    供「我不想 BP 了，一键结束本次 BP」按钮使用（v17 需求，用户 m09741）。
    走到 ``phase != "bp"``（变成 ``result``）或小局结束即停；异常时返回已推进的状态。
    """
    if not isinstance(match, dict):
        raise BPError("match 必须是 dict")
    _ensure_side_lists(match)
    phase_now = str(match.get("phase") or "bp")
    if match.get("finished") or phase_now not in ("bp", PHASE_TIEBREAK_PICK):
        return match
    # v20：巅峰对决盲选 —— 双方各 5 手没人轮转，直接把两边都补满再结算
    if is_tiebreak_match(match):
        for side in SIDES:
            fill_tiebreak_side(match, side, fast=True)
        return match
    ctx = _ctx(match)
    cur = match
    guard = 0
    limit = len(BP_STEPS) + len(TIEBREAK_STEPS) + 4
    while not cur.get("finished") and str(cur.get("phase") or "bp") == "bp" and guard < limit:
        guard += 1
        side = str(cur.get("side") or "blue")
        action = str(cur.get("action") or "pick")
        ename = None
        try:
            # v18b：一键 BP 是「帮玩家把剩余手一次走完」，不是拟人回合，
            # 所以走 decide()（无 sleep），否则 20 步 × 0.8~1.5s 要等 20 秒。
            ename = _as_int(ai_coach.decide(cur, side, ctx, action=action))
        except Exception:                       # noqa: BLE001 AI 异常时退回“第一个可选”
            ename = None
        if ename is None:
            choices = _available_enames(cur)
            ename = choices[0] if choices else None
        if ename is None:
            break
        nxt = apply_action(cur, ename, action)
        if isinstance(nxt, dict):
            cur = nxt
        if cur.get("ai_moves") is not None:
            pass
    return cur


def auto_advance(match: Dict[str, Any]) -> Dict[str, Any]:
    """让 AI 走完它该走的**所有**手，直到轮到玩家方或本小局结束。

    - ``mode == "ai"``：一直走到 ``side == player_side``（一次请求拿到完整轮转）。
    - ``mode == "free"``：只代走与进入本函数时同一方的连续手（最多 2 手），
      因为 ``/api/bp/auto`` 在双人模式下是“帮我走当前这一轮”。
    - 每次调用只停顿一次（``ai_coach.think_pause()``，v23：0.25~0.5s），
      之后连续多手不再逐手 sleep —— 否则 AI 连走两手玩家就要等一秒多。
    - 幂等：轮到玩家 / 已结束 / 无可选英雄时直接返回，不报错。
    """
    if not isinstance(match, dict):
        raise BPError("match 必须是 dict")
    _ensure_side_lists(match)
    if match.get("finished"):
        return match
    # v20：巅峰对决盲选 —— AI 只补自己那一方（玩家方由玩家自己点）
    if is_tiebreak_match(match) and str(match.get("phase") or "") in (
        "", "bp", PHASE_TIEBREAK_PICK
    ):
        mode = str(match.get("mode") or "ai").strip().lower()
        if mode != "ai":
            return match
        player_side = str(match.get("player_side") or "blue")
        if player_side not in SIDES:
            player_side = "blue"
        ai_side = _opposite_side(player_side)
        if len(tiebreak_picks(match, ai_side)) < TIEBREAK_PICKS_PER_SIDE:
            # v23：整方补满只在开头停顿一次，逐手 sleep 会让玩家干等一秒多
            ai_coach.think_pause()
            fill_tiebreak_side(match, ai_side, fast=True)
        refresh_match(match)
        return match
    if match.get("phase") not in (None, "bp"):
        return match
    mode = str(match.get("mode") or "ai").strip().lower()
    if mode not in MODES:
        mode = "ai"
    start_side = match.get("side")
    ctx = _ctx(match)
    moves = match["ai_moves"]  # type: ignore[assignment]

    guard = 0
    paused = False
    while not match.get("finished"):
        if not _ai_should_move(match, mode, start_side):
            break
        side = str(match.get("side"))
        action = str(match.get("action") or "pick")
        guard += 1
        if guard > len(BP_STEPS) + len(TIEBREAK_STEPS):
            break  # 防御：理论上不可能走到这里
        try:
            if paused:
                # v23：本次调用已经停顿过一次，后续连续手直接算，不再逐手 sleep
                ename = ai_coach.decide(match, side, ctx, action=action)
            else:
                paused = True
                ename = ai_coach.choose_action(match, side, ctx, action=action)
        except ai_coach.AICoachError:
            break
        except Exception:  # pragma: no cover - AI 出错不应该卡死对局
            break
        step_no = _as_int(match.get("step")) or 0
        try:
            apply_action(match, ename, action)
        except BPError:
            break
        moves.append(  # type: ignore[union-attr]
            {
                "step": step_no,
                "side": side,
                "action": action,
                "ename": ename,
                "hero": ctx.cname(ename),
            }
        )
    return match


# --------------------------------------------------------------------------- #
# 下一小局
# --------------------------------------------------------------------------- #


def _reset_game_state(
    match: Dict[str, Any],
    game_index: int,
    is_tiebreak: bool,
    steps: Tuple[Tuple[str, str], ...],
) -> None:
    """把逐局状态清空，准备开新一局（**不动** ``used_global`` / ``side_choice``）。"""
    match["game_index"] = game_index
    match["is_tiebreak"] = is_tiebreak
    match["bans"] = {"blue": [], "red": []}
    match["picks"] = {"blue": [], "red": []}
    match["history"] = []
    match["ai_moves"] = []
    match["available"] = []
    match["finished"] = False
    match["final"] = None
    match["result"] = "in_progress"
    match["step"] = 1
    match["side"] = steps[0][0]
    match["action"] = steps[0][1]
    match["total_steps"] = len(steps)
    match["need_wins"] = _need_wins(_as_int(match.get("bo")) or 1)
    match.pop("tiebreak_reveal", None)   # v20：上一局的盲选公布数据不能带进新局


def _apply_side_choice(
    match: Dict[str, Any],
    side: str,
    *,
    chosen_by: str = "player",
    note: Optional[str] = None,
) -> str:
    """把「选边」落到状态上：换阵营 + 重算 ``player_side`` + 记账。

    返回选边者之前的阵营（``"blue"`` / ``"red"`` / ``""``），方便调用方写日志。
    """
    if side not in SIDES:
        raise BPError("side 只接受 blue / red，收到：%r" % (side,))
    choice = match.get("side_choice")
    if not isinstance(choice, dict):
        choice = {}
        match["side_choice"] = choice
    chooser_team = _team_name_of(choice.get("chooser_team"))
    previous = _side_of_team(match, chooser_team) or ""
    # 契约 v2 §3.1 第 5 条：只改「战队 → 阵营」归属，BP 时序完全不变
    if previous and previous != side:
        _swap_camps(match)
    _recompute_player_side(match)

    game_index = _as_int(match.get("game_index")) or 1
    choice["chosen_side"] = side
    choice["resolved"] = True
    choice["chosen_by"] = chosen_by
    if note:
        if chosen_by == "ai":
            choice["auto_note"] = note
        choice["choice_note"] = "%s 选择%s方（%s）" % (chooser_team or "选边方", _SIDE_CN[side], note)
    else:
        choice["choice_note"] = "%s 选择%s方" % (chooser_team or "选边方", _SIDE_CN[side])

    match["side_assignment"] = {
        "game_index": game_index,
        "mode": "loser_choice" if chooser_team else "default",
        "reason": choice["choice_note"],
    }
    return previous


def _enter_side_choice_phase(match: Dict[str, Any]) -> None:
    """进入「等待玩家选边」阶段（契约 v2 §3.3）。

    ``step = 0``、``available = []``、``history = []``；``side`` / ``action`` 保留为
    首步值，这样前端如果要复用常规 BP 面板也不会读到 ``None``。
    """
    choice = match.get("side_choice")
    if not isinstance(choice, dict):
        choice = {}
        match["side_choice"] = choice
    match["phase"] = "side_choice"
    match["step"] = 0
    match["total_steps"] = len(_steps_for(match))
    match["available"] = []
    match["history"] = []
    match["picks"] = {"blue": [], "red": []}
    match["bans"] = {"blue": [], "red": []}
    chooser = _team_name_of(choice.get("chooser_team")) or "选边方"
    match["turn_label"] = "→ %s（上局败方）选边：蓝色方 / 红色方" % chooser


def _enter_bp(match: Dict[str, Any]) -> None:
    """切到 BP 阶段并从第 1 步开始；``mode == "ai"`` 时顺手把 AI 的手走完。

    v20：巅峰对决不走步数轮转，而是进 ``tiebreak_pick``（双方盲选）；
    ai 模式下 AI 那一方一次性把 5 个盲选补满，玩家只需选自己这 5 个。
    """
    match["phase"] = "bp"
    match["step"] = 1
    _recompute_player_side(match)
    refresh_match(match)
    if str(match.get("mode") or "ai").strip().lower() == "ai":
        if is_tiebreak_match(match):
            player_side = str(match.get("player_side") or "blue")
            if player_side not in SIDES:
                player_side = "blue"
            fill_tiebreak_side(match, _opposite_side(player_side), fast=False)
            refresh_match(match)
        else:
            auto_advance(match)


def next_game(match: Dict[str, Any]) -> Dict[str, Any]:
    """进入下一小局（原地修改并返回同一个 dict）。

    契约 §2 / v2 §3.3：

    * 只有本小局已结束才能调用，否则 ``raise BPError``；
    * 系列赛已结束（``series_over``）时**幂等返回**，不抛异常；
    * ``bo == 1``：保持 v1，直接开下一小局（实际不会发生，因为单局必然 series_over）；
    * ``bo > 1`` 且败方是 **玩家**：停在 ``phase="side_choice"`` 等 ``choose_side()``；
    * ``bo > 1`` 且败方是 **AI**：后端自动选边（``ai_coach.choose_side`` 内含
      0.8~1.5s sleep）后直接进 ``phase="bp"`` 并跑 ``auto_advance``。
    * ``used_global`` **不重置**：BO 内同一战队已 pick 过的英雄不能再次 pick。
    """
    if not isinstance(match, dict):
        raise BPError("match 必须是 dict")
    _ensure_side_lists(match)
    if not match.get("finished"):
        raise BPError(
            "当前小局还没结束（phase=%s），不能进入下一小局" % match.get("phase")
        )
    if match.get("series_over"):
        return match

    bo = _as_int(match.get("bo")) or 1
    game_index = (_as_int(match.get("game_index")) or 1) + 1
    is_tiebreak = _is_tiebreak_game(game_index, bo, match.get("score"))
    steps = TIEBREAK_STEPS if is_tiebreak else BP_STEPS
    _reset_game_state(match, game_index, is_tiebreak, steps)

    choice = match.get("side_choice")
    if not isinstance(choice, dict):
        choice = {}
        match["side_choice"] = choice
    required = bool(choice.get("required"))

    if not required:
        # bo<=1 或系列赛已结束（后者上面已 return）：无需选边，直接开打
        _enter_bp(match)
        return match

    chooser_team = _team_name_of(choice.get("chooser_team"))
    role = str(choice.get("chooser_role") or "").strip().lower()
    if role not in ("player", "ai", "human"):
        role = _chooser_role(match, chooser_team)

    if role == "ai":
        # 契约 v2 §3.3：AI 自动选边（内部 sleep 0.8~1.5s）
        side = "blue"
        note = "该赛季无蓝红胜率数据，默认蓝色方"
        try:
            info = ai_coach.choose_side(_ctx(match), chooser_team)
            picked = str((info or {}).get("side") or "").strip().lower()
            if picked in SIDES:
                side = picked
            if (info or {}).get("note"):
                note = str(info["note"])
        except Exception:  # pragma: no cover - 自动选边失败不能卡住对局
            side, note = "blue", "自动选边失败，默认蓝色方"
        _apply_side_choice(match, side, chosen_by="ai", note=note)
        _enter_bp(match)
        return match

    # 玩家选边：停在 side_choice 等 /api/bp/side_choice
    _enter_side_choice_phase(match)
    return match


def choose_side(match: Dict[str, Any], side: Any) -> Dict[str, Any]:
    """败方选边（契约 v2 §3.3 ``POST /api/bp/side_choice``）。

    原地修改并返回同一个 dict。非法状态 ``raise BPError``（``app.py`` → 409）：

    * ``phase != "side_choice"`` → 409「当前不在选边阶段（phase=bp），BP 进行中不能切换阵营」
    * ``side_choice.required != true`` → 409「本局无需选边（reason=...）」
    * ``side`` 非法 → ``BPError``（``app.py`` 通常先行校验并返回 400）

    成功后：换阵营 → ``phase="bp"``、``step=1``、``bans/picks/history/ai_moves`` 清空、
    ``used_global`` **保留**、``player_side`` 重算、``available`` 重建；``mode == "ai"``
    时照常跑 ``auto_advance``。
    """
    if not isinstance(match, dict):
        raise BPError("match 必须是 dict")
    _ensure_side_lists(match)

    phase = match.get("phase")
    if phase != "side_choice":
        raise BPError(
            "当前不在选边阶段（phase=%s），BP 进行中不能切换阵营" % phase
        )

    choice = match.get("side_choice")
    if not isinstance(choice, dict) or not choice.get("required"):
        reason = (choice or {}).get("reason") if isinstance(choice, dict) else None
        raise BPError("本局无需选边（reason=%s）" % reason)

    side_value = str(side or "").strip()
    if side_value not in SIDES:
        raise BPError("side 只接受 blue / red，收到：%r" % (side,))

    _apply_side_choice(match, side_value, chosen_by="player")
    _enter_bp(match)
    return match
