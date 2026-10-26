"""Backend contract. A prepare failure guarantees hardware was not started."""
from pathlib import Path
from typing import Protocol

from ..config import Node


class Backend(Protocol):
    nodes: tuple[Node, ...]

    def probe(self, node: str) -> list[dict]: ...
    def prepare(self, payload: dict, node: str, image: Path) -> None: ...
    def run(self, payload: dict, node: str, port: int) -> dict: ...
