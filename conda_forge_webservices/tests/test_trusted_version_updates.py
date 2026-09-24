import github
import pytest
import requests

from conda_forge_webservices import trusted_version_updates as tvu
from conda_forge_webservices.trusted_publishing import (
    GITHUB_ISSUER,
    TrustedPublishingError,
)

CONFIG = """\
conda_build_tool: rattler-build
trusted_publishers:
  - provider: github
    repository: DIRACGrid/DIRAC
    repository_owner_id: 1234
    workflow: deploy.yml
"""

CLAIMS = {
    "iss": GITHUB_ISSUER,
    "repository": "DIRACGrid/DIRAC",
    "job_workflow_ref": "DIRACGrid/DIRAC/.github/workflows/deploy.yml@refs/tags/v1",
    "run_id": "42",
}

SHA = "a" * 40


def _not_found():
    return github.UnknownObjectException(404, {"message": "Not Found"}, {})


class _Repo:
    """conda-forge/<feedstock> as the API answers for it, counting calls."""

    def __init__(self, calls, default_branch="main", branches=None, configs=None):
        self.calls = calls
        self.default_branch = default_branch
        self.branches = {"main": SHA} if branches is None else branches
        self.configs = {SHA: CONFIG} if configs is None else configs

    def get_branch(self, name):
        self.calls.append(("get_branch", name))
        if name not in self.branches:
            raise _not_found()
        sha = self.branches[name]
        return type("B", (), {"commit": type("C", (), {"sha": sha})})()

    def get_contents(self, path, ref):
        self.calls.append(("get_contents", path, ref))
        if ref not in self.configs:
            raise _not_found()
        text = self.configs[ref]
        if isinstance(text, Exception):
            raise text
        return type("F", (), {"decoded_content": text.encode()})()


class _GitHub:
    def __init__(self, repos, calls):
        self.repos = repos
        self.calls = calls

    def get_repo(self, name):
        self.calls.append(("get_repo", name))
        if name not in self.repos:
            raise _not_found()
        return self.repos[name]


@pytest.fixture(autouse=True)
def _fresh():
    tvu._RECENT.clear()
    tvu._CONFIGS.clear()
    yield
    tvu._RECENT.clear()
    tvu._CONFIGS.clear()


@pytest.fixture
def calls():
    return []


@pytest.fixture
def repo(calls, monkeypatch):
    """conda-forge/lbenv-feedstock, whose job DIRACGrid/DIRAC may publish."""
    repo = _Repo(calls)
    gh = _GitHub({"conda-forge/lbenv-feedstock": repo}, calls)
    monkeypatch.setattr(tvu, "get_gh_client", lambda: gh)
    monkeypatch.setattr(tvu, "verify_token", lambda token: CLAIMS)
    monkeypatch.setattr(tvu, "authorize", lambda claims, publishers: publishers[0])
    return repo


def _refuse(message, retryable=False):
    def _raise(*args):
        raise TrustedPublishingError(message, retryable=retryable)

    return _raise


@pytest.mark.parametrize(
    "version",
    [
        "1.2.3",
        "2026.9.10",
        "1.2.3rc1",
        "1.0.0.post1",
        "1!2.0.0",
        "v2.4.0",
        "1.2.3+cuda",
    ],
)
def test_versions_we_write_into_a_recipe(version):
    assert tvu.valid_input_version(version)


@pytest.mark.parametrize(
    "version",
    [
        "",
        " 1.2.3",
        "1.2.3 ",
        # a conda version cannot hold a dash, so a tag is not passed through
        "1.0.0-rc1",
        # the version reaches source.url, so none of these may
        '1.2"',
        "1.2.3'",
        "{{ version }}",
        "${version}",
        "$(whoami)",
        "1.2.3;rm -rf /",
        "../../etc/passwd",
        "1.2.3/../..",
        "1" * 65,
        # a pull request branch named after it would not be a valid ref
        "1..2",
        "1.",
        "1.2.",
    ],
)
def test_versions_we_refuse(version):
    assert not tvu.valid_input_version(version)


@pytest.mark.parametrize(
    "feedstock", ["lbenv-feedstock", "python-feedstock", "r-base_1-feedstock"]
)
def test_feedstock_names_we_accept(feedstock):
    assert tvu.FEEDSTOCK.fullmatch(feedstock)


@pytest.mark.parametrize(
    "feedstock",
    [
        "staged-recipes",
        "lbenv",
        "../lbenv-feedstock",
        "conda-forge/lbenv-feedstock",
        "",
        "lbenv-feedstock\n",
    ],
)
def test_feedstock_names_we_refuse(feedstock):
    assert not tvu.FEEDSTOCK.fullmatch(feedstock)


@pytest.mark.parametrize(
    "header,token",
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("bearer abc.def.ghi", "abc.def.ghi"),
        ("Bearer  abc.def.ghi ", "abc.def.ghi"),
        # tornado strips the trailing space of an empty "Bearer "
        ("Bearer", ""),
        ("Bearer ", ""),
        ("", ""),
        ("Basic dXNlcjpwYXNz", ""),
        ("abc.def.ghi", ""),
    ],
)
def test_the_token_is_read_from_a_bearer_header(header, token):
    assert tvu.bearer_token(header) == token


@pytest.mark.parametrize(
    "branch", ["main", "2.4.x", "release/2.4", "v2_x", "feature/a-b.c"]
)
def test_branch_names_we_accept(branch):
    assert tvu.valid_branch(branch)


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "x/../../../attacker/their-repo/main",
        "..",
        "a..b",
        "../main",
        "main/..",
        "/main",
        "main/",
        "a//b",
        ".hidden",
        "a/.hidden",
        "main.lock",
        "main.",
        "refs/heads/../x",
        "a b",
        "a~1",
        "a^",
        "a:b",
        "a@{1}",
        "a\\b",
        "x" * 101,
        "main\n",
        "release/2.4\n",
    ],
)
def test_branch_names_we_refuse(branch):
    assert not tvu.valid_branch(branch)


def test_a_request_without_a_token_is_refused(repo, calls):
    with pytest.raises(tvu.BadRequest, match="no identity token") as err:
        tvu.authorize_request("", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 401
    assert calls == []


@pytest.mark.parametrize(
    "feedstock,version,branch",
    [
        ("lbenv", "1.2.3", None),
        ("lbenv-feedstock", "1.0.0-rc1", None),
        ("lbenv-feedstock", "1..2", None),
        ("lbenv-feedstock", "1.2.3", "../main"),
        # would reach another repository's config through dot segments
        ("numpy-feedstock", "1.2.3", "x/../../../attacker/their-repo/main"),
        ("lbenv-feedstock", "1.2.3", "main.lock"),
        ("lbenv-feedstock", None, None),
        (None, "1.2.3", None),
    ],
)
def test_a_request_we_will_not_read_is_refused(repo, calls, feedstock, version, branch):
    with pytest.raises(tvu.BadRequest) as err:
        tvu.authorize_request("token", feedstock, version, branch)
    assert err.value.status == 400
    assert calls == [], "asked GitHub about a request that was never valid"


def test_a_token_that_is_not_genuine_is_refused_before_any_fetch(
    repo, calls, monkeypatch
):
    monkeypatch.setattr(tvu, "verify_token", _refuse("the token is not valid"))
    with pytest.raises(tvu.BadRequest, match="not valid") as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 403
    assert calls == [], "asked GitHub on behalf of a caller that proved nothing"


def test_a_retryable_refusal_says_when_to_retry(repo, monkeypatch):
    monkeypatch.setattr(
        tvu, "verify_token", _refuse("keys not loaded yet", retryable=True)
    )
    with pytest.raises(tvu.BadRequest) as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 503
    assert err.value.retry_after == tvu.RETRY_AFTER


def test_the_default_branch_is_resolved_and_read_at_its_commit(repo, calls):
    request = tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)

    assert (request.default_branch, request.branch, request.sha) == (
        "main",
        "main",
        SHA,
    )
    assert request.claims is CLAIMS
    # read at the commit the branch pointed to, which is where the pull request
    # starts, and only from conda-forge/<feedstock>
    assert calls == [
        ("get_repo", "conda-forge/lbenv-feedstock"),
        ("get_branch", "main"),
        ("get_contents", "conda-forge.yml", SHA),
    ]


def test_a_branch_is_only_ever_a_branch_of_the_feedstock(repo, calls):
    """A name shaped like a commit or a tag is looked up as a branch."""
    repo.branches = {"main": SHA, "2.4.x": "b" * 40}
    repo.configs = {SHA: CONFIG, "b" * 40: CONFIG}

    request = tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", "2.4.x")
    assert (request.default_branch, request.branch, request.sha) == (
        "main",
        "2.4.x",
        "b" * 40,
    )

    with pytest.raises(tvu.BadRequest, match="has no branch") as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", "c" * 40)
    assert err.value.status == 404
    assert ("get_branch", "c" * 40) in calls
    assert all(call[0] != "get_contents" or call[2] != "c" * 40 for call in calls)


@pytest.mark.parametrize(
    "setup,message",
    [
        (lambda repo, gh: gh.repos.clear(), "there is no conda-forge/lbenv"),
        (lambda repo, gh: repo.branches.clear(), "has no branch main"),
        (lambda repo, gh: repo.configs.clear(), "has no conda-forge.yml on main"),
    ],
)
def test_what_is_missing_is_not_found(repo, calls, monkeypatch, setup, message):
    gh = tvu.get_gh_client()
    setup(repo, gh)
    with pytest.raises(tvu.BadRequest, match=message) as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 404


@pytest.mark.parametrize(
    "config,message",
    [
        ("- a\n- b\n", "is not a mapping"),
        ("just text\n", "is not a mapping"),
        ("a: [\n", "does not parse"),
    ],
)
def test_a_config_we_cannot_read_says_so(repo, config, message):
    repo.configs = {SHA: config}
    with pytest.raises(tvu.BadRequest, match=message) as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 422


@pytest.mark.parametrize(
    "failure",
    [
        github.GithubException(502, {"message": "Bad Gateway"}, {}),
        github.GithubException(503, {"message": "Unavailable"}, {}),
        github.GithubException(429, {"message": "Too Many Requests"}, {}),
        github.RateLimitExceededException(403, {"message": "rate limit"}, {}),
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
    ],
)
def test_github_being_unavailable_is_retryable(repo, failure):
    repo.configs = {SHA: failure}
    with pytest.raises(tvu.BadRequest) as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 503
    assert err.value.retry_after == tvu.RETRY_AFTER


def test_a_feedstock_that_has_not_opted_in_says_how_to(repo):
    repo.configs = {SHA: "bot:\n  automerge: true\n"}
    with pytest.raises(tvu.BadRequest, match="lists no trusted publishers") as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 403
    assert tvu.DOCS in err.value.message


def test_what_a_branch_resolves_to_is_kept_for_a_while(repo, calls, monkeypatch):
    """Anyone can get a genuine token, so each must not cost several calls."""
    monkeypatch.setattr(tvu, "authorize", _refuse("matches no trusted publisher"))
    for _ in range(3):
        with pytest.raises(tvu.BadRequest):
            tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert len(calls) == 3

    repo.branches.clear()
    for _ in range(3):
        with pytest.raises(tvu.BadRequest, match="has no branch"):
            tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", "gone")
    assert len(calls) == 5, "a missing branch was looked up again"


def test_a_token_that_matches_nothing_is_refused(repo, monkeypatch):
    monkeypatch.setattr(tvu, "authorize", _refuse("matches no trusted publisher"))
    with pytest.raises(tvu.BadRequest, match="matches no trusted publisher") as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    assert err.value.status == 403


def test_an_authorized_request_is_answered_once_per_cooldown(repo):
    tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)

    with pytest.raises(tvu.BadRequest, match="less than") as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.4", None)
    assert err.value.status == 429
    assert err.value.retry_after == tvu.COOLDOWN


def test_the_cooldown_is_per_branch(repo):
    """A release that updates two branches does not collide with itself."""
    repo.branches = {"main": SHA, "2.4.x": SHA}
    tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", "2.4.x")


def test_a_refused_token_does_not_spend_the_cooldown(repo, monkeypatch):
    monkeypatch.setattr(tvu, "authorize", _refuse("nope"))
    with pytest.raises(tvu.BadRequest):
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)

    monkeypatch.setattr(tvu, "authorize", lambda claims, publishers: None)
    tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)


def test_the_cooldown_does_not_spend_the_token(repo, monkeypatch):
    """A job on GitLab gets one token, so a 429 must leave it usable."""
    tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)

    spent = []
    monkeypatch.setattr(
        tvu, "authorize", lambda claims, publishers: spent.append(claims)
    )
    with pytest.raises(tvu.BadRequest) as err:
        tvu.authorize_request("token", "lbenv-feedstock", "1.2.4", None)
    assert err.value.status == 429
    assert spent == []


def test_releasing_the_cooldown_lets_the_branch_be_asked_for_again(repo):
    request = tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)
    tvu.release_cooldown(request)
    tvu.authorize_request("token", "lbenv-feedstock", "1.2.3", None)


REQUEST = tvu.Authorized(
    feedstock="lbenv-feedstock",
    version="1.2.3",
    default_branch="main",
    branch="2.4.x",
    sha="b" * 40,
    # the branch in it is whatever whoever pushed to the publisher chose
    claims=dict(
        CLAIMS,
        job_workflow_ref=(
            "DIRACGrid/DIRAC/.github/workflows/deploy.yml@refs/heads/"
            "@here **bold** `tick`"
        ),
    ),
)


class _PullRequest:
    number = 7
    html_url = "https://github.com/conda-forge/lbenv-feedstock/pull/7"

    def __init__(self):
        self.comments = []

    def create_issue_comment(self, body):
        self.comments.append(body)


@pytest.fixture
def opening(monkeypatch):
    """Record what open_version_update_pr asks of GitHub and git."""
    seen = {"pr": _PullRequest()}

    class _UserGitHub:
        def __init__(self, auth):
            seen["auth"] = auth

        def get_repo(self, name):
            seen["repo"] = name
            return "upstream repo"

    class _Branch:
        def __init__(self, gh, org, name, default_branch, branch_name, start_point):
            seen["admin_feedstock_branch"] = (
                gh,
                org,
                name,
                default_branch,
                start_point,
            )
            seen["branch_name"] = branch_name

        def __enter__(self):
            return "git repo", "conda-forge-admin"

        def __exit__(self, *args):
            return False

    def _open(repo, git_repo, forked_user, branch_name, **kwargs):
        seen["open_admin_pr"] = kwargs
        return seen["pr"]

    def _dummy(git_repo, skip_ci):
        seen["skip_ci"] = skip_ci

    monkeypatch.setenv("GH_TOKEN", "conda-forge-admin's token")
    monkeypatch.setattr(tvu.github, "Github", _UserGitHub)
    monkeypatch.setattr(tvu, "admin_feedstock_branch", _Branch)
    monkeypatch.setattr(tvu, "open_admin_pr", _open)
    monkeypatch.setattr(tvu, "make_rerender_dummy_commit", _dummy)
    monkeypatch.setattr(tvu, "update_version", lambda *a, **kw: False)
    return seen


def test_the_pull_request_is_opened_as_conda_forge_admin(opening):
    data = tvu.open_version_update_pr(REQUEST)

    assert data == {
        "pull_request": 7,
        "url": _PullRequest.html_url,
        "version_update_started": True,
    }
    # a real account, since the app cannot hold a fork
    assert opening["auth"].token == "conda-forge-admin's token"
    gh, org, name, default_branch, start_point = opening["admin_feedstock_branch"]
    assert isinstance(gh, tvu.github.Github)
    # the fork is synced to the real default branch, and the pull request cut
    # from the commit whose conda-forge.yml was read
    assert (org, name, default_branch, start_point) == (
        "conda-forge",
        "lbenv-feedstock",
        "main",
        "b" * 40,
    )
    assert opening["open_admin_pr"]["base"] == "2.4.x"
    assert opening["skip_ci"] is True


def test_the_requester_cannot_mention_or_format(opening):
    tvu.open_version_update_pr(REQUEST)
    body = opening["open_admin_pr"]["body"]

    requester = body.split(" asked for")[0].rsplit("\n", 1)[1]
    assert requester.startswith("`") and "`tick`" not in requester
    assert "@here **bold** 'tick'" in requester
    assert "(https://github.com/DIRACGrid/DIRAC/actions/runs/42)" in requester


@pytest.mark.parametrize("update", ["fails", "raises"])
def test_a_version_update_that_does_not_start_is_said(opening, monkeypatch, update):
    def _update(*args, **kwargs):
        if update == "raises":
            raise RuntimeError("dispatch failed")
        return True

    monkeypatch.setattr(tvu, "update_version", _update)
    data = tvu.open_version_update_pr(REQUEST)

    # the pull request exists either way, so the caller is told about it
    assert data["pull_request"] == 7
    assert data["version_update_started"] is False
    assert "please update version" in opening["pr"].comments[0]


def test_a_live_test_can_dispatch_at_its_branch(opening, monkeypatch):
    dispatched = {}
    monkeypatch.setattr(
        tvu, "update_version", lambda *a, **kw: dispatched.update(kw) or False
    )
    monkeypatch.setenv(tvu.DISPATCH_REF_ENV, "feat/x")
    tvu.open_version_update_pr(REQUEST)
    assert dispatched["dispatch_ref"] == "feat/x"

    monkeypatch.delenv(tvu.DISPATCH_REF_ENV)
    tvu.open_version_update_pr(REQUEST)
    assert dispatched["dispatch_ref"] is None
