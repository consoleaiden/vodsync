"""app 包。"""

# 版本号唯一来源（v1.9.3 起）：
#   - CI（.github/workflows/docker-image.yml）从这里读取，生成 版本 / 主.次 / 版本-<sha7> 等镜像 tag
#   - FastAPI 的 /docs 与 /healthz 也取自这里，部署后能直接核对跑的是哪个版本
# 发版只改这一行；打 tag 时 CI 会校验 tag 与这里的值是否一致，不一致直接失败。
__version__ = "1.12.0"
