#!/usr/bin/env python3
import argparse
import base64
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

WORKFLOW = ".github/workflows/gitlab-bridge.yml"
REVERSE_WORKFLOW = ".github/workflows/sync-to-gitlab.yml"
CONTEXT = "github-actions/ci-hub"
STATES = {
    "success": "success",
    "cancelled": "canceled",
    "skipped": "skipped",
}
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
GIT_ENV = dict(os.environ, GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10")


class CommandError(Exception):
    def __init__(self, command, result):
        self.status = result.returncode
        self.stderr = result.stderr.strip()
        super().__init__(f"{command} exited {self.status}")


def run(*args, payload=None, env=None):
    result = subprocess.run(
        args, text=True, capture_output=True, timeout=300,
        input=json.dumps(payload) if payload is not None else None, env=env,
    )
    if result.returncode:
        raise CommandError(args[0], result)
    return result.stdout


def api(service, endpoint, *, method="GET", body=None, pages=False, missing=False):
    host = {"gh": "github.com", "glab": "gitlab.com"}[service]
    args = [service, "api", "--hostname", host, "--method", method]
    if pages:
        args.append("--paginate")
    if body is not None:
        args.extend(["--input", "-"])
    try:
        output = run(*args, endpoint, payload=body)
    except CommandError as error:
        if missing and re.search(r"\b(?:HTTP |status code: )404\b", error.stderr):
            return None
        raise RuntimeError(f"{method} {service} {endpoint}: {error.stderr}") from None
    if not output.strip():
        return [] if pages else None
    if not pages:
        return json.loads(output)
    decoder = json.JSONDecoder()
    items = []
    while output.strip():
        page, end = decoder.raw_decode(output.lstrip())
        items.extend(page)
        output = output.lstrip()[end:]
    return items


def gitlab(endpoint, **kwargs):
    return api("glab", endpoint, **kwargs)


def github(endpoint, **kwargs):
    return api("gh", endpoint, **kwargs)


def source(config, project_id):
    project = gitlab(f"projects/{project_id}")
    if (project["namespace"]["full_path"] != config["gitlab_namespace"]
            or project["visibility"] != "private" or project["archived"]
            or project["default_branch"] != "main" or project.get("mirror") is True):
        raise ValueError(f"{project_id}: expected a private, active project on main in the configured namespace")
    return project


def projects(config):
    return [
        project for project in gitlab("projects?membership=true&per_page=100", pages=True)
        if project["namespace"]["full_path"] == config["gitlab_namespace"]
        and project["visibility"] == "private" and not project["archived"]
    ]


def discovery(config):
    approved = {item["id"] for item in config["approved"]}
    return json.dumps([
        {**{key: project[key] for key in ("id", "path_with_namespace", "default_branch")},
         "approved": project["id"] in approved}
        for project in projects(config)
    ], ensure_ascii=False, indent=2) + "\n"


def caller(config, stack, directory):
    if stack not in {"bun", "go", "rust"}:
        raise ValueError(f"Unsupported stack: {stack}")
    if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", directory) or ".." in directory.split("/"):
        raise ValueError("Use a repository-relative CI directory")
    return f"""name: GitLab CI Bridge
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  ci:
    uses: {config["github_owner"]}/ci-hub/.github/workflows/ci.yml@v1
    with:
      stack: {stack}
      directory: {json.dumps(directory)}
"""


def file_at(project_id, ref, path=WORKFLOW):
    return gitlab(f"projects/{project_id}/repository/files/{quote(path, safe='')}?ref={ref}", missing=True)


def file_text(blob):
    return base64.b64decode(blob["content"]).decode()


def detect(project_id, directory):
    prefix = "" if directory == "." else directory + "/"
    stacks = [
        stack for stack, paths in {
            "bun": ("bun.lock", "bun.lockb"),
            "go": ("go.mod",),
            "rust": ("Cargo.toml",),
        }.items() if any(file_at(project_id, "main", prefix + path) for path in paths)
    ]
    if len(stacks) != 1:
        raise ValueError("Select --stack explicitly. Automatic detection needs one matching stack.")
    return stacks[0]


def prepare(config, project_id, stack, directory):
    source(config, project_id)
    stack = stack or detect(project_id, directory)
    expected = caller(config, stack, directory)
    existing = file_at(project_id, "main")
    if existing:
        if file_text(existing) != expected:
            raise ValueError(f"{project_id}: existing caller differs")
        print(f"{project_id}: caller already on main")
        return

    base = f"projects/{project_id}"
    branch = "ci-hub/caller"
    existing = file_at(project_id, quote(branch, safe=""))
    if existing is None:
        gitlab(f"{base}/repository/commits", method="POST", body={
            "branch": branch, "start_branch": "main", "commit_message": "Add GitHub Actions CI caller",
            "actions": [{"action": "create", "file_path": WORKFLOW, "content": expected}],
        })
    elif file_text(existing) != expected:
        raise ValueError(f"{project_id}: existing preparation branch differs")
    query = f"state=opened&source_branch={quote(branch, safe='')}&target_branch=main"
    matches = gitlab(f"{base}/merge_requests?{query}")
    merge_request = matches[0] if matches else gitlab(f"{base}/merge_requests", method="POST", body={
        "source_branch": branch, "target_branch": "main",
        "title": "Add GitHub Actions CI caller", "remove_source_branch": True,
    })
    print(f"{project_id}: review and merge {merge_request['web_url']}")


def preflight(project, expected):
    project_id = project["id"]
    base = f"projects/{project_id}"
    protected = gitlab(f"{base}/protected_branches?per_page=100", pages=True)
    if [branch["name"] for branch in protected] != ["main"]:
        raise ValueError(f"{project_id}: main must be the only protected branch")
    head = gitlab(f"{base}/repository/branches/main")["commit"]["id"]
    if expected and head != expected:
        raise ValueError(f"{project_id}: main is {head}, approval was {expected}")
    return head


def tags(project):
    output = run("git", "ls-remote", "--refs", project["ssh_url_to_repo"], "refs/tags/*",
                 env=GIT_ENV)
    return {ref.removeprefix("refs/tags/"): sha for sha, ref in
            (row.split("\t") for row in output.splitlines())}


def scan(project, sha, approved_tags):
    with tempfile.TemporaryDirectory(prefix="ci-hub-") as repository:
        try:
            run("git", "clone", "--bare", "--single-branch", "--branch", "main",
                "--no-tags", project["ssh_url_to_repo"], repository, env=GIT_ENV)
            run("git", "-C", repository, "fetch", "origin", "refs/tags/*:refs/tags/*", env=GIT_ENV)
            fetched = run("git", "-C", repository, "rev-parse", "refs/heads/main").strip()
            output = run("git", "-C", repository, "for-each-ref",
                         "--format=%(objectname)\t%(refname:strip=2)", "refs/tags")
            fetched_tags = {name: value for value, name in
                            (row.split("\t") for row in output.splitlines())}
            if fetched != sha or fetched_tags != approved_tags:
                raise ValueError(f"{project['id']}: main or tags changed during the scan")
            scanner_env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")}
            run("gitleaks", "git", "--redact=100", "--ignore-gitleaks-allow",
                "--gitleaks-ignore-path", repository, "--log-opts", "--branches --tags",
                repository, env=scanner_env)
        except CommandError as error:
            raise ValueError(f"{project['id']}: {error}. Inspect the scan locally before approval.") from None
    print(f"{project['id']}: scanned main {sha} and {len(approved_tags)} tag(s)", flush=True)


def mirror_url(repo):
    return f"ssh://git@github.com/{repo}.git"


def repo_marker(project_id):
    return f"Managed by ci-hub for GitLab.com project {project_id}."


def find_mirror(base, repo):
    url = mirror_url(repo).lower()
    mirrors = []
    for mirror in gitlab(f"{base}/remote_mirrors?per_page=100", pages=True):
        existing_url = mirror["url"].lower()
        if existing_url == url:
            mirrors.append(mirror)
        elif re.search(r"(?:^|[@/])github\.com(?=[:/])", existing_url):
            raise ValueError(f"{repo}: project already has a different GitHub push mirror")
    if len(mirrors) > 1:
        raise ValueError(f"{repo}: multiple GitLab mirrors")
    return mirrors[0] if mirrors else None


def check_mirror(mirror, repo):
    if mirror["auth_method"] != "ssh_public_key" or not (
        mirror["only_protected_branches"] and mirror["keep_divergent_refs"]
    ):
        raise ValueError(f"{repo}: existing GitLab mirror has unexpected settings")
    if mirror["update_status"] == "failed" and mirror["enabled"]:
        raise ValueError(f"{repo}: GitLab mirror failed. Check its repository mirroring settings.")


def provision(config, entry, project, repo):
    project_id = entry["id"]
    base = f"projects/{project_id}"
    path = f"repos/{repo}"
    marker = repo_marker(project_id)
    target = github(path, missing=True)
    mirror = find_mirror(base, repo)
    if target and (target["visibility"] != "public" or target["description"] != marker):
        raise ValueError(f"{repo}: existing GitHub repository is outside this bridge")
    if mirror:
        check_mirror(mirror, repo)
        if mirror["enabled"]:
            if not target:
                raise ValueError(f"{repo}: enabled mirror has no GitHub repository")
            preflight(project, None)
            if mirror["update_status"] == "none":
                gitlab(f"{base}/remote_mirrors/{mirror['id']}/sync", method="POST")
            return

    expected = caller(config, entry["stack"], entry.get("directory", "."))
    sha = preflight(project, entry["sha"])
    blob = file_at(project_id, sha)
    if blob is None or file_text(blob) != expected:
        raise ValueError(f"{project_id}: prepare and merge the matching caller first")
    if file_at(project_id, sha, REVERSE_WORKFLOW):
        raise ValueError(f"{project_id}: remove the GitHub-to-GitLab workflow before activation")
    approved_tags = entry.get("tags", {})
    if tags(project) != approved_tags:
        raise ValueError(f"{project_id}: GitLab tags differ from the publication approval")
    scan(project, sha, approved_tags)
    if target and (github(f"{path}/branches") or github(f"{path}/tags")):
        raise ValueError(f"{repo}: expected an empty repository before enabling the mirror")
    if not target:
        owner = github("user")["login"]
        if owner.lower() != config["github_owner"].lower():
            raise ValueError("The GitHub owner must be the signed-in personal account")
        github("user/repos", method="POST", body={
            "name": entry["name"], "private": False, "description": marker,
            "has_issues": False, "has_wiki": False, "auto_init": False,
        })
    if not mirror:
        host_keys = [
            key for key in github("meta")["ssh_keys"] if key.startswith("ssh-ed25519 ")
        ]
        if len(host_keys) != 1:
            raise ValueError("Expected one GitHub ED25519 host key")
        mirror = gitlab(f"{base}/remote_mirrors", method="POST", body={
            "url": mirror_url(repo), "enabled": False, "auth_method": "ssh_public_key",
            "only_protected_branches": True, "keep_divergent_refs": True,
            "host_keys": host_keys,
        })
    check_mirror(mirror, repo)
    endpoint = f"{base}/remote_mirrors/{mirror['id']}"
    key = gitlab(f"{endpoint}/public_key")["public_key"].strip()
    keys = github(f"{path}/keys?per_page=100", pages=True)
    matching = [item for item in keys if item["key"].split()[:2] == key.split()[:2]]
    if not matching:
        github(f"{path}/keys", method="POST", body={
            "key": key, "title": f"GitLab push mirror {project_id}", "read_only": False,
        })
    elif matching[0]["read_only"]:
        raise ValueError(f"{repo}: existing deploy key is read-only")
    preflight(project, sha)
    if tags(project) != approved_tags:
        raise ValueError(f"{project_id}: GitLab tags changed before mirror activation")
    gitlab(endpoint, method="PUT", body={"enabled": True})
    gitlab(f"{endpoint}/sync", method="POST")
    print(f"{project_id}: enabled mirror to {repo}", flush=True)


def report(project_id, repo, state_db):
    endpoint = f"repos/{repo}/actions/workflows/gitlab-bridge.yml"
    if github(endpoint, missing=True) is None:
        return
    runs = [
        item for item in github(f"{endpoint}/runs?per_page=50")["workflow_runs"]
        if item["event"] == "push" and item["head_branch"] == "main" and item["status"] == "completed"
    ]
    seen = set()
    for attempt in sorted(runs, key=lambda item: (item["run_number"], item["run_attempt"]), reverse=True):
        sha = attempt["head_sha"]
        if not SHA_PATTERN.fullmatch(sha) or sha in seen:
            continue
        seen.add(sha)
        state = STATES.get(attempt["conclusion"], "failed")
        url = f"https://github.com/{repo}/actions/runs/{attempt['id']}/attempts/{attempt['run_attempt']}"
        key = (project_id, sha)
        cached = state_db.execute(
            "SELECT url, state FROM reported WHERE project_id = ? AND sha = ?", key
        ).fetchone()
        if cached == (url, state):
            continue
        base = f"projects/{project_id}"
        statuses = gitlab(f"{base}/repository/commits/{sha}/statuses?ref=main&name={CONTEXT}",
                          missing=True)
        if statuses is None:
            continue
        if not any(item["target_url"] == url and item["status"] == state for item in statuses):
            gitlab(f"{base}/statuses/{sha}", method="POST", body={
                "ref": "main", "name": CONTEXT, "state": state, "target_url": url,
            })
            print(f"{project_id}: {sha} {state}", flush=True)
        with state_db:
            state_db.execute("INSERT OR REPLACE INTO reported VALUES (?, ?, ?, ?)", (*key, url, state))


def reconcile(config, state_db):
    state_db.execute("""CREATE TABLE IF NOT EXISTS reported (
        project_id INTEGER, sha TEXT, url TEXT, state TEXT, PRIMARY KEY (project_id, sha)
    )""")
    failed = False
    for entry in config["approved"]:
        project_id = entry["id"]
        repo = f"{config['github_owner']}/{entry['name']}"
        try:
            project = source(config, project_id)
            provision(config, entry, project, repo)
            report(project_id, repo, state_db)
        except (ValueError, RuntimeError, CommandError, subprocess.TimeoutExpired) as error:
            print(f"{repo}: {error}", file=sys.stderr, flush=True)
            failed = True
    return int(failed)


def load_config(path):
    config = json.loads(path.read_text())
    for owner in (config["github_owner"], *config["gitlab_namespace"].split("/")):
        if not TOKEN_PATTERN.fullmatch(owner) or owner in {".", ".."}:
            raise ValueError("Invalid namespace or GitHub owner")
    ids, names = set(), set()
    for entry in config["approved"]:
        project_id, name, sha = entry["id"], entry["name"], entry["sha"]
        if type(project_id) is not int or project_id <= 0 or not SHA_PATTERN.fullmatch(sha):
            raise ValueError("An approval needs a numeric project ID and a full SHA")
        if not TOKEN_PATTERN.fullmatch(name) or name.lower() in {".", "..", "ci-hub"}:
            raise ValueError("Invalid mirror repository name")
        if project_id in ids or name.lower() in names:
            raise ValueError("Each project and mirror name must be unique")
        if any(not SHA_PATTERN.fullmatch(value) for value in entry.get("tags", {}).values()):
            raise ValueError("Each approved tag needs its full Git object SHA")
        ids.add(project_id)
        names.add(name.lower())
    return config


def main():
    parser = argparse.ArgumentParser(description="GitLab to GitHub CI bridge")
    parser.add_argument("--config", type=Path,
                        default=Path.home() / ".config/ci-hub/projects.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    sub.add_parser("reconcile")
    sub.add_parser("tick")
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("id", type=int)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("id", type=int)
    prepare_parser.add_argument("--stack", choices=["bun", "go", "rust"])
    prepare_parser.add_argument("--directory", default=".")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "discover":
            print(discovery(config), end="")
        elif args.command == "scan":
            project = source(config, args.id)
            source_tags = tags(project)
            scan(project, preflight(project, None), source_tags)
            print(json.dumps(source_tags, ensure_ascii=False, indent=2))
        else:
            with args.config.with_name(".bridge.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("ci-hub is already running")
                    return 0
                if args.command == "prepare":
                    prepare(config, args.id, args.stack, args.directory)
                else:
                    if args.command == "tick":
                        snapshot = args.config.with_name("discovered.json")
                        staged = snapshot.with_suffix(".tmp")
                        staged.write_text(discovery(config))
                        staged.replace(snapshot)
                    state_db = sqlite3.connect(args.config.with_name("state.sqlite3"))
                    try:
                        return reconcile(config, state_db)
                    finally:
                        state_db.close()
    except (ValueError, KeyError, TypeError, RuntimeError, CommandError, OSError,
            subprocess.TimeoutExpired) as error:
        parser.exit(1, f"ci-hub: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
