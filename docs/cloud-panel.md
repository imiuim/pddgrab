# 云端控制面 + Web 面板（119.29.105.32）

## 先说清楚一条硬约束

**抢兑那一跳搬不到云上。** 兑换请求的 `anti_content` / `anti-token` 是拼多多 App 的原生库
（`libpdd_secure.so`）生成的，脱离那台手机就没有合法签名。所以：

- **云上能做的**：下发指令、收集遥测、跑 Web 面板、存日志。
- **只能手机上做的**：真正发出兑换请求（必须在拼多多 WebView 的页面 JS 里发）。

于是架构是**手机主动外连云**，而不是云去连手机——这样家里路由器一个端口都不用开。

```
        云端 119.29.105.32:8080                   手机（root）
   ┌──────────────────────────────┐          ┌──────────────────────────┐
   │ panel.py  (systemd pddpanel) │          │ agent.run.sh (常驻)      │
   │  GET  /            面板 HTML │◄── 轮询 ──│  每 5s 取指令            │
   │  GET  /api/state   面板数据  │   指令   │  执行 frida-inject       │
   │  POST /api/report  遥测入口  │─── 指令 ─►│                          │
   │  GET  /api/cmd/poll         │          │ sniper v7 (在页面 JS 里) │
   │  POST /api/cmd     下发指令  │◄── 遥测 ──│  自己上报日志            │
   │  GET  /files/*     分发文件  │          └──────────────────────────┘
   └──────────────────────────────┘
```

## 手机侧为什么能自己注入

用 **`frida-inject`**（frida 官方发布的静态 arm64 二进制，56MB）——手机 root 下直接
`frida-inject -p <pid> -s arm.js`，不需要 PC、不需要在手机上装 Python。
狙击手是用 `evaluateJavascript` 推进**页面**的 JS 上下文的，所以 frida-inject 退出后它照样活着。

## 文件

| 文件 | 位置 | 作用 |
|---|---|---|
| `cloud/panel.py` | 云端 `~/pddgrab/` | 面板 + 遥测 + 指令通道（纯标准库，机器上没有 pip） |
| `cloud/build.sh` | 云端 | 把 `sniper.js` 嵌进 `inject_arm.js`，生成 `dist/arm.js` |
| `cloud/inject_arm.js` | 云端 | frida-inject 脚本：装 + 武装狙击手 |
| `cloud/inject_status.js` | 云端 | frida-inject 脚本：读**紧凑**状态（不把 40 发日志全传回来） |
| `cloud/snapshot.js` | 云端 | frida-inject 脚本：读账号 / 减减卡 / 地址 / 全部可兑换商品 |
| `cloud/login.sh` | 手机 `/data/local/tmp/pddgrab/` | 按文字定位驱动拼多多登录页（填号/填码） |
| `cloud/agent.sh` | 手机 `/data/local/tmp/pddgrab/agent.run.sh` | 常驻 agent：取指令、执行、回报、按需顶前台 |
| `cloud/bootstrap.sh` | 手机 | 一次性初始化：从云端拉全套并渲染 token |
| `web/` | 本机 → 云端 `dist/app/` | React 仪表盘源码（Next.js 静态导出） |

## 指令

手机 agent 每 5 秒轮询一次；`arm`/`status`/`disarm` 走 frida-inject，约 30 秒返回。

```bash
B=http://119.29.105.32:8080
T=<PANEL_TOKEN>
curl -sS --noproxy '*' -X POST "$B/api/cmd?token=$T" \
     -H 'Content-Type: application/json' -d '{"cmd":"hold"}'
```

| cmd | 作用 |
|---|---|
| `hold` | 建 hold 标记，agent 才开始每 5s 保持屏幕常亮 + 拼多多前台 |
| `unhold` | 删标记，**把手机还给用户** |
| `nav` | 走搜索 ddd → 活动横幅，回到活动页 |
| `snapshot` | 读账号 / 减减卡 / 收货地址 / 全部可兑换商品并上报（不在活动页时会先 `nav` 再重试一次） |
| `login:<stage>:<value>` | 登录中继。`login:phone:13800138000` 打开登录页填号并请求验证码；`login:code:123456` 填码并提交 |
| `arm` | 装 + 武装 sniper v7（用编译时烤死的默认目标） |
| `arm:<商品名>` | **帮抢指定商品**：把 `arm.js` 里的 `keyword` 换成这个名字再注入 |
| `status` | 读紧凑状态（state / fireAt / awardId / 各错误码计数 / drift） |
| `disarm` | 解除 |
| `keepalive` | 立刻顶一次前台 |

**默认不 hold**：agent 平时只安静轮询，不会抢手机。开抢前必须先 `hold`。

## 面板（React / Next.js）

浏览器打开（token 会写进 localStorage，也可以放 `?token=`）：

```
http://119.29.105.32:8080/?token=<PANEL_TOKEN>
```

源码在 `web/`（Next.js 16 + React 19 + Tailwind 4 + shadcn/ui），构建成静态文件后由
`panel.py` 直接从 `dist/app/` 分发：

```bash
cd web && npx next build            # 输出 out/（next.config.ts 里 output:"export"、basePath:"/app"）
scp -r out/. ubuntu@<host>:~/pddgrab/dist/app/
```

| 路由 | 内容 |
|---|---|
| `/` | React 仪表盘（= `/app/`） |
| `/app/*` | 静态资源；未知路径回落 index.html |
| `/legacy` | 最早的纯 HTML 控制台（里面的 JS 有语法错，只作参考） |

仪表盘显示：**当前登录账号**（uid / 昵称 / 脱敏手机号）、**减减卡余额**（外加"当前可兑 N 件 / 卡数够 N 件"）、
**全部可兑换商品表**（场次类型 / 场次 / 商品 / 需卡 / 差额 / 状态，可筛"只看实物兑换""只看当前可兑"）、
狙击手状态 + 各错误码命中数 + 实时日志。

### 登录中继（测试机没 SIM 卡时怎么登回去）

测试机 `gsm.sim.state=ABSENT`，收不到短信；而且**拼多多这账号是单会话** ——
2026-10-10 01:33 在 iPhone 上用微信登录后，测试机直接被挤下线（登录页原话：
「在其他设备上通过微信授权进行了登录，当前设备已退出登录」）。

**为什么不干脆用 cookie 在服务器上读余额**：实测过了，两条路都不通 ——

1. 从手机 WebView 里抠出还留着的 `PDDAccessToken`，从本机直接打
   `POST /proxy/api/api/carnival/hour_coupon/task_list`，两种 cookie 都回 **`40001`（未登录）**。
   那个 token 已被挤下线作废。
2. 更根本的是：**抢兑那一跳永远离不开手机**（`anti_content` 由 `libpdd_secure.so` 原生签），
   所以哪怕服务器能读，真正的抢还是得手机登录着。**没有捷径，必须把手机登回去。**

于是做的是**中继**：云端只负责把手机号和验证码转给手机，登录动作在手机上完成。

```
面板「拼多多登录（手机号中继）」卡片
   │  手机号 → POST /api/cmd {"cmd":"login:phone:13800138000"}
   ▼
手机 agent（login:* 分支）→ sh login.sh phone <号>
   │  uiautomator dump 按**文字**找控件（不硬编码坐标，登录页改版会位移）
   │  切「手机号」入口 → 填号 → 点「立即登录 / 获取验证码」
   ▼
用户收到短信 → 面板填验证码 → login:code:123456 → login.sh code <码> → 点「登录」
```

`login.sh` 的两个细节：

- **输出保持纯 ASCII**（`{"ok":true,"stage":"phone","sent":true}` / `{"ok":false,...,"err":"no_phone_field"}`）。
  手机侧 shell 是 C locale，中文过 `$(...)` 会变 `?`；中文文案一律由面板渲染，
  错误码在面板侧有中文映射表。
- 手机号和验证码**强制纯数字**（`case "$VALUE" in *[!0-9]*) die`），挡掉把别的指令注入进 UI 驱动。

**⚠️ agent 单实例锁**：`agent.pid` + `/proc/<pid>` 存活检查。重复启动的 agent 会各自
轮询到同一条指令并**各执行一遍**（`login` 就是点两次「立即登录」，`snapshot` 就是打两遍接口）。
这个坑踩过两次，所以现在第二个 agent 会自己退出。

### 「帮抢」按钮（攒够卡再抢）

面板的「可兑换商品」表每行右侧有个**帮抢**按钮：**减减卡余额 ≥ 该商品需卡时才可点**，
点了就把狙击手的目标换成这一件（`arm:<商品名>`），到点自动抢。

实现上没动 sniper 的选品逻辑：`build.sh` 把 `keyword`（默认「酷态科」）烤进 `dist/arm.js`，
agent 收到 `arm:<商品名>` 时用 `sed` **现改一份临时脚本**再注入（`arm.target.js`），
不改原文件 —— 下次不带参数的 `arm` 还是用默认目标。商品名做了白名单校验
（挡掉 `"'\`$&\/|;<>` 这些会破坏 sed/JSON 的字符）。

**⚠️ 从命令行 curl 发中文 cmd 会被 shell 搞坏**（JSON 解析失败 → 面板回 `no cmd`）：
要么用 `\uXXXX` 转义（`{"cmd":"arm:\u5929\u5802\u6674\u96e8\u4f1e"}`），要么用文件传 body。
浏览器发的 UTF-8 正常，不受影响。

### 拼多多「每日任务」上报（面板上的进度卡片）

设备端还有一个**不依赖云**的每日任务脚本（在 Flutter 仓 `teld-signin-flutter` 里，
`assets/pdd_daily_tasks.sh`）：主窗口 **00:05–00:59 随机一分钟**自动做完浏览类任务 + 签到，
**08:05–08:59 兜底**。它**只把进度上报到面板**，指令和排程都在设备上。

> 窗口为什么是这个：同机还有特来电（00:05–02:59，RTC 闹钟随机一分钟，只在息屏时跑）
> 和元宝（1-3 点主窗口 + 8 点兜底）。**本脚本刻意排在最前面**（在元宝 1 点开窗之前），
> 跑完就把手机让给元宝；两个窗口都避开 :00 整点（TEE_AutoList 的 security_patch 在整点跑）。

上报走已有的 `POST /api/report`，`kind=daily`，body 两种形状：

```json
{"task":"浏览得卡","state":"done"}                       // 单个任务的进度
{"state":"done","before":"49","after":"55"}              // 整轮的状态
```

`panel.py` 收到 `kind=daily` 会**累积**成一张任务表（`tasks`: 任务名→状态，
`run`: 整轮状态与余额变化），存进 `state['daily']`，`/api/state` 一并返回，
React 面板渲染成「拼多多每日任务」卡片。

**分享得卡的邀请码**：活动页里点开分享会弹一张**带邀请码的图**（`193 865 860` 这种
9 位码，朋友在拼多多搜这个码就能给你助力）。两段式拿这个码：

1. **优先从无障碍树读**：那弹窗是 H5，而 PDD 的 H5 文本是进无障碍树的
   （「我的减减卡」「浏览得卡」都能 dump 到），所以邀请码很可能直接就是文本节点 ——
   脚本按 `[0-9]{3} ?[0-9]{3} ?[0-9]{3}` 找，找到就**完全不用 OCR**，
   顺便拿它的 bounds 当裁剪框。
2. **读不到才截图 + 面板侧 OCR**：整屏上传 → 面板裁剪 → tesseract 认数字。

`/api/shot` 现在接受 `crop`（`x,y,w,h`）和 `code` 两个字段：

- **裁剪是纯标准库实现的**（`cloud/pngcrop.py`）：云主机没有 PIL，而 PNG 裁剪就是
  zlib 解压 → 逐行反滤波（5 种 filter）→ 裁行裁列 → 重新压缩。已用 PIL 对拍验证
  **逐像素完全一致**（1080×2400 约 4.8 秒，一天一次可接受）。
  裁剪前的整屏会另存成 `shot_*_full.png`，不丢信息。
- **OCR 用 tesseract**（`apt install tesseract-ocr`，已装 5.5.0）。
  这张图是**白字红底的大号数字**，是理想输入：`--psm 7 -c tessedit_char_whitelist=0123456789`
  实测读出 `193 865 860`，面板把非数字字符滤掉 → `193865860`。

**为什么选 tesseract 而不是 PaddleOCR/EasyOCR**：云主机**没有 pip**（见第一节的硬约束），
任何 Python OCR 库都装不了；tesseract 是系统包，`apt` 一条命令、约 50MB、无需 pip。
而且这里要认的只是孤立数字，tesseract 的强项正好够用 —— 中文识别不行，但不需要。

**⚠️ `/api/shot` 的 token 走 query 不走 body** —— `_authed()` 只认 query/cookie，放 body 会 401（踩过）。
截图 base64 后约 0.5~2MB，**脚本用 `--data-binary @file` 发**，不能当命令行参数（超 ARG_MAX）。
`_serve_file` 里补了 `.png → image/png`（否则是 `octet-stream`，浏览器会当成下载而不是显示）。

### 快照这条链上踩过的三个坑

1. **`evaluateJavascript` 的结果是 JSON 编码过的。** 页面里 `return JSON.stringify(out)`，
   frida 回调拿到的其实是 `"\"{...}\""`——**只 parse 一次得到的是字符串，不是对象**。
   所以 `snapshot.js` 里的 `parseMaybe()` 要一直 parse 到不是字符串为止。
2. **非 ASCII 会被 shell 转成 `?`。** 手机侧 `out=$(run_inject snapshot.js)` 走的是 C locale 的
   `tr/tail/sed` 管道，中文全变成 `?`——商品名 `酷态科自带线充电宝 10000毫安` 曾经上报成
   `????????? 10000??`。修法是 `say()` 里把 `\u007f-\uffff` 转义成 `\uXXXX`，让载荷变成纯 ASCII，
   任何字节级管道都不会破坏它，JSON 解析时再还原。
3. **别让心跳页污染快照。** `sjs_blank_heart_beat.html` 是个隐藏的心跳文档，它同样报
   `visibilityState === "visible"`，但访问不到活动接口，读出来 uid/scores 全是 null。
   `snapshot.js` 现在会在只剩心跳页时**拒绝上报**（返回 `ok:false`），由 agent `nav` 后重试——
   否则一次失败读取会把面板里上一份好快照覆盖掉。

## 遥测为什么要批量 + 暂停

狙击手的日志**不逐条实时发**：开闸前后 `[-5s, +15s]` 会暂停上报，其余时间每 3 秒批量发一次。
原因是上报和兑换请求走同一个连接池，开闸瞬间必须把连接全留给兑换。

## 安全

- 除 `/files/` 外所有接口都要 token；面板 HTML 用 `?token=` 种 cookie。
- `/files/` 是公开的（手机要下载 `frida-inject`）。里面没有密钥，`token.txt` 除外——**如果介意，
  可以给 `/files/` 也加 token，但那样手机 bootstrap 就要先拿到 token**。
- 8080 端口在腾讯云安全组里是放行的（实测公网可达）。
- 手机 agent 会执行云端下发的指令，等价于给云端一个 RCE 通道——token 泄露等于手机被控。
