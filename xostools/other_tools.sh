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

mirrorAll() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/mirror_all.sh" $@
}

