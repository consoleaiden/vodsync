"""
v1.5.0 访问控制测试（密码登录 + API Token + 开关）。

覆盖项：
 1. 首次初始化：默认密码 666666 可登录、Token 自动生成、默认启用
 2. 未带凭证访问 API → 401；Bearer / X-API-Key / ?token= 三种带法都放行
 3. 面板：未登录访问 / 跳登录页；登录后可访问；/login 未登录返回页面
 4. 密码错误计数与锁定（429 + Retry-After）
 5. 修改密码：旧密码失效、旧会话（cookie）立即失效、新会话可用
 6. 关闭鉴权 → 无凭证可访问；重新开启 → 自动更换 Token，旧 Token 失效
 7. Token 重置：旧 Token 立即失效
 8. /healthz 永远免鉴权
 9. 登录跳转只允许站内路径（防开放重定向）

运行：AUTH_ENABLED=true python scripts/test_v150_auth.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-auth-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "auth.db")
os.environ["AUTH_ENABLED"] = "true"
os.environ["LOGIN_MAX_FAILS"] = "3"
os.environ["LOGIN_LOCK_MINUTES"] = "1"

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from fastapi.testclient import TestClient

    from app import auth, database
    from app.main import app

    database.init_db()
    auth.ensure_init()

    client = TestClient(app)
    client.headers.update({"Accept": "application/json"})

    print("[1] 首次初始化")
    check("默认密码 666666 生效", auth.verify_password("666666"))
    check("默认启用鉴权", auth.is_enabled())
    token = auth.get_token()
    check("已自动生成 Token", token.startswith("vs_") and len(token) > 20, token[:8])

    print("[2] API 鉴权")
    r = client.get("/api/v1/stats")
    check("无凭证 401", r.status_code == 401, r.status_code)
    check("401 带 WWW-Authenticate", "bearer" in r.headers.get("www-authenticate", "").lower())
    check("无凭证带 Bearer", client.get("/api/v1/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200)
    check("X-API-Key 头", client.get("/api/v1/stats", headers={"X-API-Key": token}).status_code == 200)
    check("?token= 参数", client.get(f"/api/v1/stats?token={token}").status_code == 200)
    check("错误 Token 401", client.get("/api/v1/stats", headers={"Authorization": "Bearer vs_wrong"}).status_code == 401)
    check("/healthz 免鉴权", client.get("/healthz").status_code == 200)

    print("[3] 面板登录")
    r = client.get("/", follow_redirects=False, headers={"Accept": "text/html"})
    check("未登录访问面板 303 跳登录", r.status_code == 303 and "/login" in r.headers.get("location", ""), r.status_code)
    check("非浏览器请求面板 401",
          client.get("/", follow_redirects=False, headers={"Accept": "application/json"}).status_code == 401)
    r = client.get("/login")
    check("/login 返回登录页", r.status_code == 200 and "管理密码" in r.text, r.status_code)

    r = client.post("/api/v1/auth/login", json={"password": "bad-pwd"})
    check("错误密码 401", r.status_code == 401, r.status_code)
    r = client.post("/api/v1/auth/login", json={"password": "666666"})
    check("默认密码登录成功", r.status_code == 200 and r.json().get("ok"), r.text[:120])
    check("登录后种下 HttpOnly cookie", "vodsync_session" in client.cookies,
          str(client.cookies.keys()))
    r = client.get("/", follow_redirects=False)
    check("登录后面板 200", r.status_code == 200, r.status_code)
    r = client.get("/api/v1/stats")
    check("cookie 也能调 API", r.status_code == 200, r.status_code)
    st = client.get("/api/v1/auth/state").json()
    check("state: enabled + logged_in", st["enabled"] and st["logged_in"], str(st))

    print("[4] 失败锁定")
    for _ in range(3):
        client.post("/api/v1/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": "9.9.9.9"})
    r = client.post("/api/v1/auth/login", json={"password": "666666"}, headers={"X-Forwarded-For": "9.9.9.9"})
    check("连续失败后 429", r.status_code == 429, r.status_code)
    check("429 带 Retry-After", "retry-after" in {k.lower() for k in r.headers}, str(dict(r.headers)))
    auth.reset_login_fail("9.9.9.9")

    print("[5] 修改密码")
    old_cookie = client.cookies.get("vodsync_session")
    r = client.post("/api/v1/auth/password", json={"old": "wrong", "new": "abcdef"})
    check("旧密码不对 400", r.status_code == 400, r.status_code)
    r = client.post("/api/v1/auth/password", json={"old": "666666", "new": "123"})
    check("新密码太短 400", r.status_code == 400, r.status_code)
    r = client.post("/api/v1/auth/password", json={"old": "666666", "new": "newpass123"})
    check("改密码成功", r.status_code == 200, r.text[:120])
    check("旧密码登录失败", client.post("/api/v1/auth/login", json={"password": "666666"}).status_code == 401)
    check("新密码登录成功", client.post("/api/v1/auth/login", json={"password": "newpass123"}).status_code == 200)

    stale = TestClient(app)
    stale.cookies.set(auth.COOKIE, old_cookie)
    check("改密码后旧会话失效", stale.get("/api/v1/stats").status_code == 401,
          stale.get("/api/v1/stats").status_code)

    print("[6] 开关")
    r = client.post("/api/v1/auth/settings", json={"enabled": False})
    check("关闭鉴权", r.status_code == 200 and r.json()["enabled"] is False, r.text[:120])
    check("关闭后无凭证可访问", TestClient(app).get("/api/v1/stats").status_code == 200)
    check("关闭后面板可直接打开", TestClient(app, follow_redirects=False).get("/").status_code == 200)

    old_token = auth.get_token()
    r = client.post("/api/v1/auth/settings", json={"enabled": True})
    body = r.json()
    check("重新启用", body["enabled"] is True)
    check("启用时自动更换 Token", body.get("token") and body["token"] != old_token)
    new_token = body["token"]
    check("旧 Token 已失效",
          TestClient(app).get("/api/v1/stats", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401)
    check("新 Token 可用",
          TestClient(app).get("/api/v1/stats", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200)

    print("[7] Token 重置")
    r = client.post("/api/v1/auth/token")
    rotated = r.json()["token"]
    check("重置返回新 Token", rotated and rotated != new_token, rotated[:8])
    check("重置后旧 Token 失效",
          TestClient(app).get("/api/v1/stats", headers={"Authorization": f"Bearer {new_token}"}).status_code == 401)
    check("GET /auth/token 可读回", client.get("/api/v1/auth/token").json()["token"] == rotated)

    print("[8] 登录跳转安全")
    r = client.get("/login?next=//evil.example.com", follow_redirects=False)
    check("开放重定向被拦（登录态下回 /）",
          r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location"))
    r = client.get("/login?next=/docs", follow_redirects=False)
    check("站内 next 正常跳转", r.status_code == 303 and r.headers["location"] == "/docs", r.headers.get("location"))

    print("[9] 兼容旧 API_KEY")
    from app import config

    config.API_KEY = "legacy-key-123"
    check("API_KEY 环境变量仍可当 Token",
          TestClient(app).get("/api/v1/stats", headers={"X-API-Key": "legacy-key-123"}).status_code == 200)
    config.API_KEY = ""

    print()
    print(f"结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
