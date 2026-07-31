from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .kicad_sch_sexp import SExp, as_text, dump_many, list_head, parse_one


@dataclass
class KicadSchematic:
    root: List[SExp]

    @classmethod
    def from_text(cls, text: str) -> "KicadSchematic":
        node = parse_one(text)
        if not isinstance(node, list) or list_head(node) != "kicad_sch":
            raise ValueError("Expected top-level (kicad_sch ...)")
        return cls(root=node)

    @classmethod
    def from_file(cls, path: Path) -> "KicadSchematic":
        return cls.from_text(path.read_text(encoding="utf-8"))

    def to_text(self) -> str:
        return dump_many([self.root]) + "\n"

    def to_file(self, path: Path) -> None:
        path.write_text(self.to_text(), encoding="utf-8")

    def top_level_nodes(self, head: Optional[str] = None) -> List[List[SExp]]:
        out: List[List[SExp]] = []
        for child in self.root[1:]:
            if not isinstance(child, list):
                continue
            if head is None or list_head(child) == head:
                out.append(child)
        return out

    def find_first(self, head: str) -> Optional[List[SExp]]:
        for node in self.top_level_nodes(head):
            return node
        return None

    def append_top_level(self, node: List[SExp]) -> None:
        self.root.append(node)

    def remove_top_level(self, node: List[SExp]) -> None:
        self.root.remove(node)


def child_lists(node: List[SExp], head: Optional[str] = None) -> List[List[SExp]]:
    out: List[List[SExp]] = []
    for item in node[1:]:
        if not isinstance(item, list):
            continue
        if head is None or list_head(item) == head:
            out.append(item)
    return out


def find_child(node: List[SExp], head: str) -> Optional[List[SExp]]:
    for item in child_lists(node, head=head):
        return item
    return None


def get_property_value(node: List[SExp], prop_name: str) -> Optional[str]:
    for prop in child_lists(node, "property"):
        if len(prop) >= 3 and as_text(prop[1]) == prop_name:
            return as_text(prop[2])
    return None

