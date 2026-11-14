"""Regenerate the bundled synthetic demo. No Excel or optional packages required."""
import json
from pathlib import Path
from app.synthetic import build_seed

TARGET = Path(__file__).resolve().parent.parent / "app" / "data" / "demo_seed.json"


def build():
    return build_seed()


def main():
    seed = build()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Synthetic demo: {len(seed['nodes'])} nodes, {sum(len(n['gpus']) for n in seed['nodes'])} GPUs, {len(seed['allocations'])} allocations")


if __name__ == "__main__":
    main()
