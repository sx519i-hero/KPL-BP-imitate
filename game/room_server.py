"""v4 · 朋友 PK 房间服务器（房主端在单独端口上监听）。

房主点「创建房间」后，本模块在房主机器上起一个独立 HTTP 服务（默认端口 5001，
可自定义），其他玩家用 ``http://房主IP:端口`` 连进来。因为监听 ``0.0.0.0`` 且
**不校验来源 IP**，所以对内网穿透（frp / ngrok 等）同样可用。

跨域：客户端页面来自他们自己的 ``http://127.0.0.1:5000``，因此这里必须返回
``Access-Control-Allow-Origin: *``。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from . import room_engine

LOGGER = logging.getLogger("kpl.room")
API_PREFIX = "/api/room"


class RoomServerError(Exception):
    pass


def _edition_hint(edition_id: str):
    """把 ``<key>:<league_id>`` 或 ``annual_package:<year>`` 拆成 (赛季, 四站, 赛事名)。

    v32：好友 PK 的联赛也支持「届次」，本函数只做解析，真实数据仍走 data_loader。
    """
    from . import data_loader

    try:
        rows = (data_loader.leagues_payload() or {}).get("leagues") or []
    except Exception:                                       # noqa: BLE001
        rows = []
    if ":" in edition_id:
        key, tail = edition_id.split(":", 1)
    else:
        key, tail = edition_id, ""
    if key == "annual_package":
        year = tail.strip()
        stations = None
        try:
            stations = data_loader.package_stations_of_year(year) or None
        except Exception:                                   # noqa: BLE001
            stations = None
        league = None
        if stations:
            league = stations[0].get("league_id")
        if not league:
            for row in rows:
                if year and year in str(row.get("name") or "") and "春季赛" in str(row.get("name") or ""):
                    league = row.get("league_id")
                    break
        return league, stations, ("%s 年度赛程包" % year if year else None)
    league = tail or None
    name = None
    for row in rows:
        if str(row.get("league_id")) == str(league):
            name = row.get("name")
            break
    return league, None, name


def _json(payload: Any, status: int = 200):
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def _fail(message: str, status: int = 400):
    return _json({"ok": False, "error": message}, status)


def local_ips() -> List[str]:
    """尽量列出本机的内网 IP（失败就只给 127.0.0.1，不报错）。"""
    ips: List[str] = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.4)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def build_app(room: room_engine.Room) -> Flask:
    app = Flask("kpl_room_%s" % room.room_id)
    app.logger.setLevel(logging.INFO)

    @app.after_request
    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(room_engine.RoomError)
    def _room_error(exc: room_engine.RoomError):
        return _fail(str(exc), getattr(exc, "status", 400))

    @app.errorhandler(404)
    def _not_found(_exc):
        return _fail("接口不存在：%s" % request.path, 404)

    @app.errorhandler(Exception)
    def _boom(exc):                                        # noqa: BLE001
        LOGGER.exception("[room] 未处理异常")
        return _fail("房主端出错：%s: %s" % (type(exc).__name__, exc), 500)

    def body() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
        data = request.form.to_dict() or {}
        return data if isinstance(data, dict) else {}

    def pid_of(data: Dict[str, Any]) -> str:
        pid = str(data.get("pid") or request.args.get("pid") or "").strip()
        if not pid:
            raise room_engine.RoomError("缺少 pid（请重新加入房间）", 400)
        return pid

    @app.route(API_PREFIX + "/ping", methods=["GET", "OPTIONS"])
    def ping():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        return _json({"ok": True, "room_id": room.room_id, "mode": room.mode,
                      "server_time": time.time(), "hoster": "kpl_bp_game v4"})

    @app.route(API_PREFIX + "/info", methods=["GET", "OPTIONS"])
    def info():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        room._refresh_online()                             # noqa: SLF001
        return _json({
            "ok": True,
            "room_id": room.room_id,
            "mode": room.mode,
            "league_id": room.league_id,
            "league_name": room.league_name,
            "players": [{"name": p["name"], "team": p["team"], "is_host": p["is_host"],
                         "online": p["online"], "side": p["side"]} for p in room.players],
            "max_players": room_engine.MAX_PLAYERS if room.mode == "league" else 2,
            "port": room.port,
        })

    @app.route(API_PREFIX + "/teams", methods=["GET", "OPTIONS"])
    def teams():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        rows = room_engine._roster(room.league_id)          # noqa: SLF001
        taken = {p["team"] for p in room.players if p.get("team")}
        return _json({"ok": True, "league_id": room.league_id,
                      "teams": [{"name": t.get("name"), "logo_path": t.get("logo_path"),
                                 "rank": t.get("rank"), "win_rate": t.get("win_rate"),
                                 "taken": t.get("name") in taken} for t in rows]})

    @app.route(API_PREFIX + "/join", methods=["POST", "OPTIONS"])
    def join():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        result = room.join(str(data.get("name") or "玩家"), data.get("team"))
        return _json({"ok": True, **result})

    @app.route(API_PREFIX + "/leave", methods=["POST", "OPTIONS"])
    def leave():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        room.leave(pid_of(data))
        return _json({"ok": True})

    @app.route(API_PREFIX + "/heartbeat", methods=["POST", "OPTIONS"])
    def heartbeat():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        return _json({"ok": True, **room.heartbeat(pid_of(data))})

    @app.route(API_PREFIX + "/state", methods=["GET", "OPTIONS"])
    def state():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        pid = str(request.args.get("pid") or "").strip()
        if not pid:
            raise room_engine.RoomError("缺少 pid（请重新加入房间）", 400)
        return _json(room.state(pid))

    @app.route(API_PREFIX + "/start_single", methods=["POST", "OPTIONS"])
    def start_single():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        # v47：单局 PK 支持 BO1/3/5/7/9
        room.start_single(pid, bo=data.get("bo") or 1)
        # 回带完整局面：前端一次请求就能渲染，不用再补一次 /state
        return _json({"ok": True, "version": room.version, "state": room.state(pid)})

    @app.route(API_PREFIX + "/start_league", methods=["POST", "OPTIONS"])
    def start_league():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        template = (data.get("template") or "").strip()
        # v9 需求 2①：自定义存档名（留空则自动命名「联机联赛 · 春季赛」）
        room.save_name = (data.get("save_name") or "").strip() or None
        if template:
            # v6：按赛事模板开完整 KPL 联赛（SAB 三阶段 / 挑杯 / 年总 / 年度赛程包）
            # v32：房主可指定「届次」，决定数据赛季 + 官方站名（和生涯模式对齐）
            edition_id = (data.get("edition_id") or "").strip()
            league_override = None
            stations_override = None
            event_override = None
            if edition_id:
                league_override, stations_override, event_override = _edition_hint(edition_id)
            room.start_league_tourney(pid, template,
                                      league_id=league_override,
                                      stations=stations_override,
                                      event_name=event_override)
        else:
            size = data.get("size") or 8
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 8
            room.start_league(pid, size=size)
        return _json({"ok": True, "version": room.version, "state": room.state(pid)})

    @app.route(API_PREFIX + "/resume_league", methods=["POST", "OPTIONS"])
    def resume_league():
        """v9 需求 2③：房主读档继续联机联赛。"""
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        save_id = (data.get("save_id") or "").strip()
        if not save_id:
            raise room_engine.RoomError("缺少 save_id") from None
        room.start_league_tourney_from_save(pid, save_id)
        return _json({"ok": True, "version": room.version, "save_id": room.save_id,
                      "state": room.state(pid)})

    @app.route(API_PREFIX + "/league_next", methods=["POST", "OPTIONS"])
    def league_next():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        result = room.league_next(pid)
        return _json({"ok": True, "version": room.version, **result,
                      "state": room.state(pid)})

    @app.route(API_PREFIX + "/action", methods=["POST", "OPTIONS"])
    def action():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        try:
            ename = int(data.get("ename"))
        except (TypeError, ValueError):
            raise room_engine.RoomError("缺少 ename") from None
        pid = pid_of(data)
        result = room.act(pid, ename, data.get("action"))
        return _json({"ok": True, **result, "state": room.state(pid)})

    @app.route(API_PREFIX + "/auto", methods=["POST", "OPTIONS"])
    def auto_advance():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        return _json({"ok": True, **room.auto_until_human(pid), "state": room.state(pid)})

    @app.route(API_PREFIX + "/autocomplete", methods=["POST", "OPTIONS"])
    def autocomplete():
        """v17：一键 BP —— 本小局剩余 ban/pick 全部交给内置 AI。"""
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        result = room.autocomplete_bp(pid)
        return _json({"ok": True, "version": room.version, "match": result.get("match"),
                      "state": room.state(pid)})

    @app.route(API_PREFIX + "/bracket", methods=["GET", "POST", "OPTIONS"])
    def bracket_view():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        stage = None
        if request.method == "POST":
            stage = (body().get("stage") or "").strip() or None
        else:
            stage = (request.args.get("stage") or "").strip() or None
        return _json({"ok": True, **room.bracket_stage(stage)})

    @app.route(API_PREFIX + "/ai_step", methods=["POST", "OPTIONS"])
    def ai_step():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        return _json({"ok": True, **room.ai_step(pid), "state": room.state(pid)})

    @app.route(API_PREFIX + "/side_choice", methods=["POST", "OPTIONS"])
    def side_choice():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        side = str(data.get("side") or "blue")
        pid = pid_of(data)
        return _json({"ok": True, **room.choose_side(pid, side), "state": room.state(pid)})

    @app.route(API_PREFIX + "/next_game", methods=["POST", "OPTIONS"])
    def next_game():
        if request.method == "OPTIONS":
            return _json({"ok": True})
        data = body()
        pid = pid_of(data)
        return _json({"ok": True, **room.next_game(pid), "state": room.state(pid)})

    return app


class _Handle:
    def __init__(self, server, thread: threading.Thread, port: int):
        self.server = server
        self.thread = thread
        self.port = port
        self.stopping = False
        self.restarts = 0

    def alive(self) -> bool:
        """服务线程还活着吗（v52：用于「端口自己关了」的自动恢复）。"""
        try:
            return bool(self.thread and self.thread.is_alive() and not self.stopping)
        except Exception:                                   # noqa: BLE001
            return False

    def stop(self) -> None:
        self.stopping = True
        try:
            self.server.shutdown()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.thread.join(timeout=3)
        except Exception:                                   # noqa: BLE001
            pass
        # 兜底：有些平台上 shutdown 后 socket 仍挂在监听态，显式关掉
        try:
            self.server.server_close()
        except Exception:                                   # noqa: BLE001
            pass


def start_server(room: room_engine.Room, port: int) -> _Handle:
    """在 ``0.0.0.0:port`` 上起房间服务；端口占用时抛 RoomServerError。"""
    app = build_app(room)
    try:
        server = make_server("0.0.0.0", int(port), app, threaded=True)
    except OSError as exc:
        raise RoomServerError("端口 %s 无法监听（可能已被占用）：%s" % (port, exc)) from exc
    # v52：外面包一层「永不退出」的循环。
    # 以前 serve_forever 一旦抛异常（Windows 上偶发的 accept 失败、杀毒软件重置监听 socket 等），
    # 房间线程就静默结束 —— 表现就是「建好房间后端口自己关了」。
    def _serve_forever() -> None:
        while True:
            try:
                server.serve_forever(poll_interval=0.5)
                return                      # 只有 shutdown() 才会正常退出
            except Exception as exc:        # noqa: BLE001
                LOGGER.exception("[room] 房间服务异常，1 秒后自动恢复监听：%s", exc)
                time.sleep(1.0)
                continue

    thread = threading.Thread(target=_serve_forever, name="kpl-room-%s" % port,
                              daemon=True)
    thread.start()
    LOGGER.info("[room] 房间 %s 已在 0.0.0.0:%s 监听", room.room_id, port)
    return _Handle(server, thread, int(port))
