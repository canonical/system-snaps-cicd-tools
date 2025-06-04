#!/bin/bash
#
# Copyright (C) 2023 Canonical Ltd
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

set -ex -o pipefail
shopt -s inherit_errexit

# Find the scripts folder
script_name=${BASH_SOURCE[0]##*/}
CICD_SCRIPTS=${BASH_SOURCE[0]%%"$script_name"}./

# shellcheck source=common.sh
. "$CICD_SCRIPTS"/common.sh

build_d=$1
snap_name=$2
channel=$3

printf "Releasing %s snap to %s\n" "$snap_name" "$channel"

# Check to avoid inadvertingly releasing by just specifying the risk -
# full track/risk (with optional branch) is required
if [[ "$channel" != */* ]]; then
    printf "ERROR: no track specified in release channel %s\n" "$channel"
    exit 1
fi

# We need snapcraft to upload and release the snaps
if ! command -v snapcraft; then
    sudo snap install snapcraft
fi
# "|| true": needed until LP#2103643 is fixed. The command is run just for
# debugging purposes anyway.
snapcraft whoami || true
push_and_release_snap "$build_d" "$snap_name" "$channel"
