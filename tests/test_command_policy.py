import shlex

import pytest

from vikram.command_policy import (
    CommandPolicyError,
    build_policy,
    load_command_policy,
    merge_policy_data,
)
from vikram.settings import VikramSettings


def _classify(policy, command):
    return policy.classify(shlex.split(command), command)


def _shared_policy_path():
    return VikramSettings(_env_file=None).spec_root / "shared" / "command_policy.toml"


def test_shared_policy_classifies_tiers():
    policy = load_command_policy(_shared_policy_path())

    assert _classify(policy, "git status")[0] == "auto"
    assert _classify(policy, "git log --oneline")[0] == "auto"
    assert _classify(policy, "echo hi")[0] == "approve"
    assert _classify(policy, "git commit -m x")[0] == "approve"

    decision, message = _classify(policy, "git push --force origin main")
    assert decision == "deny"
    assert "force" in message


def test_deny_wins_over_read_only_and_approval():
    policy = load_command_policy(_shared_policy_path())

    # `git diff` is read-only, but touching a secret file is denied first.
    decision, message = _classify(policy, "git diff .env.production")
    assert decision == "deny"
    assert "secret" in message

    # .env.example is excluded from the secret deny.
    assert _classify(policy, "cat .env.example")[0] == "approve"


def test_missing_policy_file_raises():
    with pytest.raises(CommandPolicyError):
        load_command_policy(_shared_policy_path().parent / "does_not_exist.toml")


def test_override_replaces_read_only_and_appends_deny():
    base = {
        "read_only": {
            "always": ["pwd"],
            "git": {
                "subcommands": ["status", "log"],
                "forbidden_flag_substrings": [],
                "mutating_subcommand_args": [],
            },
        },
        "deny": {
            "rule": [
                {
                    "name": "force push",
                    "executable": "git",
                    "subcommand": "push",
                    "any_flag": ["--force"],
                    "message": "no force push",
                }
            ]
        },
    }
    override = {
        "read_only": {"git": {"subcommands": ["status"]}},  # replaces the list
        "deny": {
            "rule": [
                {
                    "name": "no npm publish",
                    "executable": "npm",
                    "subcommand": "publish",
                    "message": "no publishing",
                }
            ]
        },
    }

    merged = merge_policy_data(base, override)
    policy = build_policy(merged)

    # read_only.git.subcommands was replaced: log is no longer auto.
    assert _classify(policy, "git status")[0] == "auto"
    assert _classify(policy, "git log")[0] == "approve"
    # forbidden_flag_substrings under git was preserved (merge, not replace).
    assert merged["read_only"]["git"]["forbidden_flag_substrings"] == []

    # The shared deny rule survives AND the override rule is appended.
    assert _classify(policy, "git push --force origin main")[0] == "deny"
    assert _classify(policy, "npm publish")[0] == "deny"


def test_override_cannot_remove_shared_deny_rule():
    base = {
        "deny": {
            "rule": [
                {
                    "name": "force push",
                    "executable": "git",
                    "subcommand": "push",
                    "any_flag": ["--force"],
                    "message": "no force push",
                }
            ]
        }
    }
    # An override that supplies an empty rule list must not drop the shared rule.
    override = {"deny": {"rule": []}}

    policy = build_policy(merge_policy_data(base, override))
    assert _classify(policy, "git push --force origin main")[0] == "deny"


def test_delete_refspec_prefix_match():
    policy = load_command_policy(_shared_policy_path())

    assert _classify(policy, "git push origin :main")[0] == "deny"
    # A normal src:dst refspec is not a deletion and is allowed to reach approval.
    assert _classify(policy, "git push origin main:main")[0] == "approve"
