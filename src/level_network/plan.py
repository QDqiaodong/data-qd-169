"""Offline field-work planning for leveling targets.

``level-plan`` never adjusts anything.  It validates the same points and
observations as ``level-network``, derives the *current* measurement
components from observation endpoint identities, and then chooses a minimum
total-cost set of candidate field segments that connects every requested
target to at least one anchored component.  Candidate segments are future
field work: they are never observations, never receive a height difference
and are never passed to :func:`level_network.adjustment.adjust`.
"""
from fractions import Fraction
import heapq

from .common import array, fields, text
from .linear import rational

REPORT_SCHEMA = 'level-plan-report-v1'
COST_SCALE = 10**6
PLAN_NOTE = ('Selected candidate segments are pending field collection; '
             'they are not observations and no elevations are derived.')


# ------------------------------------------------------------------- input

def _points(raw):
    points, order = {}, []
    for row in array(raw, 60):
        fields(row, 'id elevation')
        pid = text(row['id'])
        if pid in points:
            raise ValueError('duplicate point')
        elevation = row['elevation']
        if elevation is not None:
            rational(elevation)
        points[pid] = elevation
        order.append(pid)
    if not order:
        raise ValueError('points required')
    if not any(elevation is not None for elevation in points.values()):
        raise ValueError('at least one benchmark required')
    return points, order


def _observations(raw, points):
    observations, ids = [], set()
    for row in array(raw, 2000):
        fields(row, 'id from to delta weight')
        oid = text(row['id'])
        start, end = text(row['from']), text(row['to'])
        if (oid in ids or start not in points or end not in points
                or start == end):
            raise ValueError('invalid observation identity or endpoints')
        ids.add(oid)
        rational(row['delta'])
        if rational(row['weight']) <= 0:
            raise ValueError('weight must be positive')
        observations.append((oid, start, end))
    return observations


def _targets(raw, points):
    targets, seen = [], set()
    for value in array(raw, 10):
        pid = text(value)
        if pid in seen:
            raise ValueError('duplicate target point')
        if pid not in points:
            raise ValueError('unknown target point')
        if points[pid] is not None:
            raise ValueError('fixed point cannot be a target')
        seen.add(pid)
        targets.append(pid)
    return targets


def _candidates(raw, points):
    candidates, ids = [], set()
    for row in array(raw, 2000):
        fields(row, 'id from to cost')
        sid = text(row['id'])
        start, end = text(row['from']), text(row['to'])
        if sid in ids:
            raise ValueError('duplicate candidate segment id')
        if start not in points or end not in points or start == end:
            raise ValueError('invalid candidate segment endpoints')
        cost = rational(row['cost'])
        if cost <= 0:
            raise ValueError('candidate cost must be positive')
        ids.add(sid)
        candidates.append({'id': sid, 'from': start, 'to': end,
                           'cost': cost})
    return candidates


# ----------------------------------------------------------------- topology

class _Components:
    def __init__(self, point_order):
        self.parent = {pid: pid for pid in point_order}

    def find(self, pid):
        root = pid
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[pid] != pid:
            parent = self.parent[pid]
            self.parent[pid] = root
            pid = parent
        return root

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


def _component_map(point_order, observations):
    union = _Components(point_order)
    for _, start, end in observations:
        union.union(start, end)
    labels, component_of = {}, {}
    for pid in sorted(point_order):
        root = union.find(pid)
        if root not in labels:
            labels[root] = len(labels)
        component_of[pid] = labels[root]
    return component_of


def _components(points, component_of, targets):
    target_set = set(targets)
    groups = {}
    for pid in sorted(points):
        groups.setdefault(component_of[pid], []).append(pid)
    rows = []
    for number in sorted(groups):
        members = groups[number]
        benchmarks = [pid for pid in members
                      if points[pid] is not None]
        rows.append({
            'points': members,
            'benchmarks': benchmarks,
            'anchored': bool(benchmarks),
            'target_points': [pid for pid in members
                              if pid in target_set]})
    return rows


# -------------------------------------------------------------------- solve

def _best_candidate_edges(candidates, component_of, node_of,
                          anchored_components):
    """Collapse parallel component connections to their dominating edge.

    For one pair of current components every other parallel candidate is
    dominated: a higher cost is always worse, and at equal cost a larger id
    is always lexicographically worse.  A cheapest/lexicographically-first
    edge can therefore stand for the pair without losing an optimum.
    """
    best = {}
    for candidate in candidates:
        left_comp = component_of[candidate['from']]
        right_comp = component_of[candidate['to']]
        left = (0 if left_comp in anchored_components
                else node_of[left_comp])
        right = (0 if right_comp in anchored_components
                 else node_of[right_comp])
        if left == right:
            continue
        if left > right:
            left, right = right, left
        key = (left, right)
        rank = (candidate['cost'], candidate['id'])
        current = best.get(key)
        if current is None or rank < (current['cost'], current['id']):
            best[key] = candidate
    return list(best.values())


def _transformed_edges(edges):
    """Assign each edge ``scaled_cost * 2**m - id_bit``.

    Because every cost has at most six fractional digits, scaling by
    ``10**6`` is exact and integral.  The cost part of any unit difference
    dominates the whole id-bit part, so minimising the transformed sum first
    minimises exact total cost and then selects the lexicographically
    smallest sorted candidate-id list.  The largest id bit belongs to the
    smallest candidate id, which makes a larger bit mask the smaller list.
    """
    sized = sorted(edges, key=lambda row: row['id'])
    count = len(sized)
    total_bits = 1 << count
    rows = []
    for rank, edge in enumerate(sized):
        scaled = edge['cost'] * COST_SCALE
        if scaled.denominator != 1:
            raise ValueError('candidate cost precision is not representable')
        weight = scaled.numerator * total_bits - (1 << (count - 1 - rank))
        rows.append((edge, weight, 1 << (count - 1 - rank)))
    return rows


def _steiner(nodes, edges, terminals):
    """Minimum-weight tree connecting ``terminals`` (Dreyfus-Wagner).

    Returns the selected edge indexes.  Edge weights are the exact combined
    integer objective produced by :func:`_transformed_edges`.
    """
    terminal_count = len(terminals)
    if terminal_count <= 1:
        return set()
    full = (1 << terminal_count) - 1
    adjacency = [[] for _ in range(nodes)]
    for index, (edge, weight, _) in enumerate(edges):
        adjacency[edge['left']].append((edge['right'], weight, index))
        adjacency[edge['right']].append((edge['left'], weight, index))

    best = [[None] * nodes for _ in range(1 << terminal_count)]
    merge_from = [[None] * nodes for _ in range(1 << terminal_count)]
    via_edge = [[None] * nodes for _ in range(1 << terminal_count)]
    for bit, node in enumerate(terminals):
        best[1 << bit][node] = 0

    for mask in range(1, full + 1):
        row = best[mask]
        if mask & (mask - 1):
            sub = (mask - 1) & mask
            while sub:
                other = mask ^ sub
                if sub < other:
                    left_row, right_row = best[sub], best[other]
                    for node in range(nodes):
                        left, right = left_row[node], right_row[node]
                        if left is None or right is None:
                            continue
                        value = left + right
                        if row[node] is None or value < row[node]:
                            row[node] = value
                            merge_from[mask][node] = sub
                sub = (sub - 1) & mask

        heap = [(distance, node) for node, distance in enumerate(row)
                if distance is not None]
        heapq.heapify(heap)
        while heap:
            distance, node = heapq.heappop(heap)
            if distance != row[node]:
                continue
            for neighbor, weight, edge_index in adjacency[node]:
                candidate = distance + weight
                if row[neighbor] is None or candidate < row[neighbor]:
                    row[neighbor] = candidate
                    via_edge[mask][neighbor] = (node, edge_index)
                    heapq.heappush(heap, (candidate, neighbor))

    chosen_node, chosen_value = None, None
    for node in range(nodes):
        value = best[full][node]
        if value is not None and (chosen_value is None
                                  or value < chosen_value):
            chosen_node, chosen_value = node, value

    selected = set()
    stack = [(full, chosen_node)]
    while stack:
        mask, node = stack.pop()
        if mask & (mask - 1) == 0:
            if terminals[mask.bit_length() - 1] == node:
                continue
        step = via_edge[mask][node]
        if step is not None:
            previous, edge_index = step
            selected.add(edge_index)
            stack.append((mask, previous))
            continue
        sub = merge_from[mask][node]
        if sub is None:
            raise RuntimeError('incomplete plan reconstruction')
        stack.append((sub, node))
        stack.append((mask ^ sub, node))
    return selected


def _empty_target_rows(targets, points, component_of, anchored_components):
    fixed = {}
    for pid, elevation in points.items():
        if elevation is not None:
            fixed.setdefault(component_of[pid], []).append(pid)
    rows = []
    for pid in sorted(targets):
        component = component_of[pid]
        rows.append({'point': pid,
                     'benchmark': min(fixed[component]),
                     'already_anchored': component in anchored_components})
    return rows


def plan(data):
    """Validate a planning request and return a deterministic plan report."""
    fields(data, 'points observations target_points candidate_segments')
    points, point_order = _points(data['points'])
    observations = _observations(data['observations'], points)
    targets = _targets(data['target_points'], points)
    candidates = _candidates(data['candidate_segments'], points)

    component_of = _component_map(point_order, observations)
    anchored_components = {component_of[pid] for pid, elevation
                           in points.items() if elevation is not None}
    components = _components(points, component_of, targets)

    # Every distinct non-anchored component gets one Steiner node.  All
    # anchored components are contracted into node zero: a target only needs
    # to reach *some* benchmark, so the anchored side is a single root.
    node_of, next_node = {}, 1
    for pid in point_order:
        component = component_of[pid]
        if component not in anchored_components and component not in node_of:
            node_of[component] = next_node
            next_node += 1
    nodes = next_node

    edges = _best_candidate_edges(candidates, component_of, node_of,
                                  anchored_components)
    for edge in edges:
        left_comp = component_of[edge['from']]
        right_comp = component_of[edge['to']]
        edge['left'] = (0 if left_comp in anchored_components
                        else node_of[left_comp])
        edge['right'] = (0 if right_comp in anchored_components
                         else node_of[right_comp])
        if edge['left'] > edge['right']:
            edge['left'], edge['right'] = edge['right'], edge['left']

    target_components = sorted({component_of[pid] for pid in targets
                                if component_of[pid] not in anchored_components})
    target_nodes = sorted({node_of[component] for component in target_components})

    adjacency = [[] for _ in range(nodes)]
    for edge in edges:
        adjacency[edge['left']].append(edge['right'])
        adjacency[edge['right']].append(edge['left'])
    reachable = {0}
    pending = [0]
    while pending:
        node = pending.pop()
        for neighbor in adjacency[node]:
            if neighbor not in reachable:
                reachable.add(neighbor)
                pending.append(neighbor)
    unreachable = sorted(pid for pid in targets
                         if component_of[pid] not in anchored_components
                         and node_of[component_of[pid]] not in reachable)

    base = {'schema': REPORT_SCHEMA, 'components': components,
            'pending_collection': True, 'note': PLAN_NOTE}
    if unreachable:
        return {**base, 'feasible': False, 'total_cost': None,
                'segments': [], 'targets': [],
                'unreachable_targets': unreachable}

    if not target_nodes:
        return {**base, 'feasible': True, 'total_cost': '0',
                'segments': [], 'targets': _empty_target_rows(
                    targets, points, component_of, anchored_components),
                'unreachable_targets': []}

    edges = _transformed_edges(edges)
    terminals = [0] + target_nodes
    selected_indexes = _steiner(nodes, edges, terminals)
    selected = [edges[index][0] for index in sorted(selected_indexes)]
    selected.sort(key=lambda row: row['id'])

    # Rebuild actual point connectivity with the chosen future segments so
    # each target is reported against a benchmark it really reaches.
    final = _Components(point_order)
    for _, start, end in observations:
        final.union(start, end)
    for edge in selected:
        final.union(edge['from'], edge['to'])
    fixed_by_root = {}
    for pid in sorted(points):
        if points[pid] is not None:
            fixed_by_root.setdefault(final.find(pid), []).append(pid)
    target_rows = []
    for pid in sorted(targets):
        anchors = fixed_by_root.get(final.find(pid))
        if not anchors:
            raise RuntimeError('selected plan does not reach a benchmark')
        target_rows.append({'point': pid, 'benchmark': anchors[0],
                            'already_anchored': (
                                component_of[pid] in anchored_components)})

    total = sum((edge['cost'] for edge in selected), Fraction(0))
    return {**base, 'feasible': True, 'total_cost': str(total),
            'segments': [{'id': edge['id'], 'from': edge['from'],
                          'to': edge['to'], 'cost': str(edge['cost']),
                          'status': 'pending_collection'}
                         for edge in selected],
            'targets': target_rows, 'unreachable_targets': []}
