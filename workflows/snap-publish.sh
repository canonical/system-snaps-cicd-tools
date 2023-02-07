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
push_and_release_snap "$build_d" "$snap_name" "$channel"
