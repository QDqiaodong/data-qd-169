"""``level-fieldbook`` command line entry point."""
import argparse
from pathlib import Path
import sys

from .common import publish, read_json
from .fieldbook import fieldbook


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='level-fieldbook',
        description='离线手簿归卷：把混合测站记录还原到各条路线（精确分数）')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('source and output must differ')
        report = fieldbook(read_json(args.source))
        publish(args.output, report)
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError, ZeroDivisionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print('wrote ' + str(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
