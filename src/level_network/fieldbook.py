"""Offline reconstruction of official field-book rows for leveling runs.

An exported field-book mixes every survey run together.  Each row keeps its
unique serial number, its measured direction, its observation interval and
its exact backsight/foresight readings, but the run number was lost.  The
archive holds the planned walking order (``stops``) and the allowed working
window of every run, so the task is to decide whether the rows can be
returned to the runs and, where the answer is unique, to publish the official
rows.

A run is *locally* fileable when one row per station can be chosen without
regard to any other run: the rows' directions match the station, both row
times lie inside the run window, the row intervals respect the walking order,
every serial is used at most once inside that run, and the signed closure
error is within the run tolerance.  A batch is fileable only when one choice
per run exists and no serial number is used twice across the whole batch.

The reconstruction is a finite constraint problem over the (at most 4096)
locally valid row sequences of each run: two runs are compatible when their
chosen sequences share no serial number.  Runs that share no candidate serial
decompose into independent components, so feasibility, uniqueness and the
per-run counterexamples are decided component by component with deterministic
backtracking.  All deltas, closure errors and comparisons are exact
:class:`~fractions.Fraction` values; no ``float`` is used.
"""
from collections import namedtuple
from fractions import Fraction
import itertools

from .common import array, fields, integer, text
from .linear import rational

SCHEMA = 'level-fieldbook-v1'
MAX_POINTS = 60
MAX_RECORDS = 2000
MAX_RUNS = 8
MAX_STOPS = 7
MAX_CANDIDATES = 4

Record = namedtuple('Record',
                    'id origin destination start end delta')
Run = namedtuple('Run', 'id stops start end tolerance')
Sequence = namedtuple('Sequence', 'ids mask net closure')


# ------------------------------------------------------------------- input

def _points(raw):
    points = {}
    for row in array(raw, MAX_POINTS):
        fields(row, 'id elevation')
        pid = text(row['id'])
        if pid in points:
            raise ValueError('duplicate point')
        elevation = row['elevation']
        points[pid] = (None if elevation is None
                       else rational(elevation))
    return points


def _records(raw, points):
    records = {}
    for row in array(raw, MAX_RECORDS):
        fields(row, 'id from to start end backsight foresight')
        rid = text(row['id'])
        origin, destination = text(row['from']), text(row['to'])
        if rid in records:
            raise ValueError('duplicate record id')
        if (origin not in points or destination not in points
                or origin == destination):
            raise ValueError('invalid record identity or endpoints')
        start = integer(row['start'])
        end = integer(row['end'])
        if start >= end:
            raise ValueError('record interval must be positive')
        backsight = rational(row['backsight'])
        foresight = rational(row['foresight'])
        if backsight < 0 or foresight < 0:
            raise ValueError('readings must be non-negative')
        records[rid] = Record(rid, origin, destination, start, end,
                              backsight - foresight)
    return records


def _runs(raw, points):
    runs, ids = [], set()
    for row in array(raw, MAX_RUNS):
        fields(row, 'id stops start end tolerance')
        rid = text(row['id'])
        if rid in ids:
            raise ValueError('duplicate run id')
        ids.add(rid)
        raw_stops = array(row['stops'], MAX_STOPS)
        if len(raw_stops) < 2:
            raise ValueError('a run needs at least two stops')
        stops = []
        for value in raw_stops:
            pid = text(value)
            if pid not in points:
                raise ValueError('run references unknown point')
            stops.append(pid)
        for first, second in zip(stops, stops[1:]):
            if first == second:
                raise ValueError('adjacent stops must differ')
        if points[stops[0]] is None or points[stops[-1]] is None:
            raise ValueError('run endpoints must be known benchmarks')
        start = integer(row['start'])
        end = integer(row['end'])
        if start >= end:
            raise ValueError('run window must be positive')
        tolerance = rational(row['tolerance'])
        if tolerance < 0:
            raise ValueError('tolerance must be non-negative')
        runs.append(Run(rid, tuple(stops), start, end, tolerance))
    if not runs:
        raise ValueError('at least one run required')
    runs.sort(key=lambda run: run.id)
    return runs


# -------------------------------------------------------------- candidates

def _station_candidates(run, records):
    """The candidate rows of every station, in record-id order."""
    stations = []
    for origin, destination in zip(run.stops, run.stops[1:]):
        candidates = [record for record in records
                      if record.origin == origin
                      and record.destination == destination
                      and record.start >= run.start
                      and record.end <= run.end]
        if len(candidates) > MAX_CANDIDATES:
            raise ValueError('more than four candidate records at a station')
        stations.append(candidates)
    return stations


def _run_sequences(run, stations, points, bits):
    """All locally valid row sequences of one run, sorted by serial tuple."""
    first_height = points[run.stops[0]]
    last_height = points[run.stops[-1]]
    difference = last_height - first_height
    sequences = []
    for chosen in itertools.product(*stations):
        if len(set(chosen)) != len(chosen):
            continue
        if any(chosen[index].end > chosen[index + 1].start
               for index in range(len(chosen) - 1)):
            continue
        net = sum((record.delta for record in chosen), Fraction(0))
        closure = net - difference
        if abs(closure) > run.tolerance:
            continue
        mask = 0
        for record in chosen:
            mask |= 1 << bits[record.id]
        sequences.append(Sequence(tuple(record.id for record in chosen),
                                  mask, net, closure))
    sequences.sort(key=lambda sequence: sequence.ids)
    return sequences


# ---------------------------------------------------------------- feasibility

def _matching_possible(domains):
    """Relaxed disjoint-record feasibility for the remaining runs.

    Ignoring the within-run time order, each run needs one distinct record
    per station, drawn from the records its remaining sequences could use.
    A bipartite b-matching between run stations and records must exist; when
    it does not, the branch is hopeless.  This is only a necessary condition,
    so it never discards a real solution.
    """
    pools = []
    for rid in sorted(domains):
        pool = set()
        for sequence in domains[rid]:
            pool.update(sequence.ids)
        pools.extend([tuple(sorted(pool))] * len(domains[rid][0].ids))
    owner = {}
    for unit, pool in enumerate(pools):
        if not _augment(unit, pool, pools, owner, set()):
            return False
    return True


def _augment(unit, pool, pools, owner, seen):
    for record in pool:
        if record in seen:
            continue
        seen.add(record)
        holder = owner.get(record)
        if holder is None or _augment(holder, pools[holder], pools, owner,
                                      seen):
            owner[record] = unit
            return True
    return False


def _solve(domains):
    """Deterministic backtracking over run sequences with disjoint serials."""
    return _search({rid: domains[rid] for rid in sorted(domains)}, 0, set())


def _search(domains, used, dead):
    if not domains:
        return {}
    filtered, choice = {}, None
    for rid, options in domains.items():
        compatible = [sequence for sequence in options
                      if sequence.mask & used == 0]
        if not compatible:
            return None
        filtered[rid] = compatible
        if choice is None or len(compatible) < len(filtered[choice]):
            choice = rid
    if not _matching_possible(filtered):
        return None
    relevant = 0
    for options in filtered.values():
        for sequence in options:
            relevant |= sequence.mask
    key = (tuple((rid, tuple(sequence.ids for sequence in options))
                 for rid, options in sorted(filtered.items())),
           used & relevant)
    if key in dead:
        return None
    for sequence in filtered[choice]:
        rest = {rid: options for rid, options in filtered.items()
                if rid != choice}
        result = _search(rest, used | sequence.mask, dead)
        if result is not None:
            result[choice] = sequence
            return result
    dead.add(key)
    return None


class _Components:
    def __init__(self, run_ids):
        self.parent = {rid: rid for rid in run_ids}

    def find(self, rid):
        root = rid
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[rid] != rid:
            nxt = self.parent[rid]
            self.parent[rid] = root
            rid = nxt
        return root

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left

    def groups(self):
        groups = {}
        for rid in self.parent:
            groups.setdefault(self.find(rid), []).append(rid)
        return [sorted(group) for group in
                sorted(groups.values(), key=lambda group: sorted(group))]


# ----------------------------------------------------------------- assembly

def _report_infeasible(impossible):
    return {'schema': SCHEMA, 'status': 'infeasible', 'assignment': None,
            'runs': [], 'locally_impossible_runs': sorted(impossible)}


def _assignment_view(assignment, runs):
    return {run.id: list(assignment[run.id].ids) for run in runs}


def fieldbook(data):
    fields(data, 'points records runs')
    points = _points(data['points'])
    records = _records(data['records'], points)
    runs = _runs(data['runs'], points)
    ordered = sorted(records.values(), key=lambda record: record.id)

    station_rows = {}
    candidate_ids = set()
    for run in runs:
        stations = _station_candidates(run, ordered)
        station_rows[run.id] = stations
        for candidates in stations:
            candidate_ids.update(record.id for record in candidates)
    bits = {rid: position
            for position, rid in enumerate(sorted(candidate_ids))}

    domains = {}
    for run in runs:
        domains[run.id] = _run_sequences(run, station_rows[run.id],
                                         points, bits)

    impossible = [run.id for run in runs if not domains[run.id]]
    if impossible:
        return _report_infeasible(impossible)

    # Runs are coupled only through serials that occur in their valid
    # sequences; independent components can be solved separately.
    union = _Components([run.id for run in runs])
    owner = {}
    for run in runs:
        seen = set()
        for sequence in domains[run.id]:
            seen.update(sequence.ids)
        for rid in seen:
            if rid in owner:
                union.union(run.id, owner[rid])
            else:
                owner[rid] = run.id

    components = union.groups()
    witness = {}
    component_solution = {}
    for component in components:
        solution = _solve({rid: domains[rid] for rid in component})
        if solution is None:
            return _report_infeasible([])
        component_solution[tuple(component)] = solution
        witness.update(solution)

    ambiguous = {}
    for component in components:
        subdomains = {rid: domains[rid] for rid in component}
        baseline = component_solution[tuple(component)]
        for rid in component:
            if len(component) == 1:
                alternatives = [sequence for sequence in domains[rid]
                                if sequence.ids != baseline[rid].ids]
                if not alternatives:
                    continue
                alternative = dict(witness)
                alternative[rid] = alternatives[0]
            else:
                narrowed = dict(subdomains)
                narrowed[rid] = [sequence for sequence in subdomains[rid]
                                 if sequence.ids != baseline[rid].ids]
                if not narrowed[rid]:
                    continue
                solution = _solve(narrowed)
                if solution is None:
                    continue
                alternative = dict(witness)
                alternative.update(solution)
            ambiguous[rid] = alternative

    rows = []
    for run in runs:
        sequence = witness[run.id]
        if run.id in ambiguous:
            rows.append({'id': run.id, 'status': 'ambiguous',
                         'record_ids': None, 'net_delta': None,
                         'closure_error': None,
                         'alternative': _assignment_view(ambiguous[run.id],
                                                         runs)})
        else:
            rows.append({'id': run.id, 'status': 'confirmed',
                         'record_ids': list(sequence.ids),
                         'net_delta': str(sequence.net),
                         'closure_error': str(sequence.closure),
                         'alternative': None})
    return {'schema': SCHEMA, 'status': 'feasible',
            'assignment': _assignment_view(witness, runs),
            'runs': rows, 'locally_impossible_runs': []}
