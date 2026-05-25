#!/usr/bin/env python3
from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from types import SimpleNamespace
from typing import Any

import main as converter


AUDIO_BITRATE_OPTIONS = ("16k", "24k", "32k", "48k", "64k", "96k", "128k")
AUDIO_RATE_OPTIONS = ("8000", "16000", "32000", "44100", "48000")
VIDEO_RATE_OPTIONS = ("25", "30", "30000/1001", "50", "60")
AUDIO_MODE_LABELS = {
    "rebuild": "重建音频时间轴",
    "async": "异步补偿",
    "copy-aac": "AAC 直接复制",
}
VIDEO_MODE = "setts"


class CancelledConversion(Exception):
    pass


def bundled_base_dir() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def find_tool(name: str) -> str:
    base = bundled_base_dir()
    candidates = [
        base / name,
        Path(sys.executable).resolve().parent / name,
        Path(sys.executable).resolve().parent.parent / "Resources" / name,
        Path(sys.executable).resolve().parent.parent / "Frameworks" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"找不到 {name}。请使用打包版，或先安装 FFmpeg。")
    return found


def parse_ffmpeg_time(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def parse_progress_seconds(key: str, value: str) -> float | None:
    if key == "out_time":
        return parse_ffmpeg_time(value)
    if key in {"out_time_ms", "out_time_us"}:
        try:
            microseconds = int(value.strip())
        except (ValueError, AttributeError):
            return None
        return microseconds / 1_000_000
    return None


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def collect_media_files(input_path: Path, output_dir: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]

    files: list[Path] = []
    for path in sorted(input_path.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in converter.MEDIA_EXTENSIONS:
            continue
        if path.stem.endswith("_macos"):
            continue
        if output_dir.resolve() != input_path.resolve() and is_relative_to(path, output_dir):
            continue
        files.append(path)
    return files


def output_path_for(source: Path, input_path: Path, output_dir: Path) -> Path:
    if input_path.is_file():
        return output_dir / f"{source.stem}_macos.mp4"
    relative = source.relative_to(input_path)
    return output_dir / relative.parent / f"{relative.stem}_macos.mp4"


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def audio_mode_label(value: str) -> str:
    return AUDIO_MODE_LABELS.get(value, value)


def audio_mode_value(label: str) -> str:
    for value, display in AUDIO_MODE_LABELS.items():
        if label == display or label == value:
            return value
    return label


class ConverterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("医务科监控视频转换器")
        self.root.geometry("820x560")
        self.root.minsize(760, 520)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=False)
        self.video_rate_var = tk.StringVar(value=converter.DEFAULT_VIDEO_RATE)
        self.audio_bitrate_var = tk.StringVar(value=converter.DEFAULT_AUDIO_BITRATE)
        self.audio_rate_var = tk.StringVar(value=converter.DEFAULT_AUDIO_RATE)
        self.audio_mode_var = tk.StringVar(value=audio_mode_label("rebuild"))
        self.status_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_process: subprocess.Popen[str] | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_messages)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)

        form = ttk.Frame(self.root, padding=(16, 16, 16, 8))
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="输入").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(form, textvariable=self.input_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(form, text="选择文件", command=self._choose_input_file).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(form, text="选择文件夹", command=self._choose_input_dir).grid(
            row=0, column=3, padx=(8, 0)
        )

        ttk.Label(form, text="输出").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0)
        )
        ttk.Entry(form, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", pady=(10, 0)
        )
        ttk.Button(form, text="选择位置", command=self._choose_output_dir).grid(
            row=1, column=2, columnspan=2, sticky="ew", padx=(8, 0), pady=(10, 0)
        )

        options = ttk.Frame(self.root, padding=(16, 0, 16, 8))
        options.grid(row=1, column=0, sticky="ew")
        ttk.Checkbutton(
            options,
            text="覆盖已有输出",
            variable=self.overwrite_var,
        ).pack(side="left")

        params = ttk.LabelFrame(self.root, text="转换参数", padding=(12, 8, 12, 10))
        params.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        for column in (1, 3):
            params.columnconfigure(column, weight=1)

        ttk.Label(params, text="视频帧率").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            params,
            textvariable=self.video_rate_var,
            values=VIDEO_RATE_OPTIONS,
            width=14,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 16))

        ttk.Label(params, text="音频处理").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            params,
            textvariable=self.audio_mode_var,
            values=tuple(AUDIO_MODE_LABELS.values()),
            state="readonly",
            width=18,
        ).grid(row=0, column=3, sticky="ew", padx=(8, 0))

        ttk.Label(params, text="音频码率").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            params,
            textvariable=self.audio_bitrate_var,
            values=AUDIO_BITRATE_OPTIONS,
            width=14,
        ).grid(row=1, column=1, sticky="ew", padx=(8, 16), pady=(10, 0))

        ttk.Label(params, text="采样率").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Combobox(
            params,
            textvariable=self.audio_rate_var,
            values=AUDIO_RATE_OPTIONS,
            width=14,
        ).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(10, 0))

        controls = ttk.Frame(self.root, padding=(16, 0, 16, 8))
        controls.grid(row=3, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            controls,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.start_button = ttk.Button(controls, text="转换", command=self._start)
        self.start_button.grid(row=0, column=1)
        self.cancel_button = ttk.Button(
            controls,
            text="取消",
            command=self._cancel,
            state="disabled",
        )
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))

        log_frame = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(log_frame, height=12, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

        status = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        status.grid(row=5, column=0, sticky="ew")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")

    def _choose_input_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[
                ("视频文件", "*.mp4 *.m4v *.mov *.mpg *.mpeg *.ps *.vob *.ts"),
                ("所有文件", "*"),
            ],
        )
        if path:
            self._set_input(Path(path))

    def _choose_input_dir(self) -> None:
        path = filedialog.askdirectory(title="选择视频文件夹")
        if path:
            self._set_input(Path(path))

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出位置")
        if path:
            self.output_var.set(str(Path(path)))

    def _set_input(self, path: Path) -> None:
        self.input_var.set(str(path))
        self.output_var.set(str(path.parent if path.is_file() else path))

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        input_text = self.input_var.get().strip()
        output_text = self.output_var.get().strip()
        if not input_text:
            messagebox.showerror("缺少输入", "请选择输入文件或文件夹。")
            return

        input_path = Path(input_text).expanduser().resolve()
        if not input_path.exists():
            messagebox.showerror("输入不存在", str(input_path))
            return

        output_dir = (
            Path(output_text).expanduser().resolve()
            if output_text
            else input_path.parent if input_path.is_file() else input_path
        )
        try:
            settings = self._read_settings()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.cancel_event.clear()
        self.progress_var.set(0)
        self._clear_log()
        self._set_running(True)
        self.worker = threading.Thread(
            target=self._worker,
            args=(input_path, output_dir, self.overwrite_var.get(), settings),
            daemon=True,
        )
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("正在取消...")
        process = self.current_process
        if process is not None:
            terminate_process(process)

    def _worker(
        self,
        input_path: Path,
        output_dir: Path,
        overwrite: bool,
        settings: SimpleNamespace,
    ) -> None:
        try:
            ffmpeg = find_tool("ffmpeg")
            ffprobe = find_tool("ffprobe")
            output_dir.mkdir(parents=True, exist_ok=True)

            files = collect_media_files(input_path, output_dir)
            self._put_log(f"扫描到 {len(files)} 个媒体文件。")
            self._put_log("正在分析媒体信息...")

            candidates: list[tuple[Path, Path, converter.MediaInfo, converter.ConversionPlan, float]] = []
            for result in converter.probe_media_files(ffprobe, files):
                self._check_cancelled()
                source = result.path
                if result.error is not None or result.info is None:
                    self._put_log(f"跳过：{source}\n原因：{result.error}")
                    continue

                info = result.info
                self._put_log(f"识别：{source}\n{info.summary()}")
                plan = converter.choose_conversion_plan(info)
                if plan is None:
                    self._put_log(f"跳过：{converter.skip_reason(info)}")
                    continue

                target = output_path_for(source, input_path, output_dir)
                if target.exists() and not overwrite:
                    self._put_log(f"跳过：输出已存在：{target}")
                    continue

                duration = converter.estimate_duration(info, settings.video_rate)
                candidates.append((source, target, info, plan, duration))

            if not candidates:
                self.messages.put(("done", "没有需要转换的文件。"))
                return

            total = len(candidates)
            for index, (source, target, info, plan, duration) in enumerate(candidates):
                self._check_cancelled()
                self._convert_one(
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    source=source,
                    target=target,
                    info=info,
                    plan=plan,
                    duration=duration,
                    file_index=index,
                    total=total,
                    overwrite=overwrite,
                    settings=settings,
                )

            self.messages.put(("progress", 100.0, "完成"))
            self.messages.put(("done", f"完成：已转换 {total} 个文件。"))
        except CancelledConversion:
            self.messages.put(("done", "已取消。"))
        except Exception as exc:  # noqa: BLE001 - GUI must surface unexpected errors.
            self.messages.put(("error", str(exc)))
        finally:
            self.current_process = None

    def _convert_one(
        self,
        ffmpeg: str,
        ffprobe: str,
        source: Path,
        target: Path,
        info: converter.MediaInfo,
        plan: converter.ConversionPlan,
        duration: float,
        file_index: int,
        total: int,
        overwrite: bool,
        settings: SimpleNamespace,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._put_log(
            f"开始转换：{source}\n输出：{target}\n方案：{plan.name}（{plan.reason}）"
        )

        cmd = converter.build_conversion_command(
            ffmpeg=ffmpeg,
            source=source,
            target=target,
            info=info,
            args=self._conversion_args(settings),
            overwrite=overwrite,
            progress=True,
        )
        return_code = self._run_ffmpeg_process(
            cmd=cmd,
            duration=duration,
            file_index=file_index,
            total=total,
            target=target,
        )
        if return_code == 0:
            verify = converter.validate_output(ffprobe, target)
            self._put_log(f"已验证：{verify}")
            return

        if self.cancel_event.is_set():
            raise CancelledConversion()

        converter.remove_partial_output(target)
        raise RuntimeError(f"ffmpeg 转换失败，退出码：{return_code}")

    def _read_settings(self) -> SimpleNamespace:
        video_rate = self.video_rate_var.get().strip()
        if not video_rate:
            raise ValueError("视频帧率不能为空，例如 25。")
        try:
            converter.frame_duration_ticks(video_rate)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

        audio_mode = audio_mode_value(self.audio_mode_var.get().strip())
        if audio_mode not in AUDIO_MODE_LABELS:
            raise ValueError("音频处理方式无效。")

        audio_bitrate = self.audio_bitrate_var.get().strip()
        if not audio_bitrate:
            raise ValueError("音频码率不能为空，例如 16k。")

        audio_rate = self.audio_rate_var.get().strip()
        try:
            audio_rate_number = int(audio_rate)
        except ValueError as exc:
            raise ValueError("采样率必须是整数。") from exc
        if audio_rate_number <= 0:
            raise ValueError("采样率必须大于 0。")

        return SimpleNamespace(
            video_rate=video_rate,
            audio_mode=audio_mode,
            audio_bitrate=audio_bitrate,
            audio_rate=str(audio_rate_number),
        )

    def _conversion_args(self, settings: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            video_mode=VIDEO_MODE,
            video_rate=settings.video_rate,
            audio_mode=settings.audio_mode,
            audio_bitrate=settings.audio_bitrate,
            audio_rate=settings.audio_rate,
            no_faststart=False,
        )

    def _run_ffmpeg_process(
        self,
        cmd: list[str],
        duration: float,
        file_index: int,
        total: int,
        target: Path,
    ) -> int:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.current_process = process
        assert process.stdout is not None

        for raw_line in process.stdout:
            self._check_cancelled(process)
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                self._put_log(line)
                continue
            key, value = line.split("=", 1)
            if key in {"out_time", "out_time_ms", "out_time_us"}:
                seconds = parse_progress_seconds(key, value)
                if seconds is not None:
                    file_percent = min(max(seconds / max(duration, 1.0), 0.0), 1.0)
                    overall = ((file_index + file_percent) / total) * 100
                    label = f"{file_index + 1}/{total}  {target.name}"
                    self.messages.put(("progress", overall, label))
            elif key == "progress" and value == "end":
                overall = ((file_index + 1) / total) * 100
                self.messages.put(("progress", overall, target.name))

        return_code = process.wait()
        self.current_process = None
        return return_code

    def _check_cancelled(self, process: subprocess.Popen[str] | None = None) -> None:
        if not self.cancel_event.is_set():
            return
        if process is not None:
            terminate_process(process)
        raise CancelledConversion()

    def _put_log(self, text: str) -> None:
        self.messages.put(("log", text))

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, *payload = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(payload[0])
                elif kind == "progress":
                    percent, label = payload
                    self.progress_var.set(float(percent))
                    self.status_var.set(str(label))
                elif kind == "done":
                    self._append_log(payload[0])
                    self.status_var.set(payload[0])
                    self._set_running(False)
                elif kind == "error":
                    self._append_log(f"错误：{payload[0]}")
                    self.status_var.set("失败")
                    self._set_running(False)
                    messagebox.showerror("转换失败", str(payload[0]))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_messages)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("正在转换", "是否取消转换并退出？"):
                return
            self.cancel_event.set()
            process = self.current_process
            if process is not None:
                terminate_process(process)
            time.sleep(0.2)
        self.root.destroy()


def self_test() -> int:
    try:
        for name in ("ffmpeg", "ffprobe"):
            path = find_tool(name)
            result = subprocess.run(
                [path, "-version"],
                text=True,
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                print(f"{name}: failed", file=sys.stderr)
                return 1
            first_line = result.stdout.splitlines()[0] if result.stdout else path
            print(f"{name}: {path}")
            print(first_line)
        return 0
    except Exception as exc:  # noqa: BLE001 - command-line smoke test.
        print(str(exc), file=sys.stderr)
        return 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
