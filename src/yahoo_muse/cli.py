import argparse
import json
import sys

from .api import METHODS, invoke
from .config import MailError, setup


def main():
    parser = argparse.ArgumentParser(description='Yahoo Mail JSON tools. Pass a JSON object or - to read JSON from stdin.')
    parser.add_argument('command', choices=['setup', 'mcp', *METHODS])
    parser.add_argument('arguments', nargs='?', default='{}')
    args = parser.parse_args()
    try:
        if args.command == 'mcp':
            from .server import main as serve
            serve()
            return
        if args.command == 'setup':
            if not sys.stdin.isatty():
                raise MailError('Run setup in a local interactive terminal; do not supply credentials through agent messages.')
            result = setup()
        else:
            payload = sys.stdin.read(2 * 1024 * 1024 + 1) if args.arguments == '-' else args.arguments
            if len(payload) > 2 * 1024 * 1024:
                raise MailError('JSON input exceeds 2 MiB.')
            result = invoke(args.command, json.loads(payload))
        print(json.dumps(result, ensure_ascii=True))
    except (MailError, json.JSONDecodeError) as exc:
        message = str(exc) if isinstance(exc, MailError) else 'Invalid JSON input.'
        print(json.dumps({'error': message}))
        sys.exit(1)
    except (OSError, KeyboardInterrupt):
        print(json.dumps({'error': 'Setup or local operation interrupted or failed.'}))
        sys.exit(1)


if __name__ == '__main__':
    main()
