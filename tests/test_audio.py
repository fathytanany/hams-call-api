import audioop
import itertools
import math
import struct
import wave
from pathlib import Path

import pytest

from callapi.audio import (
    FRAME_BYTES,
    PLAYOUT_MAX_BYTES,
    MediaFormatError,
    MediaReader,
    Playout,
    RecordingWriter,
    patch_wav_header,
)


def write_wav(
    path: Path, rate: int, samples: list[int], *, channels: int = 1, width: int = 2
) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        fmt = "<h" if width == 2 else "<B"
        w.writeframes(b"".join(struct.pack(fmt, s) for s in samples for _ in range(channels)))
    return path


def sine(rate: int, seconds: float, hz: float = 440, amp: int = 10000) -> list[int]:
    return [int(amp * math.sin(2 * math.pi * hz * n / rate)) for n in range(int(rate * seconds))]


def read_all(path: Path) -> list[bytes]:
    with MediaReader(path) as media:
        frames = []
        while (frame := media.read_frame()) is not None:
            frames.append(frame)
        return frames


def test_ulaw_known_values() -> None:
    # G.711: silence is 0xFF, full-scale positive 0x80, full-scale negative 0x00.
    assert audioop.lin2ulaw(struct.pack("<h", 0), 2) == b"\xff"
    assert audioop.lin2ulaw(struct.pack("<h", 32767), 2) == b"\x80"
    assert audioop.lin2ulaw(struct.pack("<h", -32768), 2) == b"\x00"


def test_ulaw_round_trip_within_one_quantization_step() -> None:
    # A mu-law step is at most 1/16 of the biased magnitude (|x| + 132).
    for x in range(-32768, 32768, 7):
        back = struct.unpack("<h", audioop.ulaw2lin(audioop.lin2ulaw(struct.pack("<h", x), 2), 2))
        assert abs(back[0] - x) <= (abs(x) + 132) / 16


@pytest.mark.parametrize("rate", [8000, 16000])
def test_one_second_is_fifty_20ms_frames(tmp_path: Path, rate: int) -> None:
    frames = read_all(write_wav(tmp_path / "a.wav", rate, sine(rate, 1)))
    assert len(frames) == 50
    assert all(len(f) == FRAME_BYTES == 160 for f in frames)


def test_last_partial_frame_is_padded_with_ulaw_silence(tmp_path: Path) -> None:
    frames = read_all(write_wav(tmp_path / "a.wav", 8000, [1000] * 170))
    assert len(frames) == 2
    assert frames[1][10:] == b"\xff" * 150


def test_8k_audio_survives_conversion(tmp_path: Path) -> None:
    samples = sine(8000, 0.2)
    pcm = audioop.ulaw2lin(b"".join(read_all(write_wav(tmp_path / "a.wav", 8000, samples))), 2)
    decoded = struct.unpack(f"<{len(samples)}h", pcm)
    assert all(abs(d - s) <= (abs(s) + 132) / 16 for d, s in zip(decoded, samples, strict=True))


def test_16k_resampled_to_8k_keeps_pitch(tmp_path: Path) -> None:
    frames = read_all(write_wav(tmp_path / "a.wav", 16000, sine(16000, 1, hz=440)))
    pcm = audioop.ulaw2lin(b"".join(frames), 2)
    out = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    crossings = sum(1 for a, b in itertools.pairwise(out) if (a < 0) != (b < 0))
    assert abs(crossings - 880) <= 18  # 440 Hz: two zero crossings per cycle, within 2%


@pytest.mark.parametrize(
    ("rate", "channels", "width"),
    [(8000, 2, 2), (8000, 1, 1), (44100, 1, 2)],
    ids=["stereo", "8-bit", "44.1k"],
)
def test_rejects_formats_outside_spec(tmp_path: Path, rate: int, channels: int, width: int) -> None:
    samples = [128] * 100 if width == 1 else [0] * 100
    path = write_wav(tmp_path / "a.wav", rate, samples, channels=channels, width=width)
    with pytest.raises(MediaFormatError, match="16-bit mono"):
        MediaReader(path)


def test_rejects_non_wav(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    path.write_bytes(b"not a wav file at all")
    with pytest.raises(MediaFormatError):
        MediaReader(path)


def test_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        MediaReader(tmp_path / "missing.wav")


ULAW_FRAMES = [bytes(range(i, i + FRAME_BYTES)) for i in range(0, 90, 30)]


def read_recording(path: Path) -> tuple[tuple[int, int, int], bytes]:
    with wave.open(str(path), "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes())


def test_recording_is_16bit_8k_mono_pcm_of_the_ulaw_written(tmp_path: Path) -> None:
    path = tmp_path / "rec.wav"
    with RecordingWriter(path) as rec:
        for frame in ULAW_FRAMES:
            rec.write(frame)
    fmt, pcm = read_recording(path)
    assert fmt == (1, 2, 8000)
    assert pcm == audioop.ulaw2lin(b"".join(ULAW_FRAMES), 2)


def test_patch_recovers_recording_whose_writer_died(tmp_path: Path) -> None:
    path = tmp_path / "rec.wav"
    with RecordingWriter(path) as rec:
        for frame in ULAW_FRAMES:
            rec.write(frame)
    raw = bytearray(path.read_bytes())
    raw[4:8] = raw[40:44] = bytes(4)  # the sizes a writer leaves when it never reaches close()
    path.write_bytes(raw)

    patch_wav_header(path)

    _, pcm = read_recording(path)
    assert pcm == audioop.ulaw2lin(b"".join(ULAW_FRAMES), 2)


def test_patch_turns_headerless_file_into_empty_wav(tmp_path: Path) -> None:
    path = tmp_path / "rec.wav"
    path.write_bytes(b"")  # died before the buffered header reached disk
    patch_wav_header(path)
    assert read_recording(path) == ((1, 2, 8000), b"")


def test_patch_leaves_complete_recording_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "rec.wav"
    with RecordingWriter(path) as rec:
        rec.write(ULAW_FRAMES[0])
    before = path.read_bytes()
    patch_wav_header(path)
    assert path.read_bytes() == before


def drain(p: Playout, ticks: int) -> list[tuple[bytes, list[str]]]:
    return [p.play() for _ in range(ticks)]


def test_playout_cuts_arbitrary_chunks_into_contiguous_frames() -> None:
    p = Playout()
    audio = bytes(range(256)) * 2  # 512 bytes in odd-sized chunks
    for i in range(0, len(audio), 37):
        p.add_audio(audio[i : i + 37])
    frames = [f for f, _ in drain(p, 4)]
    assert all(len(f) == FRAME_BYTES for f in frames)
    assert b"".join(frames) == audio.ljust(4 * FRAME_BYTES, b"\xff")


def test_empty_playout_is_silence() -> None:
    assert Playout().play() == (b"\xff" * FRAME_BYTES, [])


def test_mark_with_nothing_buffered_returns_on_the_next_tick() -> None:
    p = Playout()
    p.add_mark("now")
    assert p.play() == (b"\xff" * FRAME_BYTES, ["now"])


def test_mark_returns_when_the_audio_before_it_plays() -> None:
    p = Playout()
    p.add_audio(b"\x01" * 8000)  # 1s
    p.add_mark("after-1s")
    p.add_audio(b"\x02" * 320)
    results = drain(p, 52)
    marked = [i for i, (_, marks) in enumerate(results) if marks]
    # Tick 49 plays the last 20 ms of the first second; audio after the mark keeps flowing.
    assert marked == [49]
    assert results[49][1] == ["after-1s"]
    assert results[50][0] == b"\x02" * FRAME_BYTES


def test_clear_drops_audio_and_returns_pending_marks_in_order() -> None:
    p = Playout()
    p.add_audio(b"\x01" * 1600)
    p.add_mark("a")
    p.add_audio(b"\x01" * 1600)
    p.add_mark("b")
    p.play()
    assert p.clear() == ["a", "b"]
    assert p.play() == (b"\xff" * FRAME_BYTES, [])


def test_playout_caps_buffered_audio() -> None:
    p = Playout()
    p.add_audio(b"\x01" * (PLAYOUT_MAX_BYTES - 100))
    p.add_audio(b"\x02" * 300)
    assert p.dropped_bytes == 200
    p.add_mark("still-queued")  # marks are never dropped
    frames = drain(p, PLAYOUT_MAX_BYTES // FRAME_BYTES)
    assert frames[-1][1] == ["still-queued"]
