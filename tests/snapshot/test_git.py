"""The git reader: its pure parts, then ``RealGit`` against local repositories over file://."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fakes import FakeCheckout, FakeGit
from repos import T, clone, commit, git, new_repository, served, write

from lib.forms import is_full_ref, is_object_id, is_ref_name
from lib.snapshot.git import (
    Completed,
    GitError,
    RealGit,
    branch_of,
    branch_ref,
    check_full_ref,
    check_object_id,
    check_tree_path,
    check_url,
    git_environment,
    parse_batch,
    parse_branches,
    parse_ls_remote,
    run_command,
    shown_url,
    tree_files,
)

A = "a" * 40
B = "b" * 40


# ---------------------------------------------------------------------------
# Foreign values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "master",
        "3.14.x",
        "feature/snapshot-dependencies",
        "sc-12345/-a-component-that-starts-with-a-dash",
        "agent/a-component-that-ends-with-a-dash-/then/more",
        "3.14.x-20990101-pinned",
        "refs/heads/9.9.9-test-runner",
        "refs/tags/v3.14.3",
        "a_b",
        "snapshot/feature",
    ],
)
def test_branch_and_ref_names_of_the_shapes_in_use_are_accepted(name):
    assert is_ref_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "fix+1",
        "x/${BUILDKITE_AGENT_ACCESS_TOKEN}",
        "a{b}",
        "a@b",
        "@",
        "a'b",
        'a"b',
        "a`b",
        "a<b>|c;d&e!f#g%h=i,j",
        "a]b",
        "a.lockx",
        "caf" + chr(0xE9),
        "a" + chr(0x2028) + "b",
        "head",
        "x/HEAD",
        "x" * 1000,
        "snapshot/x/${BUILDKITE_AGENT_ACCESS_TOKEN}",
    ],
)
def test_every_name_git_accepts_as_a_branch_is_accepted(name):
    assert is_ref_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-x",
        "--upload-pack=touch x",
        "HEAD",
        "/x",
        "x/",
        ".x",
        "x.",
        "a..b",
        "a//b",
        "a/.b",
        "a.lock",
        "a.lock/b",
        "a b",
        "a\tb",
        "a\nb",
        "a\x01b",
        "a\x7fb",
        "a*",
        "a?",
        "a[b",
        "a~1",
        "a^",
        "a:b",
        "a\\b",
        "a@{1}",
        "@{",
        "a\udce9",
    ],
)
def test_names_git_refuses_as_a_branch_are_refused(name):
    assert not is_ref_name(name)


def test_object_ids_are_40_lowercase_hex_digits():
    assert is_object_id(A)
    assert not is_object_id(A.upper())
    assert not is_object_id(A[:-1])
    assert not is_object_id("HEAD")
    assert not is_object_id(A + "\n")


@pytest.mark.parametrize(
    "url, protocols, ok",
    [
        ("https://github.com/bureau14/qdb-api-rest.git", ("https",), True),
        ("file:///tmp/remote.git", ("file",), True),
        ("file:///tmp/remote.git", ("https",), False),
        ("ext::sh -c touch% /tmp/x", ("https",), False),
        ("-u", ("https",), False),
        ("https://github.com/a b.git", ("https",), False),
        ("https://x-access-token:secret@github.com/a/b.git", ("https",), False),
        ("ssh://git@github.com/a/b.git", ("https",), False),
    ],
)
def test_urls(url, protocols, ok):
    if ok:
        assert check_url(url, protocols) == url
    else:
        with pytest.raises(GitError, match="refused repository URL"):
            check_url(url, protocols)


@pytest.mark.parametrize(
    "check",
    [
        check_full_ref,
        check_object_id,
        check_tree_path,
        lambda v: check_url(v, ("https",)),
    ],
    ids=["ref", "object id", "path", "url"],
)
def test_a_refused_value_is_shown_short_on_one_line_of_ascii(check):
    with pytest.raises(GitError) as e:
        check("refs/heads/" + "x" * 100_000 + "\n" + chr(0xE9))
    message = str(e.value)
    assert len(message) < 300 and "\n" not in message, message[:300]
    message.encode("ascii")


def test_a_refused_url_never_shows_its_user_part():
    with pytest.raises(GitError) as e:
        check_url("https://x-access-token:s3cr3t@github.com/a/b.git", ("https",))
    assert "s3cr3t" not in str(e.value)
    assert shown_url("https://u:p@h/x") == "https://***@h/x"


def test_the_environment_of_git():
    base = {"GIT_DIR": "/elsewhere", "GIT_WORK_TREE": "/w", "GIT_CONFIG_COUNT": "1", "PATH": "/b"}
    env = git_environment(base, ("https",))
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert env["GIT_CONFIG_COUNT"] == "1", "the job's credential helper is kept"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_LITERAL_PATHSPECS"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# ---------------------------------------------------------------------------
# Reading git's output
# ---------------------------------------------------------------------------


def test_parse_ls_remote_matches_the_exact_refs_only():
    out = f"{A}\trefs/heads/x/refs/heads/master\n{B}\trefs/heads/master\n{A}\trefs/heads/3.14.x\n"
    assert parse_ls_remote(out, ["refs/heads/master"]) == {"refs/heads/master": B}
    assert parse_ls_remote(out, ["refs/heads/feature"]) == {}
    assert parse_ls_remote("", ["refs/heads/master"]) == {}
    assert parse_ls_remote(
        out, ["refs/heads/feature", "refs/heads/3.14.x", "refs/heads/master"]
    ) == {
        "refs/heads/3.14.x": A,
        "refs/heads/master": B,
    }


def test_parse_ls_remote_takes_the_peeled_commit_of_an_annotated_tag():
    out = f"{A}\trefs/tags/v1\n{B}\trefs/tags/v1^{{}}\n"
    assert parse_ls_remote(out, ["refs/tags/v1"]) == {"refs/tags/v1": B}
    lightweight = parse_ls_remote(f"{A}\trefs/tags/v1\n", ["refs/tags/v1"])
    assert lightweight == {"refs/tags/v1": A}
    assert parse_ls_remote(f"{B}\trefs/tags/v2^{{}}\n", ["refs/tags/v1"]) == {}


@pytest.mark.parametrize(
    "ref, full",
    [
        ("refs/heads/master", True),
        ("refs/heads/feature/snapshot-dependencies", True),
        ("refs/tags/v3.14.3", True),
        ("master", False),
        ("refs/heads/", False),
        ("refs/heads/-x", False),
        ("refs/heads/a b", False),
        ("refs/tags/v1^{}", False),
        ("refs/pull/1/head", False),
        ("refs/remotes/origin/master", False),
        ("heads/master", False),
    ],
)
def test_a_full_ref_names_a_branch_or_a_tag(ref, full):
    assert is_full_ref(ref) is full


def test_the_branch_of_a_full_ref():
    assert branch_ref("3.14.x") == "refs/heads/3.14.x"
    assert branch_of("refs/heads/3.14.x") == "3.14.x"
    assert branch_of("refs/tags/v3.14.3") is None


def test_parse_branches():
    out = f"{A}\trefs/heads/master\n{B}\trefs/heads/sc-1/x\nnot a row\n{A}\trefs/tags/v1\n"
    assert parse_branches(out) == {"master": A, "sc-1/x": B}


def test_tree_files_keeps_regular_files_and_paths_as_they_are():
    listing = b"\0".join(
        [
            b"100644 blob " + A.encode() + b"\t.buildkite/steps/my step.yml",
            b"100755 blob " + B.encode() + b"\t.buildkite/run.sh",
            b"120000 blob " + A.encode() + b"\t.buildkite/link.yml",
            b"160000 commit " + B.encode() + b"\t.buildkite/tools",
            b"100644 blob " + A.encode() + b"\t.buildkite/caf\xc3\xa9 [x](y).yml",
            b"",
        ]
    )
    assert tree_files(listing) == [
        (".buildkite/steps/my step.yml", A),
        (".buildkite/run.sh", B),
        (".buildkite/caf" + chr(0xE9) + " [x](y).yml", A),
    ]


@pytest.mark.parametrize(
    "path",
    [
        b".buildkite/caf\xe9.yml",
        b".buildkite/a\nb cannot give a snapshot: forged.yml",
        b".buildkite/tab\there.yml",
        b".buildkite/\x1b[31mred.yml",
        b".buildkite/del\x7f.yml",
        b".buildkite/next\xc2\x85line.yml",
        b".buildkite/line\xe2\x80\xa8separator.yml",
    ],
    ids=["not utf-8", "a newline", "a tab", "an escape", "delete", "next line", "line separator"],
)
def test_a_path_that_is_not_utf8_or_holds_a_control_character_is_refused(path):
    # A path is quoted in messages, one refusal per line, and goes into an
    # annotation that must be encoded as UTF-8.
    listing = b"100644 blob " + A.encode() + b"\t" + path + b"\0"
    with pytest.raises(GitError, match="refused path") as e:
        tree_files(listing)
    assert all(" " <= c <= "~" for c in str(e.value)), str(e.value)


def test_parse_batch():
    out = f"{A} blob 3\nabc\n{B} blob 0\n\n".encode()
    assert parse_batch(out, [A, B]) == {A: b"abc", B: b""}


@pytest.mark.parametrize(
    "out, detail",
    [
        (f"{A} missing\n".encode(), "missing"),
        (f"{A} tree 3\nabc\n".encode(), "tree"),
        (f"{A} blob 9\nabc\n".encode(), "cut short"),
        (f"{B} blob 3\nabc\n".encode(), B),
        (b"", "nothing"),
    ],
)
def test_parse_batch_refuses_anything_but_the_blobs_asked(out, detail):
    with pytest.raises(GitError, match=detail):
        parse_batch(out, [A])


# ---------------------------------------------------------------------------
# Local repositories over file://
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.usefixtures("isolated_git")


TEMPLATE = (
    "steps:\n"
    "  - label: build {slug}\n"
    "    plugins:\n"
    "      - bureau14/qdb-artifacts#master:\n"
    "          download:\n"
    "            projects:\n"
    '              - project_id: "quasardb-build"\n'
)
RUNNER = 'from qdb_pipeline import run\n\nif __name__ == "__main__":\n    run(generate)\n'
TOOLS = "1" * 40


@pytest.fixture
def remote(tmp_path):
    """A repository with two commits on master; the first one is not a branch head.

    Its tree holds templates (one with a space in its name), ``pipeline.py``,
    a hook, a symlink, a file that is not UTF-8, and the tools submodule as a
    gitlink to a commit the repository does not have. The annotated tag
    ``v1`` names the first commit, the lightweight tag ``v2`` the second.
    """
    r = new_repository(tmp_path / "remote")
    files = {
        ".buildkite/steps/_build.yml": TEMPLATE,
        ".buildkite/steps/my step.yml": "steps: []\n",
        ".buildkite/pipeline.py": RUNNER,
        ".buildkite/hooks/post-command": "#!/bin/sh\n",
        ".buildkite/latin1.yml": b"name: caf\xe9\n",
        "README.md": "readme\n",
    }
    write(r, files)
    (r / ".buildkite/link.yml").symlink_to("steps/_build.yml")
    git(r, "add", "-A")
    git(r, "update-index", "--add", "--cacheinfo", f"160000,{TOOLS},.buildkite/tools")
    git(r, "commit", "-q", "-m", "first", date=T)
    first = git(r, "rev-parse", "HEAD")
    second = commit(r, {".buildkite/steps/_build.yml": TEMPLATE + "# changed\n"}, "second")
    git(r, "tag", "-a", "-m", "annotated", "v1", first, date=T)
    git(r, "tag", "v2", second)
    return {"path": r, "url": served(r), "first": first, "second": second, "files": files}


class Recorder:
    """A runner that runs git and records each command line and its environment."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, cwd, stdin, env):
        self.calls.append((list(args), dict(env)))
        return run_command(args, cwd, stdin, env)

    def commands(self) -> list[str]:
        return [
            next(a for a in args[1:] if not a.startswith("-") and "=" not in a)
            for args, _ in self.calls
        ]


def every_file(path: str) -> bool:
    return True


def commit_of(g: RealGit, url: str, ref: str) -> str | None:
    return g.commits_of(url, [ref]).get(ref)


def test_the_commit_of_a_branch_and_of_a_missing_one(remote):
    g = RealGit(protocols=("file",))
    assert commit_of(g, remote["url"], "refs/heads/master") == remote["second"]
    assert commit_of(g, remote["url"], "refs/heads/nope") is None


def test_the_commit_of_a_tag_is_the_commit_it_names(remote):
    g = RealGit(protocols=("file",))
    tag_object = git(remote["path"], "rev-parse", "v1")
    assert tag_object != remote["first"], "v1 is an annotated tag"
    assert commit_of(g, remote["url"], "refs/tags/v1") == remote["first"], "peeled"
    assert commit_of(g, remote["url"], "refs/tags/v2") == remote["second"], "lightweight"
    assert commit_of(g, remote["url"], "refs/tags/nope") is None
    assert commit_of(g, remote["url"], "refs/heads/v1") is None, "a tag is not a branch"


def test_the_commit_of_the_exact_ref_only(remote):
    git(remote["path"], "branch", "x/master", remote["first"])
    git(remote["path"], "branch", "refs/tags/v2", remote["first"])
    g = RealGit(protocols=("file",))
    assert commit_of(g, remote["url"], "refs/heads/master") == remote["second"]
    assert commit_of(g, remote["url"], "refs/tags/v2") == remote["second"]


def test_the_commits_of_several_refs_are_asked_in_one_ls_remote(remote):
    run = Recorder()
    g = RealGit(protocols=("file",), run=run)
    refs = ["refs/heads/nope", "refs/tags/v1", "refs/heads/master", "refs/heads/master"]
    assert g.commits_of(remote["url"], refs) == {
        "refs/tags/v1": remote["first"],
        "refs/heads/master": remote["second"],
    }
    [(args, _)] = run.calls
    assert args[-4:] == ["refs/heads/nope", "refs/tags/v1", "refs/heads/master", "refs/tags/v1^{}"]


def test_a_transport_error_is_an_error_not_an_absent_ref(tmp_path):
    with pytest.raises(GitError, match="ls-remote"):
        commit_of(RealGit(protocols=("file",)), (tmp_path / "nope").as_uri(), "refs/heads/master")


def test_reads_the_files_of_a_commit_that_is_not_a_branch_head(remote):
    files = RealGit(protocols=("file",)).read_files(
        remote["url"], remote["first"], ".buildkite", every_file
    )
    assert files == {
        ".buildkite/steps/_build.yml": TEMPLATE.encode(),
        ".buildkite/steps/my step.yml": b"steps: []\n",
        ".buildkite/pipeline.py": RUNNER.encode(),
        ".buildkite/hooks/post-command": b"#!/bin/sh\n",
        ".buildkite/latin1.yml": b"name: caf\xe9\n",
    }, "regular files only: no symlink, no submodule, nothing outside the directory"


def test_reads_the_head_commit_and_only_the_files_asked_for(remote):
    files = RealGit(protocols=("file",)).read_files(
        remote["url"], remote["second"], ".buildkite", lambda p: p.endswith(".yml")
    )
    assert files[".buildkite/steps/_build.yml"].endswith(b"# changed\n")
    assert sorted(files) == [
        ".buildkite/latin1.yml",
        ".buildkite/steps/_build.yml",
        ".buildkite/steps/my step.yml",
    ]


def test_the_recipe_is_two_fetches_whatever_the_number_of_files(remote):
    run = Recorder()
    RealGit(protocols=("file",), run=run).read_files(
        remote["url"], remote["first"], ".buildkite", every_file
    )
    assert run.commands() == ["init", "fetch", "ls-tree", "fetch", "cat-file"]
    commit_fetch, blob_fetch = (args for args, _ in run.calls if "fetch" in args)
    assert "--filter=blob:none" in commit_fetch and "--depth" in commit_fetch
    assert "--no-tags" in commit_fetch, "no ls-refs round trip for tags (experiment 33)"
    assert "--no-write-fetch-head" in blob_fetch and "--no-tags" in blob_fetch
    assert len([a for a in blob_fetch if is_object_id(a)]) == 5, "one fetch for every blob"
    assert all(env["GIT_ALLOW_PROTOCOL"] == "file" for _, env in run.calls)


def test_the_read_runs_in_a_scratch_repository_that_is_removed_after(remote, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    places: list[str] = []

    def run(args, cwd, stdin, env):
        places.append(cwd)
        return run_command(args, cwd, stdin, env)

    g = RealGit(cwd=str(tmp_path / "not-a-checkout"), protocols=("file",), run=run)
    assert g.read_files(remote["url"], remote["first"], ".buildkite", every_file)
    with pytest.raises(GitError):
        g.read_files(remote["url"], "9" * 40, ".buildkite", every_file)
    assert places and all(Path(p).parent == scratch for p in places), "never in the checkout"
    assert list(scratch.iterdir()) == [], "removed after a read and after a failed read"


def test_no_blob_is_fetched_when_no_file_counts(remote):
    run = Recorder()
    files = RealGit(protocols=("file",), run=run).read_files(
        remote["url"], remote["first"], ".buildkite", lambda p: False
    )
    assert files == {}
    assert run.commands() == ["init", "fetch", "ls-tree"]


def test_every_foreign_value_comes_after_the_end_of_the_options(remote, tmp_path):
    run = Recorder()
    g = RealGit(cwd=str(checkout_of(remote, tmp_path, "master")), protocols=("file",), run=run)
    g.commits_of(remote["url"], ["refs/heads/master"])
    g.commits_of(remote["url"], ["refs/tags/v1"])
    g.read_files(remote["url"], remote["first"], ".buildkite", every_file)
    g.head_commit()
    g.branches(remote["url"])
    g.merge_base_times(remote["url"], [remote["first"]])
    assert {"ls-remote", "fetch", "ls-tree", "rev-parse", "merge-base", "show"} <= set(
        run.commands()
    )
    for args, _ in run.calls:
        foreign = [
            i
            for i, a in enumerate(args)
            if a in (remote["url"], ".buildkite") or is_object_id(a) or a.startswith("refs/")
        ]
        ends = [i for i, a in enumerate(args) if a in ("--", "--end-of-options")]
        assert not foreign or (ends and ends[0] < foreign[0]), args


def every_command(remote, tmp_path) -> list[list[str]]:
    """The command lines of one of each call, on a checkout of ``remote``."""
    run = Recorder()
    checkout = checkout_of(remote, tmp_path, "master")
    g = RealGit(cwd=str(checkout), protocols=("file",), run=run)
    g.commits_of(remote["url"], ["refs/heads/master"])
    g.commits_of(remote["url"], ["refs/tags/v1"])
    g.read_files(remote["url"], remote["first"], ".buildkite", every_file)
    g.head_commit()
    g.branches(remote["url"])
    g.merge_base_times(remote["url"], [remote["first"]])
    return [args for args, _ in run.calls]


def talks_to_a_remote(args: list[str]) -> bool:
    return "ls-remote" in args or "fetch" in args


def test_proactive_authentication_goes_with_every_command_to_a_remote_and_no_other(
    remote, tmp_path
):
    commands = every_command(remote, tmp_path)
    assert any(talks_to_a_remote(args) for args in commands)
    for args in commands:
        sent = args[1:3] == ["-c", "http.proactiveAuth=basic"]
        assert sent is talks_to_a_remote(args), args


def test_a_tree_without_the_directory_gives_no_files(tmp_path):
    r = new_repository(tmp_path / "plain")
    c = commit(r, {"README.md": "x\n"}, "only a readme")
    assert RealGit(protocols=("file",)).read_files(served(r), c, ".buildkite", every_file) == {}


def test_a_tree_with_a_path_that_holds_a_newline_cannot_be_read(tmp_path):
    r = new_repository(tmp_path / "forged")
    commit(r, {"README.md": "x\n"}, "a readme")
    blob = git(r, "rev-parse", "HEAD:README.md")
    path = ".buildkite/a\nqdb-api-go cannot give a snapshot: forged.yml"
    git(r, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    git(r, "commit", "-q", "-m", "a path with a newline", date=T)
    c = git(r, "rev-parse", "HEAD")
    with pytest.raises(GitError, match="refused path"):
        RealGit(protocols=("file",)).read_files(served(r), c, ".buildkite", every_file)


def test_a_missing_commit_is_an_error(remote):
    with pytest.raises(GitError, match="fetch"):
        RealGit(protocols=("file",)).read_files(remote["url"], "9" * 40, ".buildkite", every_file)


def test_a_missing_repository_is_an_error(tmp_path, remote):
    url = (tmp_path / "nope").as_uri()
    with pytest.raises(GitError, match="fetch"):
        RealGit(protocols=("file",)).read_files(url, remote["first"], ".buildkite", every_file)


def test_an_inherited_git_dir_does_not_redirect_the_read(remote, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    files = RealGit(protocols=("file",)).read_files(
        remote["url"], remote["first"], ".buildkite", every_file
    )
    assert ".buildkite/pipeline.py" in files


def refuse(*args):
    raise AssertionError(f"git must not run: {args}")


@pytest.mark.parametrize(
    "url, commit, directory, detail",
    [
        ("https://github.com/a/b.git", A, ".buildkite", "URL"),
        ("ext::sh -c x", A, ".buildkite", "URL"),
        ("file:///r", "HEAD", ".buildkite", "object id"),
        ("file:///r", A.upper(), ".buildkite", "object id"),
        ("file:///r", "--upload-pack=x", ".buildkite", "object id"),
        ("file:///r", A, "../x", "path"),
        ("file:///r", A, "/etc", "path"),
        ("file:///r", A, ":(top)x", "path"),
        ("file:///r", A, "-x", "path"),
    ],
)
def test_foreign_values_are_refused_before_git_runs(url, commit, directory, detail):
    g = RealGit(protocols=("file",), run=refuse)
    with pytest.raises(GitError, match=detail):
        g.read_files(url, commit, directory, every_file)


@pytest.mark.parametrize(
    "ref",
    [
        "master",
        "-x",
        "refs/heads/-x",
        "refs/heads/a b",
        "refs/heads/*",
        "refs/heads/../x",
        "refs/tags/v1^{}",
        "refs/pull/1/head",
        "",
    ],
)
def test_a_ref_that_is_not_a_plain_full_ref_is_refused_before_git_runs(ref):
    with pytest.raises(GitError, match="refused ref"):
        RealGit(protocols=("file",), run=refuse).commits_of("file:///r", [ref])


# ---------------------------------------------------------------------------
# The checkout: its commit, its files and its merge-bases
# ---------------------------------------------------------------------------


def checkout_of(remote, tmp_path: Path, branch: str, *options: str) -> Path:
    return clone(remote["url"], tmp_path / f"checkout-{branch.replace('/', '-')}", branch, *options)


def test_head_commit_is_the_commit_checked_out(remote, tmp_path):
    c = checkout_of(remote, tmp_path, "master")
    assert RealGit(cwd=str(c), protocols=("file",)).head_commit() == remote["second"]


def test_the_checkouts_files_are_read_from_its_commit_not_its_working_tree(remote, tmp_path):
    c = checkout_of(remote, tmp_path, "master")
    write(c, {".buildkite/snapshot-pipeline.yml": "steps: []\n", ".buildkite/pipeline.py": "x"})
    run = Recorder()
    g = RealGit(cwd=str(c), protocols=("file",), run=run)
    files = g.checkout_files(".buildkite", every_file)
    assert files[".buildkite/pipeline.py"] == RUNNER.encode(), "the committed content"
    assert ".buildkite/snapshot-pipeline.yml" not in files, "a file the job wrote"
    assert ".buildkite/link.yml" not in files and ".buildkite/tools" not in files
    assert files[".buildkite/steps/_build.yml"].endswith(b"# changed\n")
    assert run.commands() == ["ls-tree", "cat-file"], "local, no fetch"


def test_the_checkouts_files_are_read_from_the_top_whatever_the_directory_it_runs_in(
    remote, tmp_path
):
    c = checkout_of(remote, tmp_path, "master")
    files = RealGit(cwd=str(c / ".buildkite"), protocols=("file",)).checkout_files(
        ".buildkite", every_file
    )
    assert ".buildkite/pipeline.py" in files


def test_a_checkout_without_the_directory_has_no_files(remote, tmp_path):
    c = checkout_of(remote, tmp_path, "master")
    assert RealGit(cwd=str(c), protocols=("file",)).checkout_files("nowhere", every_file) == {}
    with pytest.raises(GitError, match="refused path"):
        RealGit(cwd=str(c), protocols=("file",), run=refuse).checkout_files("../x", every_file)


def test_the_branches_of_a_repository(lines):
    found = RealGit(protocols=("file",)).branches(lines["url"])
    assert sorted(found) == [
        "2.0.x",
        "3.14.x",
        "3.14.x-pin",
        "from-3.14.x",
        "from-master",
        "master",
        "release/1.0.x",
    ]
    assert found["master"] == git(lines["path"], "rev-parse", "master")


def tips(lines) -> dict[str, str]:
    return {b: git(lines["path"], "rev-parse", b) for b in ("master", "3.14.x", "2.0.x")}


def test_merge_base_times_with_the_checkout(lines, tmp_path):
    t = tips(lines)
    logs: list[str] = []
    c = checkout_of(lines, tmp_path, "from-3.14.x")
    found = RealGit(cwd=str(c), protocols=("file",), log=logs.append).merge_base_times(
        lines["url"], list(t.values())
    )
    assert found == {t["master"]: T + 1000, t["3.14.x"]: T + 1500, t["2.0.x"]: None}
    times = f"{t['master'][:12]} {T + 1000}, {t['3.14.x'][:12]} {T + 1500}, {t['2.0.x'][:12]} none"
    assert logs == [f"merge-base times of HEAD with {lines['url']}: {times}"]


def fetches(run: Recorder) -> list[list[str]]:
    return [args for args, _ in run.calls if "fetch" in args]


def test_the_commits_are_fetched_in_one_fetch(lines, tmp_path):
    run = Recorder()
    c = checkout_of(lines, tmp_path, "from-3.14.x")
    RealGit(cwd=str(c), protocols=("file",), run=run).merge_base_times(
        lines["url"], list(tips(lines).values())
    )
    [fetch] = fetches(run)
    assert len([a for a in fetch if is_object_id(a)]) == 3


def test_a_shallow_checkout_has_no_merge_base_and_fetches_nothing(lines, tmp_path):
    # Each fetch would bring a line's history into the checkout, and still
    # no merge-base would be found.
    t = tips(lines)
    c = checkout_of(lines, tmp_path, "from-3.14.x", "--depth", "1")
    run, logs = Recorder(), []
    g = RealGit(cwd=str(c), protocols=("file",), run=run, log=logs.append)
    assert g.merge_base_times(lines["url"], list(t.values())) == dict.fromkeys(t.values())
    assert fetches(run) == [] and "merge-base" not in run.commands()
    assert logs == ["the checkout is shallow: no merge-base is known"]


def test_a_commit_that_cannot_be_fetched_is_an_error(lines, tmp_path):
    c = checkout_of(lines, tmp_path, "from-3.14.x")

    def fail_fetch(args, cwd, stdin, env):
        if "fetch" in args:
            return Completed(128, b"", "fatal: unable to access: 502")
        return run_command(args, cwd, stdin, env)

    g = RealGit(cwd=str(c), protocols=("file",), run=fail_fetch)
    with pytest.raises(GitError, match="502"):
        g.merge_base_times(lines["url"], [tips(lines)["master"]])


# ---------------------------------------------------------------------------
# The fake answers as the real one does
# ---------------------------------------------------------------------------


def test_the_fake_answers_from_its_dictionaries_and_refuses_what_the_real_one_refuses():
    url = "https://github.com/bureau14/qdb-duck.git"
    g = FakeGit(
        refs={
            (url, "refs/heads/master"): A,
            (url, "refs/heads/3.14.x"): B,
            (url, "refs/heads/feature"): B,
            (url, "refs/tags/4.0.x"): A,
        },
        trees={(url, A): {".buildkite/a.yml": "x", "README.md": "y"}},
        times={B: 2},
    )
    assert g.commits_of(url, ["refs/heads/nope", "refs/heads/master"]) == {"refs/heads/master": A}
    assert g.read_files(url, A, ".buildkite", every_file) == {".buildkite/a.yml": b"x"}
    assert g.branches(url) == {"master": A, "3.14.x": B, "feature": B}, "no tag"
    assert g.merge_base_times(url, [A, B]) == {A: None, B: 2}
    with pytest.raises(GitError, match="no commit"):
        g.read_files(url, B, ".buildkite", every_file)
    with pytest.raises(GitError, match="refused"):
        g.commits_of(url, ["master"])
    assert g.calls[0] == ("commits_of", url, ("refs/heads/nope", "refs/heads/master"))
    forged = FakeGit({}, trees={(url, A): {".buildkite/a\nb.yml": "x"}})
    with pytest.raises(GitError, match="refused path"):
        forged.read_files(url, A, ".buildkite", every_file)


def test_the_fake_checkout_answers_from_its_tree():
    own = FakeCheckout(A, {".buildkite/a.yml": b"x", "README.md": "y"})
    assert own.head_commit() == A
    assert own.checkout_files(".buildkite", every_file) == {".buildkite/a.yml": b"x"}
    with pytest.raises(GitError, match="refused path"):
        own.checkout_files("../x", every_file)
