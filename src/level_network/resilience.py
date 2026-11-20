"""Single-observation failure resilience review for leveling delivery targets.

The official result is the ordinary exact ``level-network`` adjustment over
the complete observation set.  Each review scenario then treats exactly one
input observation as temporarily unavailable, removes only that observation
and re-solves the remaining network with the same exact adjustment semantics.
Scenarios are independent: removals never accumulate, and benchmarks,
weights and every other input stay untouched.

For an admissible scenario the leave-one-out solution is obtained with an
exact rank-one update of the normal equations.  This is algebraically the
same solution the ordinary adjustment produces for the reduced observation
set; every value here remains a :class:`~fractions.Fraction`, so no
comparison ever passes through ``float``.  Reduced normal equations that
become singular -- an unknown component without a fixed benchmark -- are
reported as ``unavailable`` for every target rather than invented.
"""
from fractions import Fraction

from .adjustment import adjust
from .common import array, fields, text
from .linear import rational

REPORT_SCHEMA = 'level-resilience-report-v1'
NOTE = ('Each scenario treats exactly one observation as unavailable and '
        're-solves the remaining network independently; removals never '
        'accumulate and no benchmark, weight or other input is changed.')
AVAILABLE = 'available'
UNAVAILABLE = 'unavailable'


# ------------------------------------------------------------------- input

def _targets(raw, raw_points):
    known, fixed = set(), set()
    for row in raw_points:
        fields(row, 'id elevation')
        pid = text(row['id'])
        known.add(pid)
        if row['elevation'] is not None:
            fixed.add(pid)
    targets, seen = [], set()
    for value in array(raw, 10):
        pid = text(value)
        if pid in seen:
            raise ValueError('duplicate target point')
        if pid not in known:
            raise ValueError('unknown target point')
        if pid in fixed:
            raise ValueError('fixed point cannot be a target')
        seen.add(pid)
        targets.append(pid)
    if not targets:
        raise ValueError('at least one target point required')
    return targets


def _tolerances(raw, targets):
    if not isinstance(raw, dict):
        raise ValueError('tolerances object required')
    if set(raw) != set(targets):
        raise ValueError('tolerances must cover exactly the target points')
    limits = {}
    for pid in targets:
        value = rational(raw[pid])
        if value < 0:
            raise ValueError('tolerance must be non-negative')
        limits[pid] = value
    return limits


# --------------------------------------------------------------- adjustment

def _model(points, observations):
    """Build the exact normal equations of the full observation set.

    Only the coefficients over unknown points matter; fixed elevations are
    folded into each observation's constant.  The returned rows let the
    rank-one update recover every leave-one-out solution without rebuilding
    the network.
    """
    fixed, unknown = {}, []
    for row in points:
        pid = row['id']
        if row['elevation'] is None:
            unknown.append(pid)
        else:
            fixed[pid] = rational(row['elevation'])
    unknown.sort()
    index = {pid: position for position, pid in enumerate(unknown)}

    terms = []
    for row in observations:
        coefficients = {}
        constant = Fraction(0)
        for pid, sign in ((row['from'], -1), (row['to'], 1)):
            if pid in fixed:
                constant += sign * fixed[pid]
            else:
                coefficients[index[pid]] = Fraction(sign)
        terms.append({'id': row['id'], 'coefficients': coefficients,
                      'target': rational(row['delta']) - constant,
                      'weight': rational(row['weight'])})

    size = len(unknown)
    normal = [[Fraction(0) for _ in range(size)] for _ in range(size)]
    for term in terms:
        weight = term['weight']
        for i, left in term['coefficients'].items():
            row = normal[i]
            for j, right in term['coefficients'].items():
                row[j] += weight * left * right
    return unknown, index, terms, normal


def _inverse(matrix):
    """Exact Gauss-Jordan inverse; the full model is already known solvable."""
    size = len(matrix)
    rows = [list(matrix[i]) + [Fraction(1 if i == j else 0)
                               for j in range(size)]
            for i in range(size)]
    for col in range(size):
        pivot = next((row for row in range(col, size)
                      if rows[row][col]), None)
        if pivot is None:
            raise ValueError('network has unidentifiable datum')
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = rows[col][col]
        if scale != 1:
            rows[col] = [value / scale for value in rows[col]]
        for row in range(size):
            if row != col and rows[row][col]:
                scale = rows[row][col]
                rows[row] = [left - scale * right
                             for left, right in zip(rows[row], rows[col])]
    return [row[size:] for row in rows]


def _leave_one_out(term, solution, inverse, size):
    """Exact solution of the full model minus ``term``.

    Returns ``None`` when the reduced normal equations are singular.  For a
    non-singular full model that happens exactly when the reduced network has
    an unknown component without a benchmark, which the ordinary adjustment
    rejects with the same datum failure.
    """
    coefficients = term['coefficients']
    weight = term['weight']
    offset = Fraction(0)
    for position, value in coefficients.items():
        offset += value * solution[position]
    residual = offset - term['target']

    if size:
        direction = [Fraction(0) for _ in range(size)]
        for position, value in coefficients.items():
            if value:
                column = [inverse[row][position]
                          for row in range(size)]
                if value != 1:
                    column = [value * item for item in column]
                direction = [left + right
                             for left, right in zip(direction, column)]
        scalar = sum((value * direction[position]
                      for position, value in coefficients.items()),
                     Fraction(0))
    else:
        direction, scalar = [], Fraction(0)

    denominator = 1 - weight * scalar
    if denominator == 0:
        return None
    factor = weight * residual / denominator
    return [value + factor * step
            for value, step in zip(solution, direction)]


# ------------------------------------------------------------------ report

def _row(point, status, elevation=None, displacement=None, within=None):
    return {'point': point, 'status': status, 'elevation': elevation,
            'displacement': displacement, 'within_tolerance': within}


def review(data):
    """Validate a review request and return its deterministic report."""
    fields(data, 'points observations target_points tolerances')
    points = array(data['points'], 60)
    observations = array(data['observations'], 2000)
    targets = _targets(data['target_points'], points)
    limits = _tolerances(data['tolerances'], targets)

    # The official result is the ordinary full-network adjustment.  Its
    # failure is fatal: no review report may be derived from an invalid
    # baseline.
    official = adjust({'points': points, 'observations': observations})
    official_heights = {pid: Fraction(value)
                        for pid, value in official['elevations'].items()}

    unknown, index, terms, normal = _model(points, observations)
    solution = [official_heights[pid] for pid in unknown]
    inverse = _inverse(normal)
    size = len(unknown)
    ordered = sorted(terms, key=lambda term: term['id'])
    ordered_targets = sorted(targets)

    scenarios = []
    for term in ordered:
        if len(ordered) == 1:
            reduced, reason = None, 'no_observations'
        else:
            reduced = _leave_one_out(term, solution, inverse, size)
            reason = None if reduced is not None else 'unidentifiable_datum'
        row_targets = []
        for pid in ordered_targets:
            if reduced is None:
                row_targets.append(_row(pid, UNAVAILABLE))
                continue
            height = reduced[index[pid]]
            distance = abs(height - official_heights[pid])
            row_targets.append(_row(
                pid, AVAILABLE, str(height), str(distance),
                distance <= limits[pid]))
        scenarios.append({
            'observation': term['id'],
            'status': AVAILABLE if reduced is not None else UNAVAILABLE,
            'reason': reason,
            'targets': row_targets})

    target_rows = []
    for position, pid in enumerate(ordered_targets):
        rows = [scenario['targets'][position] for scenario in scenarios]
        statuses = [row['status'] for row in rows]
        if UNAVAILABLE in statuses:
            conclusion = 'delivery_at_risk'
        elif any(row['within_tolerance'] is False for row in rows):
            conclusion = 'movement_at_risk'
        else:
            conclusion = 'resilient'
        target_rows.append({
            'point': pid,
            'official_elevation': str(official_heights[pid]),
            'tolerance': str(limits[pid]),
            'conclusion': conclusion})

    return {
        'schema': REPORT_SCHEMA,
        'note': NOTE,
        'official_elevations': {pid: str(official_heights[pid])
                                for pid in ordered_targets},
        'targets': target_rows,
        'scenarios': scenarios}
