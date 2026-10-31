"""
苹果 CMS（MacCMS）采集接口客户端。

接口约定（业内通用格式）：
  列表（轻量，只含 id/分类/名称/时间）: {api}?ac=list&pg={page}
  详情（全字段，可翻页）              : {api}?ac=detail&pg={page}
  按 ID 批量取详情                   : {api}?ac=detail&ids=1,2,3

返回 JSON 结构：
  {"code":1, "total":n, "page":p, "pagecount":m,
   "class":[{"type_id":1,"type_name":"电影"}],
   "list":[{...vod 字段...}]}
"""
import json
import logging
import random
import threading
import time
import urllib.parse
from typing import Optional

import httpx

from . import config

log = logging.getLogger("vodsync.maccms")


class MacCmsError(Exception):
    """源站接口异常。"""


class _RouteFailed(Exception):
    """
    当前路由（直连或某个代理）不可用，应立即切换下一条，不做无意义重试。
    触发场景：连接失败/连接超时/读取超时（有代理可切时）/HTTP 403·429·451。
    """


def build_url(api: str, params: dict) -> str:
    """拼接查询参数，兼容 api 地址末尾带不带 / 或已带 ?a=b 的情况。"""
    sep = "&" if "?" in api else "?"
    qs = urllib.parse.urlencode(params)
    return f"{api.rstrip('/')}{sep}{qs}" if not api.endswith("?") else f"{api}{qs}"


# ---------------------------------------------------------------------------
# 中转代理：路由拼装、偏好记忆、并发闸门、观测计数
#
# 目标地址必须整体 URL 编码后再作为 url 参数传入，否则上游参数里的 & 会被
# 代理当成自己的参数截断（这是接中转代理最常见的坑）。
# ---------------------------------------------------------------------------
_proxy_pref: dict = {}          # host -> (proxy_base, 记录时间戳)
_pref_lock = threading.Lock()
_proxy_sems: dict = {}          # proxy_base -> Semaphore
_sem_lock = threading.Lock()
_proxy_stats = {"direct_success": 0, "proxy_success": 0, "route_fail": 0}
_stats_lock = threading.Lock()


def wrap_proxy(proxy_base: str, target: str) -> str:
    """把目标地址包装成代理请求 URL：{proxy}?url=<urlencode(target)>。"""
    sep = "&" if "?" in proxy_base else "?"
    return f"{proxy_base}{sep}url={urllib.parse.quote(target, safe='')}"


def _proxy_semaphore(proxy_base: str) -> threading.Semaphore:
    with _sem_lock:
        sem = _proxy_sems.get(proxy_base)
        if sem is None:
            sem = threading.Semaphore(max(1, config.PROXY_CONCURRENCY))
            _proxy_sems[proxy_base] = sem
        return sem


def get_proxy_pref(host: str) -> str:
    """返回该主机当前记忆的代理地址（过期则清除并返回空串）。"""
    with _pref_lock:
        item = _proxy_pref.get(host)
    if not item:
        return ""
    base, ts = item
    if time.time() - ts > config.PROXY_PREF_TTL_MINUTES * 60:
        with _pref_lock:
            _proxy_pref.pop(host, None)
        log.info("[proxy] %s 的代理记忆已过期，恢复直连优先", host)
        return ""
    return base


def set_proxy_pref(host: str, proxy_base: str):
    """记录/清除"该主机需走代理"（proxy_base 为空表示直连成功，清除记忆）。"""
    with _pref_lock:
        if proxy_base:
            _proxy_pref[host] = (proxy_base, time.time())
        else:
            _proxy_pref.pop(host, None)


def proxy_snapshot() -> dict:
    """当前代理配置与偏好快照（管理接口/排障展示用）。"""
    now = time.time()
    with _pref_lock:
        pref = {
            host: {"proxy": base, "age_seconds": round(now - ts, 1)}
            for host, (base, ts) in _proxy_pref.items()
        }
    with _stats_lock:
        stats = dict(_proxy_stats)
    return {
        "enabled": config.PROXY_ENABLED,
        "urls": list(config.PROXY_URLS),
        "preferred_hosts": pref,
        **stats,
    }


def _bump(key: str):
    with _stats_lock:
        _proxy_stats[key] = _proxy_stats.get(key, 0) + 1


class MacCmsClient:
    """线程安全的同步 HTTP 客户端（连接池复用，直连失败自动走中转代理）。"""

    def __init__(self, timeout: int = None, retries: int = None):
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.retries = retries if retries is not None else config.REQUEST_RETRIES
        self._client = httpx.Client(
            timeout=self.timeout,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json, text/plain, */*",
            },
            follow_redirects=True,
            limits=httpx.Limits(max_connections=config.PAGE_CONCURRENCY * 2, keepalive_expiry=30),
        )

    def close(self):
        self._client.close()

    # ------------------------------------------------------------------
    # 路由选择：记忆中的代理 → 直连 → 其余代理
    # ------------------------------------------------------------------
    def _routes(self, host: str) -> list:
        """返回按优先级排列的路由列表：[(kind, proxy_base)]，kind ∈ direct/proxy。"""
        proxies = list(config.PROXY_URLS) if (config.PROXY_ENABLED and config.PROXY_URLS) else []
        pref = get_proxy_pref(host) if proxies else ""
        routes = []
        if pref:
            routes.append(("proxy", pref))
        routes.append(("direct", ""))
        for p in proxies:
            if p != pref:
                routes.append(("proxy", p))
        return routes

    def _get_json(self, url: str) -> dict:
        """
        获取 JSON：按路由顺序尝试，任一路由成功即返回。

        失败切换策略（避免在不可达源站上白等重试）：
          - 连接失败/连接超时 → 立即切下一条路由
          - HTTP 403/429/451   → 立即切下一条路由（多为风控/封禁，代理可能绕过）
          - 其它错误（5xx、JSON 解析）→ 在当前路由重试，仍失败再切换
        直连成功会清除代理记忆，代理成功则记住 30 分钟。
        """
        host = urllib.parse.urlsplit(url).netloc.lower()
        errors = []
        for kind, base in self._routes(host):
            try:
                data = self._request(url, kind, base)
            except (_RouteFailed, MacCmsError) as e:
                label = "直连" if kind == "direct" else f"代理 {base}"
                errors.append(f"{label}: {e}")
                log.warning("[%s] %s 失败: %s", host, label, e)
                continue
            if kind == "proxy":
                _bump("proxy_success")
                if get_proxy_pref(host) != base:
                    log.info("[%s] 直连不可用，已切换代理: %s（记忆 %d 分钟）",
                             host, base, config.PROXY_PREF_TTL_MINUTES)
                set_proxy_pref(host, base)
            else:
                _bump("direct_success")
                if get_proxy_pref(host):
                    log.info("[%s] 直连已恢复，取消代理路由", host)
                set_proxy_pref(host, "")
            return data

        _bump("route_fail")
        raise MacCmsError(
            f"全部路由均失败（直连 + {len(config.PROXY_URLS) if config.PROXY_ENABLED else 0} 个代理）: "
            + " | ".join(errors)
        )

    def _request(self, url: str, kind: str, proxy_base: str) -> dict:
        """在指定路由上请求（含重试与限速）。"""
        target = url if kind == "direct" else wrap_proxy(proxy_base, url)
        timeout = self.timeout if kind == "direct" else min(self.timeout, config.PROXY_TIMEOUT)
        sem = _proxy_semaphore(proxy_base) if kind == "proxy" else None
        has_alt_route = bool(config.PROXY_ENABLED and config.PROXY_URLS) or bool(proxy_base)
        last_err: Optional[Exception] = None

        for attempt in range(1, self.retries + 1):
            try:
                time.sleep(config.REQUEST_INTERVAL_SECONDS + random.random() * 0.2)
                if sem is not None:
                    with sem:                      # 代理并发闸门，避免打挂中转服务
                        resp = self._client.get(target, timeout=timeout)
                else:
                    resp = self._client.get(target, timeout=timeout)

                if resp.status_code in (403, 429, 451):
                    # 风控/限流：当前路由重试无意义，立即换路由
                    raise _RouteFailed(f"HTTP {resp.status_code}（拒绝访问/限流）")
                resp.raise_for_status()
                # 部分源站返回 GBK/带 BOM 的 JSON，做容错解码
                data = json.loads(resp.content.decode("utf-8", errors="replace").lstrip("﻿"))
                if not isinstance(data, dict):
                    raise MacCmsError("响应不是 JSON 对象（可能命中了错误页）")
                return data

            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # 主机不可达：换路由，不浪费重试次数
                raise _RouteFailed(f"连接失败: {e}") from e

            except (httpx.ReadTimeout, httpx.PoolTimeout) as e:
                # 读取超时：还有代理可切换时立即切，否则按普通失败重试
                if has_alt_route:
                    raise _RouteFailed(f"读取超时: {e}") from e
                last_err = e
                if attempt >= self.retries:
                    break
                wait = min(2 ** attempt, 15)
                log.warning("请求超时(%d/%d) %s，%ds 后重试", attempt, self.retries, target[:100], wait)
                time.sleep(wait)

            except (httpx.HTTPError, json.JSONDecodeError, MacCmsError) as e:
                last_err = e
                if attempt >= self.retries:
                    break
                wait = min(2 ** attempt, 15)
                log.warning("请求失败(%d/%d) %s: %s，%ds 后重试", attempt, self.retries, target, e, wait)
                time.sleep(wait)

        raise MacCmsError(f"重试 {self.retries} 次仍失败: {target[:120]} ({last_err})") from last_err

    def fetch_page(self, api: str, ac: str, pg: int) -> dict:
        """按页获取 ac=list 或 ac=detail 数据。"""
        return self._get_json(build_url(api, {"ac": ac, "pg": pg}))

    def fetch_detail_by_ids(self, api: str, ids: list) -> dict:
        """按 ID 批量获取详情（单批建议 <= 50 个 id）。"""
        return self._get_json(build_url(api, {"ac": "detail", "ids": ",".join(map(str, ids))}))

    # ------------------------------------------------------------------
    # 字段提取：只保留关键字段，忽略其他次要字段
    # ------------------------------------------------------------------
    @staticmethod
    def extract_row(item: dict, class_map: dict) -> Optional[dict]:
        """从 vod 条目中提取入库所需的关键字段。"""
        try:
            vod_id = int(item.get("vod_id", 0))
        except (TypeError, ValueError):
            return None
        if not vod_id:
            return None
        type_id = int(item.get("type_id") or 0)
        return {
            "vod_id": vod_id,
            "type_id": type_id,
            "type_name": (class_map.get(type_id) or item.get("type_name") or "").strip(),
            "name": (item.get("vod_name") or "").strip(),
            "pic": (item.get("vod_pic") or "").strip(),
            "remarks": (item.get("vod_remarks") or "").strip()[:120],
            "content": (item.get("vod_content") or "").strip(),
            "play_url": (item.get("vod_play_url") or "").strip(),
            "vod_time": (item.get("vod_time") or "").strip()[:19],
        }
