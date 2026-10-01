"""Runs the example cases in cases.json against durations.parse_duration."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from durations import parse_duration

cases = json.loads((Path(__file__).parent / "cases.json").read_text())
passed = 0
for text, expected in cases.items():
    try:
        got: object = parse_duration(text)
    except ValueError:
        got = "ValueError"
    except Exception as err:
        got = f"{type(err).__name__}: {err}"
    ok = got == expected
    passed += ok
    print(f"{'PASS' if ok else 'FAIL'} {text!r}: expected {expected!r}, got {got!r}")
print(f"{passed}/{len(cases)} passed")
