"""Tests for ``level-fieldbook`` with hand-derived and independent values.

Every hand-derived expectation below is stated directly from the contract.
The randomized cross-check enumerates *all* complete filings of small inputs
with a separate, deliberately naive implementation (no code shared with the
command) and compares feasibility, the uniquely determined per-run sequences,
and the witness/alternative rows.
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

from level_network.fieldbook import SCHEMA, fieldbook

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'fieldbook'
MODULE = 'level_network.fieldbook_cli'


def point(pid, elevation=None):
    return {'id': pid, 'elevation': elevation}


def record(rid, start, end, origin='A', destination='B', backsight='0',
           foresight='0'):
    return {'id': rid, 'from': origin, 'to': destination,
            'start': start, 'end': end, 'backsight': backsight,
            'foresight': foresight}


def run(rid, stops, start, end, tolerance='0'):
    return {'id': rid, 'stops': list(stops), 'start': start, 'end': end,
            'tolerance': tolerance}


def request(points, records, runs):
    return {'points': list(points), 'records': list(records),
            'runs': list(runs)}


def row_of(report, run_id):
    return next(row for row in report['runs'] if row['id'] == run_id)


def load(name):
    return json.loads((FIXTURES / name).read_text())


# ------------------------------------------------------- independent oracle

def _fixed(points):
    return {p['id']: Fraction(p['elevation'])
            for p in points if p['elevation'] is not None}


def _delta(row):
    return Fraction(row['backsight']) - Fraction(row['foresight'])


def _local_options(run_row, records, fixed):
    """Naive enumeration of every locally valid ordered row selection."""
    stops = run_row['stops']
    pools = []
    for origin, destination in zip(stops, stops[1:]):
        pools.append([row for row in records
                      if row['from'] == origin
                      and row['to'] == destination
                      and row['start'] >= run_row['start']
                      and row['end'] <= run_row['end']])
    options = []
    for combo in itertools.product(*pools):
        ids = [row['id'] for row in combo]
        if len(set(ids)) != len(ids):
            continue
        if any(combo[i]['end'] > combo[i + 1]['start']
               for i in range(len(combo) - 1)):
            continue
        net = sum((_delta(row) for row in combo), Fraction(0))
        difference = fixed[stops[-1]] - fixed[stops[0]]
        if abs(net - difference) > Fraction(run_row['tolerance']):
            continue
        options.append(tuple(ids))
    return options


def all_filings(data):
    """Every complete filing, as {run id: id tuple} (exclusive serials)."""
    fixed = _fixed(data['points'])
    options = {row['id']: _local_options(row, data['records'], fixed)
               for row in data['runs']}
    order = sorted(options)
    filings = []

    def extend(position, used, chosen):
        if position == len(order):
            filings.append(dict(chosen))
            return
        rid = order[position]
        for sequence in options[rid]:
            if set(sequence) & used:
                continue
            chosen[rid] = sequence
            extend(position + 1, used | set(sequence), chosen)
            del chosen[rid]

    extend(0, set(), {})
    return options, filings


def validate_assignment(data, assignment):
    """Rebuild every row of an assignment from the input alone."""
    rows = {row['id']: row for row in data['records']}
    fixed = _fixed(data['points'])
    used = set()
    for run_row in data['runs']:
        sequence = assignment[run_row['id']]
        stops = run_row['stops']
        if len(sequence) != len(stops) - 1:
            return False
        chosen = [rows[rid] for rid in sequence]
        for row, (origin, destination) in zip(chosen, zip(stops, stops[1:])):
            if (row['from'], row['to']) != (origin, destination):
                return False
            if (row['start'] < run_row['start']
                    or row['end'] > run_row['end']):
                return False
        if any(chosen[i]['end'] > chosen[i + 1]['start']
               for i in range(len(chosen) - 1)):
            return False
        if len({row['id'] for row in chosen}) != len(chosen):
            return False
        if set(sequence) & used:
            return False
        used |= set(sequence)
        net = sum((_delta(row) for row in chosen), Fraction(0))
        difference = fixed[stops[-1]] - fixed[stops[0]]
        if abs(net - difference) > Fraction(run_row['tolerance']):
            return False
    return True


class OracleTests(unittest.TestCase):
    def assert_matches_oracle(self, data):
        report = fieldbook(data)
        options, filings = all_filings(data)
        locally_impossible = sorted(rid for rid, rows in options.items()
                                    if not rows)
        with self.subTest(filings=len(filings)):
            if not filings:
                self.assertEqual(report['status'], 'infeasible')
                self.assertIsNone(report['assignment'])
                self.assertEqual(report['runs'], [])
                self.assertEqual(report['locally_impossible_runs'],
                                 locally_impossible)
                return
            self.assertEqual(report['status'], 'feasible')
            self.assertEqual(report['locally_impossible_runs'], [])
            self.assertTrue(validate_assignment(data, report['assignment']))
            witness = {rid: tuple(sequence)
                       for rid, sequence in report['assignment'].items()}
            self.assertIn(witness, filings)
            filed = [row['id'] for row in data['runs']]
            self.assertEqual([row['id'] for row in report['runs']],
                             sorted(filed))
            for run_row in data['runs']:
                rid = run_row['id']
                seen = {filing[rid] for filing in filings}
                row = row_of(report, rid)
                if len(seen) == 1:
                    self.assertEqual(row['status'], 'confirmed')
                    self.assertEqual(tuple(row['record_ids']),
                                     next(iter(seen)))
                    self.assertEqual(Fraction(row['net_delta']),
                                     self._net(run_row, row, data))
                    self.assertIsNone(row['alternative'])
                    self.assertEqual(Fraction(row['closure_error']),
                                     self._closure(run_row, row, data))
                else:
                    self.assertEqual(row['status'], 'ambiguous')
                    self.assertIsNone(row['record_ids'])
                    self.assertIsNone(row['net_delta'])
                    self.assertIsNone(row['closure_error'])
                    alternative = row['alternative']
                    self.assertTrue(validate_assignment(data, alternative))
                    alt = {key: tuple(value)
                           for key, value in alternative.items()}
                    self.assertIn(alt, filings)
                    self.assertNotEqual(alt[rid], witness[rid])
                # The public witness must always be one complete filing.
            self.assertTrue(validate_assignment(data, report['assignment']))

    def _net(self, run_row, row, data):
        rows = {item['id']: item for item in data['records']}
        return sum((_delta(rows[rid]) for rid in row['record_ids']),
                   Fraction(0))

    def _closure(self, run_row, row, data):
        rows = {item['id']: item for item in data['records']}
        fixed = _fixed(data['points'])
        net = sum((_delta(rows[rid]) for rid in row['record_ids']),
                  Fraction(0))
        return net - (fixed[run_row['stops'][-1]]
                      - fixed[run_row['stops'][0]])

    def test_random_small_batches_match_exhaustive_oracle(self):
        rng = random.Random(20261120)
        for trial in range(350):
            data = self._random_case(rng)
            with self.subTest(trial=trial):
                self.assert_matches_oracle(data)

    def _random_case(self, rng):
        names = ['A', 'B', 'C'][:rng.randint(2, 3)]
        points = [point(name, str(rng.randint(0, 2)))
                  for name in names]
        fixed = _fixed(points)
        run_count = rng.randint(1, 3)
        runs, wanted = [], []
        for index in range(run_count):
            length = rng.randint(2, 3)
            stops = [rng.choice(names)]
            while len(stops) < length:
                candidate = rng.choice(names)
                if candidate != stops[-1]:
                    stops.append(candidate)
            window = (rng.randint(0, 8), rng.randint(9, 14))
            runs.append(run(f'R{index}', stops, window[0], window[1],
                            str(rng.randint(0, 2))))
            wanted.extend(zip(stops, stops[1:]))
        records = []
        serial = 0
        for origin, destination in wanted * rng.randint(1, 2):
            serial += 1
            start = rng.randint(0, 12)
            end = start + rng.randint(1, 2)
            delta = rng.choice([-1, 0, 0, 1, 2])
            if delta >= 0:
                backsight, foresight = str(delta), '0'
            else:
                backsight, foresight = '0', str(-delta)
            records.append(record(f's{serial:03d}', start, end,
                                  origin, destination, backsight,
                                  foresight))
        # The contract allows at most four candidates per station; trim any
        # excess before handing the case to the command.
        rng.shuffle(records)
        limited = []
        for run_row in runs:
            for origin, destination in zip(run_row['stops'],
                                           run_row['stops'][1:]):
                pool = [row for row in records
                        if row['from'] == origin and row['to'] == destination
                        and row['start'] >= run_row['start']
                        and row['end'] <= run_row['end']]
                if len(pool) > 4:
                    for row in pool[4:]:
                        records.remove(row)
        return request(points, records, runs)


class ContractExampleTests(unittest.TestCase):
    def base(self, second_window_start=12, last_foresight='5'):
        data = load('example.json')
        data['runs'][1]['start'] = second_window_start
        data['records'][3]['foresight'] = last_foresight
        return data

    def test_distinct_windows_confirm_both_runs(self):
        report = fieldbook(self.base())
        self.assertEqual(report['schema'], SCHEMA)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(report['assignment'],
                         {'R1': ['a', 'c'], 'R2': ['b', 'd']})
        self.assertEqual(report['locally_impossible_runs'], [])
        self.assertEqual(row_of(report, 'R1'),
                         {'id': 'R1', 'status': 'confirmed',
                          'record_ids': ['a', 'c'], 'net_delta': '0',
                          'closure_error': '0', 'alternative': None})
        self.assertEqual(row_of(report, 'R2')['record_ids'], ['b', 'd'])
        self.assertEqual(row_of(report, 'R2')['status'], 'confirmed')

    def test_overlapping_windows_make_both_ambiguous(self):
        report = fieldbook(self.base(second_window_start=10))
        self.assertEqual(report['status'], 'feasible')
        for rid in ('R1', 'R2'):
            row = row_of(report, rid)
            self.assertEqual(row['status'], 'ambiguous')
            self.assertIsNone(row['record_ids'])
            self.assertIsNone(row['net_delta'])
            self.assertIsNone(row['closure_error'])
            self.assertIsNotNone(row['alternative'])
            self.assertNotEqual(row['alternative'][rid],
                                report['assignment'][rid])
        self.assertEqual(set(report['assignment']['R1']),
                         {'a', 'c'})
        self.assertEqual(set(report['assignment']['R2']), {'b', 'd'})

    def test_changed_high_difference_breaks_batch_but_stays_local(self):
        report = fieldbook(self.base(second_window_start=10,
                                     last_foresight='6'))
        self.assertEqual(report['status'], 'infeasible')
        self.assertIsNone(report['assignment'])
        self.assertEqual(report['runs'], [])
        self.assertEqual(report['locally_impossible_runs'], [])

    def test_closure_tolerance_equality_is_accepted(self):
        points = [point('A', '0'), point('B', '0')]
        rows = [record('a', 10, 11, 'A', 'B', '4.000001', '0'),
                record('c', 15, 16, 'B', 'A', '0', '4')]
        equal = fieldbook(request(points, rows,
                                  [run('R', ['A', 'B', 'A'], 10, 16,
                                       '0.000001')]))
        row = row_of(equal, 'R')
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(row['net_delta'], '1/1000000')
        self.assertEqual(row['closure_error'], '1/1000000')
        tighter = fieldbook(request(points, rows,
                                    [run('R', ['A', 'B', 'A'], 10, 16,
                                         '0.000000')]))
        self.assertEqual(tighter['status'], 'infeasible')
        self.assertEqual(tighter['locally_impossible_runs'], ['R'])

    def test_signed_closure_is_reported(self):
        data = request(
            [point('A', '0'), point('B', '1')],
            [record('ab', 0, 1, 'A', 'B', '0', '3')],
            [run('R', ['A', 'B'], 0, 5, '5')])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(row_of(report, 'R')['net_delta'], '-3')
        self.assertEqual(row_of(report, 'R')['closure_error'], '-4')


class PartialConfirmationTests(unittest.TestCase):
    def test_one_forced_run_and_one_ambiguous_run(self):
        # R1's window contains only the low pair (x,u); R2's wide window
        # also admits the two identical high pairs, so only R1 is forced.
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('x', 10, 11, 'A', 'B', '4', '0'),
             record('u', 14, 15, 'B', 'A', '0', '4'),
             record('y', 20, 21, 'A', 'B', '5', '0'),
             record('v', 24, 25, 'B', 'A', '0', '5'),
             record('y2', 26, 27, 'A', 'B', '5', '0'),
             record('v2', 30, 31, 'B', 'A', '0', '5')],
            [run('R1', ['A', 'B', 'A'], 10, 15),
             run('R2', ['A', 'B', 'A'], 20, 35)])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(row_of(report, 'R1')['status'], 'confirmed')
        self.assertEqual(row_of(report, 'R1')['record_ids'], ['x', 'u'])
        self.assertEqual(row_of(report, 'R2')['status'], 'ambiguous')
        self.assertIsNotNone(row_of(report, 'R2')['alternative'])
        self.assertNotEqual(report['assignment']['R2'],
                            row_of(report, 'R2')['alternative']['R2'])

    def test_many_options_share_one_witness(self):
        # Four identical runs over a six-station walk with four parallel
        # rows per direction: every run is ambiguous, yet one witness plus
        # one counterexample per run suffices.
        stops = ['A', 'B', 'C', 'D', 'E', 'F', 'A']
        points = [point(name, '0') for name in 'ABCDEF']
        records, clock = [], 0
        for origin, destination in zip(stops, stops[1:]):
            for index in range(4):
                records.append(record(f'{origin}{destination}{index}',
                                      clock, clock + 1, origin, destination,
                                      '1', '0'))
                clock += 2
        runs = [run(f'R{index}', stops, 0, clock + 1, '1000')
                for index in range(4)]
        report = fieldbook(request(points, records, runs))
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(len(report['assignment']), 4)
        for run_row in report['runs']:
            self.assertEqual(run_row['status'], 'ambiguous')
            alternative = run_row['alternative']
            self.assertEqual(set(alternative), {'R0', 'R1', 'R2', 'R3'})
            self.assertNotEqual(alternative[run_row['id']],
                                report['assignment'][run_row['id']])
        self.assertEqual(len(report['locally_impossible_runs']), 0)


class LocalFailureTests(unittest.TestCase):
    def test_missing_page_is_locally_impossible(self):
        report = fieldbook(load('missing-page.json'))
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['locally_impossible_runs'], ['R3'])

    def test_time_order_breaks_local_sequence(self):
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('ab', 5, 6, 'A', 'B', '1', '0'),
             record('ba', 7, 8, 'B', 'A', '0', '1')],
            [run('R', ['A', 'B', 'A'], 0, 4, '0')])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['locally_impossible_runs'], ['R'])

    def test_row_outside_window_is_not_a_candidate(self):
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('ab', 5, 6, 'A', 'B', '1', '0'),
             record('ba', 7, 8, 'B', 'A', '0', '1')],
            [run('R', ['A', 'B', 'A'], 6, 9, '0')])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['locally_impossible_runs'], ['R'])

    def test_reversed_direction_never_used(self):
        # Only one A->B row exists, and it is used forward for station 1.
        # Station 2 needs B->A; no such row exists, so nothing is filed.
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('ab', 5, 6, 'A', 'B', '1', '0')],
            [run('R', ['A', 'B', 'A'], 0, 10, '0')])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['locally_impossible_runs'], ['R'])

    def test_row_is_never_reversed_to_fit(self):
        # A single B->A row cannot be read as A->B.
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('ba', 5, 6, 'B', 'A', '1', '0')],
            [run('R', ['A', 'B'], 0, 10, '0')])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['locally_impossible_runs'], ['R'])

    def test_no_records_is_a_normal_infeasible_batch(self):
        report = fieldbook(request(
            [point('A', '0'), point('B', '0')], [],
            [run('R', ['A', 'B'], 0, 5)]))
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['locally_impossible_runs'], ['R'])


class StabilityTests(unittest.TestCase):
    def test_reordering_inputs_does_not_change_report(self):
        data = load('contested.json')
        reference = fieldbook(data)
        shuffled = json.loads(json.dumps(data))
        shuffled['points'].reverse()
        shuffled['records'].reverse()
        shuffled['runs'].reverse()
        self.assertEqual(fieldbook(shuffled), reference)

    def test_report_is_exact_and_json_safe(self):
        report = fieldbook(load('example.json'))
        json.dumps(report, allow_nan=False)
        for row in report['runs']:
            if row['status'] == 'confirmed':
                for key in ('net_delta', 'closure_error'):
                    Fraction(report['assignment'] and row[key])
        self.assertTrue(all('alternative' in row for row in report['runs']))

    def test_extra_records_may_remain_unused(self):
        data = load('example.json')
        data['records'].append(record('z', 10, 11, 'A', 'B', '9', '0'))
        report = fieldbook(data)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(report['assignment'], {'R1': ['a', 'c'],
                                                'R2': ['b', 'd']})


class InputErrorTests(unittest.TestCase):
    def assert_rejected(self, data):
        with self.assertRaises(ValueError):
            fieldbook(data)

    def test_too_many_candidates_at_one_station(self):
        data = request(
            [point('A', '0'), point('B', '0')],
            [record(f'a{index}', 0, 1, 'A', 'B', '1', '0')
             for index in range(5)],
            [run('R', ['A', 'B'], 0, 5)])
        self.assert_rejected(data)

    def test_four_candidates_are_allowed_and_used(self):
        # Four parallel A->B rows; with exactly the right closure any of
        # them can serve, so the run is fileable and ambiguous.
        data = request(
            [point('A', '0'), point('B', '2')],
            [record(f'a{index}', 0, 1, 'A', 'B', '2', '0')
             for index in range(4)],
            [run('R', ['A', 'B'], 0, 5)])
        report = fieldbook(data)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(row_of(report, 'R')['status'], 'ambiguous')
        self.assertEqual(len(row_of(report, 'R')['alternative']['R']), 1)

    def test_duplicate_record_id(self):
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('x', 0, 1, 'A', 'B', '1', '0'),
             record('x', 2, 3, 'A', 'B', '1', '0')],
            [run('R', ['A', 'B'], 0, 5)])
        self.assert_rejected(data)

    def test_duplicate_run_id(self):
        data = request(
            [point('A', '0'), point('B', '0')],
            [record('x', 0, 1, 'A', 'B', '1', '0')],
            [run('R', ['A', 'B'], 0, 5), run('R', ['A', 'B'], 0, 5)])
        self.assert_rejected(data)

    def test_unknown_point_and_same_endpoints(self):
        base = request([point('A', '0'), point('B', '0')],
                       [record('x', 0, 1, 'A', 'B', '1', '0')],
                       [run('R', ['A', 'B'], 0, 5)])
        for broken in (
                {**base, 'points': [point('A', '0')]},
                {**base, 'runs': [run('R', ['A', 'Z'], 0, 5)]},
                {**base, 'records': [record('x', 0, 1, 'A', 'A', '1',
                                            '0')]}):
            self.assert_rejected(broken)

    def test_non_benchmark_endpoint_rejected(self):
        data = request([point('A', '0'), point('B')],
                       [record('ab', 0, 1, 'A', 'B', '1', '0')],
                       [run('R', ['A', 'B'], 0, 5)])
        self.assert_rejected(data)

    def test_numeric_contract(self):
        base = request([point('A', '0'), point('B', '0')],
                       [record('x', 0, 1, 'A', 'B', '1', '0')],
                       [run('R', ['A', 'B'], 0, 5)])
        broken = [
            {**base, 'records': [record('x', 0, 0, 'A', 'B', '1', '0')]},
            {**base, 'records': [record('x', 0, 1, 'A', 'B', '-1', '0')]},
            {**base, 'records': [record('x', 0, 1, 'A', 'B', '1.1234567',
                                        '0')]},
            {**base, 'runs': [run('R', ['A', 'B'], 0, 5, '-1')]},
            {**base, 'runs': [run('R', ['A', 'B'], 5, 5)]},
            {**base, 'records': [record('x', 10**9, 10**9 + 1, 'A', 'B',
                                        '1', '0')]},
        ]
        for bad in broken:
            self.assert_rejected(bad)

    def test_structure_contract(self):
        base = request([point('A', '0'), point('B', '0')],
                       [record('x', 0, 1, 'A', 'B', '1', '0')],
                       [run('R', ['A', 'B'], 0, 5)])
        self.assert_rejected({**base, 'extra': 1})
        self.assert_rejected({key: value for key, value in base.items()
                              if key != 'runs'})
        bad_point = {**base, 'points': [point('A', '0'), point('B', '0'),
                                        point('C', '0',)]}
        bad_point['points'][2] = {'id': 'C', 'height': '0'}
        self.assert_rejected(bad_point)
        self.assert_rejected({**base,
                              'runs': [run('R', ['A'], 0, 5)]})


# ------------------------------------------------------------------- CLI

class CliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run([sys.executable, '-m', MODULE,
                               '--source', str(source),
                               '--output', str(output)],
                              capture_output=True, text=True, timeout=30)

    def write(self, path, value):
        path.write_text(json.dumps(value))
        return path

    def test_success_is_repeatable(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            result = self.invoke(FIXTURES / 'example.json', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = output.read_bytes()
            self.assertEqual(json.loads(first)['status'], 'feasible')
            self.assertEqual(self.invoke(FIXTURES / 'example.json',
                                         output).returncode, 0)
            self.assertEqual(first, output.read_bytes())

    def test_infeasible_still_writes_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            result = self.invoke(FIXTURES / 'missing-page.json', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['status'], 'infeasible')
            self.assertEqual(report['locally_impossible_runs'], ['R3'])

    def test_invalid_input_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'report.json'
            output.write_text('old')
            source = Path(t) / 'bad.json'
            base = load('example.json')
            too_many = json.loads(json.dumps(base))
            for index in range(4):
                too_many['records'].append(
                    record(f'x{index}', 10, 11, 'A', 'B', '4', '0'))
            raw_forms = [
                '{',
                json.dumps({**base, 'extra': 1}),
                json.dumps(too_many),
                json.dumps(base)[:-1] + ', "runs": []}',
                json.dumps(base).replace('"start": 10', '"start": NaN', 1),
            ]
            for raw in raw_forms:
                source.write_text(raw)
                result = self.invoke(source, output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'old')

    def test_collision_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t) / 'source.json'
            source.write_bytes((FIXTURES / 'example.json').read_bytes())
            before = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(before, source.read_bytes())
            target = Path(t) / 'target'
            target.write_text('old')
            link = Path(t) / 'link'
            link.symlink_to(target)
            self.assertEqual(self.invoke(source, link).returncode, 2)
            self.assertEqual(target.read_text(), 'old')

    def test_console_script_is_declared(self):
        text = (ROOT / 'pyproject.toml').read_text()
        self.assertIn(
            'level-fieldbook = "level_network.fieldbook_cli:main"', text)


if __name__ == '__main__':
    unittest.main()
