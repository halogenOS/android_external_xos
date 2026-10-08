#!/bin/bash
# xostools-ng/git_credential_prompts.py defines the Git environment without
# credential prompts for the shell tools and the Python tools alike. It writes
# each variable it changes as one NAME=VALUE record ended by a NUL byte.
mapfile -d '' -t git_prompt_settings < <(python3 "$(dirname "${BASH_SOURCE[0]}")/../xostools-ng/git_credential_prompts.py")
wait $! || return
for git_prompt_setting in "${git_prompt_settings[@]}"; do
    export -- "$git_prompt_setting"
done
unset git_prompt_settings git_prompt_setting
