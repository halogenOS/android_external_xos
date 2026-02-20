#!/bin/bash

createSnapshot() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/create_snapshot.sh" $@
}

deleteSnapshot() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/delete_snapshot.sh" $@
}

restoreSnapshot() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/restore_snapshot.sh" $@
}

restoreRepoSnapshot() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/restore_repo_snapshot.sh" $@
}

listRepoTags() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/list_repo_tags.sh" $@
}

findMergeCommit() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/find_merge_commit.sh" $@
}

resetToBeforeMerge() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/reset_to_before_merge.sh" $@
}

resetAllToBeforeMerge() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/reset_all_to_before_merge.sh" $@
}

showMergeDetails() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/show_merge_details.sh" $@
}

showRefDiff() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/show_ref_diff.sh" $@
}

generateRefDiff() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/generate_ref_diff.sh" $@
}

mirrorAll() {
    TOP="$(gettop)" nix run path:"$(gettop)/external/xos/xostools-ng#mirror-all" -- $@
}

detectRepoResets() {
    repo forall -c 'line=$(git reflog -1 2>/dev/null); if echo "$line" | grep -q "reset:"; then echo "$REPO_PATH: $(echo "$line" | sed "s/^[a-f0-9]* HEAD@{0}: //")"; fi' 2>/dev/null
}

generateMissingKeys() {
    TOP="$(gettop)" \
    KEYS_DIR="${KEYS_DIR:=vendor/halogenOS/private/keys}" \
    bash -i \
    "$(gettop)/external/xos/xostools/scripts/generate_missing_keys.sh" $@
}
