"""Audio at the edges of a call: callee WAV -> mu-law frames, app mu-law -> recording WAV."""

import audioop
import struct
import wave
from pathlib import Path
from typing import Self

RATE = 8000
FRAME_BYTES = RATE * 20 // 1000  # 160 samples, one mu-law byte each
ULAW_SILENCE = b"\xff"
HEADER_BYTES = 44


class MediaFormatError(ValueError):
    """The file is not a 16-bit PCM mono WAV at 8 or 16 kHz."""


class MediaReader:
    """Reads one frame per call so a 30-minute file is never held in memory."""

    def __init__(self, path: Path) -> None:
        try:
            self._wav = wave.open(str(path), "rb")  # noqa: SIM115 - held open for the call, closed in close()
        except (wave.Error, EOFError) as e:
            raise MediaFormatError(f"{path.name}: not a PCM WAV file ({e})") from e
        width, channels, rate = (
            self._wav.getsampwidth(),
            self._wav.getnchannels(),
            self._wav.getframerate(),
        )
        if (width, channels) != (2, 1) or rate not in (8000, 16000):
            self._wav.close()
            raise MediaFormatError(
                f"{path.name}: need 16-bit mono at 8 or 16 kHz, "
                f"got {8 * width}-bit, {channels} channel(s), {rate} Hz"
            )
        self._rate = rate
        self._ratecv_state: tuple[int, tuple[tuple[int, int], ...]] | None = None

    def read_frame(self) -> bytes | None:
        """Next 20 ms frame, the last one padded with silence; None at end of file."""
        pcm = self._wav.readframes(FRAME_BYTES * self._rate // RATE)
        if not pcm:
            return None
        if self._rate != RATE:
            # State carries the filter across frames, so chunked output equals whole-file output.
            pcm, self._ratecv_state = audioop.ratecv(
                pcm, 2, 1, self._rate, RATE, self._ratecv_state
            )
        return audioop.lin2ulaw(pcm, 2).ljust(FRAME_BYTES, ULAW_SILENCE)

    def close(self) -> None:
        self._wav.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _wav_header(data_bytes: int) -> bytes:
    """Canonical 44-byte header for 16-bit mono PCM at 8 kHz."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        *(b"RIFF", 36 + data_bytes, b"WAVE"),
        *(b"fmt ", 16, 1, 1, RATE, RATE * 2, 2, 16),  # PCM, mono, byte rate, block align, bits
        *(b"data", data_bytes),
    )


def patch_wav_header(path: Path) -> None:
    """Set the header sizes from the file length: finishes a recording whose writer died."""
    data_bytes = max(path.stat().st_size - HEADER_BYTES, 0)
    with path.open("r+b") as f:
        f.write(_wav_header(data_bytes))


class RecordingWriter:
    """Appends mu-law frames as 16-bit PCM. Sizes are only known at the end, so close()
    uses the same patch_wav_header a peer server runs on a dead server's recordings."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file = path.open("wb")
        self._file.write(_wav_header(0))

    def write(self, ulaw: bytes) -> None:
        self._file.write(audioop.ulaw2lin(ulaw, 2))

    def close(self) -> None:
        self._file.close()
        patch_wav_header(self._path)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
