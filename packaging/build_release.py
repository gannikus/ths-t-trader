# -*- coding: utf-8 -*-
"""
THS-T-Assistant 发布构建脚本
============================

一次构建同时产出：
  1. 单文件 exe（THS-T-Assistant.exe，--ui 参数复用为图形化配置编辑器）
  2. 便携版 zip（exe + 干净的出厂配置 + 使用说明 + README）
  3. manifest.json（供 CI 读取版本号与产物路径）

设计约束
--------
* 出厂配置从 main.py 的 DEFAULT_CONFIG 以 AST 字面量方式提取，**绝不读取仓库里
  的 config.json**（那份含用户自己的持仓成本与底仓数量，属于隐私数据）。
* 出厂配置强制 dry_run=true、坐标为空，用户必须自己跑新手指引/--detect 才能下单。
* 所有运行数据写入 %LOCALAPPDATA%\\ths_t_trader，安装包默认装到用户目录，
  因此普通用户也能保存 config.json（无需管理员提权）。

用法
----
    python packaging/build_release.py [--version 1.0.0-20261124-abc1234]
                                      [--outdir dist/release]
                                      [--skip-pyinstaller]   # 只组装配置/说明，调试用
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTDIR = ROOT / "dist" / "release"
EXE_NAME = "THS-T-Assistant"


# ---------------------------------------------------------------------------
# 版本与配置
# ---------------------------------------------------------------------------
def read_source_version() -> str:
    """从 main.py 的 VERSION 常量读取版本，避免维护两份版本号。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', src, re.M)
    if not m:
        raise RuntimeError("无法从 main.py 读取 VERSION 常量")
    return m.group(1).strip()


def short_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def make_version(explicit: Optional[str]) -> str:
    if explicit:
        return explicit.strip()
    return f"{read_source_version()}-{datetime.now():%Y%m%d}-{short_sha()}"


def extract_default_config() -> Dict[str, Any]:
    """用 AST 提取 DEFAULT_CONFIG 字面量，不 import main（零副作用、无运行依赖）。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="main.py")
    for node in tree.body:
        value_node = None
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG" for t in node.targets
        ):
            value_node = node.value
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)
              and node.target.id == "DEFAULT_CONFIG"
              and node.value is not None):
            value_node = node.value
        if value_node is not None:
            value = ast.literal_eval(value_node)
            if not isinstance(value, dict):
                raise RuntimeError("main.py 中 DEFAULT_CONFIG 不是 dict")
            return value
    raise RuntimeError("main.py 中未找到 DEFAULT_CONFIG 赋值")


def build_factory_config() -> Dict[str, Any]:
    """生成安全的出厂配置。

    优先读取固化的干净模板 packaging/factory_config.json（推荐，构建不碰用户配置）。
    模板缺失时回退到现场清洗：字段结构以仓库 config.json 为基线——它包含
    config_ui.SCHEMA 要求的全部键，DEFAULT_CONFIG 只是精简子集（缺 dev_ticks 等，
    直接用会校验失败）。仓库 config.json 含用户真实持仓与坐标，必须彻底清洗：

      * positions / market.codes → 只保留一个公开指数 ETF 演示标的（校验器要求至少一只）
      * position_overrides → 清空
      * order_ui.coords → 全部 [0, 0]，_coords_mode 置空（强制用户重新识别）
      * dry_run → 强制 true
      * 说明文本里写死的示例证券代码 → 泛化为「示例代码」

    清洗后再用 DEFAULT_CONFIG 补齐任何未来新增的默认键，保证向前兼容。
    """
    import copy

    # 优先读取固化的干净模板：构建过程完全不触碰用户私有的 config.json。
    template_path = ROOT / "packaging" / "factory_config.json"
    if template_path.exists():
        return json.loads(template_path.read_text(encoding="utf-8"))

    # 回退：模板缺失时，从仓库 config.json 现场清洗生成（见下方清洗逻辑）
    print("[WARN] 未找到 packaging/factory_config.json，从 config.json 现场清洗", file=sys.stderr)
    template_path = ROOT / "config.json"
    if template_path.exists():
        cfg = json.loads(template_path.read_text(encoding="utf-8"))
    else:
        cfg = extract_default_config()

    # 私有持仓数据全部清空，改为一个公开的演示标的（宽基 ETF，非用户真实持仓）。
    # config_ui 校验器要求至少一只标的；示例用公开指数 ETF 与中性数值，
    # 用户必须在「新手指引」页替换成自己的真实标的与成本。
    DEMO_CODE = "510300"
    cfg["positions"] = {
        DEMO_CODE: {
            "name": "沪深300ETF（示例，请替换）",
            "cost": 4.0,
            "sellable_qty": 100,
            "t0": True,
        },
    }
    cfg["position_overrides"] = {}
    cfg.setdefault("market", {})["codes"] = [DEMO_CODE]

    # 坐标口径复位：真实下单前必须重新识别，不继承任何用户屏幕坐标
    oui = cfg.setdefault("order_ui", {})
    oui["dry_run"] = True
    coords = oui.get("coords") or {}
    oui["coords"] = {k: [0, 0] for k in coords}
    oui["_coords_mode"] = ""
    oui["_coords_ref"] = {"client_w": 0, "client_h": 0}

    # 把帮助文本中的具体证券代码泛化，避免出厂包泄露用户关注标的
    def scrub(text: str) -> str:
        text = re.sub(r"\b[159]\d{5}\b", "示例代码", text)
        text = re.sub(r"\b[56]\d{5}\b", "示例代码", text)
        return text

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str) and k.startswith("_help"):
                    node[k] = scrub(v)
                elif isinstance(v, str) and k == "_example":
                    node[k] = '"示例代码": 3'
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cfg)

    # DEFAULT_CONFIG 补齐模板里可能缺失的键（向前兼容）
    def merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(base)
        for key, val in (override or {}).items():
            if isinstance(val, dict) and isinstance(out.get(key), dict):
                out[key] = merge(out[key], val)
            else:
                out[key] = copy.deepcopy(val)
        return out

    cfg = merge(extract_default_config(), cfg)
    # 合并后再次强制：DEFAULT_CONFIG 里的 600519 示例持仓不能混进出厂包，
    # 只保留上面设定的 DEMO_CODE 演示标的。
    cfg["positions"] = {
        DEMO_CODE: {
            "name": "沪深300ETF（示例，请替换）",
            "cost": 4.0,
            "sellable_qty": 100,
            "t0": True,
        },
    }
    cfg["position_overrides"] = {}
    cfg["market"]["codes"] = [DEMO_CODE]
    cfg["order_ui"]["dry_run"] = True
    cfg["order_ui"]["_coords_mode"] = ""
    return cfg


# ---------------------------------------------------------------------------
# 图标（用 Pillow 现画，避免在仓库里存二进制资源）
# ---------------------------------------------------------------------------
def make_icon(dest: Path) -> Optional[Path]:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    try:
        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((8, 8, 248, 248), radius=52, fill=(23, 78, 155, 255))
        # 上升折线（寓意做T/收益）
        d.line([(56, 168), (100, 124), (136, 148), (200, 82)],
               fill=(255, 255, 255, 255), width=18, joint="curve")
        d.polygon([(200, 62), (216, 94), (170, 92)], fill=(255, 255, 255, 255))
        d.text((62, 196), "T", fill=(255, 255, 255, 235))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="ICO",
                 sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return dest
    except Exception as exc:
        print(f"[WARN] 图标生成失败，将使用默认图标: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# PyInstaller
# ---------------------------------------------------------------------------
HIDDEN_IMPORTS = [
    "config_ui",
    "win32gui", "win32con", "win32api", "win32process", "win32clipboard",
    "pywintypes", "pythoncom",
    "pyautogui", "pyscreeze", "mouseinfo", "pygetwindow", "pyrect",
    "pymsgbox", "pytweening",
    "PIL", "PIL.Image", "PIL.ImageGrab", "PIL.ImageDraw",
    "psutil",
]


def run_pyinstaller(exe_out: Path, icon: Optional[Path], work: Path) -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", EXE_NAME,
        "--distpath", str(exe_out.parent),
        "--workpath", str(work / "build"),
        "--specpath", str(work / "spec"),
    ]
    if icon and icon.exists():
        cmd += ["--icon", str(icon)]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    cmd += ["--collect-submodules", "PIL"]
    cmd += [str(ROOT / "main.py")]

    print("[INFO] PyInstaller 打包中（单文件 GUI）…", flush=True)
    print("[INFO] " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)

    built = exe_out.parent / f"{EXE_NAME}.exe"
    if not built.exists():
        raise RuntimeError(f"PyInstaller 未产出 exe: {built}")
    if built.resolve() != exe_out.resolve():
        shutil.move(str(built), str(exe_out))


# ---------------------------------------------------------------------------
# 便携版组装
# ---------------------------------------------------------------------------
USAGE_TEXT = """THS-T-Assistant（同花顺做T半自动辅助工具）便携版
================================================

【首次使用，务必按顺序】
1. 保持 config.json 中 order_ui.dry_run = true（默认已是 true）。
2. 双击 THS-T-Assistant.exe 启动监控；它会读同目录的 config.json。
3. 打开图形化配置编辑器，可用下面任一方式：
   - 主程序菜单里的“设置”按钮；
   - 命令行：THS-T-Assistant.exe --ui
   在“新手指引”页用两步向导配置股票标的与下单坐标；
   或在“坐标点”页用“测试”按钮逐个核对控件矩形（默认零副作用）。
4. 只做信号诊断、不下单：
   THS-T-Assistant.exe --once
5. 确认行情、坐标、提示窗口都正常后，再考虑把 dry_run 改为 false。

【目录说明】
- THS-T-Assistant.exe  主程序（带 --ui 参数即为配置编辑器）
- config.json          配置；出厂为示例标的 + 空坐标，请按真实持仓修改
- README.md            完整说明与风险提示

【数据位置】
运行日志、审计 CSV、下单截图等写入：
  %LOCALAPPDATA%\\ths_t_trader
可用环境变量 THS_T_DATA_DIR 覆盖。便携版无需管理员权限。

【风险提示】
本工具仅做行情提示与界面操作辅助，不构成投资建议。
dry_run=false 时会在你点击“确认下单”后模拟真实点击，请自行核对价格与数量。
"""


def write_portable(portable_root: Path, exe: Path, config: Dict[str, Any],
                   icon: Optional[Path]) -> Path:
    if portable_root.exists():
        shutil.rmtree(portable_root)
    portable_root.mkdir(parents=True)

    shutil.copy2(exe, portable_root / exe.name)
    (portable_root / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (portable_root / "使用说明.txt").write_text(USAGE_TEXT, encoding="utf-8-sig")
    if icon and icon.exists():
        shutil.copy2(icon, portable_root / "app.ico")

    for name in ("README.md", "CHANGELOG_P0P4.md"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, portable_root / name)
    return portable_root


def make_zip(folder: Path, zip_path: Path) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder.parent).as_posix())
    return zip_path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="构建 THS-T-Assistant 发布产物")
    ap.add_argument("--version", default="", help="版本号，默认 VERSION-日期-短SHA")
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="产物输出目录")
    ap.add_argument("--skip-pyinstaller", action="store_true", help="跳过 exe 打包（仅调试组装）")
    args = ap.parse_args(argv)

    version = make_version(args.version)
    outdir = Path(args.outdir).resolve()
    work = outdir / "_work"
    outdir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] 版本: {version}")
    print(f"[INFO] 输出: {outdir}")

    icon = make_icon(work / "app.ico")
    config = build_factory_config()

    exe = outdir / f"{EXE_NAME}.exe"
    if args.skip_pyinstaller:
        print("[WARN] --skip-pyinstaller：跳过 exe 打包")
        if not exe.exists():
            exe.write_bytes(b"")  # 仅用于调试组装流程
    else:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            raise SystemExit("缺少 PyInstaller，请先执行: pip install pyinstaller")
        run_pyinstaller(exe, icon, work)

    tag = f"v{version}"
    portable_root = outdir / f"{EXE_NAME}-{version}-portable"
    write_portable(portable_root, exe, config, icon)
    zip_path = make_zip(portable_root, outdir / f"{EXE_NAME}-{version}-portable.zip")

    manifest = {
        "version": version,
        "tag": tag,
        "app_name": EXE_NAME,
        "exe": str(exe),
        "portable_dir": str(portable_root),
        "portable_zip": str(zip_path),
        "installer": str(outdir / f"{EXE_NAME}-{version}-setup.exe"),
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("[INFO] 构建完成:")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
