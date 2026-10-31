# App 接入 API Token —— 升级施工单（给 AI 用）

> **这份文档的用法**：整篇复制给你的 AI 编程助手，让它按里面的改动点升级现有 App。
> 对应服务端版本：**v1.5.1**（鉴权自 v1.5.0 引入）｜ 文档版本：`2026-10-31`

---

## 0. 一句话结论

服务端从 v1.5.0 起默认开启访问控制：**除了 `/healthz` 和 `/api/v1/auth/*`，所有接口都必须携带 API Token**。
App 侧唯一必须做的三件事：

1. 每个请求加一个请求头 `Authorization: Bearer <token>`；
2. 设置页加「服务器地址 + API Token」两个输入项并持久化；
3. 把 401 从「网络错误」改成「Token 无效 → 引导用户重新填」。

**接口路径、参数、响应 JSON 结构全部不变**，不需要动业务代码。

| 项 | 是否要改 |
|---|---|
| 业务接口（`/api/v1/videos`、`/api/v1/search`、`/api/v1/sources` …）路径与参数 | ❌ 不变 |
| JSON 响应结构（`{code,total,list}` 、`{code,data}`） | ❌ 不变 |
| API 根地址 / 端口 | ❌ 不变 |
| 请求头 | ✅ 新增 `Authorization: Bearer <token>` |
| 设置页 / 首次启动引导 | ✅ 新增「服务器地址」「API Token」两项 |
| 401 错误处理 | ✅ 改为引导用户补 Token |
| 面板登录接口 `/api/v1/auth/login` | ❌ **App 不要接**（那是浏览器管理面板用的账号密码，客户端不需要） |

---

## 1. 鉴权模型（先理解，再动手）

服务端有两层，**互不替代**：

| 层 | 谁用 | 凭证 | 有效期 |
|---|---|---|---|
| 面板登录 | 只有浏览器打开的管理面板 `/` | 管理密码（默认 `666666`） | 会话 cookie，默认 7 天 |
| API Token | 所有 API 调用方（**你的 App**） | `vs_` 开头的字符串，约 46 字符 | **长期有效，不自动过期**；被重置则立即失效 |

关键事实：

- Token 是**服务端生成的固定字符串**，不是账号密码，**不能由 App 自己生成或计算**。
- Token 全库只有一枚，所有客户端共用。用户在你部署的服务面板里复制后，手动填进 App。
- Token 只能由服务端「重置」，重置后旧值**立即失效**（无过渡期）。
- 服务端可以**整体关闭**访问控制，此时 `enabled=false`，带不带 Token 都能调通。

### 免鉴权路径（白名单，永远不需要 Token）

```
/healthz                    # 存活探针，恒 200
/login                      # 登录页
/api/v1/auth/login
/api/v1/auth/state          # 可用于探测是否开启鉴权
/favicon.ico
/static/*
```

其余**全部**路径（含 `/`、`/docs`、`/source`、所有 `/api/v1/*`）都需要凭证。

- `/source`（源站清单 JSON）虽然不在 `/api/` 前缀下，但它是给客户端用的接口，**同样认 Token**。
- `/` 与 `/docs` 是浏览器页面：未登录且请求头接受 HTML 时返回 303 跳到 `/login`，否则返回 401 JSON。

---

## 2. 携带方式：服务端接受 3 种，App 只用第 1 种

按以下顺序解析，**取到第一个非空值就停止**（后面的忽略）：

| 顺序 | 位置 | 示例 |
|---|---|---|
| 1（推荐） | 请求头 `Authorization` | `Authorization: Bearer vs_xxxxxxxx` |
| 2（兼容） | 请求头 `X-API-Key` 或 `X-Token` | `X-API-Key: vs_xxxxxxxx` |
| 3（兜底） | 查询参数 `token` 或 `apikey` | `...?source=jkun&page=1&token=vs_xxxxxxxx` |

**写代码时注意这几点，都是真实踩过的坑：**

- `Bearer` 后面**至少一个空格**。`Bearervs_xxx`（完全没空格）会直接 401；多打一个空格服务端会自己 `strip()`，能过但不规范。
- HTTP 头名大小写不敏感，`authorization` 也可以；值里的 `Bearer` 前缀也大小写不敏感。
- **不要对 Token 做 URL 编码后再放进请求头**。Token 字符集是 `[A-Za-z0-9_-]`，本身不需要编码；编码后会变成 `%2D` 之类的形式导致校验失败。
- 放 query 参数时建议 `Uri.encode()`，虽然这个字符集安全，但养成习惯。
- 三种方式**同时存在**时只认第一种（Bearer），别混着传不同值。

### 一个反例（务必避免）

```kotlin
// ❌ 错：给所有请求都加头，Token 会泄漏给第三方图片/视频 CDN
val client = OkHttpClient.Builder()
    .addInterceptor { chain ->
        chain.proceed(chain.request().newBuilder()
            .addHeader("Authorization", "Bearer $token").build())
    }
    .build()
```

App 里往往混着两类请求：**自己服务器的 API** 和 **第三方源站的封面图 / m3u8 播放地址**。
Token 只能发给自己的服务器 —— 发给第三方等于把钥匙给了别人。正确做法见 4.1 的主机白名单。

---

## 3. 客户端改造 4 步（与语言无关）

1. **单点注入**：全 App 共用一个 HTTP 客户端（OkHttp / URLSession / Dio / fetch 封装），只在拦截器里加一次头。禁止在各处手工拼 header —— 漏一处就是一个 401。
2. **持久化配置**：`baseUrl` 与 `token` 存本地（见第 5 节的安全存储），启动时读取注入。
3. **401 统一出口**：拦截器里识别 401 → 标记「Token 无效」→ 抛一个专门的异常类型（如 `TokenInvalidException`）→ UI 层弹提示并跳设置页。**不要自动重试**，重试只会再吃一次 401。
4. **可恢复**：用户改完 Token 后能一键重放刚才失败的请求（保留请求参数或直接刷新当前页）。

### 首次启动引导（建议流程）

```
启动
 ├─ 本地无 baseUrl → 显示「输入服务器地址」页（例 http://192.168.1.10:8181）
 ├─ GET /api/v1/auth/state  （免鉴权，可安全调用）
 │    ├─ 请求失败        → 提示「无法连接服务器，检查地址/端口」
 │    ├─ enabled = false → 进入主界面（不需要 Token）
 │    └─ enabled = true
 │         ├─ 本地有 token → 直接进主界面（真失败了再靠 401 兜住）
 │         └─ 本地无 token → 显示「输入 API Token」页 + 填写指引
```

`GET /api/v1/auth/state` 返回示例（**这是唯一能判断"要不要 Token"的接口**）：

```json
{
  "enabled": true,
  "logged_in": false,
  "has_token": true,
  "token_hint": "vs_HYgS…u51c",
  "password_changed_at": "2026-10-31T16:02:11",
  "token_created_at": "2026-10-31T16:19:14"
}
```

> ⚠️ 不要用 `/healthz` 判断鉴权状态 —— 它为了容器健康检查**永远返回 200**。

**给用户的填写指引文案建议**（可直接用）：

> 打开 `http://<服务器IP>:8181` → 用管理密码登录 → 右上角「⚙ 设置」→ 复制「API Token」→ 粘贴到上方输入框。

---

## 4. 分平台代码

### 4.1 Android（Kotlin + OkHttp / Retrofit）

```kotlin
/** Token 提供者：从本地安全存储读，用户改了立即生效 */
object TokenStore {
    @Volatile var baseUrl: String = ""
    @Volatile var token: String = ""
    /** 只有指向自己服务器的请求才带 Token */
    fun isOwnHost(host: String): Boolean =
        baseUrl.isNotBlank() && host.equals(java.net.URI(baseUrl).host, ignoreCase = true)
}

class AuthInterceptor : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val req = chain.request()
        val token = TokenStore.token
        // 关键：第三方 CDN（封面图、m3u8）不加头，避免 Token 外泄
        if (token.isBlank() || !TokenStore.isOwnHost(req.url.host)) {
            return chain.proceed(req)
        }
        return chain.proceed(
            req.newBuilder()
                .header("Authorization", "Bearer $token")   // header() 覆盖，不会重复
                .build()
        )
    }
}

/** 401 统一识别成业务异常，交给 UI 处理 */
class TokenInvalidException(val detail: String) : IOException(detail)

class ErrorInterceptor : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val res = chain.proceed(chain.request())
        if (res.code == 401) {
            val detail = runCatching {
                org.json.JSONObject(res.peekBody(2048).string()).optString("detail")
            }.getOrDefault("")
            res.close()
            throw TokenInvalidException(detail.ifBlank { "API Token 无效或未填写" })
        }
        return res
    }
}

// 组装（顺序：Auth 在内层，保证重试/重定向时也带上）
val client = OkHttpClient.Builder()
    .addInterceptor(AuthInterceptor())
    .addInterceptor(ErrorInterceptor())
    .connectTimeout(10, TimeUnit.SECONDS)
    .readTimeout(20, TimeUnit.SECONDS)
    .build()

// Retrofit：拦截器统一加头，接口声明里不用写 @Header
val api = Retrofit.Builder()
    .baseUrl(TokenStore.baseUrl.ensureTrailingSlash())   // 必须以 / 结尾
    .client(client)
    .addConverterFactory(GsonConverterFactory.create())
    .build()
    .create(VodApi::class.java)
```

> 用 Ktor Client 的话同理，装一个 `HttpSend` 插件在 `headers { append("Authorization", "Bearer $token") }`，条件同上。

### 4.2 iOS（Swift + URLSession）

```swift
enum ApiError: Error { case tokenInvalid(String), server(String), network(Error) }

struct ApiClient {
    var baseURL: URL
    var token: String

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        var comp = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comp.queryItems = query }
        var req = URLRequest(url: comp.url!)
        req.timeoutInterval = 20
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw ApiError.network(URLError(.badServerResponse)) }
        switch http.statusCode {
        case 200...299:
            return try JSONDecoder().decode(T.self, from: data)
        case 401:
            struct ErrBody: Decodable { let detail: String? }
            let detail = (try? JSONDecoder().decode(ErrBody.self, from: data))?.detail
            throw ApiError.tokenInvalid(detail ?? "API Token 无效或未填写")
        default:
            throw ApiError.server("HTTP \(http.statusCode)")
        }
    }
}
```

### 4.3 Flutter（Dio）

```dart
class TokenInvalidException implements Exception {
  final String detail;
  TokenInvalidException(this.detail);
  @override
  String toString() => detail;
}

final dio = Dio(BaseOptions(
  baseUrl: baseUrl,
  connectTimeout: const Duration(seconds: 10),
  receiveTimeout: const Duration(seconds: 20),
));

dio.interceptors.add(InterceptorsWrapper(
  onRequest: (options, handler) {
    final token = tokenBox.get('token', defaultValue: '');
    // 只给自己的服务器加头
    if (token.isNotEmpty && options.uri.host == Uri.parse(baseUrl).host) {
      options.headers['Authorization'] = 'Bearer $token';
    }
    handler.next(options);
  },
  onError: (e, handler) {
    if (e.response?.statusCode == 401) {
      tokenBox.put('token_valid', false);                  // 标记失效，UI 据此提示
      return handler.reject(DioException(
        requestOptions: e.requestOptions,
        error: TokenInvalidException(
            (e.response?.data is Map) ? (e.response!.data['detail'] ?? '') : ''),
        response: e.response,
        type: DioExceptionType.badResponse,
      ));
    }
    handler.next(e);
  },
));
```

### 4.4 React Native / H5（fetch）

```ts
let TOKEN = "";                       // 从安全存储/设置页读入
const BASE = "http://192.168.1.10:8181";

export class TokenInvalidError extends Error {}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;

  const res = await fetch(BASE + path, { ...init, headers });
  if (res.status === 401) {
    const body = await res.json().catch(() => ({}));
    throw new TokenInvalidError(body.detail || "API Token 无效或未填写");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}
```

> H5 跨域说明：服务端已放开 `Access-Control-Allow-Headers: *`，浏览器可以发 `Authorization` 头。
> 但**没有开启 `allow_credentials`**，所以跨域场景下 cookie 会话用不了 —— H5 必须走 Token，这正是它存在的意义。

### 4.5 WebView / 播放器 / `<img>`：加不了请求头怎么办

有些场景根本没法自定义 header：

- HTML5 `<video>` / `hls.js` 拉 m3u8 切片
- WebView 里加载的页面
- `<img src>` 封面图
- 系统播放器（ExoPlayer 的 `DefaultHttpDataSource` 之外、AVPlayer 某些用法）

这时用 **query 参数**兜底：

```
http://<host>:8181/api/v1/<slug>?ac=detail&ids=123&token=vs_xxxxxxxx
```

但**先确认这个 URL 到底是不是你服务器的**：如果 m3u8/mp4 直链指向源站 CDN（绝大多数情况如此），
那它**根本不需要 Token**，加上了反而泄漏。只有「播放地址由本服务代理转发」时才需要带。

---

## 5. Token 存哪里（安全底线）

| 平台 | 推荐方案 | 备注 |
|---|---|---|
| Android | `EncryptedSharedPreferences`（Jetpack Security）或 Keystore 封装 | 至少不要明文放 `SharedPreferences` |
| iOS | Keychain（`kSecClassGenericPassword`）+ `kSecAttrAccessibleAfterFirstUnlock` | 不要用 `UserDefaults` |
| Flutter | `flutter_secure_storage` | 内部走 Keystore/Keychain |
| React Native | `react-native-keychain` | 同上 |
| H5 / Web | 内存变量 + 可选 `localStorage` | localStorage 会被 XSS 读取，能接受再用 |

**三条红线：**

1. ❌ **不要把 Token 硬编码进 App 或提交进 Git 仓库**。它会被反编译提取，且一旦重置，所有装旧包的设备同时失效。
2. ❌ 不要在日志/崩溃上报里打印完整 Token。真要打日志就脱敏：`vs_HYgS…u51c`。
3. ❌ 不要用「管理密码」代替 Token 调业务接口 —— 密码只对 `/api/v1/auth/login` 有效，调 `/api/v1/videos` 依然是 401。

---

## 6. 错误处理矩阵

| HTTP | 响应体 | 含义 | 客户端该做什么 |
|---|---|---|---|
| 200 | `{"code":1,...}` | 正常 | — |
| **401** | `{"detail":"未授权：请求头需携带 Authorization: Bearer <token>（也支持 X-API-Key 头或 ?token= 参数）；浏览器需先登录管理面板","code":401}` | Token 缺失 / 错误 / 已被重置 | **不重试**。标记 Token 失效 → 提示「API Token 无效，请到设置里重新填写」→ 提供跳设置页 + 重试按钮 |
| **401** | `{"detail":"请先登录管理面板"}` | 访问了非 `/api/` 路径（如 `/docs`） | App 不会遇到；遇到说明 URL 拼错了 |
| 404 | `{"detail":"源站 xxx 不存在"}` / `{"detail":"视频不存在"}` | source slug 或 vod_id 不存在、或该站还没同步过数据 | 展示空态，不要重试 |
| **429** | `{"detail":"尝试次数过多，请 N 秒后再试"}` + `Retry-After` | 只出现在反复输错**管理密码**时 | App 不涉及；若出现说明误用了登录接口 |
| **503** | `{"detail":"服务繁忙：数据库连接池已满...","code":0}` + `Retry-After: 5` | 服务端在全量同步/过载 | 按 `Retry-After` 退避，最多重试 2~3 次，中间显示「服务繁忙，稍后重试」 |
| 5xx 其他 | — | 服务端异常 | 上报日志 + 友好提示，不要无限重试 |

**重试策略速记**：401 / 404 绝不重试；503 指数退避；网络超时重试 1 次即可。

---

## 7. Token 被重置后的正确行为

服务端有两种情况会换 Token：

1. 用户在面板点「重新生成」；
2. 用户关闭访问控制后重新开启（**出于安全会自动换一枚**，因为关闭期间旧 Token 可能已泄露）。

对 App 来说这只是「下次请求收到 401」，**无法提前感知**。所以：

| 做法 | 评价 |
|---|---|
| 收到 401 → 弹出「Token 已失效，请重新填写」+ 预填旧值方便对比 | ✅ 推荐 |
| 收到 401 → 静默清空本地 Token 并踢回引导页 | ⚠️ 可接受，但用户不知道发生了什么 |
| 收到 401 → 自动重试 3 次 | ❌ 无意义，只会刷 3 次 401 |
| 收到 401 → 回退到内置默认 Token | ❌ 严重错误，等于没有鉴权 |

---

## 8. 服务端关掉鉴权时（`enabled=false`）

- 所有接口不带 Token 也能调通，带错 Token 也**不会**报 401（服务端直接放行）。
- App 不需要为此改逻辑：**始终带 Token** 即可，开关状态由服务端决定。
- 唯一需要处理的是「首次引导」：如果 `auth/state` 返回 `enabled=false`，就不该强制用户填 Token。

---

## 9. 分页约定：三列宫格每页 30 条

服务端分页参数统一为 `page`（从 1 开始）+ `page_size`（默认值由 `API_PAGE_SIZE_DEFAULT` 决定，默认 **30**；上限 `API_PAGE_SIZE_MAX`，默认 **100**）。

**三列宫格推荐 `page_size=30`**（10 行 × 3 列），一屏 4~5 行时首次加载恰好覆盖首屏且有余量，滚动到底再取下一页时列表不会"跳半行"。

```http
GET /api/v1/videos?source=jkun&page=1&page_size=30
GET /api/v1/search?wd=三体&page=1&page_size=30
GET /api/v1/jkun?ac=list&pg=1&page_size=30     ← MacCMS 兼容接口用 pg 表示页码
```

### 响应里的分页字段

```json
{
  "code": 1,
  "page": 1,
  "pagecount": 12,
  "total": 356,
  "list": [ ... 30 条 ... ]
}
```

| 字段 | 用途 |
|---|---|
| `total` | 总条数（可用于显示"共 356 部"） |
| `pagecount` | 总页数 = `ceil(total / page_size)` |
| `page` | 当前页（回显请求值） |
| `list` | 当前页数据，**最后一页可能不足 30 条** |

### 三个必须注意的点

1. **判断"到底了"要用 `page >= pagecount` 或 `len(list) < page_size`**，不要用 `list.length == 0` 当唯一条件（第 3 页返回 5 条时，它既不是 0 也不是整页）。
2. **`page_size` 超过上限会返回 422，不是自动截断**。写死 30 就不会碰到；如果要动态翻倍加载（30→60），记得自己夹一下 `min(n, 100)`。
3. **`page_size` 必须是正整数**，`0`、负数、小数都会 422。切换"每页条数"后要把 `page` 重置回 1，否则会跳过前面几页。

### 三列布局的取数策略（建议）

```text
首次进入：page=1, page_size=30  → 渲染 10 行
滚动到第 8 行可见时：预取 page=2 → 追加（不要等触底才请求，3 列图片墙触底再加载会有明显空窗）
page >= pagecount  → 停止请求，显示"没有更多了"
下拉刷新：重置 page=1、清空本地列表（保留已加载的 total 用于占位骨架）
切换分类/源站：重置 page=1 并清空列表，防止不同源的条目混在一起
```

> 每组 3 个、每页 30 条意味着**每页正好 10 整行**，不会出现最后一行只有 1~2 个格子的空白。
> 若改成每页 20 条（默认旧值），10 行布局下会出现"最后一页 2 格 + 空白"，观感会差。

## 10. 给 AI 的施工提示词（复制这段即可）

```text
任务：给现有 App 加上 API Token 鉴权支持，服务端版本 v1.5.0。

背景：服务端默认启用访问控制，除 /healthz 和 /api/v1/auth/* 外所有接口都要求
Authorization: Bearer <token>。接口路径、参数、响应结构均不变。

请完成以下改动：
1. 新增本地配置：baseUrl（服务器地址）与 apiToken（API Token），用平台的安全存储
   （Android EncryptedSharedPreferences / iOS Keychain / Flutter flutter_secure_storage /
   RN react-native-keychain）持久化，启动时读取。
2. 在统一的 HTTP 客户端拦截器里注入请求头 Authorization: Bearer <token>，
   且仅当请求 host 等于 baseUrl 的 host 时才注入（避免 Token 泄漏给第三方 CDN）。
3. 401 统一处理：不重试，解析响应体里的 detail，抛出专用异常
   TokenInvalidException，UI 层弹提示"API Token 无效，请到设置页重新填写"，
   并提供"去设置"和"重试"两个按钮。不要静默清空本地 Token。
4. 设置页增加两个输入项（服务器地址、API Token），提供"测试连接"按钮：
   调 GET /api/v1/auth/state（该接口免鉴权），
   - 请求失败 → 提示地址/端口错误
   - enabled=false → 提示"该服务器未开启鉴权，可不填 Token"
   - enabled=true 且填了 Token → 再调一次需要鉴权的接口（如 /api/v1/stats），
     200 则提示"连接成功"，401 则提示"Token 无效"
5. 首次启动引导：无 baseUrl 时先要求填地址；填完调 /api/v1/auth/state，
   若 enabled=true 且本地无 Token，则进入 Token 填写页，并展示引导文案：
   "打开 http://<服务器IP>:8181 → 用管理密码登录 → 右上角「⚙ 设置」→ 复制 API Token"
6. 日志脱敏：任何日志中 Token 只显示前 7 位和后 4 位，形如 vs_HYgS…u51c。
7. 不要实现 /api/v1/auth/login 相关功能，App 不需要管理密码。
8. 列表分页统一 page_size=30（三列 × 10 行），页码从 1 开始；
   用 page >= pagecount 判断结束，最后一页不足 30 条要正常渲染。

验收标准：
- 未填 Token 时调业务接口返回 401，界面显示"请填写 API Token"而不是"网络错误"。
- 填错 Token 时同样提示 Token 无效，可回到设置页修改。
- 填对 Token 后所有现有功能正常，响应解析代码零改动。
- 抓包确认：请求第三方图片/视频 CDN 时不带 Authorization 头。
```

---

## 11. 自测清单

### 命令行（先确认服务端行为，再改 App）

```bash
# ① 无 Token → 应 401
curl -i -s "http://<host>:8181/api/v1/stats" | head -3

# ② 带 Token → 应 200
curl -s -H "Authorization: Bearer vs_xxxx" "http://<host>:8181/api/v1/stats"

# ③ 兼容写法也要通
curl -s -H "X-API-Key: vs_xxxx" "http://<host>:8181/api/v1/stats"
curl -s "http://<host>:8181/api/v1/stats?token=vs_xxxx"

# ④ 错误 Token → 应 401 且 detail 里说明怎么带
curl -s -H "Authorization: Bearer wrong" "http://<host>:8181/api/v1/stats"

# ⑤ 探测开关状态（免鉴权）
curl -s "http://<host>:8181/api/v1/auth/state"
```

### App 内 6 个验证点

1. 首次安装 → 引导页要求填地址 → 填错端口时提示「无法连接」而不是崩溃。
2. 不填 Token 调列表 → 出现「请填写 API Token」，**不是**「网络错误」/「加载失败」。
3. 填错 Token → 同样提示 Token 无效，且能一键回到设置页。
4. 填对 Token → 列表、详情、搜索、分类全部正常，分页连续加载无 401。
5. 服务端重置 Token → App 下拉刷新收到 401 → 提示重新填写 → 填入新 Token → 恢复正常。
6. 抓包（Charles/Proxyman）确认：请求自己服务器带 `Authorization`，请求第三方图片域名**不带**。

---

## 12. 不要做的 6 件事

1. ❌ 不要在每个 API 调用处手写 `@Header("Authorization")` —— 100 个接口漏 1 个就是线上事故，用拦截器。
2. ❌ 不要把 Token 写死进代码或塞进 `BuildConfig` 后提交仓库。
3. ❌ 不要用 `/healthz` 判断鉴权状态（它恒 200）。
4. ❌ 不要接 `/api/v1/auth/login`（账号密码是浏览器面板的，不是 App 的）。
5. ❌ 不要在 401 时自动重试或自动清空 Token。
6. ❌ 不要把 Token 加给第三方域名的请求（封面图、m3u8 直链）。

---

## 13. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/API_SPEC_FOR_AI.md` | 完整接口规格（29 个接口逐一说明，含鉴权章节 2.1） |
| `docs/CLIENT_API.md` | 面向客户端开发的接口速查 |
| `docs/openapi.json` | 机器可读的 OpenAPI 3.0 描述（可直接生成客户端 SDK） |
| `README.md` → 「访问控制（v1.5.0）」 | 服务端侧：改密码、重置 Token、忘记密码怎么恢复 |
