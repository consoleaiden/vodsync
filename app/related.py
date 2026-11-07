"""
相关推荐预计算引擎（v1.9.0）。

为什么在服务端预计算
--------------------
数据源的 vod_tag/vod_class 基本缺失、演职员/地区/年代大量缺失，客户端本地算相似度
既没有信号又重复劳动。这里在同步后离线算好每个视频的 Top-N 推荐，存进 vod_related
表，接口只做一次主键查询 + 一次 IN 补卡片字段，P95 轻松进 50ms。

算法要点（与需求规格一一对应）
----------------------------
1. 权重重分配：某字段缺失（含占位值）时，其权重按比例摊给其余有效字段，
   不计 0 分 —— 字段越稀疏，标题占比自动越高。
2. 标题相似度：0.6 × 归一化标题 LCS 公共子串占比 + 0.4 × bigram Jaccard；
   双阈值门控（LCS >= 3 字 或 Jaccard >= 0.45）防两字短标题误伤。
3. 归一化：NFKC 全角转半角 → 繁转简（可选依赖 opencc）→ 剥离噪声后缀
   （国语/4K/HD/完整版...，正则表可配置）→ 序号（季/部/终章）摘出降权处理。
4. 倒排索引剪枝：按 标题 bigram/演员/导演/编剧/地区/年代/分类 建 token 索引，
   候选只取与目标共享 token 最多的前 CANDIDATE_CAP 个；小表直接全量两两比较。
5. 去重：同 (归一化标题, 年代) 只保留最新一条参与候选（同片重复收录不是推荐）。
6. 翻拍惩罚：年份差 > 6 年 → 标题分 ×0.6；序号不一致（第一季 vs 第二季）→ ×0.5。
7. 兜底链：得分命中不足时依次 同分类最近更新 → 同地区/年代 → 全站最新，
   条目带 mode=fallback 标记，前端栏位永远有内容。

存储
----
vod_related.related_json 只存精简引用 [{"v": vid, "s": score, "m": mode}]，
卡片字段读取时按 id 实时补齐（封面/备注永远是新的，被下架的条目自然消失）。

所有可调参数集中在 config.py（RELATED_*），改环境变量即可调优，不用改代码。
"""
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime

from sqlalchemy import bindparam, func, select

from . import config, database
from .models import build_vod_table, sources, star_terms, vod_related

log = logging.getLogger("vodsync.related")

# ---------------------------------------------------------------------------
# 繁转简：可选依赖（requirements 里的 opencc-python-reimplemented）。
# 没装也能跑 —— 退化为不做繁简转换，只影响繁体片名的命中率，不报错。
# ---------------------------------------------------------------------------
_t2s = None
_t2s_tried = False


def _converter():
    global _t2s, _t2s_tried
    if not _t2s_tried:
        _t2s_tried = True
        try:
            from opencc import OpenCC

            _t2s = OpenCC("t2s")
        except Exception:
            log.info("未安装 opencc（繁转简跳过）；如需繁体片名命中请 pip install opencc-python-reimplemented")
    return _t2s


# ---------------------------------------------------------------------------
# 可配置项解析（环境变量 → 运行时结构，模块加载时解析一次）
# ---------------------------------------------------------------------------
def parse_weights(raw: str = None) -> dict:
    """'class:0.30,actor:0.20,...' -> {'class': 0.30, ...}；非法项忽略。"""
    out = {}
    for part in (raw or config.RELATED_WEIGHTS).split(","):
        k, _, v = part.strip().partition(":")
        try:
            if k and float(v) > 0:
                out[k] = float(v)
        except ValueError:
            continue
    return out or {"title": 1.0}


def _parse_noise(raw: str = None) -> list:
    pats = []
    for p in (raw or config.RELATED_NOISE_PATTERNS).split(","):
        p = p.strip()
        if p:
            try:
                pats.append(re.compile(p, re.IGNORECASE))
            except re.error:
                log.warning("噪声正则非法已忽略: %s", p)
    return pats


WEIGHTS = parse_weights()
NOISE_PATTERNS = _parse_noise()

# 序号模式：从标题里摘出**具体序号值**（季2 / 部3 / 终 / 下），单独保存。
# 1) 去重键必须带序号值 —— 「XX第一季」「XX第二季」归一化后同名同年，
#    若只留类型标签会被同片去重误杀（整个系列只剩一条）；
# 2) 序号值不同的两条互推时标题分 ×ORDINAL_MISMATCH_FACTOR（同系列续作仍命中，
#    但排在完全同名者之后）；「上海滩」这种词不会命中（不在尾部）。
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str) -> int:
    """中文数字转整数（支持 一~九十九：二十三=23）。失败返回 0。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_NUM.get(s, 0)


_ORDINAL_PATTERNS = [
    (re.compile(r"第\s*(\d+)\s*季"), lambda m: f"季{m.group(1)}"),
    (re.compile(r"第\s*([一二三四五六七八九十]+)\s*季"), lambda m: f"季{_cn_to_int(m.group(1))}"),
    (re.compile(r"season\s*(\d+)"), lambda m: f"季{m.group(1)}"),
    (re.compile(r"第\s*(\d+)\s*部"), lambda m: f"部{m.group(1)}"),
    (re.compile(r"第\s*([一二三四五六七八九十]+)\s*部"), lambda m: f"部{_cn_to_int(m.group(1))}"),
    (re.compile(r"(终章|完结篇|大结局)$"), lambda m: "终"),
    (re.compile(r"(上|下|之[一二三四五])$"), lambda m: m.group(1)),
]


def normalize_title(name: str) -> tuple:
    """
    标题归一化，返回 (归一化标题, 序号值集合)。
    归一化后为空或长度 < 2 时由调用方跳过标题项。
    """
    s = unicodedata.normalize("NFKC", name or "").lower()
    cc = _converter()
    if cc:
        try:
            s = cc.convert(s)
        except Exception:
            pass
    ordvals = set()
    for pat, fn in _ORDINAL_PATTERNS:
        m = pat.search(s)
        if m:
            try:
                ordvals.add(fn(m))
            except Exception:
                pass
            s = pat.sub("", s)
    for pat in NOISE_PATTERNS:
        s = pat.sub("", s)
    # 去空白/标点：只保留字母数字汉字假名谚文（NFKC 后全角已转半角）
    s = re.sub(r"[\W_]+", "", s, flags=re.UNICODE)
    return s, frozenset(ordvals)


def _bigrams(s: str) -> frozenset:
    return frozenset(s[i : i + 2] for i in range(len(s) - 1)) if len(s) > 1 else frozenset()


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（动态规划；含 O(n) 快路径）。"""
    if not a or not b:
        return 0
    if a == b:
        return len(a)
    if b in a:
        return len(b)
    if a in b:
        return len(a)
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        ca = a[i - 1]
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if ca == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _jaccard(sa: frozenset, sb: frozenset) -> float:
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / len(sa | sb) if inter else 0.0


def title_similarity(na: str, oa: frozenset, ya: str, bga: frozenset,
                     nb: str, ob: frozenset, yb: str, bgb: frozenset) -> float:
    """
    标题相似度（0~1）。na/nb 归一化标题，oa/ob 序号值集合，ya/yb 年代串，
    bga/bgb 是预计算的标题 bigram 集合（feat 里存好，避免每对候选重复构建）。

    - 快速短路：无共享 bigram → 必无共享 3-gram → LCS 必 < 3 → 直接 0
      （绝大多数无关候选走这条路径，是性能关键）
    - 双阈值门控：LCS < RELATED_TITLE_LCS_MIN 字 且 Jaccard < JACCARD_MIN → 0
      （防「青云志」误配「青云路」这类两三字短标题）
    - 年份都已知且差 > RELATED_YEAR_GAP_PENALTY_YEARS → ×GAP_FACTOR（翻拍/同名不同片）
    - 序号值不一致（季1 vs 季2）→ ×ORDINAL_MISMATCH_FACTOR（同系列续作仍命中但降权）
    """
    if len(na) < 2 or len(nb) < 2:
        return 0.0
    inter = len(bga & bgb)
    if not inter:
        return 0.0
    if na == nb and oa == ob:
        base = 1.0
    else:
        # Jaccard 用交集数直除，不构建并集集合（热点路径，省一次大集合分配）
        ja = inter / (len(bga) + len(bgb) - inter)
        l = _lcs_len(na, nb)
        if l < config.RELATED_TITLE_LCS_MIN and ja < config.RELATED_TITLE_JACCARD_MIN:
            return 0.0
        base = 0.6 * (l / min(len(na), len(nb))) + 0.4 * ja
    if ya and yb and ya != yb:
        try:
            if abs(int(ya[:4]) - int(yb[:4])) > config.RELATED_YEAR_GAP_PENALTY_YEARS:
                base *= config.RELATED_YEAR_GAP_FACTOR
        except ValueError:
            pass
    if oa != ob:
        base *= config.RELATED_ORDINAL_MISMATCH_FACTOR
    return min(base, 1.0)


def _split_set(raw: str) -> frozenset:
    return frozenset(p for p in (raw or "").split(",") if p)


def _set_sim(sa: frozenset, sb: frozenset):
    """集合交集占比（按较小集合算，演员名单长短不一更公平）。返回 None=任一方缺失。"""
    if not sa or not sb:
        return None
    inter = len(sa & sb)
    return inter / min(len(sa), len(sb)) if inter else 0.0


def _scalar_sim(a: str, b: str):
    """分类/地区：相等 1.0，不等 0.0；None=任一方缺失（权重重分配）。"""
    if not a or not b:
        return None
    return 1.0 if a == b else 0.0


# ---------------------------------------------------------------------------
# 打分计划（性能）：字段权重降序 + 整数化 kind，避免每对候选做 7 次字符串比较
# 和 dict.items()。提前放弃：剩余权重（含被跳过的缺失字段）全按 1.0 计的上界
# 都够不到 RELATED_MIN_SCORE 时，立刻返回 0 —— 无关候选平均只算前两三个字段。
# ---------------------------------------------------------------------------
_K_CLASS, _K_AREA, _K_YEAR, _K_ACTOR, _K_DIRECTOR, _K_WRITER, _K_TITLE = range(7)
_KIND_MAP = {"class": _K_CLASS, "area": _K_AREA, "year": _K_YEAR, "actor": _K_ACTOR,
             "director": _K_DIRECTOR, "writer": _K_WRITER, "title": _K_TITLE}
SCORE_PLAN = sorted(
    ((_KIND_MAP[k], w) for k, w in WEIGHTS.items() if k in _KIND_MAP),
    key=lambda x: -x[1],
)
TOTAL_WEIGHT = sum(w for _, w in SCORE_PLAN)

# 演员信号可信度系数（按 asrc 分级；配对取两侧较小值）：
#   real=1.0 源站真实数据；strong=1.0 标题命中+字段稀疏（用户规则：相关性非常高）；
#   inf=STAR_SIM_FACTOR 弱信号（仅简介命中 / 标题命中但字段齐全）
_ASRC_FACTOR = {
    "real": 1.0,
    "strong": 1.0,
    "inf": max(0.0, min(1.0, config.RELATED_STAR_SIM_FACTOR)),
    "none": 1.0,
}


def _score_one(target: dict, cand: dict) -> float:
    """
    加权打分。缺失字段（sim=None）不计分子也不计分母 —— 这就是「权重重分配」：
    只有 标题+演员+年代 可用时，总权重自动归一为这三项的占比和。
    """
    min_score = config.RELATED_MIN_SCORE
    total = 0.0
    total_w = 0.0
    remaining = TOTAL_WEIGHT
    ta = target["actor"]; td = target["director"]; tw = target["writer"]
    for kind, w in SCORE_PLAN:
        if kind == _K_TITLE:
            tn, cn = target["name"], cand["name"]
            sim = title_similarity(
                tn, target["ords"], target["year"], target["bgs"],
                cn, cand["ords"], cand["year"], cand["bgs"],
            ) if (len(tn) >= 2 and len(cn) >= 2) else None
        elif kind == _K_YEAR:
            ya, yb = target["yi"], cand["yi"]
            if ya is None or yb is None:
                sim = None
            else:
                d = ya - yb
                if d < 0:
                    d = -d
                sim = 1.0 if d == 0 else (
                    config.RELATED_YEAR_ADJACENT_SCORE if d == 1 else 0.0)
        elif kind == _K_ACTOR:
            sim = _set_sim(ta, cand["actor"])
            if sim and (target["asrc"] != "real" or cand["asrc"] != "real"):
                # 任一侧是词库推断 → 按可信度较低的一侧降权（强推断不打折）
                sim *= min(_ASRC_FACTOR.get(target["asrc"], 1.0),
                           _ASRC_FACTOR.get(cand["asrc"], 1.0))
        elif kind == _K_DIRECTOR:
            sim = _set_sim(td, cand["director"])
        elif kind == _K_WRITER:
            sim = _set_sim(tw, cand["writer"])
        elif kind == _K_CLASS:
            t, c = target["type"], cand["type"]
            sim = None if not t or not c else (1.0 if t == c else 0.0)
        else:  # _K_AREA
            t, c = target["area"], cand["area"]
            sim = None if not t or not c else (1.0 if t == c else 0.0)
        if sim is None:
            continue
        total_w += w
        total += w * sim
        remaining -= w
        if total + remaining < min_score:   # 上界也够不到 → 无关候选提前放弃
            return 0.0
    return total / total_w if total_w > 0 else 0.0


# ---------------------------------------------------------------------------
# 明星词库（面板维护的主演名单，可选增强信号）
#
# 词库里的名字常出现在缺失演员字段视频的标题/简介中。预计算时扫描
# 标题+简介，命中即推断为演员信号参与匹配 —— 推断分乘 STAR_SIM_FACTOR
# 降权（「标题提到吴京」≠「吴京主演」，防误伤）。
# 词表进程内缓存 + 编译成单个正则（一次扫描完成全部命中）；
# 面板增删改后调 reload_star_terms() 失效重载。
# ---------------------------------------------------------------------------
_star_cache: dict = {"pattern": None, "count": 0}
_star_loaded = False


def _star_norm(s: str) -> str:
    """词库匹配用归一化：NFKC 全角转半角 + 小写 + 繁转简（保留标点，做子串匹配）。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    cc = _converter()
    if cc:
        try:
            s = cc.convert(s)
        except Exception:
            pass
    return s


def reload_star_terms() -> int:
    """读取启用的词库条目并编译匹配正则（面板增删改后调用）。返回词条数。"""
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(star_terms.c.term).where(star_terms.c.enabled == 1)
        ).all()
    terms = sorted({_star_norm(r[0]).strip() for r in rows}, key=lambda t: (-len(t), t))
    terms = [t for t in terms if len(t) >= 2]   # 单字名误报率过高，不参与推断
    pattern = re.compile("|".join(re.escape(t) for t in terms)) if terms else None
    with _locks_guard:
        _star_cache["pattern"] = pattern
        _star_cache["count"] = len(terms)
    if terms:
        log.info("明星词库已加载 %d 个词条", len(terms))
    return len(terms)


def star_pattern():
    """惰性加载词库正则（进程内首次使用时读库；增删改后由 API 调 reload 失效重载）。"""
    global _star_loaded
    if not _star_loaded:
        _star_loaded = True
        try:
            reload_star_terms()
        except Exception:
            log.exception("明星词库加载失败（按无词库处理）")
    with _locks_guard:
        return _star_cache["pattern"]


def _infer_strength(name_raw: str, content: str, area: str, year: str, type_name: str, pattern):
    """
    明星词库推断，返回 (演员集合, asrc)。
    asrc 分级（用户规则）：
      real    源站有真实演员数据（不做推断）
      strong  标题命中词库 + 视频字段稀疏（area/year/type 非空数 <= 阈值）
              → 相关性非常高，打分不打折
      inf     仅简介命中，或标题命中但字段较齐全 → 弱信号，打分降权
      none    无任何演员信号
    """
    if pattern is None:
        return frozenset(), "none"
    title_hits = frozenset(pattern.findall(_star_norm(name_raw or "")))
    if not title_hits:
        c_hits = frozenset(pattern.findall(_star_norm((content or "")[:2000])))
        return c_hits, ("inf" if c_hits else "none")
    sparse = sum(1 for v in (area, year, type_name) if v) <= config.RELATED_STAR_STRONG_MAX_FIELDS
    if sparse:
        return title_hits, "strong"
    # 标题命中但字段齐全 → 弱信号；顺带合并简介里的命中
    c_hits = frozenset(pattern.findall(_star_norm((content or "")[:2000])))
    return title_hits | c_hits, "inf"


# ---------------------------------------------------------------------------
# 行特征与倒排索引
# ---------------------------------------------------------------------------
def _feat(vod_id, name_raw, type_name, area, year, actor_set, director, writer,
          vod_time, asrc="real") -> dict:
    name, ords = normalize_title(name_raw)
    try:
        yi = int((year or "")[:4])
    except ValueError:
        yi = None
    return {
        "id": vod_id,
        "raw": name_raw or "",
        "name": name,
        "ords": ords,
        "bgs": _bigrams(name),   # 预存标题 bigram（打分短路 + tokens 复用）
        "type": type_name or "",
        "area": area or "",
        "year": year or "",
        "yi": yi,                # 预转 int，打分时不再反复 int()
        "actor": actor_set,
        "asrc": asrc,            # real=源站真实数据 / inf=明星词库推断（打分降权）
        "director": _split_set(director),
        "writer": _split_set(writer),
        "time": vod_time or "",
    }


def _tokens(f: dict) -> list:
    """该条目参与的索引 token（用于找候选，不用于打分）。"""
    out = ["n:" + bg for bg in f["bgs"]]
    out.extend("a:" + x for x in f["actor"])
    out.extend("d:" + x for x in f["director"])
    out.extend("w:" + x for x in f["writer"])
    if f["area"]:
        out.append("ar:" + f["area"])
    if f["year"]:
        out.append("y:" + f["year"])
    if f["type"]:
        out.append("t:" + f["type"])
    return out


def _dup_key(f: dict) -> tuple:
    """
    同片重复收录判定键：同归一化标题 + 同年代 + 同序号值。
    必须带序号值，否则「XX第一季/第二季」会被当成同一条误杀。
    """
    return (f["name"], f["year"], f["ords"])


# ---------------------------------------------------------------------------
# 计算引擎主体
# ---------------------------------------------------------------------------
_compute_locks: dict = {}
_locks_guard = threading.Lock()


def _slug_lock(slug: str):
    with _locks_guard:
        if slug not in _compute_locks:
            _compute_locks[slug] = threading.Lock()
        return _compute_locks[slug]


def _load_feats(slug: str):
    """
    流式加载全表特征 + 构建候选索引。

    返回 (kept, dup_list, postings, by_time, dup_ids)
      kept      去重后的特征列表（同 (标题,年代) 只留最新一条）—— 候选池
      dup_list  被去重丢弃的重复收录条目（仍作为推荐目标计算，只是不进候选池）
      postings  token -> [kept 下标]；行数 > BRUTE_FORCE_MAX 时才建（None = 全量比较）
      by_time   kept 按 vod_time 降序（兜底链用，O(n) 一次排序）
      dup_ids   被去重条目的 vod_id 集合（统计用）
    """
    table = build_vod_table(slug)
    cols = (
        table.c.vod_id, table.c.name, table.c.type_name, table.c.vod_area,
        table.c.vod_year, table.c.vod_actor, table.c.vod_director,
        table.c.vod_writer, table.c.vod_time, table.c.content,
    )
    # 明星词库（可选）：off 时不取 content，省一次大文本传输
    star_mode = (config.RELATED_STAR_INFER_MODE or "off").lower()
    use_star = star_mode in ("when_missing", "always")
    pattern = star_pattern() if use_star else None
    feats, dup_map = [], {}
    kept, dup_list = [], []
    light = False
    chunk = 2000
    offset = 0
    while True:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(*cols).order_by(table.c.vod_id).offset(offset).limit(chunk)
            ).mappings().all()
        if not rows:
            break
        if offset == 0 and len(rows) > config.RELATED_MAX_ROWS_PER_SOURCE:
            light = True   # 超大表：丢弃演职员信号（内存保护），标题/分类/年代仍可用
        offset += chunk
        for r in rows:
            actor_raw = "" if light else (r["vod_actor"] or "")
            inf_set, asrc = frozenset(), "none"
            if pattern is not None and (star_mode == "always" or not actor_raw):
                inf_set, asrc = _infer_strength(
                    r["name"], "" if light else (r["content"] or ""),
                    r["vod_area"] or "", r["vod_year"] or "", r["type_name"] or "",
                    pattern,
                )
            if actor_raw:
                actor_set = _split_set(actor_raw)
                if star_mode == "always" and inf_set:
                    actor_set = actor_set | inf_set   # 合并模式：真实 + 推断
                asrc = "real"                         # 有真实数据以真实为准
            elif inf_set:
                actor_set = inf_set
            else:
                actor_set = frozenset()
            f = _feat(
                r["vod_id"], r["name"], r["type_name"], r["vod_area"], r["vod_year"],
                actor_set,
                "" if light else r["vod_director"],
                "" if light else r["vod_writer"],
                r["vod_time"],
                asrc=asrc,
            )
            if not f["name"] and not f["raw"]:
                continue
            feats.append(f)
        if light:
            break   # 轻量模式一次性读标题即可，再分块没有意义
    if light:
        log.info("[%s] 相关推荐进入轻量模式（行数 > %d），只用标题/分类/年代",
                 slug, config.RELATED_MAX_ROWS_PER_SOURCE)

    # 同片去重：同 (标题,年代) 只留 vod_time 最新的一条进候选池
    for f in sorted(feats, key=lambda x: (x["time"], x["id"]), reverse=True):
        k = _dup_key(f)
        if k in dup_map:
            dup_list.append(f)
            continue
        dup_map[k] = f
        kept.append(f)
    kept.reverse()   # 恢复按 id 稳定顺序
    dup_list.reverse()
    dup_ids = {f["id"] for f in dup_list}

    postings = None
    if len(kept) > config.RELATED_BRUTE_FORCE_MAX:
        postings = {}
        for i, f in enumerate(kept):
            for t in _tokens(f):
                postings.setdefault(t, []).append(i)
    by_time = sorted(kept, key=lambda x: (x["time"], x["id"]), reverse=True)
    return kept, dup_list, postings, by_time, dup_ids


def _candidates(target: dict, feats: list, postings, dup_ids: set) -> list:
    """
    候选生成：小表全量；大表用倒排 token 投票取前 CANDIDATE_CAP。
    过滤：自身 + 与目标同 (标题,年代,序号) 的重复收录条目（同片不是推荐）。

    性能关键：**稀有 token 优先**。目标的一串 token 里，共享系列名的大 bigram
    倒排链可能有几千条（区分度低），而某个演员/年代 token 只有几十条（区分度高）。
    按链长升序累加，凑够 2×CAP 个去重候选就停 —— 少扫 90% 的 postings，且
    留下的恰好是最有区分度的候选。
    """
    tk = _dup_key(target)
    if postings is None:
        return [f for f in feats if f["id"] != target["id"] and _dup_key(f) != tk]
    cap = config.RELATED_CANDIDATE_CAP
    items = [(t, plist) for t, plist in
             ((t, postings.get(t)) for t in _tokens(target)) if plist]
    items.sort(key=lambda x: len(x[1]))
    votes = Counter()
    seen = set()
    stop = cap * 2
    for _t, plist in items:
        for i in plist:
            if i not in seen:
                seen.add(i)
            votes[i] += 1
        if len(seen) >= stop:
            break
    picked = [i for i, _ in votes.most_common(cap)]
    return [
        feats[i] for i in picked
        if feats[i]["id"] != target["id"] and _dup_key(feats[i]) != tk
    ]


def _rank(target: dict, cands: list) -> list:
    """打分 → 阈值过滤 → 降序。返回 [(feat, score)]。"""
    scored = []
    for c in cands:
        s = _score_one(target, c)
        if s >= config.RELATED_MIN_SCORE:
            scored.append((c, round(s, 4)))
    scored.sort(key=lambda x: -x[1])
    return scored


def _fallback_ids(target: dict, by_time: list, limit: int) -> list:
    """
    兜底链（在内存中完成，不打数据库）：
      ① 同分类最近更新 → ② 同地区/同年代最近更新 → ③ 全站最新收录
    跳过：自身 + 与目标同 (标题,年代,序号) 的重复收录条目（同片不是推荐，
    兜底也不能把同片的另一条记录塞进来）。
    """
    tk = _dup_key(target)
    seen = {target["id"]}
    out = []

    def take(pred, n):
        got = 0
        for f in by_time:
            if got >= n:
                break
            if f["id"] in seen or _dup_key(f) == tk or not pred(f):
                continue
            seen.add(f["id"])
            out.append(f)
            got += 1

    take(lambda f: bool(target["type"]) and f["type"] == target["type"], limit)
    if len(out) < limit:
        take(lambda f: bool(target["area"]) and f["area"] == target["area"], limit - len(out))
    if len(out) < limit:
        take(lambda f: bool(target["year"]) and f["year"] == target["year"], limit - len(out))
    if len(out) < limit:
        take(lambda f: True, limit - len(out))
    return out


def _store(slug: str, rows: list):
    """批量覆盖写 vod_related（UPSERT 幂等；失败整批回滚，旧版本结果保留）。"""
    if not rows:
        return
    stmt = database.dialect_insert(vod_related).values(
        slug=slug,
        vod_id=bindparam("vod_id"),
        related_json=bindparam("related_json"),
        computed_at=datetime.now(),
        algo_version=config.RELATED_ALGO_VERSION,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["slug", "vod_id"],
        set_={
            "related_json": stmt.excluded.related_json,
            "computed_at": stmt.excluded.computed_at,
            "algo_version": stmt.excluded.algo_version,
        },
    )
    with database.write_txn() as conn:
        conn.execute(stmt, rows)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def compute_source(slug: str, only_ids: list = None, reason: str = "") -> dict:
    """
    计算（或重算）一个源站的相关推荐。
    only_ids 为空 = 全站计算；否则只算指定目标（增量重算，索引仍需全量加载）。
    返回统计信息。失败抛异常由调用方兜底（旧结果不会被破坏）。
    """
    if not config.RELATED_ENABLED:
        return {"skipped": "related_disabled"}
    lock = _slug_lock(slug)
    if not lock.acquire(blocking=False):
        # 同一源站已在计算中：增量重算丢了也没事（每日校准会补），跳过比排队更安全
        log.info("[%s] 相关推荐已有计算任务在跑，本次跳过（%s）", slug, reason or "manual")
        return {"skipped": "busy"}
    started = time.time()
    try:
        kept, dup_list, postings, by_time, dup_ids = _load_feats(slug)
        if not kept and not dup_list:
            return {"computed": 0, "total": 0}
        by_id = {f["id"]: f for f in kept}
        dup_by_id = {f["id"]: f for f in dup_list}
        targets = kept + dup_list
        if only_ids:
            wanted = []
            for i in dict.fromkeys(only_ids):
                if i in by_id:
                    wanted.append(by_id[i])
                elif i in dup_by_id:
                    wanted.append(dup_by_id[i])
            targets = wanted
        limit = config.RELATED_LIMIT_DEFAULT
        n_fallback = 0
        written, batch = 0, []
        total = len(targets)
        for idx, target in enumerate(targets, 1):
            scored = _rank(target, _candidates(target, kept, postings, dup_ids))
            if scored:
                # 有真实命中：按规格 3.3，直接用得分列表（<=12 条），不掺兜底
                entries = [{"v": f["id"], "s": s} for f, s in scored[:limit]]
            else:
                # 零命中：整条走兜底链（宁可返回兜底，也不允许返回空）
                entries = [
                    {"v": f["id"], "s": 0.0, "m": "fb"}
                    for f in _fallback_ids(target, by_time, limit)
                ]
                n_fallback += len(entries)
            if entries:
                batch.append({"vod_id": target["id"], "related_json": json.dumps(entries, ensure_ascii=False)})
            if len(batch) >= config.RELATED_WRITE_BATCH:
                _store(slug, batch)
                written += len(batch)
                batch = []
            if idx % 2000 == 0:
                log.info("[%s] 相关推荐进度 %d/%d（耗时 %.1fs）", slug, idx, total, time.time() - started)
        if batch:
            _store(slug, batch)
            written += len(batch)
        stats = {
            "slug": slug, "computed": written, "total": total,
            "fallback_entries": n_fallback, "duplicates": len(dup_ids),
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        log.info("[%s] 相关推荐完成: %s%s", slug, stats, f" ({reason})" if reason else "")
        return stats
    finally:
        lock.release()


def recompute_source_safe(slug: str, reason: str = ""):
    """后台线程用：全站重算 + 异常兜底（绝不向外抛）。"""
    try:
        compute_source(slug, reason=reason or "full")
    except Exception:
        log.exception("[%s] 相关推荐全站重算失败（旧结果保留）", slug)


def recompute_ids_safe(slug: str, ids: list, reason: str = ""):
    """后台线程用：增量重算指定目标。"""
    if not ids:
        return
    try:
        compute_source(slug, only_ids=ids, reason=reason or "incremental")
    except Exception:
        log.exception("[%s] 相关推荐增量重算失败（旧结果保留）", slug)


# ---------------------------------------------------------------------------
# 存量补齐与校准
# ---------------------------------------------------------------------------
def reset_algo_version() -> int:
    """算法版本变化时清空旧结果（启动时调用）。返回删除行数。"""
    with database.engine.connect() as conn:
        n = conn.execute(
            vod_related.delete().where(vod_related.c.algo_version != config.RELATED_ALGO_VERSION)
        ).rowcount or 0
    if n:
        log.warning("算法版本已变更（当前 %s），清空 %d 条旧推荐结果（由启动补齐任务重算）",
                    config.RELATED_ALGO_VERSION, n)
    return n


def coverage(slug: str) -> dict:
    """单源站覆盖率（面板/验收用）：videos / computed / covered%。"""
    table = build_vod_table(slug)
    if not database.table_exists(table.name):
        return {"slug": slug, "videos": 0, "computed": 0, "coverage": 0.0}
    with database.engine.connect() as conn:
        videos = conn.execute(select(func.count()).select_from(table)).scalar() or 0
        computed = conn.execute(
            select(func.count()).select_from(vod_related).where(vod_related.c.slug == slug)
        ).scalar() or 0
    pct = round(computed * 100 / videos, 1) if videos else 0.0
    return {"slug": slug, "videos": videos, "computed": computed, "coverage": pct}


def coverage_all() -> list:
    """全部源站的推荐覆盖率（管理接口 / 验收核对用）。"""
    with database.engine.connect() as conn:
        slugs = [r[0] for r in conn.execute(
            select(sources.c.slug).where(sources.c.enabled == 1).order_by(sources.c.id)
        ).all()]
    return [coverage(s) for s in slugs]


def sources_needing_compute() -> list:
    """有数据但还没有（或有旧版本）推荐结果的源站 —— 启动补齐/每日校准用。"""
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(sources.c.slug, sources.c.total_videos)
            .where(sources.c.enabled == 1, sources.c.total_videos > 0)
        ).all()
    out = []
    for slug, _tv in rows:
        cov = coverage(slug)
        if cov["computed"] < cov["videos"]:
            out.append(slug)
    return out


def startup_backfill():
    """启动补齐：对覆盖率不满的源站逐站全量计算（后台线程，串行避免抢 IO）。"""
    if not config.RELATED_ENABLED:
        return
    reset_algo_version()
    slugs = sources_needing_compute()
    if not slugs:
        log.info("相关推荐：存量覆盖率完整，无需补齐")
        return

    def _worker():
        for slug in slugs:
            recompute_source_safe(slug, reason="startup_backfill")

    threading.Thread(target=_worker, daemon=True, name="related-backfill").start()
    log.info("相关推荐：启动补齐 %d 个源站: %s%s", len(slugs), ",".join(slugs[:6]),
             " ..." if len(slugs) > 6 else "")


def daily_calibration(budget: int = None) -> dict:
    """
    每日校准（调度器凌晨调用）：把 computed_at 最早 / 覆盖率不满的源站重算一遍。
    budget 限制本次最多处理几个站（默认 RELATED_CALIBRATION_BUDGET）。
    """
    if not config.RELATED_ENABLED:
        return {"skipped": "related_disabled"}
    budget = budget or config.RELATED_CALIBRATION_BUDGET
    slugs = sources_needing_compute()[:budget]
    for slug in slugs:
        recompute_source_safe(slug, reason="daily_calibration")
    return {"recomputed": slugs}


# ---------------------------------------------------------------------------
# 读取路径（API 层调用）
# ---------------------------------------------------------------------------
def _fallback_live(slug: str, table, vod_id: int, limit: int) -> list:
    """预计算结果缺失时的实时兜底（单条 SQL，走不到预计算表的新视频也能立刻有推荐）。"""
    with database.engine.connect() as conn:
        row = conn.execute(
            select(table.c.vod_id, table.c.type_name, table.c.vod_area, table.c.vod_year)
            .where(table.c.vod_id == vod_id)
        ).mappings().first()
        if not row:
            return []
        picked, seen = [], {vod_id}

        def q(where, n):
            if len(picked) >= n:
                return
            stmt = select(table.c.vod_id).order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
            if where is not None:
                stmt = stmt.where(where)
            if seen:
                stmt = stmt.where(table.c.vod_id.not_in(seen))
            for vid in conn.execute(stmt.limit(n - len(picked))).scalars().all():
                if vid not in seen:
                    seen.add(vid)
                    picked.append(vid)

        if row["type_name"]:
            q(table.c.type_name == row["type_name"], limit)
        if row["vod_area"] and len(picked) < limit:
            q(table.c.vod_area == row["vod_area"], limit)
        if row["vod_year"] and len(picked) < limit:
            q(table.c.vod_year == row["vod_year"], limit)
        if len(picked) < limit:
            q(None, limit)
        return picked


def get_related_entries(slug: str, vod_id: int, limit: int, table) -> tuple:
    """
    读预计算结果；没有则实时兜底。
    返回 (entries, fallback)：entries = [{"v":..,"s":..,"m":..}]（按分降序）。
    fallback 标记来源有二：预计算时零命中走了兜底链（条目 m=fb），
    或该视频还没有预计算结果（本次实时兜底）。
    """
    entries = []
    has_row = False
    with database.engine.connect() as conn:
        row = conn.execute(
            select(vod_related.c.related_json).where(
                vod_related.c.slug == slug, vod_related.c.vod_id == vod_id
            )
        ).first()
    if row and row[0]:
        has_row = True
        try:
            entries = json.loads(row[0])
        except (TypeError, ValueError):
            entries = []
    if entries:
        # 预计算兜底（compute_source 零命中时整条走兜底链，全部带 m=fb）
        fallback = all(e.get("m") == "fb" for e in entries)
    else:
        # 没有预计算结果（新视频未覆盖 / JSON 损坏）→ 实时兜底查询
        ids = _fallback_live(slug, table, vod_id, limit)
        entries = [{"v": i, "s": 0.0, "m": "fb"} for i in ids]
        fallback = True
    return entries[:limit], fallback


def enrich_cards(slug: str, source_name: str, entries: list, table) -> list:
    """
    把精简引用补齐为卡片字段：IN 查一次 vod_{slug}。
    被下架/未同步的 id 自然被过滤（查不到就不出现），顺序与预计算一致。
    """
    ids = [e["v"] for e in entries]
    if not ids:
        return []
    with database.engine.connect() as conn:
        found = {
            r["vod_id"]: r
            for r in conn.execute(
                select(
                    table.c.vod_id, table.c.type_name, table.c.name,
                    table.c.pic, table.c.remarks,
                ).where(table.c.vod_id.in_(ids))
            ).mappings().all()
        }
    cards = []
    for e in entries:
        r = found.get(e["v"])
        if not r:
            continue
        cards.append({
            "vod_id": r["vod_id"],
            "vod_name": r["name"],
            "vod_pic": r["pic"],
            "vod_remarks": r["remarks"],
            "type_name": r["type_name"],
            "source": slug,
            "source_name": source_name,
            "score": e.get("s", 0.0),
            "mode": e.get("m") or ("fallback" if e.get("s", 0) <= 0 else "match"),
        })
    return cards
