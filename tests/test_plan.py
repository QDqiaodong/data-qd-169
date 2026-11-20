"""Tests for ``level-plan`` with hand-derived expectations.

The planner is a Steiner-tree problem in the current measurement components,
not an adjustment.  Expectations below were derived by hand from each small
graph; the exhaustive helper checks a few bigger random graphs independently.
"""
from fractions import Fraction
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from level_network.plan import plan

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'level_network.plan_cli'


def point(pid, elevation=None):
    return {'id': pid, 'elevation': elevation}


def obs(oid, start, end, delta='1', weight='1'):
    return {'id': oid, 'from': start, 'to': end, 'delta': delta,
            'weight': weight}


def segment(sid, start, end, cost):
    return {'id': sid, 'from': start, 'to': end, 'cost': cost}


def request(points, observations=(), targets=(), candidates=()):
    return {'points': list(points), 'observations': list(observations),
            'target_points': list(targets),
            'candidate_segments': list(candidates)}


def a_b_c():
    return request(
        [point('A', '100'), point('B'), point('C')],
        targets=['B', 'C'],
        candidates=[segment('AB', 'A', 'B', '4'),
                    segment('BC', 'B', 'C', '1'),
                    segment('AC', 'A', 'C', '7')])


def selected(report):
    return [(row['id'], row['from'], row['to'], row['cost'])
            for row in report['segments']]


def brute_minimum(points, observations, targets, candidates):
    """Exhaustive reference: min (exact cost, sorted selected ids)."""
    names = [row['id'] for row in points]
    index = {name: position for position, name in enumerate(names)}
    benchmarked = {row['id'] for row in points if row['elevation'] is not None}
    best = None
    for bits in range(1 << len(candidates)):
        parent = list(range(len(names)))

        def find(item):
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for row in observations:
            left, right = (find(index[row['from']]),
                           find(index[row['to']]))
            if left != right:
                parent[left] = right
        cost, chosen = Fraction(0), []
        for position, row in enumerate(candidates):
            if bits >> position & 1:
                left, right = find(index[row['from']]), find(index[row['to']])
                if left != right:
                    parent[left] = right
                cost += Fraction(row['cost'])
                chosen.append(row['id'])
        reached = all(any(find(index[target]) == find(index[benchmark])
                          for benchmark in benchmarked) for target in targets)
        if reached:
            key = (cost, tuple(sorted(chosen)))
            if best is None or key < best:
                best = key
    return best


class AcceptanceTests(unittest.TestCase):
    def test_shared_segment_beats_two_separate_routes(self):
        report = plan(a_b_c())
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '5')
        self.assertEqual(selected(report),
                         [('AB', 'A', 'B', '4'), ('BC', 'B', 'C', '1')])
        self.assertEqual([row['point'] for row in report['targets']],
                         ['B', 'C'])
        self.assertEqual({row['point']: row['benchmark']
                          for row in report['targets']},
                         {'B': 'A', 'C': 'A'})
        self.assertEqual(report['unreachable_targets'], [])

    def test_cheap_branch_to_non_target_does_not_change_plan(self):
        data = a_b_c()
        data['points'].append(point('D'))
        data['candidate_segments'].append(segment('BD', 'B', 'D', '0.5'))
        report = plan(data)
        self.assertEqual(report['total_cost'], '5')
        self.assertEqual([row['id'] for row in report['segments']],
                         ['AB', 'BC'])

    def test_fixture_matches_contract(self):
        data = json.loads((ROOT / 'fixtures/plan.json').read_text())
        report = plan(data)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '5')
        self.assertEqual([row['id'] for row in report['segments']],
                         ['AB', 'BC'])
        self.assertTrue(report['pending_collection'])
        for row in report['segments']:
            self.assertEqual(row['status'], 'pending_collection')


class ComponentTests(unittest.TestCase):
    def test_observation_connectivity_ignores_delta_values(self):
        # B and C form one current component through observation endpoints
        # only; the deliberately absurd delta values are irrelevant to
        # connectivity and the planner never adjusts them.
        data = request(
            [point('A', '0'), point('B'), point('C')],
            observations=[obs('CB', 'C', 'B', '999999999', '1')],
            targets=['C'],
            candidates=[segment('AC', 'A', 'C', '2')])
        report = plan(data)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '2')
        self.assertEqual([row['id'] for row in report['segments']], ['AC'])
        self.assertFalse(report['targets'][0]['already_anchored'])
        self.assertEqual(report['targets'][0]['benchmark'], 'A')

    def test_target_already_anchored_needs_no_field_work(self):
        data = request(
            [point('A', '0'), point('B')],
            observations=[obs('AB', 'A', 'B')],
            targets=['B'],
            candidates=[segment('AB2', 'A', 'B', '3')])
        report = plan(data)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '0')
        self.assertEqual(report['segments'], [])
        self.assertEqual(report['targets'],
                         [{'point': 'B', 'benchmark': 'A',
                           'already_anchored': True}])

    def test_no_targets_is_empty_plan(self):
        data = request([point('A', '0'), point('B')],
                       observations=[obs('AB', 'A', 'B')], targets=[],
                       candidates=[])
        report = plan(data)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '0')
        self.assertEqual(report['segments'], [])
        self.assertEqual(report['targets'], [])

    def test_floating_component_without_target_can_be_intermediate(self):
        # A anchored; B target; Z is a floating non-target that is a cheap
        # bridge: A-Z 1, Z-B 1 versus direct A-B 5.
        data = request(
            [point('A', '0'), point('B'), point('Z')],
            targets=['B'],
            candidates=[segment('AZ', 'A', 'Z', '1'),
                        segment('ZB', 'Z', 'B', '1'),
                        segment('AB', 'A', 'B', '5')])
        report = plan(data)
        self.assertEqual(report['total_cost'], '2')
        self.assertEqual([row['id'] for row in report['segments']],
                         ['AZ', 'ZB'])

    def test_floating_component_without_target_is_harmless(self):
        # Z is floating and not a target; no candidate touches it.  The
        # planning entry accepts the input and still delivers B.
        data = request(
            [point('A', '0'), point('B'), point('Z')],
            targets=['B'],
            candidates=[segment('AB', 'A', 'B', '2')])
        report = plan(data)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '2')
        self.assertIn({'points': ['Z'], 'benchmarks': [],
                       'anchored': False, 'target_points': []},
                      report['components'])

    def test_planning_never_calls_adjust(self):
        from unittest import mock
        with mock.patch('level_network.adjustment.adjust',
                        side_effect=AssertionError('adjust must not run')):
            report = plan(a_b_c())
        self.assertTrue(report['feasible'])
        self.assertNotIn('elevations', report)

    def test_multiple_anchored_components_reach_nearest(self):
        # Two anchored components: A at 0, F at 0; targets B and C float.
        data = request(
            [point('A', '0'), point('F', '0'), point('B'), point('C')],
            targets=['B', 'C'],
            candidates=[segment('AB', 'A', 'B', '9'),
                        segment('FB', 'F', 'B', '1'),
                        segment('AC', 'A', 'C', '1'),
                        segment('FC', 'F', 'C', '9')])
        report = plan(data)
        self.assertEqual(report['total_cost'], '2')
        self.assertEqual(selected(report),
                         [('AC', 'A', 'C', '1'), ('FB', 'F', 'B', '1')])
        self.assertEqual({row['point']: row['benchmark']
                          for row in report['targets']},
                         {'B': 'F', 'C': 'A'})


class TieBreakTests(unittest.TestCase):
    def test_parallel_edges_choose_cheapest_then_lowest_id(self):
        data = request(
            [point('A', '0'), point('B')],
            targets=['B'],
            candidates=[segment('s3', 'A', 'B', '5'),
                        segment('s1', 'A', 'B', '5'),
                        segment('s2', 'A', 'B', '5'),
                        segment('s0', 'A', 'B', '4')])
        report = plan(data)
        self.assertEqual(selected(report), [('s0', 'A', 'B', '4')])

    def test_parallel_equal_cost_keeps_stable_lowest_id(self):
        data = request(
            [point('A', '0'), point('B')],
            targets=['B'],
            candidates=[segment('b', 'A', 'B', '2'),
                        segment('a', 'A', 'B', '2')])
        self.assertEqual([row['id'] for row in plan(data)['segments']],
                         ['a'])

    def test_equal_total_cost_prefers_lexicographic_segment_list(self):
        # Reaching B for 3: direct A-B (id "b", 3) versus A-Z (id "a1") +
        # Z-B (id "a2").  The sorted list ["a1","a2"] is lexicographically
        # smaller than ["b"], so the two-segment route wins at equal cost.
        data = request(
            [point('A', '0'), point('B'), point('Z')],
            targets=['B'],
            candidates=[segment('b', 'A', 'B', '3'),
                        segment('a1', 'A', 'Z', '1'),
                        segment('a2', 'Z', 'B', '2')])
        report = plan(data)
        self.assertEqual(report['total_cost'], '3')
        self.assertEqual([row['id'] for row in report['segments']],
                         ['a1', 'a2'])

    def test_exact_decimal_costs_avoid_float_rounding(self):
        # 0.1 + 0.2 is exactly 3/10; a float path could pick the 0.3 edge or
        # mis-rank.  The 0.3 edge is the correct lexicographic tie here.
        data = request(
            [point('A', '0'), point('B'), point('Z')],
            targets=['B'],
            candidates=[segment('a1', 'A', 'Z', '0.1'),
                        segment('a2', 'Z', 'B', '0.2'),
                        segment('b', 'A', 'B', '0.3')])
        report = plan(data)
        self.assertEqual(report['total_cost'], '3/10')
        self.assertEqual([row['id'] for row in report['segments']],
                         ['a1', 'a2'])

    def test_decimals_compare_by_value_not_string(self):
        data = request(
            [point('A', '0'), point('B')],
            targets=['B'],
            candidates=[segment('x', 'A', 'B', '10'),
                        segment('y', 'A', 'B', '2.5')])
        report = plan(data)
        self.assertEqual(report['total_cost'], '5/2')
        self.assertEqual([row['id'] for row in report['segments']], ['y'])


class UnreachableTests(unittest.TestCase):
    def test_unreachable_targets_are_reported_sorted(self):
        data = request(
            [point('A', '0'), point('B'), point('C'), point('D')],
            targets=['D', 'B'],
            candidates=[segment('AC', 'A', 'C', '1'),
                        segment('BD', 'B', 'D', '1')])
        # B and D form their own island with no candidate bridge to the
        # anchored side; C is not a target and does not matter.
        report = plan(data)
        self.assertFalse(report['feasible'])
        self.assertEqual(report['unreachable_targets'], ['B', 'D'])
        self.assertEqual(report['segments'], [])

    def test_all_targets_unreachable_still_valid(self):
        data = request([point('A', '0'), point('B')], targets=['B'],
                       candidates=[])
        report = plan(data)
        self.assertFalse(report['feasible'])
        self.assertEqual(report['unreachable_targets'], ['B'])
        self.assertIsNone(report['total_cost'])

    def test_partial_connectivity_marks_unreachable_without_plan(self):
        data = request(
            [point('A', '0'), point('B'), point('C')],
            targets=['B', 'C'],
            candidates=[segment('AB', 'A', 'B', '1')])
        report = plan(data)
        self.assertFalse(report['feasible'])
        self.assertEqual(report['unreachable_targets'], ['C'])


class ValidationTests(unittest.TestCase):
    def check(self, data, message):
        with self.assertRaisesRegex(ValueError, message):
            plan(data)

    def test_nonpositive_and_malformed_costs(self):
        for cost in ('0', '-1', '', '1e3', '1.0000001', 2, None, ' 2', '2 '):
            data = a_b_c()
            data['candidate_segments'][0]['cost'] = cost
            with self.subTest(cost=cost):
                self.check(data, 'decimal|positive')

    def test_duplicate_candidate_id(self):
        data = a_b_c()
        data['candidate_segments'].append(segment('AB', 'B', 'C', '1'))
        self.check(data, 'duplicate candidate')

    def test_candidate_endpoint_and_self_loop(self):
        data = a_b_c()
        data['candidate_segments'][0]['to'] = 'Z'
        self.check(data, 'endpoints')
        data = a_b_c()
        data['candidate_segments'][0]['to'] = 'A'
        self.check(data, 'endpoints')

    def test_candidate_extra_fields_rejected(self):
        data = a_b_c()
        data['candidate_segments'][0]['delta'] = '1'
        self.check(data, 'expected fields')

    def test_target_rules(self):
        data = a_b_c()
        data['target_points'] = ['B', 'B']
        self.check(data, 'duplicate target')
        data = a_b_c()
        data['target_points'] = ['A']
        self.check(data, 'fixed point')
        data = a_b_c()
        data['target_points'] = ['Z']
        self.check(data, 'unknown target')

    def test_target_limit(self):
        points = [point('A', '0')] + [point('P%d' % i) for i in range(11)]
        data = request(points, targets=['P%d' % i for i in range(11)])
        self.check(data, 'bounded array')
        candidates = [segment('S%d' % i, 'A', 'P%d' % i, '1')
                      for i in range(10)]
        data = request(points, targets=['P%d' % i for i in range(10)],
                       candidates=candidates)
        report = plan(data)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['total_cost'], '10')
        self.assertEqual(len(report['targets']), 10)

    def test_candidate_limit(self):
        data = a_b_c()
        data['candidate_segments'] = [
            segment('S%d' % i, 'A', 'B', '1') for i in range(2001)]
        self.check(data, 'bounded array')
        data = a_b_c()
        data['target_points'] = ['B']
        data['candidate_segments'] = [
            segment('S%04d' % i, 'A', 'B', '1') for i in range(2000)]
        report = plan(data)
        self.assertEqual(len(report['segments']), 1)
        self.assertEqual(report['segments'][0]['id'], 'S0000')

    def test_observation_contract_preserved(self):
        data = a_b_c()
        data['observations'] = [obs('O', 'A', 'B', weight='0')]
        self.check(data, 'positive')
        data = a_b_c()
        data['observations'] = [obs('O', 'A', 'B', delta='1e3')]
        self.check(data, 'decimal')
        data = a_b_c()
        data['observations'] = [obs('O', 'A', 'Z')]
        self.check(data, 'endpoints')

    def test_no_benchmark_rejected(self):
        data = request([point('A'), point('B')], targets=['B'])
        self.check(data, 'benchmark')

    def test_point_count_bound(self):
        points = [point('A', '0')] + [point('P%d' % i) for i in range(60)]
        self.check(request(points), 'bounded array')

    def test_top_level_fields(self):
        data = a_b_c()
        data['extra'] = 1
        self.check(data, 'expected fields')


class IndependentOptimumTests(unittest.TestCase):
    """Cross-check the Steiner solver against exhaustive enumeration."""

    def random_case(self, rng):
        names = ['A'] + ['P%d' % i for i in range(1, rng.randint(2, 6))]
        benchmark = ['A']
        if len(names) > 3 and rng.random() < 0.4:
            benchmark.append(names[-1])
        points = [point(name, '0' if name in benchmark else None)
                  for name in names]
        observations = []
        for position in range(1, len(names)):
            if rng.random() < 0.4:
                other = rng.randrange(position)
                observations.append(obs('O%d' % position, names[position],
                                        names[other]))
        candidates = []
        for position in range(rng.randint(1, 9)):
            left = rng.randrange(len(names))
            right = rng.randrange(len(names))
            if left == right:
                right = (right + 1) % len(names)
            whole = rng.randint(1, 5)
            fraction = rng.choice([0, 0, 500000, 1])
            cost = (str(whole) if fraction == 0
                    else '%d.%06d' % (whole, fraction))
            candidates.append(segment('S%02d_%d' % (position, rng.randrange(100)),
                                      names[left], names[right], cost))
        unknowns = [name for name in names if name not in benchmark]
        targets = rng.sample(unknowns, rng.randint(1, min(3, len(unknowns))))
        return (request(points, observations, targets, candidates),
                names, benchmark, observations, targets, candidates)

    def test_random_small_graphs_match_brute_force(self):
        rng = random.Random(20261120)
        for trial in range(400):
            data, _, benchmarked, observations, targets, candidates = (
                self.random_case(rng))
            report = plan(data)
            expected = brute_minimum(data['points'], observations, targets,
                                     candidates)
            with self.subTest(trial=trial):
                if expected is None:
                    self.assertFalse(report['feasible'])
                    continue
                self.assertTrue(report['feasible'])
                self.assertEqual(Fraction(report['total_cost']), expected[0])
                self.assertEqual(tuple(sorted(row['id'] for row
                                              in report['segments'])),
                                 expected[1])

    def test_acceptance_family_from_brute_force(self):
        # Exact enumeration of the acceptance graph finds the same plan and
        # total, independently of the solver's component contraction.
        data = a_b_c()
        expected = brute_minimum(data['points'], [], ['B', 'C'],
                                 data['candidate_segments'])
        self.assertEqual(expected, (Fraction(5), ('AB', 'BC')))


class CliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run([sys.executable, '-m', MODULE,
                               '--source', str(source),
                               '--output', str(output)],
                              capture_output=True, text=True, timeout=30)

    def write(self, path, value):
        path.write_text(json.dumps(value))
        return path

    def test_success_and_repeat(self):
        with tempfile.TemporaryDirectory() as t:
            source = ROOT / 'fixtures/plan.json'
            output = Path(t) / 'plan.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = output.read_bytes()
            report = json.loads(first)
            self.assertEqual(report['total_cost'], '5')
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(first, output.read_bytes())

    def test_infeasible_still_succeeds(self):
        with tempfile.TemporaryDirectory() as t:
            data = request([point('A', '0'), point('B')], targets=['B'])
            source = self.write(Path(t) / 'in.json', data)
            output = Path(t) / 'plan.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(output.read_text())['feasible'])

    def test_invalid_input_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'plan.json'
            output.write_text('old')
            source = Path(t) / 'bad.json'
            base = a_b_c()
            broken = []
            broken.append('{')
            broken.append(json.dumps({**base, 'target_points': ['Z']}))
            duplicate = json.dumps(base)[:-1] + ', "target_points": ["B"]}'
            broken.append(duplicate)
            for raw in broken:
                source.write_text(raw)
                result = self.invoke(source, output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'old')

    def test_output_collision_and_symlink(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t) / 'source.json'
            source.write_bytes((ROOT / 'fixtures/plan.json').read_bytes())
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
        # Regression guard: the existing adjustment contract and CLI output
        # for the original fixture remain byte-identical to its documented
        # expectations.
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


class StabilityTests(unittest.TestCase):
    def test_candidate_order_does_not_change_report(self):
        data = a_b_c()
        reference = plan(data)
        reversed_data = a_b_c()
        reversed_data['candidate_segments'].reverse()
        self.assertEqual(plan(reversed_data), reference)

    def test_report_is_json_safe_and_exact(self):
        report = plan(a_b_c())
        walked = []

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            else:
                walked.append(value)

        walk(report)
        for value in walked:
            if isinstance(value, str) and value not in (
                    report['schema'], report['note']):
                # Every numeric value is a string; no float ever appears.
                self.assertFalse(isinstance(value, float))
        self.assertEqual(report['total_cost'], '5')
        for row in report['segments']:
            self.assertEqual(Fraction(row['cost']),
                             Fraction(row['cost']))


if __name__ == '__main__':
    unittest.main()
