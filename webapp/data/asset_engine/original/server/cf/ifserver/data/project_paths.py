"""Authoritative Interactive Fiction project topology.

Each first-class project is one directory: Builds/InteractiveFiction/Projects/<STORY-id>/{Source,Data,Story}.
Source is editable story source, Data is editable authored WorldData/Map state, and Story is the last successful generated runtime/graph presentation.
Generated/managed asset bytes remain exclusively under Builds/Assets. Each story's successful runnable Play closure lives under Projects/<STORY-id>/Play. Intermediates is scratch only.
"""
from __future__ import annotations
from pathlib import Path
import os
import re


def repo_root() -> Path:
    """Resolve the source checkout, never the embedded Python installation.

    In a packaged app ``__file__`` belongs to Contents/Resources/python,
    which is not the repository and must not be used as a Builds authority.
    """
    from cf.io.CinterFileIO import _cinterforgeSourceCheckoutRootPath

    checkout = _cinterforgeSourceCheckoutRootPath()
    if not checkout:
        raise RuntimeError("Interactive Fiction source checkout is unavailable")
    return Path(checkout).resolve()


def interactive_fiction_root() -> Path:
    """Return the one mutable Interactive Fiction data root for this process.

    Desktop launches receive the canonical CinterForge storage root from the
    native host.  Source/maintainer launches do not, and continue to use the
    repository Builds tree.  Project discovery must follow the same authority
    as the rest of IFPlayer; deriving Projects only from ``__file__`` makes a
    packaged Mac/Windows runtime look beside its installed Python code and
    produces an empty catalog even though the installed Projects are present.
    """
    for environment_name in ("CINTERFORGE_INSTALL_STORAGE_ROOT", "CINTERFORGE_STORAGE_ROOT"):
        environment_value = str(os.environ.get(environment_name, "") or "").strip()
        if environment_value:
            return Path(environment_value).expanduser().resolve() / "InteractiveFiction"
    # Embedded Python lives inside the app bundle, not the source checkout.
    # The native storage authority also handles Debug and installed Release
    # layouts when the IF-specific environment variables are absent.
    from cf.io.CinterFileIO import cinterforgeInstallStorageRootPath

    storage_root = cinterforgeInstallStorageRootPath()
    if not storage_root:
        raise RuntimeError("CinterForge storage root is unavailable")
    return Path(storage_root).expanduser().resolve() / "InteractiveFiction"


def story_root() -> Path:
    """One source/project authority: the workspace sibling Builds tree.

    Collaborative IF Projects are build inputs, not per-Steam-install user saves.
    Installed games are published Play closures; user progress is persisted in
    CONV cards under the shared Cards library instead.
    """
    from cf.io.CinterIO import cinterforgeBuildsRootPath

    return Path(cinterforgeBuildsRootPath()).resolve() / "InteractiveFiction" / "Projects"


def copy_installed_projects_into_builds(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    """Non-destructive one-time import from an old installed Projects tree.

    Neither copy a symlink nor overwrite an existing authored file. Existing
    identical bytes are fine; a collision with different content is an error.
    The prior tree is left intact until the developer independently verifies it.
    """
    import shutil

    if not source.is_dir() or source.is_symlink():
        return []
    moved: list[tuple[Path, Path]] = []
    # Validate the entire proposed merge before copying a single file.
    pending: list[tuple[Path, Path]] = []
    for project in sorted(source.glob("STORY-*"), key=lambda item: item.name.casefold()):
        if not project.is_dir() or project.is_symlink():
            raise RuntimeError(f"Unsafe legacy IF Project: {project}")
        for item in sorted(project.rglob("*")):
            # Finder/Explorer/Git metadata is never authored IF project data.
            if any(part in {".git", "__MACOSX", "__pycache__"} for part in item.relative_to(project).parts):
                continue
            if item.name in {".DS_Store", "Thumbs.db", "desktop.ini"} or item.name.startswith("._"):
                continue
            if item.is_symlink():
                raise RuntimeError(f"Unsafe legacy IF Project symlink: {item}")
            if not item.is_file():
                continue
            target = destination / project.name / item.relative_to(project)
            if target.exists():
                if not target.is_file() or item.read_bytes() != target.read_bytes():
                    raise RuntimeError(
                        f"Conflicting IF Project files; refusing overwrite: {item} -> {target}"
                    )
            else:
                pending.append((item, target))
    for item, target in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        moved.append((item, target))
    return moved



def legacy_story_root() -> Path:
    from cf.io.CinterFileIO import _cinterforgeSourceCheckoutRootPath

    checkout = _cinterforgeSourceCheckoutRootPath()
    if not checkout:
        return None
    return Path(checkout).resolve() / "CinterForgeServer" / "cinter" / "ifplayer" / "Story"


def migrate_legacy_projects() -> list[tuple[Path, Path]]:
    """Move mutable STORY-* projects out of the implementation tree exactly once.

    Projects/<STORY-id> is authoritative. Legacy trees may fill missing project
    files during the one-time migration, but they never overwrite or conflict
    with files already present in the Project. Legacy duplicates are discarded.
    """
    import shutil
    # The legacy update-data command has not yet been retired in this source
    # drop. Do not let it import or mutate collaborative source Projects.
    if os.environ.get("CINTERFORGE_UPDATE_DATA_PIPELINE") == "1":
        return []
    # Project migration is scoped to Builds, regardless of the currently
    # installed/native runtime storage root or a retired update-data flag.
    from cf.io.CinterIO import cinterforgeBuildsRootPath

    builds_root = Path(cinterforgeBuildsRootPath()).resolve()
    legacy = legacy_story_root()
    projects = story_root()
    projects.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    # Import the previous per-Steam-install project location without deleting
    # any source. Treat conflicts as errors instead of discarding authored data.
    from cf.io.CinterIO import cinterforgeSteamAppInstallRootPath

    installed_root = cinterforgeSteamAppInstallRootPath()
    if installed_root:
        moved.extend(copy_installed_projects_into_builds(
            Path(installed_root) / "InteractiveFiction" / "Projects", projects
        ))

    def remove_filesystem_metadata(directory: Path) -> None:
        """Discard host filesystem metadata; it is never IF project state."""
        if not directory.is_dir():
            return
        for metadata_name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            metadata = directory / metadata_name
            if metadata.is_file():
                metadata.unlink()

    for source in sorted(legacy.glob("STORY-*"), key=lambda path: path.name.casefold()) if legacy is not None and legacy.is_dir() else []:
        if not source.is_dir():
            continue
        destination = projects / source.name
        if destination.exists():
            # The Project is already authoritative. Fold in only files that are
            # missing there, then delete the obsolete implementation-tree copy.
            for item in sorted(source.rglob("*")):
                if not item.is_file():
                    continue
                relative = item.relative_to(source)
                target = destination / relative
                if target.exists():
                    item.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
                moved.append((item, target))
            shutil.rmtree(source)
        else:
            shutil.move(str(source), str(destination))
            moved.append((source, destination))

    # Older builds also left an acquired-source cache under ifplayer/Story/Sources.
    # It is not editable authority. Preserve it outside the implementation tree
    # for diagnostics instead of deleting it or allowing humans/builds to mistake
    # it for the current Project source.
    legacy_sources = legacy / "Sources" if legacy is not None else None
    if legacy_sources is not None and legacy_sources.is_dir():
        cache = builds_root / "InteractiveFiction" / "Intermediates" / "LegacySourceCache"
        if cache.exists():
            raise RuntimeError(
                f"Legacy IF source cache exists in two places: {legacy_sources} and {cache}. "
                "Reconcile/remove one before continuing."
            )
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_sources), str(cache))
        moved.append((legacy_sources, cache))
    # Authoring/WorldData was the former mutable runtime authority. Move its
    # story-owned files into Projects/<STORY>/Data so Save/Build/Map/Test/Push/Pull
    # all share one project root. Existing Project Data may contain durable files
    # such as solutions.json; only the former WorldData files are replaced.
    legacy_world_root = builds_root / "InteractiveFiction" / "Authoring" / "WorldData"
    if legacy_world_root.is_dir():
        world_names = {
            "rooms.json", "objects.json", "characters.json", "puzzles.json",
            "grammar.json", "vocabulary.json", "physics.json", "map.json",
            "map_shell.json",
        }
        for source_world in sorted(legacy_world_root.glob("STORY-*"), key=lambda path: path.name.casefold()):
            if not source_world.is_dir():
                continue
            remove_filesystem_metadata(source_world)
            destination_data = projects / source_world.name / "Data"
            destination_data.mkdir(parents=True, exist_ok=True)
            for source_file in sorted(source_world.iterdir(), key=lambda path: path.name.casefold()):
                if not source_file.is_file() or source_file.name not in world_names:
                    continue
                destination = destination_data / source_file.name
                if destination.exists():
                    # Project Data wins. The former Authoring copy is stale.
                    source_file.unlink()
                else:
                    shutil.move(str(source_file), str(destination))
                    moved.append((source_file, destination))
            remaining = list(source_world.iterdir())
            if remaining:
                raise RuntimeError(
                    f"Legacy WorldData contains unrecognized files that cannot be migrated automatically: "
                    f"{source_world}: {[path.name for path in remaining]}"
                )
            source_world.rmdir()
        remove_filesystem_metadata(legacy_world_root)
        if not any(legacy_world_root.iterdir()):
            legacy_world_root.rmdir()
            authoring_root = legacy_world_root.parent
            if authoring_root.is_dir() and not any(authoring_root.iterdir()):
                authoring_root.rmdir()
    # Older producer layouts also scattered story-owned build state directly
    # under Builds/InteractiveFiction.  Fold every STORY-* child into its Project
    # and remove the empty legacy roots so there is one place to inspect a story.
    interactive_root = builds_root / "InteractiveFiction"

    def move_story_children(legacy_root: Path, destination_name: str) -> None:
        if not legacy_root.is_dir():
            return
        for source in sorted(legacy_root.glob("STORY-*"), key=lambda path: path.name.casefold()):
            if not source.is_dir():
                continue
            destination = projects / source.name / destination_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                # Merge without silently replacing project-owned files. Identical
                # files are harmless; conflicting files require human resolution.
                for item in sorted(source.rglob("*")):
                    if not item.is_file():
                        continue
                    relative = item.relative_to(source)
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        # Project state is authoritative even when the legacy
                        # duplicate differs. Never resurrect stale build state.
                        item.unlink()
                    else:
                        shutil.move(str(item), str(target))
                        moved.append((item, target))
                for directory in sorted((path for path in source.rglob("*") if path.is_dir()), reverse=True):
                    if directory.exists() and not any(directory.iterdir()):
                        directory.rmdir()
                if source.exists() and not any(source.iterdir()):
                    source.rmdir()
            else:
                shutil.move(str(source), str(destination))
                moved.append((source, destination))
        remove_filesystem_metadata(legacy_root)
        if legacy_root.exists() and not any(legacy_root.iterdir()):
            legacy_root.rmdir()

    move_story_children(interactive_root / "Sources", "Source")
    move_story_children(interactive_root / "Graphs", "Story")
    move_story_children(interactive_root / "SolutionValidation", "Validation/Solution")
    move_story_children(interactive_root / "SolutionValidationArtifacts", "Validation/Artifacts")
    move_story_children(interactive_root / "SolutionSeedCalibration", "Validation/SeedCalibration")

    # A historical presentation bug copied the repository-wide Story tree into
    # an individual project's browser closure at Story/SVG/Story.  That subtree
    # is generated contamination, not story-owned state: it can contain runnable
    # images for every registered game and makes one Project appear to own many
    # Z-machine runtimes.  Remove it from every Project during startup migration.
    for project in sorted(projects.glob("STORY-*"), key=lambda path: path.name.casefold()):
        leaked_story_tree = project / "Story" / "SVG" / "Story"
        if leaked_story_tree.is_dir():
            shutil.rmtree(leaked_story_tree)

    # Generated IF products are disposable and belong under Intermediates. Older
    # layouts left both Packs and GeneratedWorldData at the top level/Authoring.
    # Move them out of the durable project namespace; if the new scratch copy
    # already exists, discard the stale legacy copy rather than creating a
    # second authority.
    def move_generated_tree(source: Path, destination: Path) -> None:
        if not source.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
            return
        if source.is_dir() and destination.is_dir():
            for item in sorted(source.rglob("*")):
                if not item.is_file():
                    continue
                relative = item.relative_to(source)
                target = destination / relative
                if target.exists():
                    item.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item), str(target))
                    moved.append((item, target))
            shutil.rmtree(source)
            return
        raise RuntimeError(f"Legacy generated IF path has incompatible destination: {source} -> {destination}")

    # Story-specific live runtime state now belongs inside each Project.  The
    # former global Runtime/Session and Runtime/CodeWorkspaces trees were
    # disposable launch/workspace state, so remove them rather than migrating
    # stale sessions into the new Project authority. Shared Runtime/Engine
    # resources remain at the InteractiveFiction Runtime root.
    global_runtime = interactive_root / "Runtime"
    for obsolete_name in ("Session", "CodeWorkspaces", ".cinterforge-code-workspaces"):
        obsolete = global_runtime / obsolete_name
        if obsolete.is_dir() and not obsolete.is_symlink():
            shutil.rmtree(obsolete)
        elif obsolete.exists() or obsolete.is_symlink():
            obsolete.unlink()

    # Pack was the previous name for a project's runnable consumer closure.
    # The durable project-owned name is now Play. Migrate once; an existing
    # Play tree is authoritative and the obsolete Pack duplicate is discarded.
    for project in sorted(projects.glob("STORY-*"), key=lambda path: path.name.casefold()):
        old_pack = project / "Pack"
        play = project / "Play"
        if not old_pack.exists():
            continue
        if play.exists():
            if old_pack.is_dir():
                shutil.rmtree(old_pack)
            else:
                old_pack.unlink()
        else:
            old_pack.rename(play)
            moved.append((old_pack, play))

    # Launcher thumbnails flow one way from Agatha Assistant into Projects.
    # Catalog generation performs that refresh; project migration must never
    # write Project artwork back into the authored Assistant Pack.

    intermediates = interactive_root / "Intermediates"
    # Story Packs are durable outputs owned by their Projects. Migrate any
    # previous Intermediates/Plays/STORY-* trees once; an existing Project Play
    # wins and the obsolete duplicate is discarded.
    legacy_packs = intermediates / "Packs"
    if legacy_packs.is_dir():
        for legacy_pack in sorted(legacy_packs.glob("STORY-*"), key=lambda path: path.name.casefold()):
            if not legacy_pack.is_dir():
                continue
            project_pack = projects / legacy_pack.name / "Play"
            project_pack.parent.mkdir(parents=True, exist_ok=True)
            if project_pack.exists():
                shutil.rmtree(legacy_pack)
            else:
                legacy_pack.rename(project_pack)
        remove_filesystem_metadata(legacy_packs)
        if legacy_packs.is_dir() and not any(legacy_packs.iterdir()):
            legacy_packs.rmdir()

    # Older top-level Packs are also story-specific legacy output. Fold each
    # STORY-* Pack directly into its Project instead of recreating a Packs root.
    top_level_packs = interactive_root / "Packs"
    if top_level_packs.is_dir():
        for legacy_pack in sorted(top_level_packs.glob("STORY-*"), key=lambda path: path.name.casefold()):
            if not legacy_pack.is_dir():
                continue
            project_pack = projects / legacy_pack.name / "Play"
            project_pack.parent.mkdir(parents=True, exist_ok=True)
            if project_pack.exists():
                shutil.rmtree(legacy_pack)
            else:
                legacy_pack.rename(project_pack)
        remove_filesystem_metadata(top_level_packs)
        if top_level_packs.is_dir() and not any(top_level_packs.iterdir()):
            top_level_packs.rmdir()
    move_generated_tree(interactive_root / "Authoring" / "GeneratedWorldData", intermediates / "GeneratedWorldData")
    authoring_root = interactive_root / "Authoring"
    remove_filesystem_metadata(authoring_root)
    if authoring_root.is_dir() and not any(authoring_root.iterdir()):
        authoring_root.rmdir()

    return moved

def project_root_for_story_id(story_id: str) -> Path:
    value = str(story_id or "").strip()
    if not value.startswith("STORY-") or "/" in value or "\\" in value:
        raise ValueError(f"Invalid story id: {story_id!r}")
    return story_root() / value


def project_root(alias: str) -> Path:
    from .story_registry import BY_ALIAS
    key = str(alias or "").strip().casefold()
    spec = BY_ALIAS.get(key)
    if spec is not None:
        return project_root_for_story_id(spec.story_id)
    slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    matches = [p for p in story_root().glob("STORY-*") if p.is_dir() and re.sub(r"[^a-z0-9]+", "-", p.name[6:].casefold()).strip("-") == slug.removeprefix("story-")]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"No unique IF project for alias {alias!r}")


def data_dir(alias: str) -> Path:
    return project_root(alias) / "Data"


def source_dir(alias: str) -> Path:
    return project_root(alias) / "Source"


def authored_story_dir(alias: str) -> Path:
    return project_root(alias) / "Story"


def frobozz_project_root() -> Path:
    """Authoritative composite Frobozz Project (not a playable story alias)."""
    return project_root_for_story_id("STORY-Frobozz")


def frobozz_data_dir() -> Path:
    return frobozz_project_root() / "Data"


def validate_asset_references(repo: Path, alias: str, *, require_present: bool = False) -> dict[str, object]:
    """Validate Project asset closure without copying generated assets into source.

    Authored Data stores portable CinterForge asset identities. Cloudflare owns
    runtime bytes; the local Builds/Assets tree is an optional
    migration/cache source. Local presence can be reported but is not validity.
    """
    import json
    project = project_root(alias)
    data = project / "Data"
    from cf.io.CinterFileIO import cinterforgeBuildsRootPath
    central = cinterforgeBuildsRootPath(Path(repo)) / "Assets"
    prefixes = (
        "Builds/Assets/",
        "<builds>/Assets/",
    )
    references: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            normalized = value.replace("\\", "/")
            folded = normalized.casefold()
            for prefix in prefixes:
                if folded.startswith(prefix.casefold()):
                    references.add(normalized[len(prefix):])
                    break

    # Checked-in Data is the durable project authority. The active editable
    # WorldData tree is a disposable working mirror, but include it while a live
    # editor has unsaved changes so validation can report those references too.
    roots = [data]
    try:
        from .world_data import editable_dir
        editable = editable_dir(alias)
        if editable is not None and editable.resolve() != data.resolve():
            roots.append(editable)
    except Exception:
        pass
    seen_paths: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            walk(payload)
    missing: list[str] = []
    for relative in sorted(references):
        source = (central / relative).resolve()
        try:
            source.relative_to(central.resolve())
        except ValueError:
            raise RuntimeError(f"Project asset escapes managed library: {relative}")
        if not source.is_file():
            missing.append(relative)
    if missing and require_present:
        raise FileNotFoundError("Project asset closure is incomplete for " + alias + ": " + ", ".join(missing))
    return {"story_alias": alias, "references": sorted(references), "missing": missing}
