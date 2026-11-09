"""
统一响应包络与错误码（v1.14.0 / B-11）。

契约（客户端按此判定，改动即破坏兼容）
------------------------------------
1. 成功：HTTP 2xx 且 `code == 0`（值由 config.API_SUCCESS_CODE 决定，默认 0）。
   列表字段恒为 `list`，详情恒为 `detail` —— 客户端用 Gson 按字段名反序列化。
2. 业务失败：HTTP 4xx/5xx，body 为 `{"code": <int>, "error": "<语义串>", "detail": "<中文>"}`
   - `code`   给程序判断（40101 = Token 无效，40301 = 权限不足 …）
   - `error`  稳定英文短语，客户端可做 i18n / 埋点分类，不随文案改版变化
   - `detail` 人可读中文（**保留**：v1.13 及更早的客户端与面板就读这个字段）
   - `message` 与 detail 同值（部分客户端读 message，纯新增，不影响老客户端）
3. 鉴权失败 401 / 权限不足 403 / 参数错误 400 / 不存在 404 均按上表返回，
   **不再**出现「HTTP 200 + code:1 表示失败」。

为什么 detail 和 message 都留：老客户端只认 detail，新客户端读 message/error，
两边同值同时给，比让任何一方解析失败便宜得多。
"""
from . import config

# 成功响应的 code（可配置，见 config.API_SUCCESS_CODE 的注释）
SUCCESS_CODE = config.API_SUCCESS_CODE

# HTTP 状态 -> (业务码, 语义串)
STATUS_CODES = {
    400: (40001, "bad_request"),
    401: (40101, "invalid_token"),
    402: (40001, "bad_request"),          # 不用；占位避免 fallback 误判
    403: (40301, "insufficient_scope"),
    404: (40401, "not_found"),
    405: (40001, "method_not_allowed"),
    409: (40901, "conflict"),
    413: (40001, "payload_too_large"),
    422: (40001, "bad_request"),
    429: (42901, "rate_limited"),
    500: (50001, "internal_error"),
    502: (50001, "internal_error"),
    503: (50001, "internal_error"),
    504: (50001, "internal_error"),
}

# 单独列出的语义码（不走 HTTP 状态表时用）
INVALID_TOKEN = 40101
TOKEN_EXPIRED = 40102        # 预留：Token 过期（当前 Token 不过期，仅轮换）
INSUFFICIENT_SCOPE = 40301
BAD_REQUEST = 40001
NOT_FOUND = 40401


def code_for(status: int) -> tuple:
    """HTTP 状态 -> (业务码, 语义串)。未登记的状态按 4xx/5xx 大类兜底。"""
    hit = STATUS_CODES.get(status)
    if hit:
        return hit
    return (50001, "internal_error") if status >= 500 else (40001, "bad_request")


def error_body(status: int, detail: str = "", *, error: str = None,
               field: str = None, required: str = None, **extra) -> dict:
    """
    构造错误响应体。

    `field`   参数校验失败时指出具体字段（客户端可高亮对应输入框）
    `required` 权限不足时说明需要什么 scope（403 专用）
    `extra`   其它要透出的诊断信息（如 retry_after）
    """
    code, slug = code_for(status)
    body = {
        "code": code,
        "error": error or slug,
        "detail": detail or slug,
        "message": detail or slug,
    }
    if field:
        body["field"] = field
    if required:
        body["required"] = required
    if extra:
        body.update(extra)
    return body
