"""
SeleneTV 直播源融合 —— 每日从 vbskycn/iptv 拉取 m3u 入库（只增不改）。

职责：
  - fetch_m3u(): 拉主源，失败切备源；校验 #EXTM3U 头与大小上限。
  - parse_epg_url(): 从 m3u 头 x-tvg-url 提取 EPG 地址。
  - count_channels(): 统计 #EXTINF 行数。
  - sync_once(): 拉取→解析→upsert 缓存表→确保订阅记录。幂等，失败保留旧数据。

所有异常都在此模块内吞掉并记日志，绝不向上抛影响主服务或其他定时任务。
"""
import logging
import os
import re
import time
from typing import Optional, Tuple

from . import repository as repo

log = logging.getLogger("vodsync.selenetv.live_sync")

# 默认上游（raw.githubusercontent 为权威源，live.zbds.top 为同项目 CDN 备源）
DEFAULT_M3U_URL = (
    "https://raw.githubusercontent.com/vbskycn/iptv/refs/heads/master/tv/iptv4.m3u"
)
DEFAULT_M3U_FALLBACK = "https://live.zbds.top/tv/iptv4.m3u"

LIVE_KEY = "iptv4"
LIVE_NAME = "IPTV4 直播源"
MAX_M3U_BYTES = 2 * 1024 * 1024  # 2MB 上限，防超大响应

_TVG_URL_RE = re.compile(r'x-tvg-url="([^"]+)"')
_EXTINF_RE = re.compile(r"^#EXTINF", re.MULTILINE)


def _primary_url() -> str:
    return os.environ.get("SELENETV_LIVE_M3U_URL", DEFAULT_M3U_URL).strip() or DEFAULT_M3U_URL


def _fallback_url() -> str:
    return os.environ.get("SELENETV_LIVE_M3U_URL_FALLBACK", DEFAULT_M3U_FALLBACK).strip()


def _sync_enabled() -> bool:
    return os.environ.get("SELENETV_LIVE_SYNC_ENABLED", "true").strip().lower() == "true"


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
        if "\ufffd" not in text:
            return text
    except UnicodeDecodeError:
        pass
    return data.decode("gbk", errors="replace")


def _fetch_one(url: str, timeout: float) -> str:
    """用 vodsync 已有的 httpx 拉取单个地址，返回文本；失败抛异常。"""
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; vodsync-selenetv-livesync/1.0)",
                "Accept": "application/vnd.apple.mpegurl, text/plain, */*",
                "Cache-Control": "no-cache",
            },
        )
        resp.raise_for_status()
        raw = resp.content
    if len(raw) > MAX_M3U_BYTES:
        raise ValueError(f"M3U 内容过大: {len(raw)} > {MAX_M3U_BYTES}")
    text = _decode(raw)
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("响应不是合法 M3U（缺少 #EXTM3U 头）")
    return text


def fetch_m3u() -> Tuple[str, str]:
    """返回 (m3u文本, 实际使用的url)。主源失败自动切备源。"""
    timeout = float(os.environ.get("SELENETV_LIVE_FETCH_TIMEOUT", "20"))
    primary = _primary_url()
    try:
        return _fetch_one(primary, timeout), primary
    except Exception as exc:  # noqa: BLE001
        log.warning("[直播源] 主源拉取失败(%s): %s，尝试备源", primary, exc)
    fallback = _fallback_url()
    if fallback and fallback != primary:
        try:
            return _fetch_one(fallback, timeout), fallback
        except Exception as exc:  # noqa: BLE001
            log.warning("[直播源] 备源拉取失败(%s): %s", fallback, exc)
    raise RuntimeError("主源与备源均拉取失败")


def parse_epg_url(content: str) -> str:
    m = _TVG_URL_RE.search(content)
    return m.group(1).strip() if m else ""


def count_channels(content: str) -> int:
    return len(_EXTINF_RE.findall(content))


def sync_once() -> dict:
    """
    执行一次拉取与入库。返回 {"ok":bool, "channels":int, "url":str, "error":str}。
    失败时保留数据库里的旧数据不清空。
    """
    if not _sync_enabled():
        log.info("[直播源] SELENETV_LIVE_SYNC_ENABLED=false，跳过拉取")
        return {"ok": False, "skipped": True, "channels": 0, "url": "", "error": "disabled"}

    try:
        content, used_url = fetch_m3u()
        channels = count_channels(content)
        if channels <= 0:
            raise ValueError("M3U 中未解析到任何频道（#EXTINF=0）")
        epg = parse_epg_url(content)

        repo.live_playlist_upsert(
            key=LIVE_KEY,
            name=LIVE_NAME,
            url=used_url,
            content=content,
            channel_count=channels,
            fetched_at=int(time.time() * 1000),
        )
        # 订阅记录 url 用 local: 前缀标记，router 据此按请求 Host 动态生成本地 M3U 端点绝对地址。
        repo.live_source_ensure(
            key=LIVE_KEY, name=LIVE_NAME, url="local:1", epg=epg, ua="", sort_order=100,
        )
        log.info("[直播源] 拉取成功: 源=%s 频道数=%d EPG=%s", used_url, channels, epg or "无")
        return {"ok": True, "channels": channels, "url": used_url, "error": ""}
    except Exception as exc:  # noqa: BLE001
        log.exception("[直播源] 拉取失败（保留旧数据）: %s", exc)
        return {"ok": False, "channels": 0, "url": "", "error": str(exc)}


if __name__ == "__main__":
    # 手动拉取入口：python -m app.selenetv.live_sync
    print(sync_once())
