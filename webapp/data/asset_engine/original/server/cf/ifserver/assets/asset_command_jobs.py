from __future__ import annotations

import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any

_ASSET_COMMAND_JOBS_LOCK = threading.RLock()
_ASSET_COMMAND_JOBS: dict[str, dict[str, Any]] = {}

def _is_asset_migration_source(relative_path: Path) -> bool:
    """Return True only for authored/source models eligible for asset migration.

    MCAddons is generated Blockify output.  Feeding those preview GLTFs back into
    AssetMigration.csv recursively treats generated Minecraft artifacts as new
    source assets on the next reconcile.
    """
    if not relative_path.parts:
        return False
    return relative_path.parts[0].casefold() != "mcaddons"


def _asset_command_job_progress(job_id: str, **fields: Any) -> None:
    with _ASSET_COMMAND_JOBS_LOCK:
        job = _ASSET_COMMAND_JOBS.get(job_id)
        if job is None:
            return
        job["sequence"] += 1
        job["rows"].append({"sequence": job["sequence"], **fields})


def _run_asset_push_job(job_id: str) -> None:
    from cf.ifserver.assets.asset_management import asset_progress_output, publish_official
    from cf.ifserver.assets.collaboration_sync import mark_dirty, sync_now

    try:
        # Legacy Story shell Blockify files are migration seed data, never runtime
        # authority. Reconcile them into the canonical UCI asset only at the explicit
        # publish boundary so clean collaborators receive the complete Blockify
        # representation without paying migration work on every editor launch.
        from cf.ifserver.assets.asset_terrain import ensure_all_story_terrain_assets
        _asset_command_job_progress(
            job_id,
            state="running",
            output="[ifplayer assets command] terrain-publish-preflight-start",
        )
        ensure_all_story_terrain_assets()
        _asset_command_job_progress(
            job_id,
            state="running",
            output="[ifplayer assets command] terrain-publish-preflight-complete",
        )

        _asset_command_job_progress(job_id, state="running", output="[ifplayer assets command] asset-push-start")
        with asset_progress_output(lambda **fields: _asset_command_job_progress(job_id, **fields)):
            result = publish_official(progress=lambda **fields: _asset_command_job_progress(job_id, **fields))
        upload = result.get("upload") or {}
        _asset_command_job_progress(
            job_id,
            state="running",
            output=(
                "[ifplayer assets command] asset-push-complete "
                f"destination={str(upload.get('destination') or '')!r} "
                f"files={int(upload.get('files') or 0)} "
                f"added={int(upload.get('added') or 0)} "
                f"changed={int(upload.get('changed') or 0)}"
            ),
        )

        # The Project Push command owns both halves of portable project state:
        # shared Assets first, then the complete authored WorldData tree. Saves are
        # deliberately local-only, so an explicit Push must mark WorldData dirty
        # here instead of relying on an earlier save-side collaboration marker.
        _asset_command_job_progress(
            job_id,
            state="running",
            output="[ifplayer assets command] world-data-push-start",
        )
        mark_dirty("ifstarter")
        world_data = sync_now(push=True, pull_before_push=False)
        if world_data.get("state") == "failed" or world_data.get("error"):
            raise RuntimeError(
                world_data.get("error")
                or world_data.get("message")
                or "WorldData R2 push failed"
            )
        sent = sum(
            1
            for row in (world_data.get("changes") or [])
            if row.get("direction") == "outgoing"
        )
        _asset_command_job_progress(
            job_id,
            state="running",
            output=f"[ifplayer assets command] world-data-push-complete sent={sent}",
        )
        result["world_data"] = world_data

        with _ASSET_COMMAND_JOBS_LOCK:
            job = _ASSET_COMMAND_JOBS[job_id]
            job["state"] = "complete"
            job["result"] = result
    except Exception as exc:
        import traceback
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        _asset_command_job_progress(job_id, state="failed", output=detail)
        with _ASSET_COMMAND_JOBS_LOCK:
            job = _ASSET_COMMAND_JOBS[job_id]
            job["state"] = "failed"
            job["error"] = str(exc)


def _start_asset_push_job() -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    with _ASSET_COMMAND_JOBS_LOCK:
        _ASSET_COMMAND_JOBS[job_id] = {
            "state": "running",
            "sequence": 0,
            "rows": deque(maxlen=20000),
            "result": None,
            "error": "",
        }
    threading.Thread(
        target=_run_asset_push_job,
        args=(job_id,),
        daemon=True,
        name=f"ifplayer-asset-push-{job_id[:8]}",
    ).start()
    return {"ok": True, "job_id": job_id, "state": "running"}


def _run_asset_pull_job(job_id: str) -> None:
    from cf.ifserver.assets.asset_management import asset_progress_output, pull_official
    from cf.ifserver.assets.collaboration_sync import sync_now

    try:
        # WorldData is the project-authoring authority and must not be blocked by
        # the much larger shared-asset reconciliation. Pull it first so another
        # machine can reconstruct World Browser, Graph, Map, and Test immediately
        # from the newly persisted authored data.
        _asset_command_job_progress(
            job_id,
            state="running",
            output="[ifplayer assets command] world-data-pull-start",
        )
        world_data = sync_now(push=False, force_pull=True)
        if world_data.get("state") == "failed" or world_data.get("error"):
            raise RuntimeError(
                world_data.get("error")
                or world_data.get("message")
                or "WorldData R2 pull failed"
            )
        received = sum(
            1
            for row in (world_data.get("changes") or [])
            if row.get("direction") == "incoming"
        )
        _asset_command_job_progress(
            job_id,
            state="running",
            output=f"[ifplayer assets command] world-data-pull-complete received={received}",
            world_data_received=received,
        )

        # Shared assets follow independently after WorldData is safely local. The
        # browser may reload as soon as the WorldData completion row arrives; this
        # background job remains server-owned and continues the asset reconciliation.
        _asset_command_job_progress(job_id, state="running", output="[ifplayer assets command] asset-pull-start")
        with asset_progress_output(lambda **fields: _asset_command_job_progress(job_id, **fields)):
            result = pull_official(progress=lambda **fields: _asset_command_job_progress(job_id, **fields))
        _asset_command_job_progress(
            job_id,
            state="running",
            output=(
                "[ifplayer assets command] asset-pull-complete "
                f"added={int(result.get('added') or 0)} "
                f"changed={int(result.get('changed') or 0)} "
                f"unchanged={int(result.get('unchanged') or 0)} "
                f"pending={str(result.get('pending') or '')!r}"
            ),
        )
        result["world_data"] = world_data

        with _ASSET_COMMAND_JOBS_LOCK:
            job = _ASSET_COMMAND_JOBS[job_id]
            job["state"] = "complete"
            job["result"] = result
    except Exception as exc:
        import traceback
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        _asset_command_job_progress(job_id, state="failed", output=detail)
        with _ASSET_COMMAND_JOBS_LOCK:
            job = _ASSET_COMMAND_JOBS[job_id]
            job["state"] = "failed"
            job["error"] = str(exc)


def _start_asset_pull_job() -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    with _ASSET_COMMAND_JOBS_LOCK:
        _ASSET_COMMAND_JOBS[job_id] = {
            "state": "running",
            "sequence": 0,
            "rows": deque(maxlen=20000),
            "result": None,
            "error": "",
        }
    threading.Thread(
        target=_run_asset_pull_job,
        args=(job_id,),
        daemon=True,
        name=f"ifplayer-asset-pull-{job_id[:8]}",
    ).start()
    return {"ok": True, "job_id": job_id, "state": "running"}


def _asset_command_job_status(job_id: str, cursor: int = 0) -> dict[str, Any]:
    with _ASSET_COMMAND_JOBS_LOCK:
        job = _ASSET_COMMAND_JOBS.get(str(job_id or ""))
        if job is None:
            raise KeyError(f"Unknown asset command job: {job_id}")
        rows = [dict(row) for row in job["rows"] if int(row.get("sequence") or 0) > int(cursor or 0)]
        newest = max([int(cursor or 0), *[int(row.get("sequence") or 0) for row in rows]])
        return {
            "ok": True,
            "job_id": job_id,
            "state": job["state"],
            "rows": rows,
            "cursor": newest,
            "result": job["result"],
            "error": job["error"],
        }




