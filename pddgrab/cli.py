"""cli — 统一命令行入口

    python -m pddgrab.cli list                   列出未结束的场次与商品
    python -m pddgrab.cli probe  --item 汰渍      只读自检（不武装、不开火）
    python -m pddgrab.cli arm    --item 汰渍      武装（sniper 自己等到点开火）
    python -m pddgrab.cli fire   --item 汰渍      完整跑一次（冷启动→武装→盯→结果）
    python -m pddgrab.cli fire   --item 汰渍 --at 22:00 --attempts 120 --gap 0
    python -m pddgrab.cli coupon                  兑一张 1 卡券（**花 1 张卡**，验证链路）
    python -m pddgrab.cli monitor --until 22:05   设备状态监控
    python -m pddgrab.cli guard --wake 21:55 --sleep 22:05   部署设备端守护
    python -m pddgrab.cli env                     打印当前配置
"""
from __future__ import annotations

import argparse
import sys
import time

from . import config, device, grabber, monitor as monitor_mod, panel
from . import slots as slots_mod
from .webview import Bridge


def _mk_log(name: str) -> str:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.LOG_DIR / f"{name}_{time.strftime('%m%d_%H%M%S')}.log")


# ---------------------------------------------------------------- 子命令
def cmd_env(a):
    print(f"adb serial   : {device.serial()}   （候选: {config.ADB_SERIAL} / 无线）")
    print(f"frida host   : {config.FRIDA_HOST}")
    print(f"活动 id      : {config.ACT_ID}   （实物兑换在 195846「减减卡兑好礼」）")
    print(f"活动 URL     : {config.ACT_URL}")
    print(f"开火参数     : attempts={config.DEFAULT_ATTEMPTS} gap={config.DEFAULT_GAP_MS}ms "
          f"from={config.DEFAULT_FROM_MS}ms")
    print(f"日志目录     : {config.LOG_DIR}")
    print(f"adb 连通     : {device.connected()}")
    print(f"PDD 进程     : {device.pid() or '未运行'}")
    print(f"屏幕         : {'亮' if device.screen_on() else '灭'}")
    print(f"frida-server : {'OK' if device.frida_alive() else '未监听'}")
    return 0


def cmd_status(a):
    """一行看设备状态（一次 adb 调用，快）。"""
    print(device.status_line())
    return 0


def cmd_list(a):
    if not device.start_frida():
        print("frida 起不来")
        return 1
    bridge = Bridge().connect()
    try:
        t = bridge.ensure_page()
        if t is None:
            print('活动页打不开（deeplink 无效？）')
            return 1
        res = panel.query_time_awards(bridge, t)
        if not res:
            print("拿不到场次表（页面 XHR 回调被冻？试试先冷启动）")
            return 1
        print(slots_mod.describe(res, keyword=a.item))
        print(f"\n页面余额: {panel.balance(bridge, t)}")
    finally:
        bridge.close()
    return 0


def cmd_probe(a):
    out = grabber.probe(item=a.item, award_id=a.award_id, log_path=a.log or _mk_log("probe"))
    return 0 if not out.get("error") else 1


def cmd_arm(a):
    r = grabber.arm_only(item=a.item, award_id=a.award_id, at=a.at,
                         attempts=a.attempts, gap_ms=a.gap, from_ms=a.frm,
                         cold_start=not a.no_cold_start,
                         log_path=a.log or _mk_log("arm"))
    print(r)
    return 0 if r.get("ok") else 1


def cmd_fire(a):
    res = grabber.run(item=a.item, award_id=a.award_id, at=a.at,
                      attempts=a.attempts, gap_ms=a.gap, from_ms=a.frm,
                      cold_start=not a.no_cold_start,
                      until_after_s=a.until_after,
                      log_path=a.log or _mk_log("fire"))
    print(res.summary())
    return 0 if res.ok else 1


def cmd_coupon(a):
    """兑一张 1 卡券 —— 用来验证「请求链路是否通」。⚠️ 会花掉 1 张卡。"""
    if not a.yes:
        print("这会真的兑一张券、花掉 1 张减减卡。确认请加 --yes")
        return 2
    if not device.start_frida():
        print("frida 起不来")
        return 1
    bridge = Bridge().connect()
    try:
        t = bridge.ensure_page()
        if t is None:
            print('活动页打不开（deeplink 无效？）')
            return 1
        res = panel.query_time_awards(bridge, t)
        coupons = slots_mod.coupon_awards(res) if res else []
        if not coupons:
            print("没有可兑的券")
            return 1
        c = coupons[0]
        addr = panel.default_address(bridge, t)
        print(f"兑券 award_id={c.get('award_id')} 需 {c.get('consume_card')} 卡")
        before = panel.balance(bridge, t)
        code, raw = panel.exchange(bridge, t, c.get("award_id"), addr)
        time.sleep(2)
        after = panel.balance(bridge, t)
        print(f"error_code = {code}  ({config.err_text(code)})")
        print(f"raw        = {raw}")
        print(f"余额       = {before} -> {after}（页面不刷新时会显示旧值）")
        return 0 if str(code) == "0" else 1
    finally:
        bridge.close()


def cmd_monitor(a):
    hot_from = hot_to = None
    if a.at:
        base = slots_mod.parse_epoch(a.at) * 1000
        hot_from, hot_to = base - 40000, base + 90000
    m = monitor_mod.Monitor(log_path=a.log or _mk_log("monitor"),
                            interval=a.interval, hot_interval=a.hot_interval,
                            hot_from_ms=hot_from, hot_to_ms=hot_to,
                            watch_sniper=a.sniper)
    m.run(until_hhmm=a.until, seconds=a.seconds)
    return 0


def cmd_guard(a):
    """把 guard.sh 推到设备并启动（到点冷启动 + 守前台 + 抢完息屏）。"""
    wake = slots_mod.parse_epoch(a.wake)
    sleep_at = slots_mod.parse_epoch(a.sleep)
    if sleep_at <= wake:
        sleep_at += 86400
    sh = config.GUARD_SH.read_text(encoding="utf-8").replace("\r\n", "\n")
    tmp = config.LOG_DIR / "guard_push.sh"
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(sh, encoding="utf-8", newline="\n")
    device._adb("push", str(tmp), config.GUARD_REMOTE)
    device.root(f"chmod 755 {config.GUARD_REMOTE}")
    device.root(f"pkill -9 -f {config.GUARD_REMOTE.split('/')[-1]} 2>/dev/null")
    time.sleep(1)
    cmdline = (f"sh {config.GUARD_REMOTE} {wake} {sleep_at} "
               f"'{config.DEEPLINK}' {config.PKG} {config.GUARD_LOG_REMOTE}")
    device.root(f"(nohup setsid {cmdline} </dev/null >/dev/null 2>&1 &)")
    time.sleep(4)
    out = device.root(f"ps -A -o PID,ARGS 2>/dev/null | grep -a {config.GUARD_REMOTE.split('/')[-1]} | grep -v grep")
    log = device.root(f"tail -2 {config.GUARD_LOG_REMOTE} 2>/dev/null")
    print(f"唤醒 {slots_mod.ts_str(wake*1000, '%m-%d %H:%M:%S')}  "
          f"息屏 {slots_mod.ts_str(sleep_at*1000, '%m-%d %H:%M:%S')}")
    print(f"guard 进程:\n{out.strip()}")
    print(f"guard 日志:\n{log.strip()}")
    return 0 if "guard.sh" in out else 1


def cmd_stop_guard(a):
    name = config.GUARD_REMOTE.split("/")[-1]
    device.root(f"pkill -9 -f {name}")
    print("已停 guard")
    return 0


# ---------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pddgrab", description="拼多多减减卡抢兑")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_item(sp):
        sp.add_argument("--item", "-i", default=config.DEFAULT_KEYWORD or None,
                        help="商品关键词，如 汰渍 / 可口可乐 / 薯片")
        sp.add_argument("--award-id", type=int, default=None, help="直接指定奖品 id")
        sp.add_argument("--log", default=None, help="日志文件路径")

    sp = sub.add_parser("env", help="打印当前配置")
    sp.set_defaults(func=cmd_env)

    sp = sub.add_parser("status", help="一行看设备状态（快）")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("list", help="列出未结束的场次与商品")
    add_item(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("probe", help="只读自检")
    add_item(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("arm", help="武装（不盯）")
    add_item(sp)
    sp.add_argument("--at", default=None, help="开火时刻 HH:MM（默认取最近场次）")
    sp.add_argument("--attempts", type=int, default=config.DEFAULT_ATTEMPTS)
    sp.add_argument("--gap", type=int, default=config.DEFAULT_GAP_MS)
    sp.add_argument("--from-ms", dest="frm", type=int, default=config.DEFAULT_FROM_MS)
    sp.add_argument("--no-cold-start", action="store_true", help="跳过冷启动（不推荐）")
    sp.set_defaults(func=cmd_arm)

    sp = sub.add_parser("fire", help="完整跑一次")
    add_item(sp)
    sp.add_argument("--at", default=None, help="开火时刻 HH:MM（默认取最近场次）")
    sp.add_argument("--attempts", type=int, default=config.DEFAULT_ATTEMPTS)
    sp.add_argument("--gap", type=int, default=config.DEFAULT_GAP_MS,
                    help="两发间隔 ms；0=串行链（推荐）")
    sp.add_argument("--from-ms", dest="frm", type=int, default=config.DEFAULT_FROM_MS,
                    help="首发相对场次开始的偏移，默认 -1000")
    sp.add_argument("--until-after", type=int, default=200, help="开火后再盯多少秒")
    sp.add_argument("--no-cold-start", action="store_true")
    sp.set_defaults(func=cmd_fire)

    sp = sub.add_parser("coupon", help="兑一张 1 卡券（花 1 张卡，验证链路）")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_coupon)

    sp = sub.add_parser("monitor", help="设备状态监控")
    sp.add_argument("--until", default=None, help="盯到 HH:MM")
    sp.add_argument("--seconds", type=int, default=None)
    sp.add_argument("--at", default=None, help="开火时刻 HH:MM（用于窗口内加密采样）")
    sp.add_argument("--interval", type=float, default=30.0)
    sp.add_argument("--hot-interval", type=float, default=10.0)
    sp.add_argument("--sniper", action="store_true", help="同时采样 sniper 状态")
    sp.add_argument("--log", default=None)
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("guard", help="部署设备端守护（冷启动+守前台+息屏）")
    sp.add_argument("--wake", required=True, help="唤醒时刻 HH:MM")
    sp.add_argument("--sleep", required=True, help="息屏时刻 HH:MM")
    sp.set_defaults(func=cmd_guard)

    sp = sub.add_parser("stop-guard", help="停掉设备端守护")
    sp.set_defaults(func=cmd_stop_guard)

    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
