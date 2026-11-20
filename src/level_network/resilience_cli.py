"""``level-resilience`` command line entry point."""
import argparse
from pathlib import Path
import sys

from .common import publish, read_json
from .resilience import review


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='level-resilience',
        description='单次观测失效韧性审查（精确平差）')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('source and output must differ')
        report = review(read_json(args.source))
        publish(args.output, report)
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print('wrote ' + str(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
