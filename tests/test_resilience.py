"""Tests for ``level-resilience`` with hand-derived and independent values.

Every expected displacement below is derived by hand from a small network.
The random cross-check compares the rank-one leave-one-out solver against an
independent run of the ordinary ``adjust`` on each reduced observation set.
"""
from fractions import Fraction
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from level_network.adjustment import adjust
from level_network.resilience import NOTE, review

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'level_network.resilience_cli'


def point(pid, elevation=None):
    return {'id': pid, 'elevation': elevation}


def obs(oid, start, end, delta='1', weight='1'):
    return {'id': oid, 'from': start, 'to': end, 'delta': delta,
            'weight': weight}


def request(points, observations, targets, tolerances):
    return {'points': list(points), 'observations': list(observations),
            'target_points': list(targets),
            'tolerances': dict(tolerances)}


def triangle(tolerances=None):
    return request(
        [point('A', '100'), point('B'), point('C')],
        [obs('AB', 'A', 'B', '1'), obs('BC', 'B', 'C', '1'),
         obs('AC', 'A', 'C', '3')],
        ['B', 'C'], tolerances or {'B': '1', 'C': '1'})


def scenario(report, oid):
    return next(row for row in report['scenarios']
                if row['observation'] == oid)


def target_row(report, pid):
    return next(row for row in report['targets'] if row['point'] == pid)


def target_in_scenario(report, oid, pid):
    return next(row for row in scenario(report, oid)['targets']
                if row['point'] == pid)


class HandDerivedTests(unittest.TestCase):
    def test_baseline_triangle_values(self):
        report = review(triangle())
        self.assertEqual(report['schema'], 'level-resilience-report-v1')
        self.assertEqual(report['official_elevations'],
                         {'B': '304/3', 'C': '308/3'})
        # Removing any one side leaves a tree whose heights follow directly:
        # AB removed -> B=102, C=103; BC removed -> B=101, C=103;
        # AC removed -> B=101, C=102.
        expected = {
            'AB': {'B': ('102', '2/3'), 'C': ('103', '1/3')},
            'BC': {'B': ('101', '1/3'), 'C': ('103', '1/3')},
            'AC': {'B': ('101', '1/3'), 'C': ('102', '2/3')},
        }
        for oid, rows in expected.items():
            with self.subTest(observation=oid):
                self.assertEqual(scenario(report, oid)['status'], 'available')
                self.assertIsNone(scenario(report, oid)['reason'])
                for pid, (height, displacement) in rows.items():
                    row = target_in_scenario(report, oid, pid)
                    self.assertEqual(row['status'], 'available')
                    self.assertEqual(row['elevation'], height)
                    self.assertEqual(row['displacement'], displacement)
                    self.assertTrue(row['within_tolerance'])
        self.assertEqual([row['observation'] for row in report['scenarios']],
                         ['AB', 'AC', 'BC'])

    def test_report_sorts_observations_and_targets(self):
        data = triangle({'C': '1', 'B': '1'})
        data['observations'].reverse()
        data['points'].reverse()
        data['target_points'] = ['C', 'B']
        report = review(data)
        self.assertEqual(report['official_elevations'],
                         {'B': '304/3', 'C': '308/3'})
        self.assertEqual([row['observation'] for row in report['scenarios']],
                         ['AB', 'AC', 'BC'])
        self.assertEqual([row['point'] for row in report['targets']],
                         ['B', 'C'])
        for row in report['scenarios']:
            self.assertEqual([item['point'] for item in row['targets']],
                             ['B', 'C'])

    def test_scenario_removes_exactly_one_observation(self):
        data = triangle()
        report = review(data)
        # Every scenario reports the same number of rows, and the report
        # states the non-accumulating rule.
        self.assertEqual(len(report['scenarios']), len(data['observations']))
        self.assertEqual(len({row['observation'] for row in report['scenarios']}),
                         len(data['observations']))
        self.assertIn('exactly one observation', report['note'])

    def test_exact_tolerance_is_acceptable(self):
        # Two parallel observations of B give official B=2; either failure
        # leaves B at 1 or 3, a displacement of exactly 1.  A tolerance of
        # exactly 1 is acceptable, while a marginally smaller one is not.
        data = request([point('A', '0'), point('B')],
                       [obs('AB1', 'A', 'B', '1'),
                        obs('AB2', 'A', 'B', '3')], ['B'], {'B': '1'})
        report = review(data)
        self.assertEqual(target_in_scenario(report, 'AB1', 'B')
                         ['displacement'], '1')
        self.assertTrue(target_in_scenario(report, 'AB1', 'B')
                        ['within_tolerance'])
        self.assertEqual(target_row(report, 'B')['conclusion'], 'resilient')
        tighter = request([point('A', '0'), point('B')],
                          [obs('AB1', 'A', 'B', '1'),
                           obs('AB2', 'A', 'B', '3')], ['B'],
                          {'B': '0.999999'})
        self.assertEqual(target_row(review(tighter), 'B')['conclusion'],
                         'movement_at_risk')

    def test_zero_displacement_target_stays_exact(self):
        # B's removal scenarios never move C in fixtures/resilience.json.
        data = json.loads((ROOT / 'fixtures/resilience.json').read_text())
        report = review(data)
        for oid in ('AB1', 'AB2'):
            row = target_in_scenario(report, oid, 'C')
            self.assertEqual(row['displacement'], '0')
            self.assertTrue(row['within_tolerance'])
        for oid in ('AC1', 'AC2'):
            row = target_in_scenario(report, oid, 'B')
            self.assertEqual(row['displacement'], '0')
            self.assertTrue(row['within_tolerance'])

    def test_multiple_targets_with_different_tolerances(self):
        data = triangle({'B': '0.5', 'C': '1'})
        report = review(data)
        self.assertEqual(target_row(report, 'B')['conclusion'],
                         'movement_at_risk')
        self.assertEqual(target_row(report, 'C')['conclusion'], 'resilient')
        self.assertFalse(target_in_scenario(report, 'AB', 'B')
                         ['within_tolerance'])
        self.assertTrue(target_in_scenario(report, 'AB', 'C')
                        ['within_tolerance'])

    def test_bridge_removal_is_unavailable_and_takes_precedence(self):
        data = json.loads(
            (ROOT / 'fixtures/resilience-bridge.json').read_text())
        report = review(data)
        bridge = scenario(report, 'AB')
        self.assertEqual(bridge['status'], 'unavailable')
        self.assertEqual(bridge['reason'], 'unidentifiable_datum')
        for row in bridge['targets']:
            self.assertEqual(row, {'point': row['point'],
                                   'status': 'unavailable',
                                   'elevation': None,
                                   'displacement': None,
                                   'within_tolerance': None})
        # Other scenarios do move C beyond its tolerance, yet the delivered
        # conclusion must be unavailability.
        self.assertFalse(target_in_scenario(report, 'BC', 'C')
                         ['within_tolerance'])
        self.assertEqual(target_row(report, 'C')['conclusion'],
                         'delivery_at_risk')
        self.assertEqual(target_row(report, 'C')['tolerance'], '1/10')
        self.assertEqual(target_row(report, 'E')['conclusion'],
                         'delivery_at_risk')

    def test_unavailable_marks_even_anchored_targets(self):
        # Two separately anchored components: A-B and D-C.  Removing AB
        # leaves B floating (contract violation), so C -- still anchored
        # through D -- must also be marked unavailable, with no fabricated
        # height even though its own component stays solvable.
        data = request(
            [point('A', '0'), point('B'), point('C'), point('D', '5')],
            [obs('AB', 'A', 'B', '1'), obs('CD', 'D', 'C', '1')],
            ['B', 'C'], {'B': '1', 'C': '100'})
        report = review(data)
        self.assertEqual(scenario(report, 'AB')['status'], 'unavailable')
        for row in scenario(report, 'AB')['targets']:
            self.assertEqual(row['status'], 'unavailable')
            self.assertIsNone(row['elevation'])
        self.assertEqual(target_row(report, 'C')['conclusion'],
                         'delivery_at_risk')
        self.assertEqual(target_row(report, 'B')['conclusion'],
                         'delivery_at_risk')

    def test_non_target_component_losing_datum_is_unavailable(self):
        # E is an unknown non-target that reaches the benchmark only through
        # DE.  Removing DE violates the adjustment contract for E's
        # component, so the scenario is unavailable for target B too, even
        # though B's own component is still anchored through AB2.
        data = request(
            [point('A', '0'), point('B'), point('D'), point('E')],
            [obs('AB1', 'A', 'B', '1'), obs('AB2', 'A', 'B', '1'),
             obs('BD', 'B', 'D', '1'), obs('DE', 'D', 'E', '1')],
            ['B'], {'B': '1'})
        report = review(data)
        self.assertEqual(scenario(report, 'DE')['status'], 'unavailable')
        self.assertEqual(scenario(report, 'DE')['reason'],
                         'unidentifiable_datum')
        self.assertEqual(target_in_scenario(report, 'DE', 'B'),
                         {'point': 'B', 'status': 'unavailable',
                          'elevation': None, 'displacement': None,
                          'within_tolerance': None})
        for oid in ('AB1', 'AB2'):
            self.assertEqual(scenario(report, oid)['status'], 'available')
        self.assertEqual(target_row(report, 'B')['conclusion'],
                         'delivery_at_risk')

    def test_single_observation_network_unavailable_by_definition(self):
        data = request([point('A', '0'), point('B')],
                       [obs('AB', 'A', 'B', '1')], ['B'], {'B': '1'})
        report = review(data)
        self.assertEqual(scenario(report, 'AB')['status'], 'unavailable')
        self.assertEqual(scenario(report, 'AB')['reason'], 'no_observations')
        self.assertEqual(target_row(report, 'B')['conclusion'],
                         'delivery_at_risk')

    def test_fixture_matches_contract(self):
        data = json.loads((ROOT / 'fixtures/resilience.json').read_text())
        report = review(data)
        self.assertEqual(report['official_elevations'],
                         {'B': '2', 'C': '2'})
        self.assertEqual([row['observation'] for row in report['scenarios']],
                         ['AB1', 'AB2', 'AC1', 'AC2'])
        for oid in ('AB1', 'AB2', 'AC1', 'AC2'):
            self.assertEqual(scenario(report, oid)['status'], 'available')
        self.assertEqual(target_row(report, 'B')['conclusion'], 'resilient')
        self.assertEqual(target_row(report, 'C')['conclusion'], 'resilient')
        self.assertTrue(all(row['within_tolerance']
                            for row in scenario(report, 'AB1')['targets']))
        for value in walked(report):
            self.assertNotIsInstance(value, float)


def walked(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walked(item)
    elif isinstance(value, list):
        for item in value:
            yield from walked(item)
    else:
        yield value


class IndependentValueTests(unittest.TestCase):
    def direct(self, data):
        official = adjust({'points': data['points'],
                           'observations': data['observations']})
        heights = {pid: Fraction(value)
                   for pid, value in official['elevations'].items()}
        expected = {}
        for row in data['observations']:
            remaining = [item for item in data['observations']
                         if item['id'] != row['id']]
            try:
                reduced = adjust({'points': data['points'],
                                  'observations': remaining})
            except ValueError:
                expected[row['id']] = None
            else:
                expected[row['id']] = {
                    pid: Fraction(value)
                    for pid, value in reduced['elevations'].items()}
        return heights, expected

    def test_random_small_networks_match_re_adjustment(self):
        rng = random.Random(20261120)
        for trial in range(300):
            names = ['A'] + ['P%d' % i for i in range(1, rng.randint(2, 7))]
            fixed = {'A'}
            if len(names) > 3 and rng.random() < 0.3:
                fixed.add(names[-1])
            points = [point(name, '0' if name in fixed else None)
                      for name in names]
            observations = []
            for position in range(rng.randint(1, 10)):
                left, right = rng.sample(names, 2)
                observations.append(obs(
                    'O%02d_%d' % (position, rng.randrange(100)),
                    left, right, str(rng.randrange(-5, 6)),
                    rng.choice(['1', '2', '3', '0.5'])))
            # Duplicate ids would violate the contract; keep them unique.
            ids = [row['id'] for row in observations]
            if len(set(ids)) != len(ids):
                continue
            unknowns = [name for name in names if name not in fixed]
            targets = rng.sample(unknowns, rng.randint(1, min(3, len(unknowns))))
            data = request(points, observations, targets,
                           {pid: '1' for pid in targets})
            try:
                heights, expected = self.direct(data)
                report = review(data)
            except ValueError:
                with self.assertRaises(ValueError):
                    review(data)
                continue
            with self.subTest(trial=trial):
                for scenario_row in report['scenarios']:
                    reduced = expected[scenario_row['observation']]
                    if reduced is None:
                        self.assertEqual(scenario_row['status'],
                                         'unavailable')
                        continue
                    self.assertEqual(scenario_row['status'], 'available')
                    for row in scenario_row['targets']:
                        value = reduced[row['point']]
                        self.assertEqual(Fraction(row['elevation']), value)
                        self.assertEqual(
                            Fraction(row['displacement']),
                            abs(value - heights[row['point']]))


class ValidationTests(unittest.TestCase):
    def check(self, data, message):
        with self.assertRaisesRegex(ValueError, message):
            review(data)

    def test_duplicate_point_and_observation(self):
        data = triangle()
        data['points'].append(point('B'))
        self.check(data, 'duplicate point')
        data = triangle()
        data['observations'].append(obs('AB', 'A', 'B'))
        self.check(data, 'duplicate observation|identity')

    def test_unknown_and_fixed_targets(self):
        data = triangle()
        data['target_points'] = ['Z']
        data['tolerances'] = {'Z': '1'}
        self.check(data, 'unknown target')
        data = triangle()
        data['target_points'] = ['A']
        data['tolerances'] = {'A': '1'}
        self.check(data, 'fixed point')

    def test_target_list_rules(self):
        data = triangle()
        data['target_points'] = ['B', 'B']
        data['tolerances'] = {'B': '1'}
        self.check(data, 'duplicate target')
        data = triangle()
        data['target_points'] = []
        data['tolerances'] = {}
        self.check(data, 'target')
        points = [point('A', '0')] + [point('P%d' % i) for i in range(11)]
        observations = [obs('S%d' % i, 'A', 'P%d' % i, '1')
                        for i in range(11)]
        data = request(points, observations,
                       ['P%d' % i for i in range(11)],
                       {'P%d' % i: '1' for i in range(11)})
        self.check(data, 'bounded array')

    def test_ten_targets_accepted(self):
        points = [point('A', '0')] + [point('P%d' % i) for i in range(10)]
        observations = [obs('S%02d' % i, 'A', 'P%d' % i, '1')
                        for i in range(10)]
        targets = ['P%d' % i for i in range(10)]
        report = review(request(points, observations, targets,
                                {pid: '0' for pid in targets}))
        self.assertEqual(len(report['targets']), 10)
        self.assertTrue(all(row['conclusion'] == 'delivery_at_risk'
                            for row in report['targets']))

    def test_tolerances_must_exactly_cover_targets(self):
        data = triangle({'B': '1'})
        self.check(data, 'cover exactly')
        data = triangle({'B': '1', 'C': '1', 'Z': '1'})
        self.check(data, 'cover exactly')
        data = triangle({'B': '1', 'C': '-1'})
        self.check(data, 'non-negative')
        data = triangle({'B': '1', 'C': '1e3'})
        self.check(data, 'decimal')
        data = triangle({'B': '1', 'C': 1})
        self.check(data, 'decimal')
        data = triangle()
        data['tolerances'] = ['1']
        self.check(data, 'tolerances object')

    def test_top_level_fields(self):
        data = triangle()
        data['extra'] = 1
        self.check(data, 'expected fields')
        data = triangle()
        del data['tolerances']
        self.check(data, 'expected fields')

    def test_invalid_official_adjustment_fails(self):
        data = triangle()
        data['points'].append(point('D'))
        self.check(data, 'datum')
        data = triangle()
        data['observations'] = []
        self.check(data, 'observations')
        data = triangle()
        data['points'][0]['elevation'] = None
        self.check(data, 'benchmark')

    def test_observation_contract_reused(self):
        data = triangle()
        data['observations'][0]['weight'] = '0'
        self.check(data, 'positive')
        data = triangle()
        data['observations'][0]['delta'] = '1e3'
        self.check(data, 'decimal')
        data = triangle()
        data['observations'][0]['to'] = 'Z'
        self.check(data, 'endpoints')


class StabilityTests(unittest.TestCase):
    def test_observation_order_does_not_change_report(self):
        reference = review(triangle())
        shuffled = triangle()
        shuffled['observations'].reverse()
        self.assertEqual(review(shuffled), reference)

    def test_repeat_review_is_identical(self):
        data = triangle()
        self.assertEqual(review(data), review(data))
        self.assertIn(NOTE, json.dumps(review(data)))

    def test_report_contains_no_floats(self):
        for value in walked(review(triangle())):
            self.assertNotIsInstance(value, float)


class CliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', MODULE, '--source', str(source),
             '--output', str(output)],
            capture_output=True, text=True, timeout=120)

    def write(self, path, value):
        path.write_text(json.dumps(value))
        return path

    def test_fixture_success_and_repeat(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            result = self.invoke(ROOT / 'fixtures/resilience.json', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = output.read_bytes()
            report = json.loads(first)
            self.assertEqual(report['official_elevations'],
                             {'B': '2', 'C': '2'})
            self.assertEqual(self.invoke(
                ROOT / 'fixtures/resilience.json', output).returncode, 0)
            self.assertEqual(first, output.read_bytes())

    def test_bridge_fixture_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            result = self.invoke(ROOT / 'fixtures/resilience-bridge.json',
                                 output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertIn(
                {'observation': 'AB', 'status': 'unavailable',
                 'reason': 'unidentifiable_datum', 'targets': [
                     {'point': 'C', 'status': 'unavailable',
                      'elevation': None, 'displacement': None,
                      'within_tolerance': None},
                     {'point': 'E', 'status': 'unavailable',
                      'elevation': None, 'displacement': None,
                      'within_tolerance': None}]},
                report['scenarios'])

    def test_invalid_input_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            output.write_text('old')
            source = Path(t) / 'bad.json'
            base = triangle()
            cases = ['{', json.dumps({**base, 'target_points': ['Z']}),
                     json.dumps({**base, 'tolerances': {'B': '1'}}),
                     json.dumps({**base, 'tolerances': {'B': '-2',
                                                        'C': '1'}}),
                     json.dumps(base)[:-1] + ', "target_points": ["B"]}']
            floating = triangle()
            floating['points'].append(point('D'))
            cases.append(json.dumps(floating))
            for raw in cases:
                source.write_text(raw)
                result = self.invoke(source, output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'old')

    def test_output_collision_and_symlink(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t) / 'source.json'
            source.write_bytes(
                (ROOT / 'fixtures/resilience.json').read_bytes())
            before = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(before, source.read_bytes())
            target = Path(t) / 'target'
            target.write_text('old')
            link = Path(t) / 'link'
            link.symlink_to(target)
            self.assertEqual(self.invoke(source, link).returncode, 2)
            self.assertEqual(target.read_text(), 'old')

    def test_level_network_output_unchanged(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            result = subprocess.run(
                [sys.executable, '-m', 'level_network',
                 '--source', str(ROOT / 'fixtures/input.json'),
                 '--output', str(output)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['elevations'],
                             {'A': '100', 'B': '304/3', 'C': '308/3'})
            self.assertEqual(report['weighted_squared_error'], '1/3')
            self.assertEqual(report['degrees_of_freedom'], 1)
            self.assertEqual(sorted(report),
                             ['degrees_of_freedom', 'elevations',
                              'normal_residual', 'residuals',
                              'weighted_squared_error'])

    def test_resilience_command_does_not_apply_to_network_output(self):
        # The review never rewrites or extends the ordinary report shape.
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            self.assertEqual(self.invoke(
                ROOT / 'fixtures/resilience.json', output).returncode, 0)
            report = json.loads(output.read_text())
            self.assertNotIn('residuals', report)
            self.assertNotIn('weighted_squared_error', report)
            self.assertEqual(sorted(report),
                             ['note', 'official_elevations', 'scenarios',
                              'schema', 'targets'])


if __name__ == '__main__':
    unittest.main()
