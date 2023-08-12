#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

if [[ "$1" == "--help" ]]; then
    echo "Specify the tag you want to delete as argument"
    exit 0
fi

TAG_TO_DELETE="$1"

source build/envsetup.sh

cd $TOP

REMOTE_NAME="XOS"

repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$REMOTE_NAME']/@revision" $snippet | sed -re 's/^refs\/heads\/(.*)$/\1/')

while read path; do
    echo "$path"
    pushd $path

    if ! git rev-parse $TAG_TO_DELETE 2>/dev/null 1>&2; then
        echo "Invalid tag $TAG_TO_DELETE for $path, skipping"
        popd
        continue
    fi

    git push "$REMOTE_NAME" :"$TAG_TO_DELETE" || \
        echo "Note: Failed to delete tag for $path, assuming remote doesn't have it."

    git tag -d "$TAG_TO_DELETE" || :

    echo
    popd
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

echo "Everything done."

