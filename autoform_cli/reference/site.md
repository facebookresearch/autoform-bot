# The published site

How a rendered site is served, deployed and checked. The [CLI
reference](../README.md#publishing-and-the-dashboard) gives the four publishing
steps.

## Lake projects in a subdirectory

Pass a Lake project in a subdirectory, such as `v1/`, as `--lean-root v1`;
links name paths from the repository's top level. Edit the `--lean-root .`
that the generated `blueprint-pages.yml` passes to `check`, `audit` and
`render` to match.

## Dashboard

`autoform dashboard . --site-dir site` then serves the built site on
`127.0.0.1` until you stop it, with a read-only live overlay of current author
claims at `/__autoform/live.json`, which only the loopback server exposes. The
overlay matches claims on an article's node ID or its `article_id`, reports the
`claim_target` each lease used, and is never written into the vault,
publication manifest, or public site. Claim badges update while it runs; re-run
render and the MkDocs build to refresh everything else.

## Pages deploys

The generated `blueprint-pages.yml` deploys only a commit that the project's own
`.github/workflows/autoform-verify.yml` (matched by file, not name) passed on a
push to `main`, with code links rendered at that commit by `--ref`. A failed
verification deploys nothing, so the last verified site stays up. Pull requests
and manual runs build the site but never deploy, and cannot cancel a waiting
deploy. To redeploy, re-run the Pages run that the newest successful verify run
on `main` started; re-running an older one deploys the older commit. Deleting or
renaming `autoform-verify.yml`, or disabling it in the repository's Actions
settings, stops every deploy, with no error. An older scaffold deploys every
push; [Upgrading](../../CHANGELOG.md#upgrading-from-050) says how to switch, and no
pin has to move: every `AUTOFORM_REF` since 0.5.0 has `render --ref`.

## Pages reports

Pages gates on `check` and `render`, and runs `audit` after `check` and
`site check` after the MkDocs build only as reports, with `continue-on-error`:
`audit` logs every finding, or `OK: roadmap audit passed`. An invalid coverage
contract fails `render` before it writes anything, but `MAPPED` rows (each an
audit `declared-coverage-gap`; a new project lists the template's row) and
coverage-role issues, which `render` prints as warnings, still publish. An older
scaffold can add either step by hand with `continue-on-error: true` and the same
`uvx --from` source: `autoform audit blueprint --lean-root .` after `check`, and
`autoform site check site --source site-src` after the MkDocs build, which needs
an `AUTOFORM_REF` from 0.6.0 or later. To block merges on findings, run
`autoform audit` in CI without `continue-on-error`.
