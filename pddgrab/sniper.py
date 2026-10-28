"""sniper — 页面内狙击手的安装 / 武装 / 状态读取

`sniper.js` 注入到活动页的 JS 上下文里自己完成：
    对时(_stm) → 找场次+奖品 → 找默认地址 → 自旋等到开火时刻 → 串行链连打

武装参数（`ArmConfig`）：
    keyword      商品关键词（如「汰渍」），与 award_id 二选一
    award_id     直接指定奖品 id（更精确）
    fire_at_ms   开火时刻（毫秒 epoch）；None = 由页面自己按场次算
    attempts     最多打几发（默认 120）
    gap_ms       两发之间的间隔，**0 = 收到响应立刻发下一发（串行链）**
                 ⚠️ 不要设成 <800ms 的「并发」打法，会被限流（250017）
    from_ms      首发相对场次开始的偏移，默认 -1000（提前 1 秒）
    dry_run      True = 只对时+找奖品+找地址，不开火

错误码判据见 config.ERR；核心：
    250013 = 请求没被认（**先冷启动刷新页面**）
    250017 = 并发被限流（**改串行链 gap=0**）
    300001 = 真进窗口但没抢到（提高密度）
    0      = 成功
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from . import config
from .webview import unwrap

_INSTALL_JS = None


def _install_js() -> str:
    global _INSTALL_JS
    if _INSTALL_JS is None:
        _INSTALL_JS = config.SNIPER_JS.read_text(encoding="utf-8")
    return _INSTALL_JS


@dataclass
class ArmConfig:
    keyword: str | None = None
    award_id: int | None = None
    fire_at_ms: int | None = None
    attempts: int = config.DEFAULT_ATTEMPTS
    gap_ms: int = config.DEFAULT_GAP_MS
    from_ms: int = config.DEFAULT_FROM_MS
    dry_run: bool = False
    max_wait_min: int = config.MAX_ARM_WAIT_MIN

    def to_js(self) -> str:
        cfg = {
            "keyword": self.keyword or "",
            "awardId": self.award_id,
            "maxAttempts": self.attempts,
            "gapMs": self.gap_ms,
            "leadMs": 0,
            "fireAtMs": self.fire_at_ms,
            "burstOffsetsMs": None,
            "burstCount": 1,
            "burstFromMs": self.from_ms,
            "burstToMs": 0,
            "dryRun": bool(self.dry_run),
            "maxWaitMin": self.max_wait_min,
        }
        return json.dumps(cfg, ensure_ascii=False)


# ---------------------------------------------------------------- 操作
def install(bridge, sel) -> str:
    return str(bridge.evaljs(sel, _install_js()))


def arm(bridge, sel, cfg: ArmConfig) -> str:
    js = ("(typeof window.__sniper==='undefined') ? 'GONE' "
          ": window.__sniper.arm(%s)" % cfg.to_js())
    return str(bridge.evaljs(sel, js))


def disarm(bridge, sel) -> str:
    js = ("(typeof window.__sniper==='undefined') ? 'GONE' "
          ": window.__sniper.disarm()")
    return str(bridge.evaljs(sel, js))


def status(bridge, sel):
    js = ("(typeof window.__sniper==='undefined') ? 'GONE' "
          ": JSON.stringify(window.__sniper.status())")
    v = unwrap(bridge.evaljs(sel, js))
    if v in ("GONE", None):
        return None
    return v if isinstance(v, dict) else {"raw": v}


def install_and_arm(bridge, sel, cfg: ArmConfig):
    return install(bridge, sel), arm(bridge, sel, cfg)


# ---------------------------------------------------------------- 解析
def shots(st: dict) -> list[dict]:
    return [e["shot"] for e in (st or {}).get("log") or [] if e.get("ev") == "shot"]


def codes(st: dict) -> dict:
    from collections import Counter
    return dict(Counter(str(s.get("ec")) for s in shots(st)))


def won(st: dict) -> bool:
    res = (st or {}).get("result") or {}
    if res.get("ok"):
        return True
    return any(str(s.get("ec")) == "0" for s in shots(st))


def summarize(st: dict, balance=None) -> str:
    """生成人读结果摘要（CLI / 日志用）。"""
    if not st:
        return "sniper 不存在（页面可能重载过）"
    lines = []
    res = st.get("result") or {}
    sl = st.get("slot") or {}
    ok = won(st)
    lines.append("=" * 62)
    lines.append("抢兑结果：" + ("✅ 成功" if ok else "❌ 未成功"))
    lines.append("=" * 62)
    lines.append(f"state    : {st.get('state')}   attempts: {st.get('attempts')}   "
                 f"err: {st.get('err')}")
    lines.append(f"目标     : {sl.get('name')}  (award_id={st.get('awardId')}, "
                 f"场次 {sl.get('slotLabel')}, 需 {sl.get('consume')} 卡)")
    lines.append(f"对时偏移 : {st.get('offset')}ms   rtt: {st.get('rtt')}ms")
    if res:
        lines.append(f"result   : {json.dumps(res, ensure_ascii=False)[:220]}")
    sh = shots(st)
    lines.append(f"射击     : {len(sh)} 发   错误码分布: {codes(st)}")
    for s in sh:
        ec = str(s.get("ec"))
        mark = (" ★★★ 成功" if ec == "0" else
                " ★真进窗口" if ec == "300001" else
                " !限流(并发)" if ec == "250017" else
                " ·请求未被认(需冷启动)" if ec == "250013" else "")
        lines.append(f"   #{s.get('n')} drift={s.get('drift')}ms rtt={s.get('rtt')}ms "
                     f"ec={ec}{mark}")
    if balance is not None:
        lines.append(f"余额     : {balance}")
    return "\n".join(lines)
