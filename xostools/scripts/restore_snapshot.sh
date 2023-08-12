#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

if [[ "$1" == "--help" ]]; then
    echo "Specify the tag you want to restore to as argument"
    exit 0
fi

TAG_TO_RESTORE="$1"

source build/envsetup.sh

cd $TOP

REMOTE_NAME="XOS"

repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$REMOTE_NAME']/@revision" $snippet | sed -re 's/^refs\/heads\/(.*)$/\1/')

while read path; do
    echo "$path"
    pushd $path

    if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
        echo "Shallow repository detected, unshallowing first"
        git fetch --unshallow
    else
        git fetch "$REMOTE_NAME"
    fi

    if [ ! git rev-parse $TAG_TO_RESTORE 2>/dev/null 1>&2 ]; then
        echo "Invalid tag $TAG_TO_RESTORE"
        echo "Tag suggestions for $path (non-exhaustive):"
        git tag -l "${repo_revision}-*" | sort -n | tail -n10
    fi

    git push "$REMOTE_NAME" "$TAG_TO_RESTORE":"${repo_revision}"

    echo
    popd
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

echo "Everything done."

