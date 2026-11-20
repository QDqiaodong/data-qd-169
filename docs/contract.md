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

# Leveling staff calibration handover v1

`level-staff` is a separate, offline two-stage command family that lets a
calibration team hand the joint zero-error calibration of one physical staff
set to a delivery team without sharing calibration points, observation
identities, row-level readings or calibration-point elevations.  The two
teams' point and observation id spaces are independent: equal names are never
merged.  Staff are matched between stages by physical identifier only, never
by position or first appearance.  Every value stays an exact `Fraction`; no
`float` is used in either stage.

## Commands

```text
level-staff calibrate --source CALIBRATION.json --output PACKAGE.json
level-staff deliver  --source SURVEY.json --calibration PACKAGE.json --output REPORT.json
```

All input and output paths must be pairwise distinct; symlink outputs are
rejected, inputs reject duplicate JSON keys and non-finite numbers under the
same 8 MB bounded-JSON and atomic-publication rules as the other commands.
Any invalid input or package exits 2 and leaves an existing output byte
identical.  Reordering points, observations, staff identifiers or queries
never changes an artifact.

## Shared observation model

Both inputs keep the existing point and observation contract (at most 60
points, 1 to 2,000 observations, at least one fixed benchmark, bounded
decimal values, positive weights) and add two fields per observation:
`from_staff` and `to_staff`, the physical staff identifiers held at the
start and end points; the same staff may appear at both ends.  For point
elevations `h` and staff zero errors `b`, the predicted reading is

```text
h[to] - h[from] + b[to_staff] - b[from_staff]
```

Both stages minimize the weighted sum of squared reading residuals.  Fixed
benchmark elevations never participate in any fit.

## Calibration input and package

The calibration input additionally contains `rods`: 1 to 12 distinct staff
identifiers, and every observation's staff identifiers must be listed there.
A listed staff need never appear in a calibration observation, and a
calibration point component may have no fixed datum; both are legal.

The package contains only staff identifiers and their joint calibration
information -- never a calibration point, observation id, row reading or
calibration elevation.  For any fixed staff-error vector `b`, the package
cost equals the minimum weighted residual sum attainable when the unknown
calibration heights are free while the benchmarks and `b` stay fixed.  The
constant term and the coupling between staff are retained; exporting only
best errors, per-staff errors or independent staff-pair tables is explicitly
not a substitute.  A package is still produced when the zero errors are not
individually identifiable.

```json
{
  "schema": "level-staff-calibration-v1",
  "rods": ["S", "T", "U"],
  "quadratic": [["2/3", "-1/3", "-1/3"],
                ["-1/3", "2/3", "-1/3"],
                ["-1/3", "-1/3", "2/3"]],
  "linear": ["5/3", "-1/3", "-4/3"],
  "constant": "14/3"
}
```

`rods` is sorted and defines the interpretation order of the square matrix
`quadratic` (Q), the vector `linear` (g) and scalar `constant` (c): the cost
is `b^T Q b + 2 g^T b + c`.  Every number is a reduced fraction string or an
integer string; decimals and floats are forbidden.  Gauss-Jordan elimination
on the calibration normal equations tolerates singular blocks (unanchored
components): it only needs a particular solution, since null-space
directions are orthogonal to every normal-equation right-hand side.

## Package acceptance

A package is read only after validating the schema, sorted unique rods,
matrix/vector dimensions and exact number syntax.  It is rejected when Q is
asymmetric, when Q has any negative curvature, when the stationarity
equations have no solution (no finite minimum), when the attained minimum is
negative, or when adding one common offset to every staff error would change
the cost (the all-ones vector must be a null direction of Q and orthogonal to
g).  A flat direction by itself is legal: a zero-curvature package whose
invariants hold is accepted.

## Delivery input and report

The delivery input additionally contains `targets`: 1 to 20 unique queries,
either `{id, point}` for an absolute elevation or `{id, from, to}` for a
directed height difference between distinct existing points.  Every
delivery-observation staff identifier must come from the package, including
staff listed but never observed at calibration.

The joint objective is the delivery network's weighted residual sum plus the
full package cost, minimized jointly over delivery heights and one shared
staff-error vector.  A query is published only when its value is identical
across **every** optimum: algebraically, the query coefficient vector must be
consistent with the joint normal-equation matrix.  An unidentifiable staff,
point or component never blocks other uniquely determined queries; in
particular the difference of two unpublishable absolute elevations can still
be published.  The report is always written for a legal input, even when no
query can be published.

```json
{
  "schema": "level-staff-delivery-v1",
  "minimum_cost": "4/3",
  "targets": [
    {"id": "height-B", "status": "resolved", "value": "49/3"}
  ]
}
```

`targets` is sorted by query id; each row is `{id, status, value}` with
`status` `resolved` and the exact value, or `unresolved` and `null`.
`minimum_cost` is the exact joint minimum.

## Acceptance examples

`fixtures/staff/example1-calibration.json` fixes A=0 with P unknown and reads
A→P as S→S=10, S→T=12, S→U=13.  The delivery file fixes A=F=0 with B unknown,
reads A→F with S→T=4 and A→B with S→U=20.  The generated package (the
delivery run never opens the calibration input) gives B=`49/3` and joint
minimum `4/3`.

`fixtures/staff/example2-calibration.json` lists S, T, U and contains one
S→T=2 reading between two points fixed at 0.  The delivery network fixes A=0,
leaves B, C, D unknown and reads A→B S→T=12, A→C S→U=16, C→D U→U=3.  One
report gives B=`10`, C and D absolute elevations `unresolved`, the C→D
difference `3` (and D→C `-3`) and joint minimum `0`.

`fixtures/staff/unanchored-calibration.json` adds a point component without
any benchmark; the resulting flat, all-zero-curvature package still validates,
and the delivery query that depends on the unidentified staff direction is
reported `unresolved` rather than pinned to zero.

# Level acceptance review v1

`level-accept` is a separate, offline acceptance command for one field
crew.  It never calls `adjust`: a least-squares best fit can hide readings
that cannot hold simultaneously, and it can satisfy an acceptance line
while another, equally compatible elevation set fails the whole work lot.
The command first decides whether one shared reserve fraction makes the
whole reading batch jointly feasible, then decides which work lots can be
signed off against *every* compatible elevation set.  `level-network`
input, output, exact adjustment and failure behaviour are unchanged.

## Command

```text
level-accept --source INPUT.json --output REPORT.json
```

Both paths are required and must differ.  Input is at most 8 MB, rejects
duplicate JSON keys and non-finite numbers, and is fully validated before
publication.  A business rejection (an infeasible batch) still publishes
its report with exit code 0; any invalid input or runtime failure exits 2,
keeps an existing report untouched and rejects a symlink output.  Repeated
runs on the same input produce byte-identical reports, and reordering any
input array (points, observations, lots, clauses) never changes the
report.

## Input

- `points`: the existing contract -- at most 60 unique ids, a bounded
  decimal elevation string for a fixed benchmark and `null` for an
  unknown, at least one benchmark.  Floating components and isolated
  points are allowed: some acceptance questions are purely relative and
  need no manufactured datum.
- `observations`: 1 to 2,000 rows `{id, from, to, delta, nominal,
  reserve}`.  Ids are unique, endpoints exist and differ.  `delta` is the
  directed reading, `nominal` the routine tolerance and `reserve` a
  pre-authorized extra tolerance cap; the last two are non-negative
  bounded decimal strings.  These bounds are hard error limits; the input
  carries no statistical weights.
- `lots`: 1 to 20 independent work packages `{id, clauses}` with unique
  ids.  Each lot has 1 to 20 clauses with ids unique within the lot.  An
  absolute clause is `{id, point, min, max}`; a directed difference clause
  is `{id, from, to, min, max}` with distinct existing endpoints.  `min`
  and `max` are bounded decimal strings with `min <= max`; bounds are
  inclusive.  A lot requires all of its clauses to hold under **one**
  elevation assignment; lots never constrain each other.

## Survey batch acceptance

For one shared reserve fraction `r` in `[0, 1]`, every observation
requires the true directed difference within
`[delta - nominal - r*reserve, delta + nominal + r*reserve]`, and every
fixed elevation stays fixed.  The command computes the exact minimal `r`
that makes the whole batch feasible -- one ratio for the entire batch,
never per-row choices, never dropped rows, never an approximate search.
Feasibility is a negative-cycle question on the constraint graph
(observation edges plus benchmark edges to one shared zero reference), and
the minimal ratio is the maximum cycle ratio `max(-A/B)` over simple
cycles, clamped below by 0; it is found by exact rational root-chasing
over Bellman-Ford negative cycles, so the result is an exact `Fraction`.
If the batch is infeasible even at `r = 1` the survey is `rejected` (a
report is still published) and every lot is `not_evaluated`.  Otherwise
the acceptance ratio stays at the computed minimum -- it is never widened
to 1 and never relaxed to please a clause -- and only elevations
compatible at that ratio are used below.

## Lot sign-off

Each lot starts from the same acceptance result:

- `pass`: every compatible elevation assignment satisfies every clause of
  the lot (checked as exact shortest-path extrema over the compatible
  polyhedron, never as one fitted profile);
- `fail`: no single assignment satisfies all clauses jointly -- a
  contradiction cycle exists through measurements, benchmarks and this
  lot's clauses (per-clause individual feasibility is not a substitute);
- `conditional`: otherwise.  Two complete compatible profiles are
  published for field verification: one satisfying every clause, one
  violating at least one.

Compatible elevations need not be unique; freely translatable points are
never pinned to a fixed value.

## Report

```json
{
  "schema": "level-accept-v1",
  "survey": {"status": "accepted", "reserve_fraction": "1/3",
             "profile": {"A": "0", "B": "4/3"},
             "proof": [{"kind": "observation", "id": "AC",
                        "side": "lower"}]},
  "lots": [{"id": "lot-1", "status": "conditional",
            "satisfying_profile": {"A": "0", "B": "4/3"},
            "violating_profile": {"A": "0", "B": "2"},
            "proof": null}]
}
```

- `survey.reserve_fraction` is the minimal ratio as a reduced fraction or
  integer string; `survey.profile` is one compatible elevation assignment
  mapping every point id to the same kind of numeric string.  A rejected
  survey carries `null` for both.  `survey.proof` is `null` when the
  ratio is 0, otherwise one contradiction chain.
- `lots` is sorted by lot id.  `conditional` rows carry both profiles and
  a `null` proof; `fail` rows carry a contradiction chain and `null`
  profiles; `pass` and `not_evaluated` rows carry `null` for all three.

## Contradiction chains

A chain is a list of `{kind, id, side}` items a reviewer can re-add from
the original input.  `kind` is `observation`, `benchmark` or `clause`
(referencing an observation id, a fixed point id, or a clause id of the
current lot); `side` is `upper` or `lower`.  An upper item reads "the
directed difference from its start to its end is at most the upper
bound"; a lower item reads "the reverse difference is at most the negated
lower bound".  Absolute clauses and fixed benchmarks start from one
shared zero-elevation reference (not an input point id); a benchmark's
two bounds both equal its elevation.  The items chain head-to-tail and
the last returns to the first.  The chain's bounds, summed from the
original input at ratio 1 for a rejected survey or at the acceptance
ratio for a failed lot, are strictly negative; for a positive minimal
acceptance ratio the sum is exactly zero at that ratio and strictly
negative at any smaller one.  Chains are not required to be shortest;
any reproducible chain is valid.  Lot clauses never appear in a survey
conflict chain.

## Acceptance examples

`fixtures/accept/triangle.json` fixes A=0 and reads AB=1, BC=1, AC=3 with
zero nominal and unit reserve, plus AX=5 with nominal 5 and XY=0 exact.
The minimal ratio is `1/3`; every compatible assignment has B=`4/3`,
C=`8/3` and X=Y.  The lot requiring only B in [0, 1.4] is `pass`; the lot
requiring only X in [0, 4] is `conditional`; the lot requiring both X in
[0, 4] and Y in [6, 10] is `fail`.  `fixtures/accept/triangle-rejected.json`
zeroes the first three reserves, making the batch `rejected` at ratio 1.
`fixtures/accept/exact-full-reserve.json` is accepted at ratio exactly 1,
`fixtures/accept/floating.json` shows a floating component whose absolute
clause is `conditional` while its relative clauses pass or fail outright,
`fixtures/accept/benchmark-conflict.json` shows that a conflict between
fixed benchmarks is never absorbed, and `fixtures/accept/zero-reserve.json`
accepts at ratio 0 with a null proof.

# Level field-book filing v1

`level-fieldbook` is a separate, offline archiving command.  An exported
electronic field-book has every survey run mixed together: rows still carry
their unique serial number, their measured direction, their observation
interval and their exact readings, but the run number was lost.  The archive
knows each run's planned walking order (`stops`) and working window, and the
command decides whether the rows can be returned to the runs, publishing one
common witness and, for each run whose rows are not uniquely determined, one
counterexample.  `level-network`, its input/output contract and every other
command are unchanged.

## Command

```text
level-fieldbook --source INPUT.json --output REPORT.json
```

Both paths are required and must differ.  Input is at most 8 MB, rejects
duplicate JSON keys and non-finite numbers, and is fully validated before
publication.  A successful run publishes the report atomically exactly like
the other commands; an input error or any other failure exits 2, keeps an
existing report untouched and rejects a symlink output.  A legal but
unfileable batch is a business result: it still writes a report and exits 0.
Reordering `points`, `records` or `runs` never changes the report.

## Input

```json
{
  "points": [{"id": "A", "elevation": "0"},
             {"id": "B", "elevation": "0"}],
  "records": [
    {"id": "a", "from": "A", "to": "B", "start": 10, "end": 11,
     "backsight": "4", "foresight": "0"},
    {"id": "c", "from": "B", "to": "A", "start": 15, "end": 16,
     "backsight": "0", "foresight": "4"}
  ],
  "runs": [
    {"id": "R1", "stops": ["A", "B", "A"], "start": 10, "end": 18,
     "tolerance": "0"}
  ]
}
```

- `points` follows the existing contract: at most 60 unique ids, a bounded
  decimal elevation string for a known benchmark and `null` for an unknown.
- `records` is at most 2,000 rows and may be empty.  Each row is
  `{id, from, to, start, end, backsight, foresight}`: serial ids are unique
  across the whole batch, `from` and `to` are distinct known point ids,
  `start` and `end` are integer seconds in `[0, 10^9]` with `start < end`,
  and `backsight`/`foresight` are non-negative bounded decimals.  The row's
  oriented height difference is `backsight - foresight`, exactly.
- `runs` holds 1 to 8 unique route ids `{id, stops, start, end, tolerance}`.
  `stops` lists 2 to 7 points in walking order, adjacent stops differ, revisits
  are allowed, and the first and last stops must have known elevations (they
  may be the same benchmark).  The window uses the same integer seconds with
  `start < end`; `tolerance` is a non-negative bounded decimal.

Every adjacent stop pair is one station.  A row is a candidate for a station
only when its direction is exactly that station's, and both its `start` and
its `end` lie inside the run window (bounds included).  A station with more
than four candidates is a field-verification problem and rejects the whole
input with exit 2; a station with no candidates is a normal result.

## Filing semantics

A complete filing assigns one candidate row to every station of every run.
Each serial number is used at most once across the whole batch.  Inside one
run the stations follow the walking order: the previous row's `end` must not
be later than the next row's `start`.  The run's **closure error** is

```text
sum(oriented row difference over the run)
    - (known elevation of the last stop - known elevation of the first stop)
```

and must satisfy `abs(closure) <= tolerance`; equality is acceptable.  Rows
may be left unused, a row is never reversed, no reading is invented, and the
closure is never repaired by an adjustment.

A run is **locally fileable** when such a choice exists for that run alone.
The batch is fileable only when one choice per run exists and no serial is
shared.  Time constrains station order only inside one run: different runs
may have overlapping windows and overlapping times.

## Report

```json
{
  "schema": "level-fieldbook-v1",
  "status": "feasible",
  "assignment": {"R1": ["a", "c"]},
  "runs": [
    {"id": "R1", "status": "confirmed", "record_ids": ["a", "c"],
     "net_delta": "0", "closure_error": "0", "alternative": null},
    {"id": "R2", "status": "ambiguous", "record_ids": null,
     "net_delta": null, "closure_error": null,
     "alternative": {"R1": ["b", "d"], "R2": ["a", "c"]}}
  ],
  "locally_impossible_runs": []
}
```

When at least one complete filing exists, `status` is `feasible` and
`assignment` maps every run id to its station-ordered serial ids; it is one
public witness, and any reproducible such witness is acceptable.  `runs` is
sorted by run id.  A run is `confirmed` only when its serial sequence is
identical in **every** complete filing; it then carries `record_ids`, the
exact sum of its oriented row differences `net_delta` and its signed
`closure_error`.  Otherwise it is `ambiguous`, the three value fields are
`null`, and `alternative` holds another complete filing that is equally legal
but assigns that run different rows -- proof that its official rows would
change.  A `confirmed` row has `alternative: null`.  No two runs may copy the
same measured row, and two filed stations that only happen to compute the
same height difference are still distinct rows.

When no complete filing exists, `status` is `infeasible`, `assignment` is
`null` and `runs` is empty.  `locally_impossible_runs` is the sorted list of
runs that cannot be filed even in isolation (missing pages, wrong station
order, out-of-window rows or an over-tolerance closure); it is empty on every
feasible batch.  The list distinguishes a run that first needs field
investigation from the case where every run is locally fileable but the runs
compete for the same serial numbers.

Numbers are exact: every reported value is a reduced fraction string or an
integer string; no `float` is used and no value is rounded.

## Acceptance examples

`fixtures/fieldbook/example.json` fixes A and B at 0 and observes A→B at +4
and +5 and B→A at -4 and -5.  Both runs walk A→B→A with tolerance 0.  With
R1 in `[10, 18]` and R2 in `[12, 18]`, R1 must take `a, c` and R2 must take
`b, d`; both are `confirmed`.  Widening R2 to `[10, 18]` makes both runs
`ambiguous`, although the closure is 0 either way.  Changing `d` to -6 keeps
each run solvable alone, but the batch becomes `infeasible` with
`locally_impossible_runs: []`.  `fixtures/fieldbook/contested.json` is the
widened-window variant, and `fixtures/fieldbook/missing-page.json` contains a
run whose stations have no candidate rows at all, so it appears in
`locally_impossible_runs` while the other runs are locally fileable.
