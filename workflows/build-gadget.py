#!/usr/bin/env python3
"""
Build gadget snaps on Launchpad using the snap recipes.

The gadgets are built by static recipes owned by ~ubuntu-core-service. They must
be built here, due to both pro-enablement, but also due to policies.
"""

import os
import sys
import tempfile
import time
from argparse import ArgumentParser
from collections import namedtuple
from launchpadlib.launchpad import Launchpad
from lazr.restfulclient.errors import HTTPError
import se_utils

LP_API = 'https://api.launchpad.net/devel/'
SNAP_BASE_API = LP_API + '+snap-bases/{}'
DISTRO_ARCH_SERIES_API = LP_API + 'ubuntu/{}/{}'
OWNER = '~ubuntu-core-service/'

# The Ubuntu series that the Ubuntu Core series are built on
UBUNTU_SERIES = {
    '16': 'xenial',
    '18': 'bionic',
    '20': 'focal',
    '22': 'jammy',
    '24': 'noble',
    '26': 'resolute',
}

# States of the builds that have not finished yet
PENDING_STATES = (
    'Needs building',
    'Dependency wait',
    'Currently building',
    'Uploading build',
    'Cancelling build',
    'Gathering build output',
)

# Seconds between checks of the builds, and how many consecutive checks can
# fail (Launchpad timeouts) before giving up
POLL_INTERVAL = 60
MAX_POLL_ERRORS = 10

# A Launchpad recipe building a gadget for an Ubuntu Core series, and the store
# track that the snaps it builds are released to. The path is relative to the
# Launchpad API root.
Recipe = namedtuple(
    'Recipe', ['snap', 'series', 'track', 'path', 'architectures'])

# The supported recipes. A series has more than one recipe when they build
# different architectures.
RECIPES = (
    Recipe('pc', '26', '26', OWNER + '+snap/pc-26', ('amd64', 'arm64')),
    Recipe('pc', '24', '24', OWNER + '+snap/pc-24', ('amd64', 'arm64')),
    Recipe('pc', '22', '22', OWNER + '+snap/pc-22', ('amd64', 'arm64')),
    Recipe('pc', '20', '20', OWNER + '+snap/pc-amd64-20', ('amd64',)),
    Recipe('pc', '18', '18', OWNER + '+snap/pc-amd64-18', ('amd64',)),
    Recipe('pc', '18', '18', OWNER + '+snap/pc-i386-18', ('i386',)),
    Recipe('pc', '16', 'latest', OWNER + '+snap/pc-amd64', ('amd64',)),
    Recipe('pc', '16', 'latest', OWNER + '+snap/pc-i386', ('i386',)),
    Recipe('pi', '26', '26', OWNER + '+snap/pi-26', ('arm64',)),
    Recipe('pi', '24', '24', OWNER + 'snap-pi/+snap/pi-24', ('arm64',)),
    Recipe('pi', '22', '22', OWNER + '+snap/pi-arm64-22', ('arm64',)),
    Recipe('pi', '22', '22', OWNER + '+snap/pi-armhf-22', ('armhf',)),
    Recipe('pi', '20', '20', OWNER + '+snap/pi-arm64-20', ('arm64',)),
    Recipe('pi', '20', '20', OWNER + '+snap/pi-armhf-20', ('armhf',)),
    Recipe('pi', '18', '18-pi', OWNER + '+snap/pi-arm64', ('arm64',)),
    Recipe('pi', '18', '18-pi', OWNER + '+snap/pi', ('armhf',)),
)


class BuildError(Exception):
    pass


def get_series(base):
    """Return the Ubuntu Core series of a gadget base (core is series 16)."""
    if base == 'core':
        return '16'
    series = base[len('core'):] if base.startswith('core') else None
    if series not in UBUNTU_SERIES:
        raise BuildError('Unsupported base {} (supported bases: core, {})'
                         .format(base, ', '.join(
                             'core' + s for s in UBUNTU_SERIES if s != '16')))
    return series


def get_snap_base(series):
    """Return the name of the Launchpad snap base for a series."""
    return 'core' if series == '16' else 'core' + series


def get_recipes(snap_name, series):
    """Return the recipes that build a gadget for a series."""
    recipes = [r for r in RECIPES if r.snap == snap_name and r.series == series]
    if recipes:
        return recipes

    gadgets = sorted({r.snap for r in RECIPES})
    if snap_name not in gadgets:
        raise BuildError('There are no Launchpad recipes for the {} gadget '
                         '(supported gadgets: {})'
                         .format(snap_name, ', '.join(gadgets)))
    supported = sorted({r.series for r in RECIPES if r.snap == snap_name},
                       key=int)
    raise BuildError('There are no Launchpad recipes for the {} gadget for '
                     'series {} (supported series: {})'
                     .format(snap_name, series, ', '.join(supported)))


def parse_architectures(architectures):
    parsed = []
    for arch in architectures.split(','):
        arch = arch.strip()
        if arch and arch not in parsed:
            parsed.append(arch)
    return parsed


def select_architectures(recipes, architectures):
    """Return a (recipe, architectures) tuple for the recipes that build the
    requested architectures."""
    supported = [a for r in recipes for a in r.architectures]
    unsupported = [a for a in architectures if a not in supported]
    if unsupported:
        raise BuildError('There is no Launchpad recipe for the {} gadget for '
                         'series {} that builds {} (supported architectures: '
                         '{})'.format(recipes[0].snap, recipes[0].series,
                                      ', '.join(unsupported),
                                      ', '.join(supported)))

    selection = []
    for recipe in recipes:
        archs = [a for a in architectures if a in recipe.architectures]
        if archs:
            selection.append((recipe, archs))
    return selection


def plan_builds(lp, recipes, architectures, branch):
    """Return a (launchpad snap, architectures) tuple for each of the recipes
    to build.

    The requested architectures are built, or all the architectures of the
    recipes that build the branch if none are requested. As the recipes build
    a fixed branch, it is an error to ask for a branch they do not build.
    """
    if architectures:
        selection = select_architectures(recipes, architectures)
    else:
        selection = [(r, list(r.architectures)) for r in recipes]

    plan = []
    other_branches = []
    for recipe, archs in selection:
        snap = lp.load(LP_API + recipe.path)
        if branch and snap.git_path != 'refs/heads/' + branch:
            if architectures:
                raise BuildError(
                    'Launchpad recipe {} builds {} and not branch {}'
                    .format(recipe.path, snap.git_path, branch))
            other_branches.append('{} builds {}'.format(
                recipe.path, snap.git_path))
            continue

        enabled = [processor.name for processor in snap.processors]
        disabled = [a for a in archs if a not in enabled]
        if disabled:
            raise BuildError(
                'Architectures {} are not enabled in Launchpad recipe {} '
                '(enabled: {})'.format(', '.join(disabled), recipe.path,
                                       ', '.join(enabled)))
        plan.append((snap, archs))

    if not plan:
        raise BuildError('No Launchpad recipe builds branch {}: {}'.format(
            branch, '; '.join(other_branches)))
    return plan


def get_source(snap):
    """Return the URL of the repository that a recipe builds."""
    if snap.git_repository_url:
        return snap.git_repository_url
    # The repositories hosted on Launchpad are only linked by their API URL
    return snap.git_repository_link.replace(
        LP_API, 'https://code.launchpad.net/')


def print_plan(plan):
    for snap, archs in plan:
        print('Launchpad recipe {}: {}'.format(
            snap.web_link, ', '.join(archs)))
        print('    builds {} from {}'.format(snap.git_path, get_source(snap)))


def is_build_running(snap):
    """Return whether builds are already pending on a recipe, for example the
    ones that Launchpad requests when the source changes."""
    return len(snap.pending_build_requests) > 0 or any(
        build.buildstate in ('Needs building', 'Currently building')
        for build in snap.pending_builds)


def request_builds(lp, snap, series, architectures, snapcraft_channel):
    """Request a build of the recipe for each architecture."""
    snap_base_link = SNAP_BASE_API.format(get_snap_base(series))
    snap_base = lp.load(snap_base_link)
    # Start with the snap base defaults and apply the recipe overrides on top
    # of them, as Launchpad does for the automatic builds
    channels = dict(snap_base.build_channels or {})
    channels.update(snap.auto_build_channels or {})
    channels_by_arch = channels.pop('_byarch', {})

    builds = []
    for architecture in architectures:
        arch_channels = dict(channels)
        arch_channels.update(channels_by_arch.get(architecture, {}))
        if snapcraft_channel:
            arch_channels['snapcraft'] = snapcraft_channel
        distro_arch_series = lp.load(DISTRO_ARCH_SERIES_API.format(
            UBUNTU_SERIES[series], architecture))
        try:
            build = snap.requestBuild(
                archive=snap.auto_build_archive_link,
                distro_arch_series=distro_arch_series,
                pocket=snap.auto_build_pocket,
                channels=arch_channels,
                snap_base=snap_base_link)
        except HTTPError as ex:
            raise BuildError('Cannot request a {} build on {}: {}'.format(
                architecture, snap.web_link, ex))
        print('Arch: {} is building under: {}'.format(
            architecture, build.web_link))
        builds.append(build)
    return builds


def wait_for_builds(lp, builds):
    """Wait for the builds to finish and return their final state."""
    states = {}
    errors = 0
    while True:
        try:
            builds = [lp.load(build.self_link) for build in builds]
            errors = 0
        except Exception as ex:
            # Launchpad can time out or be briefly unavailable
            errors += 1
            if errors >= MAX_POLL_ERRORS:
                raise
            print('Could not get the state of the builds ({}/{}): {}'.format(
                errors, MAX_POLL_ERRORS, ex))
            time.sleep(POLL_INTERVAL)
            continue

        for build in builds:
            if states.get(build.self_link) != build.buildstate:
                states[build.self_link] = build.buildstate
                print('{}: {}'.format(build.title, build.buildstate))
        if not any(b.buildstate in PENDING_STATES for b in builds):
            return builds
        time.sleep(POLL_INTERVAL)


def download_builds(lp, builds, output_dir):
    for build in builds:
        if not se_utils.download_snap_build(lp, build.self_link, output_dir):
            return False
    return True


def login(dry_run, credentials_file):
    if dry_run:
        return Launchpad.login_anonymously(
            'core-builder', 'production', version='devel')

    def login_with(path):
        return Launchpad.login_with(
            'core-builder', 'production', version='devel',
            credentials_file=path)

    credentials = os.environ.get('LP_CREDENTIALS')
    if credentials:
        with tempfile.NamedTemporaryFile() as credentials_path:
            credentials_path.write(credentials.encode('utf-8'))
            credentials_path.flush()
            return login_with(credentials_path.name)
    if not os.path.exists(credentials_file):
        raise BuildError(
            'Credentials not found, no LP_CREDENTIALS var or file')
    return login_with(credentials_file)


def build_gadget(args):
    series = get_series(args.base)
    recipes = get_recipes(args.snap, series)
    if args.print_track:
        print(recipes[0].track)
        return 0

    lp = login(args.dry_run, args.lp_credentials)
    plan = plan_builds(lp, recipes, parse_architectures(args.architectures),
                       args.branch)
    print('Building the {} gadget for series {} on Launchpad'.format(
        args.snap, series))
    print_plan(plan)

    # A build that is already pending would make the request fail
    running = [snap.web_link for snap, _ in plan if is_build_running(snap)]
    if running:
        raise BuildError('Previous builds are still running on: {}'.format(
            ', '.join(running)))
    if args.dry_run:
        print('Dry run, not requesting any builds')
        return 0

    builds = []
    for snap, archs in plan:
        builds.extend(request_builds(
            lp, snap, series, archs, args.snapcraft_channel))
    builds = wait_for_builds(lp, builds)

    failed = False
    for build in builds:
        if build.buildstate != 'Successfully built':
            failed = True
            print('Error for {}: {} ({}), log: {}'.format(
                build.title, build.buildstate, build.web_link,
                build.build_log_url), file=sys.stderr)
        else:
            print('Built {} from revision {}'.format(
                build.title, build.revision_id))
    if failed:
        return 1
    if not download_builds(lp, builds, args.output_dir):
        return 1
    return 0


def parse_args():
    parser = ArgumentParser(
        description='Build a gadget snap on Launchpad using its '
                    'existing snap recipes.')
    parser.add_argument(
        '--snap', required=True, help='name of the gadget (pc or pi)')
    parser.add_argument(
        '--base', default='core',
        help='base of the gadget, core is used by the 16 series gadgets')
    parser.add_argument(
        '--architectures', default='',
        help='comma separated list of architectures to build, by default all '
             'the architectures that the recipes build for the branch')
    parser.add_argument(
        '--branch', default='',
        help='branch that is being built, an error is reported if the recipes '
             'do not build it (they build a fixed branch)')
    parser.add_argument(
        '--snapcraft-channel', default='',
        help='channel of the snapcraft snap used by the builds, by default '
             'the one set by the recipe')
    parser.add_argument(
        '--output-dir', default='',
        help='directory the built snaps are downloaded to')
    parser.add_argument(
        '-c', '--credentials', dest='lp_credentials',
        default='.lp_credentials',
        help='Launchpad credentials file, used if LP_CREDENTIALS is not set')
    parser.add_argument(
        '--print-track', action='store_true',
        help='print the store track the gadget is released to and exit')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='print what would be built without requesting any builds')

    args = parser.parse_args()
    if not (args.print_track or args.dry_run or args.output_dir):
        parser.error('--output-dir is required')
    return args


def main():
    args = parse_args()
    try:
        return build_gadget(args)
    except BuildError as ex:
        print('ERROR: {}'.format(ex), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
