"""slots — 场次与商品解析（换商品只改 keyword / award_id）

关键语义（2026-10-27 实盘确认）：
  * `start_time` / `end_time` 有时是秒、有时是毫秒 → 统一归一化。
  * **同一个 award_id 在每个场次都出现**（服务端按时间判断场次，不靠 award_id），
    所以「选哪个场次」必须靠 `start_time`。
  * 选场规则：**只认 `start_time > now` 的最近一场**。
    旧逻辑用 `end_time > now` 会把「今天 22:00 那场（跑到明天 00:00）」当成候选，
    于是对着一个早就开始的场次开火（踩过）。
  * `status`：0=可兑换 1=已抢光 2=待开始 11=已兑换（成功）
"""
from __future__ import annotations

import datetime as _dt
import time

from . import config

STATUS_TEXT = {0: "可兑换", 1: "已抢光", 2: "待开始", 11: "已兑换"}


# ---------------------------------------------------------------- 时间工具
def to_ms(v) -> int:
    """秒/毫秒统一成毫秒。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 10 ** 11 else n * 1000


def ts_str(ms: int, fmt: str = "%m-%d %H:%M:%S") -> str:
    try:
        return time.strftime(fmt, time.localtime(ms / 1000))
    except Exception:  # noqa: BLE001
        return f"?({ms})"


def parse_epoch(hhmm: str, day_offset: int = 0) -> int:
    """把 'HH:MM[:SS]' 解析成「今天（或 +N 天）那个时刻」的 epoch 秒。"""
    parts = [int(x) for x in hhmm.split(":")]
    while len(parts) < 3:
        parts.append(0)
    now = _dt.datetime.now()
    t = now.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)
    if day_offset:
        t += _dt.timedelta(days=day_offset)
    return int(t.timestamp())


def seconds_until(hhmm: str) -> int:
    """距下一个 'HH:MM' 还有多少秒（已过则算到明天）。"""
    target = parse_epoch(hhmm)
    if target <= time.time():
        target += 86400
    return int(target - time.time())


# ---------------------------------------------------------------- 解析
def all_slots(result) -> list[dict]:
    """把 query_time_awards 的 result 拍平成场次列表（含商品）。"""
    out = []
    for sc in (result or {}).get("exchange_scene_list") or []:
        if sc.get("scene_key") != config.SCENE_GOODS:
            continue
        for sl in sc.get("time_award_stock_result_list") or []:
            out.append({
                "label": sl.get("current_times"),
                "start_ms": to_ms(sl.get("start_time")),
                "end_ms": to_ms(sl.get("end_time")),
                "awards": sl.get("awards") or [],
            })
    out.sort(key=lambda s: s["start_ms"])
    return out


def upcoming(result, now_ms: int | None = None) -> list[dict]:
    """还没结束的场次。"""
    now_ms = now_ms or int(time.time() * 1000)
    return [s for s in all_slots(result) if s["end_ms"] > now_ms]


def find_item(slot: dict, keyword: str | None = None, award_id=None):
    """在场次里找商品：优先 award_id，其次 goods_name 含 keyword。"""
    for a in slot.get("awards") or []:
        if award_id is not None and int(a.get("award_id") or 0) == int(award_id):
            return a
    if not keyword:
        return None
    for a in slot.get("awards") or []:
        name = ((a.get("award_info") or {}).get("goods_name") or "")
        if keyword in name:
            return a
    return None


def pick_slot(result, keyword: str | None = None, award_id=None,
              now_ms: int | None = None, prefer_future: bool = True):
    """挑出要开火的场次 + 商品。

    返回 dict: {slot, item, start_ms, end_ms, label, name, consume, status, award_id}
    找不到返回 None。
    """
    now_ms = now_ms or int(time.time() * 1000)
    best = None
    for s in all_slots(result):
        if s["end_ms"] <= now_ms:
            continue
        if prefer_future and s["start_ms"] <= now_ms:
            # 已经开始的场次不能开火（旧 bug：用 end_time 判断会选中它）
            continue
        it = find_item(s, keyword, award_id)
        if not it:
            continue
        if best is None or s["start_ms"] < best["start_ms"]:
            info = it.get("award_info") or {}
            best = {
                "slot": s,
                "item": it,
                "start_ms": s["start_ms"],
                "end_ms": s["end_ms"],
                "label": s["label"],
                "name": info.get("goods_name") or "",
                "consume": it.get("consume_card"),
                "status": it.get("status"),
                "award_id": it.get("award_id"),
            }
    return best


def describe(result, keyword: str | None = None, now_ms: int | None = None) -> str:
    """生成人读的场次/商品清单（CLI 用）。"""
    now_ms = now_ms or int(time.time() * 1000)
    lines = []
    ups = upcoming(result, now_ms)
    if not ups:
        return "（没有未结束的场次）"
    for s in ups:
        started = s["start_ms"] <= now_ms
        tag = "进行中" if started else f"还有 {int((s['start_ms']-now_ms)/60000)} 分钟"
        lines.append(f"\n=== 场次 {s['label']}  {ts_str(s['start_ms'])} ~ "
                     f"{ts_str(s['end_ms'], '%H:%M')}  [{tag}] ===")
        for a in s["awards"]:
            info = a.get("award_info") or {}
            name = info.get("goods_name") or "(无名)"
            st = STATUS_TEXT.get(a.get("status"), a.get("status"))
            mark = ""
            if keyword and keyword in name:
                mark = "   <<< 目标"
            lines.append(f"   {name[:34]:<36} 需 {a.get('consume_card'):>4} 卡  "
                         f"{st}  award_id={a.get('award_id')}{mark}")
    return "\n".join(lines)


def coupon_awards(result):
    """返回可兑换的券列表（用于「链路是否通」的对照实验，1 卡一张）。"""
    out = []
    for sc in (result or {}).get("exchange_scene_list") or []:
        if sc.get("scene_key") != config.SCENE_COUPON_PERCENT:
            continue
        for sl in sc.get("time_award_stock_result_list") or []:
            for a in sl.get("awards") or []:
                if a.get("status") == 0:
                    out.append(a)
    return out
