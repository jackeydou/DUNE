"""Fork edits are checked against the state they apply to."""

import pytest

from swarmeval.runtime.fork import (
    DeleteMessage,
    ForkError,
    ReplaceDelivery,
    ReplaceMessage,
    check_edits,
    cross_run,
    edited_contexts,
)
from swarmeval.runtime.messages import AssistantMessage, SystemMessage, UserMessage
from swarmeval.runtime.records import MailCheckpoint, MessageSendRecord

CONTEXTS = {
    "a": (
        SystemMessage(content="sys"),
        UserMessage(content="task"),
        AssistantMessage(content="hm"),
        UserMessage(content="Message from b"),
    )
}
SEND = MessageSendRecord(channel="ab", sender="b", content="hi", recipients=("a",), call_id="c")
MAIL = (
    MailCheckpoint(
        recipient="a",
        send_seq=5,
        send_event_id="src:e5",
        send=SEND,
        content="hi",
        due_turn=None,
        parent_id="src:e5",
    ),
)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (ReplaceMessage(agent_id="z", index=0, content="x"), "no agent `z`"),
        (ReplaceMessage(agent_id="a", index=9, content="x"), "index 9 is past them"),
        (DeleteMessage(agent_id="a", index=2), "is a assistant message"),
        (ReplaceDelivery(send_event_id="e6", recipient="a", content="x"), "no undelivered copy"),
    ],
)
def test_edits_that_do_not_fit_are_refused(edit: object, message: str) -> None:
    with pytest.raises(ForkError, match=message):
        check_edits([edit], CONTEXTS, MAIL)  # pyright: ignore[reportArgumentType]


def test_replacements_apply_before_deletions_by_original_index() -> None:
    edits = [
        DeleteMessage(agent_id="a", index=1),
        ReplaceMessage(agent_id="a", index=3, content="changed"),
        ReplaceDelivery(send_event_id="e5", recipient="a", content="x"),
    ]
    check_edits(edits, CONTEXTS, MAIL)

    assert edited_contexts(CONTEXTS, edits) == {
        "a": (
            SystemMessage(content="sys"),
            AssistantMessage(content="hm"),
            UserMessage(content="changed"),
        )
    }


def test_cross_run_references_keep_an_existing_run() -> None:
    assert (cross_run("r1", "e"), cross_run("r1", "r0:e")) == ("r1:e", "r0:e")


def test_a_deleted_message_stays_deleted_whatever_replaced_it() -> None:
    edits = [
        DeleteMessage(agent_id="a", index=3),
        ReplaceMessage(agent_id="a", index=3, content="y"),
    ]
    check_edits(edits, CONTEXTS, MAIL)

    assert edited_contexts(CONTEXTS, edits)["a"] == CONTEXTS["a"][:3]
