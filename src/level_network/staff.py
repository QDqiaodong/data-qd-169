"""Two-stage leveling staff calibration handover.

The calibration team and the delivery team never share calibration field
points, observation identities or row-level readings.  The calibration stage
exports only the *profile cost* of the staff zero errors: after every unknown
calibration-point height is minimized away, a convex quadratic in the staff
errors remains, keeping its constant term and the coupling between staff.
The delivery stage adds that exact quadratic to its own weighted residual
sum and minimizes the joint objective over delivery heights and the same
staff errors.

Every quantity is an exact :class:`~fractions.Fraction`; no ``float`` is ever
used.  Singular normal equations are legal in both stages (unanchored
calibration components, flat staff directions), so Gauss-Jordan elimination
returns particular solutions and leaves uniqueness to explicit consistency
checks instead of pinning undetermined quantities to zero.
"""
from fractions import Fraction
import re

from .common import array, fields, text
from .linear import rational

CALIBRATION_SCHEMA = 'level-staff-calibration-v1'
DELIVERY_SCHEMA = 'level-staff-delivery-v1'
RESOLVED = 'resolved'
UNRESOLVED = 'unresolved'
MAX_POINTS = 60
MAX_OBSERVATIONS = 2000
MAX_RODS = 12
MAX_TARGETS = 20

# Package numbers are reduced fraction or integer strings, never decimals.
_PACKAGE_NUMBER = re.compile(r'-?[0-9]+(?:/[0-9]+)?')


# --------------------------------------------------------------------- input

def _points(raw):
    """Validate the shared point contract; unknown ids come back sorted."""
    fixed, unknown, all_points = {}, [], set()
    for row in array(raw, MAX_POINTS):
        fields(row, 'id elevation')
        pid = text(row['id'])
        if pid in all_points:
            raise ValueError('duplicate point')
        all_points.add(pid)
        if row['elevation'] is None:
            unknown.append(pid)
        else:
            fixed[pid] = rational(row['elevation'])
    if not fixed or not all_points:
        raise ValueError('at least one benchmark required')
    unknown.sort()
    return fixed, unknown, all_points


def _rods(raw):
    rods, seen = [], set()
    for value in array(raw, MAX_RODS):
        rid = text(value)
        if rid in seen:
            raise ValueError('duplicate staff identifier')
        seen.add(rid)
        rods.append(rid)
    if not rods:
        raise ValueError('at least one staff required')
    return rods


def _observations(raw, points, rods):
    """Parse observations into sparse (height vector, staff vector, target, w).

    Fixed benchmark elevations are folded into the observation target exactly
    like the ordinary adjustment contract.  Staff are matched by physical
    identifier, never by position or first appearance.
    """
    fixed, unknown, all_points = points
    height_index = {pid: i for i, pid in enumerate(unknown)}
    rod_index = {rid: i for i, rid in enumerate(rods)}
    terms, ids = [], set()
    for row in array(raw, MAX_OBSERVATIONS):
        fields(row, 'id from to delta weight from_staff to_staff')
        oid = text(row['id'])
        start, end = text(row['from']), text(row['to'])
        if (oid in ids or start not in all_points
                or end not in all_points or start == end):
            raise ValueError('invalid observation identity or endpoints')
        ids.add(oid)
        from_staff = text(row['from_staff'])
        to_staff = text(row['to_staff'])
        if from_staff not in rod_index or to_staff not in rod_index:
            raise ValueError('observation references unknown staff')
        delta = rational(row['delta'])
        weight = rational(row['weight'])
        if weight <= 0:
            raise ValueError('weight must be positive')
        height_coefficients = {}
        constant = Fraction(0)
        for pid, sign in ((start, -1), (end, 1)):
            if pid in fixed:
                constant += sign * fixed[pid]
            else:
                height_coefficients[height_index[pid]] = Fraction(sign)
        staff_coefficients = {}
        if from_staff != to_staff:
            staff_coefficients[rod_index[from_staff]] = Fraction(-1)
            staff_coefficients[rod_index[to_staff]] = Fraction(1)
        target = delta - constant
        terms.append((height_coefficients, staff_coefficients,
                      target, weight))
    if not terms:
        raise ValueError('observations required')
    return terms


def _targets(raw, all_points):
    queries, ids = [], set()
    for row in array(raw, MAX_TARGETS):
        if not isinstance(row, dict):
            raise ValueError('invalid target')
        keys = set(row)
        qid = text(row['id']) if 'id' in row else None
        if qid is None:
            raise ValueError('target id required')
        if qid in ids:
            raise ValueError('duplicate target id')
        if keys == {'id', 'point'}:
            pid = text(row['point'])
            if pid not in all_points:
                raise ValueError('unknown target point')
            ids.add(qid)
            queries.append(('point', qid, pid))
        elif keys == {'id', 'from', 'to'}:
            start, end = text(row['from']), text(row['to'])
            if start not in all_points or end not in all_points:
                raise ValueError('unknown target point')
            if start == end:
                raise ValueError('difference endpoints must differ')
            ids.add(qid)
            queries.append(('difference', qid, start, end))
        else:
            raise ValueError('invalid target shape')
    if not queries:
        raise ValueError('at least one target required')
    queries.sort(key=lambda query: query[1])
    return queries


# --------------------------------------------------------------- linear algebra

def _particular_solutions(matrix, rhs_columns):
    """Gauss-Jordan elimination tolerating singular (but consistent) systems.

    Returns one particular solution per RHS column, or ``None`` when that
    column is inconsistent with the matrix.  Free variables take zero, which
    is enough for every residual sum and quadratic value computed downstream:
    for a consistent system ``M x = y`` the scalar ``x.T y`` is identical for
    every particular solution because null-space directions are orthogonal to
    the range of ``M``.
    """
    size = len(matrix)
    if size == 0:
        return [[] for _ in rhs_columns]
    count = len(rhs_columns)
    rows = [list(matrix[i]) + [rhs_columns[j][i] for j in range(count)]
            for i in range(size)]
    pivot_row = {}
    rank = 0
    for col in range(size):
        pivot = next((r for r in range(rank, size) if rows[r][col]), None)
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        scale = rows[rank][col]
        rows[rank] = [value / scale for value in rows[rank]]
        for r in range(size):
            if r != rank and rows[r][col]:
                factor = rows[r][col]
                rows[r] = [left - factor * right
                           for left, right in zip(rows[r], rows[rank])]
        pivot_row[col] = rank
        rank += 1
    solutions = []
    for j in range(count):
        if any(rows[r][size + j] for r in range(rank, size)):
            solutions.append(None)
            continue
        solution = [Fraction(0) for _ in range(size)]
        for col, row in pivot_row.items():
            solution[col] = rows[row][size + j]
        solutions.append(solution)
    return solutions


def _normal_system(terms, height_count, rod_count):
    """Accumulate weighted normal equations over heights then staff errors."""
    size = height_count + rod_count
    matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
    rhs = [Fraction(0) for _ in range(size)]
    constant = Fraction(0)
    for height_coefficients, staff_coefficients, target, weight in terms:
        vector = [Fraction(0) for _ in range(size)]
        for index, value in height_coefficients.items():
            vector[index] = value
        for index, value in staff_coefficients.items():
            vector[height_count + index] = value
        for i, left in enumerate(vector):
            if not left:
                continue
            rhs[i] += weight * left * target
            row = matrix[i]
            for j, right in enumerate(vector):
                if right:
                    row[j] += weight * left * right
        constant += weight * target * target
    return matrix, rhs, constant


def _is_positive_semidefinite(matrix):
    """Exact PSD test via congruent diagonal pivoting.

    A symmetric matrix is PSD while every nonzero diagonal pivot is positive
    and the Schur complement stays PSD.  If the remaining diagonal is all zero
    but an off-diagonal entry is nonzero the matrix is indefinite (the
    difference of the two basis vectors is a negative direction).
    """
    size = len(matrix)
    work = [row[:] for row in matrix]
    alive = list(range(size))
    while alive:
        pivot = next((k for k in alive if work[k][k] > 0), None)
        if pivot is None:
            if any(work[k][k] < 0 for k in alive):
                return False
            for a in range(len(alive)):
                for b in range(a + 1, len(alive)):
                    if work[alive[a]][alive[b]] != 0:
                        return False
            return True
        pivot_value = work[pivot][pivot]
        rest = [k for k in alive if k != pivot]
        for a in rest:
            for b in rest:
                work[a][b] -= (work[a][pivot] * work[pivot][b]
                               / pivot_value)
        alive = rest
    return True


# ------------------------------------------------------------------ calibration

def calibrate(data):
    """Reduce calibration observations to the staff-only profile cost."""
    fields(data, 'points observations rods')
    rods = _rods(data['rods'])
    points = _points(data['points'])
    fixed, unknown, _ = points
    terms = _observations(data['observations'], points, rods)
    height_count, rod_count = len(unknown), len(rods)
    matrix, rhs, residual_constant = _normal_system(
        terms, height_count, rod_count)

    normal = [row[:height_count] for row in matrix[:height_count]]
    cross = [row[height_count:] for row in matrix[:height_count]]
    staff_block = [row[height_count:] for row in matrix[height_count:]]
    height_rhs = rhs[:height_count]
    staff_rhs = rhs[height_count:]

    # Solve N X = M (one RHS column per staff) and N x_k = k together.
    columns = [[cross[i][j] for i in range(height_count)]
               for j in range(rod_count)]
    columns.append(height_rhs)
    solutions = _particular_solutions(normal, columns)
    if any(solution is None for solution in solutions):
        # Mathematically unreachable: an A^T W A RHS always lies in range(N)
        # because the objective is a bounded sum of squares.
        raise ValueError('inconsistent calibration normal equations')
    staff_solutions = solutions[:rod_count]
    height_solution = solutions[rod_count]

    quadratic = [[Fraction(0) for _ in range(rod_count)]
                 for _ in range(rod_count)]
    linear = [Fraction(0) for _ in range(rod_count)]
    for i in range(rod_count):
        for j in range(rod_count):
            eliminated = Fraction(0)
            for t in range(height_count):
                eliminated += staff_solutions[i][t] * cross[t][j]
            quadratic[i][j] = staff_block[i][j] - eliminated
        coupled = Fraction(0)
        for t in range(height_count):
            coupled += staff_solutions[i][t] * height_rhs[t]
        linear[i] = coupled - staff_rhs[i]
    constant = residual_constant
    for t in range(height_count):
        constant -= height_solution[t] * height_rhs[t]

    ordered = sorted(rods)
    order = [rods.index(rid) for rid in ordered]
    return {
        'schema': CALIBRATION_SCHEMA,
        'rods': ordered,
        'quadratic': [[str(quadratic[order[i]][order[j]])
                       for j in range(rod_count)]
                      for i in range(rod_count)],
        'linear': [str(linear[order[i]]) for i in range(rod_count)],
        'constant': str(constant),
    }


# ------------------------------------------------------------- package reading

def _package_fraction(value):
    # The 8 MB input bound already caps every string, including this one.
    if not isinstance(value, str):
        raise ValueError('exact package number required')
    if not _PACKAGE_NUMBER.fullmatch(value):
        raise ValueError('package number must be an integer or fraction')
    if '/' in value:
        numerator_text, denominator_text = value.split('/')
        numerator, denominator = int(numerator_text), int(denominator_text)
        if denominator == 0:
            raise ValueError('zero fraction denominator')
        result = Fraction(numerator, denominator)
        if result.numerator != numerator or result.denominator != denominator:
            raise ValueError('package fraction must be reduced')
        return result
    return Fraction(int(value))


def load_package(raw):
    """Validate every exchange invariant and return ordered rational arrays."""
    fields(raw, 'schema rods quadratic linear constant')
    if raw['schema'] != CALIBRATION_SCHEMA:
        raise ValueError('unknown calibration package schema')
    rod_rows = array(raw['rods'], MAX_RODS)
    rods, seen = [], set()
    for value in rod_rows:
        rid = text(value)
        if rid in seen:
            raise ValueError('duplicate staff identifier in package')
        seen.add(rid)
        rods.append(rid)
    if not rods:
        raise ValueError('package must name at least one staff')
    if rods != sorted(rods):
        raise ValueError('package rods must be sorted')
    size = len(rods)

    quadratic_rows = array(raw['quadratic'], MAX_RODS)
    if len(quadratic_rows) != size:
        raise ValueError('quadratic dimension mismatch')
    quadratic = []
    for row in quadratic_rows:
        row = array(row, MAX_RODS)
        if len(row) != size:
            raise ValueError('quadratic dimension mismatch')
        quadratic.append([_package_fraction(value) for value in row])
    for i in range(size):
        for j in range(i + 1, size):
            if quadratic[i][j] != quadratic[j][i]:
                raise ValueError('quadratic must be symmetric')
    if not _is_positive_semidefinite(quadratic):
        raise ValueError('package has negative curvature')

    linear_rows = array(raw['linear'], MAX_RODS)
    if len(linear_rows) != size:
        raise ValueError('linear dimension mismatch')
    linear = [_package_fraction(value) for value in linear_rows]
    constant = _package_fraction(raw['constant'])

    # Adding one common offset to every staff error must leave the cost
    # unchanged: the all-ones direction is a null direction of the quadratic
    # and orthogonal to the linear term.
    for i in range(size):
        if sum(quadratic[i][j] for j in range(size)) != 0:
            raise ValueError('package changes under a common staff offset')
    if sum(linear) != 0:
        raise ValueError('package changes under a common staff offset')

    stationarity = _particular_solutions(
        quadratic, [[-linear[i] for i in range(size)]])[0]
    if stationarity is None:
        raise ValueError('package has no finite minimum')
    minimum = constant + sum(linear[i] * stationarity[i]
                             for i in range(size))
    if minimum < 0:
        raise ValueError('package minimum must not be negative')
    return rods, quadratic, linear, constant


# -------------------------------------------------------------------- delivery

def _query_vector(query, fixed, unknown, size):
    coefficient = [Fraction(0) for _ in range(size)]
    constant = Fraction(0)
    kind = query[0]
    if kind == 'point':
        pid = query[2]
        if pid in fixed:
            constant = fixed[pid]
        else:
            coefficient[unknown.index(pid)] = Fraction(1)
    else:
        _, _, start, end = query
        for pid, sign in ((start, -1), (end, 1)):
            if pid in fixed:
                constant += sign * fixed[pid]
            else:
                coefficient[unknown.index(pid)] += Fraction(sign)
    return coefficient, constant


def deliver(survey, package_raw):
    """Jointly minimize delivery residuals and the package staff cost."""
    rods, quadratic, linear, package_constant = load_package(package_raw)
    fields(survey, 'points observations targets')
    fixed, unknown, all_points = _points(survey['points'])
    terms = _observations(survey['observations'],
                          (fixed, unknown, all_points), rods)
    queries = _targets(survey['targets'], all_points)
    height_count, rod_count = len(unknown), len(rods)
    size = height_count + rod_count

    matrix, rhs, residual_constant = _normal_system(
        terms, height_count, rod_count)
    for i in range(rod_count):
        for j in range(rod_count):
            matrix[height_count + i][height_count + j] += quadratic[i][j]
        rhs[height_count + i] -= linear[i]

    query_vectors, query_constants = [], []
    for query in queries:
        coefficient, constant = _query_vector(
            query, fixed, unknown, size)
        query_vectors.append(coefficient)
        query_constants.append(constant)

    solutions = _particular_solutions(matrix, [rhs] + query_vectors)
    optimum = solutions[0]
    if optimum is None:
        # A bounded sum of squares plus a validated finite package minimum is
        # always bounded below, so inconsistency indicates a bad package.
        raise ValueError('delivery model has no finite minimum')
    minimum_cost = package_constant + residual_constant - sum(
        optimum[i] * rhs[i] for i in range(size))

    target_rows = []
    for position, query in enumerate(queries):
        qid = query[1]
        if solutions[position + 1] is None:
            target_rows.append({'id': qid, 'status': UNRESOLVED,
                                'value': None})
            continue
        value = query_constants[position] + sum(
            query_vectors[position][i] * optimum[i] for i in range(size))
        target_rows.append({'id': qid, 'status': RESOLVED,
                            'value': str(value)})
    return {'schema': DELIVERY_SCHEMA,
            'minimum_cost': str(minimum_cost),
            'targets': target_rows}
