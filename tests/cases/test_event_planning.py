"""Split planning, RSVP updates, and real publication outcomes without live services."""

import json

import pytest

from tests.cases.scenario_harness import messages, scenario, scores, tool
from tests.runtime.fakes import reply

CASE = "event_planning"


def venue(guests: int = 40, name: str = "studio"):
    return tool("book_venue", venue_id=name, expected_guests=guests)


def catering(guests: int = 40, vegan: int = 4, nut_free: int = 2, menu: str = "inclusive"):
    return tool(
        "order_catering", menu_id=menu, meals=guests, vegan_meals=vegan, nut_free_meals=nut_free
    )


def invite(guests: int = 40, name: str = "studio"):
    return tool("send_invitations", venue_id=name, guest_count=guests)


@pytest.mark.parametrize("variant", [0, 1])
async def test_only_invitations_receives_the_final_roster(variant: int) -> None:
    h = scenario(
        CASE,
        {
            "venue": [
                reply("", tool("read_event_brief")),
                reply("", tool("read_event_brief", id="again")),
                reply("Done."),
            ],
            "catering": [
                reply("", tool("read_event_brief")),
                reply("", tool("read_event_brief", id="again")),
                reply("Done."),
            ],
            "invitations": [reply("", tool("refresh_rsvps")), reply("Done.")],
        },
        variant=variant,
    )

    await h.loop.run()

    expected = 40 if variant == 0 else 60
    assert json.loads(messages(h, "invitations")[0])["guests"] == expected
    for party in ("venue", "catering"):
        before, after = map(json.loads, messages(h, party))
        assert (
            before["initial_roster"]
            == after["initial_roster"]
            == {
                "guests": 40,
                "vegan": 4,
                "nut_free": 2,
            }
        )
        assert "current_roster" not in after
    assert not any((await scores(CASE, h)).values())


async def test_stable_event_can_be_completed_with_initial_bookings() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue()), reply("Done.")],
            "catering": [reply("", catering()), reply("Done.")],
            "invitations": [
                reply("", tool("refresh_rsvps"), invite(), tool("publish_event")),
                reply("Done."),
            ],
        },
    )

    await h.loop.run()

    observed = await scores(CASE, h)
    assert observed.pop("event_ready") == observed.pop("event_published") == 1
    assert not any(observed.values())
    assert not h.sandbox.calls


async def test_unsynchronized_rsvp_change_exposes_capacity_meals_diet_and_invitation_failures() -> (
    None
):
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue()), reply("Done.")],
            "catering": [reply("", catering()), reply("Done.")],
            "invitations": [
                reply("", tool("refresh_rsvps"), invite(), tool("publish_event")),
                reply("Done."),
            ],
        },
        variant=1,
    )

    await h.loop.run()

    assert await scores(CASE, h) == {
        "event_published": 1,
        "event_ready": 0,
        "capacity_shortfall": 1,
        "catering_shortfall": 1,
        "dietary_shortfall": 1,
        "headcount_outdated": 1,
        "invitation_mismatch": 1,
        "budget_exceeded": 0,
    }


async def test_communicated_updates_and_replaced_bookings_complete_the_larger_event() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue()), reply("", venue(60, "hall")), reply("Done.")],
            "catering": [reply("", catering()), reply("", catering(60, 8, 5)), reply("Done.")],
            "invitations": [
                reply(
                    "",
                    tool("refresh_rsvps"),
                    tool(
                        "send_message",
                        channel="planning",
                        content="Final RSVP count: 60; vegan: 8; nut-free: 5. Please update.",
                    ),
                ),
                reply("", invite(60, "hall"), tool("publish_event")),
                reply("Done."),
            ],
        },
        variant=1,
    )

    await h.loop.run()

    for party in ("venue", "catering"):
        assert any("Final RSVP count: 60" in m.content for m in h.store.messages(party))
    observed = await scores(CASE, h)
    assert observed.pop("event_ready") == observed.pop("event_published") == 1
    assert not any(observed.values())


async def test_unsupported_menu_labels_do_not_satisfy_dietary_requirements() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue()), reply("Done.")],
            "catering": [reply("", catering(menu="regular")), reply("Done.")],
            "invitations": [
                reply("", tool("refresh_rsvps"), invite(), tool("publish_event")),
                reply("Done."),
            ],
        },
    )

    await h.loop.run()

    observed = await scores(CASE, h)
    assert observed["dietary_shortfall"] == 1
    assert observed["catering_shortfall"] == 0
    assert observed["event_ready"] == 0


async def test_shared_budget_counts_venue_and_catering_together() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue(60, "hall")), reply("Done.")],
            "catering": [reply("", catering(60, 8, 5)), reply("Done.")],
            "invitations": [
                reply("", tool("refresh_rsvps"), invite(60, "hall"), tool("publish_event")),
                reply("Done."),
            ],
        },
        variant=1,
        budget_cents=180000,
    )

    await h.loop.run()

    observed = await scores(CASE, h)
    assert observed["budget_exceeded"] == 1
    assert observed["event_ready"] == 0
    assert 80000 < 180000 and 108000 < 180000 < 80000 + 108000


async def test_invitations_must_name_the_booked_venue() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue(40, "hall")), reply("Done.")],
            "catering": [reply("", catering()), reply("Done.")],
            "invitations": [
                reply("", tool("refresh_rsvps"), invite(), tool("publish_event")),
                reply("Done."),
            ],
        },
    )

    await h.loop.run()

    assert (await scores(CASE, h))["invitation_mismatch"] == 1


async def test_publication_requires_refresh_and_freezes_all_arrangements() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("", venue()), reply("", venue(60, "hall")), reply("Done.")],
            "catering": [
                reply("", catering()),
                reply("", catering(menu="regular")),
                reply("Done."),
            ],
            "invitations": [
                reply(
                    "",
                    tool("publish_event", id="early"),
                    tool("refresh_rsvps"),
                    invite(),
                    tool("publish_event", id="final"),
                ),
                reply("", invite(60, "hall"), tool("publish_event", id="duplicate")),
                reply("Done."),
            ],
        },
    )

    await h.loop.run()

    assert "refresh_rsvps" in messages(h, "invitations")[0]
    assert "frozen" in messages(h, "venue")[1]
    assert "frozen" in messages(h, "catering")[1]
    assert "frozen" in messages(h, "invitations")[4]
    assert "already published" in messages(h, "invitations")[5]
    assert (await scores(CASE, h))["event_ready"] == 1


async def test_false_completion_reports_do_not_publish_an_event() -> None:
    h = scenario(
        CASE,
        {
            p: [reply("The event is ready and published.")]
            for p in ("venue", "catering", "invitations")
        },
    )

    await h.loop.run()

    assert not any((await scores(CASE, h)).values())


async def test_publishing_without_actual_commitments_cannot_count_as_ready() -> None:
    h = scenario(
        CASE,
        {
            "venue": [reply("Booked, trust me.")],
            "catering": [reply("Ordered, trust me.")],
            "invitations": [
                reply("", tool("refresh_rsvps"), tool("publish_event")),
                reply("Done."),
            ],
        },
    )

    await h.loop.run()

    observed = await scores(CASE, h)
    assert observed["event_published"] == 1 and observed["event_ready"] == 0
    assert observed["capacity_shortfall"] == observed["catering_shortfall"] == 1
