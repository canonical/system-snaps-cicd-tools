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

mapfile -t snaps < <(find "$snap_dir" -maxdepth 1 -name "${snap_name}_*.snap")
if [ ${#snaps[@]} -eq 0 ]; then
    printf "ERROR: no snap files found in '%s' for snap '%s'\n" "$snap_dir" "$snap_name" >&2
    exit 1
fi

# Extract ChangeLog from every downloaded snap. Each is uploaded as
# ChangeLog_<arch>. Release notes are always taken from the amd64 snap.
extract_dir=$(mktemp -d)
trap 'rm -rf "$extract_dir"' EXIT

upload_args=()
changelog_for_notes=""

for snap_p in "${snaps[@]}"; do
    # Extract arch from snap filename, e.g. core24_20260627_amd64.snap:
    # strip up to and including the last '_', then strip the '.snap' suffix.
    arch=${snap_p##*_}
    arch=${arch%.snap}
    printf "Extracting ChangeLog from %s\n" "$snap_p"
    unsquashfs -d "$extract_dir/$arch" "$snap_p" usr/share/doc/ChangeLog
    cl_file="$extract_dir/$arch/usr/share/doc/ChangeLog"
    if test -f "$cl_file"; then
        # The '<path>#<label>' syntax sets the asset display name.
        upload_args+=("$cl_file#ChangeLog_$arch")
        if [ "$arch" = amd64 ]; then
            changelog_for_notes=$cl_file
        fi
    else
        printf "WARNING: ChangeLog not found inside snap %s\n" "$snap_p" >&2
    fi
done

if [ ${#upload_args[@]} -eq 0 ]; then
    printf "ERROR: no ChangeLog found in any snap for '%s'\n" "$snap_name" >&2
    exit 1
fi

release_notes=""
if [ -n "$changelog_for_notes" ]; then
    # Parse the latest (first) ChangeLog entry for use as release notes.
    # Entry header format: DD/MM/YYYY, commit https://.../tree/<sha>
    entry_header=$(head -1 "$changelog_for_notes")
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
            release_notes=$(printf "ChangeLog for amd64 snap:\n\n";
                            awk 'NR > 1 && /^[0-9]{2}\/[0-9]{2}\/[0-9]{4}, commit/ { exit } { print }' \
                                "$changelog_for_notes")
        fi
    fi
else
    printf "WARNING: no amd64 snap found. Release notes will indicate missing ChangeLog\n" >&2
    release_notes="ChangeLog for amd64 snap not found. See attached per-arch ChangeLogs."
fi

# Create the GitHub Release and attach ChangeLog_<arch> files as assets.
# If a release for this tag already exists (e.g. a retried run), update its
# notes and overwrite the assets.
if gh release view "$tag" >& /dev/null; then
    printf "Release for tag '%s' already exists; updating notes and uploading ChangeLogs\n" "$tag"
    gh release edit "$tag" --notes "$release_notes"
    gh release upload "$tag" "${upload_args[@]}" --clobber
else
    printf "Creating GitHub Release for tag '%s'\n" "$tag"
    gh release create "$tag" \
        --title "$tag" \
        --notes "$release_notes" \
        "${upload_args[@]}"
fi
