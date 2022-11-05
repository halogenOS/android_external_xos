#!/bin/bash

export ROM_REVISION="XOS-13.0"

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

function addXos() {
  if git ls-remote xos >/dev/null 2>/dev/null; then
    git remote set-url xos git@git.halogenos.org:halogenOS/$(getUnderscorePath)
  else
    git remote add xos git@git.halogenos.org:halogenOS/$(getUnderscorePath)
  fi
  git fetch xos
}

function addXosGithub() {
  if git ls-remote xosgh >/dev/null 2>/dev/null; then
    git remote set-url xosgh git@github.com:halogenOS/$(getUnderscorePath)
  else
    git remote add xosgh git@github.com:halogenOS/$(getUnderscorePath)
  fi
  git fetch xosgh
}

function addAosp() {
  git remote remove aosp 2>/dev/null
  git remote add aosp https://android.googlesource.com/platform/$(getPlatformPath).git
  git fetch aosp
}

function addLOS() {
  git remote remove los 2>/dev/null
  local remote_domain="github.com"
  if [ $# -ge 1 ]; then
	  remote_domain="$1"
  fi
  local usp=$(getUnderscorePath)
  usp=${usp/build_make/build}
  git remote add los https://$remote_domain/LineageOS/${usp}.git
  git fetch los
}
