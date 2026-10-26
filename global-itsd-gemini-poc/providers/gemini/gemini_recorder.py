
"""Single mixed mono WAV recorder for Gemini Live RCMS sessions.

Customer PCM: 16 kHz, signed 16-bit mono.
Gemini PCM: 24 kHz, signed 16-bit mono.
Output: one 24 kHz, 16-bit mono WAV per session.

Audio chunks are placed on a shared monotonic timeline and summed.
"""
from __future__ import annotations

import logging
import os
import re
import struct
import tempfile
import time
import uuid
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

OUTPUT_RATE = 24000
SAMPLE_WIDTH = 2


def _safe_component(value: object, limit: int = 80) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "unknown"))
    return safe[:limit] or "unknown"


def _resample_s16le(
    pcm: bytes, source_rate: int, target_rate: int
) -> bytes:
    """Resample signed 16-bit little-endian mono PCM."""
    usable = len(pcm) - (len(pcm) % 2)
    pcm = pcm[:usable]

    if not pcm or source_rate == target_rate:
        return pcm

    samples = array("h")
    samples.frombytes(pcm)

    if os.sys.byteorder != "little":
        samples.byteswap()

    if not samples:
        return b""

    out_count = max(1, round(len(samples) * target_rate / source_rate))
    out = array("h")
    scale = source_rate / target_rate
    last = len(samples) - 1

    for i in range(out_count):
        pos = min(i * scale, last)
        lo = int(pos)
        hi = min(lo + 1, last)
        frac = pos - lo
        value = round(
            samples[lo] * (1.0 - frac) + samples[hi] * frac
        )
        out.append(max(-32768, min(32767, value)))

    if os.sys.byteorder != "little":
        out.byteswap()

    return out.tobytes()


class GeminiRecorder:
    """Create one mixed mono WAV file per session."""

    def __init__(
        self,
        session_id: str,
        endpoint_id: str,
        output_dir: str = "/data/recordings",
    ) -> None:
        root = Path(output_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        month_dir = root / now.strftime("%Y") / now.strftime("%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        suffix = uuid.uuid4().hex[:8]
        base = (
            f"{stamp}_{_safe_component(session_id)}_"
            f"{_safe_component(endpoint_id)}_{suffix}"
        )

        self.output_path = month_dir / f"{base}_mixed_mono_24k.wav"
        self.paths = (self.output_path,)
        self._closed = False
        self._failed = False
        self._start = time.monotonic()

        # Each event: (relative timestamp, sample rate, spool offset, byte length)
        self._events: List[Tuple[float, int, int, int]] = []

        self._spool = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".gemini-rec-",
            suffix=".tmp",
            dir=str(month_dir),
            delete=False,
        )
        self._spool_path = Path(self._spool.name)
        self._max_end_sample = 0

        logger.info(
            "Gemini mixed WAV recording started: %s",
            self.output_path.name,
        )

    def add_customer_pcm(self, pcm_bytes: bytes) -> None:
        self._add(pcm_bytes, 16000, "customer")

    def add_gemini_pcm(self, pcm_bytes: bytes) -> None:
        self._add(pcm_bytes, 24000, "Gemini")

    def _add(self, pcm_bytes: bytes, source_rate: int, label: str) -> None:
        if self._closed or self._failed or not pcm_bytes:
            return

        usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable <= 0:
            return

        try:
            timestamp = time.monotonic() - self._start

            self._spool.seek(0, os.SEEK_END)
            offset = self._spool.tell()
            self._spool.write(pcm_bytes[:usable])
            self._spool.flush()

            self._events.append(
                (timestamp, source_rate, offset, usable)
            )

            duration_samples = round(
                (usable // 2) * OUTPUT_RATE / source_rate
            )
            start_sample = max(0, round(timestamp * OUTPUT_RATE))
            self._max_end_sample = max(
                self._max_end_sample,
                start_sample + duration_samples,
            )

        except Exception as exc:
            logger.warning(
                "Gemini recorder spool write failed for %s: %s",
                label,
                exc,
            )
            self._failed = True

    @staticmethod
    def _mix_chunk(mix_file, start_sample: int, pcm: bytes) -> None:
        """Sum PCM into the output buffer, clipping to 16-bit range."""
        usable = len(pcm) - (len(pcm) % 2)
        values = array("h")
        values.frombytes(pcm[:usable])

        if os.sys.byteorder != "little":
            values.byteswap()

        mix_file.seek(start_sample * SAMPLE_WIDTH)
        existing_bytes = mix_file.read(len(values) * SAMPLE_WIDTH)

        existing = array("h")
        existing.frombytes(existing_bytes)

        if os.sys.byteorder != "little":
            existing.byteswap()

        if len(existing) < len(values):
            existing.extend([0] * (len(values) - len(existing)))

        for i, value in enumerate(values):
            existing[i] = max(
                -32768,
                min(32767, existing[i] + value),
            )

        if os.sys.byteorder != "little":
            existing.byteswap()

        mix_file.seek(start_sample * SAMPLE_WIDTH)
        mix_file.write(existing.tobytes())

    def close(self):
        """Finalize the WAV and remove temporary spool files."""
        if self._closed:
            return self.paths

        self._closed = True
        mix_raw_path: Optional[Path] = None

        try:
            self._spool.flush()

            if self._failed:
                raise OSError("Recording spool failed")

            fd, raw_name = tempfile.mkstemp(
                prefix=".gemini-mix-",
                suffix=".tmp",
                dir=str(self.output_path.parent),
            )
            os.close(fd)
            mix_raw_path = Path(raw_name)

            with open(mix_raw_path, "w+b") as mixed:
                mixed.truncate(self._max_end_sample * SAMPLE_WIDTH)

                for timestamp, source_rate, offset, length in self._events:
                    self._spool.seek(offset)
                    pcm = self._spool.read(length)

                    converted = _resample_s16le(
                        pcm,
                        source_rate,
                        OUTPUT_RATE,
                    )
                    start_sample = max(
                        0,
                        round(timestamp * OUTPUT_RATE),
                    )

                    self._mix_chunk(mixed, start_sample, converted)

            with wave.open(str(self.output_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(SAMPLE_WIDTH)
                wav.setframerate(OUTPUT_RATE)

                with open(mix_raw_path, "rb") as mixed:
                    while True:
                        block = mixed.read(1024 * 1024)
                        if not block:
                            break
                        wav.writeframesraw(block)

            logger.info(
                "Gemini mixed WAV finalized: %s (events=%d)",
                self.output_path.name,
                len(self._events),
            )

        except Exception as exc:
            logger.warning(
                "Could not finalize Gemini mixed WAV recording: %s",
                exc,
            )
            try:
                self.output_path.unlink(missing_ok=True)
            except OSError:
                pass

        finally:
            try:
                self._spool.close()
            except Exception:
                pass

            try:
                self._spool_path.unlink(missing_ok=True)
            except OSError:
                pass

            if mix_raw_path is not None:
                try:
                    mix_raw_path.unlink(missing_ok=True)
                except OSError:
                    pass

        return self.paths
