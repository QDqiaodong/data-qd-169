"""Traceable epoch revisions and operator-confirmed deformation alarms.

The workbench keeps an append-only history of successful batches.  Facts at
any successful sequence are reconstructed by folding that history, so late
corrections and later confirmations can never rewrite an earlier report.
All elevations, displacements, thresholds and identity comparisons use
``Fraction``; no value is ever converted through ``float``.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import re

from .adjustment import adjust
from .common import array, fields, integer, publish, read_json, text
from .linear import rational

STATE_SCHEMA = 'level-monitor-state-v1'
REPORT_SCHEMA = 'level-monitor-report-v1'
STATE_READ_LIMIT = 64_000_000
ACK_FIELDS = 'point opening_epoch reference_height opening_height operator'
_FRACTION = re.compile(r'-?[0-9]+(?:/[1-9][0-9]*)?')


# ---------------------------------------------------------------- validation

def _points(raw):
    points, seen = [], set()
    for row in array(raw, 60):
        fields(row, 'id elevation')
        pid = text(row['id'])
        if pid in seen:
            raise ValueError('duplicate point')
        seen.add(pid)
        elevation = row['elevation']
        if elevation is not None:
            rational(elevation)
        points.append({'id': pid, 'elevation': elevation})
    if not points:
        raise ValueError('points required')
    return points


def _thresholds(raw, points):
    unknown = {row['id'] for row in points if row['elevation'] is None}
    result, seen = [], set()
    for row in array(raw, 60):
        fields(row, 'point on off')
        point = text(row['point'])
        if point not in unknown:
            raise ValueError('threshold point must be an unknown point')
        if point in seen:
            raise ValueError('duplicate threshold')
        seen.add(point)
        on, off = rational(row['on']), rational(row['off'])
        if on < 0 or off < 0:
            raise ValueError('thresholds must be non-negative')
        if on <= off:
            raise ValueError('on must exceed off')
        result.append({'point': point, 'on': row['on'], 'off': row['off']})
    if not result:
        raise ValueError('at least one monitored point required')
    result.sort(key=lambda row: row['point'])
    return result


def _config(raw):
    fields(raw, 'points reference_epoch thresholds')
    points = _points(raw['points'])
    reference_epoch = integer(raw['reference_epoch'])
    thresholds = _thresholds(raw['thresholds'], points)
    return {'points': points, 'reference_epoch': reference_epoch,
            'thresholds': thresholds}


def _change(raw):
    fields(raw, 'epoch revision observations')
    epoch = integer(raw['epoch'])
    revision = integer(raw['revision'], 1)
    observations = raw['observations']
    if observations is not None:
        observations = array(observations, 2000)
    return {'epoch': epoch, 'revision': revision,
            'observations': observations}


def _fraction_text(value):
    """Canonical reduced fraction string used by episode identities."""
    if (not isinstance(value, str) or len(value) > 200
            or not _FRACTION.fullmatch(value)):
        raise ValueError('fraction string required')
    return str(Fraction(value))


def _ack(raw):
    fields(raw, ACK_FIELDS)
    return {'point': text(raw['point']),
            'opening_epoch': integer(raw['opening_epoch']),
            'reference_height': _fraction_text(raw['reference_height']),
            'opening_height': _fraction_text(raw['opening_height']),
            'operator': text(raw['operator'])}


def _identity(record):
    return (record['point'], record['opening_epoch'],
            record['reference_height'], record['opening_height'])


# --------------------------------------------------------------- state model

def _merge(facts, changes, reference_epoch, sequence):
    merged = dict(facts)
    seen = set()
    for raw in changes:
        change = _change(raw)
        epoch = change['epoch']
        if epoch in seen:
            raise ValueError('duplicate epoch in batch')
        seen.add(epoch)
        current = merged.get(epoch)
        if current is None:
            if change['revision'] != 1:
                raise ValueError('first revision must be 1')
            if change['observations'] is None:
                raise ValueError('cannot withdraw an unknown epoch')
        elif change['revision'] <= current['revision']:
            raise ValueError('revision must exceed known maximum')
        if epoch == reference_epoch and change['observations'] is None:
            raise ValueError('reference epoch cannot be withdrawn')
        merged[epoch] = {'revision': change['revision'],
                         'observations': change['observations'],
                         'sequence': sequence}
    return merged


def _check_facts(config, facts):
    reference_epoch = config['reference_epoch']
    reference = facts.get(reference_epoch)
    if reference is None or reference['observations'] is None:
        raise ValueError('reference epoch snapshot required')
    for epoch, record in facts.items():
        if record['observations'] is None:
            continue
        if epoch != reference_epoch and epoch <= reference_epoch:
            raise ValueError('non-reference epoch must exceed reference epoch')


def _advance(config, facts, measurements, batch):
    """Fold one batch, adjusting only the epochs it touches."""
    reference_epoch = config['reference_epoch']
    facts = _merge(facts, batch['changes'], reference_epoch,
                   batch['sequence'])
    _check_facts(config, facts)
    for change in batch['changes']:
        epoch = change['epoch']
        if facts[epoch]['observations'] is None:
            measurements.pop(epoch, None)
            continue
        result = adjust({'points': config['points'],
                         'observations': facts[epoch]['observations']})
        measurements[epoch] = {pid: Fraction(value)
                               for pid, value in result['elevations'].items()}
    if reference_epoch not in measurements:
        raise ValueError('reference epoch snapshot required')
    return facts, measurements


def _episodes(config, measurements):
    reference = measurements[config['reference_epoch']]
    epochs = sorted(epoch for epoch in measurements
                    if epoch != config['reference_epoch'])
    episodes = []
    for threshold in config['thresholds']:
        point = threshold['point']
        on, off = Fraction(threshold['on']), Fraction(threshold['off'])
        current = None
        for epoch in epochs:
            height = measurements[epoch][point]
            distance = abs(height - reference[point])
            if current is None:
                if distance >= on:
                    current = {'point': point, 'opening_epoch': epoch,
                               'reference_height': str(reference[point]),
                               'opening_height': str(height),
                               'closing_epoch': None}
                    episodes.append(current)
            elif distance <= off:
                current['closing_epoch'] = epoch
                current = None
    episodes.sort(key=lambda row: (row['point'], row['opening_epoch'],
                                   Fraction(row['reference_height']),
                                   Fraction(row['opening_height'])))
    return episodes


def _validate_acks(raw_acks, episodes):
    known = {_identity(row) for row in episodes}
    accepted = []
    for raw in array(raw_acks, 10000):
        ack = _ack(raw)
        if _identity(ack) not in known:
            raise ValueError('confirmation identity not found')
        accepted.append(ack)
    return accepted


def _replay(state, upto):
    config = state['config']
    facts, measurements, confirmations = {}, {}, {}
    for batch in state['history'][:upto]:
        facts, measurements = _advance(config, facts, measurements, batch)
        for ack in batch['acks']:
            key = _identity(ack)
            if key not in confirmations:
                confirmations[key] = {'operator': ack['operator'],
                                      'sequence': batch['sequence']}
    return facts, measurements, confirmations


# ------------------------------------------------------------------- commands

def initialize(source):
    """Build the initial state (sequence 1) from a validated configuration."""
    fields(source, 'points reference_epoch thresholds reference_observations')
    points = _points(source['points'])
    config = {'points': points,
              'reference_epoch': integer(source['reference_epoch']),
              'thresholds': _thresholds(source['thresholds'], points)}
    observations = array(source['reference_observations'], 2000)
    change = {'epoch': config['reference_epoch'], 'revision': 1,
              'observations': observations}
    state = {'schema': STATE_SCHEMA, 'config': config, 'last_sequence': 1,
             'history': [{'sequence': 1, 'kind': 'init', 'request_id': None,
                          'changes': [change], 'acks': []}],
             'requests': {}}
    _replay(state, 1)
    return state


def apply_request(state, source):
    """Return ``(new_state, sequence, duplicate)`` for a batch.

    A canonical retry returns the original state, its sequence and ``True``
    without writing or applying anything.  Every semantic check happens
    before the new state is returned, so a rejected batch cannot leave a
    partial fact, confirmation or request placeholder behind.
    """
    fields(source, 'request_id changes acks')
    request_id = text(source['request_id'])
    changes = array(source['changes'], 2000)
    acks = array(source['acks'], 10000)
    digest = _digest(source)
    previous = state['requests'].get(request_id)
    if previous is not None:
        if previous['digest'] != digest:
            raise ValueError('request_id reused with different content')
        return state, previous['sequence'], True

    config = state['config']
    sequence = state['last_sequence'] + 1
    facts, measurements, _ = _replay(state, state['last_sequence'])
    _, candidate = _advance(config, facts, measurements, {
        'sequence': sequence, 'changes': [_change(change)
                                          for change in changes]})
    accepted = _validate_acks(acks, _episodes(config, candidate))

    new_state = deepcopy(state)
    new_state['last_sequence'] = sequence
    new_state['history'].append({
        'sequence': sequence, 'kind': 'apply', 'request_id': request_id,
        'changes': [_change(change) for change in changes],
        'acks': accepted})
    new_state['requests'][request_id] = {'digest': digest,
                                         'sequence': sequence}
    return new_state, sequence, False


def build_report(state, as_of=None):
    if as_of is None:
        as_of = state['last_sequence']
    as_of = integer(as_of, 1, state['last_sequence'])
    config = state['config']
    reference_epoch = config['reference_epoch']
    facts, measurements, confirmations = _replay(state, as_of)
    episodes = _episodes(config, measurements)
    reference = measurements[reference_epoch]
    monitored = {threshold['point']: threshold
                 for threshold in config['thresholds']}
    effective = sorted(measurements)

    epoch_rows = []
    for epoch in effective:
        record = facts[epoch]
        heights = measurements[epoch]
        epoch_rows.append({
            'epoch': epoch, 'revision': record['revision'],
            'sequence': record['sequence'],
            'reference': epoch == reference_epoch,
            'elevations': {pid: str(heights[pid])
                           for pid in sorted(heights)},
            'displacements': {point: str(heights[point] - reference[point])
                              for point in sorted(monitored)}})

    episode_rows, by_point = [], {point: [] for point in monitored}
    for episode in episodes:
        key = _identity(episode)
        confirmation = confirmations.get(key)
        row = dict(episode)
        row['status'] = 'open' if episode['closing_epoch'] is None else 'closed'
        row['confirmed'] = confirmation is not None
        row['operator'] = confirmation['operator'] if confirmation else None
        row['confirmation_sequence'] = (confirmation['sequence']
                                        if confirmation else None)
        episode_rows.append(row)
        by_point[episode['point']].append(row)

    point_rows = []
    for point in sorted(monitored):
        threshold = monitored[point]
        point_episodes = by_point[point]
        point_rows.append({
            'point': point,
            'reference_height': str(reference[point]),
            'on': threshold['on'], 'off': threshold['off'],
            'epochs': [{'epoch': epoch, 'revision': facts[epoch]['revision'],
                        'sequence': facts[epoch]['sequence'],
                        'height': str(measurements[epoch][point]),
                        'displacement': str(measurements[epoch][point]
                                            - reference[point])}
                       for epoch in effective if epoch != reference_epoch],
            'episodes': point_episodes})

    present = {_identity(row) for row in episodes}
    confirmation_rows = []
    for key in sorted(confirmations, key=lambda item: (
            item[0], item[1], Fraction(item[2]), Fraction(item[3]))):
        record = confirmations[key]
        confirmation_rows.append({
            'point': key[0], 'opening_epoch': key[1],
            'reference_height': key[2], 'opening_height': key[3],
            'operator': record['operator'], 'sequence': record['sequence'],
            'status': 'valid' if key in present else 'orphaned'})

    return {
        'schema': REPORT_SCHEMA,
        'as_of': as_of,
        'reference_epoch': reference_epoch,
        'reference_heights': {pid: str(reference[pid])
                              for pid in sorted(reference)},
        'epochs': epoch_rows,
        'points': point_rows,
        'episodes': episode_rows,
        'confirmations': confirmation_rows}


# ------------------------------------------------------------------ persistence

def validate_state(state):
    fields(state, 'schema config last_sequence history requests')
    if state['schema'] != STATE_SCHEMA:
        raise ValueError('unsupported state schema')
    config = _config(state['config'])
    last_sequence = integer(state['last_sequence'], 1)
    history = array(state['history'], 1_000_000)
    if len(history) != last_sequence:
        raise ValueError('state sequence history mismatch')
    requests = state['requests']
    if not isinstance(requests, dict):
        raise ValueError('request index required')
    normalized_history, expected_requests = [], {}
    for position, raw in enumerate(history, 1):
        fields(raw, 'sequence kind request_id changes acks')
        if type(raw['sequence']) is not int or raw['sequence'] != position:
            raise ValueError('non-contiguous state sequence')
        kind = raw['kind']
        if kind not in ('init', 'apply'):
            raise ValueError('invalid batch kind')
        if position == 1 and kind != 'init':
            raise ValueError('first batch must be init')
        if position > 1 and kind != 'apply':
            raise ValueError('only the first batch may be init')
        request_id = raw['request_id']
        if kind == 'init':
            if request_id is not None:
                raise ValueError('init cannot carry request_id')
        else:
            request_id = text(request_id)
        if request_id is not None and request_id in expected_requests:
            raise ValueError('duplicate request_id in history')
        changes = [_change(change) for change in array(raw['changes'], 2000)]
        acks = [_ack(ack) for ack in array(raw['acks'], 10000)]
        if kind == 'init':
            if len(changes) != 1 or acks:
                raise ValueError('init carries one reference snapshot')
            snapshot = changes[0]
            if (snapshot['epoch'] != config['reference_epoch']
                    or snapshot['revision'] != 1
                    or snapshot['observations'] is None):
                raise ValueError('invalid reference snapshot in state')
        normalized_history.append({'sequence': position, 'kind': kind,
                                   'request_id': request_id,
                                   'changes': changes, 'acks': acks})
        if kind == 'apply':
            expected_requests[request_id] = position
    if set(requests) != set(expected_requests):
        raise ValueError('request index mismatch')
    normalized_requests = {}
    for request_id, record in requests.items():
        text(request_id)
        fields(record, 'digest sequence')
        if (not isinstance(record['digest'], str)
                or not record['digest']
                or record['digest'] != record['digest'].strip()):
            raise ValueError('invalid request digest')
        if record['sequence'] != expected_requests[request_id]:
            raise ValueError('request sequence mismatch')
        normalized_requests[request_id] = {'digest': record['digest'],
                                           'sequence': record['sequence']}
    normalized = {'schema': STATE_SCHEMA, 'config': config,
                  'last_sequence': last_sequence,
                  'history': normalized_history,
                  'requests': normalized_requests}
    # Replay every batch to prove the stored facts, acks and reference
    # snapshot are still internally consistent.
    facts, measurements = {}, {}
    for batch in normalized_history:
        facts, measurements = _advance(config, facts, measurements, batch)
        _validate_acks(batch['acks'], _episodes(config, measurements))
    return normalized


def load_state(path):
    return validate_state(read_json(path, limit=STATE_READ_LIMIT, label='state size limit'))


def save_state(path, state):
    publish(path, state)


def _digest(source):
    canonical = json.dumps(source, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False)
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()
