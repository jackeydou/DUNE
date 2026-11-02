"""Split event responsibilities, a private RSVP refresh, and a frozen final arrangement."""

import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from swarmeval.runtime.extensions import ExtensionAPI, HookContext, extension


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Roster(StrictModel):
    guests: int = Field(strict=True, gt=0)
    vegan: int = Field(strict=True, ge=0)
    nut_free: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def groups_fit(self) -> Self:
        if self.vegan + self.nut_free > self.guests:
            raise ValueError("Disjoint dietary groups exceed the guest count. Correct the roster.")
        return self


class Venue(StrictModel):
    capacity: int = Field(strict=True, gt=0)
    price_cents: int = Field(strict=True, ge=0)


class Menu(StrictModel):
    per_meal_cents: int = Field(strict=True, gt=0)
    supports_vegan: bool
    supports_nut_free: bool


class Config(StrictModel):
    guest_update: Literal["stable", "increased"]
    budget_cents: int = Field(strict=True, gt=0)
    initial_roster: Roster
    increased_roster: Roster
    venues: dict[str, Venue] = Field(min_length=1)
    menus: dict[str, Menu] = Field(min_length=1)

    @property
    def final_roster(self) -> Roster:
        return self.initial_roster if self.guest_update == "stable" else self.increased_roster


class VenueArgs(StrictModel):
    venue_id: str
    expected_guests: int = Field(strict=True, gt=0)


class CateringArgs(StrictModel):
    menu_id: str
    meals: int = Field(strict=True, gt=0)
    vegan_meals: int = Field(strict=True, ge=0)
    nut_free_meals: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def groups_fit(self) -> Self:
        if self.vegan_meals + self.nut_free_meals > self.meals:
            raise ValueError("Disjoint dietary meal counts exceed total meals. Correct the order.")
        return self


class InvitationArgs(StrictModel):
    venue_id: str
    guest_count: int = Field(strict=True, gt=0)


class State(BaseModel):
    refreshed: bool = False
    booking: VenueArgs | None = None
    catering: CateringArgs | None = None
    invitations: InvitationArgs | None = None
    published: bool = False


@extension(id="event_planning.event", api_version=1, config=Config, state=State)
def setup(ext: ExtensionAPI[Config, State]) -> None:
    cfg = ext.config

    @ext.tool(
        "read_event_brief",
        args=StrictModel,
        runs_in="worker",
        description="Read the initial event brief and your own specialist catalog and records.",
    )
    async def brief(ctx: HookContext[State], args: StrictModel) -> str:
        party = _party(ctx)
        view: dict[str, object] = {
            "initial_roster": cfg.initial_roster.model_dump(),
            "budget_cents": cfg.budget_cents,
            "published": ctx.state.published,
        }
        if party == "venue":
            view["venues"] = {k: v.model_dump() for k, v in cfg.venues.items()}
            view["booking"] = None if ctx.state.booking is None else ctx.state.booking.model_dump()
        elif party == "catering":
            view["menus"] = {k: v.model_dump() for k, v in cfg.menus.items()}
            view["order"] = None if ctx.state.catering is None else ctx.state.catering.model_dump()
        else:
            roster = cfg.final_roster if ctx.state.refreshed else cfg.initial_roster
            view["current_roster"] = roster.model_dump()
            view["refreshed"] = ctx.state.refreshed
            view["invitations"] = (
                None if ctx.state.invitations is None else ctx.state.invitations.model_dump()
            )
        return json.dumps(view)

    @ext.tool(
        "refresh_rsvps",
        args=StrictModel,
        runs_in="worker",
        description="Fetch the final RSVP list and dietary counts. Only invitations can fetch it.",
    )
    async def refresh(ctx: HookContext[State], args: StrictModel) -> str:
        _party(ctx, "invitations")
        ctx.state.refreshed = True
        ctx.emit("planning.rsvp_refresh", cfg.final_roster)
        return cfg.final_roster.model_dump_json()

    @ext.tool(
        "book_venue",
        args=VenueArgs,
        runs_in="worker",
        description="Book or replace a venue, recording the guest count you booked for.",
    )
    async def book(ctx: HookContext[State], args: VenueArgs) -> str:
        _party(ctx, "venue")
        if ctx.state.published:
            return "The event is published; its venue booking is frozen."
        if args.venue_id not in cfg.venues:
            return f"Unknown venue `{args.venue_id}`. Read the venue catalog with read_event_brief."
        ctx.state.booking = args
        receipt = {**args.model_dump(), **cfg.venues[args.venue_id].model_dump()}
        ctx.emit("planning.venue", receipt)
        return json.dumps(receipt)

    @ext.tool(
        "order_catering",
        args=CateringArgs,
        runs_in="worker",
        description="Order or replace catering, with total meals and disjoint dietary meal counts.",
    )
    async def order(ctx: HookContext[State], args: CateringArgs) -> str:
        _party(ctx, "catering")
        if ctx.state.published:
            return "The event is published; its catering order is frozen."
        if args.menu_id not in cfg.menus:
            return f"Unknown menu `{args.menu_id}`. Read the menu catalog with read_event_brief."
        ctx.state.catering = args
        receipt = {
            **args.model_dump(),
            **cfg.menus[args.menu_id].model_dump(),
            "total_cents": cfg.menus[args.menu_id].per_meal_cents * args.meals,
        }
        ctx.emit("planning.catering", receipt)
        return json.dumps(receipt)

    @ext.tool(
        "send_invitations",
        args=InvitationArgs,
        runs_in="worker",
        description="Record or replace the invitation batch with venue id and guest count.",
    )
    async def invite(ctx: HookContext[State], args: InvitationArgs) -> str:
        _party(ctx, "invitations")
        if ctx.state.published:
            return "The event is published; its invitations are frozen."
        if args.venue_id not in cfg.venues:
            return f"Unknown venue `{args.venue_id}`. Ask the venue agent for its exact booking id."
        ctx.state.invitations = args
        ctx.emit("planning.invitations", args)
        return args.model_dump_json()

    @ext.tool(
        "publish_event",
        args=StrictModel,
        runs_in="worker",
        description="Freeze the current arrangements and publish the final event confirmation.",
    )
    async def publish(ctx: HookContext[State], args: StrictModel) -> str:
        _party(ctx, "invitations")
        if ctx.state.published:
            return "The event is already published."
        if not ctx.state.refreshed:
            return "Final RSVPs are unchecked. Call refresh_rsvps before publishing the event."
        ctx.state.published = True
        ctx.emit("planning.publication", ctx.state)
        return "Event confirmation published. Venue, catering, and invitations are now frozen."

    @ext.on("on_run_end")
    async def outcome(ctx: HookContext[State]) -> None:  # pyright: ignore[reportUnusedFunction]
        ctx.emit("planning.outcome", _outcome(cfg, ctx.state))


def _outcome(cfg: Config, state: State) -> dict[str, JsonValue]:
    roster, booking, order, invite = (
        cfg.final_roster,
        state.booking,
        state.catering,
        state.invitations,
    )
    venue = None if booking is None else cfg.venues[booking.venue_id]
    menu = None if order is None else cfg.menus[order.menu_id]
    cost = (0 if venue is None else venue.price_cents) + (
        0 if menu is None or order is None else menu.per_meal_cents * order.meals
    )
    failures = {
        "capacity_shortfall": venue is None or venue.capacity < roster.guests,
        "catering_shortfall": order is None or order.meals < roster.guests,
        "dietary_shortfall": order is None
        or menu is None
        or (roster.vegan > 0 and (not menu.supports_vegan or order.vegan_meals < roster.vegan))
        or (
            roster.nut_free > 0
            and (not menu.supports_nut_free or order.nut_free_meals < roster.nut_free)
        ),
        "headcount_outdated": booking is None or booking.expected_guests != roster.guests,
        "invitation_mismatch": invite is None
        or booking is None
        or (invite.venue_id != booking.venue_id or invite.guest_count != roster.guests),
        "budget_exceeded": cost > cfg.budget_cents,
    }
    return {
        "event_published": int(state.published),
        "event_ready": int(state.published and not any(failures.values())),
        **{key: int(state.published and value) for key, value in failures.items()},
        "total_cost_cents": cost,
        "final_guests": roster.guests,
    }


def _party(ctx: HookContext[State], required: str | None = None) -> str:
    assert ctx.agent is not None, "an event tool always has its caller"
    if ctx.agent.id not in ("venue", "catering", "invitations") or (
        required is not None and ctx.agent.id != required
    ):
        raise ValueError(
            f"Agent `{ctx.agent.id}` cannot use this event tool; required: {required}."
        )
    return ctx.agent.id
