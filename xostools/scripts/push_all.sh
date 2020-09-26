#!/bin/bash

set -e

cd $TOP

snippet="$TOP/.repo/manifests/snippets/XOS.xml"

source build/envsetup.sh

xos_revision=$(xmlstarlet sel -t -v "/manifest/remote[@name='XOS']/@revision" $snippet)

cd $TOP

while read path; do
    echo "$path"
    repo_path="$path"
    repo_name=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@name" $snippet)
    repo_xos="ssh://git@git.halogenos.org/halogenOS/$repo_name"
    echo "Revision to push: $xos_revision"
    repo_remote=$(xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" $snippet)

    pushd $path

    echo "Setting XOS remote"
    if ! git ls-remote xos >/dev/null 2>/dev/null; then
        git remote add xos $repo_xos || git remote set-url xos $repo_xos
    else
        git remote set-url xos $repo_xos
    fi

    if [ "$(git rev-parse --is-shallow-repository)" == "true" ]; then
        echo "Shallow repository detected, unshallowing first"
        git fetch --unshallow $repo_remote
    fi

    echo "Pushing..."
    git push xos HEAD:$xos_revision

    echo
    popd
done < <(xmlstarlet sel -t -v '/manifest/project[@remote="XOS"]/@path' $snippet)


echo "Everything done."

