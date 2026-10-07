"""Export the app's authored changelog into the backend deployment bundle."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to Evenly/Evenly/changelog.json")
    args = parser.parse_args()
    data = args.source.read_bytes()
    entries = json.loads(data)
    if not isinstance(entries, list) or not entries:
        parser.error("The source must contain a nonempty changelog array")
    target = Path(__file__).resolve().parents[1] / "app" / "resources" / "ios-changelog.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"Synced {len(entries)} changelog entries to {target}")


if __name__ == "__main__":
    main()
