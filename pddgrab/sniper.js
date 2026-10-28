/* sniper.js — fire the 整点抢券 goods exchange at exactly slot-open time.
 *
 * Runs INSIDE the PDD Uno H5 page (mecox WebView), so the app's own native
 * HTTP layer supplies the anti-token. That is the whole point: the JS axios
 * interceptor skips anti_content when `useNativeAntiToken` is set on a native
 * app >= 4.69, because the native layer injects it.
 *
 * Endpoint (from DcCouponSpikesV5 bundle):
 *   POST api/carnival/subtract_card/exchange_time_award
 *   body { award_id, address_id, address_snapshot_id }
 *
 * Why a staggered BURST around the slot boundary (2026-10-09 lesson):
 *   the 08:00 run fired shot #0 at server-time exactly equal to the slot start
 *   (drift 0 ms) and still got 250013. A request is SENT at T+0 but ARRIVES
 *   ~20-35 ms later, so it is already late for a 1-unit stock. We now fire a
 *   ladder of shots straddling the boundary so one lands on it, and we log
 *   every shot's error code so the boundary can be mapped from evidence.
 *
 * Safe to burst: a request that fails never exchanges, and the account balance
 * (406 减减卡) cannot cover two 388 exchanges anyway. A shot with NO response
 * is treated as unknown and triggers a status re-query before anything else.
 */
(function () {
  "use strict";
  var VERSION = 13;
  if (window.__sniper && window.__sniper.version >= VERSION) {
    return "already v" + window.__sniper.version;
  }

  var API = {
    stm:   "/proxy/api/api/server/_stm",
    query: "/proxy/api/api/carnival/subtract_card/query_time_awards",
    addr:  "/proxy/api/api/origenes/addresses",
    xchg:  "/proxy/api/api/carnival/subtract_card/exchange_time_award"
  };

  var S = {
    version: VERSION,
    gen: 0,
    state: "idle",
    err: null,
    cfg: null,
    offset: 0,
    rtt: 0,
    fireAt: 0,
    awardId: null,
    addressId: null,
    addressSnapshotId: null,
    slot: null,
    attempts: 0,
    log: [],
    result: null,
    resynced: false,
    diagDone: false
  };
  window.__sniper = S;

  function nowSrv() { return Date.now() + S.offset; }

  // ---- cloud telemetry -------------------------------------------------
  // Events are buffered and flushed in batches OUTSIDE the critical window.
  // Reporting during the burst would share the connection pool with the
  // exchange requests themselves, which is the last thing we want.
  var outbox = [];
  function report(kind, body) {
    if (!S.reportCfg || !S.reportCfg.url) return;
    try {
      outbox.push({ source: "sniper", kind: kind,
                    body: typeof body === "string" ? body : JSON.stringify(body) });
      if (outbox.length > 400) outbox.shift();
    } catch (e) {}
  }
  function inCriticalWindow() {
    var d = nowSrv() - S.fireAt;
    return d > -5000 && d < 15000;
  }
  function flush() {
    if (!S.reportCfg || !S.reportCfg.url || !outbox.length || inCriticalWindow()) return;
    var batch = outbox.splice(0, outbox.length);
    for (var i = 0; i < batch.length; i++) {
      try {
        var x = new XMLHttpRequest();
        x.open("POST", S.reportCfg.url, true);
        x.setRequestHeader("Content-Type", "text/plain;charset=UTF-8");
        x.send(JSON.stringify(batch[i]));
      } catch (e) { /* telemetry must never break the grab */ }
    }
  }
  setInterval(flush, 3000);

  function log(o) { o.t = Date.now(); S.log.push(o); if (S.log.length > 400) S.log.shift(); report(o.ev || "log", o); }
  function fail(m) { S.state = "error"; S.err = String(m); log({ ev: "error", m: String(m) }); }
  function ms(v) { return v > 1e11 ? v : v * 1000; }

  // ⚠️ 2026-10-27 重大更正：**250017 不是「窗口内但没抢到」，是「请求过于频繁」**。
  //
  // 实测（窗口关闭时打请求，本该全是 250013）：
  //     5 发 / 间隔   0ms → 4 个 250017
  //     5 发 / 间隔 200ms → 3 个 250017
  //     5 发 / 间隔 400ms → 2 个 250017
  //     5 发 / 间隔 800ms → **0 个 250017**（全 250013）
  // 结论：**间隔 <800ms 就会被限流**。旧版按 15ms 打 40 发，从第 2 发起
  // 全是限流拒绝 —— 我们从来没真正进过窗口竞争
  // （一次 250009「慢了一步没抢到」都没拿到过）。
  //
  // 推论：旧文档「窗口只有 250ms 宽」是**误读 250017 得出的**，真实窗口很可能
  // 宽得多。所以策略反过来：**首发精确打在 T0，之后按安全间隔重试**。
  //
  // ★ 2026-10-27 20:30 再进一步：**串行链不被限流**！
  //   并发齐射（一次发 N 个 XHR）会被限流（250017）；
  //   但「收到上一发响应再发下一发」的串行链，实测 **12.2 发/秒、零限流**
  //   （每发 RTT ~90ms，自然形成 ~11/s 的节奏）。
  //   所以现在 gapMs=0 —— 响应一到立刻发下一发，maxAttempts=120（约 11 秒）。
  //   这才是能跟 10-09 那场（1 秒内 40 发）掰手腕的密度。
  function buildBurst(cfg) {
    if (cfg.burstOffsetsMs && cfg.burstOffsetsMs.length) return cfg.burstOffsetsMs;
    // 默认只打一发（fireAt 那一刻），其余交给 fireShot 的串行重试链
    var n = cfg.burstCount || 1;
    var a = cfg.burstFromMs, b = cfg.burstToMs;
    if (n < 2) return [Math.round(a || 0)];
    var out = [];
    for (var i = 0; i < n; i++) out.push(Math.round(a + (b - a) * i / (n - 1)));
    return out;
  }

  function req(method, url, body, cb) {
    var x = new XMLHttpRequest(), t0 = Date.now();
    try { x.open(method, url, true); } catch (e) { cb("open:" + e); return; }
    x.timeout = 8000;
    if (body != null) x.setRequestHeader("Content-Type", "application/json;charset=UTF-8");
    x.onload = function () {
      var j = null;
      try { j = JSON.parse(x.responseText); } catch (e) {}
      cb(null, x.status, j, String(x.responseText), Date.now() - t0);
    };
    x.onerror   = function () { cb("network", x.status, null, "", Date.now() - t0); };
    x.ontimeout = function () { cb("timeout", x.status, null, "", Date.now() - t0); };
    try { x.send(body == null ? null : JSON.stringify(body)); }
    catch (e) { cb("send:" + e); }
  }

  function syncTime(cb) {
    var t0 = Date.now();
    req("GET", API.stm, null, function (err, st, j, raw, rtt) {
      if (err || !j || !j.server_time) { cb("stm " + err + " " + String(raw).slice(0, 120)); return; }
      S.rtt = rtt;
      S.offset = j.server_time - (t0 + Math.round(rtt / 2));
      log({ ev: "sync", offset: S.offset, rtt: rtt, srv: j.server_time });
      cb(null);
    });
  }

  function resolveAward(cb) {
    req("POST", API.query, {}, function (err, st, j, raw) {
      if (err || !j || !j.result) { cb("query " + err + " " + String(raw).slice(0, 200)); return; }
      var scenes = j.result.exchange_scene_list || [];
      var scene = null, i;
      for (i = 0; i < scenes.length; i++) {
        if (scenes[i] && scenes[i].scene_key === "BIDDING_GOODS_EXCHANGE") scene = scenes[i];
      }
      if (!scene) { cb("no BIDDING_GOODS_EXCHANGE scene"); return; }
      var srvNow = nowSrv(), best = null;
      var slots = scene.time_award_stock_result_list || [];
      // 选「**还没开始**的最近一场」（2026-10-26 修）。
      // 旧逻辑是「en > now 就算候选，且 st 不早于 now-60s」，有两个坑：
      //   ① 今天 22:00 场的 end_time 是明天 00:00 —— 22:51 时它「还没结束」，
      //      于是被当成候选，可它早就开始了，选中它等于对着空气开火。
      //   ② 场次之间 end_time 互相重叠（10:00 场跑到 20:00），重叠区里会选错。
      // 现在只认 st > now（真的还没开始），取最早的那个。
      var seen = [];
      for (i = 0; i < slots.length; i++) {
        var sl = slots[i], stM = ms(sl.start_time), enM = ms(sl.end_time);
        if (enM <= srvNow) continue;          // 已结束
        if (stM <= srvNow) continue;          // 已开始（不能对着已开的场开火）
        var aws = sl.awards || [];
        for (var k = 0; k < aws.length; k++) {
          var aw = aws[k];
          var nm = (aw.award_info && aw.award_info.goods_name) || "";
          // 匹配规则：指定了 awardId 就按 id；否则按 goods_name 含 keyword
          if (S.cfg.awardId != null) {
            if (Number(aw.award_id) !== Number(S.cfg.awardId)) continue;
          } else if (nm.indexOf(S.cfg.keyword) < 0) {
            continue;
          }
          seen.push({ label: sl.current_times, start: stM, status: aw.status,
                      award: aw.award_id, inMs: stM - srvNow });
          if (!best || stM < best.startMs) {
            best = { awardId: aw.award_id, startMs: stM, endMs: enM,
                     status: aw.status, name: nm, entryId: aw.entry_id,
                     consume: aw.consume_card, slotLabel: sl.current_times };
          }
        }
      }
      log({ ev: "slots", seen: seen.slice(0, 12) });
      if (!best) { cb("no upcoming slot with '" + S.cfg.keyword + "'"); return; }
      // 保险：如果最近场次还有很久才开，很可能选错了场（比如选到明天 08:00）。
      // 阈值给 90 分钟：场次间隔最大 10 小时，但「下一场」正常在 90 分钟内
      // （00:00 场在 22:00 之后约 2 小时；实测提前 60 分钟武装是正常场景，
      //  2026-10-26 就被 30 分钟的阈值误伤过）。
      var waitMin = Math.round((best.startMs - srvNow) / 60000);
      if (!S.cfg.fireAtMs && waitMin > (S.cfg.maxWaitMin || 120)) {
        cb("nearest slot '" + best.slotLabel + "' is " + waitMin +
           " min away — refusing to arm on it (wrong slot?)");
        return;
      }
      S.awardId = best.awardId;
      S.slot = best;
      S.fireAt = S.cfg.fireAtMs || best.startMs;
      log({ ev: "award", slot: best, fireIn: S.fireAt - srvNow, waitMin: waitMin });
      cb(null);
    });
  }

  function resolveAddress(cb) {
    req("GET", API.addr, null, function (err, st, j, raw) {
      if (err) { cb("addr " + err); return; }
      var list = Array.isArray(j) ? j : (j && j.result) || [];
      if (!list || !list.length) { cb("no delivery address on this account"); return; }
      var pick = null;
      for (var i = 0; i < list.length; i++) if (String(list[i].is_default) === "1") pick = list[i];
      if (!pick) pick = list[0];
      S.addressId = pick.address_id;
      S.addressSnapshotId = pick.address_snapshot_id;
      log({ ev: "addr", id: S.addressId, snap: S.addressSnapshotId, who: pick.name });
      cb(null);
    });
  }

  function recheck(cb) {
    req("POST", API.query, {}, function (err, st, j) {
      try {
        var scenes = (j && j.result && j.result.exchange_scene_list) || [];
        for (var i = 0; i < scenes.length; i++) {
          if (scenes[i].scene_key !== "BIDDING_GOODS_EXCHANGE") continue;
          var slots = scenes[i].time_award_stock_result_list || [];
          for (var a = 0; a < slots.length; a++) {
            var aws = slots[a].awards || [];
            for (var b = 0; b < aws.length; b++) {
              if (aws[b].award_id === S.awardId &&
                  (aws[b].status === 11 || aws[b].status === "11")) {
                S.result = { ok: true, why: "status=EXCHANGED after ambiguous shot" };
                S.state = "done";
                return cb(true);
              }
            }
          }
        }
      } catch (e) {}
      cb(false);
    });
  }

  // retry=true only for the single sequential chain that follows the burst,
  // so the burst itself does not spawn one retry chain per shot.
  function fireShot(n, gen, retry) {
    if (gen !== S.gen || (S.result && S.result.ok)) return;
    S.state = "firing";
    S.attempts += 1;
    var t0 = nowSrv();
    req("POST", API.xchg, {
      award_id: S.awardId,
      address_id: S.addressId,
      address_snapshot_id: S.addressSnapshotId
    }, function (err, st, j, raw, rtt) {
      if (gen !== S.gen) return;
      var rec = { n: n, srv: t0, drift: t0 - S.fireAt, http: st, rtt: rtt,
                  err: err || null, raw: String(raw).slice(0, 300) };
      if (j && j.result) { rec.ec = j.result.error_code; rec.ok = j.result.success === true; }
      log({ ev: "shot", shot: rec });

      if (rec.ok) { S.result = { ok: true, shot: rec }; S.state = "done"; report("status", S.status()); flush(); return; }
      if (String(rec.ec) === "11") {
        S.result = { ok: false, why: "already exchanged (status 11)" };
        S.state = "done"; return;
      }
      if (!retry) return;

      if (S.attempts >= S.cfg.maxAttempts) {
        S.result = { ok: false, why: "attempts exhausted", last: rec };
        S.state = "done"; report("status", S.status()); setTimeout(flush, 200); return;
      }
      if (err) {
        setTimeout(function () {
          recheck(function (done) {
            if (gen !== S.gen) return;
            if (!done) setTimeout(function () { fireShot(n + 1, gen, true); }, S.cfg.gapMs);
          });
        }, Math.max(S.cfg.gapMs, 300));
      } else {
        setTimeout(function () { fireShot(n + 1, gen, true); }, S.cfg.gapMs);
      }
    });
  }

  // One probe BEFORE the slot opens. The 08:00 run only ever produced 250013, so
  // we never learned what "not yet open" looks like on this endpoint. If this
  // probe returns 250005 (未到兑换时间) then 250013 really does mean "gone" and
  // the model is right; if it also returns 250013, then 250013 means "not
  // applicable" and something upstream of the exchange is missing.
  function probeDiag(gen) {
    req("POST", API.xchg, {
      award_id: S.awardId,
      address_id: S.addressId,
      address_snapshot_id: S.addressSnapshotId
    }, function (err, st, j, raw, rtt) {
      if (gen !== S.gen) return;
      var rec = { n: "diag", srv: nowSrv(), drift: nowSrv() - S.fireAt,
                  http: st, rtt: rtt, err: err || null, raw: String(raw).slice(0, 300) };
      if (j && j.result) rec.ec = j.result.error_code;
      log({ ev: "diag", shot: rec });
    });
  }

  function schedule(gen) {
    if (gen !== S.gen) return;
    S.state = "armed";
    var left = S.fireAt - nowSrv();

    // Far out: re-check on a short chain instead of one long setTimeout, because
    // a backgrounded WebView clamps timers (intensive throttling -> 1/min).
    if (left > 100000) {
      setTimeout(function () {
        if (gen === S.gen && S.state !== "done") schedule(gen);
      }, Math.min(Math.max(left - 95000, 5000), 20000));
      return;
    }

    // Re-sync the clock once we are close: an offset measured an hour early can
    // drift by more than the race margin.
    if (!S.resynced) {
      S.resynced = true;
      syncTime(function (e) {
        if (gen !== S.gen) return;
        if (e) { fail(e); return; }
        log({ ev: "resync", offset: S.offset, rtt: S.rtt });
        schedule(gen);
      });
      return;
    }

    var offs = buildBurst(S.cfg);
    var first = S.fireAt + offs[0];

    (function approach() {
      if (gen !== S.gen || S.state === "done") return;

      // fire the diagnostic probe once, well before the boundary
      if (S.cfg.diagMs > 0 && !S.diagDone && nowSrv() >= S.fireAt - S.cfg.diagMs) {
        S.diagDone = true;
        probeDiag(gen);
      }

      var l = first - nowSrv();
      if (l > 2000) { setTimeout(approach, Math.min(l - 500, 250)); return; }

      // Spin through the ladder, firing each shot as its offset is reached.
      // Spinning (not setTimeout) keeps us exact even if timers are throttled.
      var i = 0;
      while (i < offs.length && gen === S.gen && S.state !== "done") {
        var target = S.fireAt + offs[i];
        while (gen === S.gen && nowSrv() < target) { /* spin */ }
        if (gen !== S.gen || S.state === "done") return;
        fireShot(i, gen, false);
        i++;
      }
      if (gen === S.gen && S.state !== "done") {
        // 首发之后接串行重试链。间隔必须用 gapMs（默认 900ms）——
        // 原来的 120ms 会立刻触发限流（250017），等于白发。
        setTimeout(function () { fireShot(offs.length, gen, true); }, S.cfg.gapMs);
      }
    })();
  }

  S.arm = function (cfg) {
    S.cfg = {
      keyword: "酷态科", maxAttempts: 120, gapMs: 0, fireAtMs: null, dryRun: false,
      // **单发首发**（T0 那一刻），不再打密梯 —— 间隔 <800ms 会被限流，
      // 详见 buildBurst 上方 2026-10-27 的实测记录。
      // 首发之后由 fireShot 的串行重试链按 gapMs(900ms) 续打，
      // 直到命中窗口（错误码从 250013 变成 250009/成功）或 attempts 用尽。
      burstOffsetsMs: null, burstCount: 1, burstFromMs: -1000, burstToMs: 0,
      // one probe this many ms BEFORE the boundary, to learn the "not open" code
      diagMs: 30000,
      awardId: null, maxWaitMin: 120,
      // cloud telemetry: {url, token}
      reportCfg: null
    };
    for (var k in cfg) if (Object.prototype.hasOwnProperty.call(cfg, k)) S.cfg[k] = cfg[k];
    S.gen += 1;
    var gen = S.gen;
    S.state = "syncing"; S.log = []; S.result = null; S.attempts = 0; S.err = null;
    S.resynced = false; S.diagDone = false;
    S.reportCfg = S.cfg.reportCfg || null;
    log({ ev: "arm", gen: gen, cfg: S.cfg });
    syncTime(function (e1) {
      if (gen !== S.gen) return;
      if (e1) { fail(e1); return; }
      S.state = "resolving";
      resolveAward(function (e2) {
        if (gen !== S.gen) return;
        if (e2) { fail(e2); return; }
        resolveAddress(function (e3) {
          if (gen !== S.gen) return;
          if (e3) { fail(e3); return; }
          if (S.cfg.dryRun) {
            S.state = "armed";
            log({ ev: "dryrun", fireAt: S.fireAt, inMs: S.fireAt - nowSrv() });
            return;
          }
          schedule(gen);
        });
      });
    });
    return "arming gen=" + gen;
  };

  S.disarm = function () { S.state = "idle"; S.gen += 1; return "disarmed"; };

  S.status = function () {
    return {
      state: S.state, err: S.err, offset: S.offset, rtt: S.rtt,
      fireAt: S.fireAt, inMs: S.fireAt ? S.fireAt - nowSrv() : null,
      awardId: S.awardId, addressId: S.addressId, slot: S.slot,
      attempts: S.attempts, result: S.result, log: S.log.slice(-30)
    };
  };

  return "installed v" + VERSION;
})()
