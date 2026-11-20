"""Tests for the level-monitor workbench with hand-derived expectations."""
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from level_network.monitor import (apply_request, build_report, initialize,
                                   load_state, validate_state)

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'level_network.monitor_cli'


def config(points, reference_epoch=0, thresholds=(), reference_observations=()):
    return {'points': points, 'reference_epoch': reference_epoch,
            'thresholds': list(thresholds),
            'reference_observations': list(reference_observations)}


def obs(oid, start, end, delta, weight='1'):
    return {'id': oid, 'from': start, 'to': end, 'delta': delta,
            'weight': weight}


def batch(request_id, changes=(), acks=()):
    return {'request_id': request_id, 'changes': list(changes),
            'acks': list(acks)}


def change(epoch, revision, observations):
    return {'epoch': epoch, 'revision': revision,
            'observations': observations}


def ack(point, epoch, reference_height, opening_height, operator='op'):
    return {'point': point, 'opening_epoch': epoch,
            'reference_height': reference_height,
            'opening_height': opening_height, 'operator': operator}


def a_b_config(on='5', off='2', reference='0'):
    return config(
        points=[{'id': 'A', 'elevation': '0'},
                {'id': 'B', 'elevation': None}],
        reference_epoch=0,
        thresholds=[{'point': 'B', 'on': on, 'off': off}],
        reference_observations=[obs('AB0', 'A', 'B', reference)])


def a_b(epoch, revision, delta):
    return change(epoch, revision, [obs('AB%d' % epoch, 'A', 'B', delta)])


def episodes(report):
    return [(row['point'], row['opening_epoch'], row['closing_epoch'],
             row['status'], row['confirmed']) for row in report['episodes']]


class AcceptanceTests(unittest.TestCase):
    """The exact acceptance series from the brief, checked by hand."""

    def setUp(self):
        self.state = initialize(a_b_config())

    def apply(self, request):
        self.state, sequence, duplicate = apply_request(self.state, request)
        self.assertFalse(duplicate)
        return sequence

    def test_acceptance_merge_orphan_and_restore(self):
        self.assertEqual(self.apply(batch('r1', [a_b(1, 1, '6')])), 2)
        self.assertEqual(self.apply(batch('r2', [a_b(2, 1, '1')])), 3)
        self.assertEqual(self.apply(batch('r3', [a_b(3, 1, '7')])), 4)
        self.assertEqual(episodes(build_report(self.state, 4)), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', False)])

        # Confirm the second episode after sequence 4.
        self.assertEqual(self.apply(batch('ack', [], [
            ack('B', 3, '0', '7', 'alice')])), 5)

        # Correct epoch 2 upward; (1,2) closes and (3,open) merges into (1,open).
        self.assertEqual(self.apply(batch('r5', [a_b(2, 2, '6')])), 6)
        merged = build_report(self.state, 6)
        self.assertEqual(episodes(merged), [('B', 1, None, 'open', False)])
        self.assertEqual(
            [(row['opening_epoch'], row['status'], row['operator'])
             for row in merged['confirmations']],
            [(3, 'orphaned', 'alice')])

        # Correct epoch 2 back to 1: same identity returns with its operator.
        self.assertEqual(self.apply(batch('r6', [a_b(2, 3, '1')])), 7)
        restored = build_report(self.state, 7)
        self.assertEqual(episodes(restored), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', True)])
        self.assertEqual(
            [(row['opening_epoch'], row['status'], row['operator'])
             for row in restored['confirmations']],
            [(3, 'valid', 'alice')])

        # Earlier sequences keep their original facts and confirmations.
        self.assertEqual(episodes(build_report(self.state, 4)), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', False)])
        self.assertEqual(episodes(build_report(self.state, 5)), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', True)])
        self.assertEqual(build_report(self.state, 5)['confirmations'][0]['status'],
                         'valid')

    def test_duplicate_confirmations_keep_first_operator(self):
        self.apply(batch('r1', [a_b(1, 1, '6'), a_b(2, 1, '1'),
                                a_b(3, 1, '7')]))
        self.apply(batch('c1', [], [ack('B', 1, '0', '6', 'alice'),
                                     ack('B', 3, '0', '7', 'alice')]))
        self.apply(batch('c2', [], [ack('B', 1, '0', '6', 'bob'),
                                     ack('B', 3, '0', '7', 'bob')]))
        report = build_report(self.state)
        self.assertEqual([(row['confirmed'], row['operator'])
                          for row in report['episodes']],
                         [(True, 'alice'), (True, 'alice')])

    def test_unknown_identity_rejects_whole_batch(self):
        self.apply(batch('r1', [a_b(1, 1, '6')]))
        before = json.dumps(self.state, sort_keys=True)
        with self.assertRaisesRegex(ValueError, 'identity'):
            apply_request(self.state, batch('bad', [a_b(4, 1, '7')], [
                ack('B', 1, '0', '9')]))
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)
        self.assertEqual(self.state['last_sequence'], 2)


class EpisodeBoundaryTests(unittest.TestCase):
    def test_threshold_equality_opens_and_closes(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '5')]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 1, None, 'open', False)])
        state, _, _ = apply_request(state, batch('r2', [a_b(2, 1, '2')]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 1, 2, 'closed', False)])

    def test_opposite_displacement_uses_absolute_value(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '-6')]))
        report = build_report(state)
        self.assertEqual(episodes(report), [('B', 1, None, 'open', False)])
        self.assertEqual(report['episodes'][0]['opening_height'], '-6')
        self.assertEqual(report['points'][0]['epochs'][0]['displacement'], '-6')

    def test_withdrawal_and_restore(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('r2', [change(1, 2, None)]))
        self.assertEqual(episodes(build_report(state)), [])
        self.assertNotIn(1, [row['epoch'] for row in build_report(state)['epochs']])
        state, _, _ = apply_request(state, batch('r3', [a_b(1, 3, '6')]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 1, None, 'open', False)])

    def test_out_of_order_epochs_sort_numerically(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(3, 1, '7')]))
        state, _, _ = apply_request(state, batch('r2', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('r3', [a_b(2, 1, '1')]))
        report = build_report(state)
        self.assertEqual([row['epoch'] for row in report['epochs']], [0, 1, 2, 3])
        self.assertEqual(episodes(report), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', False)])

    def test_revision_only_change_keeps_confirmation(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('c1', [], [
            ack('B', 1, '0', '6', 'alice')]))
        state, _, _ = apply_request(state, batch('r2', [a_b(1, 2, '6')]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 1, None, 'open', True)])

    def test_reference_correction_orphans_and_restores_confirmation(self):
        state = initialize(a_b_config(on='5', off='2', reference='0'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('c1', [], [
            ack('B', 1, '0', '6', 'alice')]))
        # Reference moves to 1; the epoch still spans 5 but from a new zero.
        state, _, _ = apply_request(state, batch('r2', [
            change(0, 2, [obs('AB0', 'A', 'B', '1')])]))
        report = build_report(state)
        self.assertEqual(episodes(report), [('B', 1, None, 'open', False)])
        self.assertEqual(report['episodes'][0]['reference_height'], '1')
        self.assertEqual(
            [(row['status'], row['operator']) for row in report['confirmations']],
            [('orphaned', 'alice')])
        # Reference restored exactly; the original identity returns.
        state, _, _ = apply_request(state, batch('r3', [
            change(0, 3, [obs('AB0', 'A', 'B', '0')])]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 1, None, 'open', True)])

    def test_equal_heights_under_different_observations_keep_confirmation(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [
            change(1, 1, [obs('AB1', 'A', 'B', '6')])]))
        state, _, _ = apply_request(state, batch('c1', [], [
            ack('B', 1, '0', '6', 'alice')]))
        # A redundant second observation changes the snapshot but not B=6.
        state, _, _ = apply_request(state, batch('r2', [
            change(1, 2, [obs('AB1', 'A', 'B', '6'),
                          obs('AB1b', 'A', 'B', '6', '2')])]))
        report = build_report(state)
        self.assertEqual([row['elevations']['B'] for row in report['epochs']
                          if row['epoch'] == 1], ['6'])
        self.assertEqual(episodes(report), [('B', 1, None, 'open', True)])
        self.assertEqual(report['confirmations'][0]['status'], 'valid')

    def test_closed_episode_can_be_confirmed(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('r2', [a_b(2, 1, '1')]))
        state, _, _ = apply_request(state, batch('c1', [], [
            ack('B', 1, '0', '6', 'alice')]))
        report = build_report(state)
        self.assertEqual(episodes(report), [('B', 1, 2, 'closed', True)])

    def test_confirmed_open_remains_confirmed_after_late_close(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('c1', [], [
            ack('B', 1, '0', '6', 'alice')]))
        state, _, _ = apply_request(state, batch('r2', [a_b(2, 1, '1')]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 1, 2, 'closed', True)])

    def test_multiple_points_are_independent(self):
        state = initialize(config(
            points=[{'id': 'A', 'elevation': '0'},
                    {'id': 'B', 'elevation': None},
                    {'id': 'C', 'elevation': None}],
            thresholds=[{'point': 'B', 'on': '5', 'off': '2'},
                        {'point': 'C', 'on': '2', 'off': '1'}],
            reference_observations=[obs('AB0', 'A', 'B', '0'),
                                    obs('AC0', 'A', 'C', '0')]))
        state, _, _ = apply_request(state, batch('r1', [change(1, 1, [
            obs('AB1', 'A', 'B', '6'), obs('AC1', 'A', 'C', '1')])]))
        state, _, _ = apply_request(state, batch('r2', [change(2, 1, [
            obs('AB2', 'A', 'B', '6'), obs('AC2', 'A', 'C', '-3')])]))
        report = build_report(state)
        self.assertEqual(episodes(report), [
            ('B', 1, None, 'open', False),
            ('C', 2, None, 'open', False)])
        self.assertEqual([row['point'] for row in report['points']], ['B', 'C'])

    def test_batch_withdraws_and_adds_atomically(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        # Withdraw epoch 1 and add epoch 2 in one batch.
        state, _, _ = apply_request(state, batch('r2', [
            change(1, 2, None), a_b(2, 1, '7')]))
        self.assertEqual(episodes(build_report(state)),
                         [('B', 2, None, 'open', False)])


class ExactnessTests(unittest.TestCase):
    """Unequal-weight loop: solve the normal equations by hand."""

    def setUp(self):
        self.config = config(
            points=[{'id': 'A', 'elevation': '0'},
                    {'id': 'B', 'elevation': None},
                    {'id': 'C', 'elevation': None}],
            thresholds=[{'point': 'B', 'on': '1', 'off': '0.5'},
                        {'point': 'C', 'on': '2', 'off': '1'}],
            reference_observations=[obs('AB0', 'A', 'B', '0'),
                                    obs('AC0', 'A', 'C', '0'),
                                    obs('BC0', 'B', 'C', '0', '2')])

    def test_fractional_loop_heights_and_identity(self):
        # Normal equations: 3B-2C=-1 and -2B+3C=5, so B=7/5 and C=13/5.
        state = initialize(self.config)
        state, _, _ = apply_request(state, batch('r1', [change(1, 1, [
            obs('AB1', 'A', 'B', '1'),
            obs('AC1', 'A', 'C', '3'),
            obs('BC1', 'B', 'C', '1', '2')])]))
        report = build_report(state)
        epoch = [row for row in report['epochs'] if row['epoch'] == 1][0]
        self.assertEqual(epoch['elevations']['B'], '7/5')
        self.assertEqual(epoch['elevations']['C'], '13/5')
        self.assertEqual(epoch['displacements'], {'B': '7/5', 'C': '13/5'})
        self.assertEqual(episodes(report), [
            ('B', 1, None, 'open', False),
            ('C', 1, None, 'open', False)])
        self.assertEqual(report['episodes'][0]['opening_height'], '7/5')
        self.assertEqual(report['episodes'][1]['opening_height'], '13/5')
        # Identity carries the reduced fraction, never a decimal or float.
        self.assertEqual(
            Fraction(report['episodes'][0]['opening_height']), Fraction(7, 5))

    def test_fractional_displacement_is_exact(self):
        state = initialize(self.config)
        state, _, _ = apply_request(state, batch('r1', [change(1, 1, [
            obs('AB1', 'A', 'B', '1'),
            obs('AC1', 'A', 'C', '3'),
            obs('BC1', 'B', 'C', '1', '2')])]))
        # C sits at 13/5, which is 13/5 - 0 above the reference, so the
        # displacement is exactly that reduced fraction.
        values = [Fraction(row['displacement'])
                  for row in build_report(state)['points'][1]['epochs']]
        self.assertEqual(values, [Fraction(13, 5)])


class ValidationTests(unittest.TestCase):
    def test_thresholds_must_be_nonnegative_and_ordered(self):
        for on, off in (('-1', '0'), ('2', '2'), ('1', '2')):
            with self.subTest(on=on, off=off), self.assertRaises(ValueError):
                initialize(a_b_config(on=on, off=off))

    def test_thresholds_are_required(self):
        raw = a_b_config()
        raw['thresholds'] = []
        with self.assertRaisesRegex(ValueError, 'monitored'):
            initialize(raw)
        raw['thresholds'] = [{'point': 'A', 'on': '5', 'off': '2'}]
        with self.assertRaisesRegex(ValueError, 'unknown'):
            initialize(raw)

    def test_reference_snapshot_required(self):
        raw = a_b_config()
        raw['reference_observations'] = []
        with self.assertRaisesRegex(ValueError, 'observations'):
            initialize(raw)

    def test_first_revision_must_be_one(self):
        state = initialize(a_b_config())
        with self.assertRaisesRegex(ValueError, 'first revision'):
            apply_request(state, batch('r', [a_b(1, 2, '6')]))

    def test_revision_must_increase(self):
        state = initialize(a_b_config())
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        with self.assertRaisesRegex(ValueError, 'revision'):
            apply_request(state, batch('rev-1', [a_b(1, 1, '7')]))
        with self.assertRaises(ValueError):
            apply_request(state, batch('rev-0', [a_b(1, 0, '7')]))

    def test_duplicate_epoch_in_batch_rejected(self):
        state = initialize(a_b_config())
        with self.assertRaisesRegex(ValueError, 'duplicate epoch'):
            apply_request(state, batch('r', [a_b(1, 1, '6'), a_b(1, 1, '7')]))

    def test_reference_epoch_cannot_be_withdrawn(self):
        state = initialize(a_b_config())
        with self.assertRaisesRegex(ValueError, 'reference epoch'):
            apply_request(state, batch('r', [change(0, 2, None)]))

    def test_nonreference_epoch_must_exceed_reference(self):
        raw = a_b_config()
        raw['reference_epoch'] = 2
        raw['reference_observations'] = [obs('AB0', 'A', 'B', '0')]
        state = initialize(raw)
        # Epoch 1 is below the reference and must be rejected.
        with self.assertRaisesRegex(ValueError, 'exceed'):
            apply_request(state, batch('r', [a_b(1, 1, '6')]))

    def test_cannot_withdraw_unknown_epoch(self):
        state = initialize(a_b_config())
        with self.assertRaisesRegex(ValueError, 'unknown epoch'):
            apply_request(state, batch('r', [change(1, 1, None)]))

    def test_invalid_observations_reject_whole_batch(self):
        state = initialize(a_b_config())
        bad = change(1, 1, [obs('AB1', 'A', 'B', '6', '0')])
        with self.assertRaisesRegex(ValueError, 'weight'):
            apply_request(state, batch('r', [a_b(2, 1, '7'), bad]))
        self.assertEqual(state['last_sequence'], 1)
        self.assertEqual(state['history'][1:], [])
        self.assertEqual(state['requests'], {})

    def test_unidentifiable_datum_rejects_batch(self):
        raw = config(
            points=[{'id': 'A', 'elevation': '0'},
                    {'id': 'B', 'elevation': None},
                    {'id': 'C', 'elevation': '0'}],
            thresholds=[{'point': 'B', 'on': '5', 'off': '2'}],
            reference_observations=[obs('AB0', 'A', 'B', '0'),
                                    obs('AC0', 'A', 'C', '0')])
        state = initialize(raw)
        with self.assertRaisesRegex(ValueError, 'datum'):
            apply_request(state, batch('r', [change(1, 1, [
                obs('AC1', 'A', 'C', '1')])]))

    def test_retry_is_idempotent_and_conflict_rejected(self):
        state = initialize(a_b_config())
        request = batch('same', [a_b(1, 1, '6')])
        state, sequence, duplicate = apply_request(state, request)
        self.assertEqual((sequence, duplicate), (2, False))
        snapshot = json.dumps(state, sort_keys=True)
        state2, again, duplicate = apply_request(state, request)
        self.assertEqual((again, duplicate), (2, True))
        self.assertEqual(json.dumps(state2, sort_keys=True), snapshot)
        conflicting = batch('same', [a_b(1, 1, '7')])
        with self.assertRaisesRegex(ValueError, 'different content'):
            apply_request(state, conflicting)
        self.assertEqual(json.dumps(state, sort_keys=True), snapshot)

    def test_semantically_equal_retry_is_accepted(self):
        state = initialize(a_b_config())
        request = batch('same', [a_b(1, 1, '6')])
        state, _, _ = apply_request(state, request)
        reordered = {'acks': [], 'changes': [
            {'revision': 1, 'observations': [
                {'weight': '1', 'delta': '6', 'to': 'B', 'from': 'A',
                 'id': 'AB1'}],
             'epoch': 1}], 'request_id': 'same'}
        _, sequence, duplicate = apply_request(state, reordered)
        self.assertEqual((sequence, duplicate), (2, True))


class FixtureExampleTests(unittest.TestCase):
    """The shipped synthetic example reproduces the documented acceptance run."""

    def test_fixture_sequence(self):
        directory = ROOT / 'fixtures' / 'monitor'
        load = lambda name: json.loads((directory / name).read_text())
        state = initialize(load('config.json'))
        for name in ('epoch-1.json', 'epoch-2.json', 'epoch-3.json',
                     'confirm-second.json', 'correct-epoch-2-merged.json',
                     'correct-epoch-2-restored.json'):
            before = state['last_sequence']
            state, sequence, duplicate = apply_request(state, load(name))
            self.assertFalse(duplicate)
            self.assertEqual(sequence, before + 1)
        # Seven sequences: init plus six batches.
        self.assertEqual(state['last_sequence'], 7)
        final = build_report(state)
        self.assertEqual(episodes(final), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', True)])
        self.assertEqual(
            [(row['opening_epoch'], row['status'], row['operator'])
             for row in final['confirmations']],
            [(3, 'valid', 'demo-operator')])
        merged = build_report(state, 6)
        self.assertEqual(episodes(merged), [('B', 1, None, 'open', False)])
        self.assertEqual(merged['confirmations'][0]['status'], 'orphaned')
        original = build_report(state, 4)
        self.assertEqual(episodes(original), [
            ('B', 1, 2, 'closed', False),
            ('B', 3, None, 'open', False)])


class OutputFormatTests(unittest.TestCase):
    def test_report_contains_only_exact_strings(self):
        state = initialize(a_b_config(on='5', off='2'))
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        report = build_report(state)

        def walk(value):
            if isinstance(value, float):
                self.fail('float leaked into report')
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(report)
        self.assertEqual(report['epochs'][1]['elevations']['B'], '6')
        self.assertEqual(report['points'][0]['epochs'][0]['height'], '6')
        json.dumps(report, allow_nan=False)

    def test_failed_batch_leaves_request_id_free(self):
        state = initialize(a_b_config(on='5', off='2'))
        with self.assertRaises(ValueError):
            apply_request(state, batch('reuse', [a_b(1, 1, '6')], [
                ack('B', 9, '0', '9')]))
        self.assertEqual(state['requests'], {})
        # The same id is now usable with valid, different content.
        state, sequence, duplicate = apply_request(
            state, batch('reuse', [a_b(1, 1, '6')]))
        self.assertEqual((sequence, duplicate), (2, False))


class PersistenceTests(unittest.TestCase):
    def test_state_round_trip_revalidates_every_batch(self):
        state = initialize(a_b_config())
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        state, _, _ = apply_request(state, batch('c1', [], [
            ack('B', 1, '0', '6')]))
        self.assertEqual(validate_state(json.loads(json.dumps(state))),
                         state)

    def test_tampered_state_history_rejected(self):
        state = initialize(a_b_config())
        state, _, _ = apply_request(state, batch('r1', [a_b(1, 1, '6')]))
        tampered = json.loads(json.dumps(state))
        tampered['history'][1]['sequence'] = 3
        with self.assertRaisesRegex(ValueError, 'contiguous'):
            validate_state(tampered)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, '-m', MODULE, *map(str, args)],
                              capture_output=True, text=True, timeout=30,
                              cwd=ROOT, env={'PYTHONPATH': str(ROOT / 'src')})

    def write(self, path, value):
        path.write_text(json.dumps(value))
        return path

    def test_cross_process_flow_and_request_retry(self):
        with tempfile.TemporaryDirectory() as t:
            directory = Path(t)
            state = directory / 'state.json'
            config_path = self.write(directory / 'config.json', a_b_config())
            self.assertEqual(self.invoke('init', '--state', state,
                                         '--source', config_path).returncode, 0)
            r1 = self.write(directory / 'r1.json', batch('r1', [a_b(1, 1, '6')]))
            self.assertEqual(self.invoke('apply', '--state', state,
                                         '--source', r1).returncode, 0)
            before = state.read_bytes()
            retry = self.invoke('apply', '--state', state, '--source', r1)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertIn('sequence 2', retry.stdout)
            self.assertEqual(state.read_bytes(), before)
            conflict = self.write(directory / 'conflict.json',
                                  batch('r1', [a_b(2, 1, '7')]))
            self.assertEqual(self.invoke('apply', '--state', state,
                                         '--source', conflict).returncode, 2)
            self.assertEqual(state.read_bytes(), before)

            out = directory / 'report.json'
            self.assertEqual(self.invoke('report', '--state', state,
                                         '--output', out).returncode, 0)
            report = json.loads(out.read_text())
            self.assertEqual(report['as_of'], 2)
            self.assertEqual(episodes(report),
                             [('B', 1, None, 'open', False)])

    def test_failure_keeps_old_report_and_state(self):
        with tempfile.TemporaryDirectory() as t:
            directory = Path(t)
            state = directory / 'state.json'
            config_path = self.write(directory / 'config.json', a_b_config())
            self.invoke('init', '--state', state, '--source', config_path)
            state_bytes = state.read_bytes()
            out = directory / 'report.json'
            self.invoke('report', '--state', state, '--output', out)
            old_report = out.read_bytes()
            # Bad as-of and a rejected batch both leave everything intact.
            self.assertEqual(self.invoke('report', '--state', state,
                                         '--output', out,
                                         '--as-of', 9).returncode, 2)
            self.assertEqual(out.read_bytes(), old_report)
            bad = self.write(directory / 'bad.json', batch('r', [a_b(1, 1, '6')], [
                ack('B', 1, '0', '9')]))
            self.assertEqual(self.invoke('apply', '--state', state,
                                         '--source', bad).returncode, 2)
            self.assertEqual(state.read_bytes(), state_bytes)
            self.assertEqual(out.read_bytes(), old_report)

    def test_init_refuses_existing_state(self):
        with tempfile.TemporaryDirectory() as t:
            directory = Path(t)
            state = directory / 'state.json'
            config_path = self.write(directory / 'config.json', a_b_config())
            self.assertEqual(self.invoke('init', '--state', state,
                                         '--source', config_path).returncode, 0)
            before = state.read_bytes()
            self.assertEqual(self.invoke('init', '--state', state,
                                         '--source', config_path).returncode, 2)
            self.assertEqual(state.read_bytes(), before)

    def test_report_deterministic(self):
        with tempfile.TemporaryDirectory() as t:
            directory = Path(t)
            state = directory / 'state.json'
            self.invoke('init', '--state', state, '--source',
                        self.write(directory / 'config.json', a_b_config()))
            self.invoke('apply', '--state', state, '--source',
                        self.write(directory / 'r1.json',
                                   batch('r1', [a_b(1, 1, '6')])))
            first, second = directory / 'a.json', directory / 'b.json'
            self.invoke('report', '--state', state, '--output', first)
            self.invoke('report', '--state', state, '--output', second)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == '__main__':
    unittest.main()
