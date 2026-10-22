from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    user: str
    groups: tuple[str, ...]
