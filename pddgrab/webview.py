"""webview — frida ↔ 拼多多 mecox WebView 的 JS 桥

为什么这么绕（2026-10-09 的踩坑记录，改代码前务必先读）：
  * 拼多多用的**不是系统 WebView**，是自带的 `mecox.webkit.WebView`，
    它不继承 `android.webkit.WebView` —— 挂在系统 WebView 上的钩子永远不触发。
  * `mecox` 在**另一个 classloader** 里，必须遍历 classloader 找到能解析它的那个，
    再用它建 ClassFactory。
  * **每次调用都 `Java.registerClass` 会把拼多多搞崩**（SIGSEGV），
    所以整个脚本只注册一个回调类，用 FIFO 队列分发结果。
  * `getUrl()` / `evaluateJavascript()` 必须在 **UI 线程**上调。
  * `frida-ps` 里拼多多的进程名是中文「拼多多」，要用 `pidof` 拿 pid。

另外（2026-10-27 加固）：拼多多同时开多个 `attendance.html`，且**下标顺序会变**，
`resolve_target()` 必须：
  * 按活动 id 精确匹配（默认 195846「减减卡兑好礼」）
  * **硬排除心跳页** `sjs_blank_heart_beat.html`（它会报 visible，但异步回调永不触发
    → sniper 装上去会永远卡在 syncing，看起来一切正常却不开火）
  * 优先选 `visibilityState === "visible"` 的那个
"""
from __future__ import annotations

import json
import time

import frida

from . import config

# ------------------------------------------------------------------ frida agent
AGENT = r"""
(function () {
  var mecoLoader = null;
  Java.enumerateClassLoadersSync().forEach(function (ld) {
    if (mecoLoader) return;
    try { ld.loadClass("mecox.webkit.WebView"); mecoLoader = ld; } catch (e) {}
  });
  if (!mecoLoader) { send({ ev: "fatal", why: "no mecox loader" }); return; }
  var cf = Java.ClassFactory.get(mecoLoader);
  var WV = cf.use("mecox.webkit.WebView");
  var VC = Java.use("android.webkit.ValueCallback");

  var live = [];
  var pending = [];

  // 只注册一次回调类（每次注册会 churn classloader 并把 App 搞崩）
  var CbCls = null;
  function cbClass() {
    if (CbCls) return CbCls;
    CbCls = Java.registerClass({
      name: "pddxValueCallback",
      implements: [VC],
      methods: {
        onReceiveValue: function (v) {
          var r = pending.shift();
          if (r) { try { r(v === null ? "null" : String(v)); } catch (e) {} }
        }
      }
    });
    return CbCls;
  }

  function collect() {
    live = [];
    try {
      cf.choose("mecox.webkit.WebView", {
        onMatch: function (inst) { live.push(Java.retain(inst)); },
        onComplete: function () {}
      });
    } catch (e) {}
  }

  function onMain(fn) {
    return new Promise(function (resolve, reject) {
      Java.scheduleOnMainThread(function () {
        try { resolve(fn()); } catch (e) { reject(String(e)); }
      });
    });
  }

  function evalOn(inst, js) {
    return new Promise(function (resolve) {
      var settled = false;
      function done(v) { if (!settled) { settled = true; resolve(v); } }
      pending.push(done);
      try {
        inst.evaluateJavascript(js, cbClass().$new());
      } catch (e) {
        var i = pending.indexOf(done);
        if (i >= 0) pending.splice(i, 1);
        done("<err " + e + ">");
      }
    });
  }

  rpc.exports = {
    list: function () {
      return onMain(function () {
        collect();
        return live.map(function (w, i) {
          var o = { i: i, url: null, cls: null, shown: null };
          try { o.url = String(w.getUrl()); } catch (e) { o.url = "ERR"; }
          try { o.cls = String(w.getClass().getName()); } catch (e) {}
          try { o.shown = w.isShown(); } catch (e) { o.shown = "ERR"; }
          return o;
        });
      });
    },
    evaljs: function (sel, js) {
      return onMain(function () {
        var s = String(sel == null ? "" : sel);
        var inst = null;
        if (/^\d+$/.test(s)) {
          if (!live.length) collect();
          inst = live[parseInt(s, 10)];
          if (!inst) throw new Error("no webview at index " + s + " (have " + live.length + ")");
        } else {
          if (!live.length) collect();
          var matches = [];
          for (var j = 0; j < live.length; j++) {
            var u = "";
            try { u = String(live[j].getUrl()); } catch (e) {}
            if (u && u.indexOf(s) >= 0) matches.push(live[j]);
          }
          if (!matches.length) throw new Error("no webview whose url contains '" + s + "'");
          for (var k = 0; k < matches.length; k++) {
            try { if (matches[k].isShown()) { inst = matches[k]; break; } } catch (e) {}
          }
          if (!inst) inst = matches[0];
        }
        return evalOn(inst, js);
      });
    },
    reload: function (sel) {
      return onMain(function () {
        if (!live.length) collect();
        live[parseInt(sel, 10)].reload();
        return "reloaded";
      });
    }
  };
})();
"""


def unwrap(raw):
    """evaluateJavascript 返回的是 JSON 字符串，可能要解多层。"""
    v = raw
    for _ in range(3):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                break
        else:
            break
    return v


class Bridge:
    """一次 frida 会话的封装（用完记得 close()）。"""

    def __init__(self, quiet: bool = True):
        self.session = None
        self.script = None
        self.quiet = quiet

    # ---------------------------------------------------------- 生命周期
    def connect(self, retries: int = 3, wait: float = 1.5):
        last = None
        for _ in range(retries):
            try:
                dev = frida.get_device_manager().add_remote_device(config.FRIDA_HOST)
                self.session = dev.attach(_pdd_pid())
                self.script = self.session.create_script(AGENT)
                if not self.quiet:
                    self.script.on("message", lambda m, d: print("[agent]", m))
                self.script.load()
                time.sleep(wait)
                return self
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2)
        raise RuntimeError(f"frida 连接失败: {last}")

    def reconnect(self):
        """冷启动/进程被杀之后必须重连（旧 script 会 "has been destroyed"）。"""
        self.close()
        time.sleep(3)
        return self.connect()

    def close(self):
        try:
            if self.session:
                self.session.detach()
        except Exception:  # noqa: BLE001
            pass
        self.session = self.script = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------- 基础操作
    def list_webviews(self):
        return self.script.exports_sync.list()

    def evaljs(self, sel, js):
        return self.script.exports_sync.evaljs(str(sel), js)

    def reload(self, sel):
        return self.script.exports_sync.reload(str(sel))

    def visibility(self, sel) -> str:
        return str(self.evaljs(sel, "document.visibilityState"))

    def title(self, sel) -> str:
        return str(self.evaljs(sel, "document.title"))

    def url(self, sel) -> str:
        return str(self.evaljs(sel, "location.href"))

    # ---------------------------------------------------------- 选页（加固）
    def resolve_target(self, hint: str | None = None, act_id: str | None = None) -> str:
        """挑出「真正的活动页」的 index（字符串）。

        三级优先：
          1. URL 同时含 hint 和 act_id，且不是心跳页
          2. URL 含 hint，且不是心跳页
          3. m.pinduoduo.net 的页面，且不是心跳页
        再在候选里优先选 visibilityState=visible 的。
        """
        hint = hint or config.TARGET_URL_HINT
        act_id = act_id or config.ACT_ID
        ws = [w for w in self.list_webviews() if isinstance(w, dict)]

        def url_of(w):
            return w.get("url") or ""

        def is_heartbeat(w):
            return any(h in url_of(w) for h in config.HEARTBEAT_HINTS)

        cands = [w for w in ws if hint in url_of(w) and act_id in url_of(w)
                 and not is_heartbeat(w)]
        if not cands:
            cands = [w for w in ws if hint in url_of(w) and not is_heartbeat(w)]
        if not cands:
            cands = [w for w in ws if "m.pinduoduo.net" in url_of(w) and not is_heartbeat(w)]
        if not cands:
            raise RuntimeError(
                "没有可用的活动页（心跳页已排除）；现有: "
                + ", ".join(f"[{w.get('i')}]{url_of(w)[:60]}" for w in ws))

        visible = []
        for w in cands:
            try:
                if "visible" in self.visibility(w["i"]):
                    visible.append(w)
            except Exception:  # noqa: BLE001
                pass
        return str((visible[0] if visible else cands[0])["i"])

    def ensure_page(self, hint: str | None = None, tries: int = 4,
                    gap: float = 5.0, open_if_missing: bool = True) -> str | None:
        """确保活动页存在且可见；没有就（可选）用 deeplink 打开。

        实测拼多多会在长时间运行后把活动页的 WebView 关掉，只剩心跳页；
        这时 resolve_target() 会抛错，需要主动把页面拉起来。
        """
        from . import device
        for i in range(tries):
            try:
                t = self.resolve_target(hint)
                if "visible" in self.visibility(t):
                    return t
            except Exception as e:  # noqa: BLE001
                print(f"  [{i+1}/{tries}] 没有活动页: {str(e)[:70]}")
            if not open_if_missing:
                time.sleep(gap)
                continue
            # 阶梯升级：先温和地 deeplink 打开，最后才冷启动（会丢页面内状态）
            if i == 0:
                print("  → deeplink 打开活动页")
                device.open_activity()
                time.sleep(6)
            else:
                print("  → 冷启动 PDD（force-stop + deeplink）")
                device.cold_start_pdd()
                try:
                    self.reconnect()          # 进程被杀 → 旧 session 失效
                except Exception as e2:       # noqa: BLE001
                    print(f"    重连失败: {str(e2)[:70]}")
                    time.sleep(5)
                    try:
                        self.reconnect()
                    except Exception as e3:   # noqa: BLE001
                        print(f"    再试仍失败: {str(e3)[:70]}")
                        return None
            time.sleep(gap)
        return None

    def wait_visible(self, hint: str | None = None, tries: int = 8,
                     gap: float = 10.0) -> str | None:
        """等到活动页可见，返回 index；超时返回 None。"""
        for i in range(tries):
            try:
                t = self.resolve_target(hint)
                if "visible" in self.visibility(t):
                    return t
                print(f"  [{i+1}/{tries}] 活动页还不可见，{gap:.0f}s 后重试")
            except Exception as e:  # noqa: BLE001
                print(f"  [{i+1}/{tries}] 选页失败: {str(e)[:70]}")
            time.sleep(gap)
        return None


def _pdd_pid() -> int:
    """frida.attach() 必须吃 **int pid**；传字符串会被当成「进程名」去找。"""
    from . import device
    p = device.pid()
    if not p:
        raise RuntimeError(f"{config.PKG} 没在运行")
    return int(p)
