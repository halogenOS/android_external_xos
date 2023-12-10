#!/bin/bash

set -e

cd "$TOP"
export ANDROID_BUILD_TOP="$TOP"

snippet="$TOP/.repo/manifests/snippets/XOS.xml"
aosp_snippet="$TOP/.repo/manifests/default.xml"

if [[ "$2" == "-"* ]]; then
    echo "Specify positional arguments after options, for example --no-reset android-9.0.0_r45"
    exit 2
fi

source build/envsetup.sh
source external/xos/xostools/disable_git_prompts.sh
source external/xos/xostools/includes.sh

if [ "$1" != "--no-reset" ]; then
    echo "Warning: This will perform a reporeset and a reposync to make sure everything is up to date before doing the merges"
    echo "If you do not want that to happen, abort now using CTRL+C and use the parameter --no-reset"
    echo "Otherwise, just confirm with ENTER"
    read -r
    echo
    reporeset
    reposync fast
else
    shift
fi

if [ -z "$1" ]; then
    echo "Please specify a tag to merge, e. g. android-9.0.0_r45"
    exit 1
fi

revision="$1"
typeset -a list=( $(xmlstarlet sel -t -v '/manifest/project[@merge-aosp="true"]/@path' "$snippet" && echo) )
for path in ${list[@]}; do
    echo "$path"
    repo_name_aosp=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" $aosp_snippet || echo "platform/$path")
    repo_aosp="$(xmlstarlet sel -t -v "/manifest/remote[@name='aosp']/@fetch" $aosp_snippet)/$repo_name_aosp"
    echo "AOSP remote: $repo_aosp"
    echo "Revision to merge: $revision"
    repo_remote=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" "$snippet")

    pushd "$path"

    if [[ ${ROM_VERSION} != $(git branch --show-current) ]]; then
        repo checkout $ROM_VERSION || repo start $ROM_VERSION
    fi

    echo "Setting aosp remote"
    if ! git ls-remote aosp >/dev/null 2>/dev/null; then
        git remote add aosp "$repo_aosp" || git remote set-url aosp "$repo_aosp"
    else
        git remote set-url aosp "$repo_aosp"
    fi

    if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
        echo "Shallow repository detected, unshallowing first"
        git fetch --unshallow "$repo_remote"
    fi

    echo "Fetching aosp"
    # google servers lalalalalalala missing stuff whatever skibidi toilet
    git fetch --refetch aosp
    echo "Merging aosp"
    git merge "$revision"

    echo
    popd
done

for path in ${list[@]}; do
    pushd "$path"
    repo_remote=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" "$snippet")
    repo_revision=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@revision" "$snippet" || :)
    if [ -z "$repo_revision" ]; then
        echo -n "(from remote definition) "
        repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$repo_remote']/@revision" "$snippet" || :)
    fi
    short_revision=${repo_revision/refs\/heads\//}
    echo "Revision: $repo_revision ($short_revision)"
    git push XOS HEAD:$short_revision $SCRIPT_PUSH_ARGS
    popd
done

echo "Everything done."

