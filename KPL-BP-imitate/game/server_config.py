#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""服务器监听配置（IP / 端口）——独立 JSON，不硬编码。

为什么单独一个文件
------------------
用户会把整个项目丢到云服务器上跑，然后用手机从外部访问，所以监听地址和端口
必须能改、能持久化、重启后仍然生效。项目约定「配置走独立 JSON」，这里就是那份
JSON：``data/server_config.json``。

字段
----
    host  监听地址。常见取值：
            "0.0.0.0"   允许外部 / 局域网 / 手机访问（服务器部署用这个）
            "127.0.0.1" 只允许本机访问（最安全）
            "192.168.1.10" 只绑定某张网卡
    port  监听端口，1~65535。默认 5000。

环境变量 ``HOST`` / ``PORT`` 优先级高于本文件（给多实例联调测试用）。

读写都做了兜底：文件不存在、JSON 坏掉、字段类型不对，一律回退默认值并打印一行
提示，绝不让进程起不来。
"""

import io
import json
import os
import socket
from typing import Any, Dict, List, Optional, Tuple

from . import paths  # v10：打包后配置写在 exe 同级

BASE_DIR = paths.resource_dir()
DATA_DIR = paths.data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "server_config.json")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000

# 监听地址白名单式校验：IPv4 / 主机名 / IPv6（含冒号）
_ALLOWED_HOST_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_:[]")


def _log(message: str) -> None:
    print("[server-config] %s" % message, flush=True)


def normalize_host(value: Any, fallback: str = DEFAULT_HOST) -> str:
    """规整监听地址；非法值回退 ``fallback``。"""
    host = str(value or "").strip()
    if not host:
        return fallback
    if len(host) > 64:
        return fallback
    if any(ch not in _ALLOWED_HOST_CHARS for ch in host):
        return fallback
    # 单个点 / 纯符号这类明显无效的写法直接挡掉
    if host.strip(".-_:[]") == "":
        return fallback
    return host


def normalize_port(value: Any, fallback: int = DEFAULT_PORT) -> int:
    """规整端口；非法值（非数字 / 越界）回退 ``fallback``。"""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    if port < 1 or port > 65535:
        return fallback
    return port


def defaults() -> Dict[str, Any]:
    return {"host": DEFAULT_HOST, "port": DEFAULT_PORT}


def load() -> Dict[str, Any]:
    """读取配置；任何异常都回退默认值（只打日志，不抛）。"""
    cfg = defaults()
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with io.open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            cfg["host"] = normalize_host(raw.get("host"), DEFAULT_HOST)
            cfg["port"] = normalize_port(raw.get("port"), DEFAULT_PORT)
        else:
            _log("配置文件不是 JSON 对象，回退默认值")
    except Exception as exc:                                   # noqa: BLE001
        _log("读取 %s 失败（%s），回退默认值" % (CONFIG_PATH, exc))
    return cfg


def save(config: Dict[str, Any]) -> Tuple[bool, str]:
    """原子写配置；返回 (是否成功, 说明文案)。"""
    cfg = {
        "host": normalize_host((config or {}).get("host"), DEFAULT_HOST),
        "port": normalize_port((config or {}).get("port"), DEFAULT_PORT),
    }
    cfg["_updated_at"] = _now()
    try:
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        tmp = CONFIG_PATH + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _log("已保存监听配置 %s:%d" % (cfg["host"], cfg["port"]))
        return True, "已保存"
    except Exception as exc:                                   # noqa: BLE001
        _log("保存失败：%s" % exc)
        return False, "保存失败：%s" % exc


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def effective() -> Dict[str, Any]:
    """实际生效的监听参数（环境变量覆盖文件）。"""
    cfg = load()
    host = normalize_host(os.environ.get("HOST") or cfg["host"], cfg["host"])
    port = normalize_port(os.environ.get("PORT") or cfg["port"], cfg["port"])
    return {"host": host, "port": port, "config": cfg,
            "host_from_env": bool(os.environ.get("HOST")),
            "port_from_env": bool(os.environ.get("PORT"))}


def lan_ips() -> List[str]:
    """尽力列出本机可被外部访问的 IPv4（失败返回空列表，不抛）。"""
    ips: List[str] = []

    def _add(value: Optional[str]) -> None:
        if not value:
            return
        value = str(value).strip()
        if not value or value.startswith("127.") or value == "0.0.0.0":
            return
        if value not in ips:
            ips.append(value)

    # 1) 连一个外部地址拿到出口网卡地址（不会真的发包）
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            _add(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:                                          # noqa: BLE001
        pass
    # 2) 主机名解析兜底
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except Exception:                                          # noqa: BLE001
        pass
    return ips


def access_urls(eff: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """返回可访问地址列表：``[{label, url, note}]``。"""
    eff = eff or effective()
    host, port = eff["host"], eff["port"]
    rows: List[Dict[str, str]] = []
    rows.append({"label": "本机", "url": "http://127.0.0.1:%d" % port,
                 "note": "只在这台机器上打开"})
    if host in ("0.0.0.0", "::", ""):
        for ip in lan_ips():
            rows.append({"label": "局域网 / 服务器 IP", "url": "http://%s:%d" % (ip, port),
                         "note": "同一网络下的手机、平板用这个"})
        rows.append({"label": "公网访问", "url": "http://<服务器公网IP>:%d" % port,
                     "note": "云服务器需在安全组放行端口；家用宽带可配合内网穿透"})
    elif host.startswith("127."):
        rows.append({"label": "当前仅本机", "url": "http://127.0.0.1:%d" % port,
                     "note": "监听地址是 127.0.0.1，外部设备访问不到；改成 0.0.0.0 可对外"})
    else:
        rows.append({"label": "指定网卡", "url": "http://%s:%d" % (host, port),
                     "note": "只监听这一个地址"})
    return rows
