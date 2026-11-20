"""Tests for the ``level-accept`` survey acceptance and lot sign-off command.

The acceptance examples carry hand-derived expectations straight from the
contract.  Two independent checkers support them:

* a contradiction-chain verifier that rebuilds every proof chain from the
  original input JSON alone and re-adds its bounds at the reported ratio;
* a randomized oracle that recomputes the minimal reserve ratio and every
  lot status by brute-force simple-cycle enumeration and its own
  Floyd-Warshall, sharing no code with ``level_network.accept``.
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

from level_network.accept import SCHEMA, accept

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'accept'
MODULE = 'level_network.accept_cli'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}

ZERO = '<shared zero reference>'


def load(name):
    return json.loads((FIXTURES / name).read_text())


def lot_row(report, lot_id):
    return next(row for row in report['lots'] if row['id'] == lot_id)


# ------------------------------------------------- independent chain verifier

def _chain_edges(data, lot_id, ratio, chain):
    """Rebuild every chain item as (start, end, bound_at_ratio) from the
    original input alone, exactly as a field reviewer would."""
    observations = {row['id']: row for row in data['observations']}
    fixed = {row['id']: Fraction(row['elevation'])
             for row in data['points'] if row['elevation'] is not None}
    clauses = {}
    if lot_id is not None:
        lot = next(lot for lot in data['lots'] if lot['id'] == lot_id)
        clauses = {row['id']: row for row in lot['clauses']}
    edges = []
    for item in chain:
        kind, ref, side = item['kind'], item['id'], item['side']
        if kind == 'observation':
            row = observations[ref]
            delta = Fraction(row['delta'])
            nominal = Fraction(row['nominal'])
            reserve = Fraction(row['reserve'])
            if side == 'upper':
                edges.append((row['from'], row['to'],
                              delta + nominal + ratio * reserve))
            else:
                edges.append((row['to'], row['from'],
                              nominal - delta + ratio * reserve))
        elif kind == 'benchmark':
            elevation = fixed[ref]
            if side == 'upper':
                edges.append((ZERO, ref, elevation))
            else:
                edges.append((ref, ZERO, -elevation))
        elif kind == 'clause':
            row = clauses[ref]
            low, high = Fraction(row['min']), Fraction(row['max'])
            if 'point' in row:
                start, end = ZERO, row['point']
            else:
                start, end = row['from'], row['to']
            if side == 'upper':
                edges.append((start, end, high))
            else:
                edges.append((end, start, -low))
        else:
            raise AssertionError('unknown chain kind ' + kind)
    return edges


def verify_chain(data, lot_id, ratio, chain, tight):
    """Check chain connectivity and its summed bound against the input.

    ``tight`` chains (positive minimal survey ratio) must sum to exactly
    zero at the ratio and strictly negative below it, i.e. at least one
    observation with a positive reserve must take part.  Other chains must
    sum strictly negative at the given ratio.
    """
    assert chain, 'chain must not be empty'
    edges = _chain_edges(data, lot_id, ratio, chain)
    for position, (_, end, _) in enumerate(edges):
        following = edges[(position + 1) % len(edges)]
        assert end == following[0], 'chain items must connect head-to-tail'
    total = sum((bound for _, _, bound in edges), Fraction(0))
    if tight:
        assert total == 0, 'tight chain must sum to zero at the ratio'
        observations = {row['id']: row for row in data['observations']}
        reserve = sum((Fraction(observations[item['id']]['reserve'])
                       for item in chain if item['kind'] == 'observation'),
                      Fraction(0))
        assert reserve > 0, 'tight chain must turn negative below the ratio'
    else:
        assert total < 0, 'chain must sum strictly negative'
    return total


def check_compatible(data, ratio, profile):
    """A compatible profile keeps benchmarks and honours every reading."""
    assert set(profile) == {row['id'] for row in data['points']}
    for row in data['points']:
        if row['elevation'] is not None:
            assert profile[row['id']] == Fraction(row['elevation'])
    for row in data['observations']:
        value = profile[row['to']] - profile[row['from']]
        delta = Fraction(row['delta'])
        nominal = Fraction(row['nominal'])
        reserve = Fraction(row['reserve'])
        assert delta - nominal - ratio * reserve <= value
        assert value <= delta + nominal + ratio * reserve


def clause_violations(data, lot_id, profile):
    broken = []
    lot = next(lot for lot in data['lots'] if lot['id'] == lot_id)
    for row in lot['clauses']:
        low, high = Fraction(row['min']), Fraction(row['max'])
        if 'point' in row:
            value = profile[row['point']]
        else:
            value = profile[row['to']] - profile[row['from']]
        if value < low or value > high:
            broken.append(row['id'])
    return broken


def check_report_against_input(testcase, data, report):
    """Independently re-verify every published profile and proof chain."""
    testcase.assertEqual(report['schema'], SCHEMA)
    survey = report['survey']
    if survey['status'] == 'rejected':
        testcase.assertIsNone(survey['reserve_fraction'])
        testcase.assertIsNone(survey['profile'])
        verify_chain(data, None, Fraction(1), survey['proof'], tight=False)
        for row in report['lots']:
            testcase.assertEqual(row['status'], 'not_evaluated')
            testcase.assertIsNone(row['satisfying_profile'])
            testcase.assertIsNone(row['violating_profile'])
            testcase.assertIsNone(row['proof'])
        return
    ratio = Fraction(survey['reserve_fraction'])
    profile = {pid: Fraction(value)
               for pid, value in survey['profile'].items()}
    check_compatible(data, ratio, profile)
    if ratio == 0:
        testcase.assertIsNone(survey['proof'])
    else:
        verify_chain(data, None, ratio, survey['proof'], tight=True)
        kinds = {item['kind'] for item in survey['proof']}
        testcase.assertNotIn('clause', kinds)
    for row in report['lots']:
        if row['status'] == 'pass':
            testcase.assertIsNone(row['satisfying_profile'])
            testcase.assertIsNone(row['violating_profile'])
            testcase.assertIsNone(row['proof'])
        elif row['status'] == 'fail':
            testcase.assertIsNone(row['satisfying_profile'])
            testcase.assertIsNone(row['violating_profile'])
            verify_chain(data, row['id'], ratio, row['proof'], tight=False)
        else:
            testcase.assertEqual(row['status'], 'conditional')
            testcase.assertIsNone(row['proof'])
            satisfying = {pid: Fraction(value) for pid, value
                          in row['satisfying_profile'].items()}
            violating = {pid: Fraction(value) for pid, value
                         in row['violating_profile'].items()}
            check_compatible(data, ratio, satisfying)
            check_compatible(data, ratio, violating)
            testcase.assertEqual(
                clause_violations(data, row['id'], satisfying), [])
            testcase.assertNotEqual(
                clause_violations(data, row['id'], violating), [])


# ------------------------------------------------------- independent oracle

def _simple_cycles(nodes, edges):
    """Enumerate every simple directed cycle of a tiny graph."""
    adjacency = {node: [] for node in nodes}
    for edge in edges:
        adjacency[edge[0]].append(edge)
    order = sorted(nodes, key=lambda node: (node is not ZERO, str(node)))
    rank = {node: position for position, node in enumerate(order)}
    cycles = []
    for start in order:
        stack = [(start, [], {start})]
        while stack:
            node, path, visited = stack.pop()
            for edge in adjacency[node]:
                target = edge[1]
                if target == start:
                    cycles.append(path + [edge])
                elif rank[target] > rank[start] and target not in visited:
                    stack.append((target, path + [edge],
                                  visited | {target}))
    return cycles


def _floyd(nodes, weighted):
    dist = {u: {v: None for v in nodes} for u in nodes}
    for node in nodes:
        dist[node][node] = Fraction(0)
    for start, end, weight in weighted:
        current = dist[start][end]
        if current is None or weight < current:
            dist[start][end] = weight
    for mid in nodes:
        for start in nodes:
            leg_in = dist[start][mid]
            if leg_in is None:
                continue
            for end in nodes:
                leg_out = dist[mid][end]
                if leg_out is None:
                    continue
                candidate = leg_in + leg_out
                current = dist[start][end]
                if current is None or candidate < current:
                    dist[start][end] = candidate
    return dist


def oracle(data):
    """Brute-force acceptance: exact ratio from all simple cycles."""
    fixed = {row['id']: Fraction(row['elevation'])
             for row in data['points'] if row['elevation'] is not None}
    points = [row['id'] for row in data['points']]
    edges = []
    for row in data['observations']:
        delta = Fraction(row['delta'])
        nominal = Fraction(row['nominal'])
        reserve = Fraction(row['reserve'])
        edges.append((row['from'], row['to'], delta + nominal, reserve))
        edges.append((row['to'], row['from'], nominal - delta, reserve))
    for pid, elevation in fixed.items():
        edges.append((ZERO, pid, elevation, Fraction(0)))
        edges.append((pid, ZERO, -elevation, Fraction(0)))
    nodes = [ZERO] + points
    ratio = Fraction(0)
    feasible = True
    for cycle in _simple_cycles(nodes, edges):
        constant = sum((edge[2] for edge in cycle), Fraction(0))
        coefficient = sum((edge[3] for edge in cycle), Fraction(0))
        if coefficient == 0:
            if constant < 0:
                feasible = False
        elif -constant / coefficient > ratio:
            ratio = -constant / coefficient
    if not feasible or ratio > 1:
        return {'status': 'rejected'}
    weighted = [(start, end, constant + ratio * coefficient)
                for start, end, constant, coefficient in edges]
    distances = _floyd(nodes, weighted)
    lots = {}
    for lot in data['lots']:
        clause_edges = []
        for clause in lot['clauses']:
            low = Fraction(clause['min'])
            high = Fraction(clause['max'])
            if 'point' in clause:
                start, end = ZERO, clause['point']
            else:
                start, end = clause['from'], clause['to']
            clause_edges.append((start, end, high, Fraction(0)))
            clause_edges.append((end, start, -low, Fraction(0)))
        joint = edges + clause_edges
        fail = any(sum((edge[2] + ratio * edge[3] for edge in cycle),
                       Fraction(0)) < 0
                   for cycle in _simple_cycles(nodes, joint))
        if fail:
            lots[lot['id']] = 'fail'
            continue
        status = 'pass'
        for clause in lot['clauses']:
            low = Fraction(clause['min'])
            high = Fraction(clause['max'])
            if 'point' in clause:
                start, end = ZERO, clause['point']
            else:
                start, end = clause['from'], clause['to']
            upper = distances[start][end]
            lower = distances[end][start]
            if (upper is None or upper > high
                    or lower is None or -lower < low):
                status = 'conditional'
                break
        lots[lot['id']] = status
    return {'status': 'accepted', 'ratio': ratio, 'lots': lots}


def random_case(rng):
    points = [{'id': 'P0', 'elevation': str(rng.randint(-3, 3))}]
    for index in range(1, rng.randint(2, 5)):
        elevation = None
        if rng.random() < 0.3:
            elevation = str(rng.randint(-3, 3))
        points.append({'id': 'P%d' % index, 'elevation': elevation})
    names = [row['id'] for row in points]
    # Bias most batches towards feasibility by hiding a true elevation
    # assignment behind the readings; the rest stay fully random.
    truth = {pid: rng.randint(-4, 4) for pid in names}
    consistent = rng.random() < 0.7
    observations = []
    for index in range(rng.randint(1, 8)):
        start, end = rng.sample(names, 2)
        if consistent:
            delta = truth[end] - truth[start] + rng.randint(-1, 1)
        else:
            delta = rng.randint(-5, 5)
        observations.append({
            'id': 'O%d' % index, 'from': start, 'to': end,
            'delta': str(delta),
            'nominal': str(rng.choice([0, 1, '0.5', 2])),
            'reserve': str(rng.choice([0, 1, '0.25', 2]))})
    lots = []
    for index in range(rng.randint(1, 3)):
        clauses = []
        for slot in range(rng.randint(1, 3)):
            low = rng.randint(-6, 4)
            high = low + rng.randint(0, 5)
            if rng.random() < 0.5:
                clauses.append({'id': 'c%d' % slot,
                                'point': rng.choice(names),
                                'min': str(low), 'max': str(high)})
            else:
                start, end = rng.sample(names, 2)
                clauses.append({'id': 'c%d' % slot, 'from': start,
                                'to': end, 'min': str(low),
                                'max': str(high)})
        lots.append({'id': 'lot%d' % index, 'clauses': clauses})
    return {'points': points, 'observations': observations, 'lots': lots}


# ------------------------------------------------------------ hand-derived

class AcceptanceExampleTests(unittest.TestCase):
    def test_minimal_ratio_and_profile(self):
        report = accept(load('triangle.json'))
        survey = report['survey']
        self.assertEqual(survey['status'], 'accepted')
        self.assertEqual(survey['reserve_fraction'], '1/3')
        self.assertEqual(survey['profile'], {
            'A': '0', 'B': '4/3', 'C': '8/3', 'X': '8/3', 'Y': '8/3'})
        # Every compatible assignment has B=4/3, C=8/3 and X=Y; the proof
        # is the triangle chain, exactly zero at 1/3 and negative below.
        self.assertEqual(survey['proof'], [
            {'kind': 'observation', 'id': 'AB', 'side': 'upper'},
            {'kind': 'observation', 'id': 'BC', 'side': 'upper'},
            {'kind': 'observation', 'id': 'AC', 'side': 'lower'}])
        check_report_against_input(self, load('triangle.json'), report)

    def test_lot_statuses(self):
        report = accept(load('triangle.json'))
        self.assertEqual([row['id'] for row in report['lots']],
                         ['lot-conditional', 'lot-fail', 'lot-pass'])
        passed = lot_row(report, 'lot-pass')
        self.assertEqual(passed['status'], 'pass')
        self.assertIsNone(passed['satisfying_profile'])
        self.assertIsNone(passed['violating_profile'])
        self.assertIsNone(passed['proof'])
        conditional = lot_row(report, 'lot-conditional')
        self.assertEqual(conditional['status'], 'conditional')
        self.assertEqual(conditional['satisfying_profile']['X'], '8/3')
        self.assertEqual(conditional['violating_profile']['X'], '10')
        self.assertEqual(conditional['violating_profile']['Y'], '10')
        failed = lot_row(report, 'lot-fail')
        self.assertEqual(failed['status'], 'fail')
        self.assertEqual(failed['proof'], [
            {'kind': 'clause', 'id': 'c1', 'side': 'upper'},
            {'kind': 'observation', 'id': 'XY', 'side': 'upper'},
            {'kind': 'clause', 'id': 'c2', 'side': 'lower'}])

    def test_ratio_stays_at_minimum_not_one(self):
        # At ratio 1 the B clause [0, 1.4] would only be conditional
        # (B could reach 2); at the minimal 1/3 it passes, proving the
        # lot judgement uses the minimal ratio.
        report = accept(load('triangle.json'))
        self.assertEqual(lot_row(report, 'lot-pass')['status'], 'pass')

    def test_rejected_batch(self):
        data = load('triangle-rejected.json')
        report = accept(data)
        survey = report['survey']
        self.assertEqual(survey['status'], 'rejected')
        self.assertIsNone(survey['reserve_fraction'])
        self.assertIsNone(survey['profile'])
        # The chain uses only measurements and benchmarks, never clauses.
        self.assertEqual(survey['proof'], [
            {'kind': 'observation', 'id': 'AB', 'side': 'upper'},
            {'kind': 'observation', 'id': 'BC', 'side': 'upper'},
            {'kind': 'observation', 'id': 'AC', 'side': 'lower'}])
        for row in report['lots']:
            self.assertEqual(row['status'], 'not_evaluated')
        check_report_against_input(self, data, report)


class ExactFullReserveTests(unittest.TestCase):
    def test_ratio_exactly_one_is_accepted(self):
        data = load('exact-full-reserve.json')
        report = accept(data)
        survey = report['survey']
        self.assertEqual(survey['status'], 'accepted')
        self.assertEqual(survey['reserve_fraction'], '1')
        self.assertEqual(survey['profile'],
                         {'A': '0', 'B': '3/2', 'C': '3', 'D': '3'})
        # Positive ratio: the triangle chain is exactly zero at ratio 1.
        self.assertEqual(survey['proof'], [
            {'kind': 'observation', 'id': 'AB', 'side': 'upper'},
            {'kind': 'observation', 'id': 'BC', 'side': 'upper'},
            {'kind': 'observation', 'id': 'AC', 'side': 'lower'}])
        self.assertEqual(lot_row(report, 'lot-pass')['status'], 'pass')
        conditional = lot_row(report, 'lot-conditional')
        self.assertEqual(conditional['status'], 'conditional')
        self.assertEqual(conditional['satisfying_profile']['D'], '4')
        self.assertEqual(conditional['violating_profile']['D'], '3')
        failed = lot_row(report, 'lot-fail')
        self.assertEqual(failed['status'], 'fail')
        self.assertEqual(failed['proof'], [
            {'kind': 'benchmark', 'id': 'A', 'side': 'upper'},
            {'kind': 'observation', 'id': 'AD', 'side': 'upper'},
            {'kind': 'clause', 'id': 'c1', 'side': 'lower'}])
        check_report_against_input(self, data, report)


class FloatingComponentTests(unittest.TestCase):
    def test_absolute_and_relative_clauses_differ(self):
        data = load('floating.json')
        report = accept(data)
        survey = report['survey']
        self.assertEqual(survey['status'], 'accepted')
        self.assertEqual(survey['reserve_fraction'], '0')
        self.assertIsNone(survey['proof'])
        # The floating component is never pinned: its absolute clause is
        # conditional while the relative clause passes or fails outright.
        absolute = lot_row(report, 'lot-absolute')
        self.assertEqual(absolute['status'], 'conditional')
        self.assertEqual(absolute['satisfying_profile']['X'], '0')
        self.assertEqual(absolute['violating_profile']['X'], '11')
        self.assertEqual(lot_row(report, 'lot-relative-pass')['status'],
                         'pass')
        relative_fail = lot_row(report, 'lot-relative-fail')
        self.assertEqual(relative_fail['status'], 'fail')
        self.assertEqual(relative_fail['proof'], [
            {'kind': 'observation', 'id': 'XY', 'side': 'upper'},
            {'kind': 'clause', 'id': 'c1', 'side': 'lower'}])
        check_report_against_input(self, data, report)


class BenchmarkConflictTests(unittest.TestCase):
    def test_fixed_benchmark_conflict_not_absorbed(self):
        data = load('benchmark-conflict.json')
        report = accept(data)
        survey = report['survey']
        self.assertEqual(survey['status'], 'rejected')
        self.assertEqual(survey['proof'], [
            {'kind': 'benchmark', 'id': 'A', 'side': 'upper'},
            {'kind': 'observation', 'id': 'AB', 'side': 'upper'},
            {'kind': 'observation', 'id': 'BC', 'side': 'upper'},
            {'kind': 'benchmark', 'id': 'C', 'side': 'lower'}])
        check_report_against_input(self, data, report)


class ZeroReserveTests(unittest.TestCase):
    def test_zero_ratio_has_null_proof(self):
        data = load('zero-reserve.json')
        report = accept(data)
        survey = report['survey']
        self.assertEqual(survey['status'], 'accepted')
        self.assertEqual(survey['reserve_fraction'], '0')
        self.assertIsNone(survey['proof'])
        self.assertEqual(survey['profile'], {'A': '0', 'B': '1'})
        self.assertEqual(lot_row(report, 'lot-pass')['status'], 'pass')
        conditional = lot_row(report, 'lot-conditional')
        self.assertEqual(conditional['status'], 'conditional')
        self.assertEqual(conditional['satisfying_profile']['B'], '3/2')
        self.assertEqual(conditional['violating_profile']['B'], '1')
        failed = lot_row(report, 'lot-fail')
        self.assertEqual(failed['status'], 'fail')
        self.assertEqual(failed['proof'], [
            {'kind': 'benchmark', 'id': 'A', 'side': 'upper'},
            {'kind': 'observation', 'id': 'AB', 'side': 'upper'},
            {'kind': 'clause', 'id': 'c1', 'side': 'lower'}])
        check_report_against_input(self, data, report)


class StructuralCasesTests(unittest.TestCase):
    def test_multi_jump_minimal_ratio(self):
        # The A-D-E-A cycle needs 2/3, the A-B-C-A triangle only 1/3; the
        # exact root-chase must take both jumps and keep the tighter cycle
        # as the proof.
        data = {
            'points': [{'id': 'A', 'elevation': '0'}]
                      + [{'id': pid, 'elevation': None}
                         for pid in ('B', 'C', 'D', 'E')],
            'observations': [
                {'id': 'AB', 'from': 'A', 'to': 'B', 'delta': '1',
                 'nominal': '0', 'reserve': '1'},
                {'id': 'BC', 'from': 'B', 'to': 'C', 'delta': '1',
                 'nominal': '0', 'reserve': '1'},
                {'id': 'AC', 'from': 'A', 'to': 'C', 'delta': '3',
                 'nominal': '0', 'reserve': '1'},
                {'id': 'AD', 'from': 'A', 'to': 'D', 'delta': '1',
                 'nominal': '0', 'reserve': '1'},
                {'id': 'DE', 'from': 'D', 'to': 'E', 'delta': '1',
                 'nominal': '0', 'reserve': '1'},
                {'id': 'EA', 'from': 'E', 'to': 'A', 'delta': '-4',
                 'nominal': '0', 'reserve': '1'}],
            'lots': [{'id': 'L', 'clauses': [
                {'id': 'c', 'point': 'B', 'min': '0', 'max': '10'}]}]}
        report = accept(data)
        survey = report['survey']
        self.assertEqual(survey['reserve_fraction'], '2/3')
        self.assertEqual(survey['proof'], [
            {'kind': 'observation', 'id': 'AD', 'side': 'upper'},
            {'kind': 'observation', 'id': 'DE', 'side': 'upper'},
            {'kind': 'observation', 'id': 'EA', 'side': 'upper'}])
        check_report_against_input(self, data, report)

    def test_isolated_point_absolute_clause_is_conditional(self):
        data = {
            'points': [{'id': 'A', 'elevation': '0'},
                       {'id': 'B', 'elevation': None},
                       {'id': 'Q', 'elevation': None}],
            'observations': [{'id': 'AB', 'from': 'A', 'to': 'B',
                              'delta': '2', 'nominal': '0',
                              'reserve': '0'}],
            'lots': [{'id': 'L', 'clauses': [
                {'id': 'c', 'point': 'Q', 'min': '0', 'max': '5'}]}]}
        report = accept(data)
        row = lot_row(report, 'L')
        self.assertEqual(row['status'], 'conditional')
        self.assertEqual(row['satisfying_profile']['Q'], '2')
        self.assertEqual(row['violating_profile']['Q'], '6')
        check_report_against_input(self, data, report)

    def test_conditional_difference_clause(self):
        # h_B in [1, 3] and h_C - h_B in [0, 2], so h_C - h_A ranges over
        # [1, 5] and the [2, 3.5] clause is neither always nor never met.
        data = {
            'points': [{'id': 'A', 'elevation': '0'},
                       {'id': 'B', 'elevation': None},
                       {'id': 'C', 'elevation': None}],
            'observations': [
                {'id': 'AB', 'from': 'A', 'to': 'B', 'delta': '2',
                 'nominal': '1', 'reserve': '0'},
                {'id': 'BC', 'from': 'B', 'to': 'C', 'delta': '1',
                 'nominal': '1', 'reserve': '0'}],
            'lots': [{'id': 'L', 'clauses': [
                {'id': 'c', 'from': 'A', 'to': 'C',
                 'min': '2', 'max': '3.5'}]}]}
        report = accept(data)
        row = lot_row(report, 'L')
        self.assertEqual(row['status'], 'conditional')
        satisfying = {pid: Fraction(value)
                      for pid, value in row['satisfying_profile'].items()}
        violating = {pid: Fraction(value)
                     for pid, value in row['violating_profile'].items()}
        self.assertTrue(Fraction(2)
                        <= satisfying['C'] - satisfying['A']
                        <= Fraction(3.5))
        self.assertFalse(Fraction(2)
                         <= violating['C'] - violating['A']
                         <= Fraction(3.5))
        check_report_against_input(self, data, report)


class OracleCrossCheckTests(unittest.TestCase):
    def test_fixtures_against_oracle(self):
        for name in ('triangle.json', 'triangle-rejected.json',
                     'exact-full-reserve.json', 'floating.json',
                     'benchmark-conflict.json', 'zero-reserve.json'):
            with self.subTest(name=name):
                data = load(name)
                report = accept(data)
                expected = oracle(data)
                survey = report['survey']
                self.assertEqual(survey['status'], expected['status'])
                if expected['status'] == 'accepted':
                    self.assertEqual(Fraction(survey['reserve_fraction']),
                                     expected['ratio'])
                    for row in report['lots']:
                        self.assertEqual(row['status'],
                                         expected['lots'][row['id']])

    def test_randomized_cases_against_oracle(self):
        rng = random.Random(20261120)
        for trial in range(150):
            with self.subTest(trial=trial):
                data = random_case(rng)
                report = accept(data)
                expected = oracle(data)
                survey = report['survey']
                self.assertEqual(survey['status'], expected['status'])
                if expected['status'] == 'rejected':
                    check_report_against_input(self, data, report)
                    continue
                self.assertEqual(Fraction(survey['reserve_fraction']),
                                 expected['ratio'])
                for row in report['lots']:
                    self.assertEqual(row['status'],
                                     expected['lots'][row['id']])
                check_report_against_input(self, data, report)


# ------------------------------------------------------------------ validation

def base_case():
    return {
        'points': [{'id': 'A', 'elevation': '0'},
                   {'id': 'B', 'elevation': None}],
        'observations': [{'id': 'AB', 'from': 'A', 'to': 'B',
                          'delta': '1', 'nominal': '0.5',
                          'reserve': '1'}],
        'lots': [{'id': 'L', 'clauses': [
            {'id': 'c', 'point': 'B', 'min': '0', 'max': '2'}]}]}


class ValidationTests(unittest.TestCase):
    def reject(self, mutate):
        data = base_case()
        mutate(data)
        with self.assertRaises(ValueError):
            accept(data)

    def test_top_level_fields(self):
        self.reject(lambda data: data.pop('lots'))
        self.reject(lambda data: data.update(extra=1))

    def test_points(self):
        self.reject(lambda data: data['points'].__setitem__(0, {}))
        self.reject(lambda data: data['points'].append(
            {'id': 'A', 'elevation': '1'}))
        self.reject(lambda data: data['points'].__setitem__(
            0, {'id': 'A', 'elevation': None}))
        self.reject(lambda data: data.update(points=[]))
        self.reject(lambda data: data['points'].__setitem__(
            0, {'id': 'A', 'elevation': '1.0000001'}))
        self.reject(lambda data: data.update(
            points=[{'id': 'P%d' % i, 'elevation': '0'}
                    for i in range(61)]))

    def test_observations(self):
        self.reject(lambda data: data.update(observations=[]))
        self.reject(lambda data: data['observations'].append(
            dict(data['observations'][0])))
        self.reject(lambda data: data['observations'].__setitem__(
            0, {'id': 'AB', 'from': 'A', 'to': 'A', 'delta': '1',
                'nominal': '0', 'reserve': '0'}))
        self.reject(lambda data: data['observations'].__setitem__(
            0, {'id': 'AB', 'from': 'A', 'to': 'Z', 'delta': '1',
                'nominal': '0', 'reserve': '0'}))
        self.reject(lambda data: data['observations'][0].update(
            nominal='-1'))
        self.reject(lambda data: data['observations'][0].update(
            reserve='-0.5'))
        self.reject(lambda data: data['observations'][0].update(
            delta=1.5))
        self.reject(lambda data: data['observations'][0].update(
            weight='1'))
        self.reject(lambda data: data['observations'][0].pop('reserve'))

    def test_lots(self):
        self.reject(lambda data: data.update(lots=[]))
        self.reject(lambda data: data['lots'].append(
            {'id': 'L', 'clauses': [
                {'id': 'x', 'point': 'B', 'min': '0', 'max': '1'}]}))
        self.reject(lambda data: data['lots'][0].update(clauses=[]))
        self.reject(lambda data: data['lots'][0]['clauses'].append(
            {'id': 'c', 'point': 'A', 'min': '0', 'max': '1'}))
        self.reject(lambda data: data['lots'][0]['clauses'].__setitem__(
            0, {'id': 'c', 'point': 'B', 'min': '3', 'max': '2'}))
        self.reject(lambda data: data['lots'][0]['clauses'].__setitem__(
            0, {'id': 'c', 'point': 'Z', 'min': '0', 'max': '2'}))
        self.reject(lambda data: data['lots'][0]['clauses'].__setitem__(
            0, {'id': 'c', 'from': 'A', 'to': 'A', 'min': '0', 'max': '2'}))
        self.reject(lambda data: data['lots'][0]['clauses'].__setitem__(
            0, {'id': 'c', 'from': 'A', 'to': 'B', 'min': '0', 'max': '2',
                'point': 'B'}))
        self.reject(lambda data: data['lots'][0]['clauses'].__setitem__(
            0, {'id': 'c', 'point': 'B', 'min': '0'}))

    def test_clause_on_fixed_point(self):
        data = base_case()
        data['lots'] = [{'id': 'L', 'clauses': [
            {'id': 'c', 'point': 'A', 'min': '0', 'max': '0'}]}]
        report = accept(data)
        self.assertEqual(lot_row(report, 'L')['status'], 'pass')
        data['lots'][0]['clauses'][0]['min'] = '1'
        data['lots'][0]['clauses'][0]['max'] = '2'
        report = accept(data)
        self.assertEqual(lot_row(report, 'L')['status'], 'fail')

    def test_all_fixed_network_never_conditional(self):
        data = {
            'points': [{'id': 'A', 'elevation': '0'},
                       {'id': 'B', 'elevation': '2'}],
            'observations': [{'id': 'AB', 'from': 'A', 'to': 'B',
                              'delta': '2', 'nominal': '0',
                              'reserve': '0'}],
            'lots': [{'id': 'L', 'clauses': [
                {'id': 'c', 'from': 'A', 'to': 'B',
                 'min': '1', 'max': '3'}]}]}
        report = accept(data)
        self.assertEqual(report['survey']['reserve_fraction'], '0')
        self.assertEqual(lot_row(report, 'L')['status'], 'pass')
        data['lots'][0]['clauses'][0]['max'] = '1'
        report = accept(data)
        self.assertEqual(lot_row(report, 'L')['status'], 'fail')


class DeterminismTests(unittest.TestCase):
    def test_input_reordering_keeps_report(self):
        data = load('triangle.json')
        shuffled = {
            'points': list(reversed(data['points'])),
            'observations': list(reversed(data['observations'])),
            'lots': [
                {'id': lot['id'], 'clauses': list(reversed(lot['clauses']))}
                for lot in reversed(data['lots'])]}
        self.assertEqual(accept(shuffled), accept(data))

    def test_random_shuffles_keep_report(self):
        rng = random.Random(7)
        data = load('exact-full-reserve.json')
        expected = accept(data)
        for _ in range(10):
            shuffled = {key: list(value) for key, value in data.items()}
            for key in shuffled:
                rng.shuffle(shuffled[key])
            for lot in shuffled['lots']:
                clauses = list(lot['clauses'])
                rng.shuffle(clauses)
                lot['clauses'] = clauses
            self.assertEqual(accept(shuffled), expected)


# ------------------------------------------------------------------------- CLI

class CliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', MODULE, *map(str, arguments)],
            capture_output=True, text=True, timeout=60, env=ENV)

    def test_acceptance_examples_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            expectations = {
                'triangle.json': ('accepted', '1/3'),
                'triangle-rejected.json': ('rejected', None),
                'exact-full-reserve.json': ('accepted', '1'),
                'floating.json': ('accepted', '0'),
                'benchmark-conflict.json': ('rejected', None),
                'zero-reserve.json': ('accepted', '0'),
            }
            for name, (status, ratio) in expectations.items():
                output = t / (name + '.report')
                result = self.run_cli('--source', FIXTURES / name,
                                      '--output', output)
                self.assertEqual(result.returncode, 0, result.stderr)
                survey = json.loads(output.read_text())['survey']
                self.assertEqual(survey['status'], status)
                self.assertEqual(survey['reserve_fraction'], ratio)
                # A legal business rejection still publishes its report.
                # Repeated runs are byte-identical.
                first = output.read_bytes()
                result = self.run_cli('--source', FIXTURES / name,
                                      '--output', output)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), first)

    def test_failures_preserve_existing_report(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            output = t / 'report.json'
            output.write_text('old')
            bad = t / 'bad.json'
            for raw in ('{', '{"a":1,"a":2}', '{"a":NaN}', '{}',
                        '{"points":[],"observations":[],"lots":[]}'):
                bad.write_text(raw)
                result = self.run_cli('--source', bad, '--output', output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'old')

    def test_path_collision_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            source = t / 'source.json'
            source.write_bytes((FIXTURES / 'triangle.json').read_bytes())
            before = source.read_bytes()
            result = self.run_cli('--source', source, '--output', source)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), before)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            target = t / 'target'
            target.write_text('old')
            link = t / 'link'
            link.symlink_to(target)
            result = self.run_cli('--source', FIXTURES / 'triangle.json',
                                  '--output', link)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), 'old')

    def test_missing_source_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            result = self.run_cli('--source', t / 'missing',
                                  '--output', t / 'out.json')
            self.assertEqual(result.returncode, 2)

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


if __name__ == '__main__':
    unittest.main()
