#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

if [[ "$1" == "--help" ]]; then
    echo "<dir> <tag>"
    exit 0
fi

DIR_TO_RESTORE="$1"
TAG_TO_RESTORE="$2"

source build/envsetup.sh

cd $TOP

REMOTE_NAME="XOS"

repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$REMOTE_NAME']/@revision" $snippet | sed -re 's/^refs\/heads\/(.*)$/\1/')

while read path; do
    pushd $path >/dev/null
    if [ "$(realpath "$TOP/$DIR_TO_RESTORE")" != "$(realpath "$TOP/$path")"]; then
        popd >/dev/null
        continue
    fi

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

    echo "Previous revision: $(git rev-parse "$REMOTE_NAME/$repo_revision")"
    echo "New revision: $(git rev-parse "$TAG_TO_RESTORE")"
    git push "$REMOTE_NAME" "$TAG_TO_RESTORE":"${repo_revision}"

    echo "Successfully restored $REMOTE_NAME/$repo_revision to $TAG_TO_RESTORE"

    echo
    popd >/dev/null
    break
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

