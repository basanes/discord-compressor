from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import compressor


def test_cli_parser_accepts_requested_options(tmp_path: Path) -> None:
    args = compressor.build_parser().parse_args(
        [
            "--target-size",
            "18",
            "--input",
            str(tmp_path / "in"),
            "--output",
            str(tmp_path / "out"),
            "--workers",
            "2",
            "--max-height",
            "720",
            "--no-cleanup",
        ]
    )
    assert args.target_size == 18
    assert args.input == tmp_path / "in"
    assert args.output == tmp_path / "out"
    assert args.workers == 2
    assert args.max_height == 720
    assert args.no_cleanup is True
    legacy_args = compressor.build_parser().parse_args(["--yes"])
    assert legacy_args.no_cleanup is True


def test_output_names_are_unique_for_same_stem() -> None:
    files = [Path("clip.mp4"), Path("clip.mov"), Path("other.mkv")]

    names = compressor.build_output_names(files)

    assert names[Path("other.mkv")] == "compressed_other.mp4"
    assert names[Path("clip.mp4")] == "compressed_clip_mp4.mp4"
    assert names[Path("clip.mov")] == "compressed_clip_mov.mp4"
    assert len(set(names.values())) == len(files)


def test_probe_media_reports_ffprobe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "Invalid data")

    monkeypatch.setattr(compressor, "_run_command", fake_run)
    with pytest.raises(compressor.MediaError, match="ffprobe could not read"):
        compressor.probe_media("broken.mp4")


def test_small_video_is_validated_then_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"valid media placeholder")
    monkeypatch.setattr(compressor, "get_video_info", lambda _: (2.0, True, 720))

    output = compressor.compress_video(source, tmp_path / "compressed_clip.mp4", target_size_mb=1)

    assert output.name == "uncompressed_clip.mp4"
    assert output.read_bytes() == source.read_bytes()


def test_oversized_output_retries_and_publishes_only_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source" * 40)
    monkeypatch.setattr(compressor, "get_video_info", lambda _: (2.0, False, 720))
    monkeypatch.setattr(compressor, "probe_media", lambda _: {"streams": [{"codec_type": "video"}]})
    attempts: list[int] = []

    def fake_encode(*args, **kwargs) -> None:
        output_path = Path(args[1])
        attempts.append(1)
        output_path.write_bytes(b"x" * (120 if len(attempts) == 1 else 80))

    monkeypatch.setattr(compressor, "_encode_once", fake_encode)
    output = compressor.compress_video(
        source,
        tmp_path / "compressed_clip.mp4",
        target_size_mb=0.0001,  # 104 bytes
        max_retries=3,
    )

    assert len(attempts) == 2
    assert output.name == "compressed_clip.mp4"
    assert output.stat().st_size == 80


def test_encode_command_forces_compatible_pixel_format(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], duration: float, on_progress) -> None:
        commands.append(command)

    monkeypatch.setattr(compressor, "run_ffmpeg", fake_run)
    monkeypatch.setattr(compressor, "tqdm", lambda **kwargs: _FakeProgress())
    compressor._encode_once(
        Path("input.mp4"),
        Path("output.mp4"),
        duration=1,
        has_audio=False,
        video_bitrate=100_000,
        threads=1,
        max_height=None,
        label="input.mp4",
    )

    assert all(commands)
    assert all(command[command.index("-pix_fmt") + 1] == "yuv420p" for command in commands)


class _FakeProgress:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_description_str(self, value: str) -> None:
        pass

    def refresh(self) -> None:
        pass

    n = 0


def test_validate_output_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(b"x" * 10)
    monkeypatch.setattr(compressor, "probe_media", lambda _: {"streams": []})

    with pytest.raises(compressor.MediaError, match="above the target"):
        compressor.validate_output(output, max_bytes=5)
