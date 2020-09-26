#!/bin/bash

mergeUpstream() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/merge_upstream.sh" $@
}

mergeAospUpstream() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/merge_aosp.sh" $@
}

createSnapshot() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/create_snapshot.sh" $@
}

pushAll() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/push_all.sh" $@
}

