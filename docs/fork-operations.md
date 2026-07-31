# Maintained Fork Operations

This repository is a maintained fork of
[`nesquena/hermes-webui`](https://github.com/nesquena/hermes-webui). It carries
small deployment-specific features while continuing to receive the complete
upstream WebUI history.

This runbook explains the update boundary, failure signals, and safe recovery
path. It intentionally contains no private hostnames, credentials, state paths,
or network details. Keep deployment-specific notes outside the public
repository.

## Repository and runtime boundaries

| Component | Source | Local customizations | Update path |
|---|---|---|---|
| Hermes WebUI | This fork's `master` | Android/Chromium server-originated Web Push and installed-PWA running-session restoration | Upstream is merged into the fork, then installations fast-forward from the fork |
| Hermes Agent | [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) | None in this fork | Updated independently from its official repository |
| Runtime state | Outside both source repositories | Sessions, settings, credentials, VAPID key, workspace state | Never replace or reset as part of a source update |

The WebUI and Agent are separate Git repositories. A healthy WebUI fork does
not imply that the Agent is current, and an Agent update does not update the
WebUI.

## Normal WebUI update flow

1. `.github/workflows/sync-upstream.yml` runs every six hours and can also be
   started manually from GitHub Actions.
2. The workflow fetches `nesquena/hermes-webui:master` and its release tags,
   then merges all upstream changes into this fork's `master`.
3. If the merge succeeds, the workflow pushes the new `master` and release
   tags to this fork.
4. An installation's normal **Check for updates** / **Update Now** flow fetches
   this fork and fast-forwards to the new commit.
5. The WebUI restarts so the updated Python and browser assets are loaded.

Entirely new upstream files and features are included. A conflict is possible
only when Git cannot reconcile overlapping edits or upstream removes/refactors
an area used by the fork. The workflow fails before pushing when that happens;
it does not reset or overwrite the fork.

## Expected Git configuration

On a deployed WebUI checkout:

```bash
git remote -v
git status --short --branch
git rev-list --left-right --count HEAD...origin/master
```

Expected state:

- `origin` points to the maintained fork.
- `upstream` points to `https://github.com/nesquena/hermes-webui.git`.
- the active branch is `master`, tracking `origin/master`.
- the divergence count is `0 0` immediately after an update.
- the checkout is clean.

Do not use **Force update** while diagnosing divergence. Force update resets the
checkout and can discard unpublished work. Collect `git status`, remote URLs,
and divergence counts first.

## Notifications and first response

GitHub Actions notifications should be enabled for failed workflows under
[GitHub notification settings](https://github.com/settings/notifications):
**System -> Actions -> Email** or **On GitHub**, optionally limited to failed
workflows.

When the upstream sync fails:

1. Open the failed **Sync upstream** run and read the `Merge upstream changes`
   log.
2. Confirm whether the failure is a merge conflict, GitHub permission problem,
   network failure, or workflow syntax error.
3. Do not update deployed servers until the fork's `master` is healthy.
4. Preserve the failed run URL and the conflicting paths in the incident notes.

## Resolve an upstream merge conflict

Use a disposable repair branch; do not resolve directly on a production
checkout.

```bash
git switch master
git pull --ff-only origin master
git fetch upstream master
git switch -c repair/upstream-sync-YYYYMMDD
git merge upstream/master
```

Resolve only the reported conflicts. Preserve the two fork behaviors:

- completed/error/approval/clarification Web Push after the installed PWA is
  closed;
- restoration of a saved running session when the installed PWA relaunches.

Then verify the affected behavior and neighboring PWA/session behavior:

```bash
./scripts/test.sh \
  tests/test_web_push_background_notifications.py \
  tests/test_pwa_notification_controls.py \
  tests/test_4109_notification_focus_existing_tab.py \
  tests/test_1694_root_saved_running_policy.py \
  tests/test_session_cross_tab_sync.py \
  tests/test_pwa_manifest_csp.py

node --check static/boot.js
node --check static/messages.js
node --check static/panels.js
node --check static/sw.js
```

Push the repair branch, review the diff, and merge it into the fork's `master`.
Run **Sync upstream** manually once more. Only update deployed installations
after that run passes.

## WebUI update failure on a deployment

Collect this evidence before changing files:

```bash
git status --short --branch
git remote -v
git fetch origin --tags --prune
git rev-list --left-right --count HEAD...origin/master
git log --oneline --decorate -8
```

Interpretation:

| Result | Meaning | Response |
|---|---|---|
| `0 0`, clean checkout | Source is current | Investigate service/browser state instead of Git |
| `0 N`, clean checkout | Installation is behind the fork | Use normal Update Now or `git pull --ff-only origin master` during a maintenance window |
| `N 0` | Local commits exist | Identify and publish/preserve them; do not force update |
| Both nonzero | Histories diverged | Stop and compare the commits; use a recovery branch before repair |
| Modified tracked files | Uncommitted local work exists | Save a patch or stash deliberately before updating |

After an update, verify the service, local HTTP response, and logs using the
deployment's supervisor. A `302` from `/` is normal when authentication redirects
to login; a protected API may return `401` while still proving the server is
reachable.

## Installed PWA does not show the new version

1. Confirm the server checkout and service are on the expected fork commit.
2. Use the installed PWA's reload control, then fully close and reopen it.
3. Compare the same URL in a normal browser tab.
4. Check for `401` responses on `sw.js`, the manifest, or versioned static assets.
5. Only after server-side checks pass, clear site-scoped browser data and
   reinstall the PWA if necessary.

See [the general PWA troubleshooting entry](troubleshooting.md#installed-pwa-opens-to-a-blank-screen-after-an-update)
before deleting any browser state.

## Web Push stops working

Check these layers in order:

1. The site still has notification permission on the device.
2. Notifications are enabled in WebUI Settings and a test notification works.
3. The device still has an active Push subscription.
4. The WebUI service can load its Web Push dependency.
5. The persistent VAPID private-key file still exists at the configured state
   location and has not been regenerated.
6. The focused Web Push and PWA tests above pass on the deployed revision.

Do not commit a VAPID private key, Push subscription, password, cookie, token,
or state directory to this repository.

## Running session does not restore in the installed PWA

Confirm that the app is actually running in standalone/installed mode. The fork
intentionally preserves the upstream browser behavior: a normal root browser
load may keep a saved running session sidebar-only, while an installed PWA
relaunch restores it. Run the focused session/PWA tests before changing this
policy.

## Hermes Agent updates

The Agent remains independent and should normally track
`NousResearch/hermes-agent` with a clean checkout and no local commits:

```bash
git status --short --branch
git remote -v
git rev-list --left-right --count HEAD...origin/main
```

Update the Agent through the normal Hermes updater. Record both WebUI and Agent
revisions when investigating compatibility. Updating Agent source underneath a
running WebUI can leave stale imported Python modules; restart the WebUI after an
Agent update. See
[the general Agent/WebUI restart guidance](troubleshooting.md#hermes-agent-was-updated-while-hermes-webui-was-running).

## Rollback and evidence

- Prefer a normal `git revert` in the fork over rewriting published history.
- Before any deployment repair, create a recovery branch pointing at the
  current `HEAD`.
- Never reset or delete the external WebUI/Agent state directory as part of a
  source rollback.
- Capture service status, recent logs, WebUI commit, Agent commit, fork workflow
  URL, and the exact user-visible error.
- Redact credentials, cookies, password hashes, private configuration, and
  private paths before opening a public issue.

If the failure is in unchanged upstream code, report it to the upstream WebUI
or Agent repository as appropriate. If it is specific to Web Push, installed-PWA
restore behavior, or the sync workflow, report it in this fork.
