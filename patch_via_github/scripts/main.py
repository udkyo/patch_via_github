#!/usr/bin/env python3

"""
Basic program to apply a set of patches from various GitHub pull requests
based on user criteria to a repo sync area

The current logic is to apply the patches in order of the PR numbers
(e.g. org/repo#123) once all relevant PRs have been determined
"""

import argparse
import configparser
import contextlib
import logging
import os
import os.path
import re
import subprocess
import sys
import xml.etree.ElementTree as EleTree

import requests
import requests.exceptions

from importlib.metadata import version
from shutil import which


logger = logging.getLogger('patch_via_github')


class InvalidUpstreamException(Exception):
    def __init__(self, project):
        self.project = project
        self.message = (
            f'Project {project} has an invalid upstream in manifest'
            ' - is it locked to a sha?'
        )
        super().__init__(self.message)


def print_divider():
    """Print a visual divider line"""
    print("=" * 80)


def default_ini_file():
    """
    Returns a string path to the default patch_via_github.ini
    """
    return os.path.join(
        os.path.expanduser('~'), '.ssh', 'patch_via_github.ini'
    )


class ParseCSVs(argparse.Action):
    """Parse comma separated lists"""

    def __call__(self, parser, namespace, arguments, option_string=None):
        """
        Ensure all values separated by commas are parsed out; note that
        while quoted strings with spaces are preserved, a comma within
        quoted strings does NOT preserve
        """

        results = []

        for arg in arguments:
            for value in arg.split(','):
                if len(value) > 0:
                    results.append(value)

        setattr(namespace, self.dest, results)


class GitHubPR:
    """Encapsulation of relevant information for a given GitHub pull request"""

    def __init__(self, data, use_ssh=False):
        """
        Initialize with key information for a GitHub PR
        """
        self.number = str(data['number'])
        self.state = data['state']
        self.title = data['title']
        self.repo_full_name = data['base']['repo']['full_name']
        self.project = data['base']['repo']['name']
        self.branch = data['base']['ref']
        self.head_sha = data['head']['sha']
        self.head_ref = data['head']['ref']
        self.html_url = data['html_url']
        self.labels = [label['name'] for label in data.get('labels', [])]

        if use_ssh:
            repo_url = data['base']['repo']['ssh_url']
        else:
            repo_url = data['base']['repo']['clone_url']
        self.fetch_command = [
            'git', 'fetch', repo_url, f'pull/{self.number}/head'
        ]
        self.cherry_pick_command = ['git', 'cherry-pick', 'FETCH_HEAD']
        self.checkout_command = ['git', 'checkout', 'FETCH_HEAD']


class GitHubPatches:
    """
    Determine all relevant patches to apply to a repo sync based on
    a given set of initial parameters, which can be a set of one of
    the following:
        - PR references (org/repo#number or repo#number)
        - labels (org/repo:label)

    The resulting data will include the necessary patch commands to
    be applied to the repo sync
    """

    GITHUB_API_URL = 'https://api.github.com'

    def __init__(self, token, default_org=None, checkout=False, use_ssh=True):
        """Initial GitHub connection and set base options"""

        self.token = token
        self.default_org = default_org
        self.checkout = checkout
        self.use_ssh = use_ssh
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        if token:
            self.session.headers['Authorization'] = f'Bearer {token}'

        # We need to track PRs which were specifically requested as these
        # are applied regardless of their state. Derived PRs are only
        # applied if they are still open
        self.requested_prs = []
        # We track what's been applied to ensure at least the PRs we
        # specifically requested got done
        self.applied_prs = []
        # Track what was originally requested (for reporting)
        self.request_type = None  # 'pr' or 'label'
        self.request_values = []  # The actual values requested
        # Manifest project name
        self.manifest = None
        # The manifest is only read from disk if manifest_stale is true
        self.manifest_stale = True
        self.manifest_project = 'manifest'
        self.ignore_manifest = False
        self.only_manifest = False
        self.force_check_applied = False
        self.sha_re = re.compile(r'[0-9a-f]{40}')

    @classmethod
    def from_config_file(cls, config_path, default_org=None, checkout=False,
                         use_ssh=True):
        """
        Factory method: construct a GitHubPatches from the path to a
        config file
        """
        if not os.path.exists(config_path):
            logger.error(f'Configuration file {config_path} missing!')
            sys.exit(1)

        config = configparser.ConfigParser()
        config.read(config_path)

        if 'main' not in config.sections():
            logger.error(
                f'Invalid config file "{config_path}" '
                '(missing "main" section)'
            )
            sys.exit(1)

        try:
            token = config.get('main', 'token')
        except configparser.NoOptionError:
            logger.error(
                'Required option "token" is missing from the config '
                'file.  Aborting...'
            )
            sys.exit(1)

        org = default_org
        if not org:
            with contextlib.suppress(configparser.NoOptionError):
                org = config.get('main', 'default_org')

        ssh = use_ssh
        with contextlib.suppress(configparser.NoOptionError):
            ssh = config.getboolean('main', 'ssh')

        return cls(token, org, checkout, ssh)

    def set_only_manifest(self, only_manifest):
        self.only_manifest = only_manifest

    def set_ignore_manifest(self, ignore_manifest):
        self.ignore_manifest = ignore_manifest

    def _api_get(self, endpoint):
        """Make a GET request to the GitHub API"""

        url = f'{self.GITHUB_API_URL}{endpoint}'
        logger.debug(f'  API GET: {url}')
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            raise RuntimeError(f'GitHub API error: {exc}')

    def _api_get_paginated(self, endpoint):
        """Make paginated GET requests to the GitHub API"""

        results = []
        url = f'{self.GITHUB_API_URL}{endpoint}'
        while url:
            logger.debug(f'  API GET: {url}')
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                results.extend(response.json())
                url = response.links.get('next', {}).get('url')
            except requests.exceptions.HTTPError as exc:
                raise RuntimeError(f'GitHub API error: {exc}')
        return results

    def get_project_path_and_branch_from_manifest(self, project):
        branch = None
        path = None
        if self.manifest_stale:
            repo_tool = which("repo")
            if repo_tool is None:
                logger.error("'repo' tool not found on PATH")
                sys.exit(1)
            manifest_str = subprocess.check_output(
                [repo_tool, "manifest"]
            )
            self.manifest = EleTree.fromstring(manifest_str)
            self.manifest_stale = False

        proj_info = self.manifest.find(
            f'.//project[@name="{project}"]'
        )
        if proj_info is not None:
            path = proj_info.attrib.get('path', project)
            branch = proj_info.attrib.get('revision')
            if branch is None:
                default = self.manifest.find(".//default")
                if default is not None:
                    branch = default.attrib.get('revision')
        return (path, branch)

    def parse_pr_reference(self, pr_ref):
        """
        Parse a PR reference into (org, repo, number).
        Supports formats:
            - org/repo#number
            - repo#number (uses default_org)
        """

        # Full format: org/repo#number
        match = re.match(r'^([^/]+)/([^#]+)#(\d+)$', pr_ref)
        if match:
            return match.group(1), match.group(2), match.group(3)

        # Short format: repo#number
        match = re.match(r'^([^#/]+)#(\d+)$', pr_ref)
        if match:
            if not self.default_org:
                logger.error(
                    f'PR reference "{pr_ref}" needs an org, but no '
                    'default_org configured. Use org/repo#number format '
                    'or set default_org in config.'
                )
                sys.exit(1)
            return self.default_org, match.group(1), match.group(2)

        logger.error(
            f'Invalid PR reference: "{pr_ref}". '
            'Use format: org/repo#number or repo#number'
        )
        sys.exit(1)

    def get_pr(self, org, repo, number):
        """Fetch a single PR from GitHub API"""

        logger.debug(f'Fetching PR {org}/{repo}#{number}')
        data = self._api_get(f'/repos/{org}/{repo}/pulls/{number}')
        return GitHubPR(data, use_ssh=self.use_ssh)

    def get_open_prs_by_label(self, org, repo, label):
        """Fetch all open PRs with a given label"""

        logger.debug(
            f'Fetching open PRs with label "{label}" from {org}/{repo}'
        )
        data = self._api_get_paginated(
            f'/repos/{org}/{repo}/pulls?state=open&per_page=100'
        )
        prs = {}
        for pr_data in data:
            pr_labels = [lbl['name'] for lbl in pr_data.get('labels', [])]
            if label in pr_labels:
                pr = GitHubPR(pr_data, use_ssh=self.use_ssh)
                prs[pr.number] = pr
        return prs

    def resolve_prs(self, pr_refs, id_type):
        """
        From an initial set of PR references or labels, determine all
        relevant open PRs that will need to be applied to a repo sync
        via patching
        """
        all_prs = dict()

        if id_type == 'pr':
            for pr_ref in pr_refs:
                org, repo, number = self.parse_pr_reference(pr_ref)
                pr = self.get_pr(org, repo, number)

                if pr.number in all_prs:
                    continue

                if (pr.state != 'open'
                        and pr.number not in self.requested_prs):
                    logger.info(
                        f'  Skipping {org}/{repo}#{number} - '
                        f'state is {pr.state} and not explicitly requested'
                    )
                    continue

                all_prs[pr.number] = pr

        elif id_type == 'label':
            for label_ref in pr_refs:
                # label_ref format: org/repo:label
                match = re.match(r'^([^/]+)/([^:]+):(.+)$', label_ref)
                if match:
                    org = match.group(1)
                    repo = match.group(2)
                    label = match.group(3)
                    prs = self.get_open_prs_by_label(org, repo, label)
                    all_prs.update(prs)
                else:
                    logger.error(
                        f'Invalid label reference: "{label_ref}". '
                        'Use format: org/repo:label'
                    )
                    sys.exit(1)

        # Filter out PRs for branches not matching manifest
        for pr_num, pr in list(all_prs.items()):
            (_, manifest_branch) = \
                self.get_project_path_and_branch_from_manifest(pr.project)
            if (manifest_branch
                    and pr_num not in self.requested_prs
                    and pr.branch != manifest_branch
                    and not self.sha_re.match(manifest_branch)):
                logger.info(
                    f"  Ignoring {pr.repo_full_name}#{pr.number} because "
                    f"it targets {pr.branch}, manifest branch is "
                    f"{manifest_branch}"
                )
                del all_prs[pr_num]

        logger.info('Final list of PRs to apply: {}'.format(
            ', '.join([
                f'{pr.repo_full_name}#{pr_num}'
                for pr_num, pr in all_prs.items()
            ])
        ))

        return all_prs

    def check_requested_prs_applied(self):
        """
        Verify that all explicitly-requested PRs were applied.
        If not, exit with an error.
        """

        if self.requested_prs and any(
            item not in self.applied_prs
            for item in self.requested_prs
        ):
            logger.critical(
                f"Failed to apply all explicitly-requested PRs! "
                f'Requested: {self.requested_prs} '
                f'Applied: {self.applied_prs}'
            )
            sys.exit(1)
        elif self.applied_prs:
            if self.requested_prs:
                logger.info(
                    f"All explicitly-requested PRs applied! "
                    f'Requested: {self.requested_prs} '
                    f'Applied: {self.applied_prs}'
                )
            elif self.request_type and self.request_values:
                if self.request_type == 'label':
                    labels_str = ', '.join(
                        [f"'{l}'" for l in self.request_values]
                    )
                    logger.info(
                        f"Applied PRs from label(s) {labels_str}: "
                        f"{self.applied_prs}"
                    )
            else:
                logger.info(f"Applied PRs: {self.applied_prs}")

    def apply_single_pr(self, pr, proj_path):
        """
        Given a single PR object and a path, apply the git change to
        that path (using either checkout or cherry-pick as requested).
        """

        if not os.path.exists(proj_path):
            logger.critical(
                f'***** Project {pr.project} missing on disk! '
                f'Expected to be in {proj_path}'
            )
            sys.exit(5)

        logger.info(
            f'***** Applying {pr.html_url} to project {pr.project}:'
        )
        try:
            subprocess.run(
                pr.fetch_command, cwd=proj_path,
                capture_output=True, text=True, check=True
            )
            if self.checkout:
                subprocess.run(
                    pr.checkout_command, cwd=proj_path,
                    capture_output=True, text=True, check=True
                )
            else:
                subprocess.run(
                    pr.cherry_pick_command, cwd=proj_path,
                    capture_output=True, text=True, check=True
                )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f'Patch for PR {pr.repo_full_name}#{pr.number} '
                f'failed: {exc.stderr}'
            )
        logger.info(
            f'***** Done applying PR {pr.repo_full_name}#{pr.number} '
            f'to project {pr.project}\n'
        )
        self.applied_prs.append(pr.number)

    def apply_manifest_prs(self, prs):
        """
        Digs out and applies all changes to the 'manifest' project.
        Re-runs "repo sync" if any such changes found.
        """

        logger.info("Looking for PRs on the 'manifest' project...")
        manifest_changes_found = False
        for pr_num in sorted(prs.keys()):
            pr = prs[pr_num]
            if pr.project == self.manifest_project:
                del prs[pr_num]
                manifest_changes_found = True
                self.apply_single_pr(
                    pr,
                    os.path.join(".repo", "manifests")
                )

        if manifest_changes_found:
            self.manifest_stale = True
            repo_tool = which("repo")
            if repo_tool is None:
                logger.error("'repo' tool not found on PATH")
                sys.exit(1)
            subprocess.check_call([repo_tool, "sync", "--jobs=4"])

    def apply_non_manifest_prs(self, prs):
        """
        Applies all changes NOT to the 'manifest' project.
        """

        logger.info("Looking for PRs to non-manifest projects...")
        for pr_num in sorted(prs.keys()):
            pr = prs[pr_num]
            if pr.project == self.manifest_project:
                if self.ignore_manifest:
                    logger.debug(
                        f"Ignoring PR {pr_num} for 'manifest' project"
                    )
                    continue
                logger.fatal(
                    f"Found PR {pr_num} for 'manifest' project - "
                    "should not happen at this stage!"
                )
                sys.exit(5)

            (path, branch) = \
                self.get_project_path_and_branch_from_manifest(pr.project)

            if (path, branch) == (None, None):
                logger.info(
                    f"***** NOTE: ignoring PR "
                    f"{pr.repo_full_name}#{pr_num} for project "
                    f"{pr.project} that is either not part of the "
                    f"manifest, or was excluded due to manifest "
                    f"group filters."
                )
                continue

            self.apply_single_pr(pr, path)

    def patch_repo_sync(self, pr_refs, id_type):
        """
        Patch the repo sync with the list of patch commands. Repo
        sync is presumed to be in current working directory.
        """

        prs = self.resolve_prs(pr_refs, id_type)

        if not self.ignore_manifest:
            self.apply_manifest_prs(prs)
        if not self.only_manifest:
            self.apply_non_manifest_prs(prs)

        if ((not self.only_manifest and not self.ignore_manifest)
                or self.force_check_applied):
            self.check_requested_prs_applied()


def main():
    """
    Parse the arguments, verify the repo sync exists, read and validate
    the configuration file, then determine all the needed GitHub PR
    patches and apply them to the repo sync
    """

    # PyInstaller binaries get LD_LIBRARY_PATH set for them, and that
    # can have unwanted side-effects for our subprocesses.
    os.environ.pop("LD_LIBRARY_PATH", None)

    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)

    version_string = (
        f"patch_via_github version {version('patch-via-github')}"
    )
    default_config_file = default_ini_file()
    parser = argparse.ArgumentParser(
        description='Patch repo sync with requested GitHub pull requests'
    )
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Enable debugging output')
    parser.add_argument('-c', '--config', dest='github_config',
                        help='Configuration file for patching via GitHub',
                        default=default_config_file)
    change_group = parser.add_mutually_exclusive_group(required=True)
    change_group.add_argument(
        '-p', '--pull-request', dest='pull_requests', nargs='+',
        action=ParseCSVs,
        help='Pull request references to apply (comma-separated). '
             'Format: org/repo#number or repo#number (with -o)')
    change_group.add_argument(
        '-l', '--label', dest='labels', nargs='+',
        action=ParseCSVs,
        help='Labels to search for open PRs (comma-separated). '
             'Format: org/repo:label')
    parser.add_argument('-o', '--default-org', dest='default_org',
                        help='Default GitHub organization for short-form '
                             'PR references (repo#number)')
    manifest_group = parser.add_mutually_exclusive_group(required=False)
    manifest_group.add_argument('--ignore-manifest', action='store_true',
                                help='Do not apply any changes to '
                                     '"manifest" repo')
    manifest_group.add_argument('--only-manifest', action='store_true',
                                help='Apply only changes to '
                                     '"manifest" repo')
    parser.add_argument('-s', '--source', dest='repo_source',
                        help='Location of the repo sync checkout',
                        default='.')
    parser.add_argument('-C', '--checkout', action='store_true',
                        help='When specified, checkout the PR head '
                             'rather than cherry-picking')
    parser.add_argument('--no-ssh', dest='use_ssh',
                        action='store_false', default=True,
                        help='Use HTTPS URLs for git fetch instead of '
                             'SSH (also configurable via ini: ssh = false)')
    parser.add_argument('-V', '--version', action='version',
                        help='Display patch_via_github version information',
                        version=version_string)
    args = parser.parse_args()

    if args.debug:
        handler.setLevel(logging.DEBUG)

    if not os.path.isdir(args.repo_source):
        logger.error(
            "Path for repo sync checkout doesn't exist.  Aborting..."
        )
        sys.exit(1)
    os.chdir(args.repo_source)

    if args.pull_requests:
        id_type = 'pr'
        raw_values = args.pull_requests
    else:
        id_type = 'label'
        raw_values = args.labels

    logger.info(f"******** {version_string} ********")
    print_divider()
    github_patches = GitHubPatches.from_config_file(
        args.github_config, args.default_org, args.checkout, args.use_ssh
    )
    if args.only_manifest:
        github_patches.set_only_manifest(True)
    elif args.ignore_manifest:
        github_patches.set_ignore_manifest(True)

    if id_type == 'pr':
        github_patches.requested_prs = [
            github_patches.parse_pr_reference(ref)[2]
            for ref in raw_values
        ]

    github_patches.request_type = id_type
    github_patches.request_values = raw_values

    logger.info(
        f"Initial request to patch {id_type}s: "
        f"{', '.join(raw_values)}"
    )
    github_patches.patch_repo_sync(raw_values, id_type)

    print_divider()


if __name__ == '__main__':
    try:
        main()
    except InvalidUpstreamException as e:
        print(e)
        sys.exit(1)
