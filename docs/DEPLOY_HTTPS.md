# HTTPS 双栈上线指引（v1.14.0 / B-09）

> 目标：Token 不再经明文 HTTP 传输；**8181 明文端口继续并行**至少一个版本周期，
> 客户端切默认地址失败可立刻回退，不需要动服务端。
>
> 配套样例：[`deploy/nginx-vodsync.conf`](../deploy/nginx-vodsync.conf)、
> [`deploy/Caddyfile`](../deploy/Caddyfile)

---

## 1. 为什么不能用自签证书

Android 7+ **不信任用户自签 CA**。自签证书不会弹「证书有风险」，而是**直接握手失败**，
客户端只会报「连接失败 / 网络错误」，和「服务没起」长得一模一样，排查成本极高。

因此证书必须是公共 CA 签发（Let's Encrypt 免费且够用）。

## 2. 上线顺序（每步都可单独回退）

| 步 | 动作 | 回滚 |
|---|---|---|
| 1 | 域名解析 + 证书签发（certbot / Caddy 自动） | 关掉 443 监听 |
| 2 | 反代 443 → `vodsync:8000`，**保留 8181 明文映射** | 同上；明文端口一直在 |
| 3 | 验证（见第 4 节），确认头透传、超时、gzip | — |
| 4 | 客户端把 `DEFAULT_BASE_URL` 改成 `https://...`，删掉明文白名单 | 改回 http 地址 |
| 5 | 观察 ≥1 个版本周期无异常后，再考虑关闭 8181 | 重新映射端口 |
| 6 | 最后才打开 HSTS（配置里默认注释掉） | 注释回去（已下发的 max-age 到期前浏览器仍强制 https） |

> HSTS 放最后：一旦下发，浏览器在 `max-age` 内强制 https，**面板**在浏览器里就再也
> 打不开明文端口了（App 走 OkHttp 不受 HSTS 影响）。确认前端默认地址全切完再开。

## 3. 必须检查的 5 个点

1. **头透传**：`Authorization`、`X-API-Key`、`X-Client-Version` 都要到后端。
   漏掉 `Authorization` 的表现是全站 401，而不是 403。
2. **XFF 覆盖而非追加**：反代要用 `$remote_addr` **覆盖** `X-Forwarded-For`。
   客户端自带的 XFF 若被信任，登录失败锁定形同虚设。后端要配合 `TRUST_PROXY=true` 才采信。
3. **超时**：`proxy_read_timeout ≥ 30s`（样例给 60s）。700+ 集的详情响应在慢网络下会超。
4. **gzip**：详情里 `play_lines` 与 `vod_play_list` 是同值双发（兼容两版客户端），
   不开压缩等于白付一份带宽。样例已开。
5. **TLS 版本**：只留 1.2/1.3。留着 1.0/1.1 只会拖低安全评级，实际没有客户端需要。

## 4. 验收命令

```bash
# 1) HTTPS 可用
curl -sI https://vodsync.example.com/healthz | head -1
# 期望：HTTP/2 200

# 2) 证书链被系统信任（Verify return code: 0 (ok)）
openssl s_client -connect vodsync.example.com:443 \
  -servername vodsync.example.com </dev/null 2>/dev/null | grep 'Verify return code'
# 期望：Verify return code: 0 (ok)

# 3) 明文端口并行期仍可用
curl -s http://vodsync.example.com:8181/healthz -o /dev/null -w '%{http_code}\n'
# 期望：200

# 4) 能力清单能读到（客户端连接测试也打这个接口）
curl -s https://vodsync.example.com/healthz | python -c "import json,sys;d=json.load(sys.stdin);print(d['version'],d['capabilities'])"

# 5) 鉴权头确实透传（带 Token 不应 401）
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" https://vodsync.example.com/api/v1/sources
# 期望：200

# 6) 大响应不超时（挑一部集数多的影片）
time curl -s -H "Authorization: Bearer $TOKEN" \
  "https://vodsync.example.com/api/v1/videos/ffzy/123456" -o /dev/null
```

## 5. 群晖（Container Manager）补充

群晖自带「反向代理」图形界面，等价配置要点：

- 来源 `https` / 主机名 `vodsync.example.com` / 端口 `443` → 目的地 `http` / `127.0.0.1` / `8181`
- 在「自定义标题」里加 `X-Forwarded-For: $remote_addr`（群晖默认是追加，需显式覆盖）
- 证书用「安全性 → 证书 → 新增 → Let's Encrypt」，DSM 会自动续期
- **不要**在 DSM 里同时把 8181 的端口映射删掉 —— 并行期还要用

## 6. 回退

任何一步出问题：

1. 关掉 443 监听（或把证书换回旧配置）；
2. 客户端把默认地址改回 `http://<host>:8181`；
3. 明文端口如果没有被删，**不需要重启服务端**即可恢复。

服务端本身没有任何「只有 HTTPS 才能工作」的改动，`/healthz`、Token 校验、限流在两种
协议下行为完全一致。

## 7. 排障：502 upstream connect failed（os error 11004）

**典型现场**：公网入口全线 502，反代日志报 `connect() failed (os error 11004)`；
直连 `http://<host>:8181` 却一切正常。

**根因**：反代的 `proxy_pass`/`upstream` 写成了**公网域名**（如 `mosang.asia`），
而该域名**只有 AAAA（IPv6）记录、没有 A 记录**。Windows 下的 Nginx 解析 upstream
域名时只拿 A 记录，拿不到就报 11004（"the requested name is valid, but no data of
the requested type was found"）；即使解析成功，反代再绕公网回自己也是浪费。

**修复**：

1. **upstream 一律写本地地址，不写公网域名**：

   ```nginx
   upstream vodsync_backend {
       server 127.0.0.1:8181;    # 反代与后端同机时（本项目样例即如此）
       keepalive 16;
   }
   ```

   反代跑在容器里时写容器名（`vodsync:8000`）或宿主机内网 IP。
   改完 `nginx -t && nginx -s reload`，`curl -sI https://<域名>/healthz` 期望 200。

2. **给域名补一条 A 记录**（DNS 服务商控制台操作）：类型 `A`、主机记录 `@`、
   值为服务器的公网 IPv4。只补 AAAA 的话，纯 IPv4 网络（大量手机蜂窝网络）
   根本解析不到，表现为「部分手机连不上，Wi-Fi 正常」。
   验证：`nslookup -type=A mosang.asia` 应返回 IPv4 地址。

3. **过渡期让 App 直连 IP**：A 记录生效前，客户端默认地址改用
   `http://<服务器IP>:8181`（本项目现网过渡地址：`http://100.84.176.119:8181`）。
   直连 8181 走的是明文 HTTP + Token，双栈并行期本来就是设计内的可用路径，
   HTTPS 修好后切回域名即可。
