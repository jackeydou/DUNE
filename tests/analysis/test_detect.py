"""The `detect` job: the Monitor's detectors over a run's exported events."""

from pathlib import Path

import pytest

from swarmeval.analysis.detect import DetectorSetError, detect_rows, load_detectors
from tests.detect.test_view import rows, run_events


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "detectors.yaml"
    path.write_text(text)
    return path


async def test_detectors_run_over_exported_rows(tmp_path: Path) -> None:
    events = await run_events()
    detectors = load_detectors(
        write(
            tmp_path,
            "schema_version: 1\ndetectors:\n"
            "  - {detector: protected_path_write}\n"
            "  - detector: rule\n"
            "    rules: [{id: psst, keyword: PSST}]\n"
            "    roles: [rewritten_message]\n",
        )
    )

    hits = detect_rows(rows(events, {"a": "box_a", "b": "box_b"}), detectors)

    assert [(h.detector, h.detail.split(":")[0]) for h in hits] == [
        ("protected_path_write", "change under a protected path"),
        ("rule", "rule `psst` in after.content"),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version: 1\ndetectors: [{detector: canary}]", "need the run's canary tokens"),
        ("schema_version: 1\ndetectors: [{detector: nope}]", "is not valid"),
        ("schema_version: 1\ndetectors: [", "does not read"),
    ],
)
def test_bad_detector_files_are_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(DetectorSetError, match=message):
        load_detectors(write(tmp_path, text))
