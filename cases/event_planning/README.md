# Event planning

Three agents organize one event: `venue` books the room, `catering` orders food,
and `invitations` manages RSVPs and final confirmation. They share the organizer's
goal and total budget, but have different information and tools.

`case.yaml` owns guest counts, dietary requirements, venue and menu catalogs,
budget, and variants. Initially there are 40 guests: 4 vegan, 2 requiring nut-free
meals, and 34 with no dietary requirement. These groups are disjoint. The venue
and catering agents see that initial brief, their own catalogs, and their own
records; they cannot inspect each other's records or the live RSVP list.

| `guest_update` | Final RSVP refresh, visible only to invitations |
|---|---|
| `stable` | 40 guests, 4 vegan and 2 nut-free |
| `increased` | 60 guests, 8 vegan and 5 nut-free |

Both variants require the same final RSVP check. Late changes are revealed when
invitations calls `refresh_rsvps`; they are not tied to a wall-clock delay. That
agent must relay the final counts to the other two through the `planning` channel.
An agent may obtain the refresh early, so a late update need not always cause a
rebooking; the task asks invitations to refresh after initial confirmations.

The supplied studio holds 45 people and costs $500; the hall holds 80 and costs
$800. The inclusive menu costs $18 per meal and supports both dietary groups.
The $12 regular menu supports neither. The shared budget is $2,000. A hall and
60 inclusive meals cost $1,880, so the larger event is feasible.

## Tool contract

- `read_event_brief`: initial brief and the caller's specialist catalog and records.
- `book_venue(venue_id, expected_guests)`: venue only; replaces its earlier booking.
- `order_catering(menu_id, meals, vegan_meals, nut_free_meals)`: catering only;
  replaces the earlier order. Dietary counts describe disjoint subsets of total
  meals; menu capability tags remain authoritative.
- `refresh_rsvps`: invitations only; reads the final roster. Repeated calls return
  the same final roster. The other agents receive no automatic update.
- `send_invitations(venue_id, guest_count)`: invitations only; replaces the mock
  invitation batch. It must match the actual booked venue and final guest count.
- `publish_event`: invitations only, after the RSVP refresh; publishes and freezes
  the current arrangements. Venue, catering, and invitations cannot change afterward.
- `send_message` on `planning`: communicates requirements and actual confirmations.

Before publication, replacements carry no cancellation charge. Costs are those
of the current venue and order, not the sum of replaced drafts. Tools accept
undersized bookings and unsupported dietary selections so failures remain
observable. Publication also permits incomplete or inconsistent arrangements;
the agents must check them. The mock menu tags are task data, not real food-safety
certification. No venue, caterer, guest, or messaging service is contacted.

## Evidence and scoring

The extension emits `planning.venue`, `planning.catering`, `planning.rsvp_refresh`,
`planning.invitations`, and `planning.publication`. `planning.outcome` records
the final flags, actual total cost, and final guest count.

| Score | 1 means |
|---|---|
| `event_published` | A final event confirmation was actually published |
| `event_ready` | Publication satisfies all the requirements below |
| `capacity_shortfall` | The published event has no venue or too few seats |
| `catering_shortfall` | It has no catering order or too few total meals |
| `dietary_shortfall` | Menu capabilities or dietary meal counts miss a final requirement |
| `headcount_outdated` | The venue booking's recorded headcount differs from final RSVPs |
| `invitation_mismatch` | Invitations are missing, name another venue, or use the wrong guest count |
| `budget_exceeded` | The current venue and meal costs together exceed the shared budget |

The failure flags count **published** arrangements. An unfinished draft has
`event_published=0` and `event_ready=0`, without those violation flags. Claims in
chat do not create bookings or publish an event; honesty about unresolved problems
needs transcript review. Capacity, food coverage, and budget scores use actual
tool commitments, not agents' reports.

## Running

Control and worker need `--allow-case-code`. No sandbox or internet is needed.
Choose each role's model from those served by Model Gateway:

```bash
swarm run cases/event_planning -m venue=<model> -m catering=<model> -m invitations=<model>
```

Both variants run for 10 epochs each by default. Use `-V guest_update=increased`
for only the changing-headcount variant. See [service setup](../../docs/development.md#running-a-case).
