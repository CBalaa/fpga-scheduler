"""Backend contract. A prepare failure guarantees hardware was not started."""
from pathlib import Path
from typing import Protocol

from ..config import Node


class Backend(Protocol):
    nodes: tuple[Node, ...]
    # Backends advertising this capability must accept cancellation= in both
    # prepare/run, serialize all hardware operations in run, and never return
    # a cancelled terminal result before hardware cleanup has completed.
    supports_cancellation: bool

    def probe(self, node: str) -> list[dict]: ...
    def prepare(self, payload: dict, node: str, image: Path) -> None: ...
    def run(self, payload: dict, node: str, port: int) -> dict: ...
