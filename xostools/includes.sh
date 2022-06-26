#!/bin/bash

export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=false
export GCM_INTERACTIVE=never

export ROM_REVISION="XOS-12.1"

function getPlatformPath() {
  PWD="$(pwd)"
  original_string="$PWD"
  string_to_replace="$(realpath $ANDROID_BUILD_TOP)"
  result_string="${original_string//$string_to_replace}"
  echo -n "$result_string"
}

function getUnderscorePath() {
  local prefix="android"
  ppath="$(getPlatformPath)"
  if [ $# -ge 1 ]; then
    local prefix="$1"
  fi
  echo -n "${prefix}${ppath//\//_}"
}

createXos() {
  local project_to_create="$1"
  if [ -z "$project_to_create" ]; then
    local project_to_create="$(getUnderscorePath)"
  fi
  echo "Creating $project_to_create"
  curl \
    -H "Authorization: Bearer $(<"$HOME/.creds/xos_gitlab_token")" \
    -X POST \
    "https://git.halogenos.org/api/v4/projects?name=$project_to_create&namespace_id=108"
}
