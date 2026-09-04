#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
#
# ColdSnap plugin development setup. Usage: source dev.sh

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "dev.sh must be sourced: source dev.sh" >&2
    exit 1
fi

_sparkrun_coldsnap_dev_setup() {
    local script_dir
    local venv_dir
    local checkout
    local checkout_origin
    local branch
    local managed_checkout=0
    local repository="https://github.com/spark-arena/sparkrun.git"

    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)" || return 1
    venv_dir="$script_dir/.venv"
    branch="${SPARKRUN_BRANCH:-main}"

    if ! command -v uv >/dev/null 2>&1; then
        echo "uv is not installed. Install it from https://docs.astral.sh/uv/" >&2
        return 1
    fi

    if ! command -v git >/dev/null 2>&1; then
        echo "git is required to prepare the sparkrun checkout." >&2
        return 1
    fi

    if ! git check-ref-format --branch "$branch" >/dev/null 2>&1; then
        echo "SPARKRUN_BRANCH is not a valid Git branch name: $branch" >&2
        return 1
    fi

    if [[ -n "${SPARKRUN_CHECKOUT:-}" ]]; then
        checkout="$SPARKRUN_CHECKOUT"
        if [[ ! -d "$checkout" ]]; then
            echo "SPARKRUN_CHECKOUT is not a directory: $checkout" >&2
            return 1
        fi
        checkout="$(cd "$checkout" && pwd -P)" || return 1
        echo "Using local sparkrun checkout: $checkout"
    else
        checkout="$script_dir/.dev/sparkrun"
        managed_checkout=1

        if [[ -e "$checkout" && ! -d "$checkout/.git" ]]; then
            echo "Managed checkout path exists but is not a Git repository: $checkout" >&2
            return 1
        fi

        if [[ ! -d "$checkout/.git" ]]; then
            mkdir -p "$(dirname "$checkout")" || return 1
            echo "Cloning sparkrun branch $branch from $repository ..."
            git clone --single-branch --branch "$branch" "$repository" "$checkout" || return 1
        else
            checkout_origin="$(git -C "$checkout" remote get-url origin)" || return 1
            if [[ "$checkout_origin" != "$repository" ]]; then
                echo "Managed checkout has an unexpected origin: $checkout_origin" >&2
                echo "Expected: $repository" >&2
                return 1
            fi
            if [[ -n "$(git -C "$checkout" status --porcelain)" ]]; then
                echo "Managed sparkrun checkout has local changes; refusing to update: $checkout" >&2
                return 1
            fi
            echo "Updating sparkrun branch $branch from $repository ..."
            git -C "$checkout" fetch --prune origin "$branch" || return 1
            git -C "$checkout" switch --detach FETCH_HEAD || return 1
        fi
    fi

    if [[ ! -f "$checkout/pyproject.toml" || ! -f "$checkout/src/sparkrun/__init__.py" ]]; then
        echo "Selected path is not a sparkrun checkout: $checkout" >&2
        return 1
    fi

    export SPARKRUN_CHECKOUT="$checkout"
    export SPARKRUN_BRANCH="$branch"

    if [[ ! -x "$venv_dir/bin/python" ]]; then
        echo "Creating the ColdSnap plugin environment with uv ..."
        uv venv "$venv_dir" || return 1
    fi

    echo "Installing the selected sparkrun checkout as an editable dependency ..."
    uv pip install --python "$venv_dir/bin/python" --editable "$checkout[dev]" || return 1

    echo "Installing the ColdSnap plugin and its development tools ..."
    uv pip install --python "$venv_dir/bin/python" --project "$script_dir" --group dev || return 1
    uv pip install --python "$venv_dir/bin/python" --no-deps --editable "$script_dir" || return 1

    if ! uv pip check --python "$venv_dir/bin/python"; then
        echo "The selected sparkrun checkout does not satisfy the plugin's declared compatibility range." >&2
        echo "Select a compatible branch with SPARKRUN_BRANCH or use SPARKRUN_CHECKOUT." >&2
        return 1
    fi

    # shellcheck disable=SC1091
    source "$venv_dir/bin/activate" || return 1

    echo "Installing pre-commit hooks ..."
    if ! (cd "$script_dir" && "$venv_dir/bin/pre-commit" install); then
        echo "Warning: pre-commit hook installation failed." >&2
    fi

    if (( managed_checkout )); then
        echo "Using managed sparkrun $branch checkout at $checkout"
    fi
    echo "Done. The ColdSnap plugin development environment is active."
}

_sparkrun_coldsnap_dev_cleanup() {
    local status="$1"
    unset -f _sparkrun_coldsnap_dev_setup _sparkrun_coldsnap_dev_cleanup
    return "$status"
}

_sparkrun_coldsnap_dev_setup
_sparkrun_coldsnap_dev_cleanup "$?"
return $?
