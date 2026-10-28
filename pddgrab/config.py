"""config — 集中配置（换活动 / 换商品只改这里或走命令行参数）

所有「会随活动变化」的值都放在这里，代码里不要再写死。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 手机
PKG = "com.xunmeng.pinduoduo"
# 2026-10-27：USB 调试口会反复瞬断，改用无线 adb；用环境变量覆盖即可
ADB_SERIAL = os.environ.get("PDD_ADB_SERIAL", "8a316fdb")
FRIDA_HOST = os.environ.get("PDD_FRIDA_HOST", "127.0.0.1:28080")
FRIDA_PORT = int(os.environ.get("PDD_FRIDA_PORT", "28080"))
FRIDA_BIN = os.environ.get("PDD_FRIDA_BIN", "/data/local/tmp/.x99")

# ---------------------------------------------------------------- 活动
# ⚠️ 拼多多同时开多个 attendance.html：
#     id=197270 —— 「首页搜索ddd可以找到本活动」，只有整点抢券时间条
#     id=195846 —— 「减减卡兑好礼」，**实物兑换列表在这里**（抢兑要认这个）
#   只按 "attendance" 匹配会选错页，选错的那个在后台 → XHR 回调被冻 →
#   sniper 永远卡在 syncing（2026-10-27 踩过）。
ACT_ID = os.environ.get("PDD_ACT_ID", "195846")
ACT_URL = f"https://m.pinduoduo.net/attendance.html?type=1&id={ACT_ID}&_pdd_fs=1"
DEEPLINK = ("pinduoduo://com.xunmeng.pinduoduo/web?url="
            + ACT_URL.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F")
                     .replace("=", "%3D").replace("&", "%26"))
# 页面 URL 里用来识别「是不是活动页」的子串
TARGET_URL_HINT = os.environ.get("PDD_URL_HINT", "attendance")
# 心跳页特征（会报 visible 但异步永不回调，必须硬排除）
HEARTBEAT_HINTS = ("heart_beat", "sjs_blank")

# 实物兑换的 scene key
SCENE_GOODS = "BIDDING_GOODS_EXCHANGE"
# 券的 scene（不限时间，随时可兑 —— 可用来做「链路是否通」的对照实验）
SCENE_COUPON_PERCENT = "PERCENT_COUPON_EXCHANGE"

# ---------------------------------------------------------------- 开火参数
# 2026-10-27 实盘得出（细节见 README 的「错误码判据」）：
#   * 并发齐射会被限流（250017）→ 必须用「串行链」（收到响应再发下一发）
#   * 串行链实测 11.6 发/秒、零限流（RTT ~90ms）
#   * 首发提前到 T-1s：实测库存是在 T+900ms 左右才放出，连续发才能兜住
DEFAULT_ATTEMPTS = int(os.environ.get("PDD_ATTEMPTS", "120"))
DEFAULT_GAP_MS = int(os.environ.get("PDD_GAP_MS", "0"))
DEFAULT_FROM_MS = int(os.environ.get("PDD_FROM_MS", "-1000"))
DEFAULT_KEYWORD = os.environ.get("PDD_KEYWORD", "")

# 武装时若「最近场次」还有这么久才开，认为选错场，拒绝武装
MAX_ARM_WAIT_MIN = int(os.environ.get("PDD_MAX_WAIT_MIN", "120"))

# 冷启动后等页面就绪的时间
COLD_START_WAIT_S = int(os.environ.get("PDD_COLD_START_WAIT", "14"))
# 提前多久开始武装（秒）
ARM_LEAD_S = int(os.environ.get("PDD_ARM_LEAD", "220"))

# ---------------------------------------------------------------- 路径
PKG_DIR = Path(__file__).resolve().parent
SNIPER_JS = PKG_DIR / "sniper.js"
GUARD_SH = PKG_DIR / "guard.sh"
# 设备上放 guard 的位置
GUARD_REMOTE = os.environ.get("PDD_GUARD_REMOTE", "/data/local/tmp/pdd_guard.sh")
GUARD_LOG_REMOTE = os.environ.get("PDD_GUARD_LOG", "/data/local/tmp/pdd_guard.log")
# 本机日志目录
LOG_DIR = Path(os.environ.get("PDD_LOG_DIR", str(PKG_DIR.parent / "logs")))

# ---------------------------------------------------------------- 错误码
# 2026-10-27 实测定稿
ERR = {
    0: "成功",
    250013: "不在兑换窗口 / 请求未被认（**需要冷启动刷新页面**）",
    250017: "请求过于频繁（**并发齐射被限流**；改串行链）",
    300001: "在窗口内但没抢到（真竞争，提高密度）",
    300002: "未到兑换时间",
    250005: "未到兑换时间",
    250009: "慢了一步没抢到",
    250003: "今日兑换已达上限",
    250004: "今日兑换已达上限",
    250006: "活动太火爆",
    250010: "减减卡不够",
    250014: "活动太火爆",
    40003: "错误的请求参数（参数不全）",
}


def err_text(code) -> str:
    """把错误码翻译成中文（带排错提示）。"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return str(code)
    return ERR.get(c, f"未知错误码 {c}")
