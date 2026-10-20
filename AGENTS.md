# StrimKeep repository rules

## Versioning (mandatory for AI/Codex and maintainers)

- **Release version:** use the nearest semantic `vX.Y.Z` release tag on the inspected commit's first-parent history. Fetch full history and tags, then run `python3 dev/tools/image_version.py`. Annotated and lightweight tags both count. Historical numeric maxima (such as `v1.12.11`) and tags on merged feature branches must not override later releases on the main lineage. Do not rely on `app/version.py` or the top line of `CHANGELOG.md` to infer a release. Publishing requires one unambiguous release tag on the exact commit; never rewrite old tags.
- **Source revision:** identify the actual inspected branch/ref and `git rev-parse HEAD`. The moving `main`/`latest` build can contain commits newer than the most recent release tag while still displaying that release version.
- **Production image:** `.github/workflows/docker.yml` resolves `APP_VERSION` from Git and passes it to the Dockerfile. Runtime `/api/runtime/status` returns that version. **The Web UI displays only `X.Y.Z`, never SHA, branch, or date.**
- **Local fallback:** `app/version.py` defines `0.0.0+local` only for non-CI local source/image runs with no `APP_VERSION`. It is **not** a release version. Do not auto-bump it on release.
- **Release procedure:** merge approved changes to `main`, document them in `CHANGELOG.md` under `未发布`, then move those entries into a new `X.Y.Z` section before tagging `vX.Y.Z`. Publish using a new, immutable tag. Never retarget existing tags or rewrite historical Git commits.
- **Changelog:** keep `## 未发布` first, then released headings newest first. Items after the latest tag belong under `未发布`; don't append unreleased changes to the latest release section.
- **Analysis/reporting:** always distinguish (a) latest release tag, (b) inspected Git SHA, (c) image build/tag, and (d) running container when verified; if the container was not checked, do not claim its exact revision.

See `dev/PUBLISHING.md` for release and NAS deployment steps. Do not change media/governance logic when doing version metadata maintenance.

## Layering (mandatory for new features)

Read `docs/ARCHITECTURE.md` and follow `docs/ADDING_A_FEATURE.md`. The five placement rules:

- **Rules** (judgement only, zero IO) go to `app/domain/`. Function signatures must not take `con`, `requests`, `Path`, or `engine`.
- **Flows** (ordering, external calls, failure handling) go to `app/cloud/` or the existing root-domain modules.
- **Routes** only authenticate, validate, and dispatch. No `con.execute`, no `requests`, no `Path` in `app/routers/`.
- **Any operation that writes remote data or local files must go through `app/cloud/jobs.py`** (`enqueue` → `step` → `checkpoint` → `progress`). It needs a durable job id and a safe stop point.
- **Business state has exactly one source: `cloud_jobs.state`.** `app/tasks.py` is an execution slot (`ExecSlot`; `Task` remains as a compatibility alias) for mutual exclusion, cancel signal, and the log stream — diagnostics only. Its `running/success/error/cancelled` words must not be used as business status in the UI.
- **Business state words live in `app/cloud/states.py`.** Add a state there first, then at the writer. `dev/tests/test_task_semantics.py` guards that the two vocabularies never mix.

Never auto-replay an `uncertain` write. Stop at a safe boundary and leave it for a human.

## Compatibility exports are frozen, not extended

`app/engine.py` + `app/compat.py` re-export 204 legacy names so old tests and CLIs keep working. This is a strangler shim, not a target design. `dev/tests/test_architecture_boundaries.py` locks the contract.

- New code must import the owning module directly. **Do not add exports to `engine`.**
- Each feature PR should leave the export count **net lower**: delete the legacy names your change no longer needs, and delete their lines in the boundary test with them.
- When the count reaches zero, delete `compat.py` and the re-export block.

## Documentation must match the code

`docs/ARCHITECTURE.md` is verified against source. If a change moves files or responsibilities, update that document in the same commit. The frontend is `frontend/src` (Vue 3 + Vite), built into `static/`; never describe or hand-edit `static/js`.
