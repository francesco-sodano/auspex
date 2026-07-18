import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
FABRIC = ROOT / "fabric"
NOTEBOOKS = FABRIC / "notebooks"
CELL_MARKER = re.compile(r"^# (PARAMETERS CELL|CELL) \*+\s*$", re.MULTILINE)
METADATA_MARKER = re.compile(r"^# METADATA \*+\s*$", re.MULTILINE)
SOURCE_SEPARATOR = re.compile(r"^# COMMAND ----------\s*$", re.MULTILINE)


def _python_cells(path: Path) -> list[str]:
    return [
        cell.strip()
        for cell in SOURCE_SEPARATOR.split(path.read_text(encoding="utf-8"))
        if cell.strip()
    ]


def _ipynb_code_cells(path: Path) -> list[str]:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    return [
        "\n".join(cell.get("source", [])).strip()
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    ]


def _fabric_git_cells(path: Path) -> tuple[list[str], list[str]]:
    content = path.read_text(encoding="utf-8")
    matches = list(CELL_MARKER.finditer(content))
    cells = []
    markers = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[match.end():end]
        metadata = METADATA_MARKER.search(block)
        if metadata is None:
            raise AssertionError(f"Missing cell metadata in {path}")
        cells.append(block[:metadata.start()].strip())
        markers.append(match.group(1))

    return cells, markers


class FabricGitDefinitionTests(unittest.TestCase):
    def test_fabric_notebook_definitions_match_reviewed_sources(self):
        sources = {
            path.stem: _python_cells(path)
            for path in NOTEBOOKS.glob("nb_*.py")
        }
        triage = NOTEBOOKS / "nb_01a_form4_quarantine_triage.ipynb"
        sources[triage.stem] = _ipynb_code_cells(triage)

        for notebook_name, expected_cells in sorted(sources.items()):
            with self.subTest(notebook=notebook_name):
                item_folder = FABRIC / f"{notebook_name}.Notebook"
                definition = item_folder / "notebook-content.py"
                platform = item_folder / ".platform"

                self.assertTrue(platform.exists())
                self.assertTrue(definition.exists())

                actual_cells, markers = _fabric_git_cells(definition)
                self.assertEqual(expected_cells, actual_cells)

                expected_parameter_cells = sum(
                    "mark this cell as the Fabric parameter cell" in cell
                    for cell in expected_cells
                )
                self.assertEqual(expected_parameter_cells, markers.count("PARAMETERS CELL"))


if __name__ == "__main__":
    unittest.main()