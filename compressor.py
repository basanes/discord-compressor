"""Batch-compress videos to a target size for Discord and similar services."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

try:
    from tqdm import tqdm
except ImportError as exc:  # pragma: no cover - exercised by installation, not tests
    raise SystemExit(
        "This script needs tqdm for its progress bars. Install it with: python -m pip install tqdm"
    ) from exc

SUPPORTED_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
DEFAULT_TARGET_SIZE_MB = 20.0
DEFAULT_MAX_WORKERS = max(1, (os.cpu_count() or 2) // 2)
PASS1_SHARE = 0.3
AUDIO_BITRATE = 128_000
MIN_VIDEO_BITRATE = 100_000
SIZE_SAFETY_MARGIN = 0.90
MAX_SIZE_RETRIES = 3


class MediaError(RuntimeError):
    """Raised when media cannot be probed, encoded, or validated."""


def log(message: str) -> None:
    """Print a message without corrupting active progress bars."""
    tqdm.write(message)


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a media command and return its completed process."""
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        executable = command[0]
        raise MediaError(
            f"{executable} was not found. Install ffmpeg and make sure it is on PATH."
        ) from exc
    except OSError as exc:
        raise MediaError(f"Could not start {command[0]}: {exc}") from exc


def probe_media(file_path: str | Path) -> dict:
    """Return ffprobe's JSON metadata or raise a useful MediaError."""
    path = str(file_path)
    result = _run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            path,
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown ffprobe error"
        raise MediaError(f"ffprobe could not read {path}: {detail}")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid metadata for {path}") from exc
    if not isinstance(metadata, dict):
        raise MediaError(f"ffprobe returned unexpected metadata for {path}")
    return metadata


def get_video_info(file_path: str | Path) -> tuple[float, bool, int | None]:
    """Return duration, whether audio exists, and source height."""
    metadata = probe_media(file_path)
    streams = metadata.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise MediaError(f"{file_path} has no video stream")

    duration_value = (metadata.get("format") or {}).get("duration")
    if duration_value is None:
        duration_value = video_stream.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"Could not determine the duration of {file_path}") from exc
    if not duration > 0:
        raise MediaError(f"{file_path} has no usable duration")

    height = video_stream.get("height")
    try:
        height = int(height) if height is not None else None
    except (TypeError, ValueError):
        height = None
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return duration, has_audio, height


def get_video_duration(file_path: str | Path) -> float:
    """Get video duration in seconds using ffprobe."""
    return get_video_info(file_path)[0]


def validate_output(file_path: str | Path, max_bytes: int) -> int:
    """Validate an encoded file and return its byte size."""
    path = Path(file_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError(f"ffmpeg did not create a usable output file: {path}")
    try:
        probe_media(path)
    except MediaError as exc:
        raise MediaError(f"Output validation failed for {path}: {exc}") from exc
    size = path.stat().st_size
    if size > max_bytes:
        raise MediaError(
            f"Output is {size / (1024 * 1024):.2f} MB, above the target "
            f"of {max_bytes / (1024 * 1024):.2f} MB"
        )
    return size


def run_ffmpeg(cmd: list[str], duration: float, on_progress: Callable[[float], None]) -> None:
    """Run ffmpeg and report progress, raising MediaError on failure."""
    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err_file,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        err_file.close()
        raise MediaError(
            "ffmpeg was not found. Install ffmpeg and make sure it is on PATH."
        ) from exc
    except OSError as exc:
        err_file.close()
        raise MediaError(f"Could not start ffmpeg: {exc}") from exc

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.lstrip("-").isdigit():
                seconds_done = int(value) / 1_000_000
                on_progress(min(max(seconds_done / duration, 0.0), 1.0))
        return_code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        err_file.seek(0)
        stderr = err_file.read()
        err_file.close()

    if return_code != 0:
        detail = stderr.strip() or "no error details from ffmpeg"
        raise MediaError(f"ffmpeg exited with code {return_code}: {detail}")


def _progress_filter(max_height: int | None) -> str | None:
    if max_height is None:
        return None
    return f"scale=-2:min(ih\\,{max_height})"


def _encode_once(
    input_path: Path,
    output_path: Path,
    duration: float,
    has_audio: bool,
    video_bitrate: int,
    threads: int,
    max_height: int | None,
    label: str,
) -> None:
    """Run one two-pass encode into output_path."""
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    with tempfile.TemporaryDirectory(prefix="discord-compressor-") as tmp_dir:
        passlog = str(Path(tmp_dir) / "pass")
        common = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            str(video_bitrate),
            "-threads",
            str(threads),
        ]
        video_filter = _progress_filter(max_height)
        if video_filter:
            common += ["-vf", video_filter]

        with tqdm(
            total=100,
            desc=f"{label:<25} pass 1/2",
            bar_format="{desc} {percentage:3.0f}%|{bar}| {elapsed}",
            dynamic_ncols=True,
            leave=False,
        ) as bar:

            def run_pass(number: int, options: list[str]) -> None:
                low, high = (0, PASS1_SHARE) if number == 1 else (PASS1_SHARE, 1)

                def on_progress(fraction: float) -> None:
                    bar.n = 100 * (low + (high - low) * fraction)
                    bar.refresh()

                bar.set_description_str(f"{label:<25} pass {number}/2")
                on_progress(0)
                run_ffmpeg(common + options, duration, on_progress)
                on_progress(1)

            run_pass(
                1,
                ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", null_device],
            )
            audio_options = (
                ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE // 1000}k"]
                if has_audio
                else ["-an"]
            )
            run_pass(
                2,
                [
                    "-pass",
                    "2",
                    "-passlogfile",
                    passlog,
                    *audio_options,
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
            )


def compress_video(
    input_path: str | Path,
    output_path: str | Path,
    target_size_mb: float = DEFAULT_TARGET_SIZE_MB,
    threads: int = 1,
    max_height: int | None = None,
    max_retries: int = MAX_SIZE_RETRIES,
) -> Path:
    """Compress one video, validating size and media before publishing the result."""
    input_file = Path(input_path)
    requested_output = Path(output_path)
    if target_size_mb <= 0:
        raise ValueError("target_size_mb must be greater than zero")
    if not input_file.is_file():
        raise MediaError(f"Input file does not exist: {input_file}")

    duration, has_audio, source_height = get_video_info(input_file)
    max_bytes = int(target_size_mb * 1024 * 1024)
    name = input_file.name
    size = input_file.stat().st_size

    if size <= max_bytes:
        final_path = requested_output.with_name(f"uncompressed_{name}")
        shutil.copy2(input_file, final_path)
        log(
            f"{name} is already {size / (1024 * 1024):.1f}MB "
            f"(target is {target_size_mb:g}MB), so it was copied as-is to {final_path}\n"
        )
        return final_path

    if max_height is not None and max_height <= 0:
        raise ValueError("max_height must be greater than zero")
    if max_height is not None and source_height and source_height <= max_height:
        max_height = None

    target_size_bits = max_bytes * 8
    audio_budget = AUDIO_BITRATE if has_audio else 0
    video_bitrate = max(
        MIN_VIDEO_BITRATE,
        int((target_size_bits / duration) * SIZE_SAFETY_MARGIN - audio_budget),
    )
    label = name if len(name) <= 25 else name[:22] + "..."
    started = time.monotonic()
    requested_output.parent.mkdir(parents=True, exist_ok=True)
    last_error: MediaError | None = None

    for attempt in range(1, max_retries + 1):
        with tempfile.TemporaryDirectory(prefix="discord-output-") as tmp_dir:
            temporary_output = Path(tmp_dir) / "encoded.mp4"
            try:
                log(f"Compressing {name} (attempt {attempt}/{max_retries})...")
                _encode_once(
                    input_file,
                    temporary_output,
                    duration,
                    has_audio,
                    video_bitrate,
                    threads,
                    max_height,
                    label,
                )
                output_size = validate_output(temporary_output, max_bytes)
                os.replace(temporary_output, requested_output)
                took = int(time.monotonic() - started)
                log(
                    f"Done! Saved to {requested_output} "
                    f"({output_size / (1024 * 1024):.2f}MB, took {took // 60}m {took % 60:02d}s)\n"
                )
                return requested_output
            except MediaError as exc:
                last_error = exc
                if "above the target" not in str(exc) or attempt == max_retries:
                    break
                actual_size = temporary_output.stat().st_size if temporary_output.exists() else max_bytes
                video_bitrate = max(
                    MIN_VIDEO_BITRATE,
                    int(video_bitrate * (max_bytes / actual_size) * 0.92),
                )
                log(f"{exc}; retrying with a lower video bitrate.")

    raise MediaError(f"Error processing {name}: {last_error}")


def offer_to_delete(question: str, paths: Iterable[Path]) -> None:
    """Ask whether to permanently delete paths; default is to keep them."""
    paths = list(paths)
    if not paths:
        return
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer not in ("y", "yes"):
        print("Kept.\n")
        return
    deleted = 0
    for path in paths:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            print(f"Couldn't delete {path.name}: {exc}")
    print(f"Deleted {deleted} of {len(paths)} video(s).\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compress videos to a target size using two-pass H.264 encoding."
    )
    script_dir = Path(__file__).resolve().parent
    parser.add_argument(
        "--target-size",
        type=float,
        default=DEFAULT_TARGET_SIZE_MB,
        metavar="MB",
        help=f"maximum output size in MB (default: {DEFAULT_TARGET_SIZE_MB:g})",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=script_dir / "input_videos",
        metavar="DIR",
        help="folder containing input videos",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=script_dir / "compressed_videos",
        metavar="DIR",
        help="folder for ready-to-upload videos",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        metavar="N",
        help=f"number of videos to process concurrently (default: {DEFAULT_MAX_WORKERS})",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        metavar="PIXELS",
        help="optionally scale videos taller than this height before encoding",
    )
    parser.add_argument(
        "--no-cleanup",
        "--yes",
        dest="no_cleanup",
        action="store_true",
        help="skip cleanup prompts and keep all files (--yes is a legacy alias)",
    )
    return parser


def build_output_names(files: Iterable[Path]) -> dict[Path, str]:
    """Build unique MP4 names, including an extension suffix when stems collide."""
    files = list(files)
    candidates = {path: f"compressed_{path.stem}.mp4" for path in files}
    groups: dict[str, list[Path]] = defaultdict(list)
    for path, candidate in candidates.items():
        groups[candidate.casefold()].append(path)

    output_names: dict[Path, str] = {}
    for group in groups.values():
        if len(group) == 1:
            output_names[group[0]] = candidates[group[0]]
            continue
        used: set[str] = set()
        for path in sorted(group, key=lambda item: item.name.casefold()):
            extension = path.suffix.lstrip(".").lower() or "video"
            base = f"compressed_{path.stem}_{extension}"
            candidate = f"{base}.mp4"
            index = 2
            while candidate.casefold() in used:
                candidate = f"{base}_{index}.mp4"
                index += 1
            used.add(candidate.casefold())
            output_names[path] = candidate
    return output_names


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.target_size <= 0:
        raise SystemExit("--target-size must be greater than zero")
    if args.workers <= 0:
        raise SystemExit("--workers must be greater than zero")
    if args.max_height is not None and args.max_height <= 0:
        raise SystemExit("--max-height must be greater than zero")

    args.input.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    files = sorted(
        (path for path in args.input.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda path: path.name.lower(),
    )
    if not files:
        print(f"No videos found! Please put videos inside: {args.input}")
        return 0

    workers = min(args.workers, len(files))
    threads_per_job = max(1, (os.cpu_count() or 2) // workers)
    print(f"Found {len(files)} video(s). Compressing {workers} at a time...\n")
    failed: set[Path] = set()
    output_names = build_output_names(files)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for input_file in files:
            # Compressed outputs are always MP4 for predictable Discord playback.
            output_file = args.output / output_names[input_file]
            futures[pool.submit(
                compress_video,
                input_file,
                output_file,
                args.target_size,
                threads_per_job,
                args.max_height,
            )] = input_file
        for future in as_completed(futures):
            input_file = futures[future]
            try:
                future.result()
            except Exception as exc:  # keep processing the rest of the batch
                failed.add(input_file)
                log(f"Error processing {input_file.name}: {exc}\n")

    print("All batch processing complete!\n")
    if failed:
        print(f"Note: {len(failed)} video(s) failed, so their originals are left out of cleanup below.\n")
    if not args.no_cleanup:
        originals = [path for path in files if path not in failed]
        offer_to_delete(
            f"Permanently delete the {len(originals)} original video(s) in {args.input.name}?",
            originals,
        )
        results = [
            path for path in args.output.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        offer_to_delete(
            f"Permanently delete the {len(results)} video(s) in {args.output.name}?",
            results,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
