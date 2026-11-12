"""
智能去广告（adfilter）控制面：规则包下发 + 命中上报（v1.16.0）。

边界（很重要，别越界实现）
--------------------------
1. **不碰播放数据面**：m3u8 改写、ts 反代、播放代理一律不做。客户端在端上拦截
   播放列表响应体、原位重写、分片仍直连 CDN —— 本服务零带宽成本。
2. 只做两件事：
   - 下发规则包（阈值 / 高置信 URL 模式 / 站点级例外），不发版也能改行为；
   - 收上报明细（哪个 url 删了几段、多少毫秒），给 Phase 2 的广告指纹库攒数据。
3. 已有接口一个都不改：不带 `adfilter` 能力的老客户端行为完全不变。

为什么服务端必须自己校验规则包
------------------------------
客户端装载时对越界值做**钳制**，并且「JSON 非法或字段越界 → 整体丢弃回退内置
规则」（不会部分采纳）。也就是说服务端发出一个 0.99 的 relative_max_ratio，
客户端的反应不是「按 0.95 跑」，而是「这条规则包整个不要了」。所以发布前
必须挡住越界包 —— 客户端不会替服务端兜底。

版本语义
--------
- 每个 channel（stable / beta）内 version 严格 +1，客户端只比较大小；
- 回滚 = 新增一个版本（内容取旧版），历史行永不修改，版本号永远单调，
  客户端「服务端更高就换」的逻辑不需要任何特殊分支；
- 客户端版本高于服务端（服务端刚回滚）→ 返回 changed=false，客户端继续用
  本地较新的包，无副作用。
"""
import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import Float, case, cast, func, select, text

from . import config, database
from .models import adfilter_reports, adfilter_rule_packs

log = logging.getLogger("vodsync.adfilter")

CHANNELS = ("stable", "beta")
DEFAULT_CHANNEL = "stable"

MAX_URL_PATTERN_LEN = 64
MAX_URL_PATTERNS = 64
MAX_SITE_OVERRIDES = 256
MAX_HOST_LEN = 253
MAX_URL_LEN = 2048
SITE_MODES = ("off",)

# 字段名 -> (下界, 上界, 是否整数)。区间与客户端钳制区间一致：
# 服务端放行的值，客户端一定不会再二次修正。
_THRESHOLDS = {
    "relative_max_ratio": (0.05, 0.95, False),
    "absolute_max_sec": (10, 1800, True),
    "safety_floor": (0.5, 1.0, False),
}

# 语义化商业词：放进 url_patterns 会把「/shop/ 目录下的正片」当广告删掉。
# 不硬拒（个别站的广告路径确实带 ads 字样的正常目录），只作为发布告警返回。
_SOFT_BAD_PATTERNS = ("/shop/", "/sale/", "/brand/", "/buy/", "/cart/")

# v1.16.1：解析不出 host 的上报行用这个占位，和「尚未回填」（空串）区分开，
# 否则每次启动都会把同一批脏数据重扫一遍。统计时排除它。
HOST_UNKNOWN = "-"


def host_of(url: str) -> str:
    """
    从播放列表地址提取站点级 host：**小写、去端口、去 userinfo、去路径**。

    同一站点的多个 CDN 路径/端口必须归到一组，否则按 host 的分布又会被拆散，
    和「全局平均被稀释」是同一个问题。IPv6 的方括号由 urlsplit.hostname 去掉。
    """
    if not isinstance(url, str) or not url.strip():
        return HOST_UNKNOWN
    raw = url.strip()
    try:
        # 正常上报都是 http(s)://；没有 scheme 时按相对 URL 补 // 再解析
        parts = urlsplit(raw if "//" in raw else "//" + raw)
        host = (parts.hostname or "").strip().lower().rstrip(".")
    except Exception:  # noqa: BLE001 —— 畸形 URL 不该让上报或启动失败
        return HOST_UNKNOWN
    return host[:MAX_HOST_LEN] if host else HOST_UNKNOWN


def _backfill_hosts(limit: int = 5000) -> int:
    """
    v1.16.0 建的表没有 host 列，加列后老行的值是空串 —— 启动时补齐。

    分批（每次最多 limit 行）而不是一次性全表 UPDATE：url 最长 2048 字符，
    全表读进内存再逐行 UPDATE 在积累几十万行后会拖慢启动；分几轮跑完即可。
    回填只依赖 url，不依赖客户端发版。
    """
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(adfilter_reports.c.id, adfilter_reports.c.url)
                .where(
                    (adfilter_reports.c.host == "")
                    | (adfilter_reports.c.host.is_(None))
                )
                .limit(limit)
            ).all()
        if not rows:
            return 0
        with database.write_txn() as conn:
            for rid, url in rows:
                conn.execute(
                    adfilter_reports.update()
                    .where(adfilter_reports.c.id == rid)
                    .values(host=host_of(url))
                )
        n = len(rows)
        # 只有触到本轮上限才可能有下一批；否则「下次启动继续」会让人误以为
        # 回填永远补不完（线上只回填 4 条时也这么打印过）。
        suffix = "（已达本轮上限，下次启动继续）" if n >= limit else "（已全部补齐）"
        log.info("[adfilter] 已回填 %d 条上报的 host%s", n, suffix)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("[adfilter] host 回填失败（不影响服务）: %s", str(e)[:160])
        return 0


def default_rules() -> dict:
    """内置默认规则包（与客户端内置默认值一致）。"""
    return {
        "thresholds": {
            "relative_max_ratio": 0.5,
            "absolute_max_sec": 300,
            "safety_floor": 0.8,
        },
        "url_patterns": [
            "/ad/", "/ads/", "/adv/", "/advert",
            "/gg/", "/preroll/", "/midroll/", "ad.m3u8",
        ],
        "site_overrides": [],
    }


class RuleError(Exception):
    """规则包校验失败。field 指出具体字段，供面板高亮。"""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


class RateLimited(Exception):
    """上报被限流。retry_after 为建议等待秒数。"""

    def __init__(self, retry_after: int):
        super().__init__("too many reports")
        self.retry_after = max(1, int(retry_after))


# ---------------------------------------------------------------------------
# 规则包校验与规范化
# ---------------------------------------------------------------------------
def _as_number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleError(f"{field} 必须是数字", field)
    return float(value)


def normalize(rules) -> dict:
    """
    校验并规范化规则包，返回**只含已知字段**的规范对象（可直接入库下发）。

    - 未知顶层/嵌套键静默丢弃（向前兼容：老服务端遇到新字段不至于拒收，
      新服务端也不会把无意义的键传给客户端）；
    - 越界、类型错、超条数一律抛 RuleError（客户端不会替服务端兜底）。
    """
    if rules is None:
        rules = {}
    if not isinstance(rules, dict):
        raise RuleError("rules 必须是 JSON 对象", "rules")

    out = {}

    th = rules.get("thresholds")
    if th is not None:
        if not isinstance(th, dict):
            raise RuleError("thresholds 必须是 JSON 对象", "thresholds")
        clean = {}
        for key, (lo, hi, is_int) in _THRESHOLDS.items():
            if key not in th:
                continue
            value = _as_number(th[key], f"thresholds.{key}")
            if is_int and abs(value - round(value)) > 1e-9:
                raise RuleError(f"thresholds.{key} 必须是整数", f"thresholds.{key}")
            if not (lo <= value <= hi):
                raise RuleError(
                    f"thresholds.{key} 超出允许区间 [{lo}, {hi}]：{th[key]}",
                    f"thresholds.{key}",
                )
            clean[key] = int(round(value)) if is_int else round(value, 4)
        out["thresholds"] = clean

    pats = rules.get("url_patterns")
    if pats is not None:
        if not isinstance(pats, list):
            raise RuleError("url_patterns 必须是字符串数组", "url_patterns")
        if not pats:
            # 客户端把「空数组」与「缺省」不一定区分得清，为避免回退到内置 8 条
            # 造成理解偏差，直接拒收；要关掉 URL 匹配请放一个永不命中的占位串。
            raise RuleError(
                "url_patterns 不能为空数组；不想要 URL 匹配请填一个不会出现的占位串"
                "（例如 /__none__/）",
                "url_patterns",
            )
        if len(pats) > MAX_URL_PATTERNS:
            raise RuleError(
                f"url_patterns 最多 {MAX_URL_PATTERNS} 条，当前 {len(pats)} 条",
                "url_patterns",
            )
        clean_pats, seen = [], set()
        for i, raw in enumerate(pats):
            field = f"url_patterns[{i}]"
            if not isinstance(raw, str):
                raise RuleError(f"{field} 必须是字符串", field)
            item = raw.strip().lower()
            if not item:
                raise RuleError(f"{field} 不能为空", field)
            if len(item) > MAX_URL_PATTERN_LEN:
                raise RuleError(
                    f"{field} 超过 {MAX_URL_PATTERN_LEN} 字符", field
                )
            if any(ch.isspace() for ch in item):
                raise RuleError(f"{field} 不能包含空白字符", field)
            if item in seen:
                continue
            seen.add(item)
            clean_pats.append(item)
        out["url_patterns"] = clean_pats

    sites = rules.get("site_overrides")
    if sites is not None:
        if not isinstance(sites, list):
            raise RuleError("site_overrides 必须是数组", "site_overrides")
        if len(sites) > MAX_SITE_OVERRIDES:
            raise RuleError(
                f"site_overrides 最多 {MAX_SITE_OVERRIDES} 条，当前 {len(sites)} 条",
                "site_overrides",
            )
        clean_sites, index = [], {}
        for i, raw in enumerate(sites):
            field = f"site_overrides[{i}]"
            if not isinstance(raw, dict):
                raise RuleError(f"{field} 必须是对象", field)
            host = raw.get("host")
            if not isinstance(host, str) or not host.strip():
                raise RuleError(f"{field}.host 必须是非空字符串", f"{field}.host")
            host = host.strip().lower()
            if len(host) > MAX_HOST_LEN:
                raise RuleError(f"{field}.host 超过 {MAX_HOST_LEN} 字符", f"{field}.host")
            mode = raw.get("mode")
            if not isinstance(mode, str):
                raise RuleError(f"{field}.mode 必须是字符串", f"{field}.mode")
            mode = mode.strip().lower()
            if mode not in SITE_MODES:
                raise RuleError(
                    f"{field}.mode 当前只支持 {'/'.join(SITE_MODES)}：{raw.get('mode')}",
                    f"{field}.mode",
                )
            item = {"host": host, "mode": mode}
            if host in index:           # 同 host 重复：后者覆盖，避免客户端行为取决于顺序
                clean_sites[index[host]] = item
            else:
                index[host] = len(clean_sites)
                clean_sites.append(item)
        out["site_overrides"] = clean_sites

    return out


def warnings_for(rules: dict) -> list:
    """发布前告警（不拒收，只提示）：容易误杀正常影视目录的语义词。"""
    out = []
    for pat in rules.get("url_patterns") or []:
        for bad in _SOFT_BAD_PATTERNS:
            if bad in pat:
                out.append(
                    f"url_patterns 含语义化商业词 {bad}（{pat}）："
                    "正常影视目录可能带同样的片段，容易误杀"
                )
    return out


def dumps(rules: dict) -> str:
    """规范序列化：键排序 + 紧凑分隔符，同一份规则永远得到同一个字符串。"""
    return json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: str, fallback=None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else (fallback if fallback is not None else {})
    except Exception:  # noqa: BLE001 —— 库里的坏数据不能让整个接口 500
        log.warning("adfilter 规则包 JSON 解析失败，按空规则处理")
        return fallback if fallback is not None else {}


# ---------------------------------------------------------------------------
# 渠道
# ---------------------------------------------------------------------------
def normalize_channel(channel: str) -> str:
    """未知渠道一律按 stable —— 客户端带了个奇怪的值也不该 4xx。"""
    value = (channel or "").strip().lower()
    return value if value in CHANNELS else DEFAULT_CHANNEL


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def current(channel: str = DEFAULT_CHANNEL) -> dict:
    """
    当前生效的规则包：`{"version": int, "rules": dict, "channel": str}`。

    库里没有规则包（或数据库暂不可用）时返回 version=0 + 内置默认规则：
    客户端 version=0 时得到 changed=false（版本相同），继续用自己内置的那份，
    等价于「服务端还没发过规则」；已经持有更高版本的客户端也不会被降级。
    """
    ch = normalize_channel(channel)
    try:
        with database.engine.connect() as conn:
            row = conn.execute(
                select(adfilter_rule_packs.c.version, adfilter_rule_packs.c.rules_json)
                .where(adfilter_rule_packs.c.channel == ch)
                .order_by(adfilter_rule_packs.c.version.desc())
                .limit(1)
            ).first()
    except Exception as e:  # noqa: BLE001 —— 控制面不能因为查库失败就 500
        log.warning("adfilter 读取规则包失败，降级为内置默认: %s", str(e)[:160])
        return {"version": 0, "rules": default_rules(), "channel": ch}
    if not row:
        return {"version": 0, "rules": default_rules(), "channel": ch}
    return {"version": int(row[0]), "rules": _loads(row[1]), "channel": ch}


def get(version: int, channel: str = DEFAULT_CHANNEL):
    """取指定版本（管理端回滚/查看用）；不存在返回 None。"""
    ch = normalize_channel(channel)
    with database.engine.connect() as conn:
        row = conn.execute(
            select(
                adfilter_rule_packs.c.version, adfilter_rule_packs.c.rules_json,
                adfilter_rule_packs.c.note, adfilter_rule_packs.c.created_at,
            )
            .where(
                adfilter_rule_packs.c.channel == ch,
                adfilter_rule_packs.c.version == int(version),
            )
        ).mappings().first()
    if not row:
        return None
    return {
        "channel": ch,
        "version": int(row["version"]),
        "rules": _loads(row["rules_json"]),
        "note": row["note"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def list_versions(channel: str = DEFAULT_CHANNEL, limit: int = 20) -> list:
    """历史版本（**不含**规则正文，只给版本号/备注/时间/字节数）。"""
    ch = normalize_channel(channel)
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                adfilter_rule_packs.c.version, adfilter_rule_packs.c.note,
                adfilter_rule_packs.c.created_at,
                func.length(adfilter_rule_packs.c.rules_json).label("size"),
            )
            .where(adfilter_rule_packs.c.channel == ch)
            .order_by(adfilter_rule_packs.c.version.desc())
            .limit(max(1, int(limit)))
        ).mappings().all()
    return [{
        "version": int(r["version"]),
        "note": r["note"] or "",
        "size": int(r["size"] or 0),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]


# ---------------------------------------------------------------------------
# 发布 / 回滚
# ---------------------------------------------------------------------------
def _insert_next(ch: str, rules: dict, note: str) -> int:
    """
    插入「当前最大版本 +1」的一行，返回新版本号。

    用一条 INSERT ... SELECT MAX(version)+1 完成递增，避免「先读后写」的竞态；
    并发发布撞唯一索引时由调用方转成 409（管理员操作，重试一次即可）。
    """
    payload = dumps(rules)
    now = datetime.now()
    with database.write_txn() as conn:
        if database.is_sqlite():
            # SQLite 的 INSERT..SELECT 不能引用同表做聚合写入的语义差异较大，
            # 测试后端用「读 + 写」同一事务，唯一索引仍会挡住并发重复版本。
            cur = conn.execute(
                select(func.max(adfilter_rule_packs.c.version))
                .where(adfilter_rule_packs.c.channel == ch)
            ).scalar()
            version = int(cur or 0) + 1
            conn.execute(adfilter_rule_packs.insert().values(
                channel=ch, version=version, rules_json=payload,
                note=(note or "")[:255], created_at=now,
            ))
            return version
        version = conn.execute(
            text(
                "INSERT INTO adfilter_rule_packs (channel, version, rules_json, note, created_at) "
                "SELECT :ch, COALESCE(MAX(version), 0) + 1, :rules, :note, :ts "
                "FROM adfilter_rule_packs WHERE channel = CAST(:ch AS VARCHAR(16)) "
                "RETURNING version"
            ),
            {"ch": ch, "rules": payload, "note": (note or "")[:255], "ts": now},
        ).scalar()
    return int(version)


def publish(rules, channel: str = DEFAULT_CHANNEL, note: str = "") -> dict:
    """
    发布新版本：校验 → 版本 +1 入库。返回 {version, rules, warnings, channel}。

    校验失败抛 RuleError（调用方转 400），并发撞版本抛 IntegrityError（转 409）。
    """
    ch = normalize_channel(channel)
    clean = normalize(rules)
    # 唯一索引冲突（并发发布撞同一个 version）不在这里吞：交给路由层转 409，
    # 让管理员知道「重试一次」而不是看到一个笼统的 500。
    version = _insert_next(ch, clean, note)
    log.info("[adfilter] 规则包已发布 channel=%s version=%s（%s）", ch, version, note or "无备注")
    return {
        "channel": ch,
        "version": version,
        "rules": clean,
        "warnings": warnings_for(clean),
    }


def rollback(version: int, channel: str = DEFAULT_CHANNEL, note: str = "") -> dict:
    """
    回滚到历史版本：**不修改历史行**，用旧内容发一个新版本（version+1）。

    这样客户端只按「版本号变大就换」处理，不需要「服务端版本变小」的分支；
    回滚本身也可再回滚（历史全在）。
    """
    ch = normalize_channel(channel)
    src = get(version, ch)
    if not src:
        raise LookupError(f"渠道 {ch} 不存在版本 {version}")
    clean = normalize(src["rules"])
    label = (note or f"回滚至 v{version}")[:255]
    new_version = _insert_next(ch, clean, label)
    log.info(
        "[adfilter] 规则包已回滚 channel=%s v%s -> 新版本 v%s（%s）",
        ch, version, new_version, label,
    )
    return {
        "channel": ch,
        "version": new_version,
        "rolled_back_from": int(version),
        "rules": clean,
        "warnings": warnings_for(clean),
    }


def ensure_seed() -> None:
    """
    首次启动播种：stable 渠道没有任何规则包时写入 v1（内置默认）。

    为什么要有：不发一版，客户端 version=0 拉到的服务端版本也是 0，
    面板上「当前版本」永远是 0，管理员会以为功能没生效。播种一次后
    「服务端有 v1」是明确事实，之后一切改动都是 v2、v3…… 可追溯。

    v1.16.1 起顺带做 host 列的历史回填：必须放在「已有规则包就 return」之前，
    否则升级已有部署时永远走不到回填，老上报行在按站点统计里会整批消失。
    """
    _backfill_hosts()
    try:
        with database.engine.connect() as conn:
            exists = conn.execute(
                select(adfilter_rule_packs.c.version).limit(1)
            ).first()
        if exists:
            return
        version = _insert_next(DEFAULT_CHANNEL, default_rules(), "内置默认规则（首次启动播种）")
        log.info("[adfilter] 已播种默认规则包 v%s", version)
    except Exception:  # noqa: BLE001 —— 播种失败不影响服务，首次发布时会补上
        log.exception("[adfilter] 播种默认规则包失败（不影响启动）")


# ---------------------------------------------------------------------------
# 上报限流（进程内，按 Token 身份）
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_hits: dict = {}     # user_id -> [epoch 秒, ...]


def _prune(now: float) -> None:
    for uid, stamps in list(_hits.items()):
        kept = [t for t in stamps if now - t < 3600]
        if kept:
            _hits[uid] = kept
        else:
            _hits.pop(uid, None)


def check_rate(user_id: int) -> int:
    """
    记一次调用并返回 0；超限返回建议等待秒数（调用方转 429）。

    按「滚动 1 小时窗口」计数：App 正常每会话 1~2 次，60 次/小时足够宽松，
    但足以挡住被脚本刷爆（公网可写端点必须有一道闸）。
    """
    limit = max(1, config.ADFILTER_REPORT_RATE_PER_HOUR)
    now = time.time()
    with _rate_lock:
        _prune(now)
        stamps = _hits.setdefault(int(user_id), [])
        if len(stamps) >= limit:
            oldest = min(stamps)
            return max(1, int(3600 - (now - oldest)) + 1)
        stamps.append(now)
    return 0


def reset_rate(user_id: int = None) -> None:
    """清空限流计数（测试与排障用）。"""
    with _rate_lock:
        if user_id is None:
            _hits.clear()
        else:
            _hits.pop(int(user_id), None)


# ---------------------------------------------------------------------------
# 上报入库
# ---------------------------------------------------------------------------
def _as_int(value, field: str, *, minimum: int = 0):
    """接受整数或「整数值的浮点」（JSON 客户端常见 1.0）；其它返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and abs(value - round(value)) > 1e-9:
        return None
    ivalue = int(round(value))
    return ivalue if ivalue >= minimum else None


def ingest(items: list, user_id: int) -> dict:
    """
    批量入库上报明细，返回 `{"accepted": n, "dropped": m, "reasons": {...}}`。

    丢弃语义（与契约一致且稳定，不随批次变化）：
      - url 缺失/空、超 2048 字符
      - 数值字段缺失、非整数、为负
      - removed_ms > total_ms（自相矛盾的数据）
      - at 与服务端时间偏差超过 ADFILTER_AT_SKEW_SEC
    类型错误**整批 4xx** 由路由层负责（items 不是数组、单条不是对象），
    这里只做「单条数据不合规 → 丢该条」，避免一条脏数据毁掉整批。

    幂等：按 (user_id, url_hash, at) 去重，重复上报不产生新行，
    accepted 只统计真正插入的条数（逐条取 rowcount，SQLite/PG 口径一致）。
    """
    now = int(time.time())
    skew = max(60, int(config.ADFILTER_AT_SKEW_SEC))
    rows, reasons = [], {}

    def drop(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for raw in items:
        if not isinstance(raw, dict):
            drop("not_object")
            continue
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            drop("url_invalid")
            continue
        url = url.strip()
        if len(url) > MAX_URL_LEN:
            drop("url_too_long")
            continue

        rule_version = _as_int(raw.get("rule_version", 0), "rule_version")
        removed_runs = _as_int(raw.get("removed_runs", 0), "removed_runs")
        removed_ms = _as_int(raw.get("removed_ms", 0), "removed_ms")
        total_ms = _as_int(raw.get("total_ms", 0), "total_ms")
        at = _as_int(raw.get("at"), "at", minimum=1)
        if None in (rule_version, removed_runs, removed_ms, total_ms, at):
            drop("field_invalid")
            continue
        if removed_ms > total_ms:
            drop("removed_gt_total")
            continue
        if abs(now - at) > skew:
            drop("at_skew")
            continue

        rows.append({
            "user_id": int(user_id),
            "url": url,
            "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "host": host_of(url),   # v1.16.1：写时归一，读侧直接 GROUP BY
            "rule_version": rule_version,
            "removed_runs": removed_runs,
            "removed_ms": removed_ms,
            "total_ms": total_ms,
            "at": at,
            "created_at": datetime.now(),
        })

    accepted = 0
    if rows:
        stmt = database.dialect_insert(adfilter_reports).on_conflict_do_nothing(
            index_elements=[
                adfilter_reports.c.user_id,
                adfilter_reports.c.url_hash,
                adfilter_reports.c.at,
            ]
        )
        with database.write_txn() as conn:
            for row in rows:
                result = conn.execute(stmt.values(**row))
                rc = result.rowcount
                # SQLite 的 executemany 口径是 -1；这里逐条执行，正常都是 1/0。
                accepted += 1 if (rc is None or rc > 0) else 0
    if accepted or reasons:
        log.info(
            "[adfilter] 上报 user=%s 入库 %d/%d 条，丢弃 %s",
            user_id, accepted, len(items), reasons or "无",
        )
    # dropped 只统计「数据不合规」的条数；重复上报单独用 duplicates 表达 ——
    # 两者混在一起时，客户端反复重试会和「脏数据」看起来一模一样，不好排障。
    invalid = len(items) - len(rows)
    return {
        "accepted": accepted,
        "dropped": invalid,
        "duplicates": len(rows) - accepted,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 聚合统计（管理端，不含 url —— 播放地址属于用户隐私，不对外暴露）
# ---------------------------------------------------------------------------
def stats(days: int = 7, limit: int = 0, min_reports: int = 0) -> dict:
    """
    上报量概览 + **按站点分布**（v1.16.1：全站平均会稀释单站误杀，必须分组看）。

    统计口径（写死在服务端，客户端不参与，前端只负责展示）：
      - host = 播放列表 URL 的 hostname，小写、去端口/userinfo/路径；
      - 单条 removed_ratio = removed_ms / total_ms；total_ms=0 的单条按 0 计入；
      - avg_removed_ratio / max_removed_ratio = 窗口内**逐条 ratio 的算术平均 / 最大**
        （不是 sum(removed_ms)/sum(total_ms)，后者会让长片压过短片，与 max 也不同源）；
      - 窗口 = `at >= now - days*86400`（与既有 recent 一致，按客户端上报时刻）；
      - distinct_urls 按 url_hash 去重，不返回任何 url 原文。

    异常判定（只**标记**，绝不自动改规则包）：样本数 >= min_reports 且
    avg_removed_ratio > ADFILTER_HOST_SUSPECT_RATIO 或
    avg_removed_runs > ADFILTER_HOST_SUSPECT_RUNS。min_reports 是必要的护栏：
    一条脏上报不该把正常站点标成「规则过激」。
    """
    days = max(1, int(days))
    limit = max(1, min(int(limit) or config.ADFILTER_HOST_STATS_LIMIT,
                       config.ADFILTER_HOST_STATS_MAX_LIMIT))
    min_reports = max(1, int(min_reports) or config.ADFILTER_HOST_MIN_REPORTS)
    since = int(time.time()) - days * 86400
    try:
        with database.engine.connect() as conn:
            total, urls, removed, total_ms = conn.execute(
                select(
                    func.count(),
                    func.count(func.distinct(adfilter_reports.c.url_hash)),
                    func.coalesce(func.sum(adfilter_reports.c.removed_ms), 0),
                    func.coalesce(func.sum(adfilter_reports.c.total_ms), 0),
                ).select_from(adfilter_reports)
            ).first()
            recent = conn.execute(
                select(func.count()).where(adfilter_reports.c.at >= since)
            ).scalar() or 0
            last_at = conn.execute(select(func.max(adfilter_reports.c.at))).scalar()

            # 逐条 ratio 必须用 CAST(... AS FLOAT)：两列都是 BIGINT，
            # PG 和 SQLite 都会做整数除法，不转类型的话结果恒为 0。
            ratio_expr = case(
                (adfilter_reports.c.total_ms > 0,
                 cast(adfilter_reports.c.removed_ms, Float) / adfilter_reports.c.total_ms),
                else_=0.0,
            )
            grouped = conn.execute(
                select(
                    adfilter_reports.c.host,
                    func.count().label("reports"),
                    func.count(func.distinct(adfilter_reports.c.url_hash)).label("distinct_urls"),
                    func.avg(ratio_expr).label("avg_ratio"),
                    func.max(ratio_expr).label("max_ratio"),
                    func.avg(adfilter_reports.c.removed_runs).label("avg_runs"),
                    func.max(adfilter_reports.c.at).label("last_at"),
                )
                .select_from(adfilter_reports)
                .where(
                    adfilter_reports.c.at >= since,
                    adfilter_reports.c.host != "",          # 待回填的老行
                    adfilter_reports.c.host != HOST_UNKNOWN,  # 解析不出域名的行
                )
                .group_by(adfilter_reports.c.host)
            ).all()
    except Exception as e:  # noqa: BLE001
        log.warning("adfilter 统计失败: %s", str(e)[:160])
        return {"available": False}

    ratio = round(removed / total_ms, 4) if total_ms else 0.0
    hosts_all = []
    for row in grouped:
        m = row._mapping
        n = int(m["reports"] or 0)
        avg_ratio = round(float(m["avg_ratio"] or 0), 4)
        max_ratio = round(float(m["max_ratio"] or 0), 4)
        avg_runs = round(float(m["avg_runs"] or 0), 2)
        reasons = []
        if n >= min_reports:
            if avg_ratio > config.ADFILTER_HOST_SUSPECT_RATIO:
                reasons.append("avg_removed_ratio")
            if avg_runs > config.ADFILTER_HOST_SUSPECT_RUNS:
                reasons.append("avg_removed_runs")
        hosts_all.append({
            "host": m["host"],
            "reports": n,
            "distinct_urls": int(m["distinct_urls"] or 0),
            "avg_removed_ratio": avg_ratio,
            "max_removed_ratio": max_ratio,
            "avg_removed_runs": avg_runs,
            "last_at": int(m["last_at"]) if m["last_at"] else None,
            "suspicious": bool(reasons),   # 给面板高亮，不代表已止血
            "reasons": reasons,
        })
    # 疑似过激的排最前，其次按平均删除占比；同值再按样本量，保证结果稳定可复现
    hosts_all.sort(key=lambda h: (
        not h["suspicious"], -h["avg_removed_ratio"], -h["reports"], h["host"],
    ))
    return {
        # ↓ 以下是 v1.16.0 既有字段，一个都没改；老调用方零影响
        "available": True,
        "total": int(total or 0),
        "recent": int(recent or 0),
        "days": days,
        "distinct_urls": int(urls or 0),
        "removed_ratio": ratio,          # 删除时长 / 总时长，突增说明规则过激
        "last_at": int(last_at) if last_at else None,
        # ↓ v1.16.1 新增
        "hosts": hosts_all[:limit],
        "hosts_total": len(hosts_all),   # 窗口内站点总数（可能大于 hosts 长度）
        "suspicious_count": sum(1 for h in hosts_all if h["suspicious"]),
        "thresholds": {
            "avg_removed_ratio": config.ADFILTER_HOST_SUSPECT_RATIO,
            "avg_removed_runs": config.ADFILTER_HOST_SUSPECT_RUNS,
            "min_reports": min_reports,
        },
        "host_limit": limit,
    }
