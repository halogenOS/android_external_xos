#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The environment under which Git fails an HTTPS authentication challenge
instead of asking anyone for credentials.

This module is the one definition of that environment for the Python tools
and the shell tools alike. A Python tool applies it to its own process. Run as
a program, the module writes each variable it changes in its own environment
as one NAME=VALUE record ended by a NUL byte, the form `env -0` prints, and a
shell tool exports each record as it is. It imports nothing outside the
standard library, so any Python interpreter can run it.
"""

import os
import sys
from typing import Dict, Mapping


def git_environment_without_credential_prompts(environ: Mapping[str, str]) -> Dict[str, str]:
    """Return `environ` with the variables that make Git fail an HTTPS
    authentication challenge instead of asking anyone for credentials.

    An empty credential.helper clears every configured credential helper,
    including the ones configured for a single URL, since Git reads the
    environment's configuration entries after its configuration files. It is
    appended after the entries that `environ` already carries. An empty
    GIT_ASKPASS ends Git's search for a password program before it reaches
    core.askPass or SSH_ASKPASS, and GIT_TERMINAL_PROMPT=0 stops the terminal
    prompt. SSH keeps every prompt of its own, for a host key as well as for a
    key passphrase or a password, so SSH_ASKPASS can still ask for them.

    Git reads an unset or empty GIT_CONFIG_COUNT as no entries. Any other value
    that is not plain ASCII decimal digits raises ValueError, so a count Git
    refuses never turns into one it accepts. A count above Git's own maximum
    stays above it, and Git refuses it on its first run.
    """
    count = environ.get('GIT_CONFIG_COUNT') or '0'
    if not (count.isascii() and count.isdigit()):
        raise ValueError(f"GIT_CONFIG_COUNT holds {count!r}, which is not a decimal count")
    index = int(count)
    return {
        **environ,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_ASKPASS': '',
        'GIT_CONFIG_COUNT': str(index + 1),
        f'GIT_CONFIG_KEY_{index}': 'credential.helper',
        f'GIT_CONFIG_VALUE_{index}': '',
    }


def switch_off_git_credential_prompts() -> None:
    """Make every Git process this process starts from now on fail an HTTPS
    authentication challenge instead of asking for credentials.

    Git, git lfs and repo inherit the process environment, so a tool that
    starts any of them calls this once, before it starts the first one.
    """
    os.environ.update(git_environment_without_credential_prompts(os.environ))


def changed_variable_records(environ: Mapping[str, str]) -> bytes:
    """Return the variables that turn `environ` into the environment without
    credential prompts as NAME=VALUE records, each ended by a NUL byte, in the
    form `env -0` prints. A record carries no shell syntax and no quoting."""
    changed = git_environment_without_credential_prompts(environ).items() - environ.items()
    return b''.join(os.fsencode(name) + b'=' + os.fsencode(value) + b'\0'
                    for name, value in sorted(changed))


if __name__ == '__main__':
    sys.stdout.buffer.write(changed_variable_records(os.environ))
