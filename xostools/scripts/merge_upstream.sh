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

export ANDROID_BUILD_TOP="$TOP"
pushd $TOP

source build/envsetup.sh

if [ "$1" != "--no-reset" ]; then
  reporeset
  reposync fast
fi

echo "Generating temporary manifest file"
repo manifest > full-manifest.xml

while read path; do
  echo "$path"
  repo_name=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" full-manifest.xml)
  repo_upstream_full=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@upstream" full-manifest.xml)
  repo_upstream=$(echo "$repo_upstream_full" | cut -d '|' -f1)
  echo "Upstream: $repo_upstream"
  repo_upstream_rev=$(echo "$repo_upstream_full" | cut -d '|' -f2)
  repo_upstream_third=$(echo "$repo_upstream_full" | cut -d '|' -f3)
  is_tag=false
  if [ "$repo_upstream_rev" == "tag" ] && [ -n "$repo_upstream_third" ]; then
    echo "Using tag as upstream"
    is_tag=true
    repo_upstream_rev="$repo_upstream_third"
  fi
  echo "Upstream revision: $repo_upstream_rev"
  repo_remote=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" full-manifest.xml)
  echo "Remote: $repo_remote"
  repo_revision=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@revision" full-manifest.xml || :)
  if [ -z "$repo_revision" ]; then
    echo -n "(from remote definition) "
    repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$repo_remote']/@revision" full-manifest.xml || :)
  fi
  short_revision=${repo_revision/refs\/heads\//}
  echo "Revision: $repo_revision ($short_revision)"

  pushd $TOP/$path

  echo "Setting upstream remote"
  if ! git ls-remote upstream >/dev/null 2>/dev/null; then
    if ! git remote add upstream $repo_upstream; then
      git remote set-url upstream $repo_upstream
    fi
  else
    git remote set-url upstream $repo_upstream
  fi

  if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
    echo "Shallow repository detected, unshallowing first"
    git fetch --unshallow $repo_remote
  fi

  if [[ ${short_revision} != $(git branch --show-current) ]]; then
    git checkout $short_revision || (
      git fetch $repo_remote
      git checkout $repo_remote/$short_revision -b $short_revision
      git branch -u $repo_remote/$short_revision
    )
  fi

  echo "Fetching upstream"
  git fetch upstream
  echo "Merging upstream"
  # Check if it is a tag
  if $is_tag; then
    git merge $repo_upstream_rev
  else
    git merge upstream/$repo_upstream_rev
  fi

  if [ -f .lfsconfig ] || ( [ -f .gitattributes ] && grep -q 'merge=lfs' .gitattributes ); then
    unLFS
  fi

  git push XOS HEAD:$short_revision
  popd

  echo
done < <(xmlstarlet sel -t -v '/manifest/project[@upstream]/@path' full-manifest.xml)

echo "Deleting temporary manifest file"
rm -f full-manifest.xml

popd

echo "Everything done."
