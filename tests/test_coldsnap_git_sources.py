# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import subprocess

import pytest

from sparkrun.plugins.coldsnap import git_sources


REVISION = "a" * 40
URL = "https://github.com/example/source.git"


def install_git(monkeypatch, *, failure="", gh=False, actual=REVISION):
    calls = []
    monkeypatch.setattr(git_sources.shutil, "which", lambda name: "/usr/bin/" + name if name == "git" or gh else None)
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["timeout"] == 180
        failed = failure in command and "-c" not in command
        return subprocess.CompletedProcess(command, int(failed), actual if "rev-parse" in command else "", "fetch denied" if failed else "")
    monkeypatch.setattr(git_sources.subprocess, "run", run)
    return calls


def test_https_fetch_is_commit_verified_and_noninteractive(tmp_path, monkeypatch):
    calls = install_git(monkeypatch)
    git_sources.clone_pinned_source(tmp_path / "source", URL, "refs/tags/v1.0.0", REVISION)
    assert len(calls) == 5
    assert "fetch" in calls[2] and calls[2][-1] == "refs/tags/v1.0.0"
    assert calls[-1][-2:] == ["--verify", "HEAD^{commit}"]
    assert all("credential.helper" not in " ".join(command) for command in calls)


@pytest.mark.parametrize("failure", ["init", "remote", "fetch", "checkout", "rev-parse"])
def test_git_failure_stops_at_the_first_failed_command(tmp_path, monkeypatch, failure):
    calls = install_git(monkeypatch, failure=failure)
    with pytest.raises(RuntimeError, match="fetch denied"):
        git_sources.clone_pinned_source(tmp_path / "source", URL, "refs/tags/v1.0.0", REVISION)
    assert failure in calls[-1]
    if failure == "fetch":
        assert not any("checkout" in command or "rev-parse" in command for command in calls)


def test_github_can_use_local_gh_credentials_without_persisting_them(tmp_path, monkeypatch):
    calls = install_git(monkeypatch, failure="fetch", gh=True)
    git_sources.clone_pinned_source(tmp_path / "source", URL, "refs/tags/v1.0.0", REVISION)
    authenticated = calls[3]
    assert "credential.helper=!gh auth git-credential" in authenticated
    assert "fetch" in authenticated
    assert not any("config" in command for command in calls)
    assert all("StrictHostKeyChecking=no" not in " ".join(command) for command in calls)


def test_non_github_url_never_receives_github_credentials(tmp_path, monkeypatch):
    calls = install_git(monkeypatch, failure="fetch", gh=True)
    with pytest.raises(RuntimeError, match="repository read access"):
        git_sources.clone_pinned_source(tmp_path / "source", "https://other.example/repo.git", "main", REVISION)
    assert not any("-c" in command for command in calls)


def test_existing_controller_ssh_access_is_a_noninteractive_github_fallback(tmp_path, monkeypatch):
    calls = install_git(monkeypatch, failure="fetch")
    monkeypatch.setattr(git_sources.shutil, "which", lambda name: "/usr/bin/" + name if name in {"git", "ssh"} else None)
    git_sources.clone_pinned_source(tmp_path / "source", URL, "main", REVISION)
    fallback = calls[3]
    assert "core.sshCommand=ssh -o BatchMode=yes" in fallback
    assert "git@github.com:example/source.git" in fallback
    assert "StrictHostKeyChecking=no" not in " ".join(fallback)
    assert not any("config" in command for command in calls)


def test_source_commit_mismatch_is_rejected(tmp_path, monkeypatch):
    install_git(monkeypatch, actual="b" * 40)
    with pytest.raises(RuntimeError, match="expected pinned commit"):
        git_sources.clone_pinned_source(tmp_path / "source", URL, "main", REVISION)


def test_source_url_cannot_persist_or_log_embedded_credentials(tmp_path, monkeypatch):
    calls = install_git(monkeypatch)
    with pytest.raises(ValueError, match="must not embed credentials") as error:
        git_sources.clone_pinned_source(tmp_path / "source", "https://user:secret@github.com/example/repo.git", "main", REVISION)
    assert "secret" not in str(error.value)
    assert not calls
