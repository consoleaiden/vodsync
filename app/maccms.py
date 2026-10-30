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
import time
import urllib.parse
from typing import Optional

import httpx

from . import config

log = logging.getLogger("vodsync.maccms")


class MacCmsError(Exception):
    """源站接口异常。"""


def build_url(api: str, params: dict) -> str:
    """拼接查询参数，兼容 api 地址末尾带不带 / 或已带 ?a=b 的情况。"""
    sep = "&" if "?" in api else "?"
    qs = urllib.parse.urlencode(params)
    return f"{api.rstrip('/')}{sep}{qs}" if not api.endswith("?") else f"{api}{qs}"


class MacCmsClient:
    """线程安全的同步 HTTP 客户端（连接池复用）。"""

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

    def _get_json(self, url: str) -> dict:
        """带重试与限速的 GET，返回 JSON dict。"""
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                time.sleep(config.REQUEST_INTERVAL_SECONDS + random.random() * 0.2)
                resp = self._client.get(url)
                if resp.status_code == 403:
                    raise MacCmsError(f"HTTP 403 (源站拒绝访问): {url}")
                resp.raise_for_status()
                # 部分源站返回 GBK/带 BOM 的 JSON，做容错解码
                return json.loads(resp.content.decode("utf-8", errors="replace").lstrip("﻿"))
            except (httpx.HTTPError, json.JSONDecodeError, MacCmsError) as e:
                last_err = e
                wait = min(2 ** attempt, 15)
                log.warning("请求失败(%d/%d) %s: %s，%ds 后重试", attempt, self.retries, url, e, wait)
                time.sleep(wait)
        raise MacCmsError(f"重试 {self.retries} 次仍失败: {url} ({last_err})") from last_err

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
