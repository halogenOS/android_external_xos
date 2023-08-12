#!/bin/bash

set -e

source build/envsetup.sh

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

    if [ "$(git rev-parse --is-shallow-repository)" != "true" ]; then
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

    echo
    echo -e "\033[1m$commit_message\033[0m"
    echo "Date: $date"
    echo "Author: $author <$author_email>"

    echo "Merge Commit Hash: $commit_hash"
    echo "Parent 1 (ours): $ours_commit_hash ($ours_commit_message, $ours_author_and_email)"
    echo "Parent 2 (theirs): $theirs_commit_hash ($theirs_commit_message, $theirs_author_and_email)"

    echo
    their_tags=$(git tag --points-at $theirs_commit_hash)
    if [ -z "$their_tags" ]; then
        # didn't find a tag, fetch aosp just in case
        echo "Fetching aosp to find tag…"
        addAosp
        echo
    fi
    echo "Parent 2 (theirs) tagged by:"
    echo "$their_tags"

    echo "Parent 1 (ours) has lineage from:"
    git tag --list --merged $ours_commit_hash --sort=-taggerdate | grep -E '^android-[0-9]+[.][0-9]+[.][0-9]+_r[0-9]+' | head -n3
    echo "[…]"
    echo "Parent 2 (theirs) has lineage from:"
    git tag --list --merged $theirs_commit_hash --sort=-taggerdate | grep -E '^android-[0-9]+[.][0-9]+[.][0-9]+_r[0-9]+' | head -n3
    echo "[…]"

    echo
    popd >/dev/null
    break
done < <(xmlstarlet sel -t -v "/manifest/project[@remote='$REMOTE_NAME']/@path" $snippet)

