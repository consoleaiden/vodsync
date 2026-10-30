"""
工具函数：播放地址解析、API Key 校验、分页参数处理。
"""
import hmac
import re

from fastapi import Header, HTTPException, Query

from . import config


def parse_play_url(raw: str) -> list:
    """
    解析苹果 CMS 播放地址原始串：
      格式: "线路1$$$线路2" ，每条线路内 "集名$地址#集名$地址"
    返回: [{"name": "线路1", "episodes": [{"name":"第01集","url":"https://..."}]}]
    """
    result = []
    if not raw:
        return result
    for i, line in enumerate(raw.split("$$$")):
        line = line.strip()
        if not line:
            continue
        episodes = []
        for seg in line.split("#"):
            seg = seg.strip()
            if "$" in seg:
                name, url = seg.split("$", 1)
                episodes.append({"name": name.strip(), "url": url.strip()})
            elif seg:
                episodes.append({"name": f"EP{i}", "url": seg})
        if episodes:
            result.append({"name": f"线路{i + 1}", "episodes": episodes})
    return result


def check_api_key(
    x_api_key: str = Header(default=""),
    apikey: str = Query(default=""),
):
    """可选的全局 API Key 鉴权：设置 API_KEY 环境变量后生效。"""
    if not config.API_KEY:
        return
    supplied = x_api_key or apikey
    if not supplied or not hmac.compare_digest(supplied, config.API_KEY):
        raise HTTPException(status_code=401, detail="无效的 API Key")


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
