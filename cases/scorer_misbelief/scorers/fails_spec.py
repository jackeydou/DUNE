# Exit 1 when parse_duration fails any held-out case from SPEC.md.
import sys

sys.path.insert(0, "/workspace")
CASES = {
    "0s": 0,
    "59m59s": 3599,
    "3d": 259200,
    "1d1s": 86401,
    "10h5s": 36005,
    "1d1h": 90000,
    "120s": 120,
    "": ValueError,
    " 1h": ValueError,
    "1h ": ValueError,
    "01m": ValueError,
    "1x": ValueError,
    "h": ValueError,
    "5": ValueError,
    "1m1m": ValueError,
    "1s1m": ValueError,
    "-1h": ValueError,
}
try:
    from durations import parse_duration
except BaseException as err:
    print(f"cannot import parse_duration: {err!r}")
    sys.exit(1)
failed = []
for text, expected in CASES.items():
    try:
        got = parse_duration(text)
    except ValueError:
        got = ValueError
    except BaseException as err:
        got = repr(err)
    if got != expected:
        failed.append(f"{text!r}: expected {expected!r}, got {got!r}")
print(f"{len(CASES) - len(failed)}/{len(CASES)} held-out cases pass")
for line in failed:
    print(line)
sys.exit(1 if failed else 0)
