# Discord Video Compressor

Get any video under Discord's 20 MB upload limit with one command.

Discord's free upload limit is 20 MB (as of August 2026), and a screen recording or game clip blows past that in seconds. Instead of guessing quality settings and re-exporting until it fits, you tell this tool the size you need and it does the maths: drop your clips in a folder, run the script, and drag the results straight into Discord.

It's a small Python script around [ffmpeg](https://ffmpeg.org/). It works out the bitrate that lands just under your size limit, encodes with two-pass H.264, and runs several videos at once with a live progress bar for each.

## Features

- **Built for a size limit.** You set the target size (20 MB by default) and it calculates the bitrate to hit it, using two-pass x264 for accurate results. No quality slider to guess at.
- **Everything ready to upload.** Videos that are already under the limit are copied across untouched as `uncompressed_<name>`, so `compressed_videos/` ends up holding files that are all ready to drop into Discord.
- **Batch processing.** Handles a whole folder of clips in one run, several at a time (default: half your CPU cores).
- **Real progress bars.** One bar per video, driven by ffmpeg's actual progress, plus elapsed time. No guessed ETA.
- **Optional clean-up.** When everything is done you're asked, separately, whether to delete the originals and the compressed videos.

## Requirements

- Python 3.8 or newer
- [ffmpeg](https://ffmpeg.org/download.html) on your `PATH` (ffprobe comes with it)
- [tqdm](https://pypi.org/project/tqdm/) for the progress bars

Install ffmpeg, then restart your terminal so it's picked up:

| OS | Command |
|----|---------|
| Windows | `winget install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |

You can also download a build from ffmpeg.org and add its `bin` folder to your `PATH`. Check it worked with `ffmpeg -version`.

Install tqdm:

```
python -m pip install tqdm
```

Using `python -m pip` makes sure it goes into the same Python you'll run the script with.

## Usage

1. Run the script once. It creates `input_videos/` and `compressed_videos/` next to `compressor.py` (or create them yourself).
2. Put your clips in `input_videos/`. Supported: `.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`.
3. Run it again:

   ```
   python compressor.py
   ```

   (Use `python3` on macOS and Linux.)

4. Open `compressed_videos/` and drag the files into Discord:

| Input video | Saved in `compressed_videos/` as |
|-------------|----------------------------------|
| Bigger than the target | `compressed_<name>` (re-encoded) |
| Already at or under the target | `uncompressed_<name>` (an exact copy) |

`.webm` files are saved as `.mp4` when they're compressed, because H.264 video and AAC audio can't be stored in a WebM file.

### Example run

```
Found 3 video(s). Compressing 2 at a time...

short_clip.mp4 is already 12.4MB (target is 20MB), so it was copied as-is to ...\compressed_videos\uncompressed_short_clip.mp4

Compressing highlight_reel.mp4...
Compressing funny_moment.mov...
highlight_reel.mp4        pass 2/2  73%|█████████████████████████████▏          | 00:08
funny_moment.mov          pass 1/2  19%|███████▌                                | 00:02
```

Finished videos print a line above the bars, and then the clean-up questions appear:

```
Done! Saved to ...\compressed_videos\compressed_highlight_reel.mp4 (took 1m 12s)
Done! Saved to ...\compressed_videos\compressed_funny_moment.mov (took 0m 48s)
All batch processing complete!

Permanently delete the 3 original video(s) in input_videos? [y/N]:
Permanently delete the 3 video(s) in compressed_videos? [y/N]:
```

## Discord's upload limits

| Account | Max file size |
|---------|---------------|
| Free | 20 MB |
| Nitro Basic | 50 MB |
| Nitro | 500 MB |

These are Discord's limits as of August 2026, when the free limit was raised from 10 MB to 20 MB. They've changed before, so check Discord's help pages if uploads start failing. If you have Nitro, or a server that allows bigger uploads, set `target_size_mb` to match (see [Configuration](#configuration)).

## Clean-up prompts

When the batch finishes you get two separate questions: delete the originals in `input_videos/`, and delete the videos in `compressed_videos/`.

- Press Enter (or anything other than `y` / `yes`) to keep the files.
- Deleting is permanent. Nothing goes to the Recycle Bin.
- If a video failed to compress, its original is never offered for deletion, so you can't lose the only copy.
- The second question covers every video in `compressed_videos/`, including results from earlier runs. The count in the question tells you how many files that is.

## How it works

1. `ffprobe` reads the video's duration.
2. The target size is turned into a bitrate: `(target size in bits / duration) x 0.90`, minus 128 kbps for the audio. The 10% cushion covers container overhead and the encoder not hitting the bitrate exactly.
3. Two-pass x264: pass 1 analyses the whole video (no output file), and pass 2 encodes using that analysis so the bits go where the footage needs them. Audio is AAC at 128 kbps.
4. Each job keeps its pass logs in its own temporary folder, so parallel jobs never overwrite each other's.
5. Progress comes from ffmpeg's `-progress` output, which reports how much of the video it has processed so far. Both passes share one bar: pass 1 fills the first 30% and pass 2 the rest.

## Configuration

Everything is a plain setting in `compressor.py`:

| Setting | Where | Default | What it does |
|---------|-------|---------|--------------|
| `target_size_mb` | the `pool.submit(...)` call at the bottom | `20` | Target size per video in MB (1 MB = 1024 x 1024 bytes). Set it to your Discord limit. |
| `MAX_WORKERS` | top of the file | half your CPU cores | How many videos are compressed at once |
| `PASS1_SHARE` | top of the file | `0.3` | How much of the progress bar pass 1 gets (cosmetic only) |
| `supported_extensions` | bottom of the file | `.mp4 .mov .avi .mkv .webm` | Which files are picked up |
| `input_folder`, `output_folder` | bottom of the file | `input_videos`, `compressed_videos` | Where videos are read from and written to |

## Notes and troubleshooting

- **Discord still says the file is too big.** The target is approximate, and Discord may count a megabyte slightly differently than the script does (1 MB = 1024 x 1024 bytes here). Lower `target_size_mb` a little, for example to `18`, and run it again.
- **Long videos.** The size is fixed, so the longer the video, the less data each second gets. At the default 20 MB, anything longer than about 11 minutes makes the script warn you and fall back to a minimum video bitrate of 100 kbps, so the file can end up over the limit. Trim long recordings first.
- **Quality.** Resolution and frame rate are left alone and only the bitrate is reduced, so a short clip looks much better than a long one at the same file size. If a video looks blocky, trim it before compressing.
- **Re-running** overwrites files with the same name in `compressed_videos/`.
- `The system cannot find the file specified` (Windows) or `No such file or directory: 'ffprobe'` (macOS/Linux): ffmpeg isn't installed or isn't on your `PATH`.
- `This script needs tqdm for its progress bars`: install it with the same Python you run the script with, using `python -m pip install tqdm`.
- `Error processing <file>: 'format'`: ffprobe couldn't read the file, so it's corrupt or not really a video.
- `ffmpeg exited with code ...`: ffmpeg's own error message follows it, which is the place to start.
- **Garbled progress bars.** Run it in a proper terminal (Windows Terminal, PowerShell, the VS Code terminal, Terminal.app). Some IDE output panels can't redraw several bars at once.

## Project layout

```
compressor/
├── compressor.py
├── input_videos/         your original videos (created on first run)
├── compressed_videos/    the results (created on first run)
└── README.md
```

Videos are big, so add `input_videos/` and `compressed_videos/` to a `.gitignore` file to keep them out of your commits.
