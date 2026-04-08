import argparse
import os
import subprocess
import xml.etree.ElementTree as EleTree
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests.exceptions

import patch_via_github.scripts.main as app
from patch_via_github.tests.conftest import make_pr_api_response

TESTS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Shared fixture: GitHubPatches with test manifest pre-loaded
# ---------------------------------------------------------------------------

@pytest.fixture
def gp_with_manifest(config_file):
    """GitHubPatches instance with test manifest pre-loaded."""
    gp = app.GitHubPatches.from_config_file(config_file)
    manifest_path = TESTS_DIR / "manifest.xml"
    gp.manifest = EleTree.parse(str(manifest_path)).getroot()
    gp.manifest_stale = False
    return gp


# ===================================================================
# Utilities
# ===================================================================

class TestDefaultIniFile:
    def test_path_structure(self):
        result = app.default_ini_file()
        assert result.endswith('patch_via_github.ini')
        assert '.ssh' in result


class TestParseCSVs:
    def test_single_value(self):
        namespace = argparse.Namespace()
        action = app.ParseCSVs(None, 'dest')
        action(None, namespace, ['value1'], None)
        assert namespace.dest == ['value1']

    def test_comma_separated(self):
        namespace = argparse.Namespace()
        action = app.ParseCSVs(None, 'dest')
        action(None, namespace, ['a,b,c'], None)
        assert namespace.dest == ['a', 'b', 'c']

    def test_empty_values_stripped(self):
        namespace = argparse.Namespace()
        action = app.ParseCSVs(None, 'dest')
        action(None, namespace, ['a,,b'], None)
        assert namespace.dest == ['a', 'b']

    def test_multiple_arguments(self):
        namespace = argparse.Namespace()
        action = app.ParseCSVs(None, 'dest')
        action(None, namespace, ['a,b', 'c,d'], None)
        assert namespace.dest == ['a', 'b', 'c', 'd']


# ===================================================================
# GitHubPR
# ===================================================================

class TestGitHubPR:
    def test_init(self):
        data = make_pr_api_response(
            42, "tlm", head_sha="deadbeef", labels=["bug-fix"]
        )
        pr = app.GitHubPR(data)
        assert pr.number == "42"
        assert pr.state == "open"
        assert pr.project == "tlm"
        assert pr.repo_full_name == "couchbase/tlm"
        assert pr.branch == "master"
        assert pr.head_sha == "deadbeef"
        assert pr.labels == ["bug-fix"]
        assert "pull/42/head" in pr.fetch_command
        assert pr.cherry_pick_command == ['git', 'cherry-pick', 'FETCH_HEAD']
        assert pr.checkout_command == ['git', 'checkout', 'FETCH_HEAD']

    def test_no_labels(self):
        data = make_pr_api_response(1, "tlm")
        pr = app.GitHubPR(data)
        assert pr.labels == []

    def test_multiple_labels(self):
        data = make_pr_api_response(1, "tlm", labels=["a", "b", "c"])
        pr = app.GitHubPR(data)
        assert pr.labels == ["a", "b", "c"]

    def test_fetch_command_uses_clone_url(self):
        data = make_pr_api_response(7, "server-ui", org="myorg")
        pr = app.GitHubPR(data)
        assert "https://github.com/myorg/server-ui.git" in pr.fetch_command
        assert "pull/7/head" in pr.fetch_command


# ===================================================================
# GitHubPatches — Configuration
# ===================================================================

class TestGitHubPatchesConfig:

    def test_from_config_file(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        assert gp.default_org == "couchbase"
        assert gp.checkout is False

    def test_from_config_file_missing(self, tmp_path):
        with pytest.raises(SystemExit):
            app.GitHubPatches.from_config_file(
                str(tmp_path / "nonexistent.ini")
            )

    def test_from_config_file_bad_section(self, tmp_path):
        bad_ini = tmp_path / "bad.ini"
        bad_ini.write_text("[wrong]\nfoo = bar\n")
        with pytest.raises(SystemExit):
            app.GitHubPatches.from_config_file(str(bad_ini))

    def test_from_config_file_missing_token(self, tmp_path):
        bad_ini = tmp_path / "bad.ini"
        bad_ini.write_text("[main]\ndefault_org = couchbase\n")
        with pytest.raises(SystemExit):
            app.GitHubPatches.from_config_file(str(bad_ini))

    def test_from_config_file_with_checkout(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file, checkout=True)
        assert gp.checkout is True

    def test_from_config_file_default_org_override(self, config_file):
        gp = app.GitHubPatches.from_config_file(
            config_file, default_org="other-org"
        )
        assert gp.default_org == "other-org"

    def test_from_config_file_no_org_in_file(self, config_file_no_org):
        gp = app.GitHubPatches.from_config_file(config_file_no_org)
        assert gp.default_org is None

    def test_set_flags(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        gp.set_only_manifest(True)
        assert gp.only_manifest is True
        gp.set_ignore_manifest(True)
        assert gp.ignore_manifest is True

    def test_session_headers_with_token(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        assert 'Authorization' in gp.session.headers
        assert gp.session.headers['Authorization'].startswith('Bearer ')

    def test_session_headers_without_token(self):
        gp = app.GitHubPatches("")
        assert 'Authorization' not in gp.session.headers


# ===================================================================
# GitHubPatches — PR Reference Parsing
# ===================================================================

class TestParsePrReference:

    def test_full_format(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        org, repo, num = gp.parse_pr_reference("myorg/myrepo#123")
        assert org == "myorg"
        assert repo == "myrepo"
        assert num == "123"

    def test_short_format(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        org, repo, num = gp.parse_pr_reference("tlm#456")
        assert org == "couchbase"
        assert repo == "tlm"
        assert num == "456"

    def test_short_format_no_org_exits(self, config_file_no_org):
        gp = app.GitHubPatches.from_config_file(config_file_no_org)
        with pytest.raises(SystemExit):
            gp.parse_pr_reference("tlm#456")

    def test_invalid_format_exits(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        with pytest.raises(SystemExit):
            gp.parse_pr_reference("not-valid")

    def test_missing_number_exits(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        with pytest.raises(SystemExit):
            gp.parse_pr_reference("org/repo#")

    def test_number_only_exits(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        with pytest.raises(SystemExit):
            gp.parse_pr_reference("123")


# ===================================================================
# GitHubPatches — API Layer
# ===================================================================

class TestApiGet:

    def test_success(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 1, "state": "open"}
        mock_response.raise_for_status.return_value = None
        with patch.object(gp.session, 'get', return_value=mock_response):
            result = gp._api_get('/repos/couchbase/tlm/pulls/1')
        assert result == {"id": 1, "state": "open"}

    def test_builds_correct_url(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None
        with patch.object(gp.session, 'get',
                          return_value=mock_response) as mock_get:
            gp._api_get('/repos/org/repo/pulls/42')
        mock_get.assert_called_once_with(
            'https://api.github.com/repos/org/repo/pulls/42',
            timeout=30
        )

    def test_http_error_raises_runtime_error(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = (
            requests.exceptions.HTTPError("404 Not Found")
        )
        with patch.object(gp.session, 'get', return_value=mock_response):
            with pytest.raises(RuntimeError, match="GitHub API error"):
                gp._api_get('/repos/couchbase/nonexistent/pulls/999')


class TestGetPr:

    def test_returns_github_pr(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = make_pr_api_response(99, "tlm")
        with patch.object(gp, '_api_get', return_value=mock_response):
            pr = gp.get_pr("couchbase", "tlm", "99")
        assert pr.number == "99"
        assert pr.project == "tlm"

    def test_calls_correct_endpoint(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = make_pr_api_response(42, "server-ui")
        with patch.object(gp, '_api_get',
                          return_value=mock_response) as mock_api:
            gp.get_pr("myorg", "myrepo", "42")
        mock_api.assert_called_once_with('/repos/myorg/myrepo/pulls/42')


class TestGetOpenPrsByLabel:

    def test_filters_by_label(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = [
            make_pr_api_response(1, "tlm", labels=["backport"]),
            make_pr_api_response(2, "tlm", labels=["feature"]),
            make_pr_api_response(3, "tlm", labels=["backport", "urgent"]),
        ]
        with patch.object(gp, '_api_get_paginated',
                          return_value=mock_response):
            prs = gp.get_open_prs_by_label("couchbase", "tlm", "backport")
        assert sorted(prs.keys()) == ["1", "3"]

    def test_no_matching_labels(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        mock_response = [
            make_pr_api_response(1, "tlm", labels=["feature"]),
        ]
        with patch.object(gp, '_api_get_paginated',
                          return_value=mock_response):
            prs = gp.get_open_prs_by_label("couchbase", "tlm", "backport")
        assert prs == {}

    def test_empty_response(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        with patch.object(gp, '_api_get_paginated', return_value=[]):
            prs = gp.get_open_prs_by_label("couchbase", "tlm", "backport")
        assert prs == {}


# ===================================================================
# Manifest Resolution
# ===================================================================

class TestManifestResolution:
    """Tests for get_project_path_and_branch_from_manifest against
    the test manifest."""

    def test_project_with_explicit_revision(self, gp_with_manifest):
        """server-ui: explicit revision 'main', default path."""
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("server-ui")
        assert path == "server-ui"
        assert branch == "main"

    def test_project_with_custom_path(self, gp_with_manifest):
        """couchbase-cli: custom path 'couchbase-cli-tools'."""
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("couchbase-cli")
        assert path == "couchbase-cli-tools"
        assert branch == "master"

    def test_project_no_revision_uses_default(self, gp_with_manifest):
        """cbgt: no explicit revision → inherits 'master' from default."""
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("cbgt")
        assert path == "cbgt"
        assert branch == "master"

    def test_project_with_sha_revision(self, gp_with_manifest):
        """stellar-gateway: SHA-locked revision."""
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("stellar-gateway")
        assert path == "stellar-gateway"
        assert branch == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

    def test_manifest_project(self, gp_with_manifest):
        """manifest project is resolved like any other."""
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("manifest")
        assert path == "manifest"
        assert branch == "main"

    def test_project_with_custom_path_deep(self, gp_with_manifest):
        """eventing: deep custom path."""
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("eventing")
        assert path == "goproj/src/github.com/couchbase/eventing"
        assert branch == "master"

    def test_unknown_project_returns_none(self, gp_with_manifest):
        path, branch = gp_with_manifest \
            .get_project_path_and_branch_from_manifest("nonexistent")
        assert (path, branch) == (None, None)


# ===================================================================
# resolve_prs — PR Orchestration & Branch Filtering
# ===================================================================

class TestGetReviewsByPr:
    """resolve_prs with id_type='pr'."""

    def test_open_pr_matching_branch_included(self, gp_with_manifest):
        gp = gp_with_manifest
        pr_data = make_pr_api_response(10, "server-ui", base_ref="main")
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/server-ui#10"], 'pr')
        assert "10" in prs

    def test_duplicate_pr_refs_deduplicated(self, gp_with_manifest):
        gp = gp_with_manifest
        pr_data = make_pr_api_response(10, "server-ui", base_ref="main")
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(
                ["couchbase/server-ui#10", "couchbase/server-ui#10"], 'pr'
            )
        assert len(prs) == 1

    def test_closed_pr_not_requested_skipped(self, gp_with_manifest):
        gp = gp_with_manifest
        pr_data = make_pr_api_response(
            10, "server-ui", base_ref="main", state="closed"
        )
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/server-ui#10"], 'pr')
        assert "10" not in prs

    def test_closed_pr_explicitly_requested_kept(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.requested_prs = ["10"]
        pr_data = make_pr_api_response(
            10, "server-ui", base_ref="main", state="closed"
        )
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/server-ui#10"], 'pr')
        assert "10" in prs

    def test_branch_mismatch_not_requested_skipped(self, gp_with_manifest):
        gp = gp_with_manifest
        pr_data = make_pr_api_response(
            10, "server-ui", base_ref="develop"
        )
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/server-ui#10"], 'pr')
        assert "10" not in prs

    def test_branch_mismatch_explicitly_requested_kept(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.requested_prs = ["10"]
        pr_data = make_pr_api_response(
            10, "server-ui", base_ref="develop"
        )
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/server-ui#10"], 'pr')
        assert "10" in prs

    def test_sha_locked_manifest_skips_branch_filter(self, gp_with_manifest):
        gp = gp_with_manifest
        pr_data = make_pr_api_response(
            70, "stellar-gateway", base_ref="feature-x"
        )
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/stellar-gateway#70"], 'pr')
        assert "70" in prs

    def test_any_manifest_project_resolves(self, gp_with_manifest):
        """PR for any project in the manifest is included — the manifest
        is only used for path/branch lookup, not remote filtering."""
        gp = gp_with_manifest
        pr_data = make_pr_api_response(
            50, "tlm", base_ref="master"
        )
        with patch.object(gp, '_api_get', return_value=pr_data):
            prs = gp.resolve_prs(["couchbase/tlm#50"], 'pr')
        assert "50" in prs

    def test_multiple_prs_across_projects(self, gp_with_manifest):
        gp = gp_with_manifest
        pr1 = make_pr_api_response(10, "server-ui", base_ref="main")
        pr2 = make_pr_api_response(20, "couchbase-cli", base_ref="master")

        def mock_api_get(endpoint):
            if "server-ui" in endpoint:
                return pr1
            return pr2

        with patch.object(gp, '_api_get', side_effect=mock_api_get):
            prs = gp.resolve_prs(
                ["couchbase/server-ui#10", "couchbase/couchbase-cli#20"],
                'pr'
            )
        assert "10" in prs
        assert "20" in prs


class TestGetReviewsByLabel:
    """resolve_prs with id_type='label'."""

    def test_valid_label_returns_matching_prs(self, gp_with_manifest):
        gp = gp_with_manifest
        mock_response = [
            make_pr_api_response(
                1, "server-ui", base_ref="main", labels=["backport"]
            ),
            make_pr_api_response(
                2, "server-ui", base_ref="main", labels=["feature"]
            ),
        ]
        with patch.object(gp, '_api_get_paginated',
                          return_value=mock_response):
            prs = gp.resolve_prs(["couchbase/server-ui:backport"], 'label')
        assert "1" in prs
        assert "2" not in prs

    def test_invalid_label_format_exits(self, gp_with_manifest):
        gp = gp_with_manifest
        with pytest.raises(SystemExit):
            gp.resolve_prs(["invalid-label-format"], 'label')

    def test_label_branch_mismatch_filtered(self, gp_with_manifest):
        gp = gp_with_manifest
        mock_response = [
            make_pr_api_response(
                1, "server-ui", base_ref="develop", labels=["backport"]
            ),
        ]
        with patch.object(gp, '_api_get_paginated',
                          return_value=mock_response):
            prs = gp.resolve_prs(["couchbase/server-ui:backport"], 'label')
        assert "1" not in prs


# ===================================================================
# check_requested_prs_applied
# ===================================================================

class TestCheckRequestedPrsApplied:

    def test_all_applied_succeeds(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        gp.requested_prs = ["42"]
        gp.applied_prs = ["42"]
        gp.check_requested_prs_applied()

    def test_missing_pr_exits(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        gp.requested_prs = ["42", "43"]
        gp.applied_prs = ["42"]
        with pytest.raises(SystemExit) as e:
            gp.check_requested_prs_applied()
        assert e.value.code == 1

    def test_no_requested_no_applied_is_noop(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        gp.check_requested_prs_applied()

    def test_label_mode_reporting(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        gp.request_type = "label"
        gp.request_values = ["couchbase/tlm:backport"]
        gp.applied_prs = ["10", "20"]
        gp.check_requested_prs_applied()

    def test_applied_but_no_request_tracking(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        gp.applied_prs = ["10"]
        gp.check_requested_prs_applied()


# ===================================================================
# apply_single_pr
# ===================================================================

class TestApplySinglePr:

    def test_missing_dir_exits(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(make_pr_api_response(1, "tlm"))
        with pytest.raises(SystemExit) as e:
            gp.apply_single_pr(pr, "/nonexistent_path_xyz")
        assert e.value.code == 5

    def test_cherry_pick_mode(self, config_file, tmp_path):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(make_pr_api_response(1, "tlm"))
        proj_dir = str(tmp_path / "tlm")
        os.makedirs(proj_dir)
        with patch('subprocess.run') as mock_run:
            gp.apply_single_pr(pr, proj_dir)
        assert mock_run.call_count == 2
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert "fetch" in calls[0]
        assert "cherry-pick" in calls[1]
        assert "1" in gp.applied_prs

    def test_cherry_pick_uses_cwd(self, config_file, tmp_path):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(make_pr_api_response(1, "tlm"))
        proj_dir = str(tmp_path / "tlm")
        os.makedirs(proj_dir)
        with patch('subprocess.run') as mock_run:
            gp.apply_single_pr(pr, proj_dir)
        for call in mock_run.call_args_list:
            assert call.kwargs.get('cwd') == proj_dir

    def test_checkout_mode(self, config_file, tmp_path):
        gp = app.GitHubPatches.from_config_file(config_file, checkout=True)
        pr = app.GitHubPR(make_pr_api_response(2, "tlm"))
        proj_dir = str(tmp_path / "tlm")
        os.makedirs(proj_dir)
        with patch('subprocess.run') as mock_run:
            gp.apply_single_pr(pr, proj_dir)
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert "checkout" in calls[1]

    def test_subprocess_failure_raises_runtime_error(
        self, config_file, tmp_path
    ):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(make_pr_api_response(1, "tlm"))
        proj_dir = str(tmp_path / "tlm")
        os.makedirs(proj_dir)
        with patch(
            'subprocess.run',
            side_effect=subprocess.CalledProcessError(
                1, 'git', stderr='fatal: error'
            )
        ):
            with pytest.raises(RuntimeError, match="Patch for PR"):
                gp.apply_single_pr(pr, proj_dir)

    def test_tracks_applied_pr(self, config_file, tmp_path):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(make_pr_api_response(77, "server-ui"))
        proj_dir = str(tmp_path / "server-ui")
        os.makedirs(proj_dir)
        with patch('subprocess.run'):
            gp.apply_single_pr(pr, proj_dir)
        assert "77" in gp.applied_prs


# ===================================================================
# apply_manifest_prs
# ===================================================================

class TestApplyManifestPrs:

    def test_applies_manifest_pr_and_triggers_sync(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(
            make_pr_api_response(1, "manifest", base_ref="main")
        )
        prs = {"1": pr}

        with patch.object(gp, 'apply_single_pr') as mock_apply, \
             patch('subprocess.check_call') as mock_sync, \
             patch('patch_via_github.scripts.main.which',
                   return_value='/usr/bin/repo'):
            gp.apply_manifest_prs(prs)

        mock_apply.assert_called_once_with(
            pr, os.path.join(".repo", "manifests")
        )
        mock_sync.assert_called_once()
        assert gp.manifest_stale is True
        assert "1" not in prs

    def test_no_manifest_prs_no_sync(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr = app.GitHubPR(
            make_pr_api_response(10, "server-ui", base_ref="main")
        )
        prs = {"10": pr}

        with patch.object(gp, 'apply_single_pr') as mock_apply, \
             patch('subprocess.check_call') as mock_sync:
            gp.apply_manifest_prs(prs)

        mock_apply.assert_not_called()
        mock_sync.assert_not_called()
        assert "10" in prs

    def test_multiple_manifest_prs_applied_in_order(self, config_file):
        gp = app.GitHubPatches.from_config_file(config_file)
        pr5 = app.GitHubPR(make_pr_api_response(5, "manifest"))
        pr3 = app.GitHubPR(make_pr_api_response(3, "manifest"))
        prs = {"5": pr5, "3": pr3}

        applied_order = []

        def track_apply(pr, path):
            applied_order.append(pr.number)

        with patch.object(gp, 'apply_single_pr',
                          side_effect=track_apply), \
             patch('subprocess.check_call'), \
             patch('patch_via_github.scripts.main.which',
                   return_value='/usr/bin/repo'):
            gp.apply_manifest_prs(prs)

        assert applied_order == ["3", "5"]


# ===================================================================
# apply_non_manifest_prs
# ===================================================================

class TestApplyNonManifestPrs:

    def test_routes_to_manifest_path(self, gp_with_manifest):
        gp = gp_with_manifest
        pr = app.GitHubPR(
            make_pr_api_response(10, "server-ui", base_ref="main")
        )
        prs = {"10": pr}
        with patch.object(gp, 'apply_single_pr') as mock_apply:
            gp.apply_non_manifest_prs(prs)
        mock_apply.assert_called_once_with(pr, "server-ui")

    def test_custom_path_from_manifest(self, gp_with_manifest):
        gp = gp_with_manifest
        pr = app.GitHubPR(
            make_pr_api_response(20, "couchbase-cli", base_ref="master")
        )
        prs = {"20": pr}
        with patch.object(gp, 'apply_single_pr') as mock_apply:
            gp.apply_non_manifest_prs(prs)
        mock_apply.assert_called_once_with(pr, "couchbase-cli-tools")

    def test_project_resolved_from_manifest(self, gp_with_manifest):
        gp = gp_with_manifest
        pr = app.GitHubPR(
            make_pr_api_response(40, "tlm", base_ref="master")
        )
        prs = {"40": pr}
        with patch.object(gp, 'apply_single_pr') as mock_apply:
            gp.apply_non_manifest_prs(prs)
        mock_apply.assert_called_once_with(pr, "tlm")

    def test_unknown_project_skipped(self, gp_with_manifest):
        gp = gp_with_manifest
        pr = app.GitHubPR(
            make_pr_api_response(30, "nonexistent-repo", base_ref="main")
        )
        prs = {"30": pr}
        with patch.object(gp, 'apply_single_pr') as mock_apply:
            gp.apply_non_manifest_prs(prs)
        mock_apply.assert_not_called()

    def test_ignore_manifest_skips_manifest_pr(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.set_ignore_manifest(True)
        pr = app.GitHubPR(
            make_pr_api_response(50, "manifest", base_ref="main")
        )
        prs = {"50": pr}
        with patch.object(gp, 'apply_single_pr') as mock_apply:
            gp.apply_non_manifest_prs(prs)
        mock_apply.assert_not_called()

    def test_manifest_pr_without_flag_is_fatal(self, gp_with_manifest):
        gp = gp_with_manifest
        pr = app.GitHubPR(
            make_pr_api_response(50, "manifest", base_ref="main")
        )
        prs = {"50": pr}
        with pytest.raises(SystemExit) as e:
            gp.apply_non_manifest_prs(prs)
        assert e.value.code == 5

    def test_multiple_prs_applied_in_sorted_order(self, gp_with_manifest):
        gp = gp_with_manifest
        pr20 = app.GitHubPR(
            make_pr_api_response(20, "couchbase-cli", base_ref="master")
        )
        pr10 = app.GitHubPR(
            make_pr_api_response(10, "server-ui", base_ref="main")
        )
        prs = {"20": pr20, "10": pr10}

        applied_order = []

        def track_apply(pr, path):
            applied_order.append(pr.number)

        with patch.object(gp, 'apply_single_pr', side_effect=track_apply):
            gp.apply_non_manifest_prs(prs)

        assert applied_order == ["10", "20"]


# ===================================================================
# patch_repo_sync — Top-Level Orchestration
# ===================================================================

class TestPatchRepoSync:

    def test_default_calls_both_phases(self, gp_with_manifest):
        gp = gp_with_manifest
        with patch.object(gp, 'resolve_prs', return_value={}), \
             patch.object(gp, 'apply_manifest_prs') as mock_manifest, \
             patch.object(gp, 'apply_non_manifest_prs') as mock_non:
            gp.patch_repo_sync(["couchbase/server-ui#10"], 'pr')
        mock_manifest.assert_called_once()
        mock_non.assert_called_once()

    def test_ignore_manifest_skips_manifest_phase(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.set_ignore_manifest(True)
        with patch.object(gp, 'resolve_prs', return_value={}), \
             patch.object(gp, 'apply_manifest_prs') as mock_manifest, \
             patch.object(gp, 'apply_non_manifest_prs') as mock_non:
            gp.patch_repo_sync(["couchbase/server-ui#10"], 'pr')
        mock_manifest.assert_not_called()
        mock_non.assert_called_once()

    def test_only_manifest_skips_non_manifest_phase(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.set_only_manifest(True)
        with patch.object(gp, 'resolve_prs', return_value={}), \
             patch.object(gp, 'apply_manifest_prs') as mock_manifest, \
             patch.object(gp, 'apply_non_manifest_prs') as mock_non:
            gp.patch_repo_sync(["couchbase/server-ui#10"], 'pr')
        mock_manifest.assert_called_once()
        mock_non.assert_not_called()

    def test_check_applied_called_in_default_mode(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.requested_prs = ["10"]
        gp.applied_prs = ["10"]
        with patch.object(gp, 'resolve_prs', return_value={}), \
             patch.object(gp, 'apply_manifest_prs'), \
             patch.object(gp, 'apply_non_manifest_prs'), \
             patch.object(
                 gp, 'check_requested_prs_applied'
             ) as mock_check:
            gp.patch_repo_sync(["couchbase/server-ui#10"], 'pr')
        mock_check.assert_called_once()

    def test_check_applied_skipped_when_only_manifest(self, gp_with_manifest):
        gp = gp_with_manifest
        gp.set_only_manifest(True)
        with patch.object(gp, 'resolve_prs', return_value={}), \
             patch.object(gp, 'apply_manifest_prs'), \
             patch.object(
                 gp, 'check_requested_prs_applied'
             ) as mock_check:
            gp.patch_repo_sync(["couchbase/server-ui#10"], 'pr')
        mock_check.assert_not_called()
