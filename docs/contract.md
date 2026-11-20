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
