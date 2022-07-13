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

source "build/envsetup.sh"

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

while read -r path; do
    echo "$path"
    repo_name_aosp=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" $aosp_snippet)
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
    git fetch aosp
    echo "Merging aosp"
    git merge "$revision"

    echo
    popd
done < <(xmlstarlet sel -t -v '/manifest/project[@merge-aosp="true"]/@path' "$snippet" && echo)

while read -r path; do
    pushd "$path"
    git push XOS HEAD:$ROM_VERSION
    popd
done < <(xmlstarlet sel -t -v '/manifest/project[@merge-aosp="true"]/@path' "$snippet" && echo)

echo "Everything done."

