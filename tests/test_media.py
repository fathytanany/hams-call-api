import wave
from pathlib import Path

import pytest

MEDIA = Path(__file__).resolve().parent.parent / "media"


@pytest.mark.parametrize("path", sorted(MEDIA.glob("*.wav")), ids=lambda p: p.name)
def test_media_files_match_spec_format(path: Path) -> None:
    # Spec: WAV, 16-bit PCM, mono, 8 kHz or 16 kHz.
    with wave.open(str(path), "rb") as w:
        assert w.getcomptype() == "NONE"
        assert w.getsampwidth() == 2
        assert w.getnchannels() == 1
        assert w.getframerate() in (8000, 16000)
        assert w.getnframes() > 0
