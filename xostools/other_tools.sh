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

mirrorAll() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/mirror_all.sh" $@
}

