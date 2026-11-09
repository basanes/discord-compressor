"""Join two videos into one, then compress the result to a target size.

Reuses compressor.py (keep both files in the same folder).

Example:
    python merge_compress.py intro.mp4 clip.mov -o out/final.mp4 --target-size 20
"""

from __future__ import annotations

import argparse
import os
import tempfile
from fractions import Fraction
from pathlib import Path

from compressor import (
    DEFAULT_TARGET_SIZE_MB,
    MediaError,
    compress_video,
    get_video_info,
    log,
    probe_media,
    run_ffmpeg,
    tqdm,
)

AUDIO_RATE = 48_000
DEFAULT_FPS = 30.0


def _video_stream(path: Path) -> dict:
    streams = probe_media(path).get("streams") or []
    stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if stream is None:
        raise MediaError(f"{path} has no video stream")
    return stream


def _display_size(stream: dict) -> tuple[int, int]:
    """Width/height as the video will actually be displayed (phone clips are often rotated)."""
    width, height = int(stream["width"]), int(stream["height"])
    rotation = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = int(float(tags["rotate"]))
        except ValueError:
            pass
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = int(float(side_data["rotation"]))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def _fps(stream: dict) -> float:
    try:
        fps = float(Fraction(stream.get("avg_frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return fps if fps > 0 else DEFAULT_FPS


def _even(value: int) -> int:
    return max(2, value - (value % 2))


def merge_videos(
    first: Path,
    second: Path,
    output_path: Path,
    max_height: int | None = None,
    fps: float | None = None,
    threads: int = 1,
) -> Path:
    """Concatenate two videos into one high-quality intermediate file.

    Both clips are scaled/padded to the first clip's size and frame rate so they
    can be joined even when their resolution, fps, or audio layout differ.
    """
    infos = [get_video_info(first), get_video_info(second)]  # (duration, has_audio, height)
    durations = [info[0] for info in infos]
    has_audio = [info[1] for info in infos]
    stream0 = _video_stream(first)

    width, height = _display_size(stream0)
    if max_height and height > max_height:
        width = round(width * max_height / height)
        height = max_height
    width, height = _even(width), _even(height)
    fps = fps or _fps(stream0)
    keep_audio = any(has_audio)

    command = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1"]
    for path in (first, second):
        command += ["-i", str(path)]

    # Silent filler for whichever clip has no audio, so concat gets matching streams.
    audio_labels: list[str] = []
    next_input = 2
    if keep_audio:
        for index, present in enumerate(has_audio):
            if present:
                audio_labels.append(f"[{index}:a:0]")
            else:
                command += ["-f", "lavfi", "-t", f"{durations[index]:.3f}", "-i",
                            f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}"]
                audio_labels.append(f"[{next_input}:a]")
                next_input += 1

    parts = []
    for index in range(2):
        parts.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:g},"
            f"format=yuv420p[v{index}]"
        )
        if keep_audio:
            parts.append(
                f"{audio_labels[index]}aformat=sample_rates={AUDIO_RATE}:"
                f"channel_layouts=stereo[a{index}]"
            )
    if keep_audio:
        parts.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    else:
        parts.append("[v0][v1]concat=n=2:v=1:a=0[v]")

    command += ["-filter_complex", ";".join(parts), "-map", "[v]"]
    if keep_audio:
        command += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    # Near-lossless intermediate: the real size reduction happens in the compress step,
    # so we avoid stacking two noticeable quality losses.
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                "-pix_fmt", "yuv420p", "-threads", str(threads), str(output_path)]

    total = sum(durations)
    with tqdm(total=100, desc="Merging", bar_format="{desc} {percentage:3.0f}%|{bar}| {elapsed}",
              dynamic_ncols=True, leave=False) as bar:
        def on_progress(fraction: float) -> None:
            bar.n = 100 * fraction
            bar.refresh()

        run_ffmpeg(command, total, on_progress)
        on_progress(1)
    return output_path


def merge_and_compress(
    first: Path,
    second: Path,
    output_file: Path,
    target_size_mb: float = DEFAULT_TARGET_SIZE_MB,
    max_height: int | None = None,
    fps: float | None = None,
    threads: int | None = None,
    keep_merged: bool = False,
) -> Path:
    threads = threads or (os.cpu_count() or 2)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="merge-") as tmp_dir:
        merged = Path(tmp_dir) / f"{output_file.stem}.mp4"
        log(f"Merging {first.name} + {second.name}...")
        merge_videos(first, second, merged, max_height, fps, threads)

        log("Compressing merged video...")
        result = compress_video(merged, output_file, target_size_mb, threads, max_height)

        if keep_merged:
            kept = output_file.with_name(f"{output_file.stem}_merged_uncompressed.mp4")
            kept.write_bytes(merged.read_bytes())
            log(f"Kept uncompressed merge at {kept}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Join two videos, then compress to a target size.")
    parser.add_argument("first", type=Path, help="video that plays first")
    parser.add_argument("second", type=Path, help="video that plays second")
    parser.add_argument("-o", "--output", type=Path, default=Path("merged_compressed.mp4"),
                        help="output file (default: merged_compressed.mp4)")
    parser.add_argument("--target-size", type=float, default=DEFAULT_TARGET_SIZE_MB, metavar="MB",
                        help=f"maximum output size in MB (default: {DEFAULT_TARGET_SIZE_MB:g})")
    parser.add_argument("--max-height", type=int, metavar="PIXELS",
                        help="downscale if taller than this (e.g. 720)")
    parser.add_argument("--fps", type=float, help="output frame rate (default: first video's)")
    parser.add_argument("--keep-merged", action="store_true",
                        help="also keep the uncompressed merged file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for path in (args.first, args.second):
        if not path.is_file():
            raise SystemExit(f"File not found: {path}")
    if args.target_size <= 0:
        raise SystemExit("--target-size must be greater than zero")
    if args.max_height is not None and args.max_height <= 0:
        raise SystemExit("--max-height must be greater than zero")
    try:
        merge_and_compress(args.first, args.second, args.output, args.target_size,
                           args.max_height, args.fps, keep_merged=args.keep_merged)
    except MediaError as exc:
        raise SystemExit(f"Error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())