"""tests/test_slots.py — 场次/商品解析的离线单测（不需要手机）

跑：python -m pytest tests/ -v     或     python tests/test_slots.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pddgrab import config, slots  # noqa: E402

# ---------------------------------------------------------------- 造数据
# 时间基准：某天 12:00:00（毫秒）
BASE = int(time.mktime(time.strptime("2026-10-27 12:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000


def award(name, aid, card, status):
    return {"award_id": aid, "consume_card": card, "status": status,
            "award_info": {"goods_name": name}}


def slot(label, start_ms, end_ms, awards, sec=False):
    f = 1 if sec else 1000
    return {"current_times": label,
            "start_time": start_ms // f, "end_time": end_ms // f,
            "awards": awards}


def fake_result():
    """贴近真实的返回：今天已过的场 + 明天待开始的场 + 重叠的 end_time。"""
    h = 3600 * 1000
    return {"exchange_scene_list": [
        {"scene_key": config.SCENE_GOODS,
         "time_award_stock_result_list": [
             # 今天 08:00-10:00（已结束，全抢光）
             slot("08:00", BASE - 4 * h, BASE - 2 * h,
                  [award("汰渍淡雅熏香洗衣液 1kg", 1402434, 128, 1),
                   award("可口可乐 500ml*5瓶", 1402433, 108, 1)]),
             # 今天 10:00-20:00（**已开始但还没结束** —— 旧逻辑会误选它）
             slot("10:00", BASE - 2 * h, BASE + 8 * h,
                  [award("汰渍淡雅熏香洗衣液 1kg", 1402434, 128, 1),
                   award("可口可乐 500ml*5瓶", 1402433, 108, 1)]),
             # 今天 20:00-22:00（未开始）
             # 真实里**每个场次都有全部商品**，这里照做（否则按 award_id 选会选不到）
             slot("20:00", BASE + 8 * h, BASE + 10 * h,
                  [award("汰渍淡雅熏香洗衣液 1kg", 1402434, 128, 2),
                   award("可口可乐 500ml*5瓶", 1402433, 108, 2),
                   award("好友趣薯片 6袋", 1402425, 108, 2)]),
             # 明天 10:00（更远，不该被选）
             slot("10:00", BASE + 22 * h, BASE + 32 * h,
                  [award("汰渍淡雅熏香洗衣液 1kg", 1402434, 200, 2)], sec=True),
         ]},
        {"scene_key": config.SCENE_COUPON_PERCENT,
         "time_award_stock_result_list": [
             slot("00:00", BASE, BASE + h,
                  [{"award_id": 1389999, "consume_card": 1, "status": 0,
                    "award_info": {"goods_name": ""}},
                   {"award_id": 1335012, "consume_card": 1, "status": 1,
                    "award_info": {"goods_name": ""}}]),
         ]},
    ]}


# ---------------------------------------------------------------- 断言小工具
PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: got={got!r} want={want!r}")


def truthy(name, got):
    global PASS, FAIL
    if got:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: got={got!r}")


# ---------------------------------------------------------------- 用例
def t_time():
    print("\n[时间工具]")
    check("to_ms(秒)", slots.to_ms(1793109600), 1793109600000)
    check("to_ms(毫秒)", slots.to_ms(1793109600000), 1793109600000)
    check("to_ms(垃圾)", slots.to_ms("abc"), 0)
    truthy("ts_str", "10-27" in slots.ts_str(BASE))
    e = slots.parse_epoch("22:00")
    truthy("parse_epoch 是整点", e % 60 == 0)
    truthy("seconds_until > 0", slots.seconds_until("23:59") > 0)


def t_all_slots():
    print("\n[场次拍平]")
    ss = slots.all_slots(fake_result())
    check("场次数", len(ss), 4)
    check("按 start 升序", [s["label"] for s in ss], ["08:00", "10:00", "20:00", "10:00"])
    truthy("秒/毫秒已归一", ss[-1]["start_ms"] == BASE + 22 * 3600 * 1000)


def t_upcoming():
    print("\n[未结束场次]")
    ups = slots.upcoming(fake_result(), BASE)
    check("未结束场次数", len(ups), 3)   # 10:00(到20:00) / 20:00 / 明天10:00


def t_pick():
    print("\n[选目标场次]")
    # 关键：不能选到「10:00 那场」（它已开始、但 end_time 还没到）—— 旧逻辑的坑
    p = slots.pick_slot(fake_result(), keyword="汰渍", now_ms=BASE)
    truthy("选到目标", p is not None)
    check("场次是 20:00（未开始的最近一场）", p["label"], "20:00")
    check("award_id", p["award_id"], 1402434)
    check("需卡数", p["consume"], 128)
    check("状态=待开始", p["status"], 2)
    check("开火时刻=场次开始", p["start_ms"], BASE + 8 * 3600 * 1000)

    # 薯片只在 20:00 场有
    p2 = slots.pick_slot(fake_result(), keyword="薯片", now_ms=BASE)
    check("薯片 award_id", p2["award_id"], 1402425)

    # 按 award_id 选
    p3 = slots.pick_slot(fake_result(), award_id=1402433, now_ms=BASE)
    truthy("按 award_id 选到可乐", p3 is not None and p3["award_id"] == 1402433)

    # 不存在的商品
    check("不存在商品 → None", slots.pick_slot(fake_result(), keyword="不存在", now_ms=BASE), None)

    # prefer_future=False 时允许选「进行中」的场次
    p4 = slots.pick_slot(fake_result(), keyword="汰渍", now_ms=BASE, prefer_future=False)
    check("允许进行中 → 10:00 场", p4["label"], "10:00")


def t_coupon():
    print("\n[可兑券]")
    cs = slots.coupon_awards(fake_result())
    check("只返回 status=0 的券", [c["award_id"] for c in cs], [1389999])


def t_err():
    print("\n[错误码翻译]")
    truthy("250013 提示冷启动", "冷启动" in config.err_text(250013))
    truthy("250017 提示限流", "限流" in config.err_text(250017))
    truthy("300001 是真竞争", "真竞争" in config.err_text(300001))
    truthy("0 = 成功", "成功" in config.err_text(0))
    truthy("未知码不炸", "未知" in config.err_text(999999))


def t_describe():
    print("\n[人读清单]")
    txt = slots.describe(fake_result(), keyword="汰渍", now_ms=BASE)
    truthy("含场次标题", "=== 场次 20:00" in txt)
    truthy("标记了目标", "<<< 目标" in txt)


def main() -> int:
    print("=" * 62)
    print("slots 离线单测（不需要手机）")
    print("=" * 62)
    t_time(); t_all_slots(); t_upcoming(); t_pick(); t_coupon(); t_err(); t_describe()
    print("\n" + "=" * 62)
    print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
