"""
SeleneTV 专用 API —— 响应序列化（逐字复刻 Helios 契约）。

所有字段名、omitempty 行为、SSE 驼峰键都对照 Helios models/models.go 与
handlers/*.go 逐字实现，SeleneTV 客户端无需任何改动。

Helios SearchResult（models.go:21-34）：
  id, title, poster, episodes[], episodes_titles[], source, source_name,
  class?, year, desc?, type_name?, douban_id?   （带 ? 为 omitempty 指针）
注意：class/desc/type_name/douban_id 是 *指针*，nil 时**整个 key 消失**；
vodsync PG 无 vod_class / vod_douban_id 列，这两个恒为 nil → 不输出。
"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 播放串提取：与 Helios lib/utils.go ExtractEpisodes 完全一致
#   $$$ 分线路 → 取集数最多的线路 → # 分集 → $ 分「集名/URL」→ 仅收 .m3u8 结尾
# ---------------------------------------------------------------------------
_M3U8_IN_CONTENT = re.compile(r'\$?(https?://[^\"\'\s]+?\.m3u8)')


def extract_episodes(vod_play_url: str, vod_content: str = "") -> Tuple[List[str], List[str]]:
    """
    返回 (episodes[URL], episodes_titles[集名])。

    对齐 Helios ExtractEpisodes + GetDetailFromAPI 的兜底：
      1) 从 vod_play_url 的 $$$/#/$ 结构提取，取集数最多的线路；
      2) 若为空且 content 非空，用正则从 content 里捞 m3u8（此时 titles 为空，
         与 Helios :439-447 的 fallback 行为一致）。
    """
    episodes: List[str] = []
    titles: List[str] = []
    if vod_play_url:
        for line in str(vod_play_url).split("$$$"):
            line_eps: List[str] = []
            line_titles: List[str] = []
            for seg in line.split("#"):
                parts = seg.split("$", 1)
                if len(parts) == 2 and parts[1].strip().lower().endswith(".m3u8"):
                    line_titles.append(parts[0].strip())
                    line_eps.append(parts[1].strip())
            if len(line_eps) > len(episodes):   # 取集数最多的线路
                episodes, titles = line_eps, line_titles
    if not episodes and vod_content:
        for m in _M3U8_IN_CONTENT.findall(str(vod_content)):
            episodes.append(m.lstrip("$"))
        # titles 保持为空（Helios 该分支不生成标题）
    return episodes, titles


def search_result(
    *,
    vod_id: Any,
    name: str,
    pic: str,
    episodes: List[str],
    episodes_titles: List[str],
    source: str,
    source_name: str,
    year: str = "",
    content: Optional[str] = None,
    type_name: Optional[str] = None,
    douban_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    组装 Helios SearchResult。omitempty 字段为 None 时不放入 dict
    （Python json.dumps 不会输出不存在的 key，等价 Go omitempty）。
    """
    out: Dict[str, Any] = {
        "id": str(vod_id),                       # Helios ID 是 string
        "title": name or "",
        "poster": pic or "",
        "episodes": episodes or [],
        "episodes_titles": episodes_titles or [],
        "source": source or "",
        "source_name": source_name or "",
        # class 恒省略（PG 无对应列）
        "year": year or "",
    }
    if content:                                   # desc,omitempty
        out["desc"] = content
    if type_name:                                 # type_name,omitempty
        out["type_name"] = type_name
    if douban_id is not None:                     # douban_id,omitempty
        out["douban_id"] = douban_id
    return out


# ---------------------------------------------------------------------------
# 用户态：Helios GET 返回「以 key 为字段名的裸 map」，POST/DELETE 返回 {"success":true}
# ---------------------------------------------------------------------------
def favorite_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Helios Favorite：json key 逐字；origin 恒 omitempty → 不输出。"""
    return {
        "source": str(row.get("source") or ""),
        "source_id": str(row.get("source_id") or ""),
        "source_name": str(row.get("source_name") or ""),
        "total_episodes": int(row.get("total_episodes") or 0),
        "title": str(row.get("title") or ""),
        "year": str(row.get("year") or ""),
        "cover": str(row.get("cover") or ""),
        "save_time": int(row.get("save_time") or 0),
        "search_title": str(row.get("search_title") or ""),
    }


def playrecord_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Helios PlayRecord：DB 列 index_number → JSON 名 "index"。"""
    return {
        "title": str(row.get("title") or ""),
        "source": str(row.get("source") or ""),
        "source_id": str(row.get("source_id") or ""),
        "source_name": str(row.get("source_name") or ""),
        "cover": str(row.get("cover") or ""),
        "year": str(row.get("year") or ""),
        "index": int(row.get("index_number") or 0),   # 关键别名
        "total_episodes": int(row.get("total_episodes") or 0),
        "play_time": int(row.get("play_time") or 0),
        "total_time": int(row.get("total_time") or 0),
        "save_time": int(row.get("save_time") or 0),
        "search_title": str(row.get("search_title") or ""),
    }


def data_key(source: str, source_id: Any) -> str:
    """Helios 用户数据 map 的 key：source + "+" + source_id。"""
    return f"{source}+{source_id}"


def success_true() -> Dict[str, bool]:
    return {"success": True}


# ---------------------------------------------------------------------------
# SSE 事件（Helios SSE*Event）：驼峰键 + 毫秒时间戳
# ---------------------------------------------------------------------------
def _ms() -> int:
    return int(time.time() * 1000)


def sse_frame(payload: Dict[str, Any]) -> bytes:
    """Helios: fmt.Fprintf(w, "data: %s\\n\\n", data) —— 紧凑 JSON。"""
    import json

    return ("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


def sse_start(query: str, total_sources: int) -> Dict[str, Any]:
    return {"type": "start", "query": query, "totalSources": total_sources, "timestamp": _ms()}


def sse_source_result(source: str, source_name: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": "source_result",
        "source": source,
        "sourceName": source_name,
        "results": results,
        "timestamp": _ms(),
    }


def sse_source_error(source: str, source_name: str, error: str) -> Dict[str, Any]:
    return {
        "type": "source_error",
        "source": source,
        "sourceName": source_name,
        "error": error,
        "timestamp": _ms(),
    }


def sse_complete(total_results: int, completed_sources: int) -> Dict[str, Any]:
    return {
        "type": "complete",
        "totalResults": total_results,
        "completedSources": completed_sources,
        "timestamp": _ms(),
    }
