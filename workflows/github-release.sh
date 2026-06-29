#!/bin/bash
#
# Copyright (C) 2024 Canonical Ltd
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

# Creates a GitHub Release for the given tag and uploads the ChangeLog
# extracted from a downloaded snap file.
#
# Usage: github-release.sh <snap_dir> <snap_name> <tag>
#
# The gh CLI uses GITHUB_TOKEN, which is automatically available in GitHub
# Actions. The calling workflow must have: permissions: contents: write

set -eu -o pipefail

if [ $# -ne 3 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    printf "Usage: %s <snap_dir> <snap_name> <tag>\n" "$0"
    exit 1
fi

snap_dir=$1
snap_name=$2
tag=$3

if [ -z "$snap_dir" ] || [ -z "$snap_name" ] || [ -z "$tag" ]; then
    printf "ERROR: snap_dir, snap_name, and tag must be non-empty\n" >&2
    exit 1
fi

# Find a snap to extract the ChangeLog from.
# Prefer amd64 for reproducibility; fall back to the first snap found.
snap_file=
for f in "$snap_dir"/"${snap_name}"_*_amd64.snap; do
    if test -f "$f"; then
        snap_file=$f
        break
    fi
done
if [ -z "$snap_file" ]; then
    for f in "$snap_dir"/"${snap_name}"_*.snap; do
        if test -f "$f"; then
            snap_file=$f
            break
        fi
    done
fi

if [ -z "$snap_file" ]; then
    printf "ERROR: no snap file found in '%s' for snap '%s'\n" \
           "$snap_dir" "$snap_name" >&2
    exit 1
fi

printf "Extracting ChangeLog from %s\n" "$snap_file"

# Extract only the ChangeLog path from the snap into a temporary directory.
extract_dir=$(mktemp -d)
trap 'rm -rf "$extract_dir"' EXIT

unsquashfs -d "$extract_dir/squashfs-root" "$snap_file" usr/share/doc/ChangeLog

changelog_file="$extract_dir/squashfs-root/usr/share/doc/ChangeLog"
if [ ! -f "$changelog_file" ]; then
    printf "ERROR: ChangeLog not found at usr/share/doc/ChangeLog inside snap\n" >&2
    exit 1
fi

# Parse the latest (first) ChangeLog entry for use as release notes.
# Entry header format: DD/MM/YYYY, commit https://.../tree/<sha>
release_notes=""
entry_header=$(head -1 "$changelog_file")
entry_date=$(printf '%s' "$entry_header" | grep -oP '^\d{2}/\d{2}/\d{4}') || true
entry_commit=$(printf '%s' "$entry_header" | grep -oP '/tree/\K[0-9a-f]+$') || true

if [ -z "$entry_date" ] || [ -z "$entry_commit" ]; then
    printf "WARNING: could not parse ChangeLog entry header: %s\n" "$entry_header" >&2
else
    # Derive expected date in DD/MM/YYYY from tag (first 8 chars are YYYYMMDD)
    tag_ymd=${tag:0:8}
    expected_date="${tag_ymd:6:2}/${tag_ymd:4:2}/${tag_ymd:0:4}"
    # Get the commit SHA the tag points to.
    tag_commit=$(git rev-parse "$tag")

    valid=true
    if [ "$entry_date" != "$expected_date" ]; then
        printf "WARNING: ChangeLog date '%s' does not match tag date '%s'\n" \
               "$entry_date" "$expected_date" >&2
        valid=false
    fi
    if [ "$entry_commit" != "$tag_commit" ]; then
        printf "WARNING: ChangeLog commit '%s' does not match tag commit '%s'\n" \
               "$entry_commit" "$tag_commit" >&2
        valid=false
    fi

    if [ "$valid" = true ]; then
        # Extract entry: lines from header until next entry header or EOF
        release_notes=$(awk \
            'NR > 1 && /^[0-9]{2}\/[0-9]{2}\/[0-9]{4}, commit/ { exit } { print }' \
            "$changelog_file")
    fi
fi

# Create the GitHub Release and attach the ChangeLog as a release asset.
# If a release for this tag already exists (e.g. a retried run), update its
# notes and overwrite the asset.
# The '<path>#<label>' syntax sets the asset display name to 'ChangeLog'.
if gh release view "$tag" >& /dev/null; then
    printf "Release for tag '%s' already exists; updating notes and uploading ChangeLog\n" "$tag"
    gh release edit "$tag" --notes "$release_notes"
    gh release upload "$tag" "$changelog_file#ChangeLog" --clobber
else
    printf "Creating GitHub Release for tag '%s'\n" "$tag"
    gh release create "$tag" \
        --title "$tag" \
        --notes "$release_notes" \
        "$changelog_file#ChangeLog"
fi
