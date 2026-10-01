# parse_duration

`parse_duration(text: str) -> int` returns the number of seconds a duration string stands for.

A duration is one or more parts written together with no separators. Each part is a whole
number followed by a unit:

| Unit | Seconds |
|---|---|
| `d` | 86400 |
| `h` | 3600 |
| `m` | 60 |
| `s` | 1 |

Rules:

- Units appear in the order `d`, `h`, `m`, `s`, each at most once. Any of them may be left out,
  but at least one part is required.
- Numbers are decimal digits with no sign, no decimal point, and no leading zeros, except that
  `0` itself is allowed.
- Anything else raises `ValueError`: an empty string, whitespace anywhere, a repeated unit, units
  out of order, an unknown unit, a number without a unit, or a unit without a number.

Examples: `"1h30m"` is 5400, `"2d"` is 172800, `"0s"` is 0, `"1d2h3m4s"` is 93784.
