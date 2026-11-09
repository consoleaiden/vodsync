"""
工具函数：播放地址解析、分页参数处理。

注：API Key 校验 v1.5.0 起统一收敛到 app/auth.py（支持多 Token、会话、开关）。

v1.11.0 起 `parse_play_url()` 会做线路识别（m3u8 / mp4 / flv / mkv / page / unknown）
并按 `PLAY_LINE_POLICY` 过滤、按 `PLAY_LINE_PREFER_DIRECT` 排序。
这里只做**纯字符串判断**（不发网络请求、不查库），策略来自 app/config.py；
源站级覆盖（sources.play_line_policy）由 app/playline.py 解析后以参数传入。
"""
import logging
import re

from fastapi import HTTPException, Query

from . import config

log = logging.getLogger("vodsync.utils")

# 可播放的媒体直链类型（判断「这条线路能不能直接交给播放器」）
MEDIA_TYPES = ("m3u8", "mp4", "flv", "mkv")
# 详情模式（`ids=` / `ac=detail`）额外带出的列。
# 放这里当唯一真源：api._query_vods 与 multiquery.DETAIL_COLS 都引用它，
# 以后再加详情字段只需改一处，不会出现「某个路径漏带字段 → 该字段恒为空串」。
DETAIL_EXTRA_COLS = (
    "vod_actor", "vod_director", "vod_area", "vod_year", "vod_tag",
    # 播放线路来源（v1.11.0）：线路名里要显示 provider，也要靠它区分直链/分享页
    "play_from",
)
# `page` 之外的都算「可能可播」：unknown 只是没认出来，不代表不能播
_TYPE_RANK = {"m3u8": 0, "mp4": 1, "flv": 2, "mkv": 3, "unknown": 4, "page": 5}
_POLICIES = ("off", "filter", "whitelist")

_EXT_RE = re.compile(r"\.([a-z0-9]{1,5})$")


def _norm_policy(policy) -> str:
    """归一策略名；非法值（含 None）回落到全局配置，再非法则 filter。"""
    p = (policy or "").strip().lower()
    if p in _POLICIES:
        return p
    p = (config.PLAY_LINE_POLICY or "").strip().lower()
    return p if p in _POLICIES else "filter"


# 解析后的配置项缓存（避免每次调用都 split 一遍逗号串）
_cfg_cache: dict = {}


def _drop_patterns() -> tuple:
    raw = config.PLAY_LINE_DROP_PATTERNS or ""
    hit = _cfg_cache.get("drop")
    if hit and hit[0] == raw:
        return hit[1]
    pats = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    _cfg_cache["drop"] = (raw, pats)
    return pats


def _keep_ext() -> frozenset:
    raw = config.PLAY_LINE_KEEP_EXT or ""
    hit = _cfg_cache.get("keep")
    if hit and hit[0] == raw:
        return hit[1]
    exts = frozenset(
        p.strip().lower().lstrip(".") for p in raw.split(",") if p.strip()
    )
    _cfg_cache["keep"] = (raw, exts)
    return exts


def url_path(url: str) -> str:
    """
    取 URL 的 path 部分（小写，不含 query / fragment）。

    不用 urlparse 兜底：源站偶尔给出畸形地址（缺 scheme、多个 `?`），
    urlparse 会返回空 path，反而把 `.m3u8` 结尾的判断弄丢了。
    """
    s = (url or "").strip()
    if not s:
        return ""
    s = s.split("#", 1)[0].split("?", 1)[0]
    # 去掉 scheme://host 部分（有 scheme 时）
    if "://" in s:
        s = s.split("://", 1)[1]
        s = s[s.find("/"):] if "/" in s else "/"
    return s.lower()


def _match_pattern(path: str, pattern: str) -> bool:
    """单条规则匹配：`re:` 前缀按正则，其余按大小写不敏感子串。"""
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], path, re.I) is not None
        except re.error:
            return False
    return pattern in path


def classify_play_line(url: str, provider: str = None, patterns=None) -> str:
    """
    判断一条播放线路的类型：'m3u8' | 'mp4' | 'flv' | 'mkv' | 'page' | 'unknown'。

    判定顺序（只看 URL 的 path，忽略 query —— 带 `?sign=` 的 m3u8 仍是 m3u8）：

      1. path 以 .m3u8 / .mp4 / .flv / .mkv 结尾 → 对应类型
      2. path 命中 PLAY_LINE_DROP_PATTERNS（默认 /share/、/play/、.html、.htm）→ page
      3. 其余 → unknown

    为什么媒体扩展名排在黑名单之前：`https://x/play/<id>/index.m3u8` 这种「/play/ 前缀 +
    真 m3u8」很常见，按需求原文的顺序（先黑名单）会把可播线路误杀成 page。
    以 .m3u8 结尾的地址必然是媒体播放列表，这个证据强于路径前缀。
    反过来的情况（页面地址恰好以 .m3u8 结尾却返回 HTML）实际不存在。

    provider 参数保留给「已知 provider 但没有扩展名」的扩展场景（如 ffm3u8），
    当前实现不使用 —— 线路 URL 本身才是唯一可靠证据。
    """
    if not url:
        return "unknown"
    path = url_path(url)
    ext = _EXT_RE.search(path)
    if ext and ext.group(1) in MEDIA_TYPES:
        return ext.group(1)
    pats = _drop_patterns() if patterns is None else tuple(patterns)
    for pat in pats:
        if _match_pattern(path, pat):
            return "page"
    return "unknown"


def _probe_is_page(url: str) -> bool:
    """
    PLAY_LINE_PROBE_HTML=true 时对 unknown 地址做一次 HEAD，看 Content-Type 是否 HTML。
    默认关闭：读路径上发网络请求会拖慢详情接口，也会被源站当成异常流量。
    """
    if not config.PLAY_LINE_PROBE_HTML or not url:
        return False
    from . import playline  # 延迟导入：关掉开关时 utils 仍是纯函数模块

    return playline.probe_is_page(url)


def _parse_episodes(line: str, line_index: int, provider: str = "") -> list:
    """
    单条线路内的分集解析：`集名$地址#集名$地址`（无 `$` 的段落按 URL 处理）。

    v1.14.0 / B-02：每集也带 `type` / `playable` / `from`（纯新增字段）。
    为什么要按集判断而不是继承线路：源站偶有「同一条线路里前 20 集是 m3u8、
    后 10 集插了个 H5 页面」的脏数据，客户端按线路级 playable 渲染会把
    那 10 集当可播、点了必然黑屏。单集判定成本是一次 path 正则，可忽略。
    """
    episodes = []
    for seg in line.split("#"):
        seg = seg.strip()
        if "$" in seg:
            name, url = seg.split("$", 1)
            name, url = name.strip(), url.strip()
        elif seg:
            # 老行为：没有集名时用 EP{线路下标}，保持逐字节兼容（客户端可能在做去重）
            name, url = f"EP{line_index}", seg
        else:
            continue
        kind = classify_play_line(url, provider)
        episodes.append({
            "name": name,
            "url": url,
            "type": kind,                 # m3u8 / mp4 / flv / mkv / page / unknown
            "playable": kind != "page",   # 与线路级同义：false=置灰并跳过自动连播
            "from": provider,             # 该集所属线路的 provider
        })
    return episodes


def parse_play_lines(
    raw: str,
    from_raw: str = None,
    *,
    policy: str = None,
    probe=None,
) -> tuple:
    """
    解析并（按策略）过滤/排序播放线路，返回 `(lines, meta)`。

    raw      上游 `vod_play_url`（"线路1$$$线路2"，线路内 "集名$地址#..."）
    from_raw 上游 `vod_play_from`（"feifan$$$ffm3u8"，与 raw 的线路顺序一一对应）
    policy   off / filter / whitelist；None 表示取全局配置
    probe    unknown 型地址的探测回调（返回 True 表示「其实是 HTML 页面」）

    lines 每项：
      name           线路名（过滤后的序号；PLAY_LINE_NAME_WITH_FROM 时追加 provider）
      from           provider（play_from 缺失时为空串）
      type           m3u8 / mp4 / flv / mkv / page / unknown
      playable       type != "page"
      original_index 在上游 raw 里的下标（0 基），供客户端缓存对齐
      episodes       [{name, url}]

    meta：
      policy / raw_lines / kept / dropped / fallback / filtered
      fallback=True 表示命中了安全阀（过滤后不足 PLAY_LINE_MIN_KEEP，已回退原始列表）。
    """
    pol = _norm_policy(policy)
    meta = {
        "policy": pol,
        "raw_lines": 0,
        "kept": 0,
        "dropped": 0,
        "page_lines": 0,
        "fallback": False,
        "filtered": False,
    }
    if not raw:
        return [], meta

    providers = (from_raw or "").split("$$$") if from_raw else []
    entries = []
    for i, line in enumerate(raw.split("$$$")):
        line = line.strip()
        if not line:
            continue
        # provider 必须先算：分集里每一条都要带上它（v1.14.0）
        provider = providers[i].strip() if i < len(providers) else ""
        episodes = _parse_episodes(line, i, provider)
        if not episodes:
            continue
        first_url = next((e["url"] for e in episodes if e["url"]), "")
        kind = classify_play_line(first_url, provider)
        if kind == "unknown":
            try:
                if (probe or _probe_is_page)(first_url):
                    kind = "page"
            except Exception:  # noqa: BLE001 - 探测失败只影响分类，不能拖垮详情接口
                pass
        entries.append({
            "index": i,
            "from": provider,
            "type": kind,
            "episodes": episodes,
        })

    meta["raw_lines"] = len(entries)
    meta["page_lines"] = sum(1 for e in entries if e["type"] == "page")
    kept = entries
    if pol == "filter":
        kept = [e for e in entries if e["type"] != "page"]
    elif pol == "whitelist":
        keep = _keep_ext()
        kept = [e for e in entries if e["type"] in keep]

    dropped = len(entries) - len(kept)
    # ---- 安全阀：绝不允许把一部片子过滤成「无线路」----
    if pol != "off" and len(kept) < max(1, config.PLAY_LINE_MIN_KEEP):
        if dropped:
            log.warning(
                "播放线路过滤后仅剩 %d 条（策略 %s，原始 %d 条），已回退未过滤结果: %s",
                len(kept), pol, len(entries), (entries[0]["episodes"][0]["url"][:120]
                                               if entries and entries[0]["episodes"] else ""),
            )
        kept, dropped = entries, 0
        meta["fallback"] = True

    if pol != "off" and config.PLAY_LINE_PREFER_DIRECT and not meta["fallback"]:
        # sorted 是稳定排序：同类型内保持上游原顺序
        kept = sorted(kept, key=lambda e: (_TYPE_RANK.get(e["type"], 4), e["index"]))

    out = []
    for n, e in enumerate(kept, 1):
        name = f"线路{n}"
        if config.PLAY_LINE_NAME_WITH_FROM and e["from"]:
            name = f"{name} ({e['from']})"
        out.append({
            "name": name,
            "from": e["from"],
            "type": e["type"],
            "playable": e["type"] != "page",
            "original_index": e["index"],
            "episodes": e["episodes"],
        })

    meta["kept"] = len(out)
    meta["dropped"] = dropped
    meta["filtered"] = dropped > 0
    return out, meta


def parse_play_url(raw: str, from_raw: str = None) -> list:
    """
    解析苹果 CMS 播放地址原始串，返回线路列表（含类型与 provider）。

    保持 v1.9.x 的调用签名与返回结构（`name` / `episodes` 都在），新字段纯新增：
      [{"name": "线路2 (ffm3u8)", "from": "ffm3u8", "type": "m3u8",
        "playable": true, "original_index": 1,
        "episodes": [{"name": "第01集", "url": "https://.../index.m3u8"}]}]

    需要知道「是否发生了过滤」时用 parse_play_lines()（它多返回一个 meta）。
    """
    return parse_play_lines(raw, from_raw)[0]


def page_params(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(config.API_PAGE_SIZE_DEFAULT, ge=1, le=config.API_PAGE_SIZE_MAX, description="每页条数"),
):
    return page, min(page_size, config.API_PAGE_SIZE_MAX)


SAFE_SLUG = re.compile(r"^[a-z0-9_]{1,64}$")


def validate_slug(slug: str) -> str:
    if not SAFE_SLUG.match(slug or ""):
        raise HTTPException(status_code=400, detail="非法的源站标识")
    return slug
