#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
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


@dataclass
class MediaInfo:
    path: Path
    probe: dict[str, Any]

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

    @property
    def needs_conversion(self) -> bool:
        if self.path.suffix.lower() == ".mp4" and self.is_mpeg_ps:
            return True
        gap = self.stream_start_gap
        return bool(gap is not None and gap > 1.0)

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
        if gap is not None:
            parts.append(f"av_start_gap={gap:.3f}s")
        return ", ".join(parts)


def require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required tool not found in PATH: {name}")
    return found


def run_probe(ffprobe: str, path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe failed"
        raise RuntimeError(detail)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc


def iter_media_files(root: Path, output_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path == Path(__file__).resolve():
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


def build_ffmpeg_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    crf: int,
    preset: str,
    audio_bitrate: str,
    overwrite: bool,
    has_audio: bool,
) -> list[str]:
    filters = ["[0:v:0]setpts=PTS-STARTPTS,format=yuv420p[v]"]
    maps = ["-map", "[v]"]

    if has_audio:
        filters.append(
            "[0:a:0]asetpts=PTS-STARTPTS,"
            "aresample=async=1:first_pts=0[a]"
        )
        maps.extend(["-map", "[a]"])

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
        "-y" if overwrite else "-n",
        "-fflags",
        "+genpts",
        "-analyzeduration",
        "100M",
        "-probesize",
        "100M",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(filters),
        *maps,
        "-map_metadata",
        "-1",
        "-c:v",
        "libx264",
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

    if has_audio:
        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "1",
                "-shortest",
            ]
        )

    cmd.extend(
        [
            "-movflags",
            "+faststart",
            "-max_muxing_queue_size",
            "4096",
            str(target),
        ]
    )
    return cmd


def convert_one(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: MediaInfo,
    args: argparse.Namespace,
) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_command(
        ffmpeg=ffmpeg,
        source=source,
        target=target,
        crf=args.crf,
        preset=args.preset,
        audio_bitrate=args.audio_bitrate,
        overwrite=args.force,
        has_audio=info.audio is not None,
    )

    print(f"\nConverting: {source}")
    print(f"Output:     {target}")
    if args.dry_run:
        print("Dry run command:")
        print(" ".join(shlex_quote(part) for part in cmd))
        return True

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"FAILED: ffmpeg exited with code {result.returncode}", file=sys.stderr)
        return False
    return True


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


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
        description=(
            "Convert mislabeled MPEG-PS camera exports into macOS-compatible MP4."
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
        help="Print planned conversions without running ffmpeg.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=23,
        help="H.264 quality. Lower is larger/better. Default: 23.",
    )
    parser.add_argument(
        "--preset",
        default="veryfast",
        help="libx264 preset. Default: veryfast.",
    )
    parser.add_argument(
        "--audio-bitrate",
        default="96k",
        help="AAC audio bitrate. Default: 96k.",
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
    for source in files:
        try:
            info = MediaInfo(path=source, probe=run_probe(ffprobe, source))
        except RuntimeError as exc:
            print(f"\nSkipping unreadable file: {source}")
            print(f"Reason: {exc}")
            skipped += 1
            continue

        print(f"\nFound: {source}")
        print(f"Probe: {info.summary()}")

        if not info.needs_conversion:
            print("Skip: already looks like a normal media container for this script.")
            skipped += 1
            continue

        if not info.has_valid_video:
            print(
                "Skip: ffprobe could not determine a usable video size; "
                "the file is probably damaged or exported in an unsupported variant."
            )
            skipped += 1
            continue

        target = output_path_for(source, root, output_dir)
        if target.exists() and not args.force:
            print(f"Skip: output already exists: {target}")
            skipped += 1
            continue

        if convert_one(ffmpeg, source, target, info, args):
            converted += 1
            if not args.dry_run:
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
