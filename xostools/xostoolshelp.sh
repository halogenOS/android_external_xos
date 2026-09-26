#!/bin/bash

#
# Copyright (C) 2016-2026 The halogenOS Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


function xostools_help_build() {

cat <<EOF
Usage: build <target> [lunch target] [module] [noclean]

Targets:
    full        Full ROM. Builds otapackage and names the OTA package
                <custom version>.zip, with a sha256sum file beside it.
                If KEYS_DIR is an absolute path, builds only the
                target-files directory and the OTA tools instead, signs
                the target-files directory in place with the release keys
                and names the signed OTA package the same way.
                SDK phone (emulator) targets build the default goal and
                make no package, signed or not.
    sign        Re-sign the latest build with release keys (requires lunch)
    module      Build only a specific module
    module-list Build multiple modules
    mm          Builds using mmma. Useful for frameworks or modules
                which you want to build using mmma/mmm/

noclean: use this option to skip cleaning before building
         this is not accepted on module-list

You have to specify the lunch target if you haven't lunched yet.
EOF

}

function xostools_build_no_target_device() {

cat <<EOF
No target device specified and \$TARGET_DEVICE is
undefined
EOF

}

function xostools_help_reporesync() {

cat <<EOF
Usage: reporesync <option> [repository path] [repository name] [low]

Options:
    full            Full resync: delete the whole source tree, do a sync and
                    fully resync local tree

Example: reporesync packages/apps/Settings android_packages_apps_Settings
            This removes packages/apps/Settings and the repository including
            object files and project files.

         reporesync full
            This does a sync and resyncs the local work tree. Deletes everything
            except .repo and does a sync.

EOF

}

return 0
