"""v4 · 朋友 PK 房间引擎（房主端权威状态）。

架构参考 Minecraft 局域网联机：**房主**在自己机器上起一个独立端口（默认 5001），
其他玩家用 ``http://房主IP:端口`` 连进来。房主端是唯一权威：
所有 BP 动作都由房主校验后执行，客户端只读 + 提交操作请求。

* 房间状态全部在内存里（房主关掉进程即结束，和 Minecraft 一致）；
* 任何 IP 都能连（本模块不做「只允许局域网」校验），因此兼容 frp / ngrok 等内网穿透；
* 只用标准库 + 本项目的 game.bp_engine / game.data_loader。

两种模式：
* ``single``：两名玩家打一局完整 BP（BO1），一人蓝一人红。
* ``league``：最多 8 名玩家各带一支战队，同一套循环赛积分榜（BO3），
  两名真人相遇时打 BP，真人对 AI 时由房主端 AI 自动决策，AI 对 AI 直接模拟。
"""

from __future__ import annotations

import random
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from . import ai_coach, bp_engine, data_loader, save_store

try:                                                    # pragma: no cover
    from . import tournament as tourney_engine
    from . import kpl_rules as rules_engine
except Exception as _exc:                                # noqa: BLE001
    tourney_engine = None
    rules_engine = None
    _TOURNEY_IMPORT_ERROR = _exc

MAX_PLAYERS = 8               # v4 简易联赛的人数上限（v6 起按赛事模板的参赛规模放开）
# v40：联机真人数上限跟着赛事模板的参赛队数走（联赛 18 / 挑战者杯 32 / 年度总决赛 12），
# 这里只留一个「所有模板里最大规模」的绝对上限做保险；空缺队伍仍由内置 AI 补齐。
MAX_PLAYERS_HARD = 32
_HB_TIMEOUT = 15.0            # 秒：超过这个时间没心跳算掉线
_LOG_LIMIT = 40


class RoomError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _new_pid() -> str:
    return "p_" + secrets.token_hex(4)


def _new_room_id() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _team_brief(name: str, league_id: str) -> Dict[str, Any]:
    try:
        return bp_engine.team_brief(name, league_id) or {"name": name}
    except Exception:                                       # noqa: BLE001
        return {"name": name}


def _pick_league_for_template(template_key: str, fallback: Optional[str] = None) -> Optional[str]:
    """v7 需求 2：按赛事模板挑选匹配的赛季数据（联赛模板用联赛赛季、杯赛用杯赛赛季）。

    这样房主选「挑战者杯」时，用来补齐空缺队伍的真实战队池是 32 支的杯赛赛季，
    而不是 18 支的联赛赛季，避免「凑不齐人数导致赛程缩水」。
    """
    try:
        payload = data_loader.leagues_payload()
        leagues = payload[0] if isinstance(payload, tuple) else (payload or {}).get("leagues") or []
    except Exception:                                       # noqa: BLE001
        leagues = []
    key = str(template_key or "")

    def _pick(pred) -> Optional[str]:
        for row in leagues or []:
            name = str((row or {}).get("name") or "")
            if pred(name):
                return str((row or {}).get("league_id") or "") or None
        return None

    picked = None
    if key == "challenger":
        picked = _pick(lambda n: "挑战者杯" in n)
    elif key == "annual_finals":
        picked = _pick(lambda n: "年度总决赛" in n)
    elif key in ("annual_package", "spring", "autumn"):
        picked = _pick(lambda n: ("挑战者杯" not in n and "年度总决赛" not in n))
    result = picked or fallback
    print("[room] 模板 %s 匹配赛季数据 -> %s" % (template_key, result), flush=True)
    return result


def _roster(league_id: str) -> List[Dict[str, Any]]:
    """该赛季真实战队列表（按排名）。读失败返回空列表，不抛。"""
    try:
        rows = data_loader.team_objects(league_id) or {}
    except Exception:                                       # noqa: BLE001
        return []
    out = []
    for name, team in rows.items():
        item = dict(team)
        item.setdefault("name", name)
        out.append(item)
    out.sort(key=lambda t: (t.get("rank") is None, t.get("rank") or 999, str(t.get("name"))))
    return out


def _hero_enames(values) -> List[int]:
    """把 ``picks`` / ``bans`` / ``used_global`` 的任意结构规整成 ename 列表。"""
    out: List[int] = []
    for item in values or []:
        value = None
        if isinstance(item, dict):
            value = item.get("ename")
        elif isinstance(item, (int, float)):
            value = int(item)
        elif isinstance(item, str) and item.isdigit():
            value = int(item)
        if value is not None:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                continue
    return out


def _hero_names(league_ctx, enames) -> List[str]:
    out = []
    for ename in enames or []:
        name = None
        if league_ctx is not None:
            try:
                name = league_ctx.cname(ename)
            except Exception:                               # noqa: BLE001
                name = None
        out.append(name or str(ename))
    return out


# --------------------------------------------------------------------------- #
# 房间
# --------------------------------------------------------------------------- #

class Room:
    def __init__(self, *, mode: str, league_id: str, league_name: str,
                 host_name: str, host_team: Optional[str] = None,
                 port: int = 5001):
        self.lock = threading.RLock()
        self.room_id = _new_room_id()
        self.mode = "league" if mode == "league" else "single"
        self.league_id = str(league_id)
        self.league_name = league_name or str(league_id)
        self.port = int(port)
        self.host_token = secrets.token_hex(8)
        self.created_at = time.time()
        self.version = 1
        self.players: List[Dict[str, Any]] = []
        self.log: List[Dict[str, Any]] = []
        self.match: Optional[Dict[str, Any]] = None
        self.league: Optional[Dict[str, Any]] = None
        # v6：模板驱动的联赛（None = 旧版简易 8 人 BO3 循环赛）
        self.template = None
        self.tourney = None
        # v9 需求 2：联机联赛与生涯/AI 联赛共用同一套存档（data/tourney/<save_id>.json）
        self.save_id = None
        self.save_name = None
        # v40：联赛房间在「赛事模板」选好之前先按最大规模（32）放人，
        # 开局时再收紧到该模板的参赛队数（联赛 18 / 挑杯 32 / 年总 12）。
        self.max_players = MAX_PLAYERS_HARD if self.mode == "league" else MAX_PLAYERS
        self.bo = 1                       # v47：单局 PK 的 BO（1/3/5/7/9）
        # v41：昵称 → 战队名（读档续玩 / 掉线重进时按昵称坐回原位）
        self.seat_map: Dict[str, str] = {}
        self.league_ctx = None
        try:
            self.league_ctx = data_loader.league_context(self.league_id)
        except Exception:                                   # noqa: BLE001
            self.league_ctx = None
        self.host_pid = self._add_player(host_name or "房主", host_team, is_host=True)

    # ------------------------------ 基础 ------------------------------ #

    def _touch(self) -> None:
        self.version += 1

    def _append_log(self, kind: str, text: str, **extra) -> None:
        row = {"at": time.time(), "kind": kind, "text": text}
        row.update(extra)
        self.log.append(row)
        if len(self.log) > _LOG_LIMIT:
            del self.log[: len(self.log) - _LOG_LIMIT]

    def _add_player(self, name: str, team: Optional[str], *, is_host: bool = False) -> str:
        pid = _new_pid()
        row = {
            "pid": pid,
            "name": (name or "玩家")[:12],
            "team": team,
            "is_host": is_host,
            "online": True,
            "last_seen": time.time(),
            "joined_at": time.time(),
            "side": None,
        }
        self.players.append(row)
        return pid

    def get_player(self, pid: str) -> Dict[str, Any]:
        for row in self.players:
            if row["pid"] == pid:
                return row
        raise RoomError("玩家不存在（可能已被移出房间）", 404)

    def _refresh_online(self) -> None:
        now = time.time()
        for row in self.players:
            if row["online"] and now - float(row["last_seen"] or 0) > _HB_TIMEOUT:
                row["online"] = False
                self._append_log("leave", "%s 掉线了" % row["name"])

    # ------------------------------ 加入 / 心跳 ------------------------------ #

    def join(self, name: str, team: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            self._refresh_online()
            limit = self.max_players if self.mode == "league" else 2
            if len(self.players) >= limit:
                raise RoomError("房间已满（最多 %d 人）" % limit, 409)
            if self.mode == "single" and len(self.players) >= 2:
                raise RoomError("双人单局房间已经满了（2 人）", 409)
            team = (team or "").strip() or None
            # v41：读档继续时按「昵称」认人 —— 存档里记着每个人上次用哪支队伍
            # （类似 Minecraft 按玩家名保存玩家数据），换了房间/换了 pid 也能坐回原位。
            seat_map = getattr(self, "seat_map", None) or {}
            if seat_map:
                saved_team = seat_map.get(name)
                teams_now = list((self.league or {}).get("teams") or [])
                taken = {row["team"] for row in self.players if row.get("team")}
                if saved_team and saved_team in teams_now and saved_team not in taken:
                    if team and team != saved_team:
                        print("[room] 昵称 %s 上次用「%s」，按存档恢复该队（忽略请求的 %s）"
                              % (name, saved_team, team), flush=True)
                    team = saved_team
                elif not team and teams_now:
                    free = [t for t in teams_now if t not in taken and t not in set(seat_map.values())]
                    if free:
                        team = free[0]
            if team:
                for row in self.players:
                    if row["team"] == team:
                        raise RoomError("战队「%s」已经被 %s 选了" % (team, row["name"]), 409)
            pid = self._add_player(name, team)
            if self.mode == "single":
                self._assign_sides()
            self._touch()
            self._append_log("join", "%s 加入了房间" % name, pid=pid)
            return {"pid": pid, "room_id": self.room_id, "version": self.version}

    def heartbeat(self, pid: str) -> Dict[str, Any]:
        with self.lock:
            row = self.get_player(pid)
            row["last_seen"] = time.time()
            if not row["online"]:
                row["online"] = True
                self._append_log("join", "%s 回来了" % row["name"], pid=pid)
                self._touch()
            return {"version": self.version, "online": True}

    def leave(self, pid: str) -> None:
        with self.lock:
            row = self.get_player(pid)
            row["online"] = False
            row["last_seen"] = 0
            self._touch()
            self._append_log("leave", "%s 离开了房间" % row["name"], pid=pid)

    # ------------------------------ 单局模式 ------------------------------ #

    def _assign_sides(self) -> None:
        """单局模式：房主固定蓝方，后进来的玩家固定红方（BO1，无败方选边）。"""
        for row in self.players:
            row["side"] = "blue" if row["is_host"] else "red"

    def start_single(self, pid: str, bo: int = 1) -> None:
        with self.lock:
            row = self.get_player(pid)
            if not row["is_host"]:
                raise RoomError("只有房主可以开局", 403)
            if len(self.players) < 2:
                raise RoomError("至少需要 2 名玩家（等朋友连进来再开局）", 409)
            if self.match and not self.match.get("series_over"):
                raise RoomError("对局已经在进行中", 409)
            # v47：单局 PK 也支持选 BO（1/3/5/7/9），由房主在创建房间时选
            try:
                bo = int(bo or 1)
            except (TypeError, ValueError):
                bo = 1
            if bo not in (1, 3, 5, 7, 9):
                bo = 1
            self.bo = bo
            self._assign_sides()
            blue = next(p for p in self.players if p["side"] == "blue")
            red = next(p for p in self.players if p["side"] == "red")
            self.match = bp_engine.new_match(
                league_id=self.league_id,
                blue_team=blue.get("team") or self._default_team(0),
                red_team=red.get("team") or self._default_team(1),
                bo=bo, mode="free", side="blue",
            )
            self._touch()
            self._append_log("start", "BO%d 开始：%s（蓝） vs %s（红）"
                             % (bo, blue["name"], red["name"]))

    def _default_team(self, index: int) -> str:
        """没填队名时的中性默认值（好友 PK 不使用 KPL 战队名）。"""
        return "蓝方战队" if index == 0 else "红方战队"

    # ------------------------------ 联赛模式 ------------------------------ #

    def start_league(self, pid: str, *, size: int = 8) -> None:
        with self.lock:
            row = self.get_player(pid)
            if not row["is_host"]:
                raise RoomError("只有房主可以开局", 403)
            # v9：房间可能创建时是单局模式，开局联赛后必须同步 mode，否则归属判定走单局分支
            self.mode = "league"
            roster = _roster(self.league_id)
            if not roster:
                raise RoomError("该赛季没有可用战队数据，无法开局", 503)
            size = max(4, min(8, max(size, len(self.players))))
            # v40：与 start_league_tourney 保持一致 —— 真人队名必须自定义，不能用 KPL 战队名
            roster_names = {str(t.get("name")) for t in roster}
            teams: List[str] = []
            for index, player in enumerate(self.players):
                name = (player.get("team") or "").strip()
                if name and name in roster_names:
                    raise RoomError(
                        "战队名「%s」是 KPL 战队，好友 PK 请换成自己的队名"
                        "（避免 KPL 队伍的胜率影响对战结果）" % name, 400)
                if not name:
                    name = "%s战队" % ((player.get("name") or ("玩家%d" % (index + 1)))[:6])
                base_name = name
                bump = 2
                while name in teams:
                    name = "%s%d" % (base_name[:5], bump)
                    bump += 1
                player["team"] = name
                teams.append(name)
            for item in roster:
                if len(teams) >= size:
                    break
                if item["name"] not in teams:
                    teams.append(item["name"])
            teams = teams[:size]

            schedule = []
            seq = 0
            for i in range(len(teams)):
                for j in range(i + 1, len(teams)):
                    seq += 1
                    schedule.append({
                        "key": "r%02d" % seq,
                        "blue": teams[i],
                        "red": teams[j],
                        "status": "pending",
                        "score": {"blue": 0, "red": 0},
                        "winner": None,
                        "bp_match_id": None,
                        "source": None,
                    })
            owner = {}
            for player in self.players:
                if player.get("team"):
                    owner[player["team"]] = player["pid"]

            self.league = {
                "teams": teams,
                "schedule": schedule,
                "owner": owner,
                "standings": {name: {"team": name, "played": 0, "wins": 0, "losses": 0,
                                     "game_wins": 0, "game_losses": 0} for name in teams},
                "current_key": None,
                "finished": False,
                "size": size,
                "bo": 3,
            }
            self.match = None
            self._touch()
            self._append_log("start", "联赛开始：%d 支战队循环赛（BO3）" % len(teams))

    # -------------------- v6：模板驱动的完整 KPL 联赛 -------------------- #

    def start_league_tourney(self, pid: str, template: str, *,
                             league_id: Optional[str] = None,
                             stations: Optional[List[Dict[str, Any]]] = None,
                             event_name: Optional[str] = None) -> None:
        """按赛事模板开一局完整 KPL 联赛（SAB 三阶段 / 挑杯 / 年总 / 年度赛程包）。

        与生涯模式共用同一个 ``game.tournament`` 引擎，所以赛制、积分、同分判定完全一致。
        参赛人数由模板的 ``team_count`` 决定（例如 2026 春季赛 18 人、挑战者杯 32 人），
        不再限制 8 人。
        """
        with self.lock:
            row = self.get_player(pid)
            if not row["is_host"]:
                raise RoomError("只有房主可以开局", 403)
            if tourney_engine is None:
                raise RoomError("赛制引擎未就绪，无法开启联赛", 503)
            # v9：与 start_league 同理，联赛开局即把房间模式切到 league
            self.mode = "league"
            tpl = None
            try:
                tpl = rules_engine.template(template) if rules_engine else None
            except Exception:                               # noqa: BLE001
                tpl = None
            if not tpl:
                raise RoomError("未知赛事模板：%s" % template, 400)
            team_count = int(tpl.get("team_count") or 18)
            # v21：年度赛程包要跨「联赛段（18 队）+ 挑战者杯段（32 队）」两套规模，
            # 队池按最大的那段准备，联赛段再按 league_size 截断。
            pool_count = team_count
            # v41：年度赛程包的真人上限要按「最小的一段」算 —— 包里挑战者杯段有 32 队，
            # 但联赛段只有 18 队；如果放 32 个真人进来，联赛段根本排不下。
            cap_count = team_count
            if template == "annual_package":
                try:
                    pool_count = int((rules_engine.challenger_cfg() or {}).get("team_count") or 32)
                except Exception:                           # noqa: BLE001
                    pool_count = 32
                pool_count = max(pool_count, team_count)
            # v7 需求 2：按模板挑对应赛季的真实战队池，不足时再从其它赛季补，保证凑满模板人数
            # v32：房主选了「届次」时以届次的赛季为准（和生涯模式一致）
            league_id = league_id or _pick_league_for_template(template, self.league_id) or self.league_id
            self.league_id = league_id
            roster = _roster(league_id)
            if len(roster) < pool_count:
                all_ids = []
                try:
                    payload = data_loader.leagues_payload()
                    rows = payload[0] if isinstance(payload, tuple) else (payload or {}).get("leagues") or []
                    all_ids = [str((r or {}).get("league_id") or "") for r in rows]
                except Exception:                           # noqa: BLE001
                    all_ids = []
                for extra_id in all_ids:
                    if not extra_id or extra_id == league_id:
                        continue
                    known = {row["name"] for row in roster}
                    for row in _roster(extra_id):
                        if row["name"] not in known:
                            roster.append(row)
                            known.add(row["name"])
                    if len(roster) >= pool_count:
                        break
            if not roster:
                raise RoomError("该赛季没有可用战队数据，无法开局", 503)

            # v40：好友 PK 的真人战队必须是玩家自己起的名字（不能用 KPL 战队），
            # 否则不同 KPL 队伍的历史胜率/偏好会左右好友 PK 的结果。
            roster_names = {str(t.get("name")) for t in roster}
            teams: List[str] = []
            for index, player in enumerate(self.players):
                name = (player.get("team") or "").strip()
                if name and name in roster_names:
                    raise RoomError(
                        "战队名「%s」是 KPL 战队，好友 PK 请换成自己的队名"
                        "（避免 KPL 队伍的胜率影响对战结果）" % name, 400)
                if not name:
                    name = "%s战队" % ((player.get("name") or ("玩家%d" % (index + 1)))[:6])
                # 真人之间的队名不能重复
                base_name = name
                bump = 2
                while name in teams:
                    name = "%s%d" % (base_name[:5], bump)
                    bump += 1
                player["team"] = name
                teams.append(name)
            for item in roster:
                if len(teams) >= pool_count:
                    break
                if item["name"] not in teams:
                    teams.append(item["name"])
            teams = teams[:pool_count]
            # v7 需求 2：人数补齐日志（真人 vs 内置 AI 补齐）
            humans = [p for p in self.players if p.get("team") in teams]
            print("[room] 联赛开局 template=%s 参赛 %d 队：真人 %d 队（%s），内置 AI 补齐 %d 队"
                  % (template, len(teams), len(humans),
                     ",".join(str(p.get("team")) for p in humans),
                     max(0, len(teams) - len(humans))),
                  flush=True)

            owner = {}
            for player in self.players:
                if player.get("team") in teams:
                    owner[player["team"]] = player["pid"]

            host_team = teams[0]
            save_name = (self.save_name or "").strip() or ("联机联赛 · %s" % (tpl.get("short") or template))
            # v31：年度赛程包用真实联赛名当站名（好友 PK 以前是中性名）
            # v32：房主选了届次时直接用届次的四站（年份/名称都对得上）
            if template == "annual_package" and not stations:
                try:
                    stations = data_loader.package_stations(league_id) or None
                except Exception:                           # noqa: BLE001
                    stations = None
            save = tourney_engine.new_save(
                template,
                [{"name": name} for name in teams],
                {"name": (self.players[0].get("name") if self.players else "房主"),
                 "team_name": host_team},
                mode="friend",
                save_name=save_name,
                league_id=league_id,
                event_name=event_name or tpl.get("name"),
                stations=stations,
            )
            save["room_id"] = self.room_id
            save["humans"] = list(owner.keys())
            save["room"] = {
                "room_id": self.room_id, "port": self.port, "host": pid,
                "template": template, "league_id": league_id,
                "players": [{"pid": row["pid"], "name": row["name"],
                             "team": row.get("team")} for row in self.players],
            }
            # new_save 已经把第一阶段的赛程建好（抽签 + 分组），
            # 这里只需要推进到「有比赛可打」的状态，不要再调 start_series（会清空分组）。
            tourney_engine.advance(save)
            self.template = template
            self.tourney = save
            # v40：真人上限 = 本模板的参赛队数（联赛 18 / 挑杯 32 / 年总 12），
            # 年度赛程包按最大那一段（32）算；差额全部由内置 AI 执教补齐。
            # v41：年度赛程包按最小一段（联赛 18）限制真人；其它模板就是参赛队数本身。
            if template == "annual_package":
                sizes = []
                for pipe in (tpl.get("pipeline") or []):
                    if pipe == "league_sab":
                        sizes.append(18)
                    elif pipe == "challenger_cup":
                        try:
                            sizes.append(int((rules_engine.challenger_cfg() or {}).get("team_count") or 32))
                        except Exception:                   # noqa: BLE001
                            sizes.append(32)
                    # 年总是「前 12 名才进」的淘汰阶段，不是参赛门槛，不能拿它压真人上限
                    elif pipe == "annual_finals":
                        continue
                if not sizes:
                    sizes = [team_count]
                cap_count = min(sizes)
            self.max_players = max(2, min(MAX_PLAYERS_HARD, cap_count))
            if len(self.players) > self.max_players:
                raise RoomError(
                    "本赛事模板最多 %d 名真人（当前 %d 人），请让多出来的人退出后再开局"
                    % (self.max_players, len(self.players)), 409)
            self.league = {
                "teams": teams, "schedule": [], "owner": owner, "standings": {},
                "current_key": None, "finished": False, "size": team_count,
                "bo": None, "template": template,
            }
            self.seat_map = {str(p.get("name") or ""): str(p.get("team") or "")
                             for p in self.players if p.get("team")}
            self.match = None
            self._sync_from_tourney()
            # v9 需求 2①：开局即落盘独立存档（房主可在 /career 里看到，也能读档续玩）
            self.save_id = save.get("save_id")
            self._persist_save()
            self._touch()
            self._append_log(
                "start", "联赛开始：%s（%d 名真人 · 共 %d 支战队）"
                % (tpl.get("name") or template, len(owner), len(teams)))
            print("[room] 模板联赛开局 template=%s teams=%d humans=%d"
                  % (template, len(teams), len(owner)), flush=True)

    # ---------------------- v9 需求 2：联机联赛存档 ---------------------- #

    def bracket_stage(self, stage=None):
        """v13：联机页对阵图按阶段回看（挑战者杯 32 进 8 图 / 淘汰赛双败图）。"""
        with self.lock:
            if self.tourney is None:
                return {"bracket": None, "available": []}
            view = tourney_engine.bracket_view(self.tourney, stage)
            print("[room] 对阵图请求 save=%s stage=%s" % (self.save_id, stage or "(当前)"))
            return {"bracket": view, "available": (view or {}).get("available") or []}

    def _persist_save(self) -> None:
        """把当前联机联赛写回存档（失败只记日志，绝不让对局卡死）。"""
        if self.tourney is None:
            return
        save = self.tourney
        save["save_id"] = self.save_id or save.get("save_id") or save_store.new_save_id()
        self.save_id = save["save_id"]
        save["room"] = {
            "room_id": self.room_id, "port": self.port,
            "host": next((row["pid"] for row in self.players if row.get("is_host")), None),
            "template": self.template,
            "league_id": save.get("event", {}).get("league_id"),
            "players": [{"pid": row["pid"], "name": row["name"],
                         "team": row.get("team")} for row in self.players],
        }
        save["humans"] = list((self.league or {}).get("owner") or {})
        ok, err = save_store.write(save, quiet=True)
        if not ok:
            print("[room] 联赛存档写入失败 %s：%s" % (save.get("save_id"), err), flush=True)
            self._append_log("warn", "存档写入失败：%s" % err)
            return
        print("[room] 联赛存档已保存 %s（阶段 %s 进度 %s%%）"
              % (save.get("save_id"), save.get("stage"),
                 save_store.want_progress(save)), flush=True)

    def _reset_players_teams(self) -> None:
        for row in self.players:
            row["team"] = row.get("team") or None

    def start_league_tourney_from_save(self, pid: str, save_id: str) -> None:
        """读档继续联机联赛：把存档的赛程/分组/比分/进度恢复进房间。

        房主读档后其它玩家重新加入即可继续打自己的场次；真人上限仍是 8。
        """
        with self.lock:
            row = self.get_player(pid)
            if not row["is_host"]:
                raise RoomError("只有房主可以读档开局", 403)
            if tourney_engine is None:
                raise RoomError("赛制引擎未就绪，无法读档", 503)
            save = save_store.read(save_id)
            if save is None:
                raise RoomError("存档不存在或已损坏：%s" % save_id, 404)
            if save.get("mode") != "friend":
                raise RoomError("这不是好友联机存档（mode=%s）" % save.get("mode"), 400)
            self.mode = "league"
            self.template = save.get("template")
            self.tourney = save
            self.save_id = save.get("save_id")
            self.save_name = save.get("save_name")
            teams = list((save.get("event") or {}).get("teams") or [])
            if not teams:
                teams = [row.get("name") for row in (save.get("participants") or []) if row.get("name")]
            # v41：按昵称记住每个人上次的战队（存档里的 room.players 带 name/team），
            # 这样重新开房读档时，真人只要昵称不变就能坐回自己的队伍。
            saved_players = (save.get("room") or {}).get("players") or []
            self.seat_map = {}
            for item in saved_players:
                who = str(item.get("name") or "").strip()
                tname = str(item.get("team") or "").strip()
                if who and tname in teams:
                    self.seat_map[who] = tname
            owner = {}
            for player in self.players:
                team = (player.get("team") or "").strip()
                # 存档里记过这个昵称 → 强制用它上次的队
                saved_team = self.seat_map.get(str(player.get("name") or "").strip())
                if saved_team and saved_team in teams:
                    if team != saved_team:
                        print("[room] 读档：昵称 %s 恢复为「%s」（请求的是 %s）"
                              % (player.get("name"), saved_team, team or "空"), flush=True)
                    player["team"] = team = saved_team
                if team and team in teams and team not in owner:
                    owner[team] = player["pid"]
            if not owner:
                # 房主没指定战队时，沿用存档里记录的房主战队（先按昵称，再按 pid）
                host_team = None
                host_row = next((p for p in self.players if p["pid"] == pid), None)
                host_name = str((host_row or {}).get("name") or "").strip()
                if host_name and self.seat_map.get(host_name):
                    host_team = self.seat_map[host_name]
                else:
                    for item in saved_players:
                        if item.get("pid") == pid and item.get("team") in teams:
                            host_team = item["team"]
                            break
                if host_team:
                    self.players[0]["team"] = host_team
                    owner[host_team] = pid
                elif teams:
                    self.players[0]["team"] = teams[0]
                    owner[teams[0]] = pid
            self.max_players = max(2, min(MAX_PLAYERS_HARD, len(teams) or 18))
            self.league = {
                "teams": teams, "schedule": [], "owner": owner, "standings": {},
                "current_key": None, "finished": False, "size": len(teams),
                "bo": None, "template": self.template,
            }
            self.match = None
            try:
                tourney_engine.advance(save)
            except Exception as exc:                        # noqa: BLE001
                print("[room] 读档推进失败：%s" % exc, flush=True)
            self._sync_from_tourney()
            self._touch()
            self._append_log("start", "读档继续联赛：%s（%s）"
                             % (save.get("save_name") or save_id, save.get("stage_label") or save.get("stage")))
            print("[room] 读档继续联赛 save_id=%s 参赛 %d 队 真人 %d 队 有 %d 场待打"
                  % (save_id, len(teams), len(owner),
                     len([m for m in self.league["schedule"] if m.get("status") == "pending"])),
                  flush=True)

    def _sync_from_tourney(self) -> None:
        """把 ``tournament`` 存档的状态同步成房间视图所需结构。"""
        save = self.tourney
        if save is None or not self.league:
            return
        league = self.league
        sched = tourney_engine.schedule_view(save) or {}
        rows = []
        for m in sched.get("matches") or []:
            rows.append({
                "key": m.get("match_key"),
                "blue": m.get("blue_team"),
                "red": m.get("red_team"),
                "status": "done" if m.get("status") == "done" else "pending",
                "score": m.get("score") or {"blue": 0, "red": 0},
                "winner": m.get("winner_team"),
                "source": m.get("result_source"),
                "bo": int(m.get("bo") or 5),
                "stage": m.get("stage"),
                "stage_label": m.get("stage_label"),
                "day": m.get("day"),
            })
        league["schedule"] = rows
        league["bo"] = rows[0]["bo"] if rows else None
        league["stage"] = save.get("stage")
        league["stage_label"] = save.get("stage_label")
        league["finished"] = save.get("status") == "done" and not any(
            r["status"] == "pending" for r in rows)
        rows_out: List[Dict[str, Any]] = []
        try:
            view = tourney_engine.standings(save) or {}
            for grp in view.get("groups") or []:
                for row in grp.get("rows") or []:
                    name = row.get("team")
                    if not name:
                        continue
                    game_wins = int(row.get("game_wins") or 0)
                    game_losses = int(row.get("game_losses") or 0)
                    played = int(row.get("played") or 0)
                    wins = int(row.get("wins") or 0)
                    rows_out.append({
                        "team": name,
                        "group": grp.get("name"),
                        "played": played,
                        "wins": wins,
                        "losses": int(row.get("losses") or 0),
                        "game_wins": game_wins,
                        "game_losses": game_losses,
                        "game_diff": game_wins - game_losses,
                        "points": int(row.get("points") or 0),
                        "win_rate": round(wins * 100.0 / played, 1) if played else None,
                        "is_mine": bool(self.league.get("owner", {}).get(name)),
                        "rank": row.get("rank"),
                    })
        except Exception as exc:                            # noqa: BLE001
            print("[room] 读取积分榜失败：%s" % exc, flush=True)
        rows_out.sort(key=lambda r: (-(r["points"] or 0), -(r["game_diff"] or 0), r["team"]))
        for idx, row in enumerate(rows_out):
            row["rank"] = idx + 1
        league["standings"] = rows_out
        league["pending"] = bool(self._tourney_pending())
        league["bo"] = league.get("bo") or 5
        league["annual"] = None
        try:
            league["annual"] = tourney_engine.annual_ranking(save)
        except Exception:                                   # noqa: BLE001
            league["annual"] = None
        # ---- v9 需求 1（补）：联机/大模型联赛也要看到真实赛制结构 ----
        # 分组（第一轮抽签组 / 第二轮起的 S-A-B 组）、阶段计划、阶段规则文案、卡位赛结果
        try:
            view = tourney_engine.standings(save) or {}
            groups = []
            for grp in view.get("groups") or []:
                groups.append({
                    "key": grp.get("key"),
                    "name": grp.get("name"),
                    "teams": [row.get("team") for row in (grp.get("rows") or [])],
                    "rows": [{"team": row.get("team"), "rank": row.get("rank"),
                              "points": row.get("points"), "played": row.get("played"),
                              "wins": row.get("wins"), "losses": row.get("losses"),
                              "game_diff": int(row.get("game_wins") or 0) - int(row.get("game_losses") or 0),
                              "is_mine": bool(self.league.get("owner", {}).get(row.get("team")))}
                             for row in (grp.get("rows") or [])],
                })
            league["groups"] = groups
            league["stage_rules"] = view.get("rules") or ""
            league["cards"] = view.get("cards") or []
        except Exception as exc:                            # noqa: BLE001
            print("[room] 读取分组失败：%s" % exc, flush=True)
            league["groups"] = []
        try:
            plan = tourney_engine.stage_plan(save) or []
            league["plan"] = [{"key": p.get("key"), "label": p.get("label"),
                               "bo": p.get("bo"), "status": p.get("status")}
                              for p in plan]
        except Exception:                                   # noqa: BLE001
            league["plan"] = []
        league["event_name"] = (save.get("event") or {}).get("name")
        league["save_id"] = save.get("save_id")
        league["draw_order"] = list(save.get("draw_order") or [])
        league["champion"] = save.get("champion")
        # v13：淘汰赛对阵流程图数据（联机页实时展示）
        try:
            league["bracket"] = tourney_engine.bracket_view(save)
        except Exception as exc:                              # noqa: BLE001
            print("[room] 对阵图生成失败：%s" % exc)
            league["bracket"] = None

    def _tourney_pending(self) -> Optional[Dict[str, Any]]:
        if not self.league:
            return None
        return next((m for m in self.league["schedule"] if m["status"] == "pending"), None)

    def _tourney_apply(self, row: Dict[str, Any], winner_side: Optional[str],
                       score: Optional[Dict[str, int]], *, source: str,
                       bp_match_id: Optional[str] = None,
                       bp_record: Optional[Dict[str, Any]] = None,
                       winner_team: Optional[str] = None) -> None:
        save = self.tourney
        if save is None:
            return
        try:
            tourney_engine.apply_result(save, row["key"], winner_side, score,
                                        source=source, bp_match_id=bp_match_id,
                                        bp_record=bp_record, winner_team=winner_team)
            tourney_engine.advance(save)
        except Exception as exc:                            # noqa: BLE001
            print("[room] 写入联赛结果失败：%s" % exc, flush=True)
            self._append_log("error", "写入联赛结果失败：%s" % exc)
        self._sync_from_tourney()
        self.league["current_key"] = None
        # v9 需求 2②：每场比分/进度变化后立即落盘，断电/断线也能读档继续
        self._persist_save()

    def _human_owner(self, team: str) -> Optional[str]:
        if not self.league:
            return None
        pid = (self.league.get("owner") or {}).get(team)
        if not pid:
            return None
        for row in self.players:
            if row["pid"] == pid and row["online"]:
                return pid
        return None

    def league_next(self, pid: str) -> Dict[str, Any]:
        """进入下一场。真人对真人 / 真人对 AI → 打 BP；AI 对 AI → 直接模拟。"""
        with self.lock:
            if not self.league:
                raise RoomError("联赛还没开始", 409)
            if self.match and not self.match.get("series_over"):
                raise RoomError("当前对局还没打完", 409)
            league = self.league
            pending = [m for m in league["schedule"] if m["status"] == "pending"]
            if not pending:
                league["finished"] = True
                league["current_key"] = None
                self.match = None
                self._touch()
                self._append_log("finish", "联赛全部结束")
                return {"finished": True}
            nxt = pending[0]

            if self.tourney is not None:
                # v6：模板联赛 —— 真人参与就打 BP（BO 由该场比赛决定），否则交给引擎模拟
                blue_human = self._human_owner(nxt["blue"])
                red_human = self._human_owner(nxt["red"])
                if blue_human is None and red_human is None:
                    winner_side = "blue" if self._weighted_winner(nxt["blue"], nxt["red"]) == nxt["blue"] else "red"
                    self._tourney_apply(nxt, winner_side, None, source="sim")
                    self.match = None
                    self._touch()
                    self._append_log("sim", "%s vs %s 已模拟" % (nxt["blue"], nxt["red"]))
                    return {"simulated": True, "key": nxt["key"]}
                mode = "ai" if (blue_human is None or red_human is None) else "free"
                # v6 修复：player_side 必须对齐真人阵营，否则 AI 被判定“轮到玩家”不落子（联赛 BP 卡死）
                human_side = "blue" if blue_human is not None else "red"
                bo = int(nxt.get("bo") or 5)
                if bo not in bp_engine.ALLOWED_BO:
                    bo = 5
                match = bp_engine.new_match(
                    league_id=self.league_id,
                    blue_team=nxt["blue"], red_team=nxt["red"],
                    bo=bo, mode=mode, side=human_side,
                )
                match["room_match_key"] = nxt["key"]
                self.match = match
                league["current_key"] = nxt["key"]
                self._touch()
                self._append_log("start", "%s vs %s 开始（BO%d · %s）"
                                 % (nxt["blue"], nxt["red"], bo,
                                    "BP" if mode == "free" else "你对 AI"))
                return {"key": nxt["key"], "human_blue": bool(blue_human),
                        "human_red": bool(red_human), "bo": bo}

            blue_human = self._human_owner(nxt["blue"])
            red_human = self._human_owner(nxt["red"])
            if blue_human is None and red_human is None:
                self._simulate(nxt)
                league["current_key"] = None
                self.match = None
                self._touch()
                self._append_log("sim", "%s %d:%d %s"

                                 % (nxt["blue"], nxt["score"]["blue"],
                                    nxt["score"]["red"], nxt["red"]))
                return {"simulated": True, "key": nxt["key"]}
            mode = "ai" if (blue_human is None or red_human is None) else "free"
            # v6 修复：player_side 必须对齐真人阵营，否则 AI 被判定“轮到玩家”不落子（联赛 BP 卡死）
            human_side = "blue" if blue_human is not None else "red"
            match = bp_engine.new_match(
                league_id=self.league_id,
                blue_team=nxt["blue"], red_team=nxt["red"],
                bo=3, mode=mode, side=human_side,
            )
            match["room_match_key"] = nxt["key"]
            self.match = match
            league["current_key"] = nxt["key"]
            self._touch()
            self._append_log("start", "%s vs %s 开始（%s）"
                             % (nxt["blue"], nxt["red"], "BP" if mode == "free" else "你对 AI"))
            return {"key": nxt["key"], "human_blue": bool(blue_human),
                    "human_red": bool(red_human)}

    def _simulate(self, row: Dict[str, Any]) -> None:
        """AI 对 AI：按该赛季真实胜率加权模拟 BO3。"""
        winner = self._weighted_winner(row["blue"], row["red"])
        score = {"blue": 0, "red": 0}
        for _ in range(3):
            if score["blue"] == 2 or score["red"] == 2:
                break
            side = winner if random.random() < 0.65 else (
                row["red"] if winner == row["blue"] else row["blue"])
            score["blue" if side == row["blue"] else "red"] += 1
        if score["blue"] == score["red"]:
            score["blue" if winner == row["blue"] else "red"] += 1
        row["score"] = score
        row["winner"] = row["blue"] if score["blue"] > score["red"] else row["red"]
        row["status"] = "done"
        row["source"] = "sim"
        self._record(row)

    def _weighted_winner(self, blue: str, red: str) -> str:
        def rate(team: str) -> float:
            try:
                stats = data_loader.load_team_stats(self.league_id) or {}
            except Exception:                               # noqa: BLE001
                return 50.0
            for row in stats.get("teams") or []:
                if row.get("name") == team:
                    value = row.get("win_rate")
                    if isinstance(value, (int, float)):
                        return float(value)
            return 50.0
        rb, rr = rate(blue), rate(red)
        total = rb + rr
        if total <= 0:
            return blue if random.random() < 0.5 else red
        return blue if random.random() < rb / total else red

    def _record(self, row: Dict[str, Any]) -> None:
        league = self.league or {}
        table = league.get("standings") or {}
        winner = row["winner"]
        loser = row["red"] if winner == row["blue"] else row["blue"]
        for name, is_win in ((winner, True), (loser, False)):
            entry = table.setdefault(name, {"team": name, "played": 0, "wins": 0,
                                            "losses": 0, "game_wins": 0, "game_losses": 0})
            entry["played"] += 1
            entry["wins" if is_win else "losses"] += 1
        table[winner]["game_wins"] += row["score"]["blue"] if winner == row["blue"] else row["score"]["red"]
        table[loser]["game_losses"] += row["score"]["blue"] if winner == row["blue"] else row["score"]["red"]

    # ------------------------------ BP 操作 ------------------------------ #

    def _player_camp(self, pid: str) -> Optional[str]:
        """某个真人在本局属于哪一方（巅峰对决盲选要用：双方同时都能落子）。

        联赛模式按「我队的队名 == 本场 blue/red」判断；单局模式直接用 ``side``。
        """
        row = next((p for p in self.players if p["pid"] == pid), None)
        if row is None:
            return None
        if self.league and (self.league.get("schedule") or []):
            key = (self.league or {}).get("current_key")
            cur = next((m for m in self.league["schedule"] if m["key"] == key), None)
            if cur is not None:
                team = row.get("team")
                if team and team == cur.get("blue"):
                    return "blue"
                if team and team == cur.get("red"):
                    return "red"
        side = row.get("side")
        return side if side in ("blue", "red") else None

    def _in_tiebreak_pick(self) -> bool:
        match = self.match
        if not match or match.get("finished"):
            return False
        if not match.get("is_tiebreak"):
            return False
        return str(match.get("phase") or "") in ("bp", "tiebreak_pick")

    def _side_owner_pid(self, side: str) -> Optional[str]:
        if self.match is None:
            return None
        # v9 修复：房间可能是 single 模式创建后再切联赛（mode 未同步），
        # 只要已经开局联赛，就必须按联赛对阵判定归属，否则真人被判成 AI → BP 被自动走完
        if self.league and (self.league.get("schedule") or []):
            key = (self.league or {}).get("current_key")
            row = next((m for m in self.league["schedule"] if m["key"] == key), None)
            if row is not None:
                team = row["blue"] if side == "blue" else row["red"]
                return self._human_owner(team)
        if self.mode == "single":
            for row in self.players:
                if row["side"] == side:
                    return row["pid"]
            return None
        if self.league:
            key = (self.league or {}).get("current_key")
            row = next((m for m in self.league["schedule"] if m["key"] == key), None)
            if row is None:
                return None
            team = row["blue"] if side == "blue" else row["red"]
            return self._human_owner(team)
        return None

    def act(self, pid: str, ename: int, action: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            row = self.get_player(pid)
            row["last_seen"] = time.time()
            match = self.match
            if match is None:
                raise RoomError("当前没有进行中的对局", 409)
            if match.get("finished"):
                raise RoomError("本小局已结束", 409)
            if match.get("phase") in ("result", "done", "side_choice"):
                raise RoomError("当前阶段不能选英雄（%s）" % match.get("phase"), 409)
            # v20：巅峰对决是盲选 —— 双方同时都能落子，只校验「你是本方的人」
            if self._in_tiebreak_pick():
                camp = self._player_camp(pid)
                if camp is None:
                    raise RoomError("你不在本场对阵中，无法参与巅峰对决盲选", 409)
                if len(bp_engine.tiebreak_picks(match, camp)) >= 5:
                    raise RoomError("你方巅峰对决盲选已满 5 人", 409)
                try:
                    result = bp_engine.apply_action(match, int(ename), "pick", side=camp)
                except Exception as exc:                        # noqa: BLE001
                    raise RoomError("操作被引擎拒绝：%s" % exc, 409) from exc
                if isinstance(result, dict):
                    self.match = result
                self._touch()
                self._append_log("bp", "%s 完成一手盲选" % row["name"], pid=pid)
                self._maybe_finish_series()
                return {"version": self.version}
            side = match.get("side")
            owner = self._side_owner_pid(side)
            if owner is None:
                raise RoomError("当前轮次没有真人操作方", 409)
            if owner != pid:
                raise RoomError("还没轮到你操作（当前是%s方）" % ("蓝" if side == "blue" else "红"), 409)
            try:
                result = bp_engine.apply_action(match, int(ename), action)
            except Exception as exc:                        # noqa: BLE001
                raise RoomError("操作被引擎拒绝：%s" % exc, 409) from exc
            if isinstance(result, dict):
                self.match = result
            self._touch()
            self._append_log("bp", "%s 完成一手" % row["name"], pid=pid)
            self._maybe_finish_series()
            return {"version": self.version}

    def ai_step(self, pid: str) -> Dict[str, Any]:
        """由房主端推进 AI 的所有手（真人对 AI 的联赛场次）。

        v6：加守卫 —— 手数没有推进时必须给出兜底与日志，否则前端会无限空转。
        """
        with self.lock:
            self.get_player(pid)
            match = self.match
            if match is None:
                raise RoomError("当前没有进行中的对局", 409)
            before = (int(match.get("step") or 0), str(match.get("phase")), bool(match.get("finished")))
            if str(match.get("phase") or "") == "side_choice":
                # AI（或无人在线的一方）不需要真人选边：房主端直接代选，避免前端空转
                choice = match.get("side_choice") or {}
                chooser_owner = (self._human_owner(choice.get("chooser_team"))
                                 if str(choice.get("chooser_role") or "") == "player" else None)
                if chooser_owner is None:
                    try:
                        bp_engine.choose_side(self.match, "blue")
                        self._touch()
                        self._append_log("side", "AI 已代选阵营（%s）" % choice.get("chooser_team", "?"), pid=pid)
                    except Exception as exc:                # noqa: BLE001
                        self._append_log("warn", "AI 选边失败：%s" % exc, pid=pid)
                    return {"version": self.version, "step": self.match.get("step") if self.match else None}
                return {"version": self.version, "step": match.get("step"), "wait_for": chooser_owner}
            try:
                result = bp_engine.auto_advance(match)
            except Exception as exc:                        # noqa: BLE001
                self._append_log("error", "AI 代走异常：%s" % exc, pid=pid)
                raise RoomError("AI 代走失败：%s" % exc, 409) from exc
            if isinstance(result, dict):
                self.match = result
            after = (int(self.match.get("step") or 0), str(self.match.get("phase")),
                     bool(self.match.get("finished")))
            if after == before and not self.match.get("finished"):
                side = str(self.match.get("side") or "")
                if self._force_ai_move():
                    self._append_log(
                        "ai", "AI 兜底落子（引擎未推进 · %s方 · 第 %s 步）"
                        % ("蓝" if side == "blue" else "红", self.match.get("step")), pid=pid)
                else:
                    self._append_log(
                        "warn", "AI 无法落子（phase=%s side=%s step=%s）"
                        % (self.match.get("phase"), side, self.match.get("step")), pid=pid)
            self._touch()
            self._maybe_finish_series()
            return {"version": self.version, "step": self.match.get("step") if self.match else None}

    def auto_until_human(self, pid: str, max_steps: int = 800) -> Dict[str, Any]:
        """服务端一键推进：替 AI 走完所有手，直到轮到真人（或联赛/系列赛结束）。

        v6：把原本「前端每次只推一手、靠轮询」的写法收进服务端，
        避免前端空转成百上千次请求，也避免前端与后端判定不一致导致的卡死。
        """
        with self.lock:
            self.get_player(pid)
            steps = 0
            stall = 0
            while steps < max_steps:
                steps += 1
                match = self.match
                if match is None:
                    if not self.league:
                        return {"version": self.version, "steps": steps, "idle": True}
                    try:
                        nxt = self.league_next(pid)
                    except RoomError as exc:
                        self._append_log("warn", "取下一场失败：%s" % exc, pid=pid)
                        return {"version": self.version, "steps": steps, "error": str(exc)}
                    if nxt.get("finished") or self.match is None:
                        return {"version": self.version, "steps": steps,
                                "finished": bool(self.league.get("finished"))}
                    continue
                if match.get("series_over"):
                    self._maybe_finish_series()
                    continue
                phase = str(match.get("phase") or "")
                if phase == "side_choice":
                    choice = match.get("side_choice") or {}
                    owner = (self._human_owner(choice.get("chooser_team"))
                             if str(choice.get("chooser_role") or "") == "player" else None)
                    if owner is not None:
                        break
                    try:
                        bp_engine.choose_side(match, "blue")
                        self._touch()
                        self._append_log("side", "AI 已代选阵营（%s）"
                                         % choice.get("chooser_team", "?"), pid=pid)
                    except Exception as exc:                # noqa: BLE001
                        self._append_log("warn", "AI 选边失败：%s" % exc, pid=pid)
                        break
                    continue
                if phase in ("result", "done"):
                    break
                side = str(match.get("side") or "")
                if self._side_owner_pid(side) is not None:
                    break                                   # 轮到真人
                before = (int(match.get("step") or 0), str(match.get("phase")))
                self._force_ai_move()
                self._touch()
                self._maybe_finish_series()
                if self.match is None:
                    stall = 0
                    continue
                after = (int(self.match.get("step") or 0), str(self.match.get("phase")))
                if after == before:
                    stall += 1
                    if stall >= 3:
                        self._append_log("warn", "服务端推进停滞（step=%s phase=%s）"
                                         % before, pid=pid)
                        break
                else:
                    stall = 0
            return {"version": self.version, "steps": steps,
                    "stalled": stall >= 3,
                    "finished": bool(self.league and self.league.get("finished"))}

    def _force_ai_move(self) -> bool:
        """兜底：不经过 auto_advance 的玩家侧判定，直接以 AI 身份落一手。"""
        match = self.match
        if not isinstance(match, dict):
            return False
        if match.get("finished"):
            return False
        phase = str(match.get("phase") or "")
        # v20：巅峰对决盲选阶段同样需要兜底（AI 补位方一次补一手）
        tie_pick = bool(match.get("is_tiebreak")) and phase in (
            "", "bp", bp_engine.PHASE_TIEBREAK_PICK)
        if not tie_pick and phase not in (None, "bp", ""):
            return False
        side = str(match.get("side") or "")
        action = str(match.get("action") or "pick")
        if tie_pick:
            # v20：盲选阶段 match["side"] 只是「焦点方」，不代表 AI 阵营。
            # AI 补位的一侧 = 玩家阵营的对面；双真人局（mode=free）不该由服务端代走。
            mode = str(match.get("mode") or "").strip().lower()
            if mode != "ai":
                return False
            player_side = str(match.get("player_side") or "")
            if player_side not in ("blue", "red"):
                return False
            side = "red" if player_side == "blue" else "blue"
        if side not in ("blue", "red"):
            return False
        try:
            ctx = bp_engine._ctx(match)                     # noqa: SLF001 - 与引擎内部保持一致
            ename = ai_coach.decide(match, side, ctx, action=("pick" if tie_pick else action))
            if tie_pick:
                bp_engine.apply_action(match, int(ename), "pick", side=side)
            else:
                bp_engine.apply_action(match, int(ename), action)
        except Exception as exc:                            # noqa: BLE001
            print("[room] force ai move failed:", exc)
            return False
        return True

    def autocomplete_bp(self, pid: str) -> Dict[str, Any]:
        """v17：一键 BP —— 把本小局剩余的 ban/pick 全部交给内置 AI 走完。"""
        with self.lock:
            row = self.get_player(pid)
            row["last_seen"] = time.time()
            match = self.match
            if match is None:
                raise RoomError("当前没有进行中的对局", 409)
            if match.get("finished"):
                raise RoomError("本小局已结束", 409)
            phase_now = str(match.get("phase") or "")
            # v20：巅峰对决盲选阶段同样可以一键 BP（AI 帮双方各补满 5 人）
            if phase_now != "bp" and phase_now != bp_engine.PHASE_TIEBREAK_PICK:
                raise RoomError("当前不是 BP 阶段（%s）" % match.get("phase"), 409)
            # v19：真人对真人时不允许一键 BP（会替对手也走完，破坏对局公平）。
            # 前端已按 opponent_is_ai 隐藏按钮，这里再兜一层。
            if not self._opponent_is_ai(pid):
                raise RoomError("真人对战不能一键 BP，请自己完成 BP", 409)
            before = match.get("step")
            try:
                result = bp_engine.auto_complete(match)
            except Exception as exc:                        # noqa: BLE001
                raise RoomError("一键 BP 失败：%s" % exc, 409) from exc
            if isinstance(result, dict):
                self.match = result
            self._touch()
            self._append_log("bp", "%s 触发一键 BP（步数 %s → %s）"
                             % (row["name"], before, self.match.get("step")), pid=pid)
            print("[room] 一键 BP match=%s 步数 %s → %s phase=%s"
                  % (self.match.get("match_id"), before, self.match.get("step"),
                     self.match.get("phase")))
            self._maybe_finish_series()
            return {"version": self.version, "match": self._match_view(pid)}

    def choose_side(self, pid: str, side: str) -> Dict[str, Any]:
        with self.lock:
            row = self.get_player(pid)
            row["last_seen"] = time.time()
            match = self.match
            if match is None:
                raise RoomError("当前没有进行中的对局", 409)
            if match.get("phase") != "side_choice":
                raise RoomError("当前不在选边阶段", 409)
            choice = (match.get("side_choice") or {})
            chooser = choice.get("chooser_role")
            # 选边权属于“上局败方”，操作方是该队伍的真人（v6 修正：不能用 player_side 推断）
            owner = (self._human_owner(choice.get("chooser_team"))
                     if str(chooser or "") == "player" else None)
            if chooser == "ai":
                try:
                    bp_engine.choose_side(match, "blue" if side not in ("blue", "red") else side)
                except Exception as exc:                    # noqa: BLE001
                    raise RoomError("AI 选边失败：%s" % exc, 409) from exc
            else:
                if owner is not None and owner != pid:
                    raise RoomError("本局败方不是你，不能选边", 403)
                try:
                    bp_engine.choose_side(match, side if side in ("blue", "red") else "blue")
                except Exception as exc:                    # noqa: BLE001
                    raise RoomError("选边失败：%s" % exc, 409) from exc
            if isinstance(self.match, dict):
                pass
            self._touch()
            return {"version": self.version}

    def next_game(self, pid: str) -> Dict[str, Any]:
        with self.lock:
            self.get_player(pid)
            match = self.match
            if match is None:
                raise RoomError("当前没有进行中的对局", 409)
            try:
                result = bp_engine.next_game(match)
            except Exception as exc:                        # noqa: BLE001
                raise RoomError("无法进入下一局：%s" % exc, 409) from exc
            if isinstance(result, dict):
                self.match = result
            self._touch()
            return {"version": self.version}

    def _bp_record(self, match: Dict[str, Any]) -> Dict[str, Any]:
        """v9 需求 2②：把一整个 BO 的 BP 记录整理成可序列化结构写进存档。"""
        ctx = data_loader.league_context(match.get("league_id"))
        games = []
        for item in (match.get("game_log") or []):
            games.append(item)
        history = [dict(h) for h in (match.get("history") or [])]
        picks = match.get("picks") or {}
        bans = match.get("bans") or {}
        record = {
            "match_id": match.get("match_id"),
            "bo": match.get("bo"),
            "game_index": match.get("game_index"),
            "is_tiebreak": bool(match.get("is_tiebreak")),
            "blue_team": match.get("blue_team"),
            "red_team": match.get("red_team"),
            "score": match.get("score"),
            "final": match.get("final"),
            "history": history,
            "bans": {"blue": _hero_names(ctx, bans.get("blue")),
                     "red": _hero_names(ctx, bans.get("red"))},
            "picks": {"blue": _hero_names(ctx, picks.get("blue")),
                      "red": _hero_names(ctx, picks.get("red"))},
            "games": games,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        print("[room] 生成 BP 记录 match=%s 手数=%d 蓝 %s vs 红 %s"
              % (record.get("match_id"), len(history),
                 record.get("blue_team"), record.get("red_team")), flush=True)
        return record

    def _maybe_finish_series(self) -> None:
        match = self.match
        if not match or not match.get("series_over"):
            return
        if self.mode != "league" or not self.league:
            self._append_log("finish", "BO1 结束")
            return
        key = match.get("room_match_key")
        row = next((m for m in self.league["schedule"] if m["key"] == key), None)
        if row is None or row["status"] == "done":
            return
        final = match.get("final") or {}
        winner_side = None if final.get("winner") == "draw" else (final.get("winner") or match.get("result"))
        score = match.get("score") or {"blue": 0, "red": 0}

        if self.tourney is not None:
            side = "blue" if str(winner_side).startswith("blue") else "red"
            # v18：胜负以战队名为准 —— 换边会改阵营，队名不会
            win_team = (match.get("blue_team") if side == "blue" else match.get("red_team")) or {}
            win_team_name = win_team.get("name") if isinstance(win_team, dict) else None
            self._append_log("finish", "%s %d:%d %s"
                             % (row["blue"], int(score.get("blue") or 0),
                                int(score.get("red") or 0), row["red"]))
            record = self._bp_record(match)
            self.match = None
            self._tourney_apply(row, side,
                                {"blue": int(score.get("blue") or 0),
                                 "red": int(score.get("red") or 0)},
                                source="bp", bp_match_id=match.get("match_id"),
                                bp_record=record, winner_team=win_team_name)
            return

        if winner_side is None:
            row["winner"] = row["blue"]
        else:
            row["winner"] = row["blue"] if str(winner_side).startswith("blue") else row["red"]
        row["score"] = {"blue": int(score.get("blue") or 0), "red": int(score.get("red") or 0)}
        row["status"] = "done"
        row["source"] = "bp"
        row["bp_match_id"] = match.get("match_id")
        self._record(row)
        self._append_log("finish", "%s %d:%d %s" % (row["blue"], row["score"]["blue"],
                                                   row["score"]["red"], row["red"]))
        self.match = None
        self.league["current_key"] = None

    # ------------------------------ 视图 ------------------------------ #

    def _opponent_is_ai(self, pid: str) -> bool:
        """当前对局的对手是不是内置 AI（v19）。

        判定规则：按联赛/单局对阵找到 pid 自己所在的一侧，另一侧若是「没有真人房主」
        就视为内置 AI。联赛里 AI 补位的队伍没有 owner，单局模式里对家没上线也按 AI 处理。

        用途：好友 PK 的「⚡一键 BP」只允许在打 AI 时出现，真人对真人时隐藏
        （真人局让别人替你 BP 会破坏对局公平）。
        """
        match = self.match
        if not match:
            return False
        try:
            owners = {"blue": self._side_owner_pid("blue"),
                      "red": self._side_owner_pid("red")}
        except Exception:                                   # noqa: BLE001
            return False
        my_side = None
        for side in ("blue", "red"):
            if owners.get(side) and owners.get(side) == pid:
                my_side = side
                break
        if my_side is None:
            # 我不是本小局的参战方（观战/房主旁观）：任一侧无真人即认为场上有 AI
            return any(owners.get(s) is None for s in ("blue", "red"))
        other = "red" if my_side == "blue" else "blue"
        return owners.get(other) is None

    def _match_view(self, pid: str) -> Optional[Dict[str, Any]]:
        match = self.match
        if not match:
            return None
        ctx = self.league_ctx
        picks = match.get("picks") or {}
        bans = match.get("bans") or {}
        side = match.get("side")
        if str(match.get("phase") or "") == "side_choice":
            # 选边阶段：轮到“上局败方”选边，操作方是该队伍的真人（若为 AI 则由房主端代选）
            choice = match.get("side_choice") or {}
            owner = (self._human_owner(choice.get("chooser_team"))
                     if str(choice.get("chooser_role") or "") == "player" else None)
        elif self._in_tiebreak_pick():
            # v20：巅峰对决盲选，双方同时可操作 —— 只要本方没选满就算「轮到你」
            camp = self._player_camp(pid)
            owner = pid if (camp and len(bp_engine.tiebreak_picks(match, camp)) < 5) else None
        else:
            owner = self._side_owner_pid(side)
        available = []
        for hero in (match.get("available") or []):
            available.append({
                "ename": hero.get("ename"),
                "cname": hero.get("cname"),
                "lane_list": hero.get("lane_list") or [],
                "avatar_path": hero.get("avatar_path"),
                "win_rate": hero.get("win_rate"),
                "ban_rate": hero.get("ban_rate"),
                "has_stats": hero.get("has_stats"),
            })
        # v5：全量英雄看板（含已 ban / 已选而置灰的），客户端渲染用
        hero_board = []
        for hero in (match.get("hero_board") or []):
            hero_board.append({
                "ename": hero.get("ename"),
                "cname": hero.get("cname"),
                "avatar_path": hero.get("avatar_path"),
                "lane_main": hero.get("lane_main"),
                "lane_main_official": hero.get("lane_main_official"),
                "lane_list": hero.get("lane_list") or [],
                "win_rate": hero.get("win_rate"),
                "ban_rate": hero.get("ban_rate"),
                "has_stats": hero.get("has_stats"),
                "disabled": hero.get("disabled"),
                "reason": hero.get("reason"),
            })
        return {
            "match_id": match.get("match_id"),
            "league_id": match.get("league_id"),
            "league_name": match.get("league_name"),
            "bo": match.get("bo"),
            "game_index": match.get("game_index"),
            "total_steps": match.get("total_steps"),
            "step": match.get("step"),
            "phase": match.get("phase"),
            "finished": match.get("finished"),
            "series_over": match.get("series_over"),
            "is_tiebreak": match.get("is_tiebreak"),
            "side": side,
            "action": match.get("action"),
            "turn_label": match.get("turn_label"),
            "side_choice": match.get("side_choice"),
            "score": match.get("score"),
            "result": match.get("result"),
            "final": match.get("final"),
            "blue_team": match.get("blue_team"),
            "red_team": match.get("red_team"),
            "blue_picks": _hero_names(ctx, picks.get("blue")),
            "red_picks": _hero_names(ctx, picks.get("red")),
            "blue_bans": _hero_names(ctx, bans.get("blue")),
            "red_bans": _hero_names(ctx, bans.get("red")),
            # v9 需求 1②：BP 面板展示全局 BP 状态，需要 ename 级数据
            "picks_enames": {"blue": _hero_enames(picks.get("blue")),
                             "red": _hero_enames(picks.get("red"))},
            "bans_enames": {"blue": _hero_enames(bans.get("blue")),
                            "red": _hero_enames(bans.get("red"))},
            "used_global_enames": {"blue": _hero_enames((match.get("used_global") or {}).get("blue")),
                                   "red": _hero_enames((match.get("used_global") or {}).get("red"))},
            "available": available,
            "hero_board": hero_board,
            "my_turn": bool(owner and owner == pid),
            "turn_owner": owner,
            # v20：巅峰对决盲选 —— 双方各 5 人、选完按分路公布
            "tiebreak_ready": bp_engine.tiebreak_ready(match),
            "tiebreak_reveal": match.get("tiebreak_reveal") or [],
            "tiebreak_counts": {
                "blue": len(bp_engine.tiebreak_picks(match, "blue")),
                "red": len(bp_engine.tiebreak_picks(match, "red")),
            },
            "my_camp": self._player_camp(pid),
            # v19：一键 BP 只在「对手是内置 AI」时可用（真人局隐藏按钮）
            "opponent_is_ai": self._opponent_is_ai(pid),
        }

    def state(self, pid: str) -> Dict[str, Any]:
        with self.lock:
            self._refresh_online()
            me = self.get_player(pid)
            players = [{
                "pid": row["pid"],
                "name": row["name"],
                "team": row["team"],
                "side": row["side"],
                "is_host": row["is_host"],
                "online": row["online"],
                "is_me": row["pid"] == pid,
            } for row in self.players]

            league_view = None
            if self.league:
                standings = self.league.get("standings") or []
                table = []
                if isinstance(standings, dict):                 # v4 旧结构（dict）
                    for name, entry in standings.items():
                        played = entry.get("played") or 0
                        table.append({
                            "team": name,
                            "played": played,
                            "wins": entry.get("wins") or 0,
                            "losses": entry.get("losses") or 0,
                            "game_wins": entry.get("game_wins") or 0,
                            "game_losses": entry.get("game_losses") or 0,
                            "game_diff": (entry.get("game_wins") or 0) - (entry.get("game_losses") or 0),
                            "win_rate": round((entry.get("wins") or 0) * 100.0 / played, 1) if played else None,
                            "is_mine": (self.league.get("owner") or {}).get(name) == pid,
                        })
                else:                                           # v6 模板联赛（list，已排序）
                    for entry in standings:
                        row = dict(entry)
                        row["is_mine"] = (self.league.get("owner") or {}).get(row.get("team")) == pid
                        table.append(row)
                table.sort(key=lambda r: (-(r.get("points") or 0), -(r.get("game_diff") or 0),
                                          -(r.get("wins") or 0), str(r.get("team"))))
                for index, row in enumerate(table, 1):
                    row["rank"] = index
                league_view = {
                    "teams": self.league["teams"],
                    "size": self.league["size"],
                    "bo": self.league["bo"],
                    "stage": self.league.get("stage"),
                    "stage_label": self.league.get("stage_label"),
                    "template": self.league.get("template"),
                    "annual": self.league.get("annual"),
                    "standings": table,
                    "current_key": self.league.get("current_key"),
                    "finished": bool(self.league.get("finished")),
                    "schedule": [dict(row) for row in self.league["schedule"] if row["status"] == "done"][-12:],
                    "pending": sum(1 for row in self.league["schedule"] if row["status"] == "pending"),
                    # v9 补：真实赛制结构（分组 / 阶段计划 / 阶段规则 / 卡位赛结果）
                    "groups": self.league.get("groups") or [],
                    "plan": self.league.get("plan") or [],
                    "stage_rules": self.league.get("stage_rules") or "",
                    "cards": self.league.get("cards") or [],
                    "event_name": self.league.get("event_name"),
                    "save_id": self.league.get("save_id"),
                    "draw_order": self.league.get("draw_order") or [],
                    "champion": self.league.get("champion"),
                    # v13：淘汰赛对阵流程图（联机页实时展示）
                    "bracket": self.league.get("bracket"),
                }

            return {
                "ok": True,
                "version": self.version,
                "room": {
                    "room_id": self.room_id,
                    "mode": self.mode,
                    "league_id": self.league_id,
                    "league_name": self.league_name,
                    "port": self.port,
                    "players": players,
                    # v8 需求1：房间可能是 single 模式创建的、随后切到联赛 PK 开局，
                    # 因此只要有 self.league 就按联赛口径返回（真人上限 8），
                    # 并额外给出「参赛队数」（= 模板规模，含内置 AI 补齐的队伍）。
                    "max_players": (self.max_players if (self.mode == "league" or self.league) else 2),
                    "hard_max_players": (self.max_players
                                         if (self.mode == "league" or self.league)
                                         else MAX_PLAYERS_HARD),
                    "league_size": (len(self.league["teams"]) if self.league else 0),
                    "humans": (len(players) if self.league else len(players)),
                    # v9 需求 2：联机联赛存档标识（前端显示 + 读档恢复用）
                    "save_id": self.save_id,
                    "save_name": self.save_name,
                    "host": next((p["name"] for p in players if p["is_host"]), ""),
                    "me": {"pid": me["pid"], "name": me["name"], "team": me["team"],
                           "is_host": me["is_host"], "side": me["side"]},
                },
                "match": self._match_view(pid),
                "league": league_view,
                "log": self.log[-12:],
                "server_time": time.time(),
            }


# --------------------------------------------------------------------------- #
# 房间注册表（每个进程一份；房主进程里就是一个房间）
# --------------------------------------------------------------------------- #

_ROOMS: Dict[str, Room] = {}
_REGISTRY_LOCK = threading.RLock()


def register(room: Room) -> Room:
    with _REGISTRY_LOCK:
        _ROOMS[room.room_id] = room
    return room


def get_room(room_id: str) -> Optional[Room]:
    with _REGISTRY_LOCK:
        return _ROOMS.get(str(room_id or ""))


def drop_room(room_id: str) -> None:
    with _REGISTRY_LOCK:
        _ROOMS.pop(str(room_id or ""), None)


def clear_rooms() -> None:
    with _REGISTRY_LOCK:
        _ROOMS.clear()
