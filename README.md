# Discord Video Compressor

Compress a folder of videos to a size limit with one command. The script uses `ffmpeg` and two-pass H.264 encoding, validates every output, retries files that are still too large, and keeps failed inputs safe.

## Features

- CLI options for target size, input folder, output folder, and worker count
- Two-pass H.264/AAC encoding with a size safety margin
- Final output validation with automatic bitrate retries
- Temporary output files, so failed encodes are never published as finished files
- Optional resizing with `--max-height` for long or high-resolution clips
- Batch processing with progress bars
- Already-small videos are validated and copied without re-encoding
- Failed inputs are never offered for deletion
- Compressed outputs are always MP4 for predictable playback

## Requirements

- Python 3.8 or newer
- [ffmpeg](https://ffmpeg.org/download.html) and `ffprobe` on your `PATH`
- Python dependencies in `requirements.txt`

Install ffmpeg:

| OS | Command |
|---|---|
| Windows | `winget install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Check ffmpeg:

```bash
ffmpeg -version
ffprobe -version
```

## Usage

Put clips in `input_videos/` and run:

```bash
python compressor.py
```

The default target is 20 MB. For a more conservative Discord limit:

```bash
python compressor.py --target-size 18
```

Use custom folders and concurrency:

```bash
python compressor.py \
  --target-size 20 \
  --input ./clips \
  --output ./ready-for-discord \
  --workers 2
```

Resize tall videos before encoding:

```bash
python compressor.py --target-size 20 --max-height 720
```

For automation or scripts, `--yes` skips the cleanup prompts and keeps all files:

```bash
python compressor.py --input ./clips --output ./ready --yes
```

### CLI options

| Option | Default | Description |
|---|---:|---|
| `--target-size MB` | `20` | Maximum output size per video, using 1 MB = 1024² bytes |
| `--input DIR` | `input_videos` | Folder containing source videos |
| `--output DIR` | `compressed_videos` | Folder for results |
| `--workers N` | Half of CPU cores | Number of videos processed concurrently |
| `--max-height PIXELS` | None | Scale videos taller than this height before encoding |
| `--yes` | Off | Keep files and skip cleanup prompts |

Supported input extensions are `.mp4`, `.mov`, `.avi`, `.mkv`, and `.webm`.

## Output behavior

- An input already under the target is copied as `uncompressed_<original-name>`.
- A re-encoded input is saved as `compressed_<stem>.mp4`.
- Each compressed result is written to a temporary file first.
- The result is probed and size-checked before being atomically moved into the output folder.
- If the result is too large, the video bitrate is reduced and encoding is retried up to three times.
- If all attempts fail, the original remains untouched and the program exits with status 1 after processing the other files.

The size target is a maximum, not a promise of a particular visual quality. Very long videos may need a lower resolution or a shorter duration. Use `--max-height` to make long clips more watchable at a fixed size.

## Cleanup

When run interactively, the program separately asks whether to delete:

1. Successfully processed originals
2. Videos in the output folder, including results from earlier runs

Press Enter or answer anything other than `y` / `yes` to keep files. Deletion is permanent and does not use the Recycle Bin. Use `--yes` to skip both prompts and keep everything.

## How it works

1. `ffprobe` validates the input and reads its duration, dimensions, and audio streams.
2. The target size is converted into a video bitrate with a 10% safety margin.
3. ffmpeg performs two-pass H.264 encoding and AAC audio encoding when audio exists.
4. A temporary MP4 is validated with `ffprobe` and checked against the byte limit.
5. Oversized results are retried with a lower bitrate.

## Testing

Run the test suite locally:

```bash
python -m pytest -q
```

GitHub Actions runs the suite on Python 3.8, 3.11, and 3.12. The repository does not include large media fixtures; the tests mock ffmpeg metadata and encoding where appropriate, while manual end-to-end testing should use real local clips.

## Troubleshooting

- **ffmpeg or ffprobe was not found:** install ffmpeg and restart the terminal so it is on `PATH`.
- **ffprobe could not read a file:** the file may be corrupt or may not contain a video stream.
- **The output is still too large:** the script retries automatically; use a smaller target, `--max-height`, or trim the clip.
- **The output looks blocky:** use a shorter clip or a larger target. A fixed file size gives longer videos less data per second.
- **Progress bars look garbled:** use a normal terminal such as Windows Terminal, PowerShell, Terminal.app, or a VS Code terminal.
- **Discord rejects a file near the limit:** use a conservative target such as 18 MB because service limits and size accounting can change.

## License

MIT. See [LICENSE](LICENSE).
