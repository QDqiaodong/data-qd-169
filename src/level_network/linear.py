"""Small dense exact rational linear solver, with explicit singularity rejection."""
from fractions import Fraction
import re


def rational(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?[0-9]{1,9}(?:\.[0-9]{1,6})?', value):
        raise ValueError('bounded decimal string required')
    return Fraction(value)


def solve(matrix, rhs):
    n = len(rhs)
    rows = [list(row) + [value] for row,value in zip(matrix,rhs)]
    for col in range(n):
        pivot = next((r for r in range(col,n) if rows[r][col]), None)
        if pivot is None:
            raise ValueError('network has unidentifiable datum')
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = rows[col][col]
        rows[col] = [x / scale for x in rows[col]]
        for r in range(n):
            if r != col and rows[r][col]:
                scale = rows[r][col]
                rows[r] = [a - scale*b for a,b in zip(rows[r],rows[col])]
    return [row[-1] for row in rows]
