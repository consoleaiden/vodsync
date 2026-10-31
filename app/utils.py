"""
工具函数：播放地址解析、分页参数处理。

注：API Key 校验 v1.5.0 起统一收敛到 app/auth.py（支持多 Token、会话、开关）。
"""
import re

from fastapi import HTTPException, Query

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
