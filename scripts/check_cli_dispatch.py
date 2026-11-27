#!/usr/bin/env python3
"""Verify help and invalid arguments never reach lifecycle handlers."""
import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mino_scout import cli


def main():
    commands = ('identity', 'status', 'stop', 'heavy-deps', 'run', 'start', 'probe')
    handlers = ('cmd_status', 'cmd_stop', 'cmd_heavy_deps', 'cmd_run', 'cmd_probe',
                'collect_status', 'try_start_service', 'build_core')
    with contextlib.ExitStack() as stack:
        for name in handlers:
            stack.enter_context(patch.object(cli, name, side_effect=AssertionError('lifecycle dispatch: ' + name)))
        for development in (False, True):
            stack.enter_context(patch('mino_scout.config.development', return_value=development))
            for command in commands:
                for args, expected in ((['--help'], 0), (['-h'], 0), (['--bogus'], 2), (['unexpected'], 2)):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        assert cli.main([command, *args]) == expected, (development, command, args)
    with patch.object(cli, 'cmd_stop', return_value=0) as stop:
        with patch('mino_scout.config.development', return_value=False):
            assert cli.main(['stop']) == 0
        stop.assert_called_once_with()
    print('PASS: 56 development/production help and invalid-argument checks; plain stop dispatch preserved; no real service touched')


if __name__ == '__main__':
    main()
