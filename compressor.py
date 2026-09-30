import os
import subprocess
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# How many videos to compress at the same time.
# Each ffmpeg/x264 process already uses several CPU cores, so half the core
# count is a sensible default. Raise or lower it to suit your machine.
MAX_WORKERS = max(1, (os.cpu_count() or 2) // 2)

# Stops output from different threads getting mixed together on one line.
print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(message, flush=True)


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


def run_ffmpeg(cmd):
    """Run an ffmpeg command and raise an error (with ffmpeg's message) if it fails."""
    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {result.returncode}:\n{result.stderr.strip()}")


def compress_video(input_path, output_path, target_size_mb=20, threads=0):
    """Compress video to a target file size in MB. Safe to call from several threads at once."""
    name = os.path.basename(input_path)
    duration = get_video_duration(input_path)

    # Target size in bits
    target_size_bits = target_size_mb * 1024 * 1024 * 8
    target_bitrate = (target_size_bits / duration) * 0.90

    audio_bitrate = 128 * 1000
    video_bitrate = target_bitrate - audio_bitrate

    if video_bitrate < 100000:
        log(f"Warning: {name} is too long to fit nicely into {target_size_mb}MB without quality loss.")
        video_bitrate = 100000

    log(f"Compressing {name} (Duration: {duration:.1f}s)...")

    null_device = "NUL" if os.name == "nt" else "/dev/null"

    # Two-pass encoding writes a stats log between the passes. By default every
    # ffmpeg process uses the same filename (ffmpeg2pass-0.log), so parallel jobs
    # would overwrite each other. Giving each job its own temp folder avoids that,
    # and the folder (with its logs) is deleted automatically when we're done.
    with tempfile.TemporaryDirectory() as tmp_dir:
        passlog = os.path.join(tmp_dir, "pass")

        common = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", input_path]
        video_opts = ["-c:v", "libx264", "-b:v", str(int(video_bitrate)), "-threads", str(threads)]

        # Pass 1
        run_ffmpeg(common + video_opts + [
            "-pass", "1", "-passlogfile", passlog,
            "-an", "-f", "null", null_device
        ])

        # Pass 2
        run_ffmpeg(common + video_opts + [
            "-pass", "2", "-passlogfile", passlog,
            "-c:a", "aac", "-b:a", "128k",
            output_path
        ])

    log(f"Done! Saved to {output_path}\n")


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

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for filename in files:
                in_file = os.path.join(input_folder, filename)
                out_file = os.path.join(output_folder, f"compressed_{filename}")
                future = pool.submit(
                    compress_video, in_file, out_file,
                    target_size_mb=20, threads=threads_per_job
                )
                futures[future] = filename

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    log(f"Error processing {futures[future]}: {e}\n")

        print("All batch processing complete!")