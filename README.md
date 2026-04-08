# patch\_via\_github

`patch_via_github` is a command-line tool for applying GitHub pull request
changes to a [`repo sync`](https://source.android.com/docs/setup/download/downloading)
workspace. It automates the process of fetching and cherry-picking (or checking
out) PRs from GitHub, with manifest-aware project resolution.

This is the GitHub counterpart to
[`patch_via_gerrit`](https://github.com/couchbase/patch_via_gerrit). The two
tools can be used together on the same `repo sync` workspace — each targets
its own set of PRs/reviews independently.

## Installation

The easiest way to install `patch_via_github` is using `uv`:

```bash
uv tool install patch-via-github
```

This will install `patch_via_github` to `~/.local/bin/` (or the equivalent on
Windows). Make sure this directory is on your `PATH`.

## Configuration

Before using `patch_via_github`, create a configuration file with your GitHub
credentials. By default, the tool looks for `~/.ssh/patch_via_github.ini`:

```ini
[main]
token = ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
default_org = couchbase
```

| Key | Required | Description |
|---|---|---|
| `token` | Yes | GitHub personal access token (see below) |
| `default_org` | No | Default GitHub org for short-form PR references (`repo#123`) |

An example config is provided in
[`patch_via_github.ini.example`](patch_via_github.ini.example).

You can also specify a custom config file location with the `-c/--config`
option.

### Creating a GitHub Personal Access Token

The tool needs a token with read access to pull requests. To create one:

1. Go to **GitHub → Settings → Developer settings →
   [Personal access tokens → Fine-grained tokens](https://github.com/settings/personal-access-tokens/new)**
2. Give the token a descriptive name (e.g. `patch_via_github`)
3. Set the **Expiration** as appropriate for your use case
4. Under **Repository access**, select the repos (or org) you'll be patching
5. Under **Permissions → Repository permissions**, set:
   - **Pull requests** → **Read-only**
   - **Contents** → **Read-only** (required for the API to return clone URLs)
6. Click **Generate token** and copy the value into your config file

> **Tip:** For classic tokens (`ghp_…`), the `repo` scope is sufficient. Fine-grained
> tokens are recommended as they follow the principle of least privilege.

## Usage

By default, `patch_via_github` operates on the current directory. You can either
navigate to your `repo sync` workspace first, or use the `-s/--source` option to
specify the workspace location.

### Apply Patches by PR Reference

Apply one or more specific pull requests:

```bash
patch_via_github -p couchbase/tlm#42
patch_via_github -p couchbase/tlm#42,couchbase/ns_server#108
patch_via_github --pull-request couchbase/tlm#42 couchbase/server-ui#7
```

Short-form references use the `default_org` from your config (or `-o`):

```bash
patch_via_github -o couchbase -p tlm#42,ns_server#108
```

### Apply Patches by Label

Apply all open PRs matching a label on a given repo:

```bash
patch_via_github -l couchbase/tlm:backport
patch_via_github -l couchbase/tlm:backport,couchbase/ns_server:ci-test
```

### Checkout vs Cherry-Pick

By default, patches are cherry-picked. Use `-C/--checkout` to checkout the PR
head instead:

```bash
patch_via_github -C -p couchbase/tlm#42
```

### Manifest Handling

```bash
# Ignore changes to the manifest repository
patch_via_github --ignore-manifest -p couchbase/manifest#10

# Apply only changes to the manifest repository
patch_via_github --only-manifest -p couchbase/manifest#10
```

### Debug Output

```bash
patch_via_github -d -p couchbase/tlm#42
```

## How It Works

`patch_via_github` automates several tasks:

1. **PR Resolution:** When you specify PRs by reference or label, the tool
   fetches their metadata from the GitHub API and filters based on state and
   branch.

2. **Branch Filtering:** PRs are only applied if their target branch matches
   the manifest's revision for that project (unless explicitly requested by
   PR reference, or the manifest revision is a SHA).

3. **Manifest Updates:** If any PRs modify the `manifest` repository, the tool
   applies those first and runs `repo sync` to update the workspace before
   applying remaining patches.

4. **Verification:** The tool verifies that all explicitly-requested PRs were
   successfully applied.

## Command-Line Options

```
patch_via_github [options]

Required (mutually exclusive):
  -p, --pull-request PR [PR ...]  PR references to apply (comma-separated)
                                  Format: org/repo#number or repo#number
  -l, --label LABEL [LABEL ...]   Labels to search for open PRs (comma-separated)
                                  Format: org/repo:label

Options:
  -d, --debug                     Enable debugging output
  -c, --config FILE               Configuration file (default: ~/.ssh/patch_via_github.ini)
  -s, --source DIR                Location of repo sync checkout (default: current directory)
  -o, --default-org ORG           Default GitHub org for short-form PR references
  -C, --checkout                  Checkout PR head instead of cherry-picking
  --ignore-manifest               Don't apply changes to manifest repository
  --only-manifest                 Apply only changes to manifest repository
  -V, --version                   Display version information
```

## Troubleshooting

**"Configuration file missing":**
- Create `~/.ssh/patch_via_github.ini` with your GitHub credentials
- Or specify a custom location with `-c/--config`

**"Project missing on disk":**
- Ensure you've run `repo sync` before applying patches
- The PR may reference a project not in your manifest groups

**"Failed to apply all explicitly-requested PRs":**
- There may be merge conflicts
- The project may be locked to a specific SHA in the manifest
- Try running with `--debug` to see more details

## Running Tests

```bash
pytest
```
