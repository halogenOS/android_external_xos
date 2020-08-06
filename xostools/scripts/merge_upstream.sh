#!/bin/bash

set -e

export GIT_TERMINAL_PROMPT=0

if [ "$1" != "--no-reset" ]; then
  echo "Warning: This will perform a reporeset and a reposync to make sure everything is up to date before doing the merges"
  echo "If you do not want that to happen, abort now using CTRL+C and use the parameter --no-reset"
  echo "Otherwise, just confirm with ENTER"
  read
  echo
fi

pushd $TOP

source build/envsetup.sh

if [ "$1" != "--no-reset" ]; then
  reporeset
  reposync fast
fi

echo "Generating temporary manifest file"
repo manifest > full-manifest.xml

xos_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='XOS']/@revision" full-manifest.xml)

while read path; do
  echo "$path"
  repo_path="$path"
  repo_upstream_full=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@upstream" full-manifest.xml)
  repo_upstream=$(echo "$repo_upstream_full" | cut -d '|' -f1)
  repo_xos="ssh://git@git.halogenos.org/halogenOS/$repo_name"
  echo "Upstream: $repo_upstream"
  repo_upstream_rev=$(echo "$repo_upstream_full" | cut -d '|' -f2)
  echo "Upstream revision: $repo_upstream_rev"
  repo_remote=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" full-manifest.xml)

  pushd $TOP/$path

  echo "Setting upstream remote"
  if ! git ls-remote upstream >/dev/null 2>/dev/null; then
    if ! git remote add upstream $repo_upstream; then
      git remote set-url upstream $repo_upstream
    fi
  else
    git remote set-url upstream $repo_upstream
  fi

  echo "Setting XOS remote"
  if ! git ls-remote xos >/dev/null 2>/dev/null; then
      git remote add xos $repo_xos || git remote set-url xos $repo_xos
  else
      git remote set-url xos $repo_xos
  fi

  if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
    echo "Shallow repository detected, unshallowing first"
    git fetch --unshallow $repo_remote
  fi

  echo "Fetching upstream"
  git fetch upstream
  echo "Merging upstream"
  git merge upstream/$repo_upstream_rev

  git push xos HEAD:$xos_revision
  popd

  echo
done < <(xmlstarlet sel -t -v '/manifest/project[@upstream]/@path' full-manifest.xml)

echo "Deleting temporary manifest file"
rm -f full-manifest.xml

popd

echo "Everything done."
