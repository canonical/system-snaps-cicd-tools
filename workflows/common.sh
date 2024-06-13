#!/bin/bash -ex
#
# Copyright (C) 2017 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Creates and downloads the snaps for the supported architectures
# $1: snap name
# $2: repository url (launchpad API does not accept git+ssh)
# $3: release branch
# $4: ubuntu series
# $5: results folder
# $6: architectures to build for (if empty a default for a given series will be used)
# $7: snapcraft channel (if empty a default for a given series will be used)
build_and_download_snaps()
{
    local snap_n=$1
    local repo_url=$2
    local release_br=$3
    local series=$4
    local results_d=$5
    local build_architectures=$6
    local snapcraft_channel=$7

    # Starting with core20/focal, i386 is not supported
    if [ -z "$build_architectures" ]; then
        if [ "$series" = xenial ] || [ "$series" = bionic ]; then
            archs=i386,amd64,armhf,arm64
        elif [ "$series" = focal ]; then
            archs=amd64,armhf,arm64
        else
            archs=amd64,armhf,arm64,riscv64
        fi
    else
        echo "Overriding build architectures to $build_architectures" >&2
        archs="$build_architectures"
    fi

    # Build snap without publishing it to get the new manifest.
    # TODO we should leverage it to run tests as well
    "$CICD_SCRIPTS"/trigger-lp-build.py \
                    -s "$snap_n" \
                    --architectures="$archs" \
                    --git-repo="$repo_url" \
                    --git-repo-branch="$release_br" \
                    --results-dir="$results_d" \
                    --series="$series" \
                    --snapcraft-channel="$snapcraft_channel"
}

# Inject or remove files in a snap
# $1: path to snap
# Following arguments are pairs of:
# $n: path to file to inject, empty if we want to remove instead
# $n+1: path inside snap of the file to inject/remove
modify_files_in_snap()
{
    local snap_p=$1
    shift 1
    local fs_d dest_d i

    fs_d=squashfs
    unsquashfs -d "$fs_d" "$snap_p"

    i=1
    while [ $i -le $# ]; do
        local orig_p dest_p
        orig_p=${!i}
        i=$((i + 1))
        dest_p=${!i}
        i=$((i + 1))
        if [ -n "$orig_p" ]; then
            dest_d="$fs_d"/${dest_p%/*}
            dest_f=${dest_p##*/}
            mkdir -p "$dest_d"
            cp "$orig_p" "$dest_d/$dest_f"
        else
            rm -f "$fs_d/$dest_p"
        fi
    done

    rm "$snap_p"
    snap pack --filename="$snap_p" "$fs_d"
    rm -rf "$fs_d"
}

# Login to the snap store
snap_store_login()
{
    # No need to log-in anymore as we use SNAPCRAFT_STORE_CREDENTIALS.
    # However, we print id information here as that includes expiration
    # date for the credentials, which can be useful.
    _run_snapcraft whoami
}

_run_snapcraft()
{
    snapcraft "$@"
}

# Logout of the snap store
snap_store_logout()
{
    # Doing logout does not make sense anymore as we use
    # SNAPCRAFT_STORE_CREDENTIALS env var. We keep the function as it
    # might be useful in the future.
    true
}

# Pushes and releases snaps in a folder for the given channel
# $1: directory with snaps
# $2: snap name
# $3,...,$n: channels
push_and_release_snap()
(
    local snaps_d=$1
    local snap_n=$2
    shift 2
    local channels=$*
    channels=${channels// /,}
    local snap_file

    cd "$snaps_d"
    for snap_file in "$snap_n"_*.snap; do
        _run_snapcraft upload "$snap_file" --release "$channels"
    done
)

# Return path to snapcraft.yaml. Run inside repo.
get_snapcraft_yaml_path()
{
    if [ -f snapcraft.yaml ]; then
        printf snapcraft.yaml
    elif [ -f snap/snapcraft.yaml ]; then
        printf snap/snapcraft.yaml
    fi
}

# Get snap series from snapcraft.yaml
# $1: path to snapcraft.yaml
get_series()
{
    local base
    base=$(grep -oP '^base:[[:space:]]+core\K\w+' "$1") || true
    case "$base" in
        24) printf noble ;;
        22) printf jammy ;;
        20) printf focal ;;
        18) printf bionic ;;
        *)  printf xenial ;;
    esac
}

# Get track from branch, assuming it is of the form <name>-<track>
# $1: branch
get_track_from_branch()
{
    local branch=$1
    local branch_sufix
    # If there is no '-', branch_sufix will be equal to branch
    branch_sufix=${branch##*-}
    if [ "$branch_sufix" !=  "$branch" ]; then
        printf %s "$branch_sufix"
    else
        printf latest
    fi
}
