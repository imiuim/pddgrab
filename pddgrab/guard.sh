#!/system/bin/sh
# ============================================================================
# guard.sh — 设备端「到点冷启动 + 守前台 + 抢完息屏」守护
#
# 为什么需要它（而不是只靠 PC）：
#   * 开火前必须 **force-stop 冷启动** 拼多多刷新会话 —— 否则兑换请求会被
#     服务端判成 250013（请求没被认），这是 2026-10-27 实测的关键结论。
#   * PC 可能断线/休眠，设备端自己守着更可靠。
#
# 用法（时间都是 epoch 秒）：
#   sh guard.sh <唤醒epoch> <息屏epoch> [deeplink] [包名] [日志文件]
#
# 例：
#   sh guard.sh 1793109300 1793109900 \
#     'pinduoduo://com.xunmeng.pinduoduo/web?url=https%3A%2F%2Fm.pinduoduo.net%2Fattendance.html%3Ftype%3D1%26id%3D195846%26_pdd_fs%3D1'
# ============================================================================

WAKE_AT=${1:?需要 唤醒epoch}
SLEEP_AT=${2:?需要 息屏epoch}
DEEPLINK=${3:-'pinduoduo://com.xunmeng.pinduoduo/web?url=https%3A%2F%2Fm.pinduoduo.net%2Fattendance.html%3Ftype%3D1%26id%3D195846%26_pdd_fs%3D1'}
PKG=${4:-com.xunmeng.pinduoduo}
L=${5:-/data/local/tmp/pdd_guard.log}

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$L"; }

log "guard start（冷启动 $(date -d @$WAKE_AT '+%H:%M:%S' 2>/dev/null || echo $WAKE_AT) / 息屏 $(date -d @$SLEEP_AT '+%H:%M:%S' 2>/dev/null || echo $SLEEP_AT)）"

# ---- 1) 等到唤醒时刻 ----
while [ "$(date +%s)" -lt "$WAKE_AT" ]; do sleep 20; done

# ---- 2) 唤醒 + 接管屏幕 ----
log "到点：唤醒 + 冷启动刷新页面"
settings put global stay_on_while_plugged_in 7 2>/dev/null
svc power stayon true 2>/dev/null
settings put system screen_off_timeout 2147483647 2>/dev/null
echo pddguard > /sys/power/wake_lock 2>/dev/null
input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
sleep 1
wm dismiss-keyguard >/dev/null 2>&1
cmd statusbar collapse >/dev/null 2>&1
sleep 1

am force-stop "$PKG" >/dev/null 2>&1
sleep 3
am start -a android.intent.action.VIEW -d "$DEEPLINK" >/dev/null 2>&1
sleep 14
log "冷启动完成"

# ---- 3) 窗口内守前台 ----
while [ "$(date +%s)" -lt "$SLEEP_AT" ]; do
  if ! pidof "$PKG" >/dev/null 2>&1; then
    log "PDD 进程没了，重新拉起"
    am start -a android.intent.action.VIEW -d "$DEEPLINK" >/dev/null 2>&1
    sleep 10
  else
    FG=$(dumpsys window 2>/dev/null | grep -o 'mCurrentFocus=Window{[^}]*}' | head -1)
    case "$FG" in
      *"$PKG"*) ;;
      *) log "前台被占（$FG），顶回 PDD"
         monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 ;;
    esac
  fi
  sleep 15
done

# ---- 4) 收工息屏 ----
log "收工：撤常亮并息屏"
echo pddguard > /sys/power/wake_unlock 2>/dev/null
settings put global stay_on_while_plugged_in 0 2>/dev/null
svc power stayon false 2>/dev/null
settings put system screen_off_timeout 60000 2>/dev/null
input keyevent KEYCODE_POWER >/dev/null 2>&1
log "guard end"
