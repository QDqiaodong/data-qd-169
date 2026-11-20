"""``level-staff`` command line entry point.

Two offline subcommands implement the calibration handover: ``calibrate``
exports the staff-only profile quadratic, and ``deliver`` combines a delivery
survey with a previously generated package without ever reading the original
calibration field data.
"""
import argparse
from pathlib import Path
import sys

from .common import publish, read_json
from .staff import calibrate, deliver


def _distinct(paths):
    resolved = set()
    for path in paths:
        candidate = path.resolve()
        if candidate in resolved:
            raise ValueError('input and output paths must all differ')
        resolved.add(candidate)


def _calibrate(args):
    _distinct((args.source, args.output))
    package = calibrate(read_json(args.source))
    publish(args.output, package)
    return 'wrote ' + str(args.output)


def _deliver(args):
    _distinct((args.source, args.calibration, args.output))
    report = deliver(read_json(args.source), read_json(args.calibration))
    publish(args.output, report)
    return 'wrote ' + str(args.output)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='level-staff', description='水准尺零差校准交接（离线，精确分数）')
    commands = parser.add_subparsers(dest='command', required=True)

    calibrate_parser = commands.add_parser(
        'calibrate', help='把校准场数据归约为尺零差轮廓代价包')
    calibrate_parser.add_argument('--source', type=Path, required=True)
    calibrate_parser.add_argument('--output', type=Path, required=True)
    calibrate_parser.set_defaults(handler=_calibrate)

    deliver_parser = commands.add_parser(
        'deliver', help='用校准包联合平差交付测网并发布唯一查询值')
    deliver_parser.add_argument('--source', type=Path, required=True)
    deliver_parser.add_argument('--calibration', type=Path, required=True)
    deliver_parser.add_argument('--output', type=Path, required=True)
    deliver_parser.set_defaults(handler=_deliver)

    args = parser.parse_args(argv)
    try:
        message = args.handler(args)
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError, ZeroDivisionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(message)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
