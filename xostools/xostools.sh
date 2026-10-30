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

echo -e "\033[0mincluding \033[1m\033[38;5;39mXOS\033[0m\033[1m Tools\033[0m"

# Without THREAD_COUNT_BUILD, or with it set to auto, the build sizes its
# job count from the free CPU time and memory of the machine while it runs.
if [[ -z $THREAD_COUNT_BUILD || $THREAD_COUNT_BUILD == "auto" ]]; then
  THREAD_COUNT_BUILD_ARG=""
else
  THREAD_COUNT_BUILD_ARG="-j$THREAD_COUNT_BUILD"
fi

# Save the current directory before continuing the script.
# The working directory might change during the execution of specific
# functions, which should be set back to the beginning directory
# so the user does not need to do that manually.
BEGINNING_DIR="$(pwd)"

### BASIC FUNCTIONS START

# Echo with halogen color without new line
function echoxcc() {
    echo -en "\033[1;38;5;39m$@\033[0m"
}

# Echo with halogen color with new line
function echoxc() {
    echoxcc "\033[1;38;5;39m$@\033[0m\n"
}

# Echo with new line and respect escape characters
function echoe() {
    echo -e "$@"
}

# Echo with line, respect escape characters and print in bold font
function echob() {
    echo -e "\033[1m$@\033[0m"
}

# Echo without new line
function echon() {
    echo -n "$@"
}

# Echo without new line and respect escape characters
function echoen() {
    echo -en "$@"
}

### BASIC FUNCTIONS END

if ! command -V gettop > /dev/null 2>&1; then
    export TOP="$(pwd)"
    function gettop() {
        # just assume we're a the top. usually this only happens
	# when we're just getting started
        echo $TOP
    }
fi

# Import help functions
source $(gettop)/external/xos/xostools/xostoolshelp.sh

# Build function
function build() {
    buildarg="$1"
    target="$2"
    cleanarg="$3 $4"
    module="${cleanarg//noclean/}"
    module="${module// /}"
    cleanarg="${cleanarg/$module/}"
    cleanarg="${cleanarg// /}"

    # Display help if no argument passed
    if [ -z "$buildarg" ]; then
        xostools_help_build
        return 0
    fi

    if [ "$buildarg" = "sign" ]; then
        if [ -z "$TARGET_PRODUCT" ]; then
            echo "Please run lunch first" >&2
            return 1
        fi
        local signed_out
        signed_out="$(signed_product_out)" || return $?
        sign_build m otapackage && name_ota_package "$signed_out"
        return $?
    fi

    # Notify that no target device could be found
    if [ -z "$target" ]; then
        xostools_build_no_target_device
    else
        # Handle the first argument
        case "$buildarg" in

            full | module | mm)
                if [ "$buildarg" != "full" ] && [ -z "$module" ]; then
                    if [ "$buildarg" = "mm" ]; then
                        echo "No directory named. \"build full\" makes the package." >&2
                    else
                        echo "No module named. \"build full\" makes the package." >&2
                    fi
                    return 1
                fi
                echob "Starting build..."
                if [ -n "$module" ]; then
                    echo "You have decided to build $module"
                fi
		device=${target#*_}
		device=${device%%-*}
                eval "lunch ${target//-/ }" || (breakfast $device && eval "lunch ${target//-/ }") || return 1
                # Every build is the regular build of the goal before signing,
                # then the signing run of the final goals. A full device build
                # gives the signed OTA package its release name.
                local command=m goals=() finish=print_signed_output
                if [ -n "$module" ]; then
                    [ "$buildarg" = "mm" ] && command=mmma
                    goals=("$module")
                elif [[ "$target" != *"sdk_phone"* ]]; then
                    goals=(otapackage)
                    finish=name_ota_package
                fi
                check_keys_dir || return $?
                # Clean if desired
                [[ "$cleanarg" == "noclean" ]] || m clean
                # Now start building
                if [ -n "$THREAD_COUNT_BUILD_ARG" ]; then
                    echo "Using $THREAD_COUNT_BUILD threads for build."
                else
                    echo "Using adaptive job scaling."
                fi
                if [ "$buildarg" != "mm" ] && [[ "$target" == *"sdk_phone"* ]]; then
                    echob "Building for SDK phone"
                fi
                build_and_sign "$command" "$module" "${goals[@]}" || return $?
                local signed_out
                signed_out="$(signed_product_out)" || return $?
                "$finish" "$signed_out" || return $?
            ;;

            module-list)
                local bm_result
                echob "Starting batch build..."
                shift
                ALL_MODULES_TO_BUILD="$@"
                [[ "$@" == *"noclean"* ]] || m clean
                for module in $ALL_MODULES_TO_BUILD; do
                    [[ "$module" == "noclean" ]] && continue
                    echo
                    echob "Building module $module"
                    echo
                    build module $TOOL_THIRDARG $module noclean
                    local bm_result=$?
                done
                echob "Finished batch build"
                [ $bm_result -ne 0 ] && return $bm_result
            ;;

            # Oops.
            *) echo "Unknown build command \"$TOOL_SUBARG\"." ;;

        esac
    fi
    return 0
}

# Fails unless KEYS_DIR is unset or is a directory with release keys
function check_keys_dir() {
    [ -z "$KEYS_DIR" ] && return 0
    if [[ "$KEYS_DIR" != /* ]]; then
        echo "KEYS_DIR needs an absolute path, got $KEYS_DIR" >&2
        return 1
    fi
    if [ ! -f "$KEYS_DIR/releasekey.pk8" ]; then
        echo "No release keys found in $KEYS_DIR" >&2
        return 1
    fi
}

# The regular build with the build command $1 (m or mmma), then the signing
# run of the final goals that follow $2, or of the default goal when none
# follow. For a module or directory $2, the regular build builds $2 and passes
# no signing goals, since the signing run builds the same goal. When $2 is
# empty, the regular build covers the whole product: it builds the installed
# files, and every step that the final goals need except the steps that the
# signing run runs, the steps that use the keys or the date and the steps that
# read their outputs.
function build_and_sign() {
    local regular_args
    if [ -n "$2" ]; then
        regular_args=("$2")
    else
        regular_args=(--signing-goals="$(IFS=,; echo "${*:3}")" files)
    fi
    "$1" --skip-soong-tests $THREAD_COUNT_BUILD_ARG "${regular_args[@]}" || return $?
    sign_build "$1" "${@:3}"
}

# The signing run: the build of the goals with the build command $1, with the
# release keys in KEYS_DIR or the test keys when KEYS_DIR is unset
function sign_build() {
    local args
    signing_args args || return $?
    "$1" --skip-soong-tests $THREAD_COUNT_BUILD_ARG "${args[@]}" "${@:2}"
}

# Sets the array named $1 to the arguments that make a build the signing run
function signing_args() {
    check_keys_dir || return $?
    local -n args_ref="$1"
    args_ref=(--signing-run)
    if [ -n "$KEYS_DIR" ]; then
        args_ref=(--signing-keys-dir="$KEYS_DIR" "${args_ref[@]}")
        echob "Signing with the keys in $KEYS_DIR..."
    else
        echob "Signing with the test keys..."
    fi
}

# Prints the product output directory of the signing run, which the build
# system reports.
function signed_product_out() {
    local signed_out
    signed_out="$(get_build_var SIGNED_PRODUCT_OUT)" || return $?
    if [ -z "$signed_out" ]; then
        echo "The build system did not report SIGNED_PRODUCT_OUT" >&2
        return 1
    fi
    echo "$signed_out"
}

function print_signed_output() {
    echob "Signed output: $1"
}

# Gives the OTA package in the product output directory $1 its release name
function name_ota_package() {
    local product_out="$1"
    local product_name="$TARGET_PRODUCT"
    [ "$TARGET_BUILD_TYPE" = "debug" ] && product_name="${product_name}_debug"
    local ota_package="${product_out}/${product_name}-ota.zip"
    if [ ! -f "$ota_package" ]; then
        echo "No OTA package found at $ota_package" >&2
        return 1
    fi

    local custom_version
    custom_version=$(cat "$product_out/product/etc/build.prop" \
        "$product_out/system/product/etc/build.prop" \
        2> /dev/null | read_custom_version "$product_out") || return $?
    local named_ota="${product_out}/${custom_version}.zip"

    # The hard link gives the package its release name without a copy
    echob "Naming OTA package..."
    ln -f "$ota_package" "$named_ota" || return $?
    write_sha256sum "$named_ota" || return $?

    echob "OTA: $named_ota"
}

# Reads build.prop text on stdin, $1 names where it came from
function read_custom_version() {
    local version
    version=$(grep -m1 '^ro.custom.version=' | cut -d= -f2)
    if [ -z "$version" ]; then
        echo "No ro.custom.version found in the product build.prop in $1" >&2
        return 1
    fi
    echo "$version"
}

# The checksum line names the file without its directory
function write_sha256sum() {
    local sum
    sum=$(cd "$(dirname "$1")" > /dev/null && sha256sum "$(basename "$1")") || return $?
    echo "$sum" > "$1.sha256sum"
}

function emu() {
    local target="$1"
    local skin="${2:-1080x2160}"

    if [ -z "$target" ]; then
        echo "Usage: emu <target> [skin]"
        echo "  target: lunch target (e.g. sdk_phone64_x86_64-cp2a-userdebug)"
        echo "  skin:   emulator skin resolution (default: 1080x2160)"
        echo ""
        echo "Builds the images, starts the emulator on them and waits until it"
        echo "is closed. Run it again after a change."
        return 1
    fi

    # Lunch dance (same as build full)
    local device=${target#*_}
    device=${device%%-*}
    eval "lunch ${target//-/ }" || (breakfast $device && eval "lunch ${target//-/ }") || return 1

    echob "Building (including images for emulator)..."
    build_and_sign m "" || return $?
    local signed_out
    signed_out="$(signed_product_out)" || return $?
    bash "$(gettop)/external/xos/xostools/scripts/emulator_session.sh" "$skin" "$signed_out"
}

function reposync() {
    repo sync --force-sync -c --no-clone-bundle --no-tags $@
    return $?
}

# This is repoREsync. It REsyncs. Self-explanatory?
function reporesync() {
    echo "Preparing..."
    FRSTDIR="$(pwd)"
    # Let's cd to the top of the working tree
    # Hoping that we don't land in the home directory.
    cd $(gettop)
    # Critical security check to prevent deleting home directory if the build
    # directory has been removed from the work tree for whatever reason.
    if [[ "$(pwd)" == "$(ls -d ~)" ]]; then
        # Let's warn the user about this bad state.
        echoe "WARNING: 'gettop' is returning your \033[1;91mhome directory\033[0m!"
        echoe "         In order to protect your data, this process will be aborted now."
        return 1
    else
        # Oh yeah, we passed!
        echob "Security check passed. Continuing."
    fi

    # Now let's handle the first argument as always
    case "$1" in

        # Do a full sync
        #   full:       just delete the working tree directories and sync normally
        #   full-x:     delete everything except manifest and repo tool, means
        #               you need to resync everything again.
        #   full-local: don't update the repositories, only do a full resync locally
        full)
            # Print a very important message
            echoe \
                "WARNING: This process will delete \033[1myour whole source tree!\033[0m"
            # Ask if the girl or guy really wants to continue.
            if [ "$2" != "confident" ]; then
            # Check if shell is ZSH by checking ZSH_NAME var, which is only set for zsh.
            if [[ ! -z "$ZSH_NAME" ]]; then # In use shell is zsh
                read -k 1 -r "?Do you want to continue? [y\N] : "
            else
                # Shell isn't zsh, so assume bash syntax.
                read -p "Do you want to continue? [y\N] : " \
                     -n 1 -r
            fi
            # Check the reply.
            [[ ! $REPLY =~ ^[Yy]$ ]] && echoe "\nAborted." && return 1
            fi
            # Print some lines of words
            echoe "\n"
            echob "Full source tree resync will start now."
            # Just in case...
            echo  "Your current directory is: $(pwd)"
            # ... read the printed lines so you know what's going on.
            echon "If you think that the current directory is wrong, you will "
            echo  "now have time to safely abort this process using CTRL+C."
            echoen "\n"
            echon  "Waiting for interruption..."
            # Wait 4 lovely seconds which can save your life
            sleep 4
            # Wipe out the above line, now it is redundant
            echoen "\r\033[K\r"
            echoen "Got no interruption, continuing now!"
            echoen "\n"
            # Collect all directories found in the top of the working tree
            # like build, abi, art, bionic, cts, dalvik, external, device, ...
            # and then remove them, and show the user the beautiful progress
            echo "Collecting and removing directories..."
            echo -en "\n\r"
            for ff in *; do
                case "$ff" in
                  "." | ".." | ".repo");;
                  *)
                      echo -en "\rRemoving $ff\033[K"
                      rm -rf "$ff"
                  ;;
                esac
            done
            echo -en "\n"
            # And let's sync!
            echo "Starting sync..."
            reposync
        ;;

        repo)
            echob "Resyncing $1..."
            rm -rf $1
            reposync $1
        ;;

        # Help me!
        "")
            xostools_help_reporesync
            cd $FRSTDIR
            return 0
        ;;

    esac
    cd $FRSTDIR
}

function strtrim() {
  sed -e 's/^ *//g' -e 's/ *$//g'
}

function strsplit() {
  cut -d "$1" -f$2
}

function splitix_and_trim() {
  local ix=$1
  local split="$2"
  shift 2
  echo "$@" | strsplit "$split" $ix | strtrim
}

# Resets all repositories to their corresponding remote state
# as defined in the manifest
function reporeset() {
  if [ -z "$BASH_VERSION" ]; then
    bash -ic "cd $(gettop) && source build/envsetup.sh && reporeset"
    return $?
  fi
  echo 'Resetting source tree back to remote state.' \
       'Any unsaved work will be gone.'
  cd .repo/manifests && git reset --hard m/${ROM_REVISION}

  local TOP="$(gettop)"

  repomanifest=$(repo manifest)
  function repomanifest() {
    cat <<EOF
$repomanifest
EOF
  }

  while read line; do
    local repodir=$(splitix_and_trim 1 ':' "$line")
    if [ ! -d "$(gettop)/$repodir" ]; then
      continue
    fi
    local reponame=$(splitix_and_trim 2 ':' "$line")
    local usekey="path"
    local usevalue="$repodir"
    if [ "$(repomanifest | xmlstarlet sel -t -v "//project[@path='$repodir']/@path")" != \
          "$repodir" ]; then
      local usekey="name"
      local usevalue="$reponame"
    fi
    local path="$repodir"
	repo_remote=$(repomanifest | xmlstarlet sel -t -v "/manifest/project[@path='$path']/@remote" || \
                  repomanifest | xmlstarlet sel -t -v "/manifest/default/@remote")
	repo_revision=$(repomanifest | xmlstarlet sel -t -v "/manifest/project[@path='$path']/@revision" || \
                    repomanifest | xmlstarlet sel -t -v "/manifest/default[@remote='$repo_remote']/@revision" || :)
	if [ -z "$repo_revision" ]
	then
		repo_revision=$(repomanifest | xmlstarlet sel -t -v "/manifest/remote[@name='$repo_remote']/@revision" || :)
	fi
	short_revision=${repo_revision/refs\/heads\//}
    short_revision=${repo_revision/refs\/tags\//}
	repo_url=$(repomanifest | xmlstarlet sel -t -v "/manifest/remote[@name='$repo_remote']/@fetch" || \
               repomanifest | xmlstarlet sel -t -v "/manifest/default[@remote='$repo_remote']/@fetch")
	if [ -z "$repo_revision" ]
	then
		if [ -z "$ROM_REVISION" ]
		then
			echo -e "\033[1mWarning: unable to determine revision and ROM_REVISION or ROM_VERSION not set! \033[0m"
			repo_revision="${ROM_REVISION}"
		else
			echo -e "Note: unable to determine revision, defaulting to $ROM_REVISION"
			repo_revision="$ROM_REVISION"
		fi
	fi
	local remote="$repo_remote"
	local revision="$repo_revision"
	local remote="$remote/"
    repo_url="$repo_url$reponame"
    pushd $TOP/$repodir
	echo "$repodir: resetting and cleaning up untracked files/folders"
	git rebase --abort 2> /dev/null > /dev/null || git merge --abort 2> /dev/null > /dev/null || git revert --abort 2> /dev/null > /dev/null || git cherry-pick --abort 2> /dev/null > /dev/null || :
    git stash >/dev/null 2>/dev/null || : # :D
    git reset --hard XOS/$(echo $revision | sed -re 's/^refs\/heads\/(.*)$/\1/') 2>/dev/null || git reset --hard $remote$revision 2> /dev/null || ( [ "$repo_name" != "aosp" ] && git reset --hard XOS/$revision ) 2> /dev/null || git reset --hard $revision 2> /dev/null || git reset --hard
    git clean -fdx || :
    popd
    echo
  done < <(repo list)

  unset repomanifest
}

function resetmanifest() {
  cd $(gettop)/.repo/manifests
  git fetch origin ${ROM_REVISION} 2>&1 >/dev/null
  git reset --hard origin/${ROM_REVISION} 2>&1 >/dev/null
  cd $(gettop)
}

function print_product_packages() {
    get_build_var PRODUCT_PACKAGES
}

function pretty_print_product_packages() {
    print_product_packages | tr " " "\n" | sort -u
}

source $(gettop)/external/xos/xostools/mergetools.sh
source $(gettop)/external/xos/xostools/other_tools.sh

function reticulateOurSplines() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/reticulate_our_splines.sh" $@
}

function addUpstreamRemotes() {
    TOP="$(gettop)" bash -i "$(gettop)/external/xos/xostools/scripts/add_upstream_remotes.sh" $@
}

source $(gettop)/external/xos/xostools/includes.sh

function addOther() {
	git remote remove $1
	git remote add $1 https://github.com/$1/$(getUnderscorePath).git
	git fetch $1
}

function addOtherPlatform() {
	git remote remove $1
	git remote add $1 https://github.com/$1/$(getUnderscorePath platform).git
	git fetch $1
}

function addOtherShort() {
	git remote remove $1
	git remote add $1 https://github.com/$1/$(getNonPrefixedUnderscorePath | cut -c2-).git
	git fetch $1
}

function addOtherCustom() {
	git remote remove $1
	git remote add $1 https://github.com/$1/$(getUnderscorePath $2).git
	git fetch $1
}

filterSubdirectory() {
  FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --subdirectory-filter $1 $2 -- --all
}

pickrange () {
	git rev-list --reverse --topo-order $1^..$2 | while read rev
	do
		echo "$rev"
	done | xargs git cherry-pick -s
}

mirrorThisRepo() {
    if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
        echo "Shallow repository detected, skipping"
        popd
        continue
    fi

    addXos || :
    addXosGithub || :
    git branch -r --list 'xos/*' | awk '{ print $1 }' | cut -d '/' -f2- | grep -vE '^HEAD$' | xargs -i git push xosgh 'xos/{}:refs/heads/{}' || :
    git tag --list | grep -E '^XOS-[0-9]+?[.][0-9]+?-.*' | xargs -i git push xosgh '{}' || :
}

filterbranch() {
    FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --subdirectory-filter $1 $2 -- --all
}

alias gpick="git cherry-pick -s"

if [ -f "$(gettop)/ccache.sh" ]; then
    echo "including ccache.sh"
    source "$(gettop)/ccache.sh"
    if [ -z "$CCACHE_EXEC" ]; then
        export CCACHE_EXEC="$(which ccache)"
    fi
fi

return 0
