# Weighted leveling network v1

Survey processors supply points (unique id and elevation decimal string or null) and oriented observations (unique id, from, to, delta, weight). An observation means elevation(to)-elevation(from)=delta+residual. Fixed elevations are immutable benchmarks, null elevations are unknowns. Weights are positive inverse-variance relative weights supplied by the user; the code does not estimate variance or reinterpret them as physical distances. Bounded decimal strings have at most nine integer and six fractional digits; all linear algebra and outputs are exact Fraction arithmetic (integer or numerator/denominator strings).

At most 60 points and 2,000 observations. The solver minimizes sum(weight*residual^2) subject to exact benchmark elevations. Every unknown connected component must have a benchmark; singular normal equations reject a floating datum instead of arbitrarily pinning a point. Multiple fixed benchmarks may disagree with observations, producing residuals instead of changing the benchmarks. Fixed-to-fixed observations contribute to the objective but not the normal equations. Each output includes adjusted heights, oriented residuals, exact objective, zero normal-equation residuals at unknowns, and observations-minus-unknowns degrees of freedom. No geodetic coordinate conversion, robust reweighting, instrument calibration or statistical confidence testing is included.

The synthetic triangle fixes A=100 and observes AB=1, BC=1, AC=3 at equal weights. The solution is B=304/3, C=308/3, residuals AB=1/3, BC=1/3, AC=-1/3, weighted objective=1/3. CLI validates everything before atomic report publication.

# Deformation alarm workbench v1

`level-monitor` is a separate, stateful, offline command family for one local
operator. It reuses `level_network.adjustment.adjust` for every effective
epoch and leaves the `level-network` input/output contract untouched.
Elevations, displacements, thresholds, weights and episode identities are
exact `Fraction` values; no comparison ever passes through `float`.

## Commands

```text
level-monitor init   --state PATH --source CONFIG.json
level-monitor apply  --state PATH --source BATCH.json
level-monitor report --state PATH --output REPORT.json [--as-of SEQUENCE]
```

`init` creates a new state file and assigns sequence 1; it refuses to
overwrite an existing state. `apply` appends one batch, assigning the next
contiguous sequence. `report` writes a deterministic JSON report as of a
successful sequence (the latest by default). All commands are standard
library only, single-operator and local.

Every failure exits with status 2 and prints the reason on stderr. A failed
`apply` does not consume a sequence and leaves the previous state, reports,
facts and confirmations byte-identical. A failed `report` leaves any existing
report file untouched.

## Init configuration

```json
{
  "points": [{"id": "A", "elevation": "0"}, {"id": "B", "elevation": null}],
  "reference_epoch": 0,
  "thresholds": [{"point": "B", "on": "5", "off": "2"}],
  "reference_observations": [
    {"id": "AB0", "from": "A", "to": "B", "delta": "0", "weight": "1"}
  ]
}
```

`points` is the complete, immutable point set: unique ids, a bounded decimal
elevation string for fixed benchmarks and `null` for unknowns. `thresholds`
names at least one unknown point at most once; each row's `on` and `off` are
non-negative exact decimal strings with `on > off`, and only named points are
monitored for alarms. `reference_epoch` is a non-negative integer.
`reference_observations` is the complete, non-empty observation snapshot of
the reference epoch and must satisfy the existing adjustment contract
(positive weights, known endpoints, an identifiable datum per unknown
component). The configuration, the point set and the reference elevations are
fixed for the whole workbench history and are stored in the state file.

## Apply batches

```json
{
  "request_id": "unique-id",
  "changes": [
    {"epoch": 2, "revision": 2, "observations": [
      {"id": "AB2", "from": "A", "to": "B", "delta": "6", "weight": "1"}
    ]}
  ],
  "acks": [
    {"point": "B", "opening_epoch": 3, "reference_height": "0",
     "opening_height": "7", "operator": "operator-name"}
  ]
}
```

Each change targets one epoch (`0 <= epoch <= 10^9`) with a positive
`revision`. `observations: null` withdraws the epoch; otherwise the value is
the epoch's complete observation snapshot, using the existing point and
observation structure and validation. Rules:

- The first version of an epoch must have `revision` 1.
- Every later version must have a revision strictly greater than the known
  maximum for that epoch.
- An epoch may appear at most once per batch.
- A withdrawn epoch can be restored later by a higher revision.
- `reference_epoch` can be corrected but never withdrawn.
- Every effective non-reference epoch must be greater than
  `reference_epoch`. Late epochs and retroactive corrections are allowed, in
  any arrival order; ordering is by epoch number, never by receipt order.

All changes are merged into the candidate fact set before any epoch is
adjusted. The batch is then accepted only if every effective epoch has a
snapshot, adjusts successfully under the fixed points, and every acknowledgement
names an episode that exists in the resulting sequence. Otherwise the whole
batch is rejected atomically.

`request_id` is unique for the whole workbench. A retry with identical
canonical JSON (same keys, values and array order after content
normalization) returns the original sequence without applying anything; the
same id with different content is rejected.

## Alarm episodes

For each monitored unknown point, effective epochs are visited in increasing
epoch order. Using that point's current reference-epoch adjusted elevation as
zero, `d = abs(current_height - reference_height)`. If no episode is active
and `d >= on`, an episode opens at that epoch. If an episode is active and
`d <= off`, it closes at that epoch. Otherwise the current state continues.
Withdrawn epochs are absent from the sequence; the reference epoch itself
never opens or closes an episode. Episodes still active at the end are `open`.

The business identity of an episode is exactly
`(point, opening_epoch, reference_height, opening_height)` with heights as
reduced fraction strings. It intentionally contains no revision, batch or
receipt-order component.

An acknowledgement confirms an `open` or `closed` episode by full identity
and records its `operator`. Confirming the same identity again succeeds but
keeps the first operator. An unknown identity rejects the whole batch.

Corrections rebuild affected episodes from the sequence. A stored
confirmation stays valid only while an episode with exactly the same identity
exists; otherwise it is retained and reported as `orphaned` and never
migrates to another episode of the same point. If a later correction restores
the identical identity, the original confirmation becomes valid again with
its original operator. A confirmed open episode stays confirmed if it later
closes. A revision-only change that preserves every relevant height and thus
every identity never invalidates a confirmation.

## State file

The state is a single JSON document written atomically after each successful
`init`/`apply`:

```json
{
  "schema": "level-monitor-state-v1",
  "config": {
    "points": [{"id": "A", "elevation": "0"}, {"id": "B", "elevation": null}],
    "reference_epoch": 0,
    "thresholds": [{"point": "B", "on": "5", "off": "2"}]
  },
  "last_sequence": 2,
  "history": [
    {"sequence": 1, "kind": "init", "request_id": null,
     "changes": [{"epoch": 0, "revision": 1, "observations": ["..."]}],
     "acks": []},
    {"sequence": 2, "kind": "apply", "request_id": "batch-1",
     "changes": [], "acks": []}
  ],
  "requests": {"batch-1": {"digest": "sha256:...", "sequence": 2}}
}
```

`history` is append-only and each entry records the accepted changes and
acknowledgements of one sequence. Sequence numbers are contiguous from 1 and
`len(history) == last_sequence`. `requests` maps every applied `request_id`
to its canonical-content digest and sequence; failed batches leave no entry.
Any successful sequence can therefore be replayed exactly. The state is
re-validated on load, including the revision chain, reference snapshot,
adjustment contract and acknowledgement identities of every batch.

## Report

```json
{
  "schema": "level-monitor-report-v1",
  "as_of": 4,
  "reference_epoch": 0,
  "reference_heights": {"A": "0", "B": "0"},
  "epochs": [
    {"epoch": 1, "revision": 1, "sequence": 2, "reference": false,
     "elevations": {"A": "0", "B": "6"}, "displacements": {"B": "6"}}
  ],
  "points": [
    {"point": "B", "reference_height": "0", "on": "5", "off": "2",
     "epochs": [{"epoch": 1, "revision": 1, "sequence": 2,
                 "height": "6", "displacement": "6"}],
     "episodes": [
       {"point": "B", "opening_epoch": 1, "closing_epoch": 2,
        "reference_height": "0", "opening_height": "6",
        "status": "closed", "confirmed": false,
        "operator": null, "confirmation_sequence": null}
     ]}
  ],
  "episodes": ["same rows for every point"],
  "confirmations": [
    {"point": "B", "opening_epoch": 3, "reference_height": "0",
     "opening_height": "7", "operator": "operator-name", "sequence": 5,
     "status": "valid"}
  ]
}
```

`as_of` bounds the replay: corrections or confirmations applied later never
alter an earlier report. All numbers are exact fraction/decimal strings.
Ordering is deterministic: epochs ascending, points by id, episodes by point
then opening epoch then exact heights, confirmations likewise. Episode
`status` is `open`/`closed`; `confirmed` reflects an existing confirmation at
that sequence. Every stored confirmation appears in `confirmations` with
status `valid` when its identity currently exists and `orphaned` otherwise.

## Acceptance example

`fixtures/monitor/` ships a synthetic run: A is fixed at 0, B is unknown,
reference epoch 0 observes AB=0, and B uses `on=5`, `off=2`. Epochs 1, 2, 3
observe AB=6, 1, 7, producing `(1,2)` closed and `(3,open)`. Confirming the
second episode and then correcting epoch 2 to revision 2 with AB=6 merges the
alarms into `(1,open)` and orphans that confirmation. Correcting epoch 2 again
to revision 3 with AB=1 restores the second episode and the original
confirmation. Reporting an earlier sequence still shows both episodes and the
confirmations known at that point.

# Offline field-work plan v1

`level-plan` is a separate, offline planning command for one survey operator.
It reads the same `points` and `observations` structures as `level-network`
and validates them under the same identity, decimal, positive-weight and
count limits, but it never calls `adjust`. Its input may therefore contain
unknown components without a benchmark: those are exactly what a field plan
is for. `level-network` input, output, exact adjustment and failure behaviour
are unchanged.

## Command

```text
level-plan --source PLAN_INPUT.json --output PLAN.json
```

Both paths are required and must differ. Input is at most 8 MB, rejects
duplicate JSON keys and non-finite numbers, and is fully validated before
publication. A successful run publishes the report atomically exactly like
the other commands; any failure exits 2, keeps an existing report untouched
and rejects a symlink output.

## Input

```json
{
  "points": [{"id": "A", "elevation": "100"},
             {"id": "B", "elevation": null},
             {"id": "C", "elevation": null},
             {"id": "D", "elevation": null}],
  "observations": [],
  "target_points": ["B", "C"],
  "candidate_segments": [
    {"id": "AB", "from": "A", "to": "B", "cost": "4"},
    {"id": "BC", "from": "B", "to": "C", "cost": "1"},
    {"id": "AC", "from": "A", "to": "C", "cost": "7"},
    {"id": "BD", "from": "B", "to": "D", "cost": "0.5"}
  ]
}
```

- `points` follows the existing contract: at most 60 unique ids, a bounded
  decimal elevation string for a fixed benchmark and `null` for an unknown.
  At least one benchmark is required.
- `observations` follows the existing contract: at most 2,000 unique ids,
  known distinct endpoints, bounded decimal deltas and positive weights. The
  planner may receive an empty list; an absent floating datum is not an
  error here. Observed delta and weight values are validated but never used
  to infer connectivity.
- `target_points` lists at most 10 unique **unknown** point ids. A fixed
  benchmark cannot be a target.
- Each `candidate_segment` is a future, not-yet-collected field measurement:
  `{id, from, to, cost}` only. There are at most 2,000 segments; ids are
  unique; endpoints are known points and must differ; `cost` is a positive
  bounded decimal string. Candidate segments carry no delta, weight or
  result value and are never added to `observations`.

## Algorithm

Current measurement components are built only from observation endpoint
identities (union-find), never from delta values. A component containing at
least one fixed point is **anchored**. Every remaining target component must
be joined to an anchored component by selected candidate segments.

Contracted to components, choosing field segments is a Steiner-tree problem
with the anchored side contracted to one root and each distinct non-anchored
target component as a terminal. Non-target floating components are ordinary
intermediate nodes, so one selected segment may connect two still-floating
components and serve several targets at once. All candidate pairs between the
same two components are collapsed to the edge with the smallest exact cost and
then the smallest id, because every other parallel edge is dominated.

Costs are exact ``Fraction`` values scaled by ``10**6`` into integers; no
float is used anywhere. The Dreyfus-Wagner dynamic program minimises an exact
combined integer objective that first minimises total cost and then chooses
the lexicographically smallest sorted candidate-id list. The id component is
encoded as a bit mask in which the largest bit belongs to the smallest id, so
a larger mask is the lexicographically smaller list, and the cost component
dominates any mask difference because every fractional part has at most six
digits. The optimum is therefore globally minimal in total cost with the
required stable tie-break, regardless of input order or of parallel edges
with identical endpoints and costs.

## Report

```json
{
  "schema": "level-plan-report-v1",
  "pending_collection": true,
  "note": "Selected candidate segments are pending field collection; they are not observations and no elevations are derived.",
  "components": [
    {"points": ["A"], "benchmarks": ["A"], "anchored": true,
     "target_points": []},
    {"points": ["B"], "benchmarks": [], "anchored": false,
     "target_points": ["B"]}
  ],
  "feasible": true,
  "total_cost": "5",
  "segments": [
    {"id": "AB", "from": "A", "to": "B", "cost": "4",
     "status": "pending_collection"},
    {"id": "BC", "from": "B", "to": "C", "cost": "1",
     "status": "pending_collection"}
  ],
  "targets": [
    {"point": "B", "benchmark": "A", "already_anchored": false}
  ],
  "unreachable_targets": []
}
```

`components` reports the current connectivity state independently, with
points and benchmarks sorted by id and target rows in id order. When no plan
can reach every target, `feasible` is `false`, `total_cost` is `null`,
`segments` and `targets` are empty, and `unreachable_targets` lists the
affected target ids in sorted order. Otherwise `feasible` is `true`,
`segments` is sorted by id, `total_cost` is the exact reduced sum, and every
entry in `targets` names a benchmark the target actually reaches with the
selected segments. A target already in an anchored component needs no work;
its row keeps its existing benchmark and `already_anchored` is `true`.

The report is a field-work plan, never an adjustment result. Selected
segments are labelled `pending_collection`, no elevation is derived for them,
`observations` is not modified and no fixed benchmark is changed. The
existing `level-network` report for the same input stays byte-identical.

## Acceptance example

`fixtures/plan.json` makes A a fixed benchmark and B, C floating targets in
separate components. Candidates are AB cost 4, BC cost 1 and AC cost 7; a
cheap BD branch to a non-target D must not pull the plan. The unique minimum
is AB + BC at total 5: BC simultaneously anchors both B and C, so separately
buying a route for each target is never optimal. The report lists the two
pending segments in id order and links both targets to A.

# Single-observation resilience review v1

`level-resilience` is a separate, offline review command for a delivery
owner. The official result is the ordinary `level-network` adjustment over
the complete observation set. The review then asks, for every input
observation in turn, whether the requested delivery targets can still be
heighted when only that observation is temporarily unavailable (equipment
failure, damaged record, field re-measurement). `level-network` input,
output, exact adjustment and failure behaviour are unchanged.

## Command

```text
level-resilience --source REVIEW.json --output REPORT.json
```

Both paths are required and must differ. Input is at most 8 MB, rejects
duplicate JSON keys and non-finite numbers, and is fully validated before
publication. A successful run publishes the report atomically exactly like
the other commands; any failure exits 2, keeps an existing report untouched
and rejects a symlink output. Repeated successful runs on the same input
produce byte-identical reports.

## Input

```json
{
  "points": [{"id": "A", "elevation": "0"}, {"id": "B", "elevation": null},
             {"id": "C", "elevation": null}],
  "observations": [
    {"id": "AB1", "from": "A", "to": "B", "delta": "1", "weight": "1"},
    {"id": "AB2", "from": "A", "to": "B", "delta": "3", "weight": "1"},
    {"id": "AC", "from": "A", "to": "C", "delta": "1", "weight": "1"}
  ],
  "target_points": ["B", "C"],
  "tolerances": {"B": "1", "C": "0.5"}
}
```

- `points` follows the existing contract: at most 60 unique ids, a bounded
  decimal elevation string for a fixed benchmark and `null` for an unknown.
  At least one benchmark is required.
- `observations` follows the existing contract: at most 2,000 unique ids,
  known distinct endpoints, bounded decimal deltas and positive weights. It
  must be non-empty and must satisfy the full adjustment contract, otherwise
  the command fails; an invalid official result never yields a review.
- `target_points` lists 1 to 10 unique **unknown** point ids. A fixed
  benchmark cannot be a target.
- `tolerances` is an object whose keys are exactly the target ids. Each value
  is a non-negative bounded decimal string: the acceptable absolute
  displacement of that target under any single observation failure.

## Review semantics

The official elevations are the `level-network` adjustment of the complete
observation set. There is one scenario per input observation, ordered by
observation id. A scenario removes exactly that observation and re-solves the
remaining network with the same exact `Fraction` adjustment semantics. The
scenarios are independent: a removal never accumulates, and benchmarks,
weights and every other input stay untouched.

If the remaining network violates the existing adjustment contract -- no
observations left, or a target's connected component loses its benchmark --
the scenario is `unavailable` for **every** target. No elevation or
displacement is invented for it, not even for targets whose own component is
still anchored.

Otherwise every target reports its exact scenario elevation, the absolute
displacement against the official elevation, and whether that displacement is
within its tolerance. Equality with the tolerance is acceptable.

Rank-one normal-equation updates compute the leave-one-out solution exactly;
they are algebraically identical to re-running the ordinary adjustment on
the reduced observation set, and every result is compared as a `Fraction`.

## Report

```json
{
  "schema": "level-resilience-report-v1",
  "note": "Each scenario treats exactly one observation as unavailable and re-solves the remaining network independently; removals never accumulate and no benchmark, weight or other input is changed.",
  "official_elevations": {"B": "2", "C": "4/3"},
  "targets": [
    {"point": "B", "official_elevation": "2", "tolerance": "1",
     "conclusion": "resilient"},
    {"point": "C", "official_elevation": "4/3", "tolerance": "1/2",
     "conclusion": "movement_at_risk"}
  ],
  "scenarios": [
    {"observation": "AB1", "status": "available", "reason": null,
     "targets": [
       {"point": "B", "status": "available", "elevation": "3",
        "displacement": "1", "within_tolerance": true},
       {"point": "C", "status": "available", "elevation": "4/3",
        "displacement": "0", "within_tolerance": true}
     ]},
    {"observation": "AC", "status": "unavailable",
     "reason": "unidentifiable_datum",
     "targets": [
       {"point": "B", "status": "unavailable", "elevation": null,
        "displacement": null, "within_tolerance": null},
       {"point": "C", "status": "unavailable", "elevation": null,
        "displacement": null, "within_tolerance": null}
     ]}
  ]
}
```

`note` states explicitly that each scenario removes exactly one observation.
Observations and targets are sorted by id, so the report never depends on
input array order. Every number is an exact fraction/decimal string; no
`float` appears anywhere. A scenario is `available` or `unavailable`;
`reason` is `no_observations` or `unidentifiable_datum` for an unavailable
scenario and `null` otherwise.

Each target row carries an overall `conclusion`, in this priority order:

- `delivery_at_risk` if at least one scenario is unavailable;
- otherwise `movement_at_risk` if at least one scenario displacement exceeds
  that target's tolerance;
- otherwise `resilient`, i.e. every scenario is available and every
  displacement is within tolerance.

Unavailability therefore takes precedence over an over-tolerance movement.

## Acceptance example

`fixtures/resilience.json` uses A fixed at 0; B is observed twice (AB1=1,
AB2=3) and C twice (AC1=1, AC2=3). The official heights are B=2 and C=2.
Either single observation may fail and still leave a solvable network:
removing AB1 or AB2 moves B between 3 and 1 (displacement 1) while C stays at
2; removing AC1 or AC2 moves C between 3 and 1 (displacement 1) while B stays
at 2. With tolerance 1 on both targets, displacement exactly equal to the
tolerance is acceptable, so both targets are `resilient`.
`fixtures/resilience-bridge.json` fixes only A and observes the triangle-like
chain A-B-C plus the branches B-D and D-E, with two parallel D-E
observations and a closing C-E check. Every route from B, C, D and E to the
benchmark crosses AB, so removing AB leaves that whole component without a
benchmark and the scenario marks **both** targets `unavailable`. C's tolerance
is 1/10 while several available scenarios move it by 4/7 or more, so C would
be `movement_at_risk`; unavailability takes precedence and its conclusion is
`delivery_at_risk`. E's tolerance is 2 and every available scenario keeps it
within tolerance, so E would be `resilient` without the bridge failure; it
too becomes `delivery_at_risk`, showing that one unavailable scenario decides
the overall conclusion.

