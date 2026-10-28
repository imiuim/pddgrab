"""grabber — 一次抢兑的完整编排（换商品只改 keyword/award_id）

流程（每一步的顺序都是有原因的，别乱调）：
    1. 确保 frida 在跑、页面可达
    2. **冷启动** PDD 刷新会话   ← 不刷新的话请求会被判 250013（没被认）
    3. 等「减减卡兑好礼」页可见（id=195846，不是 197270）
    4. 读场次 → 选目标场次+商品（只认 start_time > now 的最近一场）
    5. 读默认收货地址
    6. 安装 + 武装 sniper（fire_at_ms = 场次开始时间）
    7. 盯到 done，逐发记录错误码
    8. 汇总结果（成功判据：任一发 ec=0 / result.ok）
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import config, device, panel, slots as slots_mod, sniper
from .webview import Bridge


# ---------------------------------------------------------------- 日志
class Log:
    def __init__(self, path=None, echo=True):
        self.echo = echo
        self.path = path
        if path:
            from pathlib import Path
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text("", encoding="utf-8")

    def __call__(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if self.echo:
            print(line, flush=True)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------- 结果
@dataclass
class GrabResult:
    ok: bool = False
    item: str = ""
    award_id: int | None = None
    slot_label: str = ""
    fire_at_ms: int = 0
    shots: int = 0
    codes: dict = field(default_factory=dict)
    balance_before: str = "?"
    balance_after: str = "?"
    status: dict | None = None
    log_path: str | None = None
    error: str | None = None

    def summary(self) -> str:
        if self.status:
            head = sniper.summarize(self.status, balance=self.balance_after)
        else:
            head = f"（没有 sniper 状态）error={self.error}"
        extra = (f"\n目标: {self.item} (award_id={self.award_id}, 场次 {self.slot_label}, "
                 f"开火 {slots_mod.ts_str(self.fire_at_ms)})\n"
                 f"余额: {self.balance_before} -> {self.balance_after}"
                 f"（成功应减少对应卡数）")
        if self.log_path:
            extra += f"\n日志: {self.log_path}"
        return head + extra


# ---------------------------------------------------------------- 只读自检
def probe(item: str | None = None, award_id: int | None = None,
          log_path=None) -> dict:
    """只读自检：不冷启动、不武装、不开火。用于确认链路与目标是否可抢。"""
    log = Log(log_path)
    out = {}
    log("=== probe（只读自检）===")
    log(f"adb 连通: {device.connected()}")
    if not device.start_frida():
        out["error"] = "frida-server 起不来"
        log(out["error"])
        return out
    out["frida"] = True
    log("frida-server: OK")

    bridge = Bridge().connect()
    try:
        t = bridge.resolve_target()
        log(f"选中活动页 index={t}  title={bridge.title(t)}  vis={bridge.visibility(t)}")
        out["index"] = t
        out["title"] = bridge.title(t)
        out["visible"] = "visible" in bridge.visibility(t)
        out["balance"] = panel.balance(bridge, t)
        log(f"页面余额: {out['balance']}")

        res = panel.query_time_awards(bridge, t)
        if not res:
            out["error"] = "query_time_awards 拿不到（页面 XHR 回调被冻？）"
            log(out["error"])
            return out
        log(slots_mod.describe(res, keyword=item))

        pick = slots_mod.pick_slot(res, keyword=item, award_id=award_id)
        if pick:
            out["target"] = {k: pick[k] for k in
                             ("name", "award_id", "consume", "status", "label",
                              "start_ms", "end_ms")}
            log(f"目标: {pick['name']} award_id={pick['award_id']} "
                f"需 {pick['consume']} 卡 场次 {pick['label']} "
                f"({slots_mod.ts_str(pick['start_ms'])})")
        else:
            log("没找到匹配的未开始场次")
        addr = panel.default_address(bridge, t)
        out["address"] = {k: addr.get(k) for k in
                          ("address_id", "address_snapshot_id", "province", "city")} if addr else None
        log(f"默认地址: {out['address']}")
        coupons = slots_mod.coupon_awards(res)
        out["coupon_award_id"] = coupons[0].get("award_id") if coupons else None
        log(f"可兑券（链路对照用）: award_id={out['coupon_award_id']}")
    finally:
        bridge.close()
    return out


# ---------------------------------------------------------------- 开火
def run(item: str | None = None, award_id: int | None = None,
        fire_at_ms: int | None = None, at: str | None = None,
        attempts: int = config.DEFAULT_ATTEMPTS,
        gap_ms: int = config.DEFAULT_GAP_MS,
        from_ms: int = config.DEFAULT_FROM_MS,
        cold_start: bool = True,
        until_after_s: int = 200,
        log_path=None,
        stop_when_done: bool = True) -> GrabResult:
    """完整跑一次抢兑。

    item       商品关键词（如「汰渍」「可口可乐」「薯片」）
    award_id   直接指定奖品 id（优先级高于 item）
    fire_at_ms 指定开火时刻（毫秒 epoch）；at='HH:MM' 也行
    cold_start 开火前是否 force-stop 冷启动（**强烈建议 True**）
    """
    log = Log(log_path)
    res = GrabResult(item=item or str(award_id), log_path=str(log_path) if log_path else None)

    log("=== grabber.run 开始 ===")
    log(f"参数: item={item} award_id={award_id} attempts={attempts} "
        f"gap={gap_ms}ms from={from_ms}ms cold_start={cold_start}")

    if not device.connected():
        res.error = f"设备 {config.ADB_SERIAL} 未连接"
        log(res.error)
        return res
    if not device.start_frida():
        res.error = "frida-server 起不来"
        log(res.error)
        return res

    if cold_start:
        log("冷启动 PDD（刷新会话，否则请求会被判 250013）")
        device.cold_start_pdd()

    device.keep_awake()

    bridge = Bridge().connect()
    try:
        t = bridge.wait_visible(tries=8, gap=10)
        if t is None:
            res.error = "活动页始终不可见"
            log(res.error)
            return res
        log(f"活动页 index={t} title={bridge.title(t)}")

        res.balance_before = panel.balance(bridge, t)
        log(f"余额(开跑前): {res.balance_before}")

        r = panel.query_time_awards(bridge, t)
        if not r:
            res.error = "拿不到场次表"
            log(res.error)
            return res

        pick = slots_mod.pick_slot(r, keyword=item, award_id=award_id)
        if not pick:
            # 场次可能已经开始（刚过点）→ 退回「当前进行中」的场次
            pick = slots_mod.pick_slot(r, keyword=item, award_id=award_id,
                                       prefer_future=False)
        if not pick:
            res.error = "没找到匹配的商品/场次"
            log(res.error)
            log(slots_mod.describe(r, keyword=item))
            return res

        res.item = pick["name"]
        res.award_id = pick["award_id"]
        res.slot_label = pick["label"]
        res.fire_at_ms = fire_at_ms or pick["start_ms"]
        if at:
            res.fire_at_ms = slots_mod.parse_epoch(at)
        log(f"目标: {res.item} award_id={res.award_id} 需 {pick['consume']} 卡 "
            f"场次 {res.slot_label}")
        log(f"开火时刻: {res.fire_at_ms} = {slots_mod.ts_str(res.fire_at_ms)}")

        # 余额预检
        try:
            need = int(pick["consume"] or 0)
            have = int(res.balance_before)
            if need and have < need:
                res.error = f"余额不足：{have} < {need}"
                log(res.error)
                return res
            log(f"余额预检通过：{have} >= {need}")
        except (TypeError, ValueError):
            log(f"余额预检跳过（读到 {res.balance_before!r}）")

        addr = panel.default_address(bridge, t)
        if not addr:
            res.error = "没有收货地址"
            log(res.error)
            return res
        log(f"地址: id={addr.get('address_id')} snap={addr.get('address_snapshot_id')}")

        cfg = sniper.ArmConfig(keyword=item, award_id=res.award_id,
                               fire_at_ms=res.fire_at_ms, attempts=attempts,
                               gap_ms=gap_ms, from_ms=from_ms)
        inst = sniper.install(bridge, t)
        log(f"install: {inst}")
        log(f"arm: {sniper.arm(bridge, t, cfg)}")

        # ---- 盯到 done ----
        deadline = time.time() + max(60, (res.fire_at_ms / 1000 - time.time())
                                     + until_after_s)
        last = None
        while time.time() < deadline:
            time.sleep(2)
            try:
                st = sniper.status(bridge, t)
            except Exception as e:  # noqa: BLE001
                log(f"status 读取失败: {str(e)[:80]}（页面可能重载，重装重武装）")
                try:
                    sniper.install(bridge, t)
                    sniper.arm(bridge, t, cfg)
                except Exception as e2:  # noqa: BLE001
                    log(f"rearm 失败: {str(e2)[:80]}")
                continue
            if st is None:
                log("sniper 不见了 → 重装重武装")
                try:
                    sniper.install(bridge, t)
                    sniper.arm(bridge, t, cfg)
                except Exception as e2:  # noqa: BLE001
                    log(f"rearm 失败: {str(e2)[:80]}")
                time.sleep(2)
                continue
            key = (st.get("state"), st.get("attempts"))
            if key != last:
                last = key
                log(f"state={st.get('state')} attempts={st.get('attempts')} "
                    f"inMs={st.get('inMs')} codes={sniper.codes(st)}")
            if sniper.won(st):
                log("★ 命中！")
                break
            if stop_when_done and st.get("state") == "done":
                break

        try:
            res.status = sniper.status(bridge, t)
        except Exception:  # noqa: BLE001
            res.status = None
        if res.status:
            res.shots = len(sniper.shots(res.status))
            res.codes = sniper.codes(res.status)
            res.ok = sniper.won(res.status)
        try:
            res.balance_after = panel.balance(bridge, t)
        except Exception:  # noqa: BLE001
            pass
        log(res.summary())
        return res
    finally:
        bridge.close()
        device.allow_sleep(power_off=False)


# ---------------------------------------------------------------- 后台武装
def arm_only(item: str | None = None, award_id: int | None = None,
             fire_at_ms: int | None = None, at: str | None = None,
             attempts: int = config.DEFAULT_ATTEMPTS,
             gap_ms: int = config.DEFAULT_GAP_MS,
             from_ms: int = config.DEFAULT_FROM_MS,
             cold_start: bool = True, log_path=None) -> dict:
    """只武装（不盯），sniper 会在页面里自己等到点开火。

    适合配合 `monitor` / 设备端 guard 使用（PC 可以断开）。
    """
    log = Log(log_path)
    if cold_start:
        log("冷启动 PDD")
        device.cold_start_pdd()
    if not device.start_frida():
        return {"error": "frida 起不来"}
    bridge = Bridge().connect()
    try:
        t = bridge.wait_visible(tries=8, gap=10)
        if t is None:
            return {"error": "活动页不可见"}
        r = panel.query_time_awards(bridge, t)
        pick = slots_mod.pick_slot(r, keyword=item, award_id=award_id) if r else None
        if not pick:
            return {"error": "没找到目标"}
        fa = fire_at_ms or (slots_mod.parse_epoch(at) if at else pick["start_ms"])
        cfg = sniper.ArmConfig(keyword=item, award_id=pick["award_id"],
                               fire_at_ms=fa, attempts=attempts, gap_ms=gap_ms,
                               from_ms=from_ms)
        inst = sniper.install(bridge, t)
        armed = sniper.arm(bridge, t, cfg)
        log(f"武装 {pick['name']} award_id={pick['award_id']} "
            f"开火 {slots_mod.ts_str(fa)}")
        log(f"install={inst} arm={armed}")
        return {"ok": True, "index": t, "item": pick["name"],
                "award_id": pick["award_id"], "fire_at_ms": fa,
                "install": inst, "arm": armed}
    finally:
        bridge.close()
