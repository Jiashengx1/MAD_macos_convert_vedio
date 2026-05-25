#!/usr/bin/env python3
from __future__ import annotations

import argparse
from fractions import Fraction
import subprocess
import sys
from pathlib import Path

import main as converter


COPYABLE_VIDEO_CODECS = {"h264", "hevc"}
VIDEO_TIMESCALE = 90000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test MPEG-PS to MP4 remux with video stream copy and repaired "
            "AAC audio."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="data",
        help="File or directory to test. Defaults to ./data.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="copy_mp4_test_output",
        help="Output directory. Defaults to ./copy_mp4_test_output.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ffmpeg commands without running them.",
    )
    parser.add_argument(
        "--audio-bitrate",
        default=converter.DEFAULT_AUDIO_BITRATE,
        help=f"AAC audio bitrate when transcoding audio. Default: {converter.DEFAULT_AUDIO_BITRATE}.",
    )
    parser.add_argument(
        "--audio-rate",
        default="16000",
        help="AAC output sample rate when transcoding audio. Default: 16000.",
    )
    parser.add_argument(
        "--audio-mode",
        choices=("rebuild", "async", "copy-aac"),
        default="rebuild",
        help=(
            "Audio handling. rebuild: decode audio and rebuild timestamps from "
            "sample count. async: old async resample timestamp repair. copy-aac: "
            "copy AAC audio and transcode only non-AAC. Default: rebuild."
        ),
    )
    parser.add_argument(
        "--video-mode",
        choices=("setts", "direct"),
        default="setts",
        help=(
            "Video handling. setts: copy video while forcing packet timestamps "
            "to a fixed frame rate. direct: copy video timestamps as-is. "
            "Default: setts."
        ),
    )
    parser.add_argument(
        "--video-rate",
        default="25",
        help="Frame rate used by --video-mode setts. Default: 25.",
    )
    parser.add_argument(
        "--decode-check",
        action="store_true",
        help="After conversion, fully decode output with ffmpeg and report warnings.",
    )
    return parser.parse_args()


def output_path_for(source: Path, root: Path, output_dir: Path) -> Path:
    relative = source.relative_to(root)
    return output_dir / relative.parent / f"{relative.stem}_copy_mp4.mp4"


def frame_duration_ticks(video_rate: str) -> int:
    try:
        rate = Fraction(video_rate)
    except ValueError as exc:
        raise RuntimeError(f"Invalid --video-rate: {video_rate}") from exc

    if rate <= 0:
        raise RuntimeError(f"--video-rate must be positive: {video_rate}")

    ticks = Fraction(VIDEO_TIMESCALE, 1) / rate
    if ticks.denominator != 1:
        raise RuntimeError(
            "--video-rate must divide the 90000 Hz MP4 video timescale exactly; "
            f"got {video_rate}. Try a rational value like 30000/1001."
        )
    return ticks.numerator


def iter_input_files(input_path: Path, output_dir: Path) -> tuple[Path, list[Path]]:
    if input_path.is_file():
        return input_path.parent, [input_path]
    return input_path, converter.iter_media_files(input_path, output_dir)


def add_inputs(command: list[str], source: Path, info: converter.MediaInfo) -> tuple[str, str]:
    if info.use_unskipped_audio:
        converter.add_input_options(command, source, skip_initial_bytes=0)
        converter.add_input_options(command, source, skip_initial_bytes=info.input_skip_bytes)
        return "1:v:0", "0:a:0?"

    converter.add_input_options(
        command,
        source,
        skip_initial_bytes=info.input_skip_bytes,
    )
    return "0:v:0", "0:a:0?"


def build_copy_mp4_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: converter.MediaInfo,
    args: argparse.Namespace,
) -> list[str]:
    video = info.video or {}
    audio = info.audio
    video_codec = str(video.get("codec_name") or "")
    audio_codec = str(audio.get("codec_name") or "") if audio else ""

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y" if args.force else "-n",
    ]
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
            "-movflags",
            "+faststart",
            "-max_muxing_queue_size",
            "4096",
            "-f",
            "mp4",
            str(target),
        ]
    )
    return command


def run_and_collect(command: list[str]) -> tuple[int, list[str]]:
    result = subprocess.run(command, text=True, capture_output=True)
    return result.returncode, process_output_lines(result)


def validate_output(ffprobe: str, target: Path) -> str:
    probe = converter.run_probe(ffprobe, target)
    info = converter.MediaInfo(path=target, probe=probe)
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


def decode_check(ffmpeg: str, target: Path) -> tuple[int, list[str], int]:
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "warning",
            "-i",
            str(target),
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
    )
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    return result.returncode, lines[:12], len(lines)


def process_output_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return [line for line in output.splitlines() if line.strip()]


def print_output_summary(label: str, lines: list[str], max_lines: int = 12) -> None:
    if not lines:
        return
    print(f"{label}: {len(lines)} line(s)")
    for line in lines[:max_lines]:
        print(f"  {line}")


def print_command(command: list[str]) -> None:
    converter.print_command(command)


def main() -> int:
    args = parse_args()

    try:
        ffmpeg = converter.require_tool("ffmpeg")
        ffprobe = converter.require_tool("ffprobe")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output).expanduser().resolve()
    root, files = iter_input_files(input_path, output_dir)
    if not files:
        print("No media files found.")
        return 0

    converted = 0
    skipped = 0
    failed = 0

    print(f"Testing {len(files)} file(s). Output: {output_dir}")

    for source in files:
        print(f"\nFile: {source}")
        try:
            info = converter.probe_media(ffprobe, source)
        except Exception as exc:
            print(f"Skip: probe failed: {exc}")
            skipped += 1
            continue

        print(f"Probe: {info.summary()}")
        if not info.is_mpeg_ps:
            print("Skip: real container is not MPEG-PS.")
            skipped += 1
            continue

        video = info.video or {}
        video_codec = str(video.get("codec_name") or "")
        if video_codec not in COPYABLE_VIDEO_CODECS:
            print(f"Skip: video codec is not H.264/HEVC: {video_codec or 'none'}")
            skipped += 1
            continue

        target = output_path_for(source, root, output_dir)
        if target.exists() and not args.force:
            print(f"Skip: output already exists: {target}")
            skipped += 1
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        command = build_copy_mp4_command(ffmpeg, source, target, info, args)
        print("Command:")
        print_command(command)

        if args.dry_run:
            converted += 1
            continue

        return_code, output_lines = run_and_collect(command)

        print_output_summary("ffmpeg output", output_lines)
        if return_code != 0:
            print(f"FAILED: ffmpeg exited with {return_code}", file=sys.stderr)
            failed += 1
            continue

        try:
            print(f"Validated: {validate_output(ffprobe, target)}")
        except RuntimeError as exc:
            print(f"FAILED validation: {exc}", file=sys.stderr)
            failed += 1
            continue

        if args.decode_check:
            return_code, warnings, warning_count = decode_check(ffmpeg, target)
            print(f"Decode check: return_code={return_code}, warnings={warning_count}")
            for line in warnings:
                print(f"  {line}")
            if return_code != 0:
                failed += 1
                continue

        converted += 1

    print(f"\nDone. converted={converted}, skipped={skipped}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
