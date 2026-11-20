"""Survey batch acceptance and work-lot sign-off with exact reserve ratios.

A survey batch is accepted when one shared reserve fraction ``r`` makes every
directed reading jointly consistent with some elevation assignment: each
observation bounds the true difference ``h[to] - h[from]`` to
``[delta - nominal - r*reserve, delta + nominal + r*reserve]`` while every
fixed benchmark stays fixed.  The minimal such ``r`` is a maximum cycle
ratio over the constraint graph and is computed by exact rational
root-chasing -- never by per-row choices, dropped rows or approximate
search.  A batch still infeasible at ``r = 1`` is rejected but still
reported, with every lot marked ``not_evaluated``.

Each work lot is then judged against *all* elevations compatible with the
minimal ratio: ``pass`` when every compatible assignment satisfies every
clause (exact shortest-path extrema over the compatible polyhedron, never
one fitted profile), ``fail`` when no assignment satisfies the lot's
clauses jointly (a negative cycle through measurements, benchmarks and the
clauses), otherwise ``conditional`` with one satisfying and one violating
compatible profile for field verification.  All arithmetic is exact
:class:`~fractions.Fraction`; no ``float`` is ever used.
"""
from collections import namedtuple
from fractions import Fraction

from .common import array, fields, text
from .linear import rational

SCHEMA = 'level-accept-v1'
MAX_POINTS = 60
MAX_OBSERVATIONS = 2000
MAX_LOTS = 20
MAX_CLAUSES = 20


class _ZeroReference:
    """The shared zero-elevation node every absolute bound starts from."""
    __slots__ = ()

    def __repr__(self):
        return '<zero-reference>'


ZERO = _ZeroReference()

# One constraint edge: h[end] - h[start] <= constant + r*coefficient.
# kind/ref/side identify the originating input row for contradiction
# chains; synthetic forcing edges carry empty identifiers and never appear
# in a reported chain.
Edge = namedtuple('Edge', ['start', 'end', 'constant', 'coefficient',
                           'kind', 'ref', 'side'])


def _node_key(node):
    return (0, '') if node is ZERO else (1, node)


def _edge_key(edge):
    return (_node_key(edge.start), _node_key(edge.end),
            edge.kind, edge.ref, edge.side)


# --------------------------------------------------------------------- input

def _points(raw):
    fixed, all_points = {}, set()
    for row in array(raw, MAX_POINTS):
        fields(row, 'id elevation')
        pid = text(row['id'])
        if pid in all_points:
            raise ValueError('duplicate point')
        all_points.add(pid)
        if row['elevation'] is not None:
            fixed[pid] = rational(row['elevation'])
    if not fixed:
        raise ValueError('at least one benchmark required')
    return fixed, all_points


def _observations(raw, all_points):
    observations, ids = [], set()
    for row in array(raw, MAX_OBSERVATIONS):
        fields(row, 'id from to delta nominal reserve')
        oid = text(row['id'])
        start, end = text(row['from']), text(row['to'])
        if (oid in ids or start not in all_points
                or end not in all_points or start == end):
            raise ValueError('invalid observation identity or endpoints')
        ids.add(oid)
        delta = rational(row['delta'])
        nominal = rational(row['nominal'])
        reserve = rational(row['reserve'])
        if nominal < 0 or reserve < 0:
            raise ValueError('nominal and reserve must be non-negative')
        observations.append((oid, start, end, delta, nominal, reserve))
    if not observations:
        raise ValueError('observations required')
    return observations


def _lots(raw, all_points):
    lots, lot_ids = [], set()
    for row in array(raw, MAX_LOTS):
        fields(row, 'id clauses')
        lot_id = text(row['id'])
        if lot_id in lot_ids:
            raise ValueError('duplicate lot id')
        lot_ids.add(lot_id)
        clauses, clause_ids = [], set()
        for entry in array(row['clauses'], MAX_CLAUSES):
            if not isinstance(entry, dict):
                raise ValueError('invalid clause')
            keys = set(entry)
            cid = text(entry['id']) if 'id' in entry else None
            if cid is None or cid in clause_ids:
                raise ValueError('invalid or duplicate clause id')
            clause_ids.add(cid)
            if keys == {'id', 'point', 'min', 'max'}:
                point = text(entry['point'])
                if point not in all_points:
                    raise ValueError('clause references unknown point')
                low, high = rational(entry['min']), rational(entry['max'])
                clause = ('point', cid, point, low, high)
            elif keys == {'id', 'from', 'to', 'min', 'max'}:
                start, end = text(entry['from']), text(entry['to'])
                if start not in all_points or end not in all_points:
                    raise ValueError('clause references unknown point')
                if start == end:
                    raise ValueError('clause endpoints must differ')
                low, high = rational(entry['min']), rational(entry['max'])
                clause = ('difference', cid, start, end, low, high)
            else:
                raise ValueError('invalid clause shape')
            if low > high:
                raise ValueError('clause min exceeds max')
            clauses.append(clause)
        if not clauses:
            raise ValueError('at least one clause required')
        clauses.sort(key=lambda clause: clause[1])
        lots.append((lot_id, clauses))
    if not lots:
        raise ValueError('at least one lot required')
    lots.sort(key=lambda lot: lot[0])
    return lots


# -------------------------------------------------------------------- graphs

def _survey_edges(fixed, observations):
    edges = []
    for oid, start, end, delta, nominal, reserve in observations:
        edges.append(Edge(start, end, delta + nominal, reserve,
                          'observation', oid, 'upper'))
        edges.append(Edge(end, start, nominal - delta, reserve,
                          'observation', oid, 'lower'))
    for pid, elevation in fixed.items():
        edges.append(Edge(ZERO, pid, elevation, Fraction(0),
                          'benchmark', pid, 'upper'))
        edges.append(Edge(pid, ZERO, -elevation, Fraction(0),
                          'benchmark', pid, 'lower'))
    edges.sort(key=_edge_key)
    return edges


def _clause_endpoints(clause):
    if clause[0] == 'point':
        return ZERO, clause[2]
    return clause[2], clause[3]


def _clause_edges(clauses):
    edges = []
    for clause in clauses:
        start, end = _clause_endpoints(clause)
        low, high = clause[-2], clause[-1]
        edges.append(Edge(start, end, high, Fraction(0),
                          'clause', clause[1], 'upper'))
        edges.append(Edge(end, start, -low, Fraction(0),
                          'clause', clause[1], 'lower'))
    return edges


def _weights(edges, ratio):
    return [edge.constant + ratio * edge.coefficient for edge in edges]


# -------------------------------------------------------------- exact solver

def _relax(nodes, edges, weights):
    """Bellman-Ford from a virtual zero-distance super-source.

    Returns ``(dist, pred, changed)`` where ``changed`` is a node still
    relaxed in the n-th round (witnessing a negative cycle) or ``None``.
    The fixed edge order makes every extracted witness reproducible.
    """
    dist = {node: Fraction(0) for node in nodes}
    pred = {}
    changed = None
    for _ in range(len(nodes)):
        changed = None
        for position, edge in enumerate(edges):
            candidate = dist[edge.start] + weights[position]
            if dist[edge.end] > candidate:
                dist[edge.end] = candidate
                pred[edge.end] = edge
                changed = edge.end
        if changed is None:
            break
    return dist, pred, changed


def _negative_cycle(nodes, edges, weights):
    """One directed cycle of negative weight, rotated to a canonical start."""
    _, pred, changed = _relax(nodes, edges, weights)
    if changed is None:
        return None
    node = changed
    for _ in range(len(nodes)):
        node = pred[node].start
    start = node
    cycle = []
    while True:
        edge = pred[node]
        cycle.append(edge)
        node = edge.start
        if node == start:
            break
    cycle.reverse()
    weight_of = dict(zip(edges, weights))
    if sum((weight_of[edge] for edge in cycle), Fraction(0)) >= 0:
        raise ValueError('internal cycle extraction failure')
    pivot = min(range(len(cycle)), key=lambda i: _node_key(cycle[i].start))
    return cycle[pivot:] + cycle[:pivot]


def _shortest_paths(nodes, edges, weights):
    dist, _, changed = _relax(nodes, edges, weights)
    if changed is not None:
        raise ValueError('constraints are jointly infeasible')
    return dist


def _minimal_reserve(nodes, edges):
    """Exact minimal ratio in [0, 1] making the survey graph feasible.

    Assumes feasibility at ratio 1.  Each negative cycle ``A + r*B`` found
    at the current ratio is negative exactly up to its root ``-A/B`` (the
    ratio-1 precheck guarantees ``B > 0``), so jumping to that root never
    skips a feasible ratio; the cycle that reaches zero exactly is the
    proof that any smaller ratio is infeasible.
    """
    ratio = Fraction(0)
    cycle = _negative_cycle(nodes, edges, _weights(edges, ratio))
    if cycle is None:
        return ratio, None
    while True:
        constant = sum((edge.constant for edge in cycle), Fraction(0))
        coefficient = sum((edge.coefficient for edge in cycle), Fraction(0))
        if coefficient <= 0:
            raise ValueError('infeasible constant cycle escaped precheck')
        ratio = -constant / coefficient
        tighter = _negative_cycle(nodes, edges, _weights(edges, ratio))
        if tighter is None:
            return ratio, cycle
        cycle = tighter


def _all_pairs(nodes, edges, weights):
    """Exact all-pairs shortest paths; ``None`` marks an unreachable pair."""
    dist = {u: {v: None for v in nodes} for u in nodes}
    for node in nodes:
        dist[node][node] = Fraction(0)
    for position, edge in enumerate(edges):
        current = dist[edge.start][edge.end]
        if current is None or weights[position] < current:
            dist[edge.start][edge.end] = weights[position]
    ordered = sorted(nodes, key=_node_key)
    for mid in ordered:
        row_mid = dist[mid]
        for start in ordered:
            row_start = dist[start]
            leg_in = row_start[mid]
            if leg_in is None:
                continue
            for end in ordered:
                leg_out = row_mid[end]
                if leg_out is None:
                    continue
                candidate = leg_in + leg_out
                current = row_start[end]
                if current is None or candidate < current:
                    row_start[end] = candidate
    return dist


def _profile(nodes, edges, weights, point_ids):
    """One compatible elevation assignment, shifted so the zero reference
    sits at zero and every benchmark takes exactly its fixed elevation."""
    dist = _shortest_paths(nodes, edges, weights)
    base = dist[ZERO]
    return {pid: dist[pid] - base for pid in point_ids}


def _forcing_edge(clause, side, distances):
    """One extra edge pinning the clause value to a violating extreme.

    The extreme is the tightest survey-implied bound (or one step past the
    clause bound when the value is unbounded), so the augmented system
    stays feasible and every resulting profile violates the clause.
    """
    start, end = _clause_endpoints(clause)
    low, high = clause[-2], clause[-1]
    if side == 'upper':
        limit = distances[start][end]
        value = limit if limit is not None else high + 1
        return Edge(end, start, -value, Fraction(0), '', '', '')
    limit = distances[end][start]
    value = -limit if limit is not None else low - 1
    return Edge(start, end, value, Fraction(0), '', '', '')


# -------------------------------------------------------------------- report

def _chain(cycle):
    return [{'kind': edge.kind, 'id': edge.ref, 'side': edge.side}
            for edge in cycle]


def _strings(profile):
    return {pid: str(profile[pid]) for pid in sorted(profile)}


def _evaluate_lot(lot_id, clauses, nodes, survey_edges, ratio, distances,
                  point_ids):
    edges = sorted(survey_edges + _clause_edges(clauses), key=_edge_key)
    weights = _weights(edges, ratio)
    row = {'id': lot_id, 'status': None, 'satisfying_profile': None,
           'violating_profile': None, 'proof': None}
    cycle = _negative_cycle(nodes, edges, weights)
    if cycle is not None:
        row['status'] = 'fail'
        row['proof'] = _chain(cycle)
        return row
    violable = None
    for clause in clauses:
        start, end = _clause_endpoints(clause)
        low, high = clause[-2], clause[-1]
        upper = distances[start][end]
        if upper is None or upper > high:
            violable = (clause, 'upper')
            break
        lower = distances[end][start]
        if lower is None or -lower < low:
            violable = (clause, 'lower')
            break
    if violable is None:
        row['status'] = 'pass'
        return row
    row['status'] = 'conditional'
    row['satisfying_profile'] = _strings(
        _profile(nodes, edges, weights, point_ids))
    forcing = _forcing_edge(violable[0], violable[1], distances)
    violating_edges = sorted(survey_edges + [forcing], key=_edge_key)
    row['violating_profile'] = _strings(_profile(
        nodes, violating_edges, _weights(violating_edges, ratio), point_ids))
    return row


def accept(data):
    """Validate an acceptance request and return its deterministic report."""
    fields(data, 'points observations lots')
    fixed, all_points = _points(data['points'])
    observations = _observations(data['observations'], all_points)
    lots = _lots(data['lots'], all_points)
    point_ids = sorted(all_points)
    nodes = [ZERO] + point_ids
    survey_edges = _survey_edges(fixed, observations)

    rejected = _negative_cycle(nodes, survey_edges,
                               _weights(survey_edges, Fraction(1)))
    if rejected is not None:
        return {
            'schema': SCHEMA,
            'survey': {'status': 'rejected', 'reserve_fraction': None,
                       'profile': None, 'proof': _chain(rejected)},
            'lots': [{'id': lot_id, 'status': 'not_evaluated',
                      'satisfying_profile': None, 'violating_profile': None,
                      'proof': None} for lot_id, _ in lots]}

    ratio, tight = _minimal_reserve(nodes, survey_edges)
    weights = _weights(survey_edges, ratio)
    survey = {'status': 'accepted',
              'reserve_fraction': str(ratio),
              'profile': _strings(_profile(nodes, survey_edges, weights,
                                           point_ids)),
              'proof': None if tight is None else _chain(tight)}
    distances = _all_pairs(nodes, survey_edges, weights)
    lot_rows = [_evaluate_lot(lot_id, clauses, nodes, survey_edges, ratio,
                              distances, point_ids)
                for lot_id, clauses in lots]
    return {'schema': SCHEMA, 'survey': survey, 'lots': lot_rows}
