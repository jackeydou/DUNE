import io

import pytest
from inspect_ai.log import read_eval_log
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.events import ObjectStore, RunHeader, export_key, export_run
from tests.events.test_store import run_loop, store_for

pytestmark = pytest.mark.docker


async def test_a_stored_run_exports_to_the_object_store(
    engine: AsyncEngine, run_id: str, object_store: ObjectStore
) -> None:
    await run_loop(store_for(engine, run_id), run_id)
    header = RunHeader(
        run_id=run_id,
        case_id="demo",
        variant=0,
        task_args={},
        epoch=1,
        epochs=1,
        input="Do the task.",
        models={"a": "test-model", "b": "test-model"},
    )

    key = await export_run(engine, header, object_store)

    assert key == export_key(run_id) == f"runs/{run_id}/sample.eval"
    fs = object_store.filesystem()
    with fs.open_input_stream(f"{object_store.bucket}/{key}") as src:
        log = read_eval_log(io.BytesIO(src.read()))
    assert log.status == "success"
    assert log.eval.metadata is not None
    assert log.eval.metadata["swarmeval"]["workspace"] == "ws_test"
    assert log.samples is not None
    assert log.samples[0].uuid == run_id
