# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Commit-verified Git retrieval using credentials only on the control node."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def clone_pinned_source(destination: Path, url: str, ref: str, revision: str) -> None:
    """Fetch a fresh checkout; never continue after a failed Git operation.

    Existing Git credential helpers are honored. On GitHub, an authenticated
    gh CLI is also usable without writing credentials into the checkout or
    changing global Git configuration. Existing controller-side SSH access is
    also usable as a GitHub fallback; host-key verification is never disabled.
    """
    if url.startswith(("https://", "http://")) and urlsplit(url).username is not None:
        raise ValueError("ColdSnap source URLs must not embed credentials; use a Git credential helper")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("ColdSnap source retrieval requires git on the control node")
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def run(arguments):
        try:
            result = subprocess.run(
                [git, *arguments], env=environment, text=True,
                capture_output=True, check=False, timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("ColdSnap source retrieval failed: %s" % error) from error
        return result

    def checked(arguments):
        result = run(arguments)
        if result.returncode:
            detail = (result.stderr or result.stdout or "Git command failed").strip()[-2000:]
            raise RuntimeError("ColdSnap source retrieval failed: %s" % detail)
        return result

    checked(["init", "--quiet", str(destination)])
    checked(["-C", str(destination), "remote", "add", "origin", url])
    fetch = ["-C", str(destination), "fetch", "--quiet", "--depth=1", "origin", ref]
    result = run(fetch)
    if result.returncode and url.startswith("https://github.com/") and shutil.which("gh"):
        result = run(["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *fetch])
    github_path = url.removeprefix("https://github.com/") if url.startswith("https://github.com/") else ""
    if result.returncode and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_path) and shutil.which("ssh"):
        result = run([
            "-c", "core.sshCommand=ssh -o BatchMode=yes",
            "-C", str(destination), "fetch", "--quiet", "--depth=1", "git@github.com:" + github_path, ref,
        ])
    if result.returncode:
        detail = (result.stderr or result.stdout or "Git fetch failed").strip()[-2000:]
        raise RuntimeError(
            "ColdSnap could not fetch pinned source %s on the control node: %s. "
            "For private sources, grant this user repository read access and configure control-node Git credentials, gh auth login, or SSH."
            % (url, detail)
        )
    checked(["-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
    actual = checked(["-C", str(destination), "rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    if actual != revision:
        raise RuntimeError("ColdSnap source %s resolved to %s, expected pinned commit %s" % (url, actual, revision))
