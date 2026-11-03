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
    full        Full ROM: the regular build of the installed files and
                of the steps of otapackage that the signing run does
                not run, then the signing run of otapackage, which
                gives the signed OTA package its release name,
                <custom version>.zip, with a sha256sum file beside it.
                SDK phone (emulator) targets sign the default goal and
                make no package.
    sign        Only the signing run of otapackage (requires lunch)
    module      Build only the named module, without a signing run.
                Needs a module name and makes no package.
    module-list Build multiple modules
    mm          Builds using mmma. Useful for frameworks or modules
                which you want to build using mmma/mmm/. Needs a
                directory and makes no package.

The regular build is a normal build of its goal, with the test keys and
a placeholder date. The signing run is a build of the final goals with
the date of the newest file read from the regular build, and with the
release keys in an absolute KEYS_DIR, or the test keys without KEYS_DIR.
It runs only the steps that use the keys or the date and the steps that
read their outputs, and writes the signed outputs into a separate
directory. Every other step that the goals of the signing run need has
to be up to date in the regular build. When one is not, the signing run
stops before any step runs and lists where the regular build has to
start. The signed OTA package and its release name are in the product
directory there. The build system sets that directory in
SIGNED_PRODUCT_OUT.

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
