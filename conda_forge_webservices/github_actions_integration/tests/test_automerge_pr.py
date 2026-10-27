import unittest.mock
from unittest.mock import MagicMock

import pytest

from conda_forge_webservices.utils import TRUSTED_PUBLISHING_MARKER

from ..automerge import automerge_pr


@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
def test_automerge_pr_bad_user(get_cfg_mock):
    get_cfg_mock.return_value = {}
    repo = MagicMock()
    repo.full_name = "go"

    pr = MagicMock()
    pr.user.login = "blah"

    pr_for_admin = MagicMock()
    pr_for_admin.user.login = "regro-cf-autotick-bot"

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "user blah" in reason
    get_cfg_mock.assert_called_once_with(pr)
    pr_for_admin.create_issue_comment.assert_not_called()
    pr_for_admin.get_issue_comments.assert_not_called()
    pr_for_admin.merge.assert_not_called()


@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
def test_automerge_pr_no_title_slug(get_cfg_mock):
    get_cfg_mock.return_value = {}
    repo = MagicMock()
    repo.full_name = "go"

    pr = MagicMock()
    pr.user.login = "regro-cf-autotick-bot"
    pr.title = "blah"

    pr_for_admin = MagicMock()
    pr_for_admin.user.login = "regro-cf-autotick-bot"

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "slug in the title" in reason
    get_cfg_mock.assert_called_once_with(pr)
    pr_for_admin.create_issue_comment.assert_not_called()
    pr_for_admin.get_issue_comments.assert_not_called()
    pr_for_admin.merge.assert_not_called()


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"bot": {}},
        {"bot": {"automerge": False}},
    ],
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
def test_automerge_pr_feedstock_off(get_cfg_mock, cfg):
    get_cfg_mock.return_value = cfg
    repo = MagicMock()
    repo.full_name = "go"

    pr = MagicMock()
    pr.user.login = "regro-cf-autotick-bot"
    pr.title = "[bot-automerge] blah"

    pr_for_admin = MagicMock()
    pr_for_admin.user.login = "regro-cf-autotick-bot"

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "off for this feedstock" in reason
    get_cfg_mock.assert_called_once_with(pr)
    pr_for_admin.create_issue_comment.assert_not_called()
    pr_for_admin.get_issue_comments.assert_not_called()
    pr_for_admin.merge.assert_not_called()


@pytest.mark.parametrize("fail", ["check", "status"])
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_required_checks_and_statuses"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_checks"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_statuses"
)
def test_automerge_pr_feedstock_status_or_check_fail(
    stat_mock, check_mock, req_mock, get_cfg_mock, fail
):
    check_mock.return_value = {"check1": True, "check2": True, "check3": False}
    stat_mock.return_value = {"status1": True, "status2": True, "status3": True}
    req_mock.return_value = ["check1", "check2", "status1", "status3"]
    get_cfg_mock.return_value = {"bot": {"automerge": True}}

    if fail == "check":
        check_mock.return_value["check2"] = False
    else:
        stat_mock.return_value["status1"] = False

    repo = MagicMock()
    repo.full_name = "go"

    pr = MagicMock()
    pr.user.login = "regro-cf-autotick-bot"
    pr.title = "[bot-automerge] blah"
    pr.is_merged.return_value = False

    pr_for_admin = MagicMock()
    pr_for_admin.user.login = "regro-cf-autotick-bot"
    pr_for_admin.get_issue_comments.return_value = []

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "pending statuses" in reason
    get_cfg_mock.assert_called_once_with(pr)
    check_mock.assert_called_once_with(repo, pr)
    stat_mock.assert_called_once_with(repo, pr)
    req_mock.assert_called_once_with(pr, get_cfg_mock.return_value)
    pr_for_admin.create_issue_comment.assert_called_once()
    pr_for_admin.get_issue_comments.assert_called()
    pr_for_admin.merge.assert_not_called()


@pytest.mark.parametrize("fail", ["check", "status"])
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_required_checks_and_statuses"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_checks"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_statuses"
)
def test_automerge_pr_feedstock_status_or_check_fail_and_pending(
    stat_mock, check_mock, req_mock, get_cfg_mock, fail
):
    check_mock.return_value = {"check1": True, "check2": None, "check3": None}
    stat_mock.return_value = {"status1": True, "status2": True, "status3": True}
    req_mock.return_value = ["check1", "check2", "status1", "status3"]
    get_cfg_mock.return_value = {"bot": {"automerge": True}}

    if fail == "check":
        check_mock.return_value["check1"] = False
    else:
        stat_mock.return_value["status1"] = False

    repo = MagicMock()
    repo.full_name = "go"

    pr = MagicMock()
    pr.user.login = "regro-cf-autotick-bot"
    pr.title = "[bot-automerge] blah"
    pr.is_merged.return_value = False

    pr_for_admin = MagicMock()
    pr_for_admin.user.login = "regro-cf-autotick-bot"
    pr_for_admin.get_issue_comments.return_value = []

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is None
    assert "pending statuses" in reason
    get_cfg_mock.assert_called_once_with(pr)
    check_mock.assert_called_once_with(repo, pr)
    stat_mock.assert_called_once_with(repo, pr)
    req_mock.assert_called_once_with(pr, get_cfg_mock.return_value)
    pr_for_admin.create_issue_comment.assert_not_called()
    pr_for_admin.get_issue_comments.assert_not_called()
    pr_for_admin.merge.assert_not_called()


@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_required_checks_and_statuses"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_checks"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_statuses"
)
def test_automerge_pr_feedstock_no_statuses_or_checks(
    stat_mock, check_mock, req_mock, get_cfg_mock
):
    check_mock.return_value = {}
    stat_mock.return_value = {}
    req_mock.return_value = []
    get_cfg_mock.return_value = {"bot": {"automerge": True}}

    repo = MagicMock()
    repo.full_name = "go"

    pr = MagicMock()
    pr.user.login = "regro-cf-autotick-bot"
    pr.title = "[bot-automerge] blah"
    pr.is_merged.return_value = False

    pr_for_admin = MagicMock()
    pr_for_admin.user.login = "regro-cf-autotick-bot"

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "At least one status or check must be required" in reason
    get_cfg_mock.assert_called_once_with(pr)
    check_mock.assert_called_once_with(repo, pr)
    stat_mock.assert_called_once_with(repo, pr)
    req_mock.assert_called_once_with(pr, get_cfg_mock.return_value)
    pr_for_admin.create_issue_comment.assert_not_called()
    pr_for_admin.get_issue_comments.assert_not_called()
    pr_for_admin.merge.assert_not_called()


def _trusted_publishing_pr(committers):
    pr = MagicMock()
    pr.user.login = "conda-forge-admin"
    pr.head.repo.owner.login = "conda-forge-admin"
    pr.head.ref = "version-update-1.0.0-0123abcd"
    pr.title = "chore: update version to 1.0.0"
    pr.body = f"asked for an update\n\n{TRUSTED_PUBLISHING_MARKER}\n"
    pr.is_merged.return_value = False
    commits = []
    for login in committers:
        c = MagicMock()
        c.author.login = login
        c.committer.login = login
        commits.append(c)
    pr.get_commits.return_value = commits
    return pr


@pytest.mark.parametrize("cfg", [{}, {"bot": {"automerge": False}}])
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
def test_automerge_pr_trusted_publishing_feedstock_off(get_cfg_mock, cfg):
    get_cfg_mock.return_value = cfg
    repo = MagicMock()
    pr = _trusted_publishing_pr(["conda-forge-webservices[bot]"])
    pr_for_admin = MagicMock()

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "off for this feedstock" in reason
    pr_for_admin.merge.assert_not_called()


@pytest.mark.parametrize(
    "committers",
    [
        ["conda-forge-webservices[bot]", "chrisburr"],
        # a commit whose email is not linked to any account
        ["conda-forge-webservices[bot]", None],
        ["conda-forge-webservices[bot]", "regro-cf-autotick-bot"],
    ],
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
def test_automerge_pr_trusted_publishing_other_committer(get_cfg_mock, committers):
    get_cfg_mock.return_value = {"bot": {"automerge": True}}
    repo = MagicMock()
    pr = _trusted_publishing_pr(committers)
    pr_for_admin = MagicMock()
    pr_for_admin.get_issue_comments.return_value = []

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert "non-bot commits" in reason
    pr_for_admin.create_issue_comment.assert_called_once()
    pr_for_admin.merge.assert_not_called()


@pytest.mark.parametrize(
    "login,owner,ref",
    [
        ("chrisburr", "chrisburr", "version-update-1.0.0-0123abcd"),
        ("conda-forge-admin", "chrisburr", "version-update-1.0.0-0123abcd"),
        # a PR opened for an issue command, whose body a maintainer can edit
        ("conda-forge-admin", "conda-forge-admin", "conda_forge_admin_85"),
        ("conda-forge-admin", "conda-forge-admin", "version-update-1.0.0"),
        ("conda-forge-admin", "conda-forge-admin", "xversion-update-1.0.0-0123abcd"),
    ],
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
def test_automerge_pr_not_trusted_publishing(get_cfg_mock, login, owner, ref):
    get_cfg_mock.return_value = {"bot": {"automerge": True}}
    repo = MagicMock()
    # the marker in the body is not enough
    pr = _trusted_publishing_pr(["conda-forge-webservices[bot]"])
    pr.user.login = login
    pr.head.repo.owner.login = owner
    pr.head.ref = ref
    pr_for_admin = MagicMock()

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    assert did_merge is False
    assert f"user {login} cannot automerge" in reason
    pr_for_admin.merge.assert_not_called()


@pytest.mark.parametrize("draft", [True, False])
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_conda_forge_config"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_required_checks_and_statuses"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_checks"
)
@unittest.mock.patch(
    "conda_forge_webservices.github_actions_integration.automerge._get_github_statuses"
)
def test_automerge_pr_trusted_publishing(
    stat_mock, check_mock, req_mock, get_cfg_mock, draft
):
    check_mock.return_value = {"linter": True}
    stat_mock.return_value = {}
    req_mock.return_value = ["linter"]
    get_cfg_mock.return_value = {"bot": {"automerge": True}}

    repo = MagicMock()
    pr = _trusted_publishing_pr(
        ["conda-forge-webservices[bot]", "conda-forge-webservices[bot]"]
    )
    fresh_pr = repo.get_pull.return_value
    fresh_pr.is_merged.return_value = False
    fresh_pr.draft = draft
    fresh_pr.mergeable = True
    pr_for_admin = MagicMock()
    pr_for_admin.get_issue_comments.return_value = []
    pr_for_admin.merge.return_value.merged = True

    did_merge, reason = automerge_pr(repo, pr, pr_for_admin)

    if draft:
        assert did_merge is None
        assert "draft" in reason
        pr_for_admin.merge.assert_not_called()
    else:
        assert did_merge is True
        pr_for_admin.merge.assert_called_once()
