#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""启动器辅助：文案打印 / Python 探测 / 端口检查 / 延时开浏览器。

为什么需要这个文件
------------------
`start.bat` 必须保持**纯 ASCII**。cmd.exe 解析 .bat 时用的是当前控制台
代码页：含多字节中文的 .bat 在中文 Windows（CP936）下会把 `for /f "delims="`
这类行的引号和括号拆错，直接报「不是内部或外部命令」，脚本根本跑不到
启动那一步（已实测复现）。而文件开头的 `chcp 65001` 是在**解析之后**才
生效，救不了已经解析崩掉的脚本。

第二个坑：`for /f ... in ('"%PY%" -c "..."')` 这种**带引号的可执行文件
路径**放进 for /f 会被 cmd 吃掉首尾引号，报
`'D:\\...\\python.exe" -c "import' is not recognized`。所以版本探测这类
需要捕获输出的动作统一交给本文件做，bat 里只做直接调用。

所以：start.bat 里一个中文都不放（先 `chcp 65001` 把控制台切成 UTF-8），
所有中文提示由本文件打印 —— 本文件是 UTF-8 源码，Python 读取没有问题，
输出前再把 stdout 重设为 UTF-8，配合 65001 代码页即可正确显示。

子命令
------
    python launcher.py probe <python路径>
        验证该 Python 可运行；成功打印「已找到 Python：... （版本 ...）」
        并退出 0，失败退出 1。

    python launcher.py port <端口>
        端口空闲退出 0，已被监听退出 1。

    python launcher.py open_delayed [秒数] [端口]
        等若干秒（默认 3）后打开 http://127.0.0.1:<端口>（默认 5000）。

    python launcher.py open [端口]
        立即打开浏览器。

    python launcher.py <文案键> [参数...]
        打印预定义中文文案（见 MESSAGES）。
"""

import io
import os
import socket
import sys
import time

BAR = "=" * 70

MESSAGES = {
    "banner": [
        "",
        "  KPL 模拟 BP 教练 · 一键启动",
        "",
    ],
    "py_missing": [
        "",
        "[错误] 没有找到可用的 Python。",
        "",
        "请先安装 Python 3.8 或更高版本： https://www.python.org/downloads/",
        "安装时务必勾选 “Add Python to PATH”，然后重新运行本脚本。",
        "",
    ],
    "deps_installing": [
        "[3/5] 正在安装依赖（首次启动需要联网，约 1-3 分钟）...",
    ],
    "venv_creating": [
        "[2/5] 正在创建虚拟环境 .venv（首次启动）...",
    ],
    "venv_ready": [
        "[2/5] 虚拟环境已就绪（.venv）。",
    ],
    "venv_fallback": [
        "[2/5] 虚拟环境创建失败，改用系统 Python 继续。",
    ],
    "deps_done": [
        "[3/5] 依赖安装完成。",
    ],
    "deps_ready": [
        "[3/5] 依赖已就绪。",
    ],
    "scrape_start": [
        "",
        "[4/5] 首次启动，正在抓取真实 KPL 数据，请稍候...",
        "      数据源：腾讯王者荣耀官方联赛接口（pvp.qq.com / prod.comp.smoba.qq.com）",
        "      全量抓取约需 10-25 分钟（约 700 场比赛 / 3000 个小局的完整 BP 记录）",
        "      已抓到的内容会缓存在 .scrape_cache/，中途关掉下次会接着抓。",
        "",
    ],
    "scrape_done": [
        "",
        "[4/5] 数据抓取完成。",
    ],
    "data_missing": [
        "",
        "[提示] 没有找到可用的英雄数据（data/heroes.json）。",
        "       启动游戏后打开「数据 → 更新数据」，点「更新并抓取」即可自动抓取，",
        "       不需要自己运行 Python 脚本。",
        "",
    ],
    "data_skip": [
        "[4/5] 数据已存在，跳过抓取。",
    ],
    "serve": [
        "",
        "[5/5] 正在启动服务器 ...",
    ],
    "serve_lines": [
        "",
        "  浏览器会自动打开 http://127.0.0.1:{0}",
        "  如果没有自动打开，请手动在浏览器输入上面的地址。",
        "",
        "  关闭这个窗口即可停止游戏。",
        "",
    ],
    "stopped": [
        "",
        "服务器已停止。",
    ],
    "already_running": [
        "",
        "[提示] 检测到 127.0.0.1:{0} 上已经有本游戏在运行。",
        "       正在为你打开浏览器，不再重复启动。",
        "",
        "       如果要重启服务，请先关闭那个窗口。",
        "",
    ],
}


def _stdout_utf8():
    """把 stdout 切到 UTF-8（bat 已 chcp 65001）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def emit(lines):
    _stdout_utf8()
    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def probe_python(exe):
    """验证 exe 真的是一个可用的 Python，返回版本号或 None。"""
    import subprocess

    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print(sys.version.split()[0])"],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    ver = (out.stdout or "").strip().splitlines()
    return ver[0].strip() if ver else None


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _open_browser(port):
    """尽力打开系统默认浏览器。

    三条路径依次尝试，任何一条成功即可：
      1) Windows 下 os.startfile（最贴近「双击」语义，不受 webbrowser 注册表探测影响）
      2) webbrowser.open
      3) Windows 下再兜底 cmd /c start
    打不开不是致命错误——用户手动输入提示里的地址一样能玩。
    """
    import webbrowser

    url = "http://127.0.0.1:%d" % port

    if os.name == "nt":
        try:
            os.startfile(url)  # noqa: S606  (Windows 专有，故意为之)
            return 0
        except OSError:
            pass

    try:
        if webbrowser.open(url, new=2):
            return 0
    except Exception:
        pass

    if os.name == "nt":
        try:
            import subprocess

            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            pass
    return 0


def _config_port(default=5000):
    """v9 补丁：端口优先取 data/server_config.json，读不到才用默认 5000。

    这样用户在「设置 → 服务器访问地址」里把端口改成 8080 之后，重启脚本
    检查端口、开浏览器用的都是 8080，不会还傻等 5000。
    """
    import json
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "data", "server_config.json")
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        port = int(raw.get("port"))
        if 1 <= port <= 65535:
            return port
    except Exception:
        pass
    return default


def _config_host(default="0.0.0.0"):
    """v9 补丁：监听地址（只用于打印提示，浏览器始终开 127.0.0.1）。"""
    import json
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "data", "server_config.json")
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        host = str(raw.get("host") or "").strip()
        if host:
            return host
    except Exception:
        pass
    return default


def _parse_port(value, default=5000):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        emit(["usage: python launcher.py <command|message-key> [args...]"])
        return 2

    key, rest = argv[0], argv[1:]

    if key == "probe":
        exe = rest[0] if rest else ""
        ver = probe_python(exe)
        if not ver:
            return 1
        emit(["[1/5] 已找到 Python：%s（版本 %s）" % (exe, ver)])
        return 0

    if key == "port":
        return 1 if _port_in_use(_parse_port(rest[0] if rest else _config_port())) else 0

    if key == "config_port":
        emit([str(_config_port())])
        return 0

    if key == "config_host":
        emit([_config_host()])
        return 0

    if key == "open_delayed":
        delay = 3.0
        if rest:
            try:
                delay = max(0.0, float(rest[0]))
            except (TypeError, ValueError):
                delay = 3.0
        port = _parse_port(rest[1] if len(rest) > 1 else _config_port())
        # 轮询等端口就绪，最多等 30 秒；端口已就绪则立即打开
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if _port_in_use(port):
                break
            time.sleep(0.25)
        else:
            time.sleep(delay)
        return _open_browser(port)

    if key == "open":
        return _open_browser(_parse_port(rest[0] if rest else _config_port()))

    template = MESSAGES.get(key)
    if template is None:
        emit(["unknown message key: %s" % key])
        return 2

    # v9 补丁：start.bat / start.sh 不再传端口（改由 data/server_config.json 决定），
    # 这里给带 {0} 的文案补上配置里的端口，避免打印出字面量 "{0}"。
    args = list(rest)
    if not args and any("{0}" in line for line in template):
        args = [str(_config_port())]
    try:
        emit([line.format(*args) for line in template])
    except (IndexError, KeyError):
        emit(list(template))
    return 0


if __name__ == "__main__":
    sys.exit(main())
