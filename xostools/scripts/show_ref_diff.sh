#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"
aosp_snippet="$TOP/.repo/manifests/default.xml"


if [[ "$1" == "--help" ]]; then
    echo "<from> <to>"
    exit 0
fi

FROM_REF="$1"
TO_REF="$2"

cd $TOP

REMOTE_NAME="XOS"

repos_reset=""

limit_string_with_ellipsis() { local s="$1" m="$2"; echo "${s:0:$m}"$([ "${#s}" -gt "$m" ] && echo "..."); }

all_paths=$(
    (
        xmlstarlet sel -t -v "/manifest/project[@merge-aosp='true']/@path" $snippet && echo && \
        xmlstarlet sel -t -v "/manifest/project/@path" $aosp_snippet
    ) | sort -u
)

for path in $all_paths; do
    if [ ! -d "$path" ]; then
        continue
    fi
    pushd "$path" >/dev/null

    if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
        popd >/dev/null
        continue
    fi

    from_commit_hash=$(git rev-parse --verify $FROM_REF >/dev/null 2>&1 && git rev-parse $FROM_REF || echo "")
    to_commit_hash=$(git rev-parse --verify $TO_REF >/dev/null 2>&1 && git rev-parse $TO_REF || echo "")

    if [ -z "$from_commit_hash" ] || [ -z "$to_commit_hash" ]; then
        if [ "$SKIP_AOSP_FETCH" != "true" ] && ( [[ "$FROM_REF" == android-* ]] || [[ "$TO_REF" == android-* ]] ); then
            echo "Fetching AOSP for $path…"
            git remote get-url --all aosp >/dev/null 2>&1 || (
                repo_name_aosp=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" $aosp_snippet || echo "platform/$path")
                repo_aosp="$(xmlstarlet sel -t -v "/manifest/remote[@name='aosp']/@fetch" $aosp_snippet || :)/$repo_name_aosp"
                if [ -n "$repo_aosp" ]; then
                    git remote add aosp "$repo_aosp"
                else
                    git remote add aosp https://android.googlesource.com/platform/$path.git
                fi
            )
            if [[ "$FROM_REF" == android-* ]] && [[ "$TO_REF" == android-* ]]; then
                git fetch --no-tags aosp +refs/tags/$FROM_REF:refs/tags/$FROM_REF +refs/tags/$TO_REF:refs/tags/$TO_REF || :
            elif [[ "$FROM_REF" == android-* ]]; then
                git fetch --no-tags aosp +refs/tags/$FROM_REF:refs/tags/$FROM_REF || :
            elif [[ "$TO_REF" == android-* ]]; then
                git fetch --no-tags aosp +/refs/tags$TO_REF:refs/tags/$TO_REF || :
            fi
            from_commit_hash=$(git rev-parse --verify $FROM_REF >/dev/null 2>&1 && git rev-parse $FROM_REF || echo "")
            to_commit_hash=$(git rev-parse --verify $TO_REF >/dev/null 2>&1 && git rev-parse $TO_REF || echo "")
            if [ -z "$from_commit_hash" ] || [ -z "$to_commit_hash" ]; then
                popd >/dev/null
                continue
            fi
        else
            popd >/dev/null
            continue
        fi
    fi
    did_start=false
    for commit in $(git log --pretty=format:%h --no-merges $from_commit_hash..$to_commit_hash); do
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
done

