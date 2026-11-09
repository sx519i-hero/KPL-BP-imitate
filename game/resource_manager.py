"""本地赛事资源管理（v5 需求 8）。

设计目标
--------
1. **下载前置检测**：任何一次下载之前先看本地文件在不在、大小/哈希对不对；
   校验通过就直接跳过，绝不重复拉取。
2. **增量更新**：本地索引里存了每场比赛的 ``end_time``（比赛结束时间）。
   只有「``end_time`` < 当前系统时间」（比赛已经打完）且本地还没有这场比赛资源时，
   才会自动联网抓取。未结束的比赛不抓。
3. **不在每次打开游戏/进入赛事页时联网**：本模块只在你点击
   「检查并增量更新」或显式调用 ``/api/resources/update`` 时才发请求。
4. **完全容错**：任何异常只写日志（``log`` 列表 + 后端 logger），
   继续使用本地已有资源；文件损坏（大小/哈希不符）会被识别并重新下载。
5. **解耦**：本模块只读写 ``data/resources/`` 与 ``data/resources/index.json``，
   不碰 ``data/league_*`` / 生涯存档 / BP 历史的结构。

资源粒度
--------
一个「赛事资源包」= 一场比赛（match）：包含该场的小局列表、BP 序列、选手数据。
落盘路径：``data/resources/matches/<league_id>/<match_id>.json``
索引文件：``data/resources/index.json``（记录 end_time / size / sha1 / 抓取时间）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths  # v10：打包后资源目录在 exe 同级

PKG_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(paths.data_dir())
RES_DIR = DATA_DIR / "resources"
MATCH_DIR = RES_DIR / "matches"
INDEX_PATH = RES_DIR / "index.json"
CACHE_DIR = PKG_DIR / ".scrape_cache"

INDEX_VERSION = 1
_LOCK = threading.RLock()

#: 单次「增量更新」最多抓多少场，避免一次点下去跑十几分钟
DEFAULT_LIMIT = 12
#: 判定「比赛已结束」时允许的额外宽限（网络时间差/裁判延迟）
END_GRACE = timedelta(minutes=30)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now()


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:  # 时间戳（秒/毫秒）
        num = float(raw)
        if num > 1e12:
            num /= 1000.0
        return datetime.fromtimestamp(num)
    except (TypeError, ValueError):
        return None


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_dirs() -> None:
    RES_DIR.mkdir(parents=True, exist_ok=True)
    MATCH_DIR.mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, payload: Any) -> None:
    _ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
def load_index() -> Dict[str, Any]:
    """读本地资源索引；缺失/损坏都返回一个空索引（不抛异常）。"""
    with _LOCK:
        if not INDEX_PATH.exists():
            return {"version": INDEX_VERSION, "updated_at": "", "leagues": {}, "matches": {}}
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("index.json 顶层不是对象")
            data.setdefault("version", INDEX_VERSION)
            data.setdefault("leagues", {})
            data.setdefault("matches", {})
            if not isinstance(data["matches"], dict):
                data["matches"] = {}
            return data
        except Exception:
            return {"version": INDEX_VERSION, "updated_at": "", "leagues": {}, "matches": {}}


def save_index(index: Dict[str, Any]) -> None:
    index["version"] = INDEX_VERSION
    index["updated_at"] = _iso()
    _write_json_atomic(INDEX_PATH, index)


# --------------------------------------------------------------------------- #
# 网络（复用 scraper.HttpClient：带磁盘缓存 + 抓取礼仪）
# --------------------------------------------------------------------------- #
def _client():
    """懒加载 scraper.HttpClient；失败返回 None（此时只用本地资源）。"""
    try:
        import scraper  # noqa: WPS433 —— 延迟导入，避免拖慢 Flask 启动

        if scraper.requests is None:  # 没装 requests
            return None
        return scraper
    except Exception:
        return None


def _http_json(module, url: str) -> Optional[Dict[str, Any]]:
    """GET 一个 JSON 接口。任何异常都返回 None，绝不上抛。"""
    try:
        use_cache = CACHE_DIR.exists()
        client = module.HttpClient(CACHE_DIR, use_cache=use_cache)
        return client.fetch_json(url)
    except Exception as exc:  # pragma: no cover - 网络问题一律吞掉
        try:
            import logging

            logging.getLogger("kpl.resources").info("资源抓取失败（忽略，用本地资源）：%s", exc)
        except Exception:
            pass
        return None


def _api_list(obj: Any) -> List[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return []
    for key in ("results", "data"):
        rows = obj.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


# --------------------------------------------------------------------------- #
# 赛事 / 比赛清单
# --------------------------------------------------------------------------- #
def list_leagues() -> List[Dict[str, Any]]:
    """读本地 ``data/leagues.json``（不联网）。"""
    path = DATA_DIR / "leagues.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        rows = payload.get("leagues") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    except Exception:
        pass
    return []


def fetch_match_list(league_id: str) -> List[Dict[str, Any]]:
    """抓某赛季的比赛清单（含 end_time）。失败返回空列表。"""
    module = _client()
    if module is None:
        return []
    url = "%s?league_id=%s" % (module.URL_MATCHES, league_id)
    rows = _api_list(_http_json(module, url))
    out: List[Dict[str, Any]] = []
    for row in rows:
        mid = row.get("match_id")
        if not mid:
            continue
        end_dt = _parse_dt(row.get("end_time"))
        out.append(
            {
                "match_id": str(mid),
                "league_id": str(league_id),
                "end_time": _iso(end_dt) if end_dt else "",
                "end_dt": end_dt,
                "start_time": row.get("start_time") or "",
                "bo": row.get("bo"),
                "status": row.get("status"),
                "stage_name": row.get("match_stage_desc") or row.get("match_stage_name") or "",
                "camp1": row.get("camp1") or {},
                "camp2": row.get("camp2") or {},
                "battles": row.get("match_battle_video_list") or [],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 资源包落盘 / 校验
# --------------------------------------------------------------------------- #
def resource_path(league_id: str, match_id: str) -> Path:
    return MATCH_DIR / str(league_id) / ("%s.json" % match_id)


def is_finished(match: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """比赛是否已经打完（end_time + 宽限 < 当前时间）。"""
    end_dt = match.get("end_dt") or _parse_dt(match.get("end_time"))
    if end_dt is None:
        return False
    return (now or _now()) > (end_dt + END_GRACE)


def verify_entry(entry: Dict[str, Any], league_id: str = "", match_id: str = "") -> str:
    """校验一条索引记录对应的本地文件。

    返回: ``"ok"`` | ``"missing"`` | ``"corrupt"``
    """
    path_text = entry.get("path")
    path = Path(path_text) if path_text else resource_path(
        league_id or entry.get("league_id", ""), match_id or entry.get("match_id", "")
    )
    if not path.exists():
        return "missing"
    try:
        size = path.stat().st_size
    except OSError:
        return "missing"
    if size <= 0:
        return "corrupt"
    expected_size = entry.get("size")
    if isinstance(expected_size, int) and expected_size >= 0 and size != expected_size:
        return "corrupt"
    expected_sha = entry.get("sha1")
    if isinstance(expected_sha, str) and expected_sha:
        try:
            if sha1_of(path) != expected_sha:
                return "corrupt"
        except OSError:
            return "corrupt"
    return "ok"


def build_bundle(league_id: str, match: Dict[str, Any], log: List[str]) -> Optional[Dict[str, Any]]:
    """抓一场比赛的小局详情并与比赛清单合并成一个资源包。

    只用官方公开接口；任何小局抓不到就跳过（保留已有信息），不编造。
    """
    module = _client()
    if module is None:
        log.append("· 未能加载抓取模块（scraper.py / requests 不可用），跳过 %s" % match.get("match_id"))
        return None

    battles = []
    for item in match.get("battles") or []:
        if not isinstance(item, dict):
            continue
        bid = item.get("battle_id")
        if not bid:
            continue
        detail = _http_json(module, "%s?battle_id=%s" % (module.URL_BATTLE_DETAIL, bid))
        data = detail.get("data") if isinstance(detail, dict) else None
        if not isinstance(data, dict):
            log.append("· 小局 %s 详情抓取失败，已跳过" % bid)
            continue
        battles.append(
            {
                "battle_id": str(bid),
                "battle_seq": item.get("battle_seq"),
                "camp1": data.get("camp1"),
                "camp2": data.get("camp2"),
                "win_camp": data.get("win_camp"),
                "bp_list": data.get("bp_list") or [],
                "battle_player_list": data.get("battle_player_list") or [],
            }
        )

    bundle = {
        "league_id": str(league_id),
        "match_id": str(match.get("match_id")),
        "start_time": match.get("start_time") or "",
        "end_time": match.get("end_time") or "",
        "bo": match.get("bo"),
        "stage": match.get("stage_name") or "",
        "camp1": match.get("camp1") or {},
        "camp2": match.get("camp2") or {},
        "battles": battles,
        "_fetched_at": _iso(),
        "_source": "https://prod.comp.smoba.qq.com/leaguesite/battle/open",
    }
    return bundle


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #
def summary() -> Dict[str, Any]:
    """本地资源现状（**不联网、不发请求**）。"""
    index = load_index()
    leagues = list_leagues()
    matches = index.get("matches") or {}
    files = 0
    size_bytes = 0
    corrupt = 0
    for key, entry in list(matches.items()):
        if not isinstance(entry, dict):
            continue
        state = verify_entry(entry)
        if state == "ok":
            files += 1
            try:
                size_bytes += Path(entry.get("path") or "").stat().st_size
            except OSError:
                pass
        elif state == "corrupt":
            corrupt += 1

    pending = 0
    now = _now()
    for row in (index.get("leagues") or {}).values():
        if isinstance(row, dict):
            pending += int(row.get("pending") or 0)

    return {
        "leagues": len(leagues),
        "matches": len(matches),
        "files": files,
        "pending": pending,
        "corrupt": corrupt,
        "size_mb": round(size_bytes / 1048576.0, 2),
        "updated_at": index.get("updated_at") or "",
        "index_path": str(INDEX_PATH.relative_to(PKG_DIR)).replace("\\", "/"),
    }


def check(log: Optional[List[str]] = None) -> Dict[str, Any]:
    """校验本地资源完整性（不联网）。损坏的会被标记，等下次 update 重下。"""
    log = log if log is not None else []
    with _LOCK:
        index = load_index()
        matches = index.get("matches") or {}
        ok = missing = corrupt = 0
        fixed = 0
        for key, entry in list(matches.items()):
            if not isinstance(entry, dict):
                continue
            state = verify_entry(entry, entry.get("league_id", ""), entry.get("match_id", key))
            entry["state"] = state
            entry["last_checked"] = _iso()
            if state == "ok":
                ok += 1
            elif state == "missing":
                missing += 1
                entry.pop("sha1", None)
            else:
                corrupt += 1
                entry.pop("sha1", None)
                fixed += 1
                log.append("· %s 本地文件损坏，已标记待重新下载" % key)
        save_index(index)
    log.append("校验完成：正常 %d、缺失 %d、损坏 %d" % (ok, missing, corrupt))
    result = summary()
    result["checked"] = {"ok": ok, "missing": missing, "corrupt": corrupt}
    return result


def update(limit: int = DEFAULT_LIMIT, league_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """增量更新：只抓「已结束 + 本地缺失/损坏」的比赛资源。"""
    log: List[str] = []
    _ensure_dirs()
    with _LOCK:
        index = load_index()
        matches = index.get("matches") or {}
        leagues_meta = index.get("leagues") or {}

        targets = league_ids or [r.get("league_id") for r in list_leagues() if r.get("league_id")]
        fetched = skipped = failed = 0
        downloaded_names: List[str] = []

        for lid in targets:
            if fetched >= limit:
                break
            rows = fetch_match_list(lid)
            if not rows:
                log.append("· 赛季 %s 比赛清单拉取失败/为空，跳过（用本地资源）" % lid)
                leagues_meta[lid] = dict(leagues_meta.get(lid) or {}, last_error=_iso())
                continue

            finished = [r for r in rows if is_finished(r)]
            not_yet = len(rows) - len(finished)
            need: List[Dict[str, Any]] = []
            for row in finished:
                mid = row["match_id"]
                entry = matches.get(mid)
                if isinstance(entry, dict) and verify_entry(entry, lid, mid) == "ok":
                    skipped += 1
                    continue
                need.append(row)
            leagues_meta[lid] = {
                "match_total": len(rows),
                "finished": len(finished),
                "not_yet": not_yet,
                "pending": len(need),
                "last_checked": _iso(),
            }
            log.append(
                "· 赛季 %s：共 %d 场，已结束 %d 场，未到结束时间 %d 场，需下载 %d 场"
                % (lid, len(rows), len(finished), not_yet, len(need))
            )

            for row in need:
                if fetched >= limit:
                    log.append("· 已达单次上限 %d 场，剩余留到下次" % limit)
                    break
                mid = row["match_id"]
                bundle = build_bundle(lid, row, log)
                if bundle is None:
                    failed += 1
                    continue
                path = resource_path(lid, mid)
                try:
                    _write_json_atomic(path, bundle)
                    entry = {
                        "league_id": lid,
                        "match_id": mid,
                        "end_time": row.get("end_time") or "",
                        "path": str(path),
                        "size": path.stat().st_size,
                        "sha1": sha1_of(path),
                        "fetched_at": _iso(),
                        "battles": len(bundle.get("battles") or []),
                        "state": "ok",
                    }
                    matches[mid] = entry
                    fetched += 1
                    downloaded_names.append(mid)
                except Exception as exc:
                    failed += 1
                    log.append("· %s 写入失败：%s" % (mid, exc))

        index["matches"] = matches
        index["leagues"] = leagues_meta
        index["updated_at"] = _iso()
        try:
            save_index(index)
        except Exception as exc:
            log.append("· 索引写入失败：%s（本地文件仍可用）" % exc)

    log.append(
        "增量更新完成：新下载 %d 场、跳过（本地已完整）%d 场、失败 %d 场"
        % (fetched, skipped, failed)
    )
    result = summary()
    result["update"] = {
        "fetched": fetched,
        "skipped": skipped,
        "failed": failed,
        "downloaded": downloaded_names[:20],
    }
    result["log"] = log
    return result


def status() -> Dict[str, Any]:
    """给设置页用的轻量状态（不联网）。"""
    data = summary()
    data["ok"] = True
    return data
