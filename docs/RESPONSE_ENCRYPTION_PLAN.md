# API 响应加密 / 请求签名 —— 方案与实施设计

> **状态：规划稿，未实施**（截至 v1.11.0 服务端未启用任何响应加密或请求签名；本文仅为待评估方案）
> 版本：v1.7.x 起草，v1.11.0 复核仍适用
> 目标读者：项目维护者 + 执行落地的 AI
> 前置结论：**响应加密可行，但代价是传输体积涨约 6 倍且 gzip 完全失效**；签名方案零带宽代价。
> 建议先做 C（签名），再视需要叠加 A（加密）。

---

## 1. 目标与非目标

| | 内容 |
|---|---|
| **目标** | 让「用浏览器/curl 直接 GET API」拿不到可读数据；提高爬虫门槛；密钥定期轮换，泄露可收敛 |
| **非目标** | ❌ 防不住逆向 App 的人（key 最终在内存中明文）<br>❌ 不保护数据库静态数据（那是存储加密，另一回事）<br>❌ 不替代 TLS（HTTPS 仍是传输安全的基础，本方案是叠加层） |

**威胁模型必须写清楚**，否则容易做成「心理安全」：

| 攻击者 | A 加密 | C 签名 |
|---|---|---|
| 浏览器/curl 直接请求 | ✅ 只见密文 | ✅ 401/403 |
| 有 Token 的人抓包 | ✅ 挡（但可调 /auth/key，见 §4.3） | ✅ 挡 |
| 会写爬虫的人 | ⚠️ 需逆向 App | ⚠️ 需逆向签名算法 |
| 逆向 APK/IPA | ❌ | ⚠️ 同样可逆向出算法与 Token |
| 拿到数据库 | ❌ 无关 | ❌ 无关 |

---

## 2. 实测数据（决定选型的依据）

测试环境：本机 Python 3.12 + pycryptodome，样本为 30 条真实分布的影视记录。

| 指标 | 列表页（30 条） | detail 页（30 条，含 content/play_url） |
|---|---|---|
| 原始 JSON | 8,504 B | 61,362 B |
| 当前 gzip(6) 后 | 1,866 B（4.6×） | 14,027 B（4.4×） |
| AES-256-GCM + base64 后 | **11,431 B** | **81,911 B** |
| 相对当前传输体积 | **+6.1×** | **+5.8×** |
| 加密后还能 gzip 吗 | ❌ 压缩率 1.0（随机字节） | ❌ |
| 服务端加密耗时 | **0.090 ms/次** | 约 0.6 ms/次 |
| 客户端解密耗时 | < 0.5 ms | < 3 ms |

结论：
- **CPU 完全不是问题**（服务端 0.09ms、客户端亚毫秒）。
- **带宽是唯一真实成本**：加密与压缩互斥，反代/CDN 的 gzip 从此失效。
- 按每天翻 200 页估算：约 +2.3 MB/天。单人使用可忽略，多人共享需评估。

---

## 3. 三条路线的取舍

| | A 轻量加密 | B 加固加密 | C 请求签名 |
|---|---|---|---|
| 核心做法 | 服务端下发轮换 DEK，响应整体 AES-256-GCM | A + `resp_key = HKDF(session_secret, req_nonce)` | `X-Sign = HMAC(token, method\|path\|ts\|nonce)` |
| 抓一次响应能否解其他 | ⚠️ 能 | ✅ 不能 | — |
| 防重放 | ❌ | ✅ | ✅ |
| 带宽代价 | +6× | +6× | **0** |
| gzip | ❌ 失效 | ❌ 失效 | ✅ 保留 |
| 服务端改动 | 约 180 行 | 约 260 行 | 约 90 行 |
| 客户端改动 | 1 个拦截器（约 60 行） | 拦截器 + HKDF（约 120 行） | 1 个拦截器（约 30 行） |
| 调试难度 | 高（Network 全是密文） | 高 | 低 |
| 缓存 | ❌ ETag/CDN 全废 | ❌ | ✅ |

**推荐路径**：先 C，一小时内上线；观察一周后再决定是否叠 A。A 与 C 不冲突，C 的中间件可预留 `X-Sign` 校验位。

---

## 4. 方案 A 详细设计

### 4.1 密钥层级

```
KEK  = env DATA_KEK (base64 32B)，永不下发、不入库
 └─ AES-256-GCM 加密后存 settings 表
      └─ DEK (32B)：下发给客户端，带 kid 标识，定期轮换
```

- 若 `DATA_KEK` 未配置：自动随机生成并存 `settings.data_kek`（降级模式，能防「只读 API 不读库」的攻击者，不防「拖库」）。
- **持久化时必须用 KEK 加密 DEK**，否则 settings 表泄露 = 客户端密钥泄露。

### 4.2 settings 表键位

| key | 含义 |
|---|---|
| `data_kek` | 仅当 env 未提供时存在 |
| `data_dek_cur` / `data_dek_cur_kid` / `data_dek_cur_at` | 当前 DEK（KEK 加密后的密文） |
| `data_dek_prev` / `data_dek_prev_kid` / `data_dek_prev_at` | 宽限期内的上一把 |

`kid` 格式：`k{YYYYMMDDHH}{4位随机hex}`，例如 `k2026110408a3f1`。带随机后缀是为了同一小时内多次轮换不撞号。

### 4.3 密钥下发接口

```
GET /api/v1/auth/key
Authorization: Bearer <token>          ← 必须鉴权，且不在 PUBLIC_PATHS 白名单
X-Key-Kid: k2026110408a3f1             ← 可选，客户端声明自己当前持有哪把

200 OK
{
  "kid": "k2026110408a3f1",
  "key": "base64(32B)",
  "expires_at": 1762264800,      // 该 key 停止签发新响应的时刻
  "grace_until": 1762286400      // 已签发响应的最后可解时刻
}
```

**这是 A 方案最大的弱点，必须正视**：能调到这个接口就等于拿到 key。因此：
- 该接口必须要求 Token/会话（与 `/videos` 同级鉴权）
- 响应头 `Cache-Control: no-store`
- 可选的进一步加固：把 key 与 Token 绑定（不同 Token 下发不同 DEK 派生值），但这会引入「多客户端多密钥」的管理复杂度，**V1 不做**

### 4.4 响应信封格式

```json
{
  "_enc": "A256GCM",
  "kid": "k2026110408a3f1",
  "n": "base64(12B nonce)",
  "d": "base64(ciphertext || tag)",
  "t": 1762243200
}
```

- `nonce`：12 字节，**每次响应必须随机生成**。同 key 下 nonce 重复 = 密钥流复用 = 明文可恢复且 tag 可伪造，属灾难级错误。
- `AAD = f"{kid}|{t}"` 的 UTF-8 字节。绑定时间戳可防止信封被无限期重放；绑定 kid 防止跨轮换误用。
- `t` 仅作 AAD 用途，不做服务端时间窗校验（响应是服务端发出的，不存在「过期响应」问题）。
- **不使用 path 作为 AAD**：客户端拼接 path 时容易因尾斜杠/编码差异导致解密失败，收益不足以抵消兼容风险。

### 4.5 加密中间件

位置：**最外层**（在 `add_middleware(CORSMiddleware)` 之后再注册），顺序为：

```
请求：encrypt → CORS → auth_guard → route
响应：route → auth_guard → CORS → encrypt
```

放在最外层的原因：CORS 已加完头，encrypt 复制 headers 后再替换 body；`auth_guard` 生成的 401 也会流经这里，但会被状态码判断拦下（不加密）。

```python
@app.middleware("http")            # 必须放在最后一个注册
async def _encrypt_response(request, call_next):
    response = await call_next(request)
    if not should_encrypt(request, response):
        return response
    body = b"".join([c async for c in response.body_iterator])
    env = crypto.encrypt_envelope(body, kid=request.headers.get("x-key-kid"))
    headers = dict(response.headers)
    headers.pop("content-encoding", None)     # 原 gzip 已无意义
    return Response(
        content=json.dumps(env, ensure_ascii=False).encode(),
        media_type="application/json",
        status_code=response.status_code,
        headers=headers,
    )
```

`should_encrypt()` 判定（**顺序即优先级**）：

1. 全局开关 `RESPONSE_ENCRYPTION=off` → False
2. `auto` 模式且请求未带 `Accept: application/vnd.vodsync.enc+json` → False（老客户端兼容）
3. `response.status_code >= 400` → False（**401/500 必须明文**，否则客户端拿不到 key 时死锁）
4. path 在豁免表 → False
5. `Content-Type` 不是 `application/json` → False
6. 否则 True

豁免表：`/healthz`、`/docs`、`/redoc`、`/openapi.json`、`/login`、`/favicon.ico`、`/static/*`

### 4.6 轮换流程

```
定时任务（每小时检查一次）:
  if now - data_dek_cur_at > DATA_KEY_ROTATE_HOURS:
      data_dek_prev ← data_dek_cur
      data_dek_cur  ← 新随机 32B + 新 kid
  if now - data_dek_prev_at > DATA_KEY_GRACE_HOURS:
      清空 prev
```

- 默认 `ROTATE=24h`、`GRACE=6h`
- **轮换不能让在途客户端失败**：客户端请求可带 `X-Key-Kid`，若该 kid 仍在 `prev` 且未过宽限期，服务端就用它加密；否则用 `cur`，客户端收到未知 kid 后重新拉 key 并重试一次。

### 4.7 客户端实现

**Android (OkHttp Interceptor)**

```kotlin
class DecryptInterceptor(private val keyStore: KeyStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        var req = chain.request()
        keyStore.kid?.let { req = req.newBuilder().header("X-Key-Kid", it).build() }
        var resp = chain.proceed(req)
        val body = resp.peekBody(Long.MAX_VALUE).string()
        if (!body.startsWith("{\"_enc\"")) return resp          // 明文（错误响应/未加密）

        val env = Json.parseToJsonElement(body).jsonObject
        val kid = env["kid"]!!.jsonPrimitive.content
        val key = keyStore.get(kid) ?: keyStore.refresh()        // 未知 kid → 拉一次
        val plain = AesGcm.decrypt(
            keyB64 = key,
            nonce = Base64.decode(env["n"]!!.jsonPrimitive.content),
            data = Base64.decode(env["d"]!!.jsonPrimitive.content),
            aad = "$kid|${env["t"]!!.jsonPrimitive.long}".toByteArray()
        )
        return resp.newBuilder()
            .body(plain.toResponseBody("application/json".toMediaType()))
            .build()
    }
}
```

要点：
- 用 `peekBody` 不消费原 body，避免破坏响应流。
- **解密必须在 IO 线程**（Interceptor 本身就在 OkHttp 的 IO 线程，天然满足）。切勿在 RecyclerView `onBindViewHolder` 里同步解密——快速滚动时每帧 3~5 个 item × 3 字段会掉帧。
- 解密结果建议内存 LRU 缓存（列表页明文约 8KB，缓存 50 页 = 400KB，可接受）。

**iOS (URLSession + CryptoKit)**

```swift
let box = try AES.GCM.SealedBox(
    nonce: Data(base64Encoded: env.n)!,
    ciphertext: ct,                       // 去掉末尾 16B tag
    tag: tag
)
let aad = "\(env.kid)|\(env.t)".data(using: .utf8)!
let plain = try AES.GCM.open(box, using: key, authenticating: aad)
```

**Flutter (Dio Interceptor)**：`dio.interceptors.add(DecryptInterceptor())`，用 `pointycastle` 或 `cryptography` 包，逻辑同上。

**Web 面板 (WebCrypto)**

```js
async function decryptEnvelope(env, keyB64) {
  const raw = Uint8Array.from(atob(keyB64), c => c.charCodeAt(0));
  const k = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt']);
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64(env.n), additionalData: new TextEncoder().encode(`${env.kid}|${env.t}`), tagLength: 128 },
    k, b64(env.d)          // WebCrypto 接受 ciphertext||tag 拼接形式
  );
  return JSON.parse(new TextDecoder().decode(pt));
}
```

面板只需改 `app/web/index.html:270` 的 `api()` 一处：拿响应 → 判断 `_enc` → 解密 → 返回对象。key 只放**内存变量**，不要写 localStorage（XSS 直接偷走）。

### 4.8 配置项

```bash
RESPONSE_ENCRYPTION=off        # off | on | auto（auto = 客户端声明才加密，用于灰度）
DATA_KEK=                      # base64 32B；留空则自动生成并降级存库
DATA_KEY_ROTATE_HOURS=24
DATA_KEY_GRACE_HOURS=6
ENCRYPT_EXEMPT_PATHS=/healthz,/docs,/redoc,/openapi.json,/login,/favicon.ico
```

---

## 5. 方案 C 详细设计（签名）

### 5.1 请求头

```
Authorization: Bearer <token>
X-Ts: 1762243200
X-Nonce: 9f3c8a7b6d5e4f2a
X-Sign: base64(HMAC-SHA256(token, f"{method}\n{path}\n{ts}\n{nonce}"))
```

### 5.2 服务端校验

1. `RESPONSE_SIGNING=off` → 放行（默认 off，灰度）
2. `auto` 模式且请求未带 `X-Sign` → 放行（老客户端兼容）
3. 缺 `X-Sign`/`X-Ts`/`X-Nonce` → 401 `{"code":401,"detail":"signature required"}`
4. `|now - ts| > SIGN_TTL_SECONDS`（默认 300）→ 401 `{"code":401,"detail":"timestamp expired"}`
5. `nonce` 在窗口内已出现 → 401 `{"code":401,"detail":"nonce reused"}`（内存 LRU，容量 10000，按分钟清理）
6. HMAC 不匹配 → 401
7. 通过 → 放行

注意事项：
- `path` 用 `request.url.path`，**不含 query**（query 顺序不稳定，容易误判）。
- 用 `hmac.compare_digest` 做常数时间比较，不要用 `==`。
- nonce 缓存是**进程级**的：多副本部署时需换成 Redis；本项目单实例，够用。

### 5.3 客户端

```kotlin
class SignInterceptor(private val token: String) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val req = chain.request()
        val ts = (System.currentTimeMillis() / 1000).toString()
        val nonce = ByteArray(8).also { SecureRandom().nextBytes(it) }.toHex()
        val msg = "${req.method}\n${req.url.encodedPath}\n$ts\n$nonce"
        val sign = Base64.encodeToString(hmacSha256(token, msg), NO_WRAP)
        return chain.proceed(req.newBuilder()
            .header("X-Ts", ts).header("X-Nonce", nonce).header("X-Sign", sign).build())
    }
}
```

---

## 6. 迁移与回滚

| 阶段 | 动作 | 回滚 |
|---|---|---|
| 1 | 部署服务端，`RESPONSE_ENCRYPTION=auto`（默认不加密） | 无影响 |
| 2 | App 加解密拦截器 + 带 `Accept: application/vnd.vodsync.enc+json` | App 不升级就继续走明文 |
| 3 | 观察 1~2 天，确认无解密失败（服务端记录 `enc_fail` 计数） | 改回 `auto` |
| 4 | 全部客户端升级后切 `on`（强制加密） | 改回 `auto` 立即恢复 |
| 5 | 同步加 C 签名，同样从 `auto` 开始 | 同上 |

**关键**：没有 `auto` 模式就不要上这个功能。一旦客户端解密有 bug，`on` 模式下全线断服，且无法通过 App 更新快速修复（用户不升级就永久不可用）。

---

## 7. 验收清单

- [ ] 同一响应连续加密两次，密文与 nonce 均不同
- [ ] nonce 长度恒为 12 字节；AAD 拼装与客户端 `kid|t` 完全一致
- [ ] 401/403/500 响应始终明文（否则客户端死锁）
- [ ] `/healthz`、`/docs`、`/openapi.json` 不受影响，Docker healthcheck 仍通过
- [ ] 轮换瞬间（cur→prev）在途请求不失败；带旧 kid 的请求仍可解
- [ ] 超出宽限期的旧 kid → 客户端拉新 key 后重试成功
- [ ] `auto` 模式下不带 `Accept` 头返回明文，带则返回信封
- [ ] 面板 `/` 在加密开启后仍能正常加载列表（Network 里是密文，页面正常）
- [ ] gzip 已对加密响应关闭（`content-encoding` 被移除，避免二次压缩浪费 CPU）
- [ ] 日志不打印 key/nonce/明文片段
- [ ] 探针接口的 `Cache-Control` 与加密态下 ETag 行为已确认

---

## 8. 三个必须避开的坑

1. **GCM nonce 复用**：同 key 下 nonce 重复 → 密钥流复用 → 明文可恢复 + tag 可伪造。必须每次 `os.urandom(12)`，绝不从计数器/时间戳派生（多线程会撞）。
2. **401 被加密导致死锁**：客户端没 key 时收到密文 401，既解不开也不知道该去拉 key。所有 4xx/5xx 一律明文。
3. **缓存与 ETag**：随机 nonce 让相同内容的密文不同，`ETag` 必须基于**明文**计算并放响应头；否则客户端缓存命中率归零，每次都全量下载。

---

## 9. 实施工作量

| 项 | A | C |
|---|---|---|
| 服务端新增模块 | `app/response_crypto.py`（约 180 行） | `app/signing.py`（约 90 行） |
| 中间件 | 1 个 | 1 个 |
| 新接口 | `GET /api/v1/auth/key` | — |
| 配置项 | 5 个 | 4 个 |
| 面板改动 | `api()` 一处 + WebCrypto 约 40 行 | 0 |
| 测试 | `test_v180_crypto.py` 约 40 项 | `test_v180_signing.py` 约 25 项 |
| 文档 | 本文档 + `APP_TOKEN_UPGRADE.md` 增章 | 同上 |

---

## 10. 待决策项

1. 先做 C 还是直接 A（或 A+C）？
2. 面板是否一起加密？一起加密则调试需 `?plain=1` 开关（仅 localhost/已登录会话生效）。
3. 是否接受 detail 页 82KB 的传输体积？若不接受，可只对列表页加密、detail 页保持明文——但那样爬虫只需请求 detail 接口即可，等于白做。
