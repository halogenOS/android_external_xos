#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

if [[ "$1" == "--help" ]]; then
    echo "<dir> [-a]"
    exit 0
fi

DIR_TO_SHOW="$1"
SHOW_ALL=$([ "$2" == "-a" ] && echo "true" || echo "false")

cd $TOP

REMOTE_NAME="XOS"

repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$REMOTE_NAME']/@revision" $snippet | sed -re 's/^refs\/heads\/(.*)$/\1/')

while read path; do
    pushd $path >/dev/null
    if [ "$(realpath "$TOP/$DIR_TO_SHOW")" != "$(realpath "$TOP/$path")" ]; then
        popd >/dev/null
        continue
    fi

    if [ "$(git rev-parse --is-shallow-repository)" != "true" ]; then
        git fetch "$REMOTE_NAME"
    fi

    echo
    if $SHOW_ALL; then
        echo -e "\033[1mTags for $path\033[0m"
        git tag | sort -n
    else
        echo -e "\033[1mSuggested tags for restoring $path:\033[0m"
        git tag -l "${repo_revision}-*" | sort -n | tail -n15
    fi

    echo
    popd >/dev/null
    break
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

