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
            "--yes",
        ]
    )
    assert args.target_size == 18
    assert args.input == tmp_path / "in"
    assert args.output == tmp_path / "out"
    assert args.workers == 2
    assert args.max_height == 720
    assert args.yes is True


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


def test_validate_output_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(b"x" * 10)
    monkeypatch.setattr(compressor, "probe_media", lambda _: {"streams": []})

    with pytest.raises(compressor.MediaError, match="above the target"):
        compressor.validate_output(output, max_bytes=5)
