import os
import subprocess
import json
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tqdm import tqdm
except ImportError:
    raise SystemExit("This script needs tqdm for its progress bars. Install it with:  pip install tqdm")

# How many videos to compress at the same time.
# Each ffmpeg/x264 process already uses several CPU cores, so half the core
# count is a sensible default. Raise or lower it to suit your machine.
MAX_WORKERS = max(1, (os.cpu_count() or 2) // 2)

# Two-pass encoding reads the whole video twice. Pass 1 only analyses it (and x264
# runs it with faster settings), so it takes less time than pass 2. This is the
# share of each progress bar given to pass 1. If the bar seems to speed up or
# slow down when it switches to pass 2, nudge this number.
PASS1_SHARE = 0.3


def log(message):
    # tqdm.write prints above the progress bars without garbling them, and it is
    # thread-safe, so output from different threads can't get mixed together.
    tqdm.write(message)


def get_video_duration(file_path):
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = json.loads(result.stdout)
    return float(data['format']['duration'])


def run_ffmpeg(cmd, duration, on_progress):
    """Run an ffmpeg command, calling on_progress(fraction) as it works (fraction goes 0.0 -> 1.0).
    Raises an error (with ffmpeg's message) if it fails."""
    # ffmpeg's error text goes to a temp file rather than a pipe, so it can't fill
    # up and stall ffmpeg while we're busy reading its progress from stdout.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err_file,
            encoding="utf-8",
            errors="replace",
        )
        try:
            # Because of "-progress pipe:1", ffmpeg prints a block of key=value lines
            # about twice a second. out_time_us is how much of the video it has
            # processed so far, in microseconds ("N/A" until the first frame is done).
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.lstrip("-").isdigit():
                    seconds_done = int(value) / 1_000_000
                    on_progress(min(max(seconds_done / duration, 0.0), 1.0))
            proc.wait()
        except BaseException:
            proc.kill()  # don't leave an orphaned ffmpeg running
            proc.wait()
            raise
        finally:
            proc.stdout.close()

        if proc.returncode != 0:
            err_file.seek(0)
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}:\n{err_file.read().strip()}")


def compress_video(input_path, output_path, target_size_mb=20, threads=0):
    """Compress video to a target file size in MB. Safe to call from several threads at once.
    A video that's already under the target is copied across untouched (as uncompressed_<name>) instead."""
    name = os.path.basename(input_path)

    # Already small enough? Then don't compress it: copy it across as-is, with an
    # "uncompressed_" prefix (instead of "compressed_") so you can tell it wasn't touched.
    size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if size_mb <= target_size_mb:
        output_path = os.path.join(os.path.dirname(output_path), f"uncompressed_{name}")
        shutil.copy2(input_path, output_path)
        log(f"{name} is already {size_mb:.1f}MB (target is {target_size_mb}MB), so it was copied as-is to {output_path}\n")
        return

    duration = get_video_duration(input_path)

    # Target size in bits
    target_size_bits = target_size_mb * 1024 * 1024 * 8
    target_bitrate = (target_size_bits / duration) * 0.90

    audio_bitrate = 128 * 1000
    video_bitrate = target_bitrate - audio_bitrate

    if video_bitrate < 100000:
        log(f"Warning: {name} is too long to fit nicely into {target_size_mb}MB without quality loss.")
        video_bitrate = 100000

    log(f"Compressing {name}...")

    null_device = "NUL" if os.name == "nt" else "/dev/null"

    # Fixed-width label so the bars of different videos line up
    label = name if len(name) <= 25 else name[:22] + "..."
    started = time.monotonic()

    # Two-pass encoding writes a stats log between the passes. By default every
    # ffmpeg process uses the same filename (ffmpeg2pass-0.log), so parallel jobs
    # would overwrite each other. Giving each job its own temp folder avoids that,
    # and the folder (with its logs) is deleted automatically when we're done.
    with tempfile.TemporaryDirectory() as tmp_dir, tqdm(
        total=100,
        desc=f"{label:<25} pass 1/2",
        bar_format="{desc} {percentage:3.0f}%|{bar}| {elapsed}",  # elapsed time only, no ETA
        dynamic_ncols=True,
        leave=False,  # the bar disappears when finished; the "Done!" line below replaces it
    ) as bar:
        passlog = os.path.join(tmp_dir, "pass")

        common = [
            "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-nostats", "-progress", "pipe:1",  # machine-readable progress on stdout
            "-i", input_path,
        ]
        video_opts = ["-c:v", "libx264", "-b:v", str(int(video_bitrate)), "-threads", str(threads)]

        def run_pass(number, pass_opts):
            # Each pass reports 0-100% of the video; map that onto its slice of the bar.
            low, high = (0, PASS1_SHARE) if number == 1 else (PASS1_SHARE, 1)

            def on_progress(fraction):
                bar.n = 100 * (low + (high - low) * fraction)
                bar.refresh()

            bar.set_description_str(f"{label:<25} pass {number}/2")
            on_progress(0)
            run_ffmpeg(common + video_opts + pass_opts, duration, on_progress)
            on_progress(1)

        # Pass 1
        run_pass(1, ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", null_device])

        # Pass 2
        run_pass(2, [
            "-pass", "2", "-passlogfile", passlog,
            "-c:a", "aac", "-b:a", "128k",
            output_path
        ])

    took = int(time.monotonic() - started)
    log(f"Done! Saved to {output_path} (took {took // 60}m {took % 60:02d}s)\n")


def offer_to_delete(question, paths):
    """Ask a yes/no question (the default is no) and permanently delete the given files if the answer is yes."""
    if not paths:
        return
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):  # no keyboard attached, or Ctrl+C: play it safe and keep everything
        print()
        return
    if answer not in ("y", "yes"):
        print("Kept.\n")
        return

    deleted = 0
    for path in paths:
        try:
            os.remove(path)
            deleted += 1
        except OSError as e:  # e.g. the file is open in a video player
            print(f"Couldn't delete {os.path.basename(path)}: {e}")
    print(f"Deleted {deleted} of {len(paths)} video(s).\n")


if __name__ == "__main__":
    # Use the folder where the script itself is currently running (Relative Path)
    current_dir = os.path.dirname(os.path.abspath(__file__))

    input_folder = os.path.join(current_dir, "input_videos")
    output_folder = os.path.join(current_dir, "compressed_videos")

    os.makedirs(input_folder, exist_ok=True)
    os.makedirs(output_folder, exist_ok=True)

    supported_extensions = (".mp4", ".mov", ".avi", ".mkv", ".webm")

    files = [f for f in os.listdir(input_folder) if f.lower().endswith(supported_extensions)]

    if not files:
        print(f"No videos found! Please put your videos inside: {input_folder}")
    else:
        workers = min(MAX_WORKERS, len(files))

        # Split the CPU cores between the running jobs so they don't fight over them.
        threads_per_job = max(1, (os.cpu_count() or 2) // workers)

        print(f"Found {len(files)} video(s). Compressing {workers} at a time...\n")

        failed = set()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for filename in files:
                in_file = os.path.join(input_folder, filename)
                # H.264 video + AAC audio can't be stored in a .webm file, so those come out as .mp4
                stem, ext = os.path.splitext(filename)
                out_name = f"compressed_{stem}.mp4" if ext.lower() == ".webm" else f"compressed_{filename}"
                out_file = os.path.join(output_folder, out_name)
                future = pool.submit(
                    compress_video, in_file, out_file,
                    target_size_mb=20, threads=threads_per_job
                )
                futures[future] = filename

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    failed.add(futures[future])
                    log(f"Error processing {futures[future]}: {e}\n")

        print("All batch processing complete!\n")

        # Clean-up: two separate questions, and anything other than "y" keeps the files.
        # The originals of videos that failed are never offered for deletion, so you can't lose them.
        if failed:
            print(f"Note: {len(failed)} video(s) failed, so their originals are left out of the clean-up below.\n")

        originals = [os.path.join(input_folder, f) for f in files if f not in failed]
        offer_to_delete(
            f"Permanently delete the {len(originals)} original video(s) in {os.path.basename(input_folder)}?",
            originals
        )

        results = [
            os.path.join(output_folder, f) for f in os.listdir(output_folder)
            if f.lower().endswith(supported_extensions)
        ]
        offer_to_delete(
            f"Permanently delete the {len(results)} video(s) in {os.path.basename(output_folder)}?",
            results
        )