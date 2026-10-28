"""pddgrab — 拼多多「减减卡兑好礼」抢兑工具包（可复用任意商品）

模块划分
--------
config    配置常量（活动 id、默认参数、路径）
device    手机侧操作（adb：唤醒/解锁/冷启动/前台/息屏/保活/dump）
webview   frida ↔ 拼多多 mecox WebView 的 JS 桥（含选页加固）
panel     活动接口封装（场次/地址/校验/兑换/余额）
slots     场次与商品解析（按关键词或 award_id 选目标）
sniper    页面内狙击手的安装/武装/状态
grabber   一次抢兑的完整编排（冷启动 → 武装 → 盯 → 结果）
monitor   设备状态采样监控
cli       命令行入口

典型用法
--------
    from pddgrab import grabber, slots

    # 抢今天最近的「汰渍」场
    res = grabber.run(keyword="汰渍")
    print(res)

    # 只做只读自检
    print(slots.describe("汰渍"))

命令行
------
    python -m pddgrab.cli list                    列出今天未结束的场次与商品
    python -m pddgrab.cli probe --item 汰渍        只读自检（不武装、不开火）
    python -m pddgrab.cli arm   --item 汰渍        武装（等到场次自动开火）
    python -m pddgrab.cli fire  --item 汰渍        立刻按一轮流程打（调试）
    python -m pddgrab.cli monitor --until 22:05    设备状态监控
"""

__version__ = "1.0.0"

from . import config, device, slots, webview  # noqa: F401

__all__ = ["config", "device", "webview", "panel", "slots", "sniper",
           "grabber", "monitor", "cli", "__version__"]
