#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
import os
from pathlib import Path


APP_NAME = "医务科监控修复助手"
VENDOR_FFMPEG_DIR = Path("vendor") / "ffmpeg-macos-arm64"


def require_binary(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"找不到 {name}，请先安装 FFmpeg。")
    return Path(found).resolve()


def bundled_binary(project_dir: Path, name: str) -> Path | None:
    candidate = project_dir / VENDOR_FFMPEG_DIR / name
    if candidate.exists():
        return candidate.resolve()
    return None


def resolve_ffmpeg_binary(project_dir: Path, name: str) -> Path:
    bundled = bundled_binary(project_dir, name)
    if bundled is not None:
        return bundled
    return require_binary(name)


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    print(" ".join(command))
    subprocess.run(command, check=True, env=env)


def main() -> int:
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
        print(f"使用兼容版 FFmpeg：{ffmpeg.parent}")
    else:
        print(
            "警告：未找到 vendor/ffmpeg-macos-arm64，"
            "将使用本机 PATH 中的 FFmpeg；发给旧 macOS 可能不兼容。"
        )

    pyinstaller_check = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        text=True,
        capture_output=True,
    )
    if pyinstaller_check.returncode != 0:
        print("找不到 PyInstaller。请先运行：python3.12 -m pip install pyinstaller")
        return 2

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
        str(project_dir / "build"),
        "--distpath",
        str(project_dir / "dist"),
        "--specpath",
        str(project_dir),
        "--add-binary",
        f"{ffmpeg}:.",
        "--add-binary",
        f"{ffprobe}:.",
        str(gui_py),
    ]
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(project_dir / ".pyinstaller-cache")
    run(command, env=env)

    app_path = project_dir / "dist" / f"{APP_NAME}.app"
    if not app_path.exists():
        print(f"打包结束，但没有找到 {app_path}", file=sys.stderr)
        return 1

    codesign = shutil.which("codesign")
    if codesign:
        run([codesign, "--force", "--deep", "--sign", "-", str(app_path)])

    exe = app_path / "Contents" / "MacOS" / APP_NAME
    if exe.exists():
        run([str(exe), "--self-test"])

    ditto = shutil.which("ditto")
    if ditto:
        zip_path = project_dir / "dist" / f"{APP_NAME}-macos.zip"
        if zip_path.exists():
            zip_path.unlink()
        run([ditto, "-c", "-k", "--keepParent", str(app_path), str(zip_path)])
        print(f"已生成：{zip_path}")

    print(f"已生成：{app_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
