"""The `detect` job: the Monitor's detectors over exported runs, offline
(docs/services/analysis.md#capabilities, M2 spec decision 6).

Detectors read each event of a run's `events.parquet` in `seq` order through the offline
adapter, so they see what the online Monitor would have. `canary` and `cross_sandbox` need the
run's tokens, which exports do not hold, and are refused.

```yaml
schema_version: 1
detectors:
  - { detector: zero_width }
  - { detector: acrostic, words: [hold, raise] }
```
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from swarmeval.detect.defs import NEEDS_RUN_TOKENS, DetectorDef
from swarmeval.detect.detectors import Hit, build, run_detectors
from swarmeval.detect.view import view_of_row


class DetectorSetError(Exception):
    """A detector file that does not read, validate, or run offline."""


class DetectorSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    detectors: Annotated[tuple[DetectorDef, ...], Field(min_length=1)]


def load_detectors(path: Path) -> DetectorSet:
    try:
        loaded = DetectorSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as err:
        raise DetectorSetError(f"detector file {path} does not read: {err}") from err
    except ValidationError as err:
        raise DetectorSetError(
            f"detector file {path} is not valid:\n{err}\nSee docs/services/analysis.md#detectors."
        ) from err
    online = sorted({d.detector for d in loaded.detectors} & NEEDS_RUN_TOKENS)
    if online:
        raise DetectorSetError(
            f"detector file {path} lists {', '.join(online)}, which need the run's canary tokens; "
            "exports do not hold them. Use them in a case's `rule` scorer or the Monitor."
        )
    return loaded


def detect_rows(rows: Sequence[Mapping[str, object]], detectors: DetectorSet) -> list[Hit]:
    """`rows` are one run's `events.parquet` rows in `seq` order. A fresh set of detectors per
    run, as each run has its own Monitor."""
    built = [build(d, None) for d in detectors.detectors]
    return run_detectors(built, [view_of_row(r) for r in rows])
