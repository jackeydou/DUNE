import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
import httpx2
import pytest

from swarmeval.gateway.model.app import create_app
from swarmeval.gateway.model.client import GatewaySession, ModelGatewayError
from swarmeval.gateway.model.config import GatewayConfig, ReasoningPassback
from swarmeval.gateway.model.recorder import Attachments, NotAttachedError, Recorder
from swarmeval.gateway.model.upstream import Upstreams, passback
from swarmeval.gateway.model.wire import (
    CALL_ID_HEADER,
    WireAssistantMessage,
    WireMessage,
    WireUserMessage,
)
from swarmeval.proto.swarmeval.modelgw.v1 import recorder_pb2 as pb
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import (
    add_RecorderServiceServicer_to_server,
)
from swarmeval.runtime.loop import RunLoop
from swarmeval.runtime.messages import (
    AssistantMessage,
    ModelRequest,
    RequestOptions,
    SystemMessage,
    ToolCall,
    ToolMessage,
    ToolSchema,
    UserMessage,
)
from swarmeval.runtime.ports import AgentCaller, Caller, ExtensionCaller
from swarmeval.runtime.records import ModelCallRecord
from swarmeval.runtime.specs import RunSpec
from swarmeval.runtime.tools import BUILTIN_TOOLS
from swarmeval.runtime.writer import RunWriter
from tests.gateway.mock_backend import MockBackend, completion, tool_call
from tests.runtime.fakes import FakePauser, FakeSandbox, FakeStore, agent

CONFIG = GatewayConfig.model_validate(
    {
        "backends": {
            "mock": {
                "base_url": "http://backend/v1",
                "reasoning_passback": "within_turn",
                "weights_hash": "sha256:abc",
                "defaults": {"temperature": 0.6, "seed": 42},
                "max_retries": 1,
            }
        },
        "models": {"qwen-test": {"backend": "mock", "upstream_model": "Org/Upstream-Model"}},
    }
)
DEV = AgentCaller("dev")
CALLERS: tuple[Caller, ...] = (DEV, ExtensionCaller("acme.judge"))


@dataclass
class Rig:
    backend: MockBackend
    store: FakeStore
    writer: RunWriter
    http: httpx2.AsyncClient
    channel: grpc.aio.Channel
    attachments: Attachments

    def session(self, *, run_id: str = "run_1", owner_epoch: int = 1) -> GatewaySession:
        return GatewaySession(
            http=self.http,
            channel=self.channel,
            run_id=run_id,
            owner_epoch=owner_epoch,
            callers=CALLERS,
            writer=self.writer,
        )


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    backend = MockBackend()
    attachments = Attachments(ack_timeout_s=5)
    upstreams = Upstreams(CONFIG, transports={"mock": httpx2.ASGITransport(app=backend.app())})
    server = grpc.aio.server()
    add_RecorderServiceServicer_to_server(Recorder(attachments), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    app = create_app(attachments, upstreams)
    store = FakeStore()
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://gateway", timeout=None
        ) as http,
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
    ):
        yield Rig(backend, store, RunWriter(store), http, channel, attachments)
    await server.stop(None)
    await upstreams.close()


def request(
    *messages: SystemMessage | UserMessage | AssistantMessage | ToolMessage,
) -> ModelRequest:
    return ModelRequest(
        model="qwen-test",
        messages=messages or (SystemMessage(content="sys"), UserMessage(content="go")),
        gen=0,
        tools=(ToolSchema(name="shell", description="Run.", parameters={"type": "object"}),),
        options=RequestOptions(tools=("shell",), top_p=0.9),
    )


async def test_a_call_is_committed_before_its_response_returns(rig: Rig) -> None:
    rig.backend.reply(
        completion(
            "",
            reasoning="let me look",
            tool_calls=[tool_call("shell", '{"cmd": "ls"}', id="c1")],
            prompt_tokens=12,
            completion_tokens=3,
        )
    )

    async with rig.session() as session:
        recorded = await session.generate(DEV, request(), parent_id=None)

    message = recorded.response.message
    assert message.reasoning == "let me look"
    assert message.tool_calls == (ToolCall(id="c1", name="shell", arguments='{"cmd": "ls"}'),)
    assert recorded.response.usage.total == 15
    (event,) = rig.store.events
    assert event == recorded.event
    assert event.agent_id == "dev"
    record = event.record
    assert isinstance(record, ModelCallRecord)
    assert (record.model, record.gen, record.length) == ("qwen-test", 0, 2)
    upstream = record.gateway.upstream
    assert (upstream.backend, upstream.model, upstream.served_model) == (
        "mock",
        "Org/Upstream-Model",
        "Org/Upstream-Model",
    )
    assert upstream.sampling == {"temperature": 0.6, "top_p": 0.9, "seed": 42}
    assert (upstream.weights_hash, upstream.system_fingerprint) == ("sha256:abc", "fp_mock")
    assert record.gateway.attempts == 1
    assert json.loads(record.gateway.upstream_response_json)["model"] == "Org/Upstream-Model"
    (sent,) = rig.backend.requests
    assert sent["model"] == "Org/Upstream-Model"
    assert (sent["temperature"], sent["top_p"], sent["seed"]) == (0.6, 0.9, 42)
    assert sent["tools"][0]["function"]["name"] == "shell"


async def test_only_the_current_turns_reasoning_goes_back_upstream(rig: Rig) -> None:
    rig.backend.reply(completion("done"))
    earlier = AssistantMessage(content="hi", reasoning="old thoughts")
    current = AssistantMessage(
        reasoning="current thoughts", tool_calls=(ToolCall(id="c1", name="shell", arguments="{}"),)
    )

    async with rig.session() as session:
        await session.generate(
            DEV,
            request(
                UserMessage(content="first"),
                earlier,
                UserMessage(content="second"),
                current,
                ToolMessage(tool_call_id="c1", content="out"),
            ),
            parent_id=None,
        )

    sent = rig.backend.requests[0]["messages"]
    assert "reasoning_content" not in sent[1]
    assert sent[3]["reasoning_content"] == "current thoughts"
    assert "content" not in sent[3]


async def test_reasoning_under_the_newer_field_name_is_read(rig: Rig) -> None:
    rig.backend.reply(completion("ok", reasoning="hmm", reasoning_field="reasoning"))

    async with rig.session() as session:
        recorded = await session.generate(DEV, request(), parent_id=None)

    assert recorded.response.message.reasoning == "hmm"


async def test_nul_in_model_output_is_replaced_before_it_is_stored(rig: Rig) -> None:
    rig.backend.reply(completion("a\x00b", tool_calls=[tool_call("shell", '{"cmd":"\x00"}')]))

    async with rig.session() as session:
        recorded = await session.generate(DEV, request(), parent_id=None)

    assert recorded.response.message.content == "a�b"
    assert recorded.response.message.tool_calls[0].arguments == '{"cmd":"�"}'


async def test_extension_calls_are_attributed_to_the_instance(rig: Rig) -> None:
    rig.backend.reply(completion("verdict"))

    async with rig.session() as session:
        await session.generate(ExtensionCaller("acme.judge"), request(), parent_id="evt_trigger")

    (event,) = rig.store.events
    assert (event.agent_id, event.extension) == (None, "acme.judge")
    assert event.parent_id == "evt_trigger"


async def test_retries_are_counted_and_only_the_final_response_is_recorded(rig: Rig) -> None:
    rig.backend.reply({"error": {"message": "overloaded"}}, status=503)
    rig.backend.reply(completion("second try"))

    async with rig.session() as session:
        recorded = await session.generate(DEV, request(), parent_id=None)

    assert recorded.response.message.content == "second try"
    (event,) = rig.store.events
    assert isinstance(event.record, ModelCallRecord)
    assert event.record.gateway.attempts == 2


async def test_a_backend_error_fails_the_call_without_a_record(rig: Rig) -> None:
    rig.backend.reply({"error": {"message": "context too long"}}, status=400)

    async with rig.session() as session:
        with pytest.raises(ModelGatewayError, match="answered 502") as info:
            await session.generate(DEV, request(), parent_id=None)

    assert info.value.status == 502
    assert "context too long" in str(info.value)
    assert rig.store.events == []


async def test_an_unknown_model_is_404(rig: Rig) -> None:
    async with rig.session() as session:
        with pytest.raises(ModelGatewayError, match="model_not_found"):
            await session.generate(
                DEV, request().model_copy(update={"model": "gpt-nope"}), parent_id=None
            )

    assert rig.backend.requests == []


async def test_without_an_attached_stream_the_backend_is_never_called(rig: Rig) -> None:
    session = rig.session()
    async with session:
        pass

    with pytest.raises(ModelGatewayError) as info:
        await session.generate(DEV, request(), parent_id=None)

    assert info.value.status == 503
    assert "run_not_attached" in str(info.value)
    assert rig.backend.requests == []


async def test_a_record_the_worker_cannot_commit_fails_the_call(rig: Rig) -> None:
    rig.backend.reply(completion("lost"))

    async def broken(txn: object) -> list[object]:
        raise RuntimeError("database is gone")

    rig.store.commit = broken  # type: ignore[method-assign]
    async with rig.session() as session:
        with pytest.raises(ModelGatewayError, match="not committed: database is gone") as info:
            await session.generate(DEV, request(), parent_id=None)
        assert isinstance(info.value.__cause__, RuntimeError)
        with pytest.raises(ModelGatewayError, match="failed earlier"):
            await session.generate(DEV, request(), parent_id=None)


@pytest.mark.parametrize(
    ("epoch", "outcome"),
    [(1, "refused"), (2, "replaces")],
)
async def test_a_second_stream_for_a_run_needs_a_higher_epoch(
    rig: Rig, epoch: int, outcome: str
) -> None:
    async with rig.session(owner_epoch=1) as first:
        if outcome == "refused":
            with pytest.raises(ModelGatewayError, match="already attached at owner_epoch 1"):
                async with rig.session(owner_epoch=epoch):
                    pass
            return
        async with rig.session(owner_epoch=epoch) as second:
            rig.backend.reply(completion("from the new owner"))
            recorded = await second.generate(DEV, request(), parent_id=None)
            assert recorded.response.message.content == "from the new owner"
            with pytest.raises(ModelGatewayError, match="failed earlier") as info:
                await first.generate(DEV, request(), parent_id=None)
            assert "replaced by owner_epoch 2" in str(info.value.__cause__)


@pytest.mark.parametrize(
    ("auth", "call_id", "body", "code"),
    [
        (True, None, b"{}", "missing_call_id"),
        (True, "c", b'{"model": "qwen-test", "messages": [], "n": 2}', "invalid_request"),
        (False, "c", b"{}", "run_not_attached"),
    ],
)
async def test_malformed_requests_are_refused(
    rig: Rig, auth: bool, call_id: str | None, body: bytes, code: str
) -> None:
    async with rig.session() as session:
        headers: dict[str, str] = {}
        if auth:
            key = session._keys["agent:dev"]  # pyright: ignore[reportPrivateUsage]
            headers["Authorization"] = f"Bearer {key}"
        if call_id is not None:
            headers[CALL_ID_HEADER] = call_id
        reply = await rig.http.post("/v1/chat/completions", content=body, headers=headers)

    assert reply.json()["error"]["code"] == code
    assert rig.backend.requests == []


async def test_models_lists_the_configured_names(rig: Rig) -> None:
    reply = await rig.http.get("/v1/models")

    assert [m["id"] for m in reply.json()["data"]] == ["qwen-test"]


async def test_a_run_loop_drives_an_agent_through_the_gateway(rig: Rig) -> None:
    rig.backend.reply(completion("", tool_calls=[tool_call("shell", '{"cmd": "ls"}', id="c1")]))
    rig.backend.reply(completion("all done"))
    sandbox = FakeSandbox()

    async with rig.session() as session:
        loop = RunLoop(
            RunSpec(run_id="run_1", seed=1, agents=(agent("dev", model="qwen-test"),)),
            writer=rig.writer,
            model_client=session,
            sandbox_executor=sandbox,
            pauser=FakePauser(),
            tools=BUILTIN_TOOLS,
        )
        outcome = await loop.run()

    assert outcome.status == "finished"
    assert [c.argv for _, _, c in sandbox.calls] == [("sh", "-c", "ls")]
    models = rig.store.records("model")
    assert len(models) == 2
    assert all(isinstance(e.record, ModelCallRecord) for e in models)
    second = rig.backend.requests[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[3] == {"role": "tool", "tool_call_id": "c1", "content": "sh -c ls"}


@pytest.mark.parametrize(
    ("mode", "kept"),
    [("none", [None, None]), ("within_turn", [None, "b"]), ("all", ["a", "b"])],
)
def test_reasoning_passback_modes(mode: ReasoningPassback, kept: list[str | None]) -> None:
    messages: list[WireMessage] = [
        WireUserMessage(content="1"),
        WireAssistantMessage(content="x", reasoning_content="a"),
        WireUserMessage(content="2"),
        WireAssistantMessage(content="y", reasoning_content="b"),
    ]

    adapted = passback(messages, mode)

    assert [m.reasoning_content for m in adapted if isinstance(m, WireAssistantMessage)] == kept


async def test_a_call_waiting_for_its_ack_fails_when_the_stream_goes() -> None:
    attachments = Attachments()
    hello = pb.Hello(run_id="r", owner_epoch=1, keys=[pb.CallerKey(key="k", caller="agent:a")])
    attachment = attachments.attach(hello)
    waiting = asyncio.create_task(attachments.record(attachment, pb.CallRecord(call_id="c1")))
    await asyncio.sleep(0)

    attachments.detach(attachment, "stream ended")

    with pytest.raises(NotAttachedError, match="closed \\(stream ended\\) before it acknowledged"):
        await waiting
    assert attachments.lookup("k") is None
