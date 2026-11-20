"""``level-monitor`` command line entry point."""
import argparse
from pathlib import Path
import sys

from .common import publish, read_json
from .monitor import (apply_request, build_report, initialize, load_state,
                      save_state)


def _same(left, right):
    return Path(left).resolve() == Path(right).resolve()


def _init(args):
    if _same(args.state, args.source):
        raise ValueError('state and source must differ')
    if Path(args.state).exists():
        raise ValueError('state already exists')
    state = initialize(read_json(args.source))
    save_state(args.state, state)
    return 'wrote {0} (sequence {1})'.format(args.state, state['last_sequence'])


def _apply(args):
    if _same(args.state, args.source):
        raise ValueError('state and source must differ')
    state = load_state(args.state)
    updated, sequence, duplicate = apply_request(state, read_json(args.source))
    if duplicate:
        return 'unchanged {0} (sequence {1})'.format(args.state, sequence)
    save_state(args.state, updated)
    return 'wrote {0} (sequence {1})'.format(args.state, sequence)


def _report(args):
    if args.as_of is not None and args.as_of < 1:
        raise ValueError('as-of must be a positive sequence')
    state = load_state(args.state)
    if _same(args.state, args.output):
        raise ValueError('state and output must differ')
    report = build_report(state, args.as_of)
    publish(args.output, report)
    return 'wrote {0} (as-of {1})'.format(args.output, report['as_of'])


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='level-monitor', description='水准测次变形告警工作台')
    commands = parser.add_subparsers(dest='command', required=True)

    init = commands.add_parser('init', help='初始化参考测次与固定配置')
    init.add_argument('--state', type=Path, required=True)
    init.add_argument('--source', type=Path, required=True)
    init.set_defaults(handler=_init)

    apply = commands.add_parser('apply', help='应用一个测次修订批次')
    apply.add_argument('--state', type=Path, required=True)
    apply.add_argument('--source', type=Path, required=True)
    apply.set_defaults(handler=_apply)

    report = commands.add_parser('report', help='导出指定成功 sequence 的报告')
    report.add_argument('--state', type=Path, required=True)
    report.add_argument('--output', type=Path, required=True)
    report.add_argument('--as-of', type=int, default=None)
    report.set_defaults(handler=_report)

    args = parser.parse_args(argv)
    try:
        message = args.handler(args)
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(message)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
