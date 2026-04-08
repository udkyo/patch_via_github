import json
import os
import tempfile

import pytest


def make_pr_api_response(
    number, repo_name, org="couchbase", base_ref="master",
    head_sha="abc123", state="open", labels=None, title=None
):
    """Build a mock GitHub PR API response dict."""
    return {
        "number": number,
        "state": state,
        "title": title or f"PR #{number}",
        "html_url": f"https://github.com/{org}/{repo_name}/pull/{number}",
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "base": {
            "ref": base_ref,
            "repo": {
                "name": repo_name,
                "full_name": f"{org}/{repo_name}",
                "clone_url": f"https://github.com/{org}/{repo_name}.git",
            },
        },
        "head": {
            "sha": head_sha,
            "ref": f"feature-{number}",
            "repo": {
                "clone_url": f"https://github.com/{org}/{repo_name}.git",
            },
        },
    }


@pytest.fixture
def config_file(tmp_path):
    """Create a temporary config file for tests."""
    ini = tmp_path / "patch_via_github.ini"
    ini.write_text(
        "[main]\n"
        "token = ghp_test_token_000000000000000000000\n"
        "default_org = couchbase\n"
    )
    return str(ini)


@pytest.fixture
def config_file_no_org(tmp_path):
    """Create a config file without default_org."""
    ini = tmp_path / "patch_via_github.ini"
    ini.write_text(
        "[main]\n"
        "token = ghp_test_token_000000000000000000000\n"
    )
    return str(ini)
