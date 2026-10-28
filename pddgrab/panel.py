"""panel — 活动接口封装（都在页面 JS 上下文里发，原生层自动签名）

为什么必须在页面里发：拼多多原生 App ≥4.69 的 axios 拦截器对
`useNativeAntiToken` 的请求**不做 JS 签名** —— anti_content 由原生网络层注入。
所以脱离 WebView 用 curl 发会被判非法；在页面里发普通 XHR 就行。

接口（前缀 `/proxy/api/`，页面里写的是相对路径 `api/...`）：
    GET  api/server/_stm                                    服务器时间（对时）
    POST api/carnival/subtract_card/query_time_awards        场次+奖品表
    GET  api/origenes/addresses                              收货地址
    POST api/carnival/subtract_card/check_address            地址校验
    POST api/carnival/subtract_card/exchange_time_award      **兑换（就是它）**
"""
from __future__ import annotations

import json
import time

from . import config
from .webview import unwrap

P = "/proxy/api/api"


def _xhr(bridge, sel, method: str, path: str, body=None, timeout_ms: int = 8000,
         wait_s: float = 6.0):
    """在页面里发一个 XHR 并把结果读回来。返回 dict 或 None。"""
    body_js = "null" if body is None else json.dumps(json.dumps(body))
    js = f"""
(function(){{
  window.__pddr = null;
  var x = new XMLHttpRequest();
  x.open({json.dumps(method)}, {json.dumps(path)}, true);
  x.timeout = {timeout_ms};
  if ({json.dumps(method)} !== 'GET') x.setRequestHeader('Content-Type','application/json;charset=UTF-8');
  x.onload = function(){{ var j=null; try{{ j=JSON.parse(x.responseText); }}catch(e){{}}
    window.__pddr = {{ http: x.status, json: j, raw: x.responseText.slice(0, 4000) }}; }};
  x.onerror   = function(){{ window.__pddr = {{ err: 'NET' }}; }};
  x.ontimeout = function(){{ window.__pddr = {{ err: 'TIMEOUT' }}; }};
  x.send({body_js});
  return 'sent';
}})()
"""
    bridge.evaljs(sel, js)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.2)
        v = unwrap(bridge.evaljs(sel, "JSON.stringify(window.__pddr)"))
        if isinstance(v, dict) and v:
            return v
    return None


# ---------------------------------------------------------------- 基础接口
def server_time(bridge, sel):
    return _xhr(bridge, sel, "GET", f"{P}/server/_stm")


def query_time_awards(bridge, sel):
    """拿场次+奖品表。返回 result 字典或 None。"""
    r = _xhr(bridge, sel, "POST", f"{P}/carnival/subtract_card/query_time_awards", {})
    if not r or not r.get("json"):
        return None
    return (r["json"].get("result") or {})


def addresses(bridge, sel):
    r = _xhr(bridge, sel, "GET", f"{P}/origenes/addresses")
    if not r:
        return []
    j = r.get("json")
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        return j.get("result") or []
    return []


def default_address(bridge, sel):
    lst = addresses(bridge, sel)
    if not lst:
        return None
    for a in lst:
        if str(a.get("is_default")) == "1":
            return a
    return lst[0]


def check_address(bridge, sel, award_id, address_id):
    r = _xhr(bridge, sel, "POST", f"{P}/carnival/subtract_card/check_address",
             {"award_id": award_id, "address_id": address_id})
    if not r or not r.get("json"):
        return None
    return (r["json"].get("result") or {})


def exchange(bridge, sel, award_id, addr, wait_s: float = 6.0):
    """兑换（= 下单）。返回 (error_code, raw_response)。

    ⚠️ 参数与官方前端**逐字一致**：
        exchangeAwards({award_id, address_id, address_snapshot_id})
    """
    body = {"award_id": award_id,
            "address_id": addr.get("address_id"),
            "address_snapshot_id": addr.get("address_snapshot_id")}
    r = _xhr(bridge, sel, "POST", f"{P}/carnival/subtract_card/exchange_time_award",
             body, wait_s=wait_s)
    if not r:
        return None, None
    if r.get("err"):
        return r["err"], r.get("raw")
    j = r.get("json") or {}
    res = j.get("result") or {}
    return res.get("error_code"), r.get("raw")


def balance(bridge, sel):
    """读页面上的「我的减减卡」余额（页面不刷新时会读到旧值）。"""
    js = (r"(function(){var t=document.body.innerText||'';"
          r"var m=t.match(/我的减减卡[:：]?\s*(\d{1,6})/);return m?m[1]:'?'})()")
    v = unwrap(bridge.evaljs(sel, js))
    return str(v).strip('"') if v is not None else "?"


def refresh_page(bridge, sel) -> str:
    """重载页面并等它就绪，返回新的 index（WebView 下标可能变）。"""
    bridge.evaljs(sel, "location.reload()")
    time.sleep(12)
    return bridge.resolve_target()
