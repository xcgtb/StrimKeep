# StrimKeep repository rules

## Versioning (mandatory for AI/Codex and maintainers)

- **Release version:** obtain the highest semantic `vX.Y.Z` Git tag reachable from the commit being inspected. Use `git fetch --tags` and `git tag --merged HEAD --sort=-version:refname`. An annotated tag and a lightweight tag both count. Do not rely on `app/version.py` or the top line of `CHANGELOG.md` to infer a release.
- **Source revision:** identify the actual inspected branch/ref and `git rev-parse HEAD`. The moving `main`/`latest` build can contain commits newer than the most recent release tag while still displaying that release version.
- **Production image:** `.github/workflows/docker.yml` resolves `APP_VERSION` from Git and passes it to the Dockerfile. Runtime `/api/runtime/status` returns that version. **The Web UI displays only `X.Y.Z`, never SHA, branch, or date.**
- **Local fallback:** `app/version.py` defines `0.0.0+local` only for non-CI local source/image runs with no `APP_VERSION`. It is **not** a release version. Do not auto-bump it on release.
- **Release procedure:** merge approved changes to `main`, document them in `CHANGELOG.md` under `未发布`, then move those entries into a new `X.Y.Z` section before tagging `vX.Y.Z`. Publish using a new, immutable tag. Never retarget existing tags or rewrite historical Git commits.
- **Changelog:** keep `## 未发布` first, then released headings newest first. Items after the latest tag belong under `未发布`; don't append unreleased changes to the latest release section.
- **Analysis/reporting:** always distinguish (a) latest release tag, (b) inspected Git SHA, (c) image build/tag, and (d) running container when verified; if the container was not checked, do not claim its exact revision.

See `dev/PUBLISHING.md` for release and NAS deployment steps. Do not change media/governance logic when doing version metadata maintenance.
