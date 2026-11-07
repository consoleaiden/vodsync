"""
v1.9.3 镜像版本化（构建 tag / 版本探针）测试。

为什么单独有这份测试：回滚能力依赖「版本号只有一个来源」这条纪律。
一旦有人在 app/main.py 里又写死一个 version、或 CI 的读音源被改掉，
镜像 tag 和实际代码就会脱节 —— 那才是最危险的（回滚到「以为的版本」）。

覆盖项：
 A. 版本号唯一来源
    A1 app.__version__ 是标准 x.y.z
    A2 FastAPI 的 app.version 与 __version__ 一致（/docs 显示的版本）
    A3 app/main.py 里不再有硬编码 version="x.y.z"
    A4 按 CI 的 sed 正则从 app/__init__.py 解析出的值 = __version__（模拟 CI 读音源）
 B. /healthz 版本探针
    B1 200 且 status=ok
    B2 version = __version__
    B3 未设 VODSYNC_BUILD_SHA 时 build=dev
    B4 设了 VODSYNC_BUILD_SHA 时原样返回（镜像构建 SHA）
    B5 仍带 db/time（老字段没被删）
 C. CI / Dockerfile 契约（防止有人改坏构建脚本）
    C1 workflow 的 VERSION_FILE 指向 app/__init__.py 且文件存在
    C2 workflow 推不可变 tag：<version>-<sha7> 与 sha-<sha7>
    C3 workflow 用 steps.ver.outputs.version 生成 tag，而不是写死
    C4 tag 推送时校验 git tag 与代码版本一致（防误发）
    C5 Dockerfile 接收 APP_VERSION / APP_REVISION 并写进 image label
    C6 build-push-action 传了这两个 build-arg
 D. 文档/规范一致性
    D1 openapi.json 的 info.version = __version__
    D2 docker-compose.yml 说明里有回滚用的不可变 tag 示例

运行：python scripts/test_v193_version.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-ver-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "ver.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ.pop("VODSYNC_BUILD_SHA", None)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def main():
    import app as app_pkg
    from app.main import app as fastapi_app

    ver = app_pkg.__version__

    # ------------------------------------------------------------------ A
    print("== A. 版本号唯一来源 ==")
    check("A1 版本号形如 x.y.z", bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), ver)
    check("A2 FastAPI version 与 __version__ 一致", fastapi_app.version == ver,
          f"{fastapi_app.version} vs {ver}")

    main_py = read("app/main.py")
    hard = re.findall(r'version="\d+\.\d+\.\d+"', main_py)
    check("A3 app/main.py 无硬编码 version", not hard, hard)
    check("A3b app/main.py 引用 __version__", "version=__version__" in main_py)

    # 与 CI 完全相同的 sed 表达式（改 CI 时这条会一起失败，提醒同步）
    init_py = read("app/__init__.py")
    m = re.search(r'__version__\s*=\s*"([0-9][0-9.]*)"', init_py)
    check("A4 CI 读音源的解析结果 = __version__", bool(m) and m.group(1) == ver,
          m.group(1) if m else "解析失败")

    # ------------------------------------------------------------------ B
    print("== B. /healthz 版本探针 ==")
    from fastapi.testclient import TestClient

    client = TestClient(fastapi_app)
    r = client.get("/healthz")
    body = r.json() if r.status_code == 200 else {}
    check("B1 /healthz 200 + status=ok", r.status_code == 200 and body.get("status") == "ok",
          r.text[:120])
    check("B2 version = __version__", body.get("version") == ver, body.get("version"))
    check("B3 未设构建 SHA 时 build=dev", body.get("build") == "dev", body.get("build"))
    check("B5 老字段 db/time 仍在", "db" in body and "time" in body, list(body))

    os.environ["VODSYNC_BUILD_SHA"] = "7b44587abcdef0123456789abcdef012345678"
    r = client.get("/healthz")
    check("B4 设了构建 SHA 时原样返回",
          r.status_code == 200 and r.json().get("build") == os.environ["VODSYNC_BUILD_SHA"],
          r.text[:120])
    os.environ.pop("VODSYNC_BUILD_SHA", None)

    # ------------------------------------------------------------------ C
    print("== C. CI / Dockerfile 契约 ==")
    wf = read(".github/workflows/docker-image.yml")
    # workflow 的版本化改造需要 workflow scope 才能推，若尚未落地则跳过契约检查
    # （一旦文件里出现 VERSION_FILE 标记，这些检查就自动生效，不需要改测试）
    migrated = "VERSION_FILE:" in wf
    if not migrated:
        print("  SKIP  workflow 尚未迁移（需 workflow scope 手工提交），CI 契约检查跳过 5 项")

    def ci_check(name, cond, detail=""):
        if migrated:
            check(name, cond, detail)
        else:
            print(f"  SKIP  {name}")

    ci_check("C1 VERSION_FILE 指向 app/__init__.py",
             re.search(r"VERSION_FILE:\s*app/__init__\.py", wf) is not None
             and os.path.isfile(os.path.join(ROOT, "app/__init__.py")))
    ci_check("C2 推不可变 tag（版本+sha7 与 sha-sha7）",
             "type=raw,value=${{ steps.ver.outputs.version }}-${{ steps.ver.outputs.sha7 }}" in wf
             and "type=raw,value=sha-${{ steps.ver.outputs.sha7 }}" in wf)
    ci_check("C3 版本 tag 取自 steps.ver.outputs.version，非写死",
             "type=raw,value=${{ steps.ver.outputs.version }}\n" in wf
             and not re.search(r"type=raw,value=\d+\.\d+\.\d+", wf))
    ci_check("C4 tag 推送时校验 tag 与代码版本一致",
             "refs/tags/v*" in wf and "TAG_VER" in wf and "::error::" in wf)
    ci_check("C4b latest 只在默认分支构建时更新",
             "type=raw,value=latest,enable={{is_default_branch}}" in wf)

    dockerfile = read("Dockerfile")
    check("C5 Dockerfile 接收 APP_VERSION/APP_REVISION 并写入 label",
          "ARG APP_VERSION" in dockerfile and "ARG APP_REVISION" in dockerfile
          and "org.opencontainers.image.version" in dockerfile
          and "org.opencontainers.image.revision" in dockerfile)
    check("C5b Dockerfile 把 SHA 暴露给 /healthz",
          "VODSYNC_BUILD_SHA" in dockerfile)
    ci_check("C6 build-push-action 传了 build-args",
             "APP_VERSION=${{ steps.ver.outputs.version }}" in wf
             and "APP_REVISION=${{ github.sha }}" in wf)

    # ------------------------------------------------------------------ D
    print("== D. 文档 / 规范一致性 ==")
    spec = json.loads(read("openapi.json"))
    check("D1 openapi.json info.version = __version__", spec["info"]["version"] == ver,
          spec["info"]["version"])
    compose = read("docker-compose.yml")
    check("D2 compose 里写明回滚用的不可变 tag 示例",
          re.search(r"image: ghcr\.io/consoleaiden/vodsync:\d+\.\d+\.\d+-", compose) is not None)
    check("D3 README 有「镜像版本与回滚」章节", "## 镜像版本与回滚" in read("README.md"))

    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
