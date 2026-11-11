"""
能力清单（v1.14.0 / B-10）：`/healthz.capabilities` 的唯一真源。

为什么要有这个清单
------------------
前后端发版永远不可能同时完成。客户端需要一条稳定判据回答「这台服务器支持
?tag= 吗 / 支持 mode=backfill_play_from 吗」，而不是靠 version 字符串比较
（版本号只增不减，但某项能力可能被灰度关掉）。所以：

    能力是否可用 -> 看 capabilities 里有没有这个名字，不看版本号。

规则（与客户端约定）
--------------------
1. 数组形态（客户端也接受 `{name: bool}` 对象形态，这里固定产出数组，更省字节）。
2. **只声明真正可用的能力**：`tag_search` 要等索引回填完（`tags.ready()`），
   半成品就声明会让客户端立刻把检索切过去、然后搜不到东西。
3. 数据库不可用时收缩为极小子集：不能查库的能力（tag_search / related …）
   一律不声明。`/healthz` 本身仍返回 200，用 `status: degraded` 表达。
4. 灰度期用环境变量单独开关：`CAPABILITIES_DISABLE` / `CAPABILITIES_EXTRA`。

新增能力时只改本文件：客户端不需要知道它背后是哪张表、哪个模块。
"""
from . import config


def _static() -> list:
    """
    不依赖数据库状态的能力（代码上线即成立）。

    每项都对应一个已实现的接口/参数，注释里写清「客户端据此做什么」，
    避免有人只加名字不实现，把客户端带进沟里。
    """
    return [
        # mode=backfill_play_from（v1.11.0）：回填上游 vod_play_from，
        # 客户端控制台的「回填线路」按钮据此决定是否显示
        "backfill_play_from",
        # 结构化播放线路（v1.11.0）：play_lines[].playable/type/from/original_index
        "play_line_filter",
        # 统一错误码（B-11）：业务失败一律非 2xx + {code, error}，
        # 客户端据此把「网络错误」文案换成服务端给的可读原因
        "error_codes",
        # Token 权限粒度（B-08）：/auth/tokens 可签发 read/admin 两档 Token。
        # 注意：声明 ≠ 已强制校验（由 TOKEN_SCOPE_ENFORCE 控制），
        # 客户端按「我能创建 read Token」这一事实决定控制台用哪枚。
        "token_scope",
        # 明星作品/相关推荐（v1.9.x）：/star/works、/star/related、/star/tags/*
        "star_tags",
        "related",
        # 分页语义固化（B-04）：pagecount 恒以 page_size 为基准，缺省 18
        "paging_v2",
        # 未成年模式（v1.15.0）：请求带 X-Minor-Mode / ?minor_mode=1 时，
        # 服务端屏蔽 is_adult=1 的源站及其全部内容；源站清单每项带 is_adult。
        # 静态项：声明的是「服务端已实现这套过滤」，不依赖库里有没有标记。
        "minor_mode",
    ]


# 需要查库才能兑现的能力：数据库不可用时必须从清单里摘掉，否则客户端会
# 去调用一个必然 500 的接口。其余静态项（错误码、分页语义、路由形态）
# 跟库没关系，降级时照常声明 —— 它们描述的是「服务端行为契约」，不是数据。
_DB_DEPENDENT = frozenset({"star_tags", "related", "tag_search"})


def snapshot(db_ok: bool = True) -> list:
    """
    当前能力清单（有序，便于比对与日志）。

    顺序稳定：静态项在前，依赖数据的项在后 —— 灰度排查时 diff 两版输出更直观。
    """
    caps = [c for c in _static() if db_ok or c not in _DB_DEPENDENT]
    if db_ok:
        # 标签精确检索（B-01）：索引回填完成才声明。导入放在函数内，
        # 避免 healthz 与 tags 之间形成 import 环（tags 依赖 models/database）。
        if config.TAG_SEARCH_ENABLED:
            try:
                from . import tags

                if tags.ready():
                    caps.append("tag_search")
            except Exception:  # noqa: BLE001 —— 探测失败就当没就绪
                pass
    caps = [c for c in caps if c not in config.CAPABILITIES_DISABLE]
    for extra in config.CAPABILITIES_EXTRA:
        if extra not in caps:
            caps.append(extra)
    return caps


def detailed(db_ok: bool = True) -> dict:
    """
    对象形态（`?detail=1` 时用）：`{name: bool}`，便于人读「为什么没有某项」。

    客户端只用数组；这个给运维和面板用。
    """
    active = set(snapshot(db_ok=db_ok))
    return {name: (name in active) for name in _static() + ["tag_search"]}
