#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

if [[ "$1" == "--help" ]]; then
    echo "<substring>"
    echo "Tip: omitting the substring will use the latest merge, irrespective of what it is"
    exit 0
fi

SEARCH_STRING="$1"

cd $TOP

REMOTE_NAME="XOS"

repos_reset=""

limit_string_with_ellipsis() { local s="$1" m="$2"; echo "${s:0:$m}"$([ "${#s}" -gt "$m" ] && echo "..."); }
repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$REMOTE_NAME']/@revision" $snippet | sed -re 's/^refs\/heads\/(.*)$/\1/')

while read path; do
    if [ ! -d "$path" ]; then
        continue
    fi
    pushd "$path" >/dev/null

    if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
        popd >/dev/null
        continue
    fi

    commit_hash=$(git log --grep="$SEARCH_STRING" --merges --pretty=format:'%H' -n 1)

    if [ -z "$commit_hash" ]; then
        popd >/dev/null
        continue
    fi

    # Get the parents of the merge commit
    parents=($(git log --pretty=format:'%P' -n 1 $commit_hash))

    if [ ${#parents[@]} -lt 2 ]; then
        echo "The specified commit $commit_hash is not a valid merge commit."
        popd >/dev/null
        exit 1
    fi

    did_start=false
    for commit in $(git log --pretty=format:%h --no-merges ${parents[0]}..$commit_hash); do
        if [ -z "$(git diff-tree --quiet -r $commit)" ]; then
            # Empty commit
            continue
        fi
        if ! $did_start; then
            echo -e "\033[1m$path\033[0m"
            did_start=true
        fi
        short_hash=$(git log --pretty=format:%h -n 1 $commit)
        author=$(git log --pretty=format:%an -n 1 $commit)
        email=$(git log --pretty=format:%ae -n 1 $commit)
        subject=$(limit_string_with_ellipsis "$(git log --pretty=format:%s -n 1 $commit)" 72)
        author_email=$(limit_string_with_ellipsis "$author <$email>" 48)

        echo -e "\033[32m$short_hash\033[0m: \033[1m$subject\033[0m \033[90m($author_email)\033[0m"
    done

    if $did_start; then
        echo
    fi

    popd >/dev/null
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

