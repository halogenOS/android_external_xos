#!/bin/bash

set -e

pushd "$TOP"
export ANDROID_BUILD_TOP="$TOP"

source build/envsetup.sh
source external/xos/xostools/disable_git_prompts.sh
source external/xos/xostools/includes.sh

if [ -z "$ROM_REVISION" ]; then
  ROM_REVISION="$ROM_VERSION"
fi

echo "Generating temporary manifest file"
repo manifest > full-manifest.xml
echo "Generating repository list"
typeset -a list=( $(xmlstarlet sel -t -v '/manifest/project[@upstream]/@path' full-manifest.xml) )

for path in ${list[@]}; do
  echo
  echo "$path"
  repo_upstream_full=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@upstream" full-manifest.xml)
  repo_upstream=$(echo "$repo_upstream_full" | cut -d '|' -f1)
  echo "Upstream: $repo_upstream"

  pushd $path

  echo "Setting upstream remote"
  git remote add upstream $repo_upstream || git remote set-url upstream $repo_upstream
  echo "Fetching upstream"
  git fetch upstream

  popd
done

echo
echo "Deleting temporary manifest file"
rm -f full-manifest.xml

popd

echo "Everything done."
