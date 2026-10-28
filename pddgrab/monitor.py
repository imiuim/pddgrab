"""monitor — 设备状态采样监控（用于事后复盘「开火那一刻设备在干什么」）

采样内容：时间 / 屏幕 / 前台 / PDD 进程 / sniper 状态（可选）
开火窗口内自动加密采样（默认 10 秒，其余 30 秒）。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config, device, slots as slots_mod, sniper
from .webview import Bridge


class Monitor:
    def __init__(self, log_path=None, interval: float = 30.0,
                 hot_interval: float = 10.0, hot_from_ms: int | None = None,
                 hot_to_ms: int | None = None, watch_sniper: bool = False):
        self.path = Path(log_path) if log_path else (config.LOG_DIR / "monitor.log")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.interval = interval
        self.hot_interval = hot_interval
        self.hot_from_ms = hot_from_ms
        self.hot_to_ms = hot_to_ms
        self.watch_sniper = watch_sniper
        self._bridge = None

    def say(self, msg: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg, flush=True)

    def _sniper_status(self):
        if not self.watch_sniper:
            return None
        try:
            if self._bridge is None:
                self._bridge = Bridge().connect(retries=1)
            t = self._bridge.resolve_target()
            return sniper.status(self._bridge, t)
        except Exception:  # noqa: BLE001
            try:
                self._bridge.close()
            except Exception:  # noqa: BLE001
                pass
            self._bridge = None
            return None

    def run(self, until_ms: int | None = None, until_hhmm: str | None = None,
            seconds: int | None = None):
        if until_hhmm:
            until_ms = slots_mod.parse_epoch(until_hhmm) * 1000
        if until_ms is None and seconds is None:
            raise ValueError("需要 until_ms / until_hhmm / seconds 之一")

        self.say(f"=== 监控启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        self.say(f"采样 {self.interval:.0f}s（窗口内 {self.hot_interval:.0f}s）；"
                 f"日志 {self.path}")
        if self.hot_from_ms:
            self.say(f"窗口: {slots_mod.ts_str(self.hot_from_ms)} ~ "
                     f"{slots_mod.ts_str(self.hot_to_ms)}")

        end = until_ms / 1000 if until_ms else time.time() + (seconds or 0)
        while time.time() < end:
            now = time.time()
            w = "Awake" if device.screen_on() else "Asleep"
            pid = device.pid() or "无"
            fg = device.foreground()[:44]
            line = f"[{time.strftime('%H:%M:%S')}] 屏幕={w} PDD={pid} 前台={fg}"
            if self.hot_from_ms and self.hot_from_ms <= now * 1000 <= (self.hot_to_ms or 0):
                line += "   <<< 开火窗口"
            self.say(line)
            st = self._sniper_status()
            if st is not None:
                self.say(f"          sniper: state={st.get('state')} "
                         f"attempts={st.get('attempts')} codes={sniper.codes(st)}")
            if self.hot_from_ms and self.hot_from_ms <= now * 1000 <= (self.hot_to_ms or 0):
                time.sleep(self.hot_interval)
            else:
                time.sleep(self.interval)

        self.say(f"=== 监控结束 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        try:
            if self._bridge:
                self._bridge.close()
        except Exception:  # noqa: BLE001
            pass
