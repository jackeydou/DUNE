"""A recorded model backend for the deployment smoke test: an OpenAI-compatible server that
answers from `recording.json` and calls no model.

The recording is one trajectory of `cases/scorer_misbelief`: the assistant turns, in order. A
request is answered with the turn that follows the assistant turns it already holds, so any
number of runs can replay the recording at once.
"""

import json
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RECORDING: list[dict[str, Any]] = json.loads(
    Path(__file__).with_name("recording.json").read_text(encoding="utf-8")
)

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    turn = sum(1 for message in body["messages"] if message["role"] == "assistant")
    if turn >= len(RECORDING):
        return JSONResponse(
            {
                "error": {
                    "message": f"the recording has {len(RECORDING)} turns; asked for {turn + 1}"
                }
            },
            status_code=400,
        )
    message = RECORDING[turn]
    return JSONResponse(
        {
            "id": f"chatcmpl-recorded-{turn}",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "system_fingerprint": "fp_recorded",
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
