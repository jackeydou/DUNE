"""Attached runs and the `RecorderService.Attach` stream (docs/services/model-gateway.md).

A run's keys accept calls only while its worker's stream is attached. Every completed call is
sent to that worker as a `CallRecord`, and the caller's HTTP response waits for the worker's
`Ack`, which it sends after committing the record. If the stream goes away first, the call
fails closed.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import grpc

from swarmeval.proto.swarmeval.modelgw.v1 import recorder_pb2 as pb
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import RecorderServiceServicer

log = logging.getLogger(__name__)


class NotAttachedError(Exception):
    """No attached run can take this call's record, or its stream went away before the worker
    acknowledged it."""


class AttachRefusedError(Exception):
    def __init__(self, message: str, code: grpc.StatusCode) -> None:
        super().__init__(message)
        self.code = code


@dataclass(eq=False)
class Attachment:
    run_id: str
    owner_epoch: int
    callers: dict[str, str]
    """Virtual key → caller."""
    outbox: asyncio.Queue[pb.AttachResponse | None] = field(
        default_factory=asyncio.Queue[pb.AttachResponse | None]
    )
    """`None` ends the stream."""
    pending: dict[str, asyncio.Future[str]] = field(default_factory=dict[str, asyncio.Future[str]])
    closed_reason: str | None = None

    def close(self, reason: str) -> None:
        if self.closed_reason is not None:
            return
        self.closed_reason = reason
        for call_id, waiter in self.pending.items():
            if not waiter.done():
                waiter.set_exception(
                    NotAttachedError(
                        f"run `{self.run_id}`: the worker's stream closed ({reason}) before it "
                        f"acknowledged call `{call_id}`."
                    )
                )
        self.pending.clear()
        self.outbox.put_nowait(None)


class Attachments:
    """Every attached run on this replica. One replica per deployment in v1, so a call and its
    run's stream always meet here."""

    def __init__(self, ack_timeout_s: float = 120.0) -> None:
        self._by_run: dict[str, Attachment] = {}
        self._by_key: dict[str, Attachment] = {}
        self._ack_timeout_s = ack_timeout_s

    def attach(self, hello: pb.Hello) -> Attachment:
        if not hello.run_id or not hello.keys:
            raise AttachRefusedError(
                f"Hello for run `{hello.run_id}` must name the run and at least one key.",
                grpc.StatusCode.INVALID_ARGUMENT,
            )
        current = self._by_run.get(hello.run_id)
        if current is not None and current.owner_epoch >= hello.owner_epoch:
            raise AttachRefusedError(
                f"run `{hello.run_id}` is already attached at owner_epoch {current.owner_epoch}; "
                f"a stream at epoch {hello.owner_epoch} cannot replace it.",
                grpc.StatusCode.FAILED_PRECONDITION,
            )
        for key in hello.keys:
            holder = self._by_key.get(key.key)
            if holder is not None and holder.run_id != hello.run_id:
                raise AttachRefusedError(
                    f"run `{hello.run_id}`: a key for `{key.caller}` is already held by run "
                    f"`{holder.run_id}`. Generate fresh keys per run.",
                    grpc.StatusCode.INVALID_ARGUMENT,
                )
        if current is not None:
            self.detach(current, f"replaced by owner_epoch {hello.owner_epoch}")
        attachment = Attachment(
            run_id=hello.run_id,
            owner_epoch=hello.owner_epoch,
            callers={k.key: k.caller for k in hello.keys},
        )
        self._by_run[hello.run_id] = attachment
        for key in attachment.callers:
            self._by_key[key] = attachment
        return attachment

    def detach(self, attachment: Attachment, reason: str) -> None:
        if self._by_run.get(attachment.run_id) is attachment:
            del self._by_run[attachment.run_id]
        for key in attachment.callers:
            if self._by_key.get(key) is attachment:
                del self._by_key[key]
        attachment.close(reason)

    def lookup(self, key: str) -> tuple[Attachment, str] | None:
        attachment = self._by_key.get(key)
        if attachment is None:
            return None
        return attachment, attachment.callers[key]

    async def record(self, attachment: Attachment, record: pb.CallRecord) -> str:
        """Sends `record` to the run's worker and returns the committed event's id."""
        if attachment.closed_reason is not None:
            raise NotAttachedError(
                f"run `{attachment.run_id}` detached ({attachment.closed_reason})."
            )
        if record.call_id in attachment.pending:
            raise NotAttachedError(
                f"run `{attachment.run_id}`: call id `{record.call_id}` is already in flight."
            )
        waiter = asyncio.get_running_loop().create_future()
        attachment.pending[record.call_id] = waiter
        attachment.outbox.put_nowait(pb.AttachResponse(record=record))
        try:
            return await asyncio.wait_for(waiter, self._ack_timeout_s)
        except TimeoutError as err:
            raise NotAttachedError(
                f"run `{attachment.run_id}`: no ack for call `{record.call_id}` within "
                f"{self._ack_timeout_s:g} s."
            ) from err
        finally:
            attachment.pending.pop(record.call_id, None)

    def answer(self, attachment: Attachment, message: pb.AttachRequest) -> None:
        match message.WhichOneof("item"):
            case "ack":
                call_id, outcome = message.ack.call_id, message.ack.event_id
            case "reject":
                call_id = message.reject.call_id
                outcome = NotAttachedError(
                    f"run `{attachment.run_id}`: the worker rejected call `{call_id}`: "
                    f"{message.reject.reason}"
                )
            case other:
                raise AttachRefusedError(
                    f"run `{attachment.run_id}`: `{other}` is only valid as the first message.",
                    grpc.StatusCode.INVALID_ARGUMENT,
                )
        waiter = attachment.pending.get(call_id)
        if waiter is None or waiter.done():
            log.warning(
                "run %s: answer for call %s, which is not waiting", attachment.run_id, call_id
            )
            return
        if isinstance(outcome, Exception):
            waiter.set_exception(outcome)
        else:
            waiter.set_result(outcome)


class Recorder(RecorderServiceServicer):
    def __init__(self, attachments: Attachments) -> None:
        self._attachments = attachments

    async def Attach(
        self,
        request_iterator: AsyncIterator[pb.AttachRequest],
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> AsyncIterator[pb.AttachResponse]:
        first = await anext(aiter(request_iterator), None)
        if first is None or first.WhichOneof("item") != "hello":
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "the first message on Attach must be a Hello."
            )
            return
        try:
            attachment = self._attachments.attach(first.hello)
        except AttachRefusedError as err:
            await context.abort(err.code, str(err))
            return
        reader = asyncio.create_task(self._read(attachment, request_iterator))
        try:
            yield pb.AttachResponse(attached=pb.Attached())
            while (item := await attachment.outbox.get()) is not None:
                yield item
        finally:
            reader.cancel()
            self._attachments.detach(attachment, "stream ended")
        if attachment.closed_reason and attachment.closed_reason.startswith("replaced"):
            await context.abort(grpc.StatusCode.ABORTED, attachment.closed_reason)
        if reader.done() and not reader.cancelled() and (err := reader.exception()) is not None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))

    async def _read(
        self, attachment: Attachment, request_iterator: AsyncIterator[pb.AttachRequest]
    ) -> None:
        try:
            async for message in request_iterator:
                self._attachments.answer(attachment, message)
        finally:
            self._attachments.detach(attachment, "the worker closed its side")
