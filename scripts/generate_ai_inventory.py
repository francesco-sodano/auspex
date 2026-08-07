import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.auspex_api.recommender.policy import MODEL_VERSION as POLICY_VERSION
from engine.company_package import MODEL_VERSION as SCORE_VERSION, WEIGHT_VERSION


BEGIN = "<!-- BEGIN GENERATED DETERMINISTIC INVENTORY -->"
END = "<!-- END GENERATED DETERMINISTIC INVENTORY -->"


def render():
    return "\n".join([
        BEGIN,
        f"| Deterministic policy | `{POLICY_VERSION}` | Personalized portfolio actions and amounts | Recommendation | risk-profile policy; coverage, raw-composite, financing and cost gates; no execution |",
        f"| Deterministic company engine | `{SCORE_VERSION}` / `{WEIGHT_VERSION}` | Fresh 90-day six-leg company outlook across the research universe | Research classification | compact active windows; per-leg evidence lineage; PIT checks; theme-relative normalization; append-on-change packages |",
        END,
    ])


def update(text):
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    return text[:start] + render() + text[end:]


def main():
    parser = argparse.ArgumentParser(description="Generate deterministic AI inventory rows")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--document", default=str(ROOT / "doc" / "compliance-mvp.md")
    )
    args = parser.parse_args()
    path = Path(args.document)
    original = path.read_text(encoding="utf-8")
    generated = update(original)
    if args.check:
        if original != generated:
            raise SystemExit("AI inventory is stale; run scripts/generate_ai_inventory.py")
        print("AI inventory is current")
        return
    path.write_text(generated, encoding="utf-8")
    print(f"Updated {path}")


if __name__ == "__main__":
    main()
