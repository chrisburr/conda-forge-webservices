"""Open a version update pull request for a CI job that proved who it is.

The request carries no secret. A job sends the identity token its provider
issued it, `trusted_publishing` checks that token against the list the
feedstock keeps in its own conda-forge.yml, and the pull request is then made
by conda-forge-admin exactly as the `@conda-forge-admin, please update version`
comment would have made it.

The caller chooses only the feedstock, the branch and the version string. The
branch is resolved to a commit through the API, and that one commit is both
where conda-forge.yml is read and where the pull request starts. The version
is checked with commands.valid_input_version, as a version asked for in an
issue command is, so that it cannot inject anything into the recipe; the
update itself runs in a container, in a job without secrets. Which sources
the version then names is up to the recipe, and a human still merges the
pull request.
"""

import dataclasses
import logging
import os
import re
import threading
import uuid
from typing import Any

import cachetools
import github
import requests
import yaml

from conda_forge_webservices.commands import (
    admin_feedstock_branch,
    make_rerender_dummy_commit,
    open_admin_pr,
    update_version,
    valid_input_version,
)
from conda_forge_webservices.tokens import get_gh_client
from conda_forge_webservices.trusted_publishing import (
    GITHUB_ISSUER,
    TrustedPublishingError,
    authorize,
    describe,
    parse_publishers,
    verify_token,
)

LOGGER = logging.getLogger("conda_forge_webservices.trusted_version_updates")

DOCS = "https://conda-forge.org/docs/maintainer/conda_forge_yml/#trusted-publishers"

FEEDSTOCK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}-feedstock")

# what `git check-ref-format --branch` would take, narrowed: slash separated
# parts that are not empty, do not start with a dot and do not end in .lock.
# These are all used with fullmatch, since $ also matches before a newline.
_BRANCH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

# One in flight per feedstock branch is enough for releases, and it keeps a
# job stuck in a retry loop from opening a pull request per attempt.
COOLDOWN = 60
_RECENT: cachetools.TTLCache = cachetools.TTLCache(maxsize=1024, ttl=COOLDOWN)
_RECENT_LOCK = threading.Lock()

# A branch's resolved commit and publishers are kept this long, so that
# anyone who can get a genuine token, which is anyone, cannot make every
# request cost several calls to GitHub. Removing a publisher from
# conda-forge.yml takes effect after at most this long.
CONFIG_TTL = 60
_CONFIGS: cachetools.TTLCache = cachetools.TTLCache(maxsize=1024, ttl=CONFIG_TTL)
_CONFIGS_LOCK = threading.Lock()

# what a caller refused as retryable is told to wait, which is enough for the
# key refresh that the refusal asked for
RETRY_AFTER = 15

# set in a live test, whose webservices version is not a ref to dispatch at
DISPATCH_REF_ENV = "CF_TRUSTED_PUBLISHING_DISPATCH_REF"


class BadRequest(Exception):
    """A request we will not act on, with the status to answer it with."""

    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


@dataclasses.dataclass(frozen=True)
class Authorized:
    """A request authorize_request has accepted, and where it applies."""

    feedstock: str
    version: str
    default_branch: str
    branch: str
    sha: str
    claims: dict[str, Any]


def _refused(err: TrustedPublishingError) -> BadRequest:
    if err.retryable:
        return BadRequest(503, str(err), retry_after=RETRY_AFTER)
    return BadRequest(403, str(err))


def bearer_token(authorization: str) -> str:
    """The token of an Authorization header, or "" if it carries none.

    tornado strips the trailing space from "Bearer ", so the scheme is split
    off rather than removed as a prefix.
    """
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def valid_branch(branch: str) -> bool:
    if not 0 < len(branch) <= 100 or ".." in branch:
        return False
    return all(
        _BRANCH_PART.fullmatch(part) and not part.endswith((".lock", "."))
        for part in branch.split("/")
    )


def _check_request(feedstock: Any, version: Any, branch: Any) -> None:
    if not isinstance(feedstock, str) or not FEEDSTOCK.fullmatch(feedstock):
        raise BadRequest(
            400, f"{feedstock!r} is not the name of a conda-forge feedstock"
        )

    # checked again by update_version, but here before the token is spent
    if not valid_input_version(version):
        raise BadRequest(
            400, f"{version!r} is not a version we will write into a recipe"
        )

    if branch is not None and (not isinstance(branch, str) or not valid_branch(branch)):
        raise BadRequest(400, f"{branch!r} is not a branch name")


def _unavailable(err: Exception) -> BadRequest:
    LOGGER.warning("could not read a feedstock from GitHub: %r", err)
    return BadRequest(
        503, "GitHub could not be reached, try again shortly", retry_after=RETRY_AFTER
    )


def _resolve(feedstock: str, branch: str | None) -> tuple[str, str, str, list]:
    """The default branch, the branch, its commit and its trusted publishers.

    The branch is looked up through the API, so a name can only ever mean a
    branch of conda-forge/<feedstock>, and conda-forge.yml is read at the
    commit it points to, which is where the pull request will start too. What
    it finds is kept for CONFIG_TTL, a missing branch or a broken config too.
    """
    key = (feedstock, branch)
    with _CONFIGS_LOCK:
        cached = _CONFIGS.get(key)
    if isinstance(cached, BadRequest):
        raise cached
    if cached is not None:
        return cached

    try:
        resolved = _fetch_config(feedstock, branch)
    except BadRequest as err:
        if err.status in (404, 422):
            with _CONFIGS_LOCK:
                _CONFIGS[key] = err
        raise

    with _CONFIGS_LOCK:
        _CONFIGS[key] = resolved
    return resolved


def _fetch_config(feedstock: str, branch: str | None) -> tuple[str, str, str, list]:
    try:
        try:
            repo = get_gh_client().get_repo(f"conda-forge/{feedstock}")
            default_branch = repo.default_branch
        except github.UnknownObjectException as err:
            raise BadRequest(404, f"there is no conda-forge/{feedstock}") from err
        target = branch or default_branch

        try:
            sha = repo.get_branch(target).commit.sha
        except github.UnknownObjectException as err:
            raise BadRequest(
                404, f"conda-forge/{feedstock} has no branch {target}"
            ) from err

        try:
            text = repo.get_contents("conda-forge.yml", ref=sha).decoded_content
        except github.UnknownObjectException as err:
            raise BadRequest(
                404, f"conda-forge/{feedstock} has no conda-forge.yml on {target}"
            ) from err
    except github.GithubException as err:
        # 403 is how GitHub says a rate limit was hit
        if err.status in (403, 429) or err.status >= 500:
            raise _unavailable(err) from err
        raise
    except (requests.RequestException, OSError) as err:
        raise _unavailable(err) from err

    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise BadRequest(
            422, f"conda-forge/{feedstock}'s conda-forge.yml on {target} does not parse"
        ) from err
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise BadRequest(
            422,
            f"conda-forge/{feedstock}'s conda-forge.yml on {target} is not a mapping",
        )

    return (
        default_branch,
        target,
        sha,
        parse_publishers(config.get("trusted_publishers")),
    )


def authorize_request(
    token: str, feedstock: Any, version: Any, branch: Any
) -> Authorized:
    """Decide whether a request may open a version update pull request.

    Raises BadRequest. The token is verified before anything about the
    feedstock is read, so a caller that has not proved who it is costs no
    request to GitHub.
    """
    if not token:
        raise BadRequest(401, "no identity token was sent")

    _check_request(feedstock, version, branch)

    try:
        claims = verify_token(token)
    except TrustedPublishingError as err:
        raise _refused(err) from err

    default_branch, target, sha, publishers = _resolve(feedstock, branch)
    if not publishers:
        raise BadRequest(
            403,
            f"conda-forge/{feedstock} lists no trusted publishers on {target}. "
            f"Add a trusted_publishers section to its conda-forge.yml: {DOCS}",
        )

    # The cooldown is checked before authorize spends the token, since a job
    # on GitLab cannot get another, and held so two requests cannot both pass.
    with _RECENT_LOCK:
        if (feedstock, target) in _RECENT:
            raise BadRequest(
                429,
                f"a version update for {feedstock} on {target} was requested "
                f"less than {COOLDOWN}s ago",
                retry_after=COOLDOWN,
            )
        try:
            authorize(claims, publishers)
        except TrustedPublishingError as err:
            raise _refused(err) from err
        _RECENT[feedstock, target] = True

    return Authorized(feedstock, version, default_branch, target, sha, claims)


def release_cooldown(request: Authorized) -> None:
    """Let the feedstock branch be asked for again, when nothing was opened."""
    with _RECENT_LOCK:
        _RECENT.pop((request.feedstock, request.branch), None)


def _requester(claims: dict[str, Any]) -> str:
    """Markdown naming the job that asked, which cannot mention or format.

    The ref in it is a branch name that whoever pushes to the publisher chose.
    """
    text = "`" + describe(claims).replace("`", "'") + "`"

    if claims["iss"] == GITHUB_ISSUER:
        repository, run = str(claims.get("repository")), str(claims.get("run_id"))
        if (
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            and run.isdigit()
        ):
            text += f" ([run](https://github.com/{repository}/actions/runs/{run}))"
    else:
        project, job = str(claims.get("project_path")), str(claims.get("job_id"))
        if (
            re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+", project)
            and job.isdigit()
        ):
            text += f" ([job]({claims['iss']}/{project}/-/jobs/{job}))"

    return text


def open_version_update_pr(request: Authorized) -> dict[str, Any]:
    """Open the draft pull request and set the version updater going on it.

    Raises only if no pull request was opened. Once one is, a version update
    that does not start is said on the pull request and in the answer, rather
    than raised, so that a caller retrying does not open a second one.
    """
    # an account rather than the app, since only an account can hold the fork,
    # as the "please update version" command does
    gh = github.Github(auth=github.Auth.Token(os.environ["GH_TOKEN"]))
    repo = gh.get_repo(f"conda-forge/{request.feedstock}")
    branch_name = f"version-update-{request.version}-{uuid.uuid4().hex[:8]}"

    with admin_feedstock_branch(
        gh,
        "conda-forge",
        request.feedstock,
        request.default_branch,
        branch_name,
        start_point=request.sha,
    ) as (git_repo, forked_user):
        make_rerender_dummy_commit(git_repo, skip_ci=True)
        pr = open_admin_pr(
            repo,
            git_repo,
            forked_user,
            branch_name,
            base=request.branch,
            title=f"chore: update version to {request.version}",
            body=(
                "Hi! This is the friendly automated conda-forge-webservice.\n\n"
                f"{_requester(request.claims)} asked for this feedstock to be "
                f"updated to `{request.version}` using trusted publishing.\n\n"
                "I'm updating the recipe now and will push to this pull "
                "request shortly.\n"
            ),
            draft=True,
        )

    try:
        failed = update_version(
            f"conda-forge/{request.feedstock}",
            pr.number,
            request.version,
            dispatch_ref=os.environ.get(DISPATCH_REF_ENV) or None,
        )
    except Exception:
        LOGGER.exception("could not start the version update on %s", pr.html_url)
        failed = True

    if failed:
        try:
            pr.create_issue_comment(
                "Hi! This is the friendly automated conda-forge-webservice.\n\n"
                "I opened this pull request but could not start the version "
                "update. Comment `@conda-forge-admin, please update version` "
                "here to try again."
            )
        except Exception:
            LOGGER.exception("could not comment on %s", pr.html_url)

    return {
        "pull_request": pr.number,
        "url": pr.html_url,
        "version_update_started": not failed,
    }
