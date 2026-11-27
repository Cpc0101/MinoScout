#!/usr/bin/env python3
"""把 Scout 冻结成不依赖 Python 的可执行文件。

**为什么必须有这一步**：执行节点跑在别人的机器上，不能假设那台机器装了 Python 3.12。
现有的 `install.sh` 第一件事就是检查 `sys.version_info >= (3, 12)`，不满足直接退出 ——
对机房专机勉强可行，对任何非开发机器都是死路。

用法：
    python scripts/build_binary.py                 # 冻结到 build/dist/mino-scout/
    python scripts/build_binary.py --check         # 冻结后跑一次 probe 自检
    python scripts/build_binary.py --clean         # 先清干净

产物是 **onedir**（一个目录，不是单文件）：
  - 单文件启动要每次解压到临时目录，Scout 是常驻进程，没必要付这个代价
  - onedir 便于按层增量替换与回滚（见 scripts/layers.py）

## mino_scout 刻意**不进** PYZ

冻结产物里 `app/mino_scout/` 是**纯源码**，不是编进可执行文件的字节码。原因是包体：
实测 onedir 1.0 GB，Scout 自己的代码只占 0.9%，而每次发版只有它在变。留在 PYZ 里，
"改一行代码"的最小更新单位就是 9.2 MB 的可执行文件，而它和 `_internal/` 是同一次
build 的绑定产物、不能单独换 —— 于是每次发版都得重推 450 MB。挪出来之后是 85 KB。

这带来两个后果，都必须处理：

1. **入口只能用 importlib 取 mino_scout。** 写成 `from mino_scout.cli import main`
   的话 PyInstaller 静态分析会看见它、又把整个包拖回 PYZ；而 FrozenImporter 的优先级
   高于文件系统，外挂的那份会被**无声忽略** —— 症状是改了代码却没生效，极难排查。
   所以入口用 `importlib.import_module`，并且额外加 `--exclude-module mino_scout`。
2. **依赖发现要自己接上。** mino_scout 退出分析范围后，PyInstaller 不再顺着它的
   `import adbutils` 去发现第三方包，连它用的**标准库**模块也会漏。
   `scan_external_imports()` 用 ast 把 mino_scout 里所有外部 import 扒出来当
   hiddenimports 顶上 —— 覆盖第三方和 stdlib 两者。扫描保留完整模块名
   （例如 `html.parser`，不能只留顶层 `html`）。mino_scout 内**没有**动态
   import（`importlib` / `__import__` 全仓零引用，grep 验证过），所以 ast 扫描是完备的。

## 已知的坑（都来自上游 MiniOrangeServer/build.py 的经验）

1. **Playwright 的浏览器要单独装进 onedir。** `collect-all playwright` 只带 Python 包和
   driver/`node`，不含 Chromium。冻结后执行 `playwright install chromium` 写到
   `ms-playwright/`，运行时 `PLAYWRIGHT_BROWSERS_PATH` 指向那里。
2. **动态 import 的模块要显式声明。** 见上面第 2 点。
3. **adbutils 带二进制资源**（adb.exe 等），需要 collect_all 才能带上。
4. **zip 必须用 Unix 文件模式。** PyInstaller 拷出来的 `node` 经常是 0644；打包脚本按
   Mach-O/ELF/PE 魔数写成 0755，否则 macOS 解压后 Playwright 会 Permission denied。
5. **改了这里的 COLLECT_ALL / EXTRA_HIDDEN / EXCLUDES / 入口脚本，
   要把 `scripts/layers.py` 的 `RUNTIME_ABI` +1。** 否则依赖声明没变、产物变了，
   runtime 指纹却不动，客户端会误以为自己那份 runtime 还能配新 app。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = BUILD / "dist"
WORK = BUILD / "work"
NAME = "mino-scout"
PKG = "mino_scout"


def _configure_utf8_stdio() -> None:
    """Windows CI 默认 cp1252，print 中文 / → 会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_utf8_stdio()

# 需要连数据文件一起收集的包（光靠静态分析带不全）
COLLECT_ALL = ["adbutils", "uiautomator2", "playwright", "zeroconf", "certifi"]

# 静态分析看不到、但运行时会 import 的
EXTRA_HIDDEN = [
    "websockets", "websockets.client", "websockets.legacy",
    "PIL", "PIL.Image",
    "numpy",
    "pydantic", "pydantic.deprecated.decorator",
    "psutil",
    "wda",              # facebook-wda，包名和 pip 名不一样
    "encodings.idna",
]

# 明确排除：Scout 用不到，但会被依赖链拖进来的大件
EXCLUDES = [
    "torch", "torchvision", "transformers", "open_clip",
    "paddle", "paddleocr", "cv2", "onnxruntime", "ultralytics",
    "matplotlib", "scipy", "pandas", "IPython", "tkinter",
    "sqlalchemy",       # 硬约束 1：Scout 不碰数据库
    "fastapi", "uvicorn",
    # 录屏相关：uiautomator2/screenrecord.py 会 import 它们，但 Scout 对
    # av / imageio / ffmpeg / screenrecord **零引用**（grep 验证过），
    # 而且它们不是 uiautomator2 的声明依赖，只是碰巧装在开发 venv 里。
    # 实测这两个占 100 MB —— 冻结产物的 26%。
    "av", "imageio", "imageio_ffmpeg",
    "uiautomator2.screenrecord",
]

# 入口。刻意用 importlib 取 mino_scout —— 见模块 docstring「mino_scout 刻意不进 PYZ」。
_ENTRY_SRC = '''\
# PyInstaller 入口。真正的实现在外挂的 app/mino_scout/cli.py。
import importlib
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _base = Path(sys.executable).resolve().parent
    _browsers = _base / "ms-playwright"
    if _browsers.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_browsers))
    # app 层是纯源码，与可执行文件同级。缺了就是安装/更新没落地完整，
    # 明确报错，别让它变成一句难认的 ModuleNotFoundError。
    _app = _base / "app"
    if not (_app / "mino_scout" / "cli.py").is_file():
        sys.stderr.write(
            "mino-scout: app layer missing at %s\\n"
            "The install is incomplete - reinstall from Mino Workbench.\\n" % _app
        )
        sys.exit(2)
    sys.path.insert(0, str(_app))

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if not _reconfigure:
        continue
    try:
        _reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_cli = importlib.import_module("mino_scout.cli")
sys.exit(_cli.main())
'''


def scan_external_imports(pkg: str) -> list[str]:
    """用 ast 扒出 pkg 里所有**外部** import 的完整模块名。

    这些要当 hiddenimports 交给 PyInstaller —— pkg 本身被 --exclude-module 排掉了，
    分析器不会再顺着它去发现依赖，连 stdlib 都会漏。必须保留完整模块名：
    只记 `html` 不会自动收集 `html.parser`。相对 import 与 pkg 自身跳过。
    """
    found: set[str] = set()
    base = ROOT / pkg
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise SystemExit(f"{path} 语法错误，无法扫描 import：{exc}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # from . import x —— 包内相对
                    continue
                if node.module:
                    found.add(node.module)
    found = {name for name in found if name != pkg and not name.startswith(pkg + ".")}
    return sorted(found)


def hidden_imports(pkg: str) -> list[str]:
    """只把**本机真的能导入**的名字交给 PyInstaller。

    `scan_external_imports` 是静态扫描，会把 `if sys.platform == "win32"` 分支里
    的 pywinauto、以及可选 extra（cua_driver）也算进来。跨平台构建时这些模块
    根本不装，PyInstaller 对每个不存在的 hidden-import 都会报 ERROR；Linux 首次
    构建就是栽在这里。缺的模块本来也应该由运行时按平台降级，不能因此让打包失败。
    """
    names = set(scan_external_imports(pkg)) | set(EXTRA_HIDDEN)
    out: list[str] = []
    for name in sorted(names):
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except (ImportError, AttributeError, ValueError):
            continue
        out.append(name)
    return out


def app_dir(dist_dir: Path) -> Path:
    return dist_dir / "app"


def write_runtime_lock(dist_dir: Path) -> None:
    """写入 pip freeze，供 runtime 层指纹与排障使用。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=120,
    )
    if proc.returncode != 0:
        print("→ 警告：pip freeze 失败，未写入 runtime.lock")
        return
    path = dist_dir / "runtime.lock"
    path.write_text(proc.stdout, encoding="utf-8")
    print(f"→ runtime.lock ({len(proc.stdout.splitlines())} packages)")


def materialize_app(dist_dir: Path) -> Path:
    """把 mino_scout 源码放到冻结产物的 app/ 下。

    放在这里而不是打包时才拷，是为了让 --check 自检跑的就是真实布局 ——
    自检要能发现"app 层没落地"这类问题。
    """
    dest = app_dir(dist_dir) / PKG
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT / PKG, dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    return dest


def build(*, clean: bool) -> Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "需要 PyInstaller：pip install pyinstaller\n"
            "（它只在打包时用，不进 pyproject 的运行依赖）"
        )

    if clean and BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    entry = BUILD / "_entry.py"
    entry.write_text(_ENTRY_SRC, encoding="utf-8")

    hidden = hidden_imports(PKG)
    argv = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(BUILD),
        "--console",                 # 常驻服务，不要 windowed
        "--paths", str(ROOT),
    ]
    for mod in hidden:
        argv += ["--hidden-import", mod]
    # Optional wheels must include their native library AND distribution metadata.
    # Linux/Windows release readiness is independent from merely bundling a wheel.
    if importlib.util.find_spec("cua_driver") is not None:
        argv += ["--collect-all", "cua_driver", "--copy-metadata", "cua-driver"]
    for pkg in COLLECT_ALL:
        argv += ["--collect-all", pkg]
    for pkg in list(EXCLUDES) + [PKG]:   # PKG 排掉：它走 app 层，不进 PYZ
        argv += ["--exclude-module", pkg]
    argv.append(str(entry))

    print(f"→ 冻结 {NAME}（{len(hidden)} 个 hidden-import，{len(COLLECT_ALL)} 个 collect-all，"
          f"{PKG} 走 app 层不进 PYZ）")
    proc = subprocess.run(argv, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，rc={proc.returncode}")

    out = DIST / NAME
    if not out.is_dir():
        raise SystemExit(f"产物目录没生成：{out}")
    _assert_pkg_not_frozen(out)
    materialize_app(out)
    write_runtime_lock(out)
    if not str(os.environ.get("MINO_SCOUT_SKIP_BROWSER") or "").strip():
        install_playwright_chromium(out)
    else:
        print("→ MINO_SCOUT_SKIP_BROWSER：跳过打包 Chromium（首次安装后由 Scout 拉 browser 层）")
    chmod_frozen_payload(out)
    return out


def _assert_pkg_not_frozen(dist_dir: Path) -> None:
    """确认 mino_scout 真的没被编进 PYZ。

    如果它同时存在于 PYZ 和 app/，FrozenImporter 会赢，app 层的修改**无声失效**。
    这个失败模式太难认，值得在 build 阶段就断掉。
    """
    exe = dist_dir / (f"{NAME}.exe" if platform.system() == "Windows" else NAME)
    if not exe.is_file():
        return
    needle = f"{PKG}.cli".encode("utf-8")
    try:
        blob = exe.read_bytes()
    except OSError:
        return
    if needle in blob:
        raise SystemExit(
            f"{PKG} 似乎被编进了可执行文件的 PYZ（在 {exe.name} 里找到 {PKG}.cli）。\n"
            "FrozenImporter 优先级高于文件系统，app 层会被无声忽略。\n"
            "检查入口是否用了 importlib，以及 --exclude-module 是否生效。"
        )


def browsers_dir(dist_dir: Path) -> Path:
    return dist_dir / "ms-playwright"


def install_playwright_chromium(dist_dir: Path) -> None:
    dest = browsers_dir(dist_dir)
    dest.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(dest)
    print(f"→ playwright install chromium → {dest}")
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=env,
        cwd=str(ROOT),
        timeout=15 * 60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"playwright install chromium 失败，rc={proc.returncode}")


def chmod_frozen_payload(dist_dir: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from pack_release import chmod_payload  # noqa: E402

    chmod_payload(dist_dir)


def binary_path(dist_dir: Path) -> Path:
    exe = dist_dir / (f"{NAME}.exe" if platform.system() == "Windows" else NAME)
    if not exe.is_file():
        raise SystemExit(f"找不到可执行文件：{exe}")
    return exe


def dir_size_mb(path: Path) -> int:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return round(total / 1024 / 1024)


def report_layers(dist_dir: Path) -> None:
    """按层报体积 —— 分层的收益要看得见，回归了也要一眼看出来。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import layers as L  # noqa: E402

    buckets = L.split_frozen(dist_dir)
    print("\n层        文件数   体积")
    for name in L.LAYERS:
        files = buckets[name]
        size = round(sum(p.stat().st_size for p in files) / 1024 / 1024, 1)
        print(f"{name:<9} {len(files):>6}   {size:>7} MB")


def check(dist_dir: Path) -> int:
    """跑一次 probe，确认冻结产物真的能用。

    **这一步不能省。** PyInstaller 最常见的失败是"打包成功但一跑就 ModuleNotFoundError"
    —— 懒加载模块没进 hiddenimports。只有真跑一次才看得出来。
    把 mino_scout 挪出 PYZ 之后更不能省：依赖发现改成了 ast 扫描，漏一个
    stdlib 模块的唯一症状就是运行时 ImportError。
    """
    exe = binary_path(dist_dir)
    print(f"\n→ 自检：{exe} probe")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # 确保没有偷偷用到源码树
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    browsers = browsers_dir(dist_dir)
    if browsers.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    proc = subprocess.run(
        [str(exe), "probe"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        cwd=str(dist_dir),
        stdin=subprocess.DEVNULL,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    print(f"rc={proc.returncode}")
    print("--- stdout ---")
    print(stdout[-2000:] or "(empty)")
    print("--- stderr ---")
    print(stderr[-2000:] or "(empty)")
    # 1 = 没有可用 executor，也算跑通了（CI 上本来就没有设备）
    if proc.returncode not in (0, 1):
        print(f"\n自检失败 rc={proc.returncode}")
        return 1
    if '"node_id"' not in stdout and '"node_id"' not in stderr:
        print("自检失败：probe 没有输出 manifest")
        return 1
    worker = subprocess.run([str(exe), "worker-check"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=180, cwd=str(dist_dir), stdin=subprocess.DEVNULL)
    print("--- frozen worker submit check ---")
    print(worker.stdout[-4000:])
    print(worker.stderr[-4000:])
    if worker.returncode != 0 or 'PASS: real ScriptRecords.submit' not in worker.stdout:
        print(f"自检失败：frozen worker submit rc={worker.returncode}")
        return 1
    print(f"自检通过。产物大小：{dir_size_mb(dist_dir)} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结 Scout 为免 Python 的可执行文件")
    ap.add_argument("--check", action="store_true", help="冻结后跑一次 probe 自检")
    ap.add_argument("--clean", action="store_true", help="先清掉 build/")
    args = ap.parse_args()

    dist_dir = build(clean=args.clean)
    print(f"\n产物：{dist_dir}（{dir_size_mb(dist_dir)} MB）")
    print(f"可执行：{binary_path(dist_dir)}")
    report_layers(dist_dir)
    if args.check:
        return check(dist_dir)
    print("\n提示：加 --check 会真跑一次 probe —— PyInstaller 最常见的失败是"
          "『打包成功但一跑就 ModuleNotFoundError』，只有真跑才看得出来。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
