"""赛程存档读写（三方共用：KPL 年总模式 / 好友联机联赛 / 大模型联赛）。

设计目标
--------
* **同一套存档系统**：所有赛程存档都落在 ``data/tourney/<save_id>.json``，
  结构就是 ``game.tournament.new_save()`` 产出的那个 dict，所以三方可以互相读。
* **原子写**：先写 ``.tmp`` 再 ``os.replace``，断电/异常不会留下半截文件。
* **不依赖 Flask**：``game/room_engine.py``（联机房间）也要能读写存档，
  所以这里只用标准库，绝不 import ``app``。
* **失败不致命**：读写异常统一返回 ``(False, 错误信息)`` 或 ``None`` + 日志，
  由调用方决定是否弹窗提示，避免把整局游戏卡死。
* **新旧兼容**：读取时只做最小校验（必须是 dict 且有 ``save_id``），
  老存档缺的字段由 ``game.tournament`` 在推进时按需补默认值。

调试日志统一前缀 ``[save]``，方便在终端里 grep：
    [save] 写入 ts_xxx（mode=friend 阶段 r1 进度 12%）
    [save] 读取失败 ts_xxx：Expecting value: line 1 column 1 (char 0)
"""

from __future__ import annotations

import io
import json
import os
import secrets
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import paths  # v10：打包后存档写在 exe 同级的 data/tourney

BASE_DIR = paths.resource_dir()
SAVE_DIR = paths.data_path("tourney")

SAVE_VERSION = 4
MAX_SAVES = 120

_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _ensure_dir() -> None:
    try:
        os.makedirs(SAVE_DIR, exist_ok=True)
    except Exception as exc:                                  # noqa: BLE001
        print("[save] 创建存档目录失败：%s" % exc)


def new_save_id() -> str:
    """生成存档 ID：``ts_`` + 12 位十六进制（与 app.py 的校验正则一致）。"""
    return "ts_" + secrets.token_hex(6)


def save_path(save_id: str) -> str:
    return os.path.join(SAVE_DIR, "%s.json" % save_id)


def is_valid_id(save_id: str) -> bool:
    if not isinstance(save_id, str):
        return False
    if not save_id.startswith("ts_") or len(save_id) < 9 or len(save_id) > 35:
        return False
    return all(ch.isalnum() or ch == "_" for ch in save_id)


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 读 / 写 / 列表 / 删除
# --------------------------------------------------------------------------
def write(save: Dict[str, Any], *, quiet: bool = False) -> Tuple[bool, str]:
    """原子写入一个存档。返回 ``(ok, 错误信息)``。"""
    if not isinstance(save, dict) or not save.get("save_id"):
        return False, "存档缺少 save_id"
    save_id = str(save["save_id"])
    if not is_valid_id(save_id):
        return False, "存档 ID 不合法：%s" % save_id
    with _LOCK:
        _ensure_dir()
        save["version"] = SAVE_VERSION
        save["updated_at"] = _stamp()
        path = save_path(save_id)
        tmp = path + ".tmp"
        try:
            with io.open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(save, ensure_ascii=False, separators=(",", ":")))
            os.replace(tmp, path)
        except Exception as exc:                              # noqa: BLE001
            print("[save] 写入 %s 失败：%s" % (save_id, exc))
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:                                 # noqa: BLE001
                pass
            return False, str(exc)
        if not quiet:
            print("[save] 写入 %s（mode=%s 阶段 %s 进度 %s%%）"
                  % (save_id, save.get("mode"), save.get("stage"),
                     want_progress(save)))
        return True, ""


def read(save_id: str) -> Optional[Dict[str, Any]]:
    """读取存档；不存在/损坏返回 None（并打印日志），不抛异常。"""
    if not is_valid_id(str(save_id or "")):
        print("[save] 读取被拒绝：ID 不合法 %r" % (save_id,))
        return None
    path = save_path(str(save_id))
    if not os.path.exists(path):
        print("[save] 读取失败 %s：文件不存在" % save_id)
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:                                  # noqa: BLE001
        print("[save] 读取失败 %s：%s" % (save_id, exc))
        return None
    if not isinstance(data, dict) or not data.get("save_id"):
        print("[save] 读取失败 %s：格式不正确" % save_id)
        return None
    print("[save] 读取 %s（mode=%s 阶段 %s）"
          % (save_id, data.get("mode"), data.get("stage")))
    return data


def delete(save_id: str) -> bool:
    with _LOCK:
        path = save_path(str(save_id or ""))
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
        except Exception as exc:                              # noqa: BLE001
            print("[save] 删除 %s 失败：%s" % (save_id, exc))
            return False
        print("[save] 已删除 %s" % save_id)
        return True


def list_saves(mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出存档（按 updated_at 倒序）；``mode`` 为空则全部。

    返回的是**精简单行**，不含完整对局，够做列表 UI 用。
    """
    rows: List[Dict[str, Any]] = []
    if not os.path.isdir(SAVE_DIR):
        return rows
    for name in sorted(os.listdir(SAVE_DIR)):
        if not name.endswith(".json") or name.endswith(".tmp.json"):
            continue
        save_id = name[:-5]
        if not is_valid_id(save_id):
            continue
        data = read(save_id)
        if data is None:
            continue
        if mode and data.get("mode") != mode:
            continue
        rows.append(_row(data))
        if len(rows) >= MAX_SAVES:
            break
    rows.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return rows


def _row(save: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "save_id": save.get("save_id"),
        "save_name": save.get("save_name"),
        "mode": save.get("mode"),
        "template": save.get("template"),
        "category": save.get("category"),
        "event": (save.get("event") or {}).get("name") or save.get("event_name"),
        "stage": save.get("stage"),
        "stage_label": save.get("stage_label"),
        "status": save.get("status"),
        "progress": want_progress(save),
        "updated_at": save.get("updated_at"),
        "created_at": save.get("created_at"),
        "room": save.get("room") or {},
        "humans": (save.get("humans") or []),
    }


def want_progress(save: Dict[str, Any]) -> int:
    """粗略进度（0-100）：已完成场次 / 总场次。"""
    try:
        rows = save.get("schedule") or []
        total = len(rows)
        if not total:
            return 0
        done = sum(1 for match in rows if isinstance(match, dict)
                   and match.get("status") == "done")
        return int(round(done * 100.0 / total))
    except Exception:                                         # noqa: BLE001
        return 0


def stamp_created(save: Dict[str, Any]) -> Dict[str, Any]:
    if not save.get("created_at"):
        save["created_at"] = _stamp()
    return save
