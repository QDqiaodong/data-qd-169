"""Tests for the two-stage ``level-staff`` calibration handover.

The two acceptance handovers carry hand-derived expectations straight from
the contract.  A separate randomized oracle below rebuilds the *joint*
calibration-plus-delivery normal equations with its own Gauss-Jordan code
(independent of ``level_network.staff``) and checks both the exact joint
minimum and every target's uniqueness decision against the package handover,
which never sees the original calibration observations at delivery time.
"""
from fractions import Fraction
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from level_network.staff import (CALIBRATION_SCHEMA, DELIVERY_SCHEMA,
                                 calibrate, deliver, load_package)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'staff'
MODULE = 'level_network.staff_cli'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def point(pid, elevation=None):
    return {'id': pid, 'elevation': elevation}


def obs(oid, start, end, delta, from_staff, to_staff, weight='1'):
    return {'id': oid, 'from': start, 'to': end, 'delta': str(delta),
            'weight': str(weight), 'from_staff': from_staff,
            'to_staff': to_staff}


def load(name):
    return json.loads((FIXTURES / name).read_text())


def target(report, qid):
    return next(row for row in report['targets'] if row['id'] == qid)


# ------------------------------------------------------------- independent oracle

def _gauss_jordan(matrix, columns):
    """Standalone singular-tolerant Gauss-Jordan; None marks inconsistency."""
    size = len(matrix)
    width = len(columns)
    rows = [list(matrix[i]) + [columns[k][i] for k in range(width)]
            for i in range(size)]
    pivot_rows, rank = {}, 0
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
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[rank])]
        pivot_rows[col] = rank
        rank += 1
    answers = []
    for k in range(width):
        if any(rows[r][size + k] for r in range(rank, size)):
            answers.append(None)
            continue
        answer = [Fraction(0) for _ in range(size)]
        for col, row in pivot_rows.items():
            answer[col] = rows[row][size + k]
        answers.append(answer)
    return answers


def _oracle(calibration, survey, package_rods):
    """Joint minimum over both networks' heights and one shared staff vector."""
    cal_unknown = sorted(row['id'] for row in calibration['points']
                         if row['elevation'] is None)
    cal_fixed = {row['id']: Fraction(row['elevation'])
                 for row in calibration['points']
                 if row['elevation'] is not None}
    del_unknown = sorted(row['id'] for row in survey['points']
                         if row['elevation'] is None)
    del_fixed = {row['id']: Fraction(row['elevation'])
                 for row in survey['points']
                 if row['elevation'] is not None}
    rod_index = {rid: i for i, rid in enumerate(package_rods)}
    variables = ([('h', pid) for pid in cal_unknown]
                 + [('z', pid) for pid in del_unknown]
                 + [('b', rid) for rid in package_rods])
    index = {variable: i for i, variable in enumerate(variables)}
    size = len(variables)
    matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
    rhs = [Fraction(0) for _ in range(size)]
    constant = Fraction(0)

    def add(rows, fixed, prefix):
        nonlocal constant
        for row in rows:
            vector = [Fraction(0) for _ in range(size)]
            folded = Fraction(0)
            for pid, sign in ((row['from'], -1), (row['to'], 1)):
                if pid in fixed:
                    folded += sign * fixed[pid]
                else:
                    vector[index[(prefix, pid)]] = Fraction(sign)
            vector[index[('b', row['from_staff'])]] -= Fraction(1)
            vector[index[('b', row['to_staff'])]] += Fraction(1)
            weight = Fraction(row['weight'])
            target = Fraction(row['delta']) - folded
            for i, left in enumerate(vector):
                if not left:
                    continue
                rhs[i] += weight * left * target
                for j, right in enumerate(vector):
                    if right:
                        matrix[i][j] += weight * left * right
            constant += weight * target * target

    add(calibration['observations'], cal_fixed, 'h')
    add(survey['observations'], del_fixed, 'z')
    answers = _gauss_jordan(matrix, [rhs])
    optimum = answers[0]
    assert optimum is not None
    minimum = constant - sum(optimum[i] * rhs[i] for i in range(size))

    query_results = {}
    for row in survey['targets']:
        vector = [Fraction(0) for _ in range(size)]
        folded = Fraction(0)
        if 'point' in row:
            pid = row['point']
            if pid in del_fixed:
                folded = del_fixed[pid]
            else:
                vector[index[('z', pid)]] = Fraction(1)
        else:
            for pid, sign in ((row['from'], -1), (row['to'], 1)):
                if pid in del_fixed:
                    folded += sign * del_fixed[pid]
                else:
                    vector[index[('z', pid)]] += Fraction(sign)
        solved = _gauss_jordan(matrix, [rhs, vector])[1]
        if solved is None:
            query_results[row['id']] = None
        else:
            query_results[row['id']] = folded + sum(
                vector[i] * optimum[i] for i in range(size))
    return minimum, query_results


# ------------------------------------------------------------ calibration stage

class CalibrationTests(unittest.TestCase):
    def test_example1_profile_quadratic_hand_derived(self):
        package = calibrate(load('example1-calibration.json'))
        self.assertEqual(set(package),
                         {'schema', 'rods', 'quadratic', 'linear', 'constant'})
        self.assertEqual(package['schema'], CALIBRATION_SCHEMA)
        self.assertEqual(package['rods'], ['S', 'T', 'U'])
        self.assertEqual(package['quadratic'], [
            ['2/3', '-1/3', '-1/3'],
            ['-1/3', '2/3', '-1/3'],
            ['-1/3', '-1/3', '2/3']])
        self.assertEqual(package['linear'], ['5/3', '-1/3', '-4/3'])
        self.assertEqual(package['constant'], '14/3')
        # No calibration point, observation id or reading leaks into the
        # exchange package.
        text = json.dumps(package)
        for leaked in ('AP-SS', 'AP-ST', 'AP-SU', '"A"', '"P"', '10', '12'):
            self.assertNotIn(leaked, text)

    def test_example2_profile_quadratic_hand_derived(self):
        package = calibrate(load('example2-calibration.json'))
        self.assertEqual(package['rods'], ['S', 'T', 'U'])
        self.assertEqual(package['quadratic'], [
            ['1', '-1', '0'], ['-1', '1', '0'],
            ['0', '0', '0']])
        self.assertEqual(package['linear'], ['2', '-2', '0'])
        self.assertEqual(package['constant'], '4')

    def test_unanchored_component_is_a_valid_flat_package(self):
        package = calibrate(load('unanchored-calibration.json'))
        self.assertEqual(package['rods'], ['S', 'T', 'V'])
        zero = ['0', '0', '0']
        self.assertEqual(package['quadratic'], [zero, zero, zero])
        self.assertEqual(package['linear'], zero)
        self.assertEqual(package['constant'], '0')
        # The flat package must pass every exchange invariant.
        rods, quadratic, linear, constant = load_package(package)
        self.assertEqual(rods, ['S', 'T', 'V'])
        self.assertEqual(constant, 0)

    def test_listed_rod_never_observed_keeps_zero_row(self):
        data = {
            'points': [point('A', '0'), point('B', '0')],
            'rods': ['S', 'T'],
            'observations': [obs('o1', 'A', 'B', 1, 'S', 'S')]}
        package = calibrate(data)
        self.assertEqual(package['rods'], ['S', 'T'])
        self.assertEqual(package['quadratic'],
                         [['0', '0'], ['0', '0']])
        self.assertEqual(package['linear'], ['0', '0'])
        self.assertEqual(package['constant'], '1')
        load_package(package)

    def test_same_staff_both_ends_drops_staff_term(self):
        data = {
            'points': [point('A', '0'), point('B')],
            'rods': ['S'],
            'observations': [obs('o1', 'A', 'B', 3, 'S', 'S', '2')]}
        package = calibrate(data)
        self.assertEqual(package['quadratic'], [['0']])
        self.assertEqual(package['linear'], ['0'])
        # Single weighted residual at the fitted height B=3: 2*(3-3)^2 = 0.
        self.assertEqual(package['constant'], '0')

    def test_reordering_inputs_leaves_package_identical(self):
        data = load('example1-calibration.json')
        shuffled = {
            'points': list(reversed(data['points'])),
            'rods': list(reversed(data['rods'])),
            'observations': list(reversed(data['observations']))}
        self.assertEqual(calibrate(shuffled), calibrate(data))

    def test_profile_cost_equals_brute_force_height_minimization(self):
        # Central package contract: for each fixed staff-error vector, the
        # package cost must equal the minimum calibration residual sum over
        # unknown heights alone.  The height minimization below is solved
        # independently of staff.calibrate.
        data = load('example1-calibration.json')
        package = calibrate(data)
        rods = package['rods']
        Q = [[Fraction(v) for v in row] for row in package['quadratic']]
        g = [Fraction(v) for v in package['linear']]
        c = Fraction(package['constant'])
        fixed = {row['id']: Fraction(row['elevation'])
                 for row in data['points']
                 if row['elevation'] is not None}
        unknown = sorted(row['id'] for row in data['points']
                         if row['elevation'] is None)
        h_index = {pid: i for i, pid in enumerate(unknown)}
        r_index = {rid: i for i, rid in enumerate(rods)}

        def brute_force(staff_errors):
            normal = [[Fraction(0) for _ in unknown] for _ in unknown]
            rhs = [Fraction(0) for _ in unknown]
            constant = Fraction(0)
            for row in data['observations']:
                vector = [Fraction(0) for _ in unknown]
                folded = Fraction(0)
                for pid, sign in ((row['from'], -1), (row['to'], 1)):
                    if pid in fixed:
                        folded += sign * fixed[pid]
                    else:
                        vector[h_index[pid]] = Fraction(sign)
                staff_term = (staff_errors[r_index[row['to_staff']]]
                              - staff_errors[r_index[row['from_staff']]])
                target = (Fraction(row['delta']) - folded - staff_term)
                weight = Fraction(row['weight'])
                for i, left in enumerate(vector):
                    if not left:
                        continue
                    rhs[i] += weight * left * target
                    for j, right in enumerate(vector):
                        if right:
                            normal[i][j] += weight * left * right
                constant += weight * target * target
            answer = _gauss_jordan(normal, [rhs])[0]
            return constant - sum(answer[i] * rhs[i]
                                  for i in range(len(unknown)))

        trials = [(0, 0, 0), (1, 0, 0), (2, -3, 1), (1, 1, 1),
                  (-4, 2, 7), (1, 2, 4)]
        for values in trials:
            staff_errors = dict(zip(rods, map(Fraction, values)))
            vector = [staff_errors[rid] for rid in rods]
            package_cost = (
                sum(Q[i][j] * vector[i] * vector[j]
                    for i in range(3) for j in range(3))
                + 2 * sum(g[i] * vector[i] for i in range(3)) + c)
            self.assertEqual(package_cost, brute_force(vector), values)
            # A common offset never changes either side.
            shifted = [value + 5 for value in vector]
            shifted_cost = (
                sum(Q[i][j] * shifted[i] * shifted[j]
                    for i in range(3) for j in range(3))
                + 2 * sum(g[i] * shifted[i] for i in range(3)) + c)
            self.assertEqual(shifted_cost, package_cost)

    def test_invalid_calibration_inputs(self):
        base = load('example1-calibration.json')
        cases = []
        bad_rods = {**base, 'rods': ['S', 'S', 'T']}
        cases.append(bad_rods)
        unknown_staff = {**base, 'observations': [
            {**base['observations'][0], 'to_staff': 'Z'}]}
        cases.append(unknown_staff)
        no_benchmark = {**base, 'points': [
            point('A'), point('P')]}
        cases.append(no_benchmark)
        bad_weight = {**base, 'observations': [
            {**base['observations'][0], 'weight': '0'}]}
        cases.append(bad_weight)
        duplicate_obs = {**base,
                         'observations': base['observations']
                         + [base['observations'][0]]}
        cases.append(duplicate_obs)
        float_reading = {**base, 'observations': [
            {**base['observations'][0], 'delta': 1.5}]}
        cases.append(float_reading)
        empty_rods = {**base, 'rods': []}
        cases.append(empty_rods)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    calibrate(case)


# ----------------------------------------------------------- package validation

def valid_small_package():
    return {
        'schema': CALIBRATION_SCHEMA,
        'rods': ['A', 'B'],
        'quadratic': [['1', '-1'], ['-1', '1']],
        'linear': ['1', '-1'],
        'constant': '2'}


class PackageValidationTests(unittest.TestCase):
    def test_valid_package_loads(self):
        rods, quadratic, linear, constant = load_package(
            valid_small_package())
        self.assertEqual(rods, ['A', 'B'])
        self.assertEqual(quadratic[0][1], Fraction(-1))
        self.assertEqual(constant, 2)

    def _reject(self, package):
        with self.assertRaises(ValueError):
            load_package(package)

    def test_wrong_schema_rejected(self):
        package = valid_small_package()
        package['schema'] = 'other'
        self._reject(package)

    def test_unsorted_rods_rejected(self):
        package = valid_small_package()
        package['rods'] = ['B', 'A']
        self._reject(package)

    def test_duplicate_rods_rejected(self):
        package = valid_small_package()
        package['rods'] = ['A', 'A']
        self._reject(package)

    def test_dimension_mismatch_rejected(self):
        package = valid_small_package()
        package['quadratic'] = [['1', '0', '0'], ['0', '1', '0'],
                                ['0', '0', '1']]
        self._reject(package)
        package = valid_small_package()
        package['linear'] = ['1']
        self._reject(package)

    def test_asymmetric_quadratic_rejected(self):
        package = valid_small_package()
        package['quadratic'] = [['1', '-1'], ['0', '1']]
        self._reject(package)

    def test_negative_curvature_rejected(self):
        package = valid_small_package()
        package['quadratic'] = [['1', '2'], ['2', '1']]
        self._reject(package)

    def test_common_offset_sensitivity_rejected(self):
        package = valid_small_package()
        package['quadratic'] = [['1', '0'], ['0', '1']]
        self._reject(package)
        package = valid_small_package()
        package['linear'] = ['1', '0']
        self._reject(package)

    def test_no_finite_minimum_rejected(self):
        # Rank-one quadratic whose null space is larger than the common
        # offset direction.  The linear term is offset-orthogonal (sums to
        # zero) but still has a component in the extra null space, so no
        # stationarity point exists even though every offset invariant holds.
        package = {
            'schema': CALIBRATION_SCHEMA,
            'rods': ['A', 'B', 'C'],
            'quadratic': [['1', '-1', '0'], ['-1', '1', '0'],
                          ['0', '0', '0']],
            'linear': ['1/2', '1/2', '-1'],
            'constant': '0'}
        self._reject(package)

    def test_negative_minimum_rejected(self):
        package = {
            'schema': CALIBRATION_SCHEMA,
            'rods': ['A', 'B'],
            'quadratic': [['1', '-1'], ['-1', '1']],
            'linear': ['1', '-1'],
            'constant': '0'}
        self._reject(package)

    def test_unreduced_fraction_and_float_rejected(self):
        package = valid_small_package()
        package['constant'] = '2/4'
        self._reject(package)
        package = valid_small_package()
        package['constant'] = 2.0
        self._reject(package)
        package = valid_small_package()
        package['constant'] = '0.5'
        self._reject(package)

    def test_extra_and_missing_fields_rejected(self):
        package = valid_small_package()
        package['secret'] = 1
        self._reject(package)
        self._reject({'schema': CALIBRATION_SCHEMA})


# ----------------------------------------------------------------- delivery stage

class DeliveryTests(unittest.TestCase):
    def test_example1_handover_hand_derived(self):
        package = calibrate(load('example1-calibration.json'))
        report = deliver(load('example1-delivery.json'), package)
        self.assertEqual(report['schema'], DELIVERY_SCHEMA)
        self.assertEqual(report['minimum_cost'], '4/3')
        self.assertEqual(target(report, 'height-B'),
                         {'id': 'height-B', 'status': 'resolved',
                          'value': '49/3'})
        self.assertEqual(target(report, 'diff-AF')['status'], 'resolved')
        self.assertEqual(target(report, 'diff-AF')['value'], '0')
        self.assertEqual([row['id'] for row in report['targets']],
                         sorted(row['id'] for row in report['targets']))

    def test_example2_handover_hand_derived(self):
        package = calibrate(load('example2-calibration.json'))
        report = deliver(load('example2-delivery.json'), package)
        self.assertEqual(report['minimum_cost'], '0')
        self.assertEqual(target(report, 'height-B'),
                         {'id': 'height-B', 'status': 'resolved',
                          'value': '10'})
        self.assertEqual(target(report, 'height-C')['status'], 'unresolved')
        self.assertIsNone(target(report, 'height-C')['value'])
        self.assertEqual(target(report, 'height-D')['status'], 'unresolved')
        self.assertEqual(target(report, 'diff-CD'),
                         {'id': 'diff-CD', 'status': 'resolved', 'value': '3'})
        self.assertEqual(target(report, 'diff-DC')['value'], '-3')

    def test_difference_of_two_unresolved_heights_can_resolve(self):
        # Example 2 already proves it: C and D are unresolved while C-D is 3.
        package = calibrate(load('example2-calibration.json'))
        report = deliver(load('example2-delivery.json'), package)
        self.assertEqual(target(report, 'height-C')['status'], 'unresolved')
        self.assertEqual(target(report, 'height-D')['status'], 'unresolved')
        self.assertEqual(target(report, 'diff-CD')['status'], 'resolved')

    def test_legal_report_when_every_target_unresolved(self):
        package = calibrate(load('unanchored-calibration.json'))
        report = deliver(load('unanchored-delivery.json'), package)
        self.assertEqual(report['minimum_cost'], '0')
        self.assertEqual(report['targets'],
                         [{'id': 'height-B', 'status': 'unresolved',
                           'value': None}])

    def test_floating_delivery_component_does_not_block_anchored_query(self):
        calibration = {
            'points': [point('A', '0'), point('B', '0')],
            'rods': ['S', 'T'],
            'observations': [obs('o1', 'A', 'B', 1, 'S', 'T')]}
        package = calibrate(calibration)
        survey = {
            'points': [point('A', '0'), point('B'),
                       point('F'), point('G')],
            'observations': [
                obs('ab', 'A', 'B', 3, 'S', 'T'),
                obs('fg', 'F', 'G', 9, 'S', 'S')],
            'targets': [
                {'id': 'anchored', 'point': 'B'},
                {'id': 'floating', 'point': 'G'},
                {'id': 'floating-diff', 'from': 'F', 'to': 'G'}]}
        report = deliver(survey, package)
        self.assertEqual(target(report, 'anchored')['status'], 'resolved')
        self.assertEqual(target(report, 'floating')['status'], 'unresolved')
        # G-F = 9 regardless of the missing datum of that component.
        self.assertEqual(target(report, 'floating-diff'),
                         {'id': 'floating-diff', 'status': 'resolved',
                          'value': '9'})

    def test_all_fixed_delivery_network(self):
        calibration = {
            'points': [point('A', '0'), point('B', '0')],
            'rods': ['S'],
            'observations': [obs('o1', 'A', 'B', 2, 'S', 'S')]}
        package = calibrate(calibration)
        survey = {
            'points': [point('A', '0'), point('B', '5')],
            'observations': [obs('ab', 'A', 'B', 4, 'S', 'S')],
            'targets': [{'id': 'q', 'from': 'A', 'to': 'B'}]}
        report = deliver(survey, package)
        # Calibration contributes constant 4 (fixed-point S->S reading 2),
        # the delivery residual (5-0-4)^2 = 1 adds one: joint minimum 5.
        # The fixed benchmark difference is still published even though no
        # free delivery variable exists.
        self.assertEqual(report['minimum_cost'], '5')
        self.assertEqual(target(report, 'q'),
                         {'id': 'q', 'status': 'resolved', 'value': '5'})

    def test_observation_may_use_rod_listed_but_never_calibrated(self):
        calibration = {
            'points': [point('A', '0'), point('B', '0')],
            'rods': ['S', 'T'],
            'observations': [obs('o1', 'A', 'B', 1, 'S', 'S')]}
        package = calibrate(calibration)
        survey = {
            'points': [point('A', '0'), point('X')],
            'observations': [obs('x1', 'A', 'X', 5, 'S', 'T')],
            'targets': [{'id': 'q1', 'point': 'X'}]}
        report = deliver(survey, package)
        self.assertEqual(target(report, 'q1')['status'], 'unresolved')

    def test_unknown_staff_in_delivery_rejected(self):
        package = calibrate(load('example1-calibration.json'))
        survey = load('example1-delivery.json')
        survey['observations'][0]['to_staff'] = 'Z'
        with self.assertRaises(ValueError):
            deliver(survey, package)

    def test_fixed_point_query_is_its_benchmark(self):
        package = calibrate(load('example2-calibration.json'))
        survey = load('example2-delivery.json')
        survey['targets'] = [{'id': 'qA', 'point': 'A'}]
        report = deliver(survey, package)
        self.assertEqual(target(report, 'qA'),
                         {'id': 'qA', 'status': 'resolved', 'value': '0'})

    def test_difference_from_benchmark_matches_unknown_height(self):
        package = calibrate(load('example2-calibration.json'))
        survey = load('example2-delivery.json')
        survey['targets'] = [
            {'id': 'qAB', 'from': 'A', 'to': 'B'},
            {'id': 'qB', 'point': 'B'}]
        report = deliver(survey, package)
        self.assertEqual(target(report, 'qAB')['status'], 'resolved')
        self.assertEqual(target(report, 'qAB')['value'],
                         target(report, 'qB')['value'])

    def test_reordering_survey_leaves_report_identical(self):
        package = calibrate(load('example2-calibration.json'))
        survey = load('example2-delivery.json')
        shuffled = {
            'points': list(reversed(survey['points'])),
            'observations': list(reversed(survey['observations'])),
            'targets': list(reversed(survey['targets']))}
        self.assertEqual(deliver(shuffled, package), deliver(survey, package))

    def test_invalid_targets_rejected(self):
        package = calibrate(load('example2-calibration.json'))
        base = load('example2-delivery.json')
        unknown_point = {**base, 'targets': [{'id': 'q', 'point': 'Z'}]}
        with self.assertRaises(ValueError):
            deliver(unknown_point, package)
        same_ends = {**base, 'targets': [{'id': 'q', 'from': 'A', 'to': 'A'}]}
        with self.assertRaises(ValueError):
            deliver(same_ends, package)
        duplicate_id = {**base, 'targets': base['targets']
                        + [{'id': 'height-B', 'point': 'A'}]}
        with self.assertRaises(ValueError):
            deliver(duplicate_id, package)
        empty = {**base, 'targets': []}
        with self.assertRaises(ValueError):
            deliver(empty, package)

    def test_randomized_joint_oracle_cross_check(self):
        rng = random.Random(20261120)
        for trial in range(25):
            calibration, survey = _random_case(rng)
            package = calibrate(calibration)
            report = deliver(survey, package)
            expected_minimum, expected_queries = _oracle(
                calibration, survey, package['rods'])
            self.assertEqual(Fraction(report['minimum_cost']),
                             expected_minimum, trial)
            for row in report['targets']:
                expected = expected_queries[row['id']]
                if expected is None:
                    self.assertEqual(row['status'], 'unresolved', trial)
                    self.assertIsNone(row['value'])
                else:
                    self.assertEqual(row['status'], 'resolved', trial)
                    self.assertEqual(Fraction(row['value']), expected, trial)


def _random_case(rng):
    rod_names = ['R' + str(i) for i in range(rng.randint(2, 4))]
    cal_points = [point('C' + str(i),
                        str(rng.randint(0, 3)) if i == 0 else None)
                  for i in range(rng.randint(2, 4))]
    cal_observations = []
    for i in range(rng.randint(2, 6)):
        start, end = rng.sample([row['id'] for row in cal_points], 2)
        cal_observations.append(obs(
            'co' + str(i), start, end, rng.randint(0, 9),
            rng.choice(rod_names), rng.choice(rod_names)))
    calibration = {'points': cal_points, 'rods': rod_names,
                   'observations': cal_observations}

    survey_points = [point('D' + str(i),
                           str(rng.randint(0, 3)) if i == 0 else None)
                     for i in range(rng.randint(2, 5))]
    survey_observations = []
    for i in range(rng.randint(2, 6)):
        start, end = rng.sample([row['id'] for row in survey_points], 2)
        survey_observations.append(obs(
            'do' + str(i), start, end, rng.randint(0, 9),
            rng.choice(rod_names), rng.choice(rod_names),
            str(rng.randint(1, 3))))
    names = [row['id'] for row in survey_points]
    targets = []
    for i in range(rng.randint(1, 3)):
        if rng.random() < 0.5:
            targets.append({'id': 't' + str(i),
                            'point': rng.choice(names)})
        else:
            start, end = rng.sample(names, 2)
            targets.append({'id': 't' + str(i), 'from': start, 'to': end})
    survey = {'points': survey_points,
              'observations': survey_observations, 'targets': targets}
    return calibration, survey


# ------------------------------------------------------------------------- CLI

class CliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', MODULE, *map(str, arguments)],
            capture_output=True, text=True, timeout=60, env=ENV)

    def test_full_handover_for_both_examples(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            for name, expected in (
                    ('example1', ('49/3', '4/3')),
                    ('example2', ('10', '0'))):
                package = t / (name + '-package.json')
                report = t / (name + '-report.json')
                result = self.run_cli(
                    'calibrate', '--source',
                    FIXTURES / (name + '-calibration.json'),
                    '--output', package)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.run_cli(
                    'deliver', '--source',
                    FIXTURES / (name + '-delivery.json'),
                    '--calibration', package, '--output', report)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(report.read_text())
                self.assertEqual(payload['minimum_cost'], expected[1])
                heights = [row for row in payload['targets']
                           if row['id'] == 'height-B']
                if heights:
                    self.assertEqual(heights[0]['value'], expected[0])
                # Repeated runs publish byte-identical artifacts.
                first_package = package.read_bytes()
                first_report = report.read_bytes()
                self.assertEqual(self.run_cli(
                    'calibrate', '--source',
                    FIXTURES / (name + '-calibration.json'),
                    '--output', package).returncode, 0)
                self.assertEqual(self.run_cli(
                    'deliver', '--source',
                    FIXTURES / (name + '-delivery.json'),
                    '--calibration', package,
                    '--output', report).returncode, 0)
                self.assertEqual(package.read_bytes(), first_package)
                self.assertEqual(report.read_bytes(), first_report)

    def test_delivery_never_opens_calibration_source(self):
        # Deleting the calibration input after packaging must not affect the
        # delivery run, which only receives the package.
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            source = t / 'calibration.json'
            package = t / 'package.json'
            source.write_text((FIXTURES / 'example1-calibration.json')
                              .read_text())
            result = self.run_cli('calibrate', '--source', source,
                                  '--output', package)
            self.assertEqual(result.returncode, 0, result.stderr)
            source.unlink()
            report = t / 'report.json'
            result = self.run_cli(
                'deliver', '--source',
                FIXTURES / 'example1-delivery.json',
                '--calibration', package, '--output', report)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text())['minimum_cost'],
                             '4/3')

    def test_failures_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            output = t / 'report.json'
            output.write_text('old')
            package = t / 'package.json'
            good_package = calibrate(load('example1-calibration.json'))
            package.write_text(json.dumps(good_package))
            bad_source = t / 'bad.json'
            cases = [
                ('calibrate', '--source', bad_source, '--output', output),
            ]
            survey = t / 'survey.json'
            survey.write_text((FIXTURES / 'example1-delivery.json')
                              .read_text())
            raw_cases = ['{', '{"a":1,"a":2}', '{"a":NaN}', '{}']
            for raw in raw_cases:
                bad_source.write_text(raw)
                for arguments in cases:
                    result = self.run_cli(*arguments)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_text(), 'old')
            # Delivery survey JSON must obey the same bounded-JSON rules.
            for raw in raw_cases:
                survey.write_text(raw)
                result = self.run_cli(
                    'deliver', '--source', survey,
                    '--calibration', package, '--output', output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'old')
            survey.write_text((FIXTURES / 'example1-delivery.json')
                              .read_text())
            # Bad packages must likewise leave the report untouched.
            bad_packages = []
            broken = dict(good_package)
            broken['quadratic'] = [['1', '0', '0'], ['0', '1', '0'],
                                   ['0', '0', '1']]
            bad_packages.append(json.dumps(broken))
            bad_packages.append('{')
            bad_packages.append(json.dumps({**good_package, 'schema': 'x'}))
            for raw in bad_packages:
                package.write_text(raw)
                result = self.run_cli(
                    'deliver', '--source', survey,
                    '--calibration', package, '--output', output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'old')

    def test_path_collisions_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            source = t / 'source.json'
            package = t / 'package.json'
            output = t / 'output.json'
            source.write_text((FIXTURES / 'example1-calibration.json')
                              .read_text())
            package.write_text(json.dumps(
                calibrate(load('example1-calibration.json'))))
            survey = t / 'survey.json'
            survey.write_text((FIXTURES / 'example1-delivery.json')
                              .read_text())
            self.assertEqual(self.run_cli(
                'calibrate', '--source', source,
                '--output', source).returncode, 2)
            self.assertEqual(self.run_cli(
                'deliver', '--source', survey, '--calibration', package,
                '--output', package).returncode, 2)
            self.assertEqual(self.run_cli(
                'deliver', '--source', survey, '--calibration', survey,
                '--output', output).returncode, 2)
            self.assertEqual(self.run_cli(
                'deliver', '--source', survey, '--calibration', package,
                '--output', survey).returncode, 2)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            target = t / 'target'
            target.write_text('old')
            link = t / 'link'
            link.symlink_to(target)
            result = self.run_cli(
                'calibrate', '--source',
                FIXTURES / 'example1-calibration.json', '--output', link)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), 'old')

    def test_missing_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            self.assertEqual(self.run_cli(
                'calibrate', '--source', t / 'missing',
                '--output', t / 'out.json').returncode, 2)

    def test_existing_commands_still_work(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            output = t / 'network.json'
            result = subprocess.run(
                [sys.executable, '-m', 'level_network',
                 '--source', str(ROOT / 'fixtures' / 'input.json'),
                 '--output', str(output)],
                capture_output=True, text=True, timeout=30, env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['elevations'],
                             {'A': '100', 'B': '304/3', 'C': '308/3'})
            plan_output = t / 'plan.json'
            result = subprocess.run(
                [sys.executable, '-m', 'level_network.plan_cli',
                 '--source', str(ROOT / 'fixtures' / 'plan.json'),
                 '--output', str(plan_output)],
                capture_output=True, text=True, timeout=30, env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(plan_output.read_text())
                             ['total_cost'], '5')


if __name__ == '__main__':
    unittest.main()
