#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


APP_NAME = "医务科监控修复助手"
VENDOR_FFMPEG_DIR = Path("vendor") / "ffmpeg-windows-x64"


def require_binary(name: str) -> Path:
    for executable_name in (f"{name}.exe", name):
        found = shutil.which(executable_name)
        if found:
            return Path(found).resolve()
    raise RuntimeError(f"找不到 {name}.exe，请先安装 Windows 版 FFmpeg。")


def bundled_binary(project_dir: Path, name: str) -> Path | None:
    candidate = project_dir / VENDOR_FFMPEG_DIR / f"{name}.exe"
    if candidate.exists():
        return candidate.resolve()
    return None


def resolve_ffmpeg_binary(project_dir: Path, name: str) -> Path:
    bundled = bundled_binary(project_dir, name)
    if bundled is not None:
        return bundled
    return require_binary(name)


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    print(subprocess.list2cmdline(command))
    subprocess.run(command, check=True, env=env)


def zip_directory(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()

    parent = source_dir.parent
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            archive.write(path, path.relative_to(parent))


def main() -> int:
    if sys.platform != "win32":
        print(
            "Windows 版本必须在 Windows 上打包；PyInstaller 不能从 macOS "
            "交叉生成 Windows EXE。",
            file=sys.stderr,
        )
        return 2

    project_dir = Path(__file__).resolve().parent
    gui_py = project_dir / "gui.py"
    if not gui_py.exists():
        print(f"找不到 {gui_py}", file=sys.stderr)
        return 2

    try:
        ffmpeg = resolve_ffmpeg_binary(project_dir, "ffmpeg")
        ffprobe = resolve_ffmpeg_binary(project_dir, "ffprobe")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if ffmpeg.parent == project_dir / VENDOR_FFMPEG_DIR:
        print(f"使用 Windows 版 FFmpeg：{ffmpeg.parent}")
    else:
        print(
            "警告：未找到 vendor\\ffmpeg-windows-x64，"
            "将使用本机 PATH 中的 FFmpeg；请确认目标电脑兼容。"
        )

    pyinstaller_check = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        text=True,
        capture_output=True,
    )
    if pyinstaller_check.returncode != 0:
        print("找不到 PyInstaller。请先运行：py -3.12 -m pip install pyinstaller")
        return 2

    dist_windows = project_dir / "dist" / "windows"
    spec_dir = project_dir / "build" / "windows-spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--workpath",
        str(project_dir / "build" / "windows"),
        "--distpath",
        str(dist_windows),
        "--specpath",
        str(spec_dir),
        "--add-binary",
        f"{ffmpeg};.",
        "--add-binary",
        f"{ffprobe};.",
        str(gui_py),
    ]
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(project_dir / ".pyinstaller-cache" / "windows")
    run(command, env=env)

    app_dir = dist_windows / APP_NAME
    exe = app_dir / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"打包结束，但没有找到 {exe}", file=sys.stderr)
        return 1

    run([str(exe), "--self-test"])

    zip_path = project_dir / "dist" / f"{APP_NAME}-windows-x64.zip"
    zip_directory(app_dir, zip_path)
    print(f"已生成：{app_dir}")
    print(f"已生成：{zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
