from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FABRIC = ROOT / "fabric"
CELL_MARKER = re.compile(r"^# (PARAMETERS CELL|CELL) \*+\s*$", re.MULTILINE)
METADATA_MARKER = re.compile(r"^# METADATA \*+\s*$", re.MULTILINE)


def notebook_cells(name: str) -> list[tuple[str, str]]:
    path = FABRIC / f"{name}.Notebook" / "notebook-content.py"
    content = path.read_text(encoding="utf-8")
    matches = list(CELL_MARKER.finditer(content))
    cells = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[match.end():end]
        metadata = METADATA_MARKER.search(block)
        if metadata is None:
            raise AssertionError(f"Missing cell metadata in {path}")
        cells.append((match.group(1), block[:metadata.start()].strip()))

    return cells


def notebook_code(name: str) -> str:
    return "\n\n".join(code for _, code in notebook_cells(name))
