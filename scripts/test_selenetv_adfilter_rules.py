"""
SeleneTV /api/adfilter/rules —— 新端点契约 + 零影响验证。

覆盖：
  A. 未登录 → 401（Helios 纯文本）。
  B. 登录后首次拉取（version=0）→ changed=true + rules。
  C. 持同版本再拉 → changed=false，不带 rules。
  D. 带更高版本（回滚场景）→ changed=false。
  E. 无 cookie 直打 /api/v1/adfilter/rules → 401（原闸门行为不变）。
  F. SELENETV_API_ENABLED=false 时 /api/adfilter/rules 回到 vodsync 闸门（未带 token 401）。

运行：.venv_selene/Scripts/python.exe scripts/test_selenetv_adfilter_rules.py
"""
import os
import sys
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="vodsync-selenetv-adfilter-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DATA_DIR, "af.db").replace("\\", "/")
os.environ["AUTH_ENABLED"] = "true"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"
os.environ["TAG_INDEX_ON_STARTUP"] = "false"
os.environ["SELENETV_API_ENABLED"] = "true"
os.environ["SELENETV_LIVE_SYNC_ENABLED"] = "false"
os.environ["ADMIN_PASSWORD"] = "panel_pwd_123"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import database
from app.selenetv.migrate import ensure_tables
from app.selenetv import users as users_repo
from app.main import app

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


ensure_tables(database.engine)
# TestClient 不触发 lifespan，这里显式初始化 vodsync 库与规则包（生产由 startup 完成）
database.init_db()
from app import adfilter as _adfilter
_adfilter.ensure_seed()
# 建一个 SeleneTV 用户供登录
try:
    users_repo.create("af_tester", "af_pwd_123", allow_adult=1)
    print("user created")
except Exception as e:
    print("create_user:", e)

client = TestClient(app)

# --- A. 未登录 401 ---
r = client.get("/api/adfilter/rules")
check("未登录 /api/adfilter/rules 401", r.status_code == 401, f"got {r.status_code}")
check("401 是 Helios 纯文本", "text/plain" in r.headers.get("content-type", ""), r.headers.get("content-type"))

# --- 登录拿 cookie ---
r = client.post("/api/login", json={"username": "af_tester", "password": "af_pwd_123"})
check("登录成功", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
cookie = r.cookies.get("auth") or ""
check("拿到 auth cookie", bool(cookie))

# --- B. 首次拉取 ---
r = client.get("/api/adfilter/rules", cookies={"auth": cookie})
check("首次拉取 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
body = r.json()
check("changed=true", body.get("changed") is True, str(body)[:120])
check("带 rules 对象", isinstance(body.get("rules"), dict) and len(body["rules"]) > 0)
check("version>=1", isinstance(body.get("version"), int) and body["version"] >= 1)
ver = body.get("version", 0)
check("no-store 头", r.headers.get("cache-control") == "no-store", r.headers.get("cache-control"))

# --- C. 同版本再拉 ---
r = client.get(f"/api/adfilter/rules?version={ver}", cookies={"auth": cookie})
check("同版本 changed=false", r.json().get("changed") is False, r.text[:120])
check("同版本不带 rules", "rules" not in r.json())

# --- D. 更高版本（回滚场景） ---
r = client.get(f"/api/adfilter/rules?version={ver + 5}", cookies={"auth": cookie})
check("更高版本 changed=false", r.json().get("changed") is False, r.text[:120])

# --- E. 原端点行为不变 ---
r = client.get("/api/v1/adfilter/rules")
check("原 /api/v1 端点无 token 仍 401", r.status_code == 401, f"got {r.status_code}")

# --- F. SELENETV_API_ENABLED=false 回到原闸门 ---
import importlib
os.environ["SELENETV_API_ENABLED"] = "false"
r = client.get("/api/adfilter/rules", cookies={"auth": cookie})
check("开关关闭时回到 vodsync 闸门(401)", r.status_code == 401, f"got {r.status_code}")
os.environ["SELENETV_API_ENABLED"] = "true"

print(f"\n===== {passed} passed, {failed} failed =====")
sys.exit(1 if failed else 0)
