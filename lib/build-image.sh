#!/bin/bash

# build-image.sh: Build an Ubuntu Core image runnable in spread CI
#
# Copyright (C) 2023 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# This produces an Ubuntu Core image for testing in file `pc.img.gz`.
# It will initialize the spread user as well as the project directory
# (which has to be defined in `PROJECT_PATH`).
# This is expected to be called from spread.

# TODO: This build Ubuntu Core 22. We should fix change it to build
# any version.

set -eu

TMPDIR="$(mktemp -d)"
cleanup() {
    rm -rf "${TMPDIR}"
}

trap cleanup EXIT

DEBIAN_FRONTEND=noninteractive apt install -y --no-install-recommends snapd squashfs-tools
snap install --classic ubuntu-image

wget -O "${TMPDIR}/model.model" https://raw.githubusercontent.com/snapcore/models/master/ubuntu-core-22-amd64-dangerous.model

(cd "${TMPDIR}"; snap download pc --channel=22/edge --basename=pc)
unsquashfs -d "${TMPDIR}/pc" "${TMPDIR}/pc.snap"

cat <<EOF >>"${TMPDIR}/pc/gadget.yaml"
defaults:
  system:
    service:
      console-conf:
        disable: true
EOF

case "${SPREAD_BACKEND}" in
    google)
        ssh_user="root"
        ssh_passwd="$(grep '^root:' </etc/shadow | cut -d: -f2)"
        ;;
    qemu)
        ssh_user="ubuntu"
        ssh_passwd="$(openssl passwd -6 ubuntu)"
        ;;
    *)
        echo "unknown backend" 1>&2
        exit 1
        ;;
esac

case "${ssh_user}" in
    root)
        # Some spread test instances (like GCE) expect root to be
        # used. We cannot set a password for root with extrausers. So
        # we need to bind mount a modified shadow (which is read-only)
        # to set the root password
        cat <<EOF >etc-shadow.mount
[Unit]
[Mount]
What=/writable/shadow
Where=/etc/shadow
Type=none
Options=bind
[Install]
WantedBy=local-fs.target
EOF
        shadow_unit="$(base64 -w0 etc-shadow.mount)"
        cat <<EOF >"${TMPDIR}/pc/cloud.conf"
#cloud-config
datasource_list: [NoCloud]
runcmd:
  - echo "Setting up spread user"
  - mkdir '${PROJECT_PATH}'
  - tar zxf /run/mnt/gadget/data.tar.gz -C '${PROJECT_PATH}'
  - chown -R "${ssh_user}:${ssh_user}" '${PROJECT_PATH}'
  - sed 's,${ssh_user}:[*],${ssh_user}:${ssh_passwd},' /etc/shadow >/writable/shadow
  - echo '${shadow_unit}' | base64 -d >/etc/systemd/system/etc-shadow.mount
  - echo 'PermitRootLogin yes' >>/etc/ssh/sshd_config
  - systemctl daemon-reload
  - systemctl enable etc-shadow.mount
  - systemctl start etc-shadow.mount
  - systemctl reload ssh.service
  - echo "Done setting up spread user"
EOF
        ;;
    *)
        cat <<EOF >"${TMPDIR}/pc/cloud.conf"
#cloud-config
datasource_list: [NoCloud]
runcmd:
  - echo "Setting up spread user"
  - adduser --extrausers --shell /bin/bash "${ssh_user}"
  - echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers.d/99-ubuntu-user
  - mkdir '${PROJECT_PATH}'
  - tar zxf /run/mnt/gadget/data.tar.gz -C '${PROJECT_PATH}'
  - chown -R "${ssh_user}":"${ssh_user}" '${PROJECT_PATH}'
  - "usermod -p '${ssh_passwd}' '${ssh_user}'"
  - echo "Done setting up spread user"
EOF
        ;;
esac

cat >>"${TMPDIR}/pc/cmdline.extra" <<EOF
systemd.journald.forward_to_console=1 console=ttyS0
EOF

tar zcf "${TMPDIR}/pc/data.tar.gz" -C "${PROJECT_PATH}" .

snap pack --filename "${TMPDIR}/pc.snap" "${TMPDIR}/pc"
ubuntu-image snap "${TMPDIR}/model.model" --snap "${TMPDIR}/pc.snap"
gzip pc.img
