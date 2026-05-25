#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


MEDIA_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mpg",
    ".mpeg",
    ".ps",
    ".vob",
    ".ts",
}

COPYABLE_VIDEO_CODECS = {"h264", "hevc"}

QUICK_PROBE_ANALYZE_DURATION = "5M"
QUICK_PROBE_SIZE = "5M"
DEEP_PROBE_ANALYZE_DURATION = "100M"
DEEP_PROBE_SIZE = "100M"

DEFAULT_AUDIO_BITRATE = "16k"
DEFAULT_AUDIO_RATE = "16000"
DEFAULT_VIDEO_RATE = "25"
DEFAULT_PROBE_WORKERS = 8
VIDEO_TIMESCALE = 90000


@dataclass(frozen=True)
class ConversionPlan:
    name: str
    reason: str
    mode: str


@dataclass
class MediaInfo:
    path: Path
    probe: dict[str, Any]
    input_skip_bytes: int = 0
    use_unskipped_audio: bool = False
    unskipped_audio: dict[str, Any] | None = None

    @property
    def format_name(self) -> str:
        return str(self.probe.get("format", {}).get("format_name", ""))

    @property
    def format_long_name(self) -> str:
        return str(self.probe.get("format", {}).get("format_long_name", ""))

    @property
    def streams(self) -> list[dict[str, Any]]:
        streams = self.probe.get("streams", [])
        return streams if isinstance(streams, list) else []

    @property
    def video(self) -> dict[str, Any] | None:
        return next((s for s in self.streams if s.get("codec_type") == "video"), None)

    @property
    def audio(self) -> dict[str, Any] | None:
        if self.use_unskipped_audio and self.unskipped_audio:
            return self.unskipped_audio
        return next((s for s in self.streams if s.get("codec_type") == "audio"), None)

    @property
    def is_mpeg_ps(self) -> bool:
        names = {part.strip() for part in self.format_name.split(",")}
        return "mpeg" in names or "MPEG-PS" in self.format_long_name

    @property
    def has_valid_video(self) -> bool:
        video = self.video
        if not video:
            return False
        return int(video.get("width") or 0) > 0 and int(video.get("height") or 0) > 0

    @property
    def stream_start_gap(self) -> float | None:
        video = self.video
        audio = self.audio
        if not video or not audio:
            return None
        try:
            video_start = float(video.get("start_time"))
            audio_start = float(audio.get("start_time"))
        except (TypeError, ValueError):
            return None
        return abs(audio_start - video_start)

    def summary(self) -> str:
        video = self.video or {}
        audio = self.audio or {}
        gap = self.stream_start_gap
        parts = [
            f"container={self.format_name or 'unknown'}",
            f"video={video.get('codec_name', 'none')}",
            f"audio={audio.get('codec_name', 'none')}",
        ]
        if video.get("width") and video.get("height"):
            parts.append(f"size={video.get('width')}x{video.get('height')}")
        if video.get("pix_fmt"):
            parts.append(f"pix_fmt={video.get('pix_fmt')}")
        if gap is not None:
            parts.append(f"av_start_gap={gap:.3f}s")
        if self.input_skip_bytes:
            parts.append(f"skip_initial_bytes={self.input_skip_bytes}")
        if self.use_unskipped_audio:
            parts.append("audio_source=unskipped")
        return ", ".join(parts)


@dataclass
class ProbeResult:
    path: Path
    info: MediaInfo | None = None
    error: Exception | None = None


def require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required tool not found in PATH: {name}")
    return found


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def run_probe(
    ffprobe: str,
    path: Path,
    skip_initial_bytes: int = 0,
    *,
    deep: bool = False,
) -> dict[str, Any]:
    analyzeduration = (
        DEEP_PROBE_ANALYZE_DURATION if deep else QUICK_PROBE_ANALYZE_DURATION
    )
    probesize = DEEP_PROBE_SIZE if deep else QUICK_PROBE_SIZE

    command = [
        ffprobe,
        "-v",
        "error",
        "-analyzeduration",
        analyzeduration,
        "-probesize",
        probesize,
        "-show_format",
        "-show_streams",
        "-of",
        "json",
    ]
    if skip_initial_bytes > 0:
        command.extend(["-skip_initial_bytes", str(skip_initial_bytes)])
    command.append(str(path))

    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe failed"
        raise RuntimeError(detail)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc


def probe_with_fallback(
    ffprobe: str,
    path: Path,
    skip_initial_bytes: int = 0,
) -> dict[str, Any]:
    try:
        return run_probe(
            ffprobe,
            path,
            skip_initial_bytes=skip_initial_bytes,
            deep=False,
        )
    except RuntimeError:
        return run_probe(
            ffprobe,
            path,
            skip_initial_bytes=skip_initial_bytes,
            deep=True,
        )


def audio_is_usable(stream: dict[str, Any] | None) -> bool:
    if not stream or not stream.get("codec_name"):
        return False
    try:
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        return False
    return sample_rate > 0 and channels > 0


def find_mpeg_ps_start_offset(path: Path, max_scan_bytes: int = 4 * 1024 * 1024) -> int:
    pattern = b"\x00\x00\x01\xba"
    chunk_size = 1024 * 1024
    overlap = len(pattern) - 1
    offset = 0
    carry = b""

    with path.open("rb") as file:
        while offset < max_scan_bytes:
            read_size = min(chunk_size, max_scan_bytes - offset)
            data = file.read(read_size)
            if not data:
                break

            block = carry + data
            start = 0
            while True:
                index = block.find(pattern, start)
                if index < 0:
                    break

                absolute = offset - len(carry) + index
                next_byte_index = index + len(pattern)
                if next_byte_index < len(block) and block[next_byte_index] & 0xC0 == 0x40:
                    return absolute
                start = index + 1

            carry = block[-overlap:]
            offset += len(data)

    return 0


def probe_media(ffprobe: str, path: Path) -> MediaInfo:
    base_probe = probe_with_fallback(ffprobe, path)
    base_info = MediaInfo(path=path, probe=base_probe)
    if base_info.has_valid_video:
        return base_info

    skip_bytes = find_mpeg_ps_start_offset(path)
    if skip_bytes <= 0:
        return base_info

    try:
        skipped_probe = probe_with_fallback(
            ffprobe,
            path,
            skip_initial_bytes=skip_bytes,
        )
    except RuntimeError:
        return base_info

    skipped_info = MediaInfo(
        path=path,
        probe=skipped_probe,
        input_skip_bytes=skip_bytes,
    )
    if not skipped_info.has_valid_video:
        return base_info

    base_audio = base_info.audio
    skipped_audio = skipped_info.audio
    if (
        audio_is_usable(base_audio)
        and (
            not audio_is_usable(skipped_audio)
            or base_audio.get("codec_name") == "pcm_alaw"
        )
    ):
        skipped_info.use_unskipped_audio = True
        skipped_info.unskipped_audio = base_audio

    return skipped_info


def probe_one_for_batch(ffprobe: str, path: Path) -> ProbeResult:
    try:
        return ProbeResult(path=path, info=probe_media(ffprobe, path))
    except Exception as exc:  # noqa: BLE001 - caller reports unreadable media.
        return ProbeResult(path=path, error=exc)


def probe_media_files(
    ffprobe: str,
    files: list[Path],
    max_workers: int = DEFAULT_PROBE_WORKERS,
) -> list[ProbeResult]:
    if not files:
        return []

    workers = max(1, min(max_workers, len(files)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(lambda path: probe_one_for_batch(ffprobe, path), files))


def iter_media_files(root: Path, output_dir: Path) -> list[Path]:
    files: list[Path] = []
    script_path = Path(__file__).resolve()

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == script_path:
            continue
        if path.stem.endswith("_macos"):
            continue
        try:
            path.resolve().relative_to(output_dir)
            continue
        except ValueError:
            pass
        if path.suffix.lower() in MEDIA_EXTENSIONS:
            files.append(path)

    return sorted(files)


def output_path_for(input_path: Path, root: Path, output_dir: Path) -> Path:
    relative = input_path.relative_to(root)
    return output_dir / relative.parent / f"{relative.stem}_macos.mp4"


def frame_duration_ticks(video_rate: str) -> int:
    try:
        rate = Fraction(video_rate)
    except ValueError as exc:
        raise RuntimeError(f"Invalid video rate: {video_rate}") from exc

    if rate <= 0:
        raise RuntimeError(f"Video rate must be positive: {video_rate}")

    ticks = Fraction(VIDEO_TIMESCALE, 1) / rate
    if ticks.denominator != 1:
        raise RuntimeError(
            "Video rate must divide the 90000 Hz MP4 video timescale exactly; "
            f"got {video_rate}. Try a rational value like 30000/1001."
        )
    return ticks.numerator


def choose_conversion_plan(info: MediaInfo) -> ConversionPlan | None:
    if not info.is_mpeg_ps:
        return None
    if not info.has_valid_video:
        return None

    video = info.video or {}
    video_codec = str(video.get("codec_name") or "")
    if video_codec not in COPYABLE_VIDEO_CODECS:
        return None

    return ConversionPlan(
        name="copy video + rebuild MP4 timeline",
        reason=(
            "real container is MPEG-PS; H.264/HEVC video can be copied, "
            "while packet timestamps and AAC audio are rebuilt"
        ),
        mode="setts_copy",
    )


def skip_reason(info: MediaInfo) -> str:
    if not info.is_mpeg_ps:
        return "real container is not MPEG-PS"
    if not info.has_valid_video:
        return "ffprobe could not determine a usable video stream"

    video = info.video or {}
    video_codec = str(video.get("codec_name") or "")
    if video_codec not in COPYABLE_VIDEO_CODECS:
        return f"video codec is not H.264/HEVC: {video_codec or 'none'}"
    return "no conversion plan"


def stream_duration(stream: dict[str, Any] | None) -> float | None:
    if not stream:
        return None
    try:
        duration = float(stream.get("duration"))
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def estimate_duration(info: MediaInfo, video_rate: str = DEFAULT_VIDEO_RATE) -> float:
    audio_duration = stream_duration(info.audio)
    if audio_duration:
        return audio_duration

    video = info.video or {}
    try:
        frame_count = int(video.get("nb_frames") or 0)
        rate = float(Fraction(video_rate))
    except (TypeError, ValueError, ZeroDivisionError):
        frame_count = 0
        rate = 0.0
    if frame_count > 0 and rate > 0:
        return frame_count / rate

    for stream in (info.probe.get("format", {}), info.video):
        duration = stream_duration(stream)
        if duration and duration < 24 * 3600:
            return duration

    return 1.0


def add_input_options(
    cmd: list[str],
    source: Path,
    skip_initial_bytes: int = 0,
) -> None:
    cmd.extend(
        [
            "-fflags",
            "+genpts",
            "-analyzeduration",
            QUICK_PROBE_ANALYZE_DURATION,
            "-probesize",
            QUICK_PROBE_SIZE,
        ]
    )
    if skip_initial_bytes > 0:
        cmd.extend(["-skip_initial_bytes", str(skip_initial_bytes)])
    cmd.extend(["-i", str(source)])


def add_inputs(command: list[str], source: Path, info: MediaInfo) -> tuple[str, str]:
    if info.use_unskipped_audio:
        add_input_options(command, source, skip_initial_bytes=0)
        add_input_options(command, source, skip_initial_bytes=info.input_skip_bytes)
        return "1:v:0", "0:a:0?"

    add_input_options(command, source, skip_initial_bytes=info.input_skip_bytes)
    return "0:v:0", "0:a:0?"


def ffmpeg_command_prefix(
    ffmpeg: str,
    overwrite: bool,
    *,
    progress: bool = False,
) -> list[str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if progress:
        cmd.extend(["-nostats", "-progress", "pipe:1"])
    else:
        cmd.append("-stats")
    cmd.append("-y" if overwrite else "-n")
    return cmd


def build_conversion_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: MediaInfo,
    args: argparse.Namespace,
    *,
    overwrite: bool,
    progress: bool = False,
) -> list[str]:
    video = info.video or {}
    audio = info.audio
    video_codec = str(video.get("codec_name") or "")
    audio_codec = str(audio.get("codec_name") or "") if audio else ""

    command = ffmpeg_command_prefix(ffmpeg, overwrite, progress=progress)
    video_map, audio_map = add_inputs(command, source, info)

    command.extend(
        [
            "-map",
            video_map,
            "-map_metadata",
            "-1",
            "-c:v",
            "copy",
        ]
    )

    if args.video_mode == "setts":
        ticks = frame_duration_ticks(str(args.video_rate))
        command.extend(
            [
                "-bsf:v",
                f"setts=ts=N*{ticks}:duration={ticks}:time_base=1/{VIDEO_TIMESCALE}",
            ]
        )

    if video_codec == "h264":
        command.extend(["-tag:v", "avc1"])
    elif video_codec == "hevc":
        command.extend(["-tag:v", "hvc1"])

    if audio:
        command.extend(["-map", audio_map])
        if args.audio_mode == "copy-aac" and audio_codec == "aac":
            command.extend(["-c:a", "copy"])
        else:
            if args.audio_mode == "async":
                audio_filter = "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0"
            else:
                audio_filter = f"aresample={args.audio_rate},asetpts=N/SR/TB"

            command.extend(
                [
                    "-filter:a",
                    audio_filter,
                    "-c:a",
                    "aac",
                    "-b:a",
                    args.audio_bitrate,
                    "-ar",
                    str(args.audio_rate),
                    "-ac",
                    "1",
                ]
            )

    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-video_track_timescale",
            str(VIDEO_TIMESCALE),
        ]
    )
    if not args.no_faststart:
        command.extend(["-movflags", "+faststart"])
    command.extend(
        [
            "-max_muxing_queue_size",
            "4096",
            "-f",
            "mp4",
            str(target),
        ]
    )
    return command


def run_command(command: list[str]) -> bool:
    result = subprocess.run(command)
    return result.returncode == 0


def print_command(command: list[str]) -> None:
    print(" ".join(shlex_quote(part) for part in command))


def remove_partial_output(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def validate_output(ffprobe: str, path: Path) -> str:
    probe = run_probe(ffprobe, path)
    info = MediaInfo(path=path, probe=probe)
    format_name = info.format_name
    video = info.video or {}
    audio = info.audio or {}
    if "mp4" not in format_name and "mov" not in format_name:
        raise RuntimeError(f"output is not MP4/QuickTime: {format_name}")

    return (
        f"container={format_name}, "
        f"video={video.get('codec_name', 'none')} "
        f"{video.get('width', '-')}x{video.get('height', '-')}, "
        f"video_duration={video.get('duration', '-')}, "
        f"video_fps={video.get('avg_frame_rate', '-')}, "
        f"audio={audio.get('codec_name', 'none')}, "
        f"audio_duration={audio.get('duration', '-')}, "
        f"start={probe.get('format', {}).get('start_time', '-')}"
    )


def convert_one(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: MediaInfo,
    plan: ConversionPlan,
    args: argparse.Namespace,
) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = build_conversion_command(
        ffmpeg=ffmpeg,
        source=source,
        target=target,
        info=info,
        args=args,
        overwrite=args.force,
    )

    print(f"\nConverting: {source}")
    print(f"Output:     {target}")
    print(f"Plan:       {plan.name} ({plan.reason})")

    if args.dry_run:
        print("Dry run command:")
        print_command(command)
        return True

    if run_command(command):
        return True

    remove_partial_output(target)
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert broken MPEG-PS camera exports into standard MP4 by copying "
            "H.264/HEVC video, rebuilding video timestamps, and writing AAC audio."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=".",
        help="File or directory to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="converted_macos",
        help="Output directory. Defaults to ./converted_macos.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing converted files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned ffmpeg commands without running conversion.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip ffprobe validation after successful conversion.",
    )
    parser.add_argument(
        "--no-faststart",
        action="store_true",
        help="Do not move MP4 metadata to the beginning of the file.",
    )
    parser.add_argument(
        "--probe-workers",
        type=int,
        default=DEFAULT_PROBE_WORKERS,
        help=f"Number of concurrent ffprobe workers. Default: {DEFAULT_PROBE_WORKERS}.",
    )
    parser.add_argument(
        "--video-mode",
        choices=("setts", "direct"),
        default="setts",
        help=(
            "setts rebuilds video packet timestamps at --video-rate while "
            "copying video; direct preserves source timestamps. Default: setts."
        ),
    )
    parser.add_argument(
        "--video-rate",
        default=DEFAULT_VIDEO_RATE,
        help=f"Frame rate used by --video-mode setts. Default: {DEFAULT_VIDEO_RATE}.",
    )
    parser.add_argument(
        "--audio-mode",
        choices=("rebuild", "async", "copy-aac"),
        default="rebuild",
        help=(
            "Audio handling. rebuild reconstructs audio timestamps from sample "
            "count; async uses ffmpeg async resampling; copy-aac copies AAC and "
            "transcodes non-AAC. Default: rebuild."
        ),
    )
    parser.add_argument(
        "--audio-bitrate",
        default=DEFAULT_AUDIO_BITRATE,
        help=f"AAC audio bitrate. Default: {DEFAULT_AUDIO_BITRATE}.",
    )
    parser.add_argument(
        "--audio-rate",
        default=DEFAULT_AUDIO_RATE,
        help=f"AAC output sample rate. Default: {DEFAULT_AUDIO_RATE}.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    args = parse_args()
    try:
        frame_duration_ticks(str(args.video_rate))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        ffmpeg = require_tool("ffmpeg")
        ffprobe = require_tool("ffprobe")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 2

    if Path(args.output).is_absolute():
        output_dir = Path(args.output).expanduser().resolve()
    else:
        output_dir = (Path.cwd() / args.output).resolve()

    root = input_path if input_path.is_dir() else input_path.parent
    files = iter_media_files(root, output_dir) if input_path.is_dir() else [input_path]
    if not files:
        print("No media files found.")
        return 0

    converted = 0
    skipped = 0
    failed = 0

    print(f"Scanning {len(files)} media file(s) under {root}")
    probe_results = probe_media_files(ffprobe, files, max_workers=args.probe_workers)

    for result in probe_results:
        source = result.path

        if result.error is not None or result.info is None:
            print(f"\nSkipping unreadable file: {source}")
            print(f"Reason: {result.error}")
            skipped += 1
            continue

        info = result.info
        print(f"\nFound: {source}")
        print(f"Probe: {info.summary()}")

        plan = choose_conversion_plan(info)
        if plan is None:
            print(f"Skip: {skip_reason(info)}")
            skipped += 1
            continue

        target = output_path_for(source, root, output_dir)
        if target.exists() and not args.force:
            print(f"Skip: output already exists: {target}")
            skipped += 1
            continue

        if convert_one(ffmpeg, source, target, info, plan, args):
            converted += 1
            if not args.dry_run and not args.no_validate:
                try:
                    print(f"Verified output: {validate_output(ffprobe, target)}")
                except RuntimeError as exc:
                    print(f"Warning: conversion finished but validation failed: {exc}")
        else:
            print("FAILED: ffmpeg exited with a non-zero status.", file=sys.stderr)
            failed += 1

    print(
        f"\nDone. converted={converted}, skipped={skipped}, failed={failed}, "
        f"output_dir={output_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
