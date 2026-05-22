#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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

OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080

QUICK_PROBE_ANALYZE_DURATION = "5M"
QUICK_PROBE_SIZE = "5M"
DEEP_PROBE_ANALYZE_DURATION = "100M"
DEEP_PROBE_SIZE = "100M"

HARDWARE_VIDEO_ENCODER = "h264_videotoolbox"
SOFTWARE_VIDEO_ENCODER = "libx264"

DEFAULT_AUDIO_BITRATE = "16k"
DEFAULT_HARDWARE_QUALITY = 40
DEFAULT_PROBE_WORKERS = 8


@dataclass(frozen=True)
class ConversionPlan:
    name: str
    reason: str
    mode: str  # "copy", "copy_video_transcode_audio", "full_transcode"


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

    # Some camera exports have junk bytes before the MPEG-PS pack header.
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

    # In a few broken files, video is only readable after skipping junk bytes,
    # while audio is only readable from the original input.
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


def is_video_copy_compatible(info: MediaInfo) -> bool:
    video = info.video or {}
    return (
        video.get("codec_name") == "h264"
        and int(video.get("width") or 0) == OUTPUT_WIDTH
        and int(video.get("height") or 0) == OUTPUT_HEIGHT
        and video.get("pix_fmt") in {None, "yuv420p"}
    )


def is_audio_copy_compatible(info: MediaInfo) -> bool:
    audio = info.audio
    if audio is None:
        return True
    return audio.get("codec_name") == "aac"


def has_safe_timestamps_for_copy(info: MediaInfo) -> bool:
    gap = info.stream_start_gap
    return (
        info.input_skip_bytes <= 0
        and not info.use_unskipped_audio
        and (gap is None or gap <= 1.0)
    )


def is_already_macos_mp4(info: MediaInfo) -> bool:
    return (
        info.path.suffix.lower() == ".mp4"
        and not info.is_mpeg_ps
        and is_video_copy_compatible(info)
        and is_audio_copy_compatible(info)
        and has_safe_timestamps_for_copy(info)
    )


def choose_conversion_plan(info: MediaInfo) -> ConversionPlan | None:
    if not info.has_valid_video:
        return None

    if is_already_macos_mp4(info):
        return None

    if has_safe_timestamps_for_copy(info) and is_video_copy_compatible(info):
        if is_audio_copy_compatible(info):
            return ConversionPlan(
                name="remux/copy",
                reason="video and audio are already MP4-compatible; only remuxing",
                mode="copy",
            )

        return ConversionPlan(
            name="copy video + transcode audio",
            reason="video is already H.264 1080p; only audio needs AAC conversion",
            mode="copy_video_transcode_audio",
        )

    return ConversionPlan(
        name="full transcode",
        reason="video size/codec/pixel format/timestamps require filtering or re-encoding",
        mode="full_transcode",
    )


def h264_videotoolbox_available(ffmpeg: str) -> bool:
    with tempfile.TemporaryDirectory() as temp_dir:
        output = Path(temp_dir) / "videotoolbox_test.mp4"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=25",
            "-t",
            "0.2",
            "-c:v",
            HARDWARE_VIDEO_ENCODER,
            "-q:v",
            str(DEFAULT_HARDWARE_QUALITY),
            "-profile:v",
            "high",
            "-level:v",
            "5.1",
            "-tag:v",
            "avc1",
            "-an",
            str(output),
        ]

        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

        return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def describe_video_encoder(video_encoder: str) -> str:
    if video_encoder == HARDWARE_VIDEO_ENCODER:
        return HARDWARE_VIDEO_ENCODER
    if video_encoder == SOFTWARE_VIDEO_ENCODER:
        return SOFTWARE_VIDEO_ENCODER
    return video_encoder


def encoder_attempts(video_encoder: str, hardware_available: bool) -> list[str]:
    if video_encoder == SOFTWARE_VIDEO_ENCODER:
        return [SOFTWARE_VIDEO_ENCODER]
    if video_encoder == HARDWARE_VIDEO_ENCODER:
        return [HARDWARE_VIDEO_ENCODER, SOFTWARE_VIDEO_ENCODER]
    if hardware_available:
        return [HARDWARE_VIDEO_ENCODER, SOFTWARE_VIDEO_ENCODER]
    return [SOFTWARE_VIDEO_ENCODER]


def video_encoder_options(
    video_encoder: str,
    crf: int,
    preset: str,
    hardware_quality: int,
    video_bitrate: str | None,
) -> list[str]:
    if video_encoder == HARDWARE_VIDEO_ENCODER:
        options = [
            "-c:v",
            HARDWARE_VIDEO_ENCODER,
        ]
        if video_bitrate:
            options.extend(["-b:v", video_bitrate])
        else:
            options.extend(["-q:v", str(hardware_quality)])

        options.extend(
            [
                "-profile:v",
                "high",
                "-level:v",
                "5.1",
                "-tag:v",
                "avc1",
            ]
        )
        return options

    return [
        "-c:v",
        SOFTWARE_VIDEO_ENCODER,
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-profile:v",
        "high",
        "-level:v",
        "5.1",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "avc1",
    ]


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


def add_common_output_options(cmd: list[str], target: Path, faststart: bool) -> None:
    if faststart:
        cmd.extend(["-movflags", "+faststart"])
    cmd.extend(["-max_muxing_queue_size", "4096", str(target)])


def ffmpeg_command_prefix(ffmpeg: str, overwrite: bool, *, progress: bool = False) -> list[str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "fatal",
    ]
    if progress:
        cmd.extend(["-nostats", "-progress", "pipe:1"])
    else:
        cmd.append("-stats")
    cmd.append("-y" if overwrite else "-n")
    return cmd


def build_copy_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: MediaInfo,
    args: argparse.Namespace,
    *,
    transcode_audio: bool,
    overwrite: bool,
    progress: bool = False,
) -> list[str]:
    cmd = ffmpeg_command_prefix(ffmpeg, overwrite, progress=progress)

    add_input_options(cmd, source)

    cmd.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-c:v",
            "copy",
        ]
    )

    if info.audio is not None:
        if transcode_audio:
            cmd.extend(
                [
                    "-af",
                    "aresample=async=1:first_pts=0",
                    "-c:a",
                    "aac",
                    "-b:a",
                    args.audio_bitrate,
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                ]
            )
        else:
            cmd.extend(["-c:a", "copy"])

    add_common_output_options(cmd, target, faststart=not args.no_faststart)
    return cmd


def build_full_transcode_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: MediaInfo,
    args: argparse.Namespace,
    *,
    video_encoder: str,
    overwrite: bool,
    progress: bool = False,
) -> list[str]:
    has_audio = info.audio is not None
    video_input = "1:v:0" if info.use_unskipped_audio else "0:v:0"
    audio_input = "0:a:0"

    video_filter = (
        f"[{video_input}]setpts=PTS-STARTPTS,"
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=yuv420p[v]"
    )

    filters = [video_filter]
    maps = ["-map", "[v]"]

    if has_audio:
        filters.append(
            f"[{audio_input}]asetpts=PTS-STARTPTS,"
            "aresample=async=1:first_pts=0[a]"
        )
        maps.extend(["-map", "[a]"])

    cmd = ffmpeg_command_prefix(ffmpeg, overwrite, progress=progress)

    if info.use_unskipped_audio:
        add_input_options(cmd, source, skip_initial_bytes=0)
        add_input_options(cmd, source, skip_initial_bytes=info.input_skip_bytes)
    else:
        add_input_options(cmd, source, skip_initial_bytes=info.input_skip_bytes)

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            *maps,
            "-map_metadata",
            "-1",
            *video_encoder_options(
                video_encoder=video_encoder,
                crf=args.crf,
                preset=args.preset,
                hardware_quality=args.hardware_quality,
                video_bitrate=args.video_bitrate,
            ),
        ]
    )

    if has_audio:
        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                args.audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "1",
                "-shortest",
            ]
        )

    add_common_output_options(cmd, target, faststart=not args.no_faststart)
    return cmd


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
        # If the file cannot be removed, ffmpeg with -y may still overwrite it.
        pass


def convert_one(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: MediaInfo,
    plan: ConversionPlan,
    args: argparse.Namespace,
) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    encoders = encoder_attempts(args.video_encoder, args.hardware_encoder_available)

    print(f"\nConverting: {source}")
    print(f"Output:     {target}")
    print(f"Plan:       {plan.name} ({plan.reason})")

    if plan.mode in {"copy", "copy_video_transcode_audio"}:
        transcode_audio = plan.mode == "copy_video_transcode_audio"
        copy_cmd = build_copy_command(
            ffmpeg=ffmpeg,
            source=source,
            target=target,
            info=info,
            args=args,
            transcode_audio=transcode_audio,
            overwrite=args.force,
        )

        if args.dry_run:
            print("Dry run command:")
            print_command(copy_cmd)
            return True

        if run_command(copy_cmd):
            return True

        print(
            "Copy/remux path failed; retrying with full transcode.",
            file=sys.stderr,
        )
        remove_partial_output(target)

    if args.dry_run:
        for index, video_encoder in enumerate(encoders):
            cmd = build_full_transcode_command(
                ffmpeg=ffmpeg,
                source=source,
                target=target,
                info=info,
                args=args,
                video_encoder=video_encoder,
                overwrite=args.force or index > 0,
            )
            print(f"Dry run command ({describe_video_encoder(video_encoder)}):")
            print_command(cmd)
        return True

    for index, video_encoder in enumerate(encoders):
        print(f"Encoder: {describe_video_encoder(video_encoder)}")
        cmd = build_full_transcode_command(
            ffmpeg=ffmpeg,
            source=source,
            target=target,
            info=info,
            args=args,
            video_encoder=video_encoder,
            overwrite=args.force or index > 0 or plan.mode != "full_transcode",
        )

        if run_command(cmd):
            return True

        print(
            f"FAILED with {describe_video_encoder(video_encoder)}: "
            "ffmpeg exited with a non-zero status.",
            file=sys.stderr,
        )
        remove_partial_output(target)

        if index < len(encoders) - 1:
            print("Retrying with libx264 CPU fallback.")

    return False


def validate_output(ffprobe: str, path: Path) -> str:
    probe = run_probe(ffprobe, path)
    info = MediaInfo(path=path, probe=probe)
    video = info.video or {}
    audio = info.audio or {}
    video_start = video.get("start_time", "unknown")
    audio_start = audio.get("start_time", "none")
    return (
        f"{info.summary()}, "
        f"video_start={video_start}, audio_start={audio_start}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert camera/video exports into macOS-compatible MP4 files."
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
        help=(
            "Number of concurrent ffprobe workers. "
            f"Default: {DEFAULT_PROBE_WORKERS}. "
            "Use 2-4 for external drives; 4-8 for fast internal SSDs."
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=23,
        help="libx264 quality. Lower is larger/better. Default: 23.",
    )
    parser.add_argument(
        "--preset",
        default="veryfast",
        help="libx264 preset. Default: veryfast.",
    )
    parser.add_argument(
        "--video-encoder",
        choices=(HARDWARE_VIDEO_ENCODER, SOFTWARE_VIDEO_ENCODER),
        default=HARDWARE_VIDEO_ENCODER,
        help=(
            f"Video encoder for full transcode. Default: {HARDWARE_VIDEO_ENCODER}. "
            "h264_videotoolbox falls back to libx264 if hardware encoding fails."
        ),
    )
    parser.add_argument(
        "--hardware-quality",
        type=int,
        default=DEFAULT_HARDWARE_QUALITY,
        help=(
            "h264_videotoolbox quality, 0-100, higher is better. "
            f"Default: {DEFAULT_HARDWARE_QUALITY}. "
            "Ignored when --video-bitrate is set."
        ),
    )
    parser.add_argument(
        "--video-bitrate",
        default=None,
        help=(
            "Use bitrate mode for h264_videotoolbox, e.g. 5000k. "
            "If omitted, h264_videotoolbox uses --hardware-quality."
        ),
    )
    parser.add_argument(
        "--audio-bitrate",
        default=DEFAULT_AUDIO_BITRATE,
        help=f"AAC audio bitrate. Default: {DEFAULT_AUDIO_BITRATE}.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    args = parse_args()

    try:
        ffmpeg = require_tool("ffmpeg")
        ffprobe = require_tool("ffprobe")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    args.hardware_encoder_available = False
    if args.video_encoder == HARDWARE_VIDEO_ENCODER:
        args.hardware_encoder_available = h264_videotoolbox_available(ffmpeg)
        if args.hardware_encoder_available:
            print("Hardware encoder available: h264_videotoolbox")
        else:
            print(
                "Warning: h264_videotoolbox preflight failed; "
                "conversion will try it and then fall back to libx264 if needed."
            )
            args.hardware_encoder_available = True
    else:
        print("Using libx264.")

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

        if not info.has_valid_video:
            print(
                "Skip: ffprobe could not determine a usable video size; "
                "the file is probably damaged or exported in an unsupported variant."
            )
            skipped += 1
            continue

        plan = choose_conversion_plan(info)
        if plan is None:
            print("Skip: already looks like a macOS-compatible MP4 for this script.")
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
            failed += 1

    print(
        f"\nDone. converted={converted}, skipped={skipped}, failed={failed}, "
        f"output_dir={output_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
