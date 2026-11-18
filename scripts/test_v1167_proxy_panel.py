"""
v1.16.7 回归测试：中转代理配置从 docker 环境变量搬到管理面板。

背景
----
原先 PROXY_ENABLED / PROXY_URLS / PROXY_PREF_TTL_MINUTES / PROXY_CONCURRENCY
只能写在 docker-compose，改一次要重启容器。现在面板可改、立即生效，
持久化在 settings 表。

本测试锁定：
1. 面板没配过时，生效值 == 环境变量默认值，source == "env"（平滑升级）；
2. 面板保存后生效值立即改变，source == "panel"，重启（重建缓存）后仍在；
3. 非法输入被收敛而不是抛异常（URL 非 http/https 丢弃、超范围回落默认）；
4. 「恢复环境变量默认」会清掉面板配置并回到 env；
5. maccms 的并发信号量会随面板并发上限变化而重建（真的即时生效）。

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1167_proxy_panel.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.gettempdir(), "_v1167_proxy_panel.db")
if os.path.exists(DB):
    os.remove(DB)

os.environ["DATABASE_URL"] = f"sqlite:///./{DB}"
os.environ["PROXY_ENABLED"] = "true"
os.environ["PROXY_URLS"] = "https://env-a.example/,https://env-b.example/"
os.environ["PROXY_PREF_TTL_MINUTES"] = "30"
os.environ["PROXY_CONCURRENCY"] = "4"
os.environ["PROXY_TIMEOUT"] = "20"

from app import database, proxyconf  # noqa: E402
from app import maccms  # noqa: E402

database.init_db()

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


# ---- 1) 无面板配置：完全走环境变量 ----
env = proxyconf.env_default()
check("env 默认 enabled", env["enabled"] is True)
check("env 默认 urls", env["urls"] == ["https://env-a.example", "https://env-b.example"], env["urls"])
check("env 默认 ttl", env["pref_ttl_minutes"] == 30)
check("env 默认并发", env["concurrency"] == 4)

cur = proxyconf.get(refresh=True)
check("未配置时生效值 == env", cur == env, cur)
check("未配置时 source == env", proxyconf.source() == "env", proxyconf.source())

# ---- 2) 面板保存后立即生效 ----
saved = proxyconf.set_config({
    "enabled": False,
    "urls": ["https://panel-a.example/", "https://panel-b.example/"],
    "pref_ttl_minutes": 5,
    "concurrency": 9,
    "timeout": 7,
})
check("保存后 enabled 生效", proxyconf.get(refresh=True)["enabled"] is False)
check("保存后 urls 生效（去尾斜杠）",
      proxyconf.get()["urls"] == ["https://panel-a.example", "https://panel-b.example"],
      proxyconf.get()["urls"])
check("保存后 ttl 生效", proxyconf.get()["pref_ttl_minutes"] == 5)
check("保存后并发生效", proxyconf.get()["concurrency"] == 9)
check("保存后超时生效", proxyconf.get()["timeout"] == 7)
check("保存后 source == panel", proxyconf.source() == "panel")

# 模拟重启：清进程内缓存后仍应从 settings 读回面板值
proxyconf.invalidate()
cur2 = proxyconf.get(refresh=True)
check("重建缓存后仍是面板值（已持久化）",
      cur2["urls"] == ["https://panel-a.example", "https://panel-b.example"] and cur2["concurrency"] == 9,
      cur2)

# ---- 3) 非法输入被收敛 ----
dirty = proxyconf.set_config({
    "enabled": True,
    "urls": ["ftp://nope.example", "https://ok.example", "not-a-url", "https://ok.example"],
    "pref_ttl_minutes": 99999,      # 超上限 -> 回落 env 的 30
    "concurrency": 0,               # 低于下限 -> 回落 env 的 4
    "timeout": "abc",               # 非数字 -> 回落 env 的 20
})
check("非 http/https 与垃圾 URL 被丢弃", dirty["urls"] == ["https://ok.example"], dirty["urls"])
check("重复 URL 去重", len(dirty["urls"]) == 1, dirty["urls"])
check("超上限 ttl 回落默认", dirty["pref_ttl_minutes"] == 30, dirty["pref_ttl_minutes"])
check("越界并发回落默认", dirty["concurrency"] == 4, dirty["concurrency"])
check("非数字超时回落默认", dirty["timeout"] == 20, dirty["timeout"])

# 逗号字符串形式也应被接受（面板表单可能直接给串）
csv_in = proxyconf.set_config({
    "enabled": True,
    "urls": "https://c1.example, https://c2.example",
    "pref_ttl_minutes": 1, "concurrency": 2, "timeout": 3,
})
check("urls 支持逗号串", csv_in["urls"] == ["https://c1.example", "https://c2.example"], csv_in["urls"])

# ---- 4) 恢复环境变量默认 ----
back = proxyconf.reset()
check("reset 后 == env 默认", back == env, back)
check("reset 后 source == env", proxyconf.source() == "env")
proxyconf.invalidate()
check("reset 后生效值 == env", proxyconf.get(refresh=True) == env)

# ---- 5) maccms 并发信号量随配置变化 ----
proxyconf.set_config({
    "enabled": True, "urls": ["https://sem.example"],
    "pref_ttl_minutes": 10, "concurrency": 3, "timeout": 5,
})
sem_a = maccms._proxy_semaphore("https://sem.example")
check("并发=3 时信号量容量为 3", sem_a._value == 3, sem_a._value)

proxyconf.set_config({
    "enabled": True, "urls": ["https://sem.example"],
    "pref_ttl_minutes": 10, "concurrency": 8, "timeout": 5,
})
sem_b = maccms._proxy_semaphore("https://sem.example")
check("并发=8 时信号量重建为 8", sem_b._value == 8, sem_b._value)
check("确实换了信号量对象", sem_a is not sem_b)

# 路由选择也应跟随面板开关：关掉代理后不应再有代理路由
proxyconf.set_config({
    "enabled": False, "urls": ["https://sem.example"],
    "pref_ttl_minutes": 10, "concurrency": 4, "timeout": 5,
})
client = maccms.MacCmsClient(timeout=1, retries=0)
routes = client._routes("some.host.example")
check("关闭代理后路由只剩直连", [k for k, _ in routes] == ["direct"], routes)

proxyconf.reset()
client.close()

print()
if fails:
    print(f"{len(fails)} 项失败: {fails}")
    sys.exit(1)
print("ALL_V1167_PROXY_PANEL_TESTS_PASS")
try:
    os.remove(DB)
except OSError:
    pass
