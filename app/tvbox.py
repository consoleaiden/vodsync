# ---------------------------------------------------------------------------
# v1.20.0 TVBox 配置接口
#
# 把本服务的源站清单转换成 TVBox 兼容配置 JSON：
#   { "sites": [...], "lives": [...], "spider": ..., "wallpaper": ... }
#
# 站点类型用 type=1（苹果 CMS JSON 采集站）：本服务本来就提供
# `/api/v1/{slug}?ac=list|detail` 兼容接口（分类/分页/搜索/详情全部齐备），
# TVBox 对 type=1 站点会自动追加 ac / pg / wd / ids 参数，直接可用。
#
# 鉴权约定（TVBox 是纯客户端，无法带 Authorization 头）：
#   - 配置 URL 带 ?token=xxx（Token 本来就支持 query 参数）；
#   - 生成的 sites[].api 自动携带同一枚 Token，TVBox 后续请求免配置。
#   - AUTH_ENABLED=false 的开放部署则什么都不带。
# ---------------------------------------------------------------------------
import logging
from urllib.parse import quote

from fastapi import Request
from sqlalchemy import select

from . import auth, config, database, minor_mode
from .models import sources

log = logging.getLogger("vodsync.tvbox")

# TVBox 站点能力位：searchable=参与首页搜索；filterable=分类筛选可用。
# quickSearch 受配置控制（源站多时并行搜索会打爆本机/源站）。
SITE_FLAGS = {"searchable": 1, "filterable": 1}

# TVBox 顶层 "flags" 是解析嗅探标记（TVBox 约定俗成），与站点能力无关。
TOP_FLAGS = ["省", "feizhu"]


def base_url(request: Request = None) -> str:
    """
    生成站点 api 的对外前缀。

    优先级：TVBOX_BASE_URL 环境变量 > 反代头（X-Forwarded-Proto/Host）
    > 请求自身 scheme/host。self-hosted 直连场景后两者结果一致。
    """
    if config.TVBOX_BASE_URL:
        return config.TVBOX_BASE_URL
    if request is None:
        return ""
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip()
    forwarded_host = (request.headers.get("x-forwarded-host")
                      or request.headers.get("host") or "").strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}"
    return str(request.base_url).rstrip("/")


def _site_api_token(request: Request) -> str:
    """
    站点 api 需要携带的 Token。

    取请求自带 Token（推荐：配置 URL 就带 ?token=）；没带但开了鉴权时
    回退到存量单 Token（面板/环境变量那枚），保证「面板里复制出来的
    配置地址」拿来就能用。开放部署（鉴权关闭）返回空串。
    """
    if not auth.is_enabled():
        return ""
    supplied = auth.extract_token(request)
    if supplied:
        return supplied
    return auth.get_token() or ""


def _with_token(api: str, token: str) -> str:
    if not token:
        return api
    sep = "&" if "?" in api else "?"
    return f"{api}{sep}token={quote(token, safe='')}"


def build_config(request: Request = None, slugs=None, quick_search: bool = None) -> dict:
    """
    生成 TVBox 兼容配置。

    - slugs：非空时只输出指定源站（逗号分隔，TVBox 配置 URL ?slug=a,b）；
    - quick_search：None=跟随配置；False 强制关闭 quickSearch。
    输出口径：
      sites     每个可用源站一个 type=1 站点，api=本服务 MacCMS 兼容接口
      lives     TVBOX_LIVE_URL 非空时下发一个 type=0 直播组，否则空数组
      spider    TVBOX_SPIDER（可为空串）
      wallpaper TVBOX_WALLPAPER（非空才下发）
    未成年模式（X-Minor-Mode: 1 / ?minor_mode=1）下 18+ 源站照常剔除。
    """
    if quick_search is None:
        quick_search = config.TVBOX_QUICK_SEARCH

    slug_set = None
    if slugs:
        slug_set = {s.strip().lower() for s in str(slugs).split(",") if s.strip()}

    cond = [
        sources.c.enabled == 1,
        sources.c.status == "ok",
        sources.c.total_videos >= max(0, config.TVBOX_MIN_VIDEOS),
    ]
    minor_clause = minor_mode.source_clause(request)
    if minor_clause is not None:
        cond.append(minor_clause)

    sites = []
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(sources.c.slug, sources.c.name, sources.c.total_videos)
                .where(*cond).order_by(sources.c.id)
            ).mappings().all()
    except Exception as e:  # noqa: BLE001
        # 配置接口挂了不能把 TVBox 拒之门外：降级为空 sites 并记日志
        log.warning("[tvbox] 源站清单查询失败，降级输出空 sites: %s", e)
        rows = []

    base = base_url(request)
    token = _site_api_token(request)
    for r in rows:
        slug = r["slug"]
        if slug_set is not None and slug not in slug_set:
            continue
        api = _with_token(f"{base}/api/v1/{slug}", token)
        sites.append({
            "key": f"vodsync_{slug}",
            "name": r["name"] or slug,
            "type": 1,
            "api": api,
            **SITE_FLAGS,
            "quickSearch": 1 if quick_search else 0,
        })

    cfg = {
        "sites": sites,
        "lives": _build_lives(),
        "spider": config.TVBOX_SPIDER or "",
        "flags": TOP_FLAGS,
    }
    if config.TVBOX_WALLPAPER:
        cfg["wallpaper"] = config.TVBOX_WALLPAPER
    if slug_set is not None:
        cfg["requested_slugs"] = sorted(slug_set)
    return cfg


def _build_lives() -> list:
    if not config.TVBOX_LIVE_URL:
        return []
    return [{
        "name": "直播",
        "type": 0,
        "url": config.TVBOX_LIVE_URL,
        "playerType": 1,  # 用内置播放器播 m3u8 直链，避免外层 IJK 配置问题
    }]
