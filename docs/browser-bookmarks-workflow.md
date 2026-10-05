# Export X bookmarks with Codex computer use

This workflow uses the Codex desktop browser or a connected signed-in Chrome profile. The desktop browser tool is not automatically available to `codex exec`. The included script commands use the official X API when unattended export/removal is needed; API credentials are separate from your Codex login.

## Browser export without X developer credentials

Ask Codex in the desktop app:

> Use my signed-in X browser. Start at https://x.com/i/history and navigate to Bookmarks if the page is a different history view. Export my bookmarked post URLs to /home/brett/repos/x_post_to_obsidian/bookmarks.txt, one URL per line. Collect the primary post URL for each bookmarked entry, rather than quoted or recommended posts. Scroll the bookmark list, deduplicate by post ID, and keep collecting across virtualized items until the UI explicitly shows the end or a conservative stopping condition is reached. Save a sidecar manifest and report whether the export is complete. Do not remove bookmarks during export.

The agent should inspect the current page, verify the signed-in account handle, and use the rendered bookmark entries as evidence. The provided route may change; do not assume a History screen is a bookmark list. If login, CAPTCHA, access denial, or a loading failure prevents listing bookmarks, report that blocker and do not represent a partial/empty capture as a complete export.

A browser export must retain collected URLs between scrolls: X can remove earlier entries from the DOM. Link extraction should be scoped to each primary post's timestamp/permalink. A quote can have its own status URL; capturing every `/status/` anchor would export unbookmarked posts. Related recommendations and conversation links also need to be excluded. Use the current DOM/accessibility state to identify entries; avoid assuming unseen selectors or calling X's private endpoints with browser cookies.

After collecting links in a scratch file, normalize them and create a manifest without making API calls:

```bash
./x2o bookmarks export --from-file captured-links.txt \
  --account-handle YOUR_SIGNED_IN_HANDLE --output bookmarks.txt
./x2o ingest --file bookmarks.txt
./x2o bookmarks cleanup --file bookmarks.txt
```

The manifest records that the source was a browser capture. It proves which URLs were captured, not that the entire bookmark collection was exhausted; the agent must separately report that status. Use `--account-id` instead of a handle when the actual X account ID is known. No account credential is stored in the export.

## Remove archived bookmarks through the UI

`cleanup` defaults to a preview and writes `bookmarks.txt.cleanup.json`. Its `eligible` list includes only records with successful-write receipts and valid notes. A dry-run research plan, a note with no receipt, an edited post note, or a missing topic contribution is insufficient. Earlier x2o imports without receipts require a refresh with `--force`.

To remove through computer use, explicitly instruct Codex to remove the `eligible` entries for the account recorded in the export. Immediately before each UI removal, verify that the archive is still eligible, verify the active account, open the exact post, inspect the current bookmark state, then click the UI's removal control. Never toggle a currently unbookmarked post back into bookmarks. Verify the changed state, record the result alongside the export, and provide a screenshot showing completion. Preserve original URLs so available posts can be bookmarked again if needed. Do not clear all bookmarks or remove failed/unimported entries.

This document is a workflow guide, not a runnable desktop-browser bridge or a grant of permission to remove bookmarks. The agent must follow the browser tool's applicable confirmation policy and the user's explicit removal instruction.

## Run export → ingestion → cleanup from a script

For a shell script, use the official X API integration in `x2o`:

```bash
# X_USER_ACCESS_TOKEN must already contain an authorized X OAuth user token.
./x2o bookmarks sync --output bookmarks.txt --remove-after-import
```

Omit `--remove-after-import` to leave all bookmarks in X. The default refuses to overwrite an existing export; choose a new filename for each run, or explicitly pass `--overwrite`.

Separately:

```bash
./x2o bookmarks export --output bookmarks.txt
./x2o ingest --file bookmarks.txt
./x2o bookmarks cleanup --file bookmarks.txt             # preview
./x2o bookmarks cleanup --file bookmarks.txt --execute   # actually remove
```

Even if ingestion has partial failures, an independently invoked cleanup removes only verified successful imports. `sync` performs that same filtering, returns a nonzero exit code on partial failures, and keeps the failed post URLs in the export.

X API access needs an approved developer app and an OAuth user token with the relevant read/write permissions (including `bookmark.read`, and `bookmark.write` for removal, plus required post/user read scopes). The API provider checks the authenticated account before changing bookmarks. Tokens are read from `X_USER_ACCESS_TOKEN`, are never extracted from a browser, and are never saved in notes, reports, or manifests. Refresh an expired token through your authorized OAuth flow; this tool does not create the X developer app or handle OAuth enrollment.

Exports have a companion `<file>.json` manifest. Cleanup writes `<file>.cleanup.json` and journals removal attempts/results in `<file>.removed.jsonl`. Preserve these files until you are satisfied that your notes contain everything you need. A changed export invalidates its manifest; create a new export before cleanup. Shared topic notes can accumulate other posts without invalidating earlier receipts; edits to the individual post note conservatively block cleanup.

The API export uses pagination and fails on partial API errors. It does not claim to export inaccessible bookmarks that X does not return. The authenticated browser/API operations remain unverified until a signed-in session or a valid user token is supplied.

References: [desktop browser availability](https://learn.chatgpt.com/docs/browser), [X bookmark endpoints](https://docs.x.com/x-api/posts/bookmarks/introduction), [X user bookmark listing](https://docs.x.com/x-api/users/get-bookmarks), [X bookmark removal](https://docs.x.com/x-api/users/delete-bookmark).
