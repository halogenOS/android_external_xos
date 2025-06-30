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

has_createxos=true
if ! type createXos >/dev/null 2>/dev/null; then
  echo -e "\033[1mNote: createXos not found, repositories won't be created if missing! \033[0m"
  has_createxos=false
fi

snippet="$TOP/.repo/manifests/snippets/XOS.xml"
aosp_snippet="$TOP/.repo/manifests/default.xml"

echo "Generating temporary manifest file"
repo manifest > full-manifest.xml
echo "Generating repository list"
if [ -z "$1" ]; then
  typeset -a list=(
    $(xmlstarlet sel -t -v '/manifest/project[@merge-aosp]/@path' "$snippet")
    $(xmlstarlet sel -t -v '/manifest/project[@upstream]/@path' full-manifest.xml)
  )
else
  typeset -a list=( $1 )
fi

for path in ${list[@]}; do
  echo
  echo "$path"
  repo_path="$path"
  repo_name=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" full-manifest.xml)
  merge_aosp=$(xmlstarlet sel -t -v  "/manifest/project[@path='$path']/@merge-aosp" "$snippet" || :)
  if [[ $merge_aosp == true ]]; then
    echo "Detected AOSP repo"
    aosp_path=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" "$aosp_snippet" || echo "platform/$path")
    repo_upstream="https://android.googlesource.com/$aosp_path"
    repo_upstream_rev=$(
      (
        xmlstarlet sel -t -v "/manifest/remote[@name='aosp']/@revision" "$aosp_snippet" || \
        xmlstarlet sel -t -v "/manifest/default[@remote='aosp']/@revision" "$aosp_snippet"
      ) | sed -re 's/^refs\/heads\/(.*)$/\1/'
    )
    echo "AOSP upstream: $repo_upstream"
    if [ -z "$repo_upstream_rev" ]; then
      echo "Unable to determine AOSP upstream revision"
      exit 1
    fi
  else
    repo_upstream_full=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@upstream" full-manifest.xml)
    if grep -q '|' <<<"$repo_upstream_full"; then
      repo_upstream=$(echo "$repo_upstream_full" | cut -d '|' -f1 | cut -d '#' -f1)
      echo "Upstream: $repo_upstream"
      repo_upstream_rev=$(echo "$repo_upstream_full" | cut -d '|' -f2 | cut -d '#' -f2)
      repo_upstream_third=$(echo "$repo_upstream_full" | cut -d '|' -f3 | cut -d '#' -f3)
      is_tag=false
      if [ "$repo_upstream_rev" == "tag" ] && [ -n "$repo_upstream_third" ]; then
        echo "Using tag as upstream"
        is_tag=true
        repo_upstream_rev="$repo_upstream_third"
      fi
    else
      # our own branch
      repo_upstream_rev="$repo_upstream_full"
      repo_upstream="https://git.halogenos.org/halogenOS/$repo_name"
      echo "Our upstream $repo_upstream with rev $repo_upstream_rev"
      is_tag=false
    fi
  fi
  echo "Upstream revision: $repo_upstream_rev"
  repo_remote=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" full-manifest.xml || :)
  echo "Remote: $repo_remote"
  repo_revision=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@revision" full-manifest.xml || :)
  if [ -z "$repo_revision" ]; then
    echo -n "(from remote definition) "
    repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$repo_remote']/@revision" full-manifest.xml || :)
  fi
  short_revision=${repo_revision/refs\/heads\//}
  echo "Revision: $repo_revision ($short_revision)"

  if [ -z "$repo_revision" ]; then
    if [ -z "$ROM_REVISION" ]; then
      echo -e "\033[1mWarning: unable to determine revision and ROM_REVISION or ROM_VERSION not set, skipping! \033[0m"
      popd
      continue
    else
      echo -e "\033[1mWarning: unable to determine revision, defaulting to $ROM_REVISION \033[0m"
      repo_revision="$ROM_REVISION"
    fi
  fi

  mkdir -p $path
  pushd $path

  if [ ! -d .git ]; then
    echo "Initializing git repository"
    git init
  fi

  if ! git ls-remote XOS >/dev/null 2>/dev/null; then
    git remote add XOS https://git.halogenos.org/halogenOS/$repo_name ||
      git remote set-url XOS https://git.halogenos.org/halogenOS/$repo_name
    git remote set-url --push XOS git@git.halogenos.org:halogenOS/$repo_name
  fi

  if [[ $(git ls-remote XOS "${repo_revision}" 2>/dev/null | wc -l) -gt 0 ]]; then
    echob "Skipping $repo_path, ref $repo_revision already exists"
    popd
    continue
  fi

  git remote set-url --push XOS git@git.halogenos.org:halogenOS/$repo_name

  echo "Setting upstream remote"
  git remote add upstream $repo_upstream || git remote set-url upstream $repo_upstream
  echo "Fetching upstream"
  git fetch upstream
  echo "Fetching XOS"
  git fetch XOS || :

  if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
    echo "Shallow repository detected, unshallowing first"
    git fetch --unshallow
  fi

  echo "Checking out $repo_upstream_rev -> $short_revision"
  if $is_tag; then
    git checkout $repo_upstream_rev -B $short_revision
  else
    git checkout upstream/$repo_upstream_rev -B $short_revision
  fi
  $has_createxos && echo "Creating repository (if it doesn't exist)" && createXos || :

  if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
    echo "Shallow branch detected, unshallowing first"
    git fetch --unshallow
  fi

  if [ -f .lfsconfig ] || ( [ -f .gitattributes ] && grep -q 'merge=lfs' .gitattributes ); then
    unLFS
  fi

  if [[ ${FORCE_PUSHES} == true ]]; then
    git push XOS HEAD:$repo_revision -f
  else
    git push XOS HEAD:$repo_revision
  fi

  popd

done

echo
echo "Deleting temporary manifest file"
rm -f full-manifest.xml

popd

echo "Everything done."
