# 走接口抢兑：把「整点抢券」从模拟点击换成 API

> 2026-10-09 实盘验证的产物。结论：**能做，而且比 UI 快得多、可靠得多。**
> 但有一个前提：请求必须由**页面自己**发出（见「为什么不能 curl」）。

---

## 一、为什么值得换

UI 路线（`device/pdd_grab_multi_slot.sh`）的硬伤不是手速，是**页面状态**：

- 活动页是 WebView，长期挂在前台**不会自己重新拉数据**，按钮停在「待开始」；
- 于是开闸瞬间只能盲点，点在一个 `clickable=false` 的按钮上（0 点场就是这么输的）。

接口路线把「等页面翻转」整个删掉了：**到点直接发兑换请求**，页面显示什么无所谓。
代价是要先搞清楚请求长什么样。下面就是全部答案。

---

## 二、怎么进到页面 JS 里（踩坑记录）

### 坑 1：拼多多用的不是系统 WebView

`dumpsys activity top` 显示视图树里是：

```
com.xunmeng.pinduoduo.widget.nested.OverFlingWebView
mecox.webkit.WebView
mecox.provider.impl.InternalWebViewImpl
```

**`mecox` 是拼多多自带的 WebView 实现**（`/data/user/0/com.xunmeng.pinduoduo/app_meco_64/dexs/dex_*/SystemWebViewApk`），
它**不继承** `android.webkit.WebView`。所以：

- `Java.use("android.webkit.WebView")` → 找不到实例（`Java.choose` 返回 0）；
- 挂在 `android.webkit.WebView.loadUrl` 上的钩子**永远不触发**。

### 坑 2：mecox 在**另一个 classloader** 里

`Java.use("mecox.webkit.WebView")` 直接 `ClassNotFoundException`。
必须遍历 classloader 找到能解析它的那个（实测是 `x33.f[... app_meco_64/dexs/dex_503.2.9/SystemWebViewApk ...]`），
再用它建 ClassFactory：

```js
var mecoLoader = null;
Java.enumerateClassLoadersSync().forEach(function (ld) {
  if (mecoLoader) return;
  try { ld.loadClass("mecox.webkit.WebView"); mecoLoader = ld; } catch (e) {}
});
var cf = Java.ClassFactory.get(mecoLoader);
var WV = cf.use("mecox.webkit.WebView");
cf.choose("mecox.webkit.WebView", { onMatch: ..., onComplete: ... });
```

### 坑 3：**每次调用都 `Java.registerClass` 会把拼多多搞崩**

第一版捕获脚本每调一次 `evaluateJavascript` 就注册一个匿名 `ValueCallback` 类，
几十次之后拼多多 **SIGSEGV**（crash log 里栈是 `frida-agent-64.so`），进程重启。

修法：**整个脚本只注册一个回调类**，用 FIFO 队列把结果分发给对应的 Promise。

```js
var CbCls = null;
function cbClass() {
  if (CbCls) return CbCls;
  CbCls = Java.registerClass({
    name: "pddxValueCallback",
    implements: [Java.use("android.webkit.ValueCallback")],
    methods: { onReceiveValue: function (v) { var r = pending.shift(); if (r) r(String(v)); } }
  });
  return CbCls;
}
```

`getUrl()` / `evaluateJavascript()` 都必须在 **UI 线程**上调，用 `Java.scheduleOnMainThread`。

### 坑 4：`frida-ps` 里拼多多的名字是「拼多多」

`dev.get_process("com.xunmeng.pinduoduo")` 会 `ProcessNotFoundError`。
进程名是**应用标签**。用 `adb shell pidof com.xunmeng.pinduoduo` 拿 pid。

---

## 三、接口全貌

活动页 URL：

```
https://m.pinduoduo.net/attendance.html?type=1&id=197270&_x_query=ddd&...
```

所有接口都挂在 `/proxy/api/` 前缀下（页面里写的是相对路径 `api/...`）。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `api/server/_stm` | 服务器时间（对时用） |
| POST | `api/carnival/subtract_card/query_time_awards` | 场次+奖品表（body 传 `{}` 即可） |
| GET | `api/origenes/addresses` | 收货地址列表 |
| POST | `api/carnival/subtract_card/check_address` | 地址可配送校验（**非必需**，只是弹窗里的提示） |
| POST | **`api/carnival/subtract_card/exchange_time_award`** | **兑换实物（就是它）** |

### 兑换请求体（从 `DcCouponSpikesV5` bundle 里读出来的）

```js
// store.clickGiftBtn 的 onOk：
e.exchangeAwards({ award_id: t.award_id,
                   address_id: n.address_id,
                   address_snapshot_id: n.address_snapshot_id });
```

```json
POST /proxy/api/api/carnival/subtract_card/exchange_time_award
{"award_id":1402419,"address_id":"<你的默认地址ID>","address_snapshot_id":"<快照ID>"}
```

实测：只传 `award_id` → `error_code 40003`（错误的请求参数）；
加上两个地址字段 → 参数校验通过，只剩业务错误。**地址字段是必需的。**

### 目标奖品（`scene_key = BIDDING_GOODS_EXCHANGE`）

```json
{"award_id":1402419,"entry_id":"704386696832","entry_type":1006,
 "consume_card":388,
 "award_info":{"template_goods_id":704386696832,
               "goods_name":"酷态科自带线充电宝 10000毫安",
               "status":2}}
```

`status`：`0`=可兑换 `1`=已抢光 `2`=待开始 `11`=已兑换。

### 错误码

前端映射表原文：

```
300001/250009 慢了一步没抢到      300002/250005 未到兑换时间
300003/250010 积分（减减卡）不够   300004/250003/250004 今日兑换已达上限
250014/250006 活动太火爆
40003         错误的请求参数（参数不全）
```

**但实测发现前端表里没有的两个码才是关键**（2026-10-09 实盘挖出来的）：

| 码 | 含义 | 证据 |
|---|---|---|
| `250013` | **不在兑换窗口内**（窗口没开，或已经关了） | 开闸前 30 秒的探针返回它；开闸 +270ms 之后一直打到 +9.2s 的 40 发也全是它 |
| `250017` | **在兑换窗口内，但没抢到** | 只在开闸边界出现（10:00 场：T-100ms ~ T+150ms 那几发） |

**这个区分极其重要**：08:00 那场「40 发全 250013」曾被误读成「抢不过别人」，
其实真相是**兑换窗口只有约 250ms 宽**，绝大多数发数落在窗口外，压根没参赛。
换句话说 250013 多 ≠ 输了，只说明**没进窗口**；要看有没有 250017 才知道是不是真的在竞争。

（边界内 250013/250017 会混着出现，怀疑是到达抖动或多后端状态不一致，未完全解释。）

### 由此得出的调优方向

窗口只有 ~250ms，那么**唯一可控的杠杆就是「窗口内放多少发」**。
`sniper.js` v6 把齐射从 9 发改成 **40 发、覆盖 `[-250ms, +350ms]`、约每 15ms 一发**
（`burstCount` / `burstFromMs` / `burstToMs` 可调）。余额 406 < 776，
两次 388 不可能同时成功，所以密齐射不会造成重复兑换。

再往上就是多账号并发——单账号 + 家宽的天花板就在这里。

---

## 四、关键发现：`useNativeAntiToken`

`promotion-*.js` 里的 axios 请求拦截器：

```js
R = function (e) { return { name: "appendAntiContent", request: function (t) {
  if (!1 === t.needAntiContent && !t.useNativeAntiToken) return t;
  var n = getCurrentPlatform(), r = n.isNativePlatform, o = n.version;
  return t.useNativeAntiToken && r && compareVersion(o, "4.69.0") >= 0
    ? t                                   // ← 原生 App ≥4.69：JS 什么都不加
    : d.getAntiContent(e.serverTime).then(function (e) { ...anti_content... });
}}};
```

也就是说：**在 8.26.0 这种原生 App 里，`anti_content` 不是 JS 生成的，是原生网络层注入的。**

推论（实测成立）：**只要请求是从这个 WebView 的 JS 里发出的，原生层就会给它签名。**
所以我们不需要逆 `libpdd_secure.so`、不需要复现签名算法 —— 在页面里发一个普通的
`XMLHttpRequest` 就行。

> 这也解释了「为什么不能 curl」：脱离 WebView 就没有那层原生签名，
> 请求会被判成非法。想脱离 WebView，就得去逆 native 签名，那是另一个量级的工程。

---

## 五、T-0 狙击手（`api/sniper.js`）

注入到页面 JS 上下文里，自己完成全套动作：

1. `GET api/server/_stm` 对时，`offset = server_time - (t0 + rtt/2)`
   （实测这台机器设备时钟比服务器慢 **~715ms**，不对时必输）；
2. `query_time_awards` 找到 `BIDDING_GOODS_EXCHANGE` 里**最近一个还没开始的场次**、
   且 `goods_name` 含「酷态科」的 award → `award_id`；
   - 注意：不能只按 `end_time` 过滤 —— 07:50 时 00:00 场还没结束（它跑到 08:00），
     但那场的酷态科是 `status=1`（已抢光）。要按 `start_time` 挑。
3. `GET api/origenes/addresses` 取默认地址（`is_default === "1"`）；
4. 开闸前 100s 重新对时一次（设备时钟会漂），然后**自旋**到 `fireAt` 精确开火；
   - 最后 2 秒用 busy-wait 而不是 `setTimeout`：WebView 被切到后台会
     把定时器节流到 1/s，靠 `setTimeout` 会晚最多一秒。
5. 失败重试（默认 50 次、间隔 100ms）。

### 安全设计：不会重复兑换

- 收到**业务错误**（有响应）说明没兑换成功 → 重试是安全的；
- 收到**网络错误/超时**（没有响应）→ 结果未知，**先 `query_time_awards` 查状态**，
  确认 `status !== 11` 才继续重试。否则一次「慢但成功」的首发会被追一次二发，
  变成扣两次 388 张。

### 重入

每次 `arm()` 递增 `S.gen`，所有定时器链和回调都带 gen 校验，
所以中途重新 arm 不会让旧链子继续开火。

---

## 六、怎么用

```bash
cd api

python fr_arm.py status                 # 看当前状态
python fr_arm.py dry                    # 彩排：对时+找奖品+找地址，不开火
python fr_arm.py arm                    # 真武装（自己从服务器解析场次）
python fr_arm.py watch 600              # 常驻，页面重载会自动重装重武装

# 无人值守：07:59:15 重新武装，盯到 08:01:30
python run_slot.py --arm-at 07:59:15 --until 08:01:30 --keyword 酷态科
```

辅助工具：

| 文件 | 用途 |
|---|---|
| `h5.py` | 在页面里执行任意 JS（`list` / `eval` / `cap` / `dump` / `reload`） |
| `fr_capture.py` | 抓 H5 的 XHR/fetch（挂 `onPageFinished` 自动重注入，扛页面重载） |
| `fr_arm.py` | 装/武装/监控狙击手 |
| `run_slot.py` | 无人值守：到点重新武装 + 盯结果 |
| `fr_probe.py` / `fr_webview.py` / `fr_loaders.py` / `fr_mecox.py` / `fr_hook_url.py` | 侦察用：找进程、找 WebView、找 classloader、挂导航 |

前置条件：设备已 root（KernelSU），`frida-server` 已在跑：

```bash
adb shell "su -c 'setsid /data/local/tmp/.x99 -l 0.0.0.0:28080 >/data/local/tmp/.x99log 2>&1 &'"
adb forward tcp:28080 tcp:28080
```

---

## 七、和 UI 脚本的关系

两者**可以并存**，而且互为备份：

- 接口狙击手：T+0.000 直接发请求，不依赖页面状态；
- UI 脚本：T-12 秒 `HOME`+`monkey` 把页面拉回前台（实测**不会**重载 WebView，
  狙击手能活下来），T-0 连点。

一个真实的耦合点：UI 脚本在 `onpage` 失败时会走 `navigate()` 重新进活动页，
**那会重载页面、把狙击手清掉**。所以 `run_slot.py` 会持续轮询，
发现 `window.__sniper` 不见了就自动重装重武装。

## 八、风险

- 平台规则 7.1 禁止插件/外挂。这条路线比模拟点击更接近「外挂」的定义，
  账号风险自负。
- `frida` 常驻进程 + 调试端口本身是可被检测的特征。本次实测 8.26.0 没有当场
  反制（多次 attach/detach 均正常），但不代表以后不会。
- 兑换**不可撤销**。狙击手默认 `autoConfirm` 语义（直接兑换，不等人工确认），
  这是需求「抢到必须兑换」的直接体现。
