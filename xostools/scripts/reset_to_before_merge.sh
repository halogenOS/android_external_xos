#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

if [[ "$1" == "--help" ]]; then
    echo "<dir> <substring>"
    exit 0
fi

DIR_TO_SHOW="$1"
SEARCH_STRING="$2"

cd $TOP

REMOTE_NAME="XOS"

limit_string_with_ellipsis() { local s="$1" m="$2"; echo "${s:0:$m}"$([ "${#s}" -gt "$m" ] && echo "..."); }
repo_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='$REMOTE_NAME']/@revision" $snippet | sed -re 's/^refs\/heads\/(.*)$/\1/')

while read path; do
    pushd $path >/dev/null
    if [ "$(realpath "$TOP/$DIR_TO_SHOW")" != "$(realpath "$TOP/$path")" ]; then
        popd >/dev/null
        continue
    fi

    if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
        echo "Shallow repository detected, unshallowing first"
        git fetch --unshallow
    else
        git fetch "$REMOTE_NAME"
    fi

    echo

    commit_hash=$(git log --grep="$SEARCH_STRING" --merges --pretty=format:'%H' -n 1)

    if [ -z "$commit_hash" ]; then
        echo "No merge commits found with the specified substring in the commit message."
        exit 1
    fi

    # Get the parents of the merge commit
    parents=($(git log --pretty=format:'%P' -n 1 $commit_hash))

    if [ ${#parents[@]} -lt 2 ]; then
        echo "The specified commit $commit_hash is not a valid merge commit."
        exit 1
    fi

    author=$(git log -1 --pretty=format:'%an' $commit_hash)
    author_email=$(git log -1 --pretty=format:'%ae' $commit_hash)
    date=$(git log -1 --pretty=format:'%ad' $commit_hash)
    commit_message=$(git log -1 --pretty=format:'%s' $commit_hash)

    ours_commit_hash=${parents[0]}
    theirs_commit_hash=${parents[1]}

    ours_author_and_email=$(git log -1 --pretty=format:'%an <%ae>' $ours_commit_hash)
    ours_commit_message=$(limit_string_with_ellipsis "$(git log -1 --pretty=format:'%s' $ours_commit_hash)" 72)

    theirs_author_and_email=$(git log -1 --pretty=format:'%an <%ae>' $theirs_commit_hash)
    theirs_commit_message=$(limit_string_with_ellipsis "$(git log -1 --pretty=format:'%s' $theirs_commit_hash)" 72)

    echo -e "Resetting \033[97;1m$path\033[0m to \033[97m$ours_commit_hash\033[0m \033[90m($ours_commit_message, $ours_author_and_email)\033[0m, before \033[97m$commit_message\033[0m \033[90m($commit_hash)\033[0m"
    git reset --hard $ours_commit_hash

    echo
    popd >/dev/null
    break
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

