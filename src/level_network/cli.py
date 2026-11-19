import argparse
from pathlib import Path
import sys
from .common import read_json, publish
from .adjustment import adjust


def main():
    parser = argparse.ArgumentParser(description='水准测量网平差')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('source and output must differ')
        report = adjust(read_json(args.source))
        publish(args.output, report)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print('wrote ' + str(args.output))
    return 0
