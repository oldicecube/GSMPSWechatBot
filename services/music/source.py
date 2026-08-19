from typing import Protocol

from .models import ResolvedAudio, SongInfo


class MusicSourceError(RuntimeError):
    """An expected failure from one music source."""

    def __init__(self, source_id: str, reason: str):
        self.source_id = str(source_id)
        self.reason = str(reason).strip() or "未知错误"
        super().__init__(f"{self.source_id}: {self.reason}")


class MusicSource(Protocol):
    source_id: str
    name: str

    def resolve(
        self,
        song: SongInfo,
        quality: str,
        timeout: float,
    ) -> ResolvedAudio:
        """Resolve one normalized song to a downloadable audio URL."""
