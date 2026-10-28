"""device — 手机侧操作（全部通过 adb）

只负责「设备」，不碰业务接口。所有函数都可单独调用、便于测试。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time

from . import config


class DeviceError(RuntimeError):
    pass


_RESOLVED_SERIAL = None


def _candidate_serials():
    """按优先级列出候选串号：环境变量 → 无线地址 → USB 串号 → adb 已连上的任意设备。

    2026-10-27：这台机 USB 调试口会反复瞬断，断了就只能走无线 adb，
    所以串号必须能自动兜底，不能写死。
    """
    import os
    out = []
    env = os.environ.get("PDD_ADB_SERIAL")
    if env:
        out.append(env)
    wifi = os.environ.get("PDD_WIFI_ADDR", "")   # 例：192.168.1.50:5555
    if wifi:
        out.append(wifi)
    if config.ADB_SERIAL and config.ADB_SERIAL not in out:
        out.append(config.ADB_SERIAL)
    # adb 已经连上的设备（USB 或无线）
    try:
        p = subprocess.run([shutil.which("adb") or "adb", "devices"],
                           capture_output=True, text=True, timeout=15)
        for line in (p.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                if parts[0] not in out:
                    out.append(parts[0])
    except Exception:  # noqa: BLE001
        pass
    return out


def serial(refresh: bool = False) -> str:
    """返回一个当前可用的 adb 串号（自动兜底 USB/无线）。"""
    global _RESOLVED_SERIAL
    if _RESOLVED_SERIAL and not refresh:
        return _RESOLVED_SERIAL
    exe = shutil.which("adb") or "adb"
    for s in _candidate_serials():
        try:
            p = subprocess.run([exe, "-s", s, "get-state"],
                               capture_output=True, text=True, timeout=12)
            if "device" in (p.stdout or "") and "not found" not in (p.stdout + p.stderr):
                _RESOLVED_SERIAL = s
                return s
        except Exception:  # noqa: BLE001
            continue
    # 无线地址没连上就试着连一次
    import os
    wifi = os.environ.get("PDD_WIFI_ADDR", "")   # 例：192.168.1.50:5555
    try:
        subprocess.run([exe, "connect", wifi], capture_output=True, text=True, timeout=15)
        p = subprocess.run([exe, "-s", wifi, "get-state"],
                           capture_output=True, text=True, timeout=12)
        if "device" in (p.stdout or ""):
            _RESOLVED_SERIAL = wifi
            return wifi
    except Exception:  # noqa: BLE001
        pass
    _RESOLVED_SERIAL = config.ADB_SERIAL
    return _RESOLVED_SERIAL


def _adb(*args, timeout=30) -> str:
    exe = shutil.which("adb") or "adb"
    p = subprocess.run([exe, "-s", serial(), *args],
                       capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "") + (p.stderr or "")


def shell(cmd: str, timeout=30) -> str:
    return _adb("shell", cmd, timeout=timeout)


def root(cmd: str, timeout=30) -> str:
    """以 root 执行（设备是 KernelSU，su -c 可用）。"""
    return shell(f"su -c {_q(cmd)}", timeout=timeout)


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 连通性
def connected() -> bool:
    out = _adb("get-state", timeout=10)
    return "device" in out and "not found" not in out


def frida_alive() -> bool:
    out = root(f"ss -ltn 2>/dev/null | grep -c {config.FRIDA_PORT}")
    return out.strip().splitlines()[-1].strip() not in ("", "0") if out.strip() else False


def start_frida() -> bool:
    """确保 frida-server 在跑；返回是否可用。"""
    if frida_alive():
        return True
    root(f"setsid {config.FRIDA_BIN} -l 0.0.0.0:{config.FRIDA_PORT} "
         f">/data/local/tmp/frida.log 2>&1 &")
    time.sleep(3)
    _adb("forward", f"tcp:{config.FRIDA_PORT}", f"tcp:{config.FRIDA_PORT}", timeout=10)
    return frida_alive()


def forward_frida() -> None:
    _adb("forward", f"tcp:{config.FRIDA_PORT}", f"tcp:{config.FRIDA_PORT}", timeout=10)


# ---------------------------------------------------------------- 进程/前台
def pid(pkg: str = config.PKG) -> str:
    out = root(f"pidof {pkg}").strip()
    return out.splitlines()[-1].strip() if out.strip() else ""


def running(pkg: str = config.PKG) -> bool:
    return bool(pid(pkg))


def foreground() -> str:
    out = shell("dumpsys window 2>/dev/null | grep -oE 'mCurrentFocus=Window\\{[^}]*}' | head -1")
    m = re.search(r"u0 (\S+?)[/}]", out)
    return m.group(1) if m else ""


def foreground_full() -> str:
    out = shell("dumpsys window 2>/dev/null | grep -oE 'mCurrentFocus=Window\\{[^}]*}' | head -1")
    return out.strip()


def pdd_foreground() -> bool:
    return foreground().startswith(config.PKG)


# ---------------------------------------------------------------- 屏幕/电源
def screen_on() -> bool:
    out = root("dumpsys power | grep -o 'mWakefulness=[A-Za-z]*' | head -1")
    return "Awake" in out


def wake() -> None:
    root("input keyevent KEYCODE_WAKEUP")
    time.sleep(0.6)
    root("wm dismiss-keyguard")
    time.sleep(0.4)
    root("cmd statusbar collapse")


def keep_awake(tag: str = "pddgrab") -> None:
    """执行期间保活：常亮 + 息屏超时拉满 + 内核 wakelock（防 sleep 被冻住）。"""
    root(f"settings put global stay_on_while_plugged_in 7; "
         f"svc power stayon true; "
         f"settings put system screen_off_timeout 2147483647; "
         f"echo {tag} > /sys/power/wake_lock 2>/dev/null")


def allow_sleep(tag: str = "pddgrab", power_off: bool = True) -> None:
    """还原省电：撤常亮、恢复 60s 超时，可选直接关屏。"""
    root(f"echo {tag} > /sys/power/wake_unlock 2>/dev/null; "
         f"svc power stayon false; "
         f"settings put global stay_on_while_plugged_in 0; "
         f"settings put system screen_off_timeout 60000")
    if power_off:
        root("input keyevent KEYCODE_POWER")


# ---------------------------------------------------------------- 页面
def cold_start_pdd() -> None:
    """★ 关键步骤：force-stop 后重新打开活动页。

    2026-10-27 实测：页面/会话存活久了，兑换请求会被服务端判成 250013
    （请求没被认）；force-stop 重启后同样的请求立刻变成 300001（真进窗口）。
    所以**开火前必须冷启动**。
    """
    root(f"am force-stop {config.PKG}")
    time.sleep(3)
    root(f'am start -a android.intent.action.VIEW -d "{config.DEEPLINK}"')
    time.sleep(config.COLD_START_WAIT_S)


def open_activity() -> None:
    """不杀进程，直接把活动页拉起来（deeplink）。

    必须先唤醒 + 收通知栏 —— 通知栏拉下来时前台是 NotificationShade，
    deeplink 会被投给后台实例、页面打不开（踩过）。
    """
    wake()
    root(f'am start -a android.intent.action.VIEW -d "{config.DEEPLINK}"')
    time.sleep(2)
    root("cmd statusbar collapse")


def bring_pdd_front() -> None:
    """HOME + monkey 拉回前台（不重载 WebView，sniper 能活下来）。"""
    root("input keyevent KEYCODE_HOME")
    time.sleep(0.6)
    root(f"monkey -p {config.PKG} -c android.intent.category.LAUNCHER 1")
    time.sleep(3)


def ensure_foreground(retries: int = 3, gap: float = 5.0) -> bool:
    for _ in range(retries):
        if pdd_foreground():
            return True
        bring_pdd_front()
        time.sleep(gap)
    return pdd_foreground()


# ---------------------------------------------------------------- UI dump
def dump_ui(remote: str = "/sdcard/pddgrab_ui.xml", retries: int = 3) -> str:
    """dump 当前界面 XML（失败会返回空串）。

    注意：dump 在活动页上可能很慢（实测 2~30 秒），且**滚动之后 WebView 常常
    不再暴露 bounds**（整页变 [0,0][0,0]）—— 所以抢兑主流程不依赖它。
    """
    for _ in range(retries):
        root(f"rm -f {remote}")
        root(f"uiautomator dump {remote}")
        out = root(f"cat {remote} 2>/dev/null")
        if "<hierarchy" in out:
            return out
        time.sleep(0.5)
    return ""


def text_nodes(xml: str, skip_empty: bool = True):
    """从 dump XML 里抽出 (text, x1, y1, x2, y2)，坐标是设备像素。"""
    out = []
    for m in re.finditer(r'text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        t = m.group(1)
        if skip_empty and not t:
            continue
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(2, 6))
        if x2 <= x1 or y2 <= y1:      # 滚出视口的节点高度为负，丢掉
            continue
        out.append((t, x1, y1, x2, y2))
    return out


def find_text(xml: str, needle: str):
    """按文字找节点，返回 (text, cx, cy) 或 None。"""
    for t, x1, y1, x2, y2 in text_nodes(xml):
        if needle in t:
            return t, (x1 + x2) // 2, (y1 + y2) // 2
    return None


def tap(x: int, y: int) -> None:
    root(f"input tap {x} {y}")


def screenshot(remote: str = "/sdcard/pddgrab_shot.png") -> bytes:
    root(f"screencap -p {remote}")
    p = subprocess.run([shutil.which("adb") or "adb", "-s", serial(),
                        "exec-out", f"cat {remote}"],
                       capture_output=True, timeout=60)
    return p.stdout


def status() -> dict:
    """**一次 adb 调用**取回全部状态（原来是 4~5 次 shell 来回，慢）。

    返回 {screen, pid, fg, stay_on, timeout, mem_free_mb}
    """
    # 注意：整段是「在设备上跑的 shell」，用双引号包 grep 的模式，避免嵌套引号地狱
    script = (
        'dumpsys power 2>/dev/null | grep -o "mWakefulness=[A-Za-z]*" | head -1; '
        f'echo "PID=$(pidof {config.PKG} 2>/dev/null)"; '
        'dumpsys window 2>/dev/null | grep -o "mCurrentFocus=Window{[^}]*}" | head -1; '
        'echo "STAYON=$(settings get global stay_on_while_plugged_in)"; '
        'echo "TIMEOUT=$(settings get system screen_off_timeout)"; '
        'echo "MEM=$(free -m | sed -n 2p | awk \'{print $4}\')"'
    )
    out = root(script, timeout=25)
    d = {"screen": "", "pid": "", "fg": "", "stay_on": "", "timeout": "",
         "mem_free_mb": None}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("mWakefulness="):
            d["screen"] = line.split("=", 1)[1]
        elif line.startswith("PID="):
            d["pid"] = line[4:].strip()
        elif line.startswith("STAYON="):
            d["stay_on"] = line[7:].strip()
        elif line.startswith("TIMEOUT="):
            d["timeout"] = line[8:].strip()
        elif line.startswith("MEM="):
            try:
                d["mem_free_mb"] = int(line[4:].strip())
            except ValueError:
                d["mem_free_mb"] = None
        elif "mCurrentFocus=Window{" in line:
            m = re.search(r"u0 (\S+?)[/}]", line)
            d["fg"] = m.group(1) if m else line[:60]
    return d


def status_line() -> str:
    """一行摘要，给日志用。"""
    d = status()
    pid = d["pid"] or "无"
    return (f"屏幕={d['screen'] or '?'} PDD={pid} 前台={d['fg'][:40] or '?'} "
            f"mem={d['mem_free_mb']}MB")


def sleep_screen_now() -> None:
    """立刻息屏（撤常亮 + 关屏），不要等息屏超时。"""
    allow_sleep()


def mem_free_mb():
    out = root("free -m | head -2 | tail -1")
    m = re.search(r"\s(\d+)\s*$", out.strip())
    return int(m.group(1)) if m else None


def drop_caches() -> None:
    root("sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null")
