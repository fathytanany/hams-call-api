"""Callee WAV files in, 20 ms frames of 8 kHz mu-law out (the Media Streams format)."""

import audioop
import wave
from pathlib import Path
from types import TracebackType
from typing import Self

RATE = 8000
FRAME_BYTES = RATE * 20 // 1000  # 160 samples, one mu-law byte each
ULAW_SILENCE = b"\xff"


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

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
