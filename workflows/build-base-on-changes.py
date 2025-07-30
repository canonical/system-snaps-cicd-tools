#!/usr/bin/python3

import apt_pkg
import gzip
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import yaml

from argparse import ArgumentParser
from datetime import datetime
from debian import deb822
from launchpadlib.launchpad import Launchpad


SNAP_API = \
    'https://api.launchpad.net/devel/~ubuntu-core-service/+snap/core{}'
CORE_SNAP_API = \
    'https://api.launchpad.net/devel/~snappy-dev/+snap/core{}'


_logger = logging.getLogger('ubuntu-image')

# The empty string stands for the core snap, which was released as 16 series
series_map = {
    "": "xenial",
    "18": "bionic",
    "20": "focal",
    "22": "jammy",
    "24": "noble",
}


def update_snap2version(snap2version, package, version):
    if version.isspace() and package.isspace():
        return
    if version.isspace() or package.isspace():
        print('parse error, one of package ({}) or version ({}) is empty'.
              format(package, version))
        sys.exit(1)
    # Update if new version is more modern only
    if package in snap2version:
        if apt_pkg.version_compare(version, snap2version[package]) > 0:
            snap2version[package] = version
    else:
        snap2version[package] = version


def package_versions_from_file(pkgs_p, snap2version):
    with gzip.open(pkgs_p, 'rt') as pkgs_f:
        for pkg in deb822.Packages.iter_paragraphs(pkgs_f):
            package = pkg.get('Package')
            version = pkg.get('Version')
            update_snap2version(snap2version, package, version)


def check_packages_changed(core_series):
    # Note that we consider here only amd64, at the moment there are no
    # differences in packages primed in bases depending on arches.
    changed = False
    with tempfile.TemporaryDirectory() as base_tmpd:
        # Download edge snap, extract manifest
        base = 'core{}'.format(core_series)
        subprocess.run(['snap', 'download', '--edge', '--basename', base,
                        '--target-directory', base_tmpd, base], check=True)
        sq_d = os.path.join(base_tmpd, base)
        base_p = os.path.join(base_tmpd, base + '.snap')
        dpkg_sq_p = 'usr/share/snappy/dpkg.yaml'
        dpkg_p = os.path.join(sq_d, dpkg_sq_p)
        subprocess.run(['unsquashfs', '-d', sq_d, base_p, dpkg_sq_p],
                       check=True, stdout=subprocess.DEVNULL)

        # Load manifest
        with open(dpkg_p, 'r') as dpkg_f:
            dpkg = yaml.safe_load(dpkg_f)

        # Download archive/esm packages files
        series = series_map.get(core_series)
        url_tmpl = 'http://archive.ubuntu.com/ubuntu/dists/' + series + \
            '{}/{}/binary-amd64/Packages.gz'
        urls = []
        pkg_files = []
        for suite in '', '-updates', '-security':
            for comp in 'main', 'restricted', 'universe', 'multiverse':
                urls.append(url_tmpl.format(suite, comp))
                pkg_files.append('-'.join([series, suite, comp,
                                           'packages.gz']))
        # ESM categories:
        # infra-security,infra-updates,apps-updates,apps-security
        # Reference: https://github.com/canonical/se-misc/tree/main/esmadison
        # TODO fips when we consider fips snaps here
        url_tmpl = 'https://esm.ubuntu.com/{}/' + \
            'ubuntu/dists/{}/main/binary-amd64/Packages.gz'
        for cat in 'infra', 'apps':
            for pocket in 'security', 'updates':
                suite = '-'.join([series, cat, pocket])
                urls.append(url_tmpl.format(cat, suite))
                pkg_files.append('-'.join([suite, 'packages.gz']))

        # PPAs used in the build
        ppas = []
        core_version = 16
        if core_series != '':
            core_version = int(core_series)
        # ucdev has packages only for 20 and 22
        if core_version == 20 or core_version == 22:
            ppas.append('ucdev/base-ppa')
        # The ice patch in cryptutils is only in 22
        # TODO should this be ported to 24?
        if core_version == 22:
            ppas.append('ubuntu-security/fde-ice')
        # snappy-dev was used for core and again 24+
        if core_version == 16 or core_version >= 24:
            ppas.append('snappy-dev/image')
        series = series_map[core_series]
        for ppa in ppas:
            urls.append('https://ppa.launchpadcontent.net/' + ppa +
                        '/ubuntu/dists/' + series +
                        '/main/binary-amd64/Packages.gz')
            pkg_files.append('-'.join([ppa.replace('/', '-'), 'packages.gz']))

        snap2version = {}
        for i, url in enumerate(urls):
            pkg_file = os.path.join(base_tmpd, pkg_files[i])
            tries = 3
            for i in range(tries):
                try:
                    print('downloading {}'.format(url))
                    urllib.request.urlretrieve(url, pkg_file)
                    break
                except Exception as e:
                    if i == tries - 1:
                        raise e
                    print('while downloading: ' + str(e) + ' - retrying')
            package_versions_from_file(pkg_file, snap2version)

        # On 20 and 22 these packages are built by the snap and not pulled from
        # the archive.
        built_by_snap = ['console-conf', 'probert-common',
                         'probert-network', 'subiquitycore']
        # Look out for changes. We could return on first change, but we'll
        # print all changes for the moment for debugging purposes.
        for pkg in dpkg['packages']:
            [pkgName, pkgVersion] = pkg.split('=')
            # pkgName can have a :<arch> suffix, like :amd64 or :i386. The few
            # i386 packages that are in the manifest are also present as amd64
            # packages, so we do not worry with filtering.
            pkgName = pkgName.split(':')[0]
            if pkgName in built_by_snap and (
                    core_version == 20 or core_version == 22):
                continue
            if pkgName not in snap2version:
                print('unexpected error, package {} from {} '
                      'not found in the archive'.format(pkgName, base))
                sys.exit(1)
            if apt_pkg.version_compare(pkgVersion, snap2version[pkgName]) < 0:
                print('change in {}: {} package version updated ({} -> {})'.
                      format(base, pkgName, pkgVersion, snap2version[pkgName]))
                changed = True

    return changed


# We know that the branch has changed if there are no date tags in HEAD. If we
# find one, branch changes are already in some published snap.
def check_branch_changed(branch):
    gtag = subprocess.run(['git', 'tag', '--points-at', 'HEAD'],
                          check=True, stdout=subprocess.PIPE)
    # We expect <date><month><day> with an optional dash followed by a number,
    # followed by _<branch>: 20250510_<branch>, 20263001_<branch>,
    # 20250510-1_<branch> and 20263001-2_<branch> are valid.
    date_re = re.compile(r'^[0-9]{8}(-[0-9]+)?_' + re.escape(branch) + r'$')
    for tag in gtag.stdout.splitlines():
        tag = tag.decode("utf-8")
        print('found tag', tag)
        if re.match(date_re, tag):
            return False

    print('no date tag found, triggering build')
    return True


# Get tag that we will use in the build.
def get_build_tag(branch):
    today = datetime.today().strftime('%Y%m%d')
    gtag = subprocess.run(['git', 'tag', '--points-at', 'HEAD'],
                          check=True, stdout=subprocess.PIPE)
    # We expect <today> followed by an optional dash and sequence number and by
    # _<branch>.
    date_re = re.compile(
        rf'^{re.escape(today)}(-[0-9]+)?_{re.escape(branch)}$')
    last_seq = 0
    today_found = False
    for tag in gtag.stdout.splitlines():
        tag = tag.decode("utf-8")
        m = re.match(date_re, tag)
        if m:
            today_found = True
            if m.group(1) != '':
                # Remove the dash and get sequence number
                seq = int(m.group(1)[1:])
                if seq > last_seq:
                    last_seq = seq

    if today_found:
        return today + '-' + str(last_seq + 1) + '_' + branch

    return today + '_' + branch


def is_build_running(snap):
    if len(snap.pending_build_requests) > 0:
        print('A {} snap build request is pending, skipping.'.format(
            snap.name))
        return True
    for build in snap.pending_builds:
        if build.buildstate in ('Needs building', 'Currently building'):
            print('A {} snap build is in progress.'.format(
                snap.name))
            return True
    print('No {} snap build pending or currently running.'.format(
        snap.name))
    return False


# builds is a collection of snap_build objects
def download_snaps(lp, builds, output_dir):
    for b in builds.entries:
        build = lp.load(b['self_link'])
        urls = build.getFileUrls()
        if len(urls) == 0:
            print('ERROR: no built files found')
            return False
        snap_found = False
        for url in urls:
            if not url.endswith('.snap'):
                continue
            _logger.debug('Downloading snap from {}'.format(url))
            snap_file = url.rsplit('/', 1)[-1]
            snap_path = os.path.join(output_dir, snap_file)
            urllib.request.urlretrieve(url, snap_path)
            snap_found = True
        if not snap_found:
            print('No snap found after finishing build in {}'.format(url))
            return False

    return True


# Builds in lp the snap recipe and downloads the built snaps to output_dir,
# unless dry_run is set, as in that case the function only prints a message.
# Returns success of the operation.
def build_and_download(lp, snap, branch, output_dir, dry_run):
    tag = get_build_tag(branch)
    if dry_run:
        print('Would trigger new snap builds for {}, with tag {}.'.format(
            snap.name, tag))
        return True
    print('Triggering new snap build of {}, with tag {}.'.format(
        snap.name, tag))
    subprocess.run(['git', 'tag', tag], check=True)
    subprocess.run(['git', 'push', 'origin', tag], check=True)

    # We use all the defaults of the snap recipe
    request = snap.requestBuilds(
        archive=snap.auto_build_archive_link,
        pocket=snap.auto_build_pocket,
        channels=snap.auto_build_channels)

    # Wait for the builds to be launched
    print('builds requested:', request.builds_collection_link)
    while True:
        # We always want a first reload as initial status is always Pending
        request = lp.load(request.self_link)
        if request.status == 'Pending':
            time.sleep(10)
            continue

        if request.status == 'Failed':
            print('Cannot start builds, request failed')
            sys.exit(1)
        # Must be 'Completed'
        print('Request builds sucessful')
        break

    # Waiting for the builds to finish
    while True:
        builds = lp.load(request.builds_collection_link)
        wait = False
        for b in builds.entries:
            if b['buildstate'] in ['Needs building',
                                   'Dependency wait',
                                   'Currently building',
                                   'Uploading build',
                                   'Cancelling build',
                                   'Gathering build output']:
                wait = True
                break

        # Poll once per minute
        if wait:
            time.sleep(60)
            continue

        # No build pending
        break

    # Check state
    success = True
    for b in builds.entries:
        if b['buildstate'] != 'Successfully built':
            print('Error for {}: {} ({})'.format(
                b['title'], b['buildstate'], b['web_link']))
            success = False

    if success:
        success = download_snaps(lp, builds, output_dir)

    if not success:
        _logger.debug('Removing tag {}'.format(tag))
        subprocess.run(['git', 'push', 'origin', ':'+tag], check=True)
        subprocess.run(['git', 'tag', '--delete', tag], check=True)

    return success


def main():
    parser = ArgumentParser()

    parser.add_argument('core_series')
    parser.add_argument(
        '-d', '--debug', dest='debug', action='store_true')
    parser.add_argument(
        '-c', '--credentials', dest='lp_credentials',
        default='.lp_credentials')
    parser.add_argument(
        '--output-dir', dest='output_dir', default='')
    parser.add_argument(
        '--no-git-check', dest='no_git_check', action='store_true')
    parser.add_argument(
        '--dry-run', dest='dry_run', action='store_true')

    args = parser.parse_args()

    apt_pkg.init_system()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    if args.lp_credentials:
        args.lp_credentials = os.path.expanduser(args.lp_credentials)

    if args.dry_run:
        lp = Launchpad.login_anonymously(
            'core-builder', 'production', version='devel')
    else:
        def login_f(creds_f):
            return Launchpad.login_with(
                'core-builder', 'production', version='devel',
                credentials_file=creds_f)
        creds_env = os.environ.get("LP_CREDENTIALS")
        if creds_env and creds_env != '':
            _logger.debug("using credentials from LP_CREDENTIALS env var")
            with tempfile.NamedTemporaryFile() as credential_store_path:
                credential_store_path.write(creds_env.encode("utf-8"))
                credential_store_path.flush()
                lp = login_f(credential_store_path.name)
        else:
            _logger.debug("no LP_CREDENTIALS environment variable")
            if not os.path.exists(args.lp_credentials):
                print('Credentials not found, no LP_CREDENTIALS var or file')
                sys.exit(1)
            lp = login_f(args.lp_credentials)

    print('Checking core{}'.format(args.core_series))

    if args.core_series not in series_map:
        print('Invalid core series.  Only %s are supported.' %
              (', '.join(series_map.keys())))
        return 1

    recipe_tmpl = SNAP_API
    if args.core_series == '':
        recipe_tmpl = CORE_SNAP_API
    recipe = recipe_tmpl.format(args.core_series)
    print('building snap recipe', recipe)
    snap = lp.load(recipe)
    # Move on only if the snap is not building already
    if is_build_running(snap):
        return 0

    # Archive downloads can be a bit flaky, use a timeout so we do not need to
    # wait too much to do a retry. See check_packages_changed retry code.
    socket.setdefaulttimeout(60)

    branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                            check=True, stdout=subprocess.PIPE)

    # policies are called to determine if we need to trigger a build
    policies = []
    if not args.no_git_check:
        policies.append(lambda: check_branch_changed(branch))
    policies.append(lambda: check_packages_changed(args.core_series))

    ret = 0
    for policy in policies:
        # Go through all policies
        if not policy():
            continue

        if not build_and_download(lp, snap, branch,
                                  args.output_dir, args.dry_run):
            ret = 1
        break

    return ret


if __name__ == '__main__':
    sys.exit(main())
