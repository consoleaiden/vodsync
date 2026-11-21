"""
SeleneTV 用户管理 + 18+ 权限 —— CRUD/鉴权/过滤契约 + 零影响验证。

覆盖：
  A. 建表幂等：selenetv_users 只增表；重复 ensure 不报错。
  B. CRUD：增/查/改/删；重复用户名 409；停用用户无法登录；不返回 password_hash。
  C. 兼容回退：表外用户名仍可用面板密码登录（现有 mumu cookie 不失效）。
  D. 鉴权接入用户表：建用户后按用户密码登录，错误密码失败。
  E. 18+ 权限真正生效：
     - allow_adult=0 用户：/api/search/resources 不含 18+ 源、detail 访问 18+ 源 404；
     - allow_adult=1 用户 / 兼容 owner：可见全部源。
  F. 零影响：vodsync 现有表不被新增/删除。

运行：.venv_selene/Scripts/python.exe scripts/test_selenetv_users.py
"""
import os
import sys
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="vodsync-selenetv-users-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DATA_DIR, "users.db").replace("\\", "/")
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
from sqlalchemy import text

from app import auth, database
from app.selenetv.migrate import ensure_tables
from app.selenetv import users as users_repo
from app.selenetv import repository as repo
from app.selenetv import security
from app.main import app

PANEL_PWD = "panel_pwd_123"
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def section(title):
    print(f"\n--- {title} ---")


database.init_db()
auth.set_password(PANEL_PWD)
ensure_tables(verbose=False)
ensure_tables(verbose=False)  # 幂等

anon = TestClient(app)
admin = TestClient(app)
# 面板登录拿 session（admin API 走 vodsync 会话闸门）
r = admin.post("/api/v1/auth/login", json={"username": "admin", "password": PANEL_PWD})
check("面板登录 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

section("A. 建表幂等")
check("selenetv_users 表存在", "selenetv_users" in set(database.list_tables()))
before = set(database.list_tables())
ensure_tables(verbose=False)
check("二次建表不改变表集合", set(database.list_tables()) == before)

section("B. CRUD")
r = admin.post("/api/v1/admin/selenetv/users",
               json={"username": "kid", "password": "kid_pwd", "allow_adult": False})
check("新增用户 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
kid_id = r.json().get("detail", {}).get("id")
check("返回不含 password_hash", "password_hash" not in r.text)
r2 = admin.post("/api/v1/admin/selenetv/users",
                json={"username": "kid", "password": "x"})
check("重复用户名 409", r2.status_code == 409, f"{r2.status_code}")

r = admin.get("/api/v1/admin/selenetv/users")
check("列表 200", r.status_code == 200)
lst = r.json().get("list", [])
check("列表含 kid", any(u["username"] == "kid" for u in lst))
check("列表不含 password_hash", "password_hash" not in r.text)

# kid allow_adult True
r = admin.put(f"/api/v1/admin/selenetv/users/{kid_id}", json={"allow_adult": True})
check("改权限 200", r.status_code == 200, r.text[:120])
check("权限已写入", users_repo.get_by_id(kid_id)["allow_adult"] == 1)

# 停用
admin.put(f"/api/v1/admin/selenetv/users/{kid_id}", json={"enabled": False})
ok, _, reason = users_repo.verify("kid", "kid_pwd")
check("停用用户校验失败", (not ok) and reason == "disabled", reason)
admin.put(f"/api/v1/admin/selenetv/users/{kid_id}", json={"enabled": True})

section("C. 兼容回退（现有用户零影响）")
ok, u, reason = users_repo.verify("aiden", PANEL_PWD)
check("表外用户走面板密码成功", ok and reason == "compat_owner", f"{ok} {reason}")
check("兼容 owner 有 18+ 权限", users_repo.allows_adult(u) is True)

section("D. 用户表鉴权")
ok, u, reason = users_repo.verify("kid", "kid_pwd")
check("用户表密码校验成功", ok and reason == "table", f"{ok} {reason}")
ok2, _, reason2 = users_repo.verify("kid", "wrong_pwd")
check("错误密码失败", (not ok2) and reason2 == "bad_password", reason2)

# 用 kid 的 Helios cookie 调 SeleneTV（构造双编码 auth cookie）
import json as _json
from urllib.parse import quote_plus
def helios_cookie(username, password):
    payload = _json.dumps({"role": "user", "username": username, "password": password},
                          ensure_ascii=False, separators=(",", ":"))
    return {"auth": quote_plus(quote_plus(payload))}

section("E. 18+ 权限真正生效")
# 造两个源：normal / adult
with database.write_txn() as conn:
    conn.execute(text(
        "INSERT OR REPLACE INTO sources(slug,name,api_url,homepage,status,enabled,total_videos,is_adult) "
        "VALUES('normalsrc','普通站','http://n','','ok',1,0,0)"
    ))
    conn.execute(text(
        "INSERT OR REPLACE INTO sources(slug,name,api_url,homepage,status,enabled,total_videos,is_adult) "
        "VALUES('adultsrc','成人站','http://a','','ok',1,0,1)"
    ))

admin.put(f"/api/v1/admin/selenetv/users/{kid_id}", json={"allow_adult": False})
ck_no = helios_cookie("kid", "kid_pwd")
r = anon.get("/api/search/resources", cookies=ck_no)
check("无18+权限: resources 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
keys = [x.get("key") for x in r.json()]
check("无18+权限: 不含成人源", "adultsrc" not in keys and "normalsrc" in keys, str(keys))

r = anon.get("/api/detail", params={"id": "123", "source": "adultsrc"}, cookies=ck_no)
check("无18+权限: detail 成人站 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

admin.put(f"/api/v1/admin/selenetv/users/{kid_id}", json={"allow_adult": True})
ck_yes = helios_cookie("kid", "kid_pwd")
r = anon.get("/api/search/resources", cookies=ck_yes)
keys = [x.get("key") for x in r.json()]
check("有18+权限: 含成人源", "adultsrc" in keys, str(keys))

# 兼容 owner（面板密码）也应见全源
ck_owner = helios_cookie("aiden", PANEL_PWD)
r = anon.get("/api/search/resources", cookies=ck_owner)
keys = [x.get("key") for x in r.json()]
check("兼容owner: 含成人源", "adultsrc" in keys, str(keys))

r = anon.get("/api/detail", params={"id": "123", "source": "adultsrc"}, cookies=ck_owner)
check("兼容owner: detail 成人站非404", r.status_code != 404, f"{r.status_code}")

section("F. 删除与零影响")
r = admin.delete(f"/api/v1/admin/selenetv/users/{kid_id}")
check("删除用户 200", r.status_code == 200, r.text[:120])
check("用户已删除", users_repo.get_by_id(kid_id) is None)
after = set(database.list_tables())
check("现有表未被删除", before.issubset(after), str(before - after))
check("无新增非selenetv表", all(t.startswith("selenetv_") or t in before for t in after))

print(f"\n结果：{passed} PASS / {failed} FAIL")
sys.exit(1 if failed else 0)
