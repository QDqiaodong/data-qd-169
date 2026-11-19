"""Weighted least squares for oriented height differences, with fixed benchmarks."""
from fractions import Fraction
from .common import fields, text, array
from .linear import rational, solve


def adjust(data):
    fields(data, 'points observations')
    fixed, unknown, all_points = {}, [], set()
    for row in array(data['points'],60):
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
    indexes = {pid:i for i,pid in enumerate(unknown)}
    normal = [[Fraction(0) for _ in unknown] for _ in unknown]
    rhs = [Fraction(0) for _ in unknown]
    observations, ids = [], set()
    for row in array(data['observations'],2000):
        fields(row, 'id from to delta weight')
        oid, start, end = text(row['id']), text(row['from']), text(row['to'])
        if oid in ids or start not in all_points or end not in all_points or start == end:
            raise ValueError('invalid observation identity or endpoints')
        ids.add(oid)
        delta, weight = rational(row['delta']), rational(row['weight'])
        if weight <= 0:
            raise ValueError('weight must be positive')
        coefficients = {}
        constant = Fraction(0)
        for pid, sign in ((start,-1),(end,1)):
            if pid in fixed:
                constant += sign * fixed[pid]
            else:
                coefficients[indexes[pid]] = Fraction(sign)
        target = delta - constant
        for i, ai in coefficients.items():
            rhs[i] += weight * ai * target
            for j, aj in coefficients.items():
                normal[i][j] += weight * ai * aj
        observations.append((oid,start,end,delta,weight))
    if not observations:
        raise ValueError('observations required')
    solution = solve(normal,rhs)
    heights = {**fixed, **dict(zip(unknown,solution))}
    residuals, objective = [], Fraction(0)
    gradients = {pid:Fraction(0) for pid in unknown}
    for oid,start,end,delta,weight in sorted(observations):
        predicted = heights[end]-heights[start]
        residual = predicted-delta
        objective += weight*residual*residual
        residuals.append({'id':oid,'adjusted_delta':str(predicted),'residual':str(residual)})
        if start in gradients:
            gradients[start] -= weight*residual
        if end in gradients:
            gradients[end] += weight*residual
    return {'elevations':{pid:str(heights[pid]) for pid in sorted(heights)},
            'residuals':residuals,'weighted_squared_error':str(objective),
            'normal_residual':{pid:str(value) for pid,value in gradients.items()},
            'degrees_of_freedom':len(observations)-len(unknown)}
