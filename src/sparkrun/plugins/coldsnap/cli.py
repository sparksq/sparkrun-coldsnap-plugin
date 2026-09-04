# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Click surface loaded lazily by the first-party ColdSnap plugin."""

from __future__ import annotations

import json

from sparkrun.plugins.coldsnap.service import ColdSnapService


def _recipe_help(summary: str) -> str:
    return "%s\n\nRECIPE is a recipe name or YAML path." % summary


def build_command():
    # Plugin discovery recursively imports modules while initializing the
    # library API.  Keep Click (and the CLI-heavy public facade) behind the
    # command loader so ``import sparkrun.api`` remains console-free.
    import click

    from sparkrun.cli._common import CLUSTER_NAME, RECIPE_NAME, dry_run_option

    descriptor_path = click.Path(dir_okay=False, path_type=str)
    output_path = click.Path(dir_okay=False, path_type=str)
    binary_path = click.Path(dir_okay=False, executable=True, path_type=str)

    @click.group("coldsnap")
    def group():
        """Manage ColdSnap captures and restored workloads."""

    def common(command):
        command = click.argument("recipe", metavar="RECIPE", type=RECIPE_NAME)(command)
        command = click.option(
            "--cluster",
            metavar="NAME",
            type=CLUSTER_NAME,
            help="Use a saved cluster.",
        )(command)
        command = click.option(
            "--host",
            "hosts",
            metavar="HOST",
            multiple=True,
            help="Use a host directly; repeat for multiple hosts.",
        )(command)
        command = dry_run_option(command)
        command = click.option(
            "--timings/--no-timings",
            "show_timings",
            default=True,
            help="Show merged Sparkrun and ColdSnap timings.",
        )(command)
        command = click.option(
            "--coldsnap-binary",
            metavar="FILE",
            type=binary_path,
            default="",
            help="Use a local controller and its sibling tools.",
        )(command)
        command = click.option(
            "--snapshot-driver",
            type=click.Choice(["auto", "n580", "n610"]),
            default="auto",
            show_default=True,
            help="Select a driver instead of hardware auto-detection.",
        )(command)
        return command

    def weighted(command):
        command = common(command)
        command = click.option(
            "--weights",
            type=click.Choice(["auto", "native", "recovery", "cache-only-auto"]),
            help="Select the restore weight provider.",
        )(command)
        return command

    @group.command("capture", help=_recipe_help("Capture and locally verify a snapshot."))
    @click.option(
        "--output",
        metavar="PATH",
        type=output_path,
        default="",
        help="Write the descriptor to PATH.",
    )
    @weighted
    def capture(output, recipe, cluster, hosts, weights, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "capture",
            recipe,
            cluster,
            hosts,
            weights,
            dry_run,
            show_timings,
            coldsnap_binary,
            output=output,
            snapshot_driver=snapshot_driver,
        )

    @group.command("restore", help=_recipe_help("Restore and start a captured workload."))
    @click.option(
        "--artifact",
        metavar="PATH",
        type=descriptor_path,
        default="",
        help="Use an explicit artifact descriptor.",
    )
    @click.option(
        "--materialize-native",
        type=click.Choice(["off", "async", "required"]),
        help="Generate native weights during recovery.",
    )
    @weighted
    def restore(
        artifact,
        materialize_native,
        recipe,
        cluster,
        hosts,
        weights,
        dry_run,
        show_timings,
        coldsnap_binary,
        snapshot_driver,
    ):
        _run(
            "restore",
            recipe,
            cluster,
            hosts,
            weights,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            materialize_native=materialize_native or "",
            snapshot_driver=snapshot_driver,
        )

    @group.command("warm", help=_recipe_help("Warm an n610 workload without hydrating weights or KV."))
    @click.option(
        "--artifact",
        metavar="PATH",
        type=descriptor_path,
        default="",
        help="Use an explicit artifact descriptor.",
    )
    @weighted
    def warm(artifact, recipe, cluster, hosts, weights, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "warm",
            recipe,
            cluster,
            hosts,
            weights,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            snapshot_driver=snapshot_driver,
        )

    def lifecycle(command):
        command = click.option(
            "--artifact",
            metavar="PATH",
            type=descriptor_path,
            default="",
            help="Use an explicit artifact descriptor.",
        )(command)
        return common(command)

    @group.command("sleep", help=_recipe_help("Release GPU memory from a live workload."))
    @lifecycle
    def sleep(artifact, recipe, cluster, hosts, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "sleep",
            recipe,
            cluster,
            hosts,
            None,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            snapshot_driver=snapshot_driver,
        )

    @group.command("wake", help=_recipe_help("Hydrate and resume a sleeping or warm workload."))
    @lifecycle
    def wake(artifact, recipe, cluster, hosts, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "wake",
            recipe,
            cluster,
            hosts,
            None,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            snapshot_driver=snapshot_driver,
        )

    @group.command("status", help=_recipe_help("Show lifecycle state for a restored workload."))
    @lifecycle
    def status(artifact, recipe, cluster, hosts, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "status",
            recipe,
            cluster,
            hosts,
            None,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            snapshot_driver=snapshot_driver,
        )

    @group.command("native-status", help=_recipe_help("Show per-worker native-weight state."))
    @lifecycle
    def native_status(artifact, recipe, cluster, hosts, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "native-status",
            recipe,
            cluster,
            hosts,
            None,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            snapshot_driver=snapshot_driver,
        )

    @group.command("materialize", help=_recipe_help("Prepare and verify local vLLM restore assets."))
    @click.option(
        "--artifact",
        metavar="PATH",
        type=descriptor_path,
        default="",
        help="Use an explicit artifact descriptor.",
    )
    @click.option(
        "--native-weights",
        type=click.Choice(["auto", "required", "off"]),
        default="auto",
        show_default=True,
        help="Generate and verify node-local native weights.",
    )
    @click.option(
        "--residual-overlay",
        type=click.Choice(["auto", "required", "off"]),
        default="auto",
        show_default=True,
        help="Capture and verify an n580 target-local overlay.",
    )
    @common
    def materialize(
        artifact,
        native_weights,
        residual_overlay,
        recipe,
        cluster,
        hosts,
        dry_run,
        show_timings,
        coldsnap_binary,
        snapshot_driver,
    ):
        _materialize(
            recipe,
            cluster,
            hosts,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            native_weights=native_weights,
            residual_overlay=residual_overlay,
            snapshot_driver=snapshot_driver,
        )

    @group.command("publish", help=_recipe_help("Publish capsules and the portable descriptor."))
    @click.option(
        "--artifact",
        metavar="PATH",
        type=descriptor_path,
        default="",
        help="Use an explicit artifact descriptor.",
    )
    @common
    def publish(artifact, recipe, cluster, hosts, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "publish",
            recipe,
            cluster,
            hosts,
            None,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            snapshot_driver=snapshot_driver,
        )

    @group.command("publish-native", help=_recipe_help("Publish native weights to Hugging Face."))
    @click.option(
        "--artifact",
        metavar="PATH",
        type=descriptor_path,
        default="",
        help="Use an explicit artifact descriptor.",
    )
    @click.option("--hf-repo", metavar="REPOSITORY", required=True, help="Upload native weights to this repository.")
    @click.option(
        "--revision",
        metavar="REVISION",
        default="main",
        show_default=True,
        help="Upload to this revision and record its commit.",
    )
    @common
    def publish_native(artifact, hf_repo, revision, recipe, cluster, hosts, dry_run, show_timings, coldsnap_binary, snapshot_driver):
        _run(
            "publish-native",
            recipe,
            cluster,
            hosts,
            None,
            dry_run,
            show_timings,
            coldsnap_binary,
            artifact=artifact,
            native_repository=hf_repo,
            native_revision=revision,
            snapshot_driver=snapshot_driver,
        )

    @group.command("delete", help=_recipe_help("Preview or delete state for one recipe fingerprint."))
    @click.option(
        "--scope",
        type=click.Choice(["local", "published", "both"]),
        default="local",
        show_default=True,
        help="Delete local state, published objects, or both.",
    )
    @click.option(
        "--driver",
        type=click.Choice(["all", "n580", "n610"]),
        default="all",
        show_default=True,
        help="Limit deletion to one snapshot driver.",
    )
    @click.option(
        "--native-revision",
        default="",
        metavar="REVISION",
        help="Update this revision when deleting published weights.",
    )
    @click.option("--yes", is_flag=True, help="Apply the plan without confirmation.")
    @click.argument("recipe", metavar="RECIPE", type=RECIPE_NAME)
    @click.option("--cluster", metavar="NAME", type=CLUSTER_NAME, help="Use a saved cluster.")
    @click.option(
        "--host",
        "hosts",
        metavar="HOST",
        multiple=True,
        help="Use a host directly; repeat for multiple hosts.",
    )
    @dry_run_option
    def delete_artifacts(scope, driver, native_revision, yes, recipe, cluster, hosts, dry_run):
        _delete(
            recipe,
            cluster,
            hosts,
            scope=scope,
            driver=driver,
            native_revision=native_revision,
            yes=yes,
            dry_run=dry_run,
        )

    return group


def _resolve_materialization_policy(snapshot_driver, engine, native_weights, residual_overlay):
    if snapshot_driver not in {"n580", "n610"}:
        raise ValueError("materialization requires snapshot driver n580 or n610")
    if engine != "vllm":
        raise ValueError("local ColdSnap materialization currently requires vLLM")
    if native_weights == "auto":
        native_weights = "required" if snapshot_driver == "n610" else "off"
    if residual_overlay == "auto":
        residual_overlay = "required" if snapshot_driver == "n580" else "off"
    if residual_overlay == "required" and snapshot_driver != "n580":
        raise ValueError("target-local residual overlays require snapshot driver n580")
    if native_weights == "off" and residual_overlay == "off":
        raise ValueError("materialization resolved no local assets")
    return native_weights, residual_overlay


def _materialize(
    recipe,
    cluster,
    hosts,
    dry_run,
    show_timings,
    binary,
    *,
    artifact,
    native_weights,
    residual_overlay,
    snapshot_driver,
):
    from dataclasses import replace

    import click

    import sparkrun.api as api
    from sparkrun.api._context import default_sctx
    from sparkrun.core.timing import STATUS_ERROR, Timeline
    from sparkrun.plugins.coldsnap.compatibility import verify_coldsnap_hosts

    sctx = default_sctx()
    if not dry_run and getattr(sctx, "timing", None) is None:
        sctx.timing = Timeline()
    operation_span = None if dry_run else sctx.timing.begin("coldsnap.materialize", operation="materialize")

    def finish_timing(status="ok"):
        nonlocal operation_span
        if operation_span is None or getattr(sctx, "timing", None) is None:
            return
        sctx.timing.end(operation_span, status=status)
        operation_span = None
        if show_timings:
            from sparkrun.utils.cli_formatters import format_launch_timings

            rendered = format_launch_timings(sctx.timing.export(), title="ColdSnap timings")
            if rendered:
                click.echo()
                click.echo(rendered)
                click.echo()

    base_strategy = {
        key: value
        for key, value in {
            "artifact": artifact,
            "binary": binary,
            "snapshot_driver": snapshot_driver if snapshot_driver != "auto" else "",
        }.items()
        if value
    }
    options = api.RunOptions(
        recipe=recipe,
        cluster=cluster,
        hosts=tuple(hosts) or None,
        dry_run=dry_run,
        follow=False,
        strategy_options=base_strategy,
    )
    try:
        plan = api.plan(options, sctx=sctx)
        engine = str(plan.runtime.get_family())
        if dry_run:
            selected_driver = snapshot_driver if snapshot_driver != "auto" else "n610"
            hardware = None
        else:
            hardware = verify_coldsnap_hosts(
                plan,
                sctx,
                snapshot_driver=None if snapshot_driver == "auto" else snapshot_driver,
            )
            selected_driver = hardware.snapshot_driver
        resolved_native, resolved_residual = _resolve_materialization_policy(
            selected_driver,
            engine,
            native_weights,
            residual_overlay,
        )
        resolved = {
            "format": 1,
            "kind": "sparkrun-coldsnap-materialization-plan",
            "snapshot_driver": selected_driver,
            "engine": engine,
            "native_weights": resolved_native,
            "residual_overlay": resolved_residual,
            "hardware_verified": not dry_run,
        }
        if dry_run:
            click.echo(json.dumps(resolved, indent=2, sort_keys=True))
            return

        service = ColdSnapService(binary)
        if resolved_native == "required":
            native_strategy = {
                **base_strategy,
                "weights": "auto",
                "materialize_native": "required",
                "snapshot_driver": selected_driver,
            }
            native_options = replace(options, strategy_options=native_strategy)
            native_plan = api.plan(native_options, sctx=sctx)
            native_result = api.run(native_options, plan=native_plan, sctx=sctx)
            if native_result.rc:
                raise RuntimeError("ColdSnap native-weight materialization failed with exit code %d" % native_result.rc)

        overlay = None
        if resolved_residual == "required":
            # A prior restore may replace source images with local capsule IDs.
            # Always rematerialize the recipe plan before target-local capture.
            overlay_plan = api.plan(options, sctx=sctx)
            overlay, _record, _created = service.materialize_local_overlay(
                options,
                plan=overlay_plan,
                sctx=sctx,
                artifact=artifact,
                snapshot_driver=selected_driver,
                hardware=hardware,
            )
            verify_strategy = {
                **base_strategy,
                "weights": "native" if resolved_native == "required" else "recovery",
                "materialize_native": "off",
                "snapshot_driver": selected_driver,
            }
            if overlay is not None:
                verify_strategy["artifact"] = str(overlay)
            verify_options = replace(options, strategy_options=verify_strategy)
            verify_plan = api.plan(verify_options, sctx=sctx)
            verify_result = api.run(verify_options, plan=verify_plan, sctx=sctx)
            if verify_result.rc:
                raise RuntimeError("ColdSnap materialized-asset verification failed with exit code %d" % verify_result.rc)

        ready = []
        if resolved_native == "required":
            ready.append("native weights")
        if resolved_residual == "required":
            ready.append("target-local residuals")
        click.echo("ColdSnap materialization ready: %s." % " and ".join(ready))
    except Exception as error:
        finish_timing(STATUS_ERROR)
        if isinstance(error, click.ClickException):
            raise
        raise click.ClickException(str(error)) from error
    finish_timing()


def _delete(recipe, cluster, hosts, *, scope, driver, native_revision, yes, dry_run):
    import click

    import sparkrun.api as api
    from sparkrun.api._context import default_sctx
    from sparkrun.plugins.coldsnap.deletion import build_deletion_plan, execute_deletion_plan

    sctx = default_sctx()
    options = api.RunOptions(
        recipe=recipe,
        cluster=cluster,
        hosts=tuple(hosts) or None,
        dry_run=True,
        follow=False,
    )
    try:
        plan = api.plan(options, sctx=sctx)
        drivers = ("n580", "n610") if driver == "all" else (driver,)
        deletion = build_deletion_plan(
            plan=plan,
            options=options,
            sctx=sctx,
            scope=scope,
            drivers=drivers,
            native_revision=native_revision,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(deletion.to_dict(), indent=2, sort_keys=True))
    if dry_run:
        return
    if deletion.empty:
        click.echo("ColdSnap: no matching artifacts found.")
        return
    if not yes:
        click.confirm("Delete every item in this ColdSnap plan?", abort=True)
    try:
        result = execute_deletion_plan(deletion, plan=plan, sctx=sctx)
    except (OSError, RuntimeError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        "ColdSnap deleted: %d artifact store(s), %d host(s), %d capsule tag(s), "
        "%d descriptor tag(s), %d native payload(s)."
        % (
            result.artifact_stores,
            result.hosts,
            result.capsule_tags,
            result.descriptor_tags,
            result.native_payloads,
        )
    )


def _run(
    operation,
    recipe,
    cluster,
    hosts,
    weights,
    dry_run,
    show_timings,
    binary,
    *,
    artifact="",
    output="",
    native_repository="",
    native_revision="",
    materialize_native="",
    snapshot_driver="auto",
):
    import click

    import sparkrun.api as api
    from sparkrun.api._context import default_sctx
    from sparkrun.core.timing import STATUS_ERROR, Timeline

    sctx = default_sctx()
    operation_span = None
    if not dry_run:
        if getattr(sctx, "timing", None) is None:
            sctx.timing = Timeline()
        operation_span = sctx.timing.begin("coldsnap.%s" % operation, operation=operation)

    def finish_timing(status="ok"):
        nonlocal operation_span
        if operation_span is None or getattr(sctx, "timing", None) is None:
            return
        sctx.timing.end(operation_span, status=status)
        operation_span = None
        if show_timings:
            from sparkrun.utils.cli_formatters import format_launch_timings

            rendered = format_launch_timings(sctx.timing.export(), title="ColdSnap timings")
            if rendered:
                click.echo()
                click.echo(rendered)
                click.echo()

    strategy_options = {
        key: value
        for key, value in {
            "artifact": artifact,
            "weights": weights,
            "binary": binary,
            "materialize_native": materialize_native,
            "activation_state": "warm" if operation == "warm" else "",
            "snapshot_driver": snapshot_driver if snapshot_driver != "auto" else "",
        }.items()
        if value
    }
    options = api.RunOptions(
        recipe=recipe,
        cluster=cluster,
        hosts=tuple(hosts) or None,
        dry_run=dry_run or operation not in {"restore", "warm"},
        follow=False,
        strategy_options=strategy_options,
    )
    try:
        plan = api.plan(options, sctx=sctx)
    except Exception:
        finish_timing(STATUS_ERROR)
        raise
    service = ColdSnapService(binary)
    if operation in {"restore", "warm"} and not dry_run:
        try:
            result = api.run(options, plan=plan, sctx=sctx)
        except Exception as error:
            finish_timing(STATUS_ERROR)
            raise click.ClickException(str(error)) from error
        if result.rc:
            finish_timing(STATUS_ERROR)
            raise click.ClickException("ColdSnap restore failed with exit code %d" % result.rc)
        if operation == "warm":
            click.echo("ColdSnap workload is warm: CUDA and NCCL are restored; weights and KV remain unhydrated.")
        finish_timing()
        return
    # Capture obtains communication settings from its shared image-distribution
    # pass. Restore resolves them here. Publish launches no serving process and
    # needs no communication environment.
    comm_env = None
    if operation == "native-status":
        try:
            request, statuses = service.inspect_native_status(
                options,
                plan=plan,
                sctx=sctx,
                artifact=artifact,
                render_only=dry_run,
                snapshot_driver=None if snapshot_driver == "auto" else snapshot_driver,
            )
        except (OSError, RuntimeError, ValueError) as error:
            finish_timing(STATUS_ERROR)
            raise click.ClickException(str(error)) from error
        click.echo(
            json.dumps(
                {
                    "format": 1,
                    "kind": "sparkrun-coldsnap-native-status",
                    "snapshot_driver": request["snapshot_driver"]["id"],
                    "artifact": request["artifact"],
                    "workers": list(statuses),
                },
                indent=2,
                sort_keys=True,
            )
        )
        finish_timing()
        return
    if operation in {"sleep", "wake", "status"}:
        try:
            request, report = service.execute_lifecycle(
                operation,
                options,
                plan=plan,
                sctx=sctx,
                artifact=artifact,
                render_only=dry_run,
                snapshot_driver=None if snapshot_driver == "auto" else snapshot_driver,
            )
        except (OSError, RuntimeError, ValueError) as error:
            finish_timing(STATUS_ERROR)
            raise click.ClickException(str(error)) from error
        if dry_run:
            click.echo(json.dumps(request, indent=2, sort_keys=True))
            return
        click.echo(
            "ColdSnap %s: state=%s cluster=%s capture=%s (%.2fs)"
            % (
                operation,
                report.get("state"),
                report.get("cluster_id"),
                report.get("capture_id"),
                float(report.get("seconds") or 0.0),
            )
        )
        finish_timing()
        return
    try:
        request, removed, failures = service.execute_explicit(
            "restore" if operation == "warm" else operation,
            options,
            plan=plan,
            sctx=sctx,
            artifact=artifact,
            output=output,
            weight_mode=weights,
            comm_env=comm_env,
            render_only=dry_run,
            native_repository=native_repository,
            native_revision=native_revision,
            snapshot_driver=None if snapshot_driver == "auto" else snapshot_driver,
        )
    except (OSError, RuntimeError, ValueError) as error:
        finish_timing(STATUS_ERROR)
        raise click.ClickException(str(error)) from error
    if operation == "warm":
        request["lifecycle"] = {"activation_state": "warm"}
    if dry_run:
        payload = json.dumps(request, indent=2, sort_keys=True) + "\n"
        click.echo(payload, nl=False)
        return
    if failures:
        click.echo(
            "ColdSnap model payloads unavailable; using safetensors recovery: %s" % "; ".join(failures),
            err=True,
        )
    if operation == "publish" and request.get("published_artifact_reference"):
        click.echo("ColdSnap portable artifact: %s" % request["published_artifact_reference"])
    if operation == "publish-native":
        click.echo("ColdSnap native provider: %s@%s" % (request["published_native_repository"], request["published_native_revision"]))
        if request.get("published_artifact_reference"):
            click.echo("ColdSnap portable artifact refreshed: %s" % request["published_artifact_reference"])
    if operation in {"capture", "publish", "publish-native"} and not output:
        from sparkrun.plugins.coldsnap.artifacts import resolve_artifact_store

        action = "activated"
        if operation == "publish":
            action = "published and activated"
        elif operation == "publish-native":
            action = "native-published and activated"
        click.echo(
            "ColdSnap artifact %s: %s"
            % (
                action,
                resolve_artifact_store(
                    plan=plan,
                    options=options,
                    sctx=sctx,
                    snapshot_driver=request["snapshot_driver"]["id"],
                ).current,
            )
        )
        if removed:
            click.echo("ColdSnap pruned %d expired artifact generation(s)." % len(removed))
    finish_timing()


def _resolve_comm_env(plan, sctx):
    """Resolve Sparkrun-owned per-host transport settings for ColdSnap."""
    from sparkrun.orchestration.infiniband import detect_ib_for_hosts
    from sparkrun.orchestration.primitives import build_ssh_kwargs

    ssh_kwargs = build_ssh_kwargs(sctx.config)
    if plan.cluster.user:
        ssh_kwargs = {**ssh_kwargs, "ssh_user": plan.cluster.user}
    result = detect_ib_for_hosts(
        list(plan.host_list),
        ssh_kwargs=ssh_kwargs,
        dry_run=False,
        topology=plan.cluster.topology,
    )
    return result.comm_env


__all__ = ["build_command"]
