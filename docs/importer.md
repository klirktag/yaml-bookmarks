# Importing bookmarks

> **Status: implemented** in `yaml_bookmarks/importers.py`, surfaced through the
> CLI (`import`) and the web UI (**⬆ Import CSV**).

Bookmarks can be imported from other tools' exports. The first (and currently
only) supported format is the **Raindrop.io CSV export**.

## Raindrop.io CSV

A Raindrop export is a UTF-8 CSV with a header row and these columns:

```
id,title,note,excerpt,url,folder,tags,created,cover,highlights,favorite
```

### Field mapping

| Raindrop column | Bookmark field | Notes |
|---|---|---|
| `url`      | `url`         | Required — rows without a URL are skipped. |
| `title`    | `title`       | |
| `note`     | `description` | Preferred; falls back to `excerpt` when the note is empty. |
| `excerpt`  | `description` | Used only when `note` is empty. |
| `tags`     | `tags`        | Comma-separated cell → list; each tag trimmed. |
| `folder`   | *folder / location* | See folder mapping below. |
| `created`  | `created`     | ISO-8601 (e.g. `2026-03-30T11:36:02.712Z`) → optional **unix timestamp** (seconds). Bad/empty leaves it unset; `save` then fills it with the import time. |
| `id`, `cover`, `highlights`, `favorite` | — | Not imported (no field for them in the model). |

### Folder mapping

Raindrop nests collections with `` / `` (space-slash-space). The importer:

- Splits on `` / `` into segments and joins them with our `/` separator, so
  `Acting / Voice acting` → `Acting/Voice acting`.
- Maps the special collection **`Unsorted` → the root** (empty folder).
- Treats a **literal `/`** inside a single name as part of the name, not nesting:
  `Båt/kanot` → `Båt-kanot` (the slash is sanitised to `-`).
- Sanitises each segment so it is always a valid folder: characters illegal on
  Windows/macOS/Linux (`< > : " | ? * \ /` and control chars) become `-`,
  trailing dots/spaces are trimmed, empty segments become `imported`, and
  reserved device names (`con`, `prn`, …) get a trailing `_`.

## Encryption

Imported bookmarks are encrypted **iff the store is unlocked** at import time —
the same rule as adding a new bookmark while the padlock is engaged
(`encrypt = store.is_unlocked`).

- Padlock engaged / `-p PASSWORD` given → the whole import is stored encrypted,
  joining the currently engaged vault.
- Locked → the import is stored as plaintext.
- If `allow_unencrypted` is **false** and the store is **locked**, importing is
  refused (you can neither store plaintext nor encrypt) until you unlock.

## Idempotency

Rows are written with `BookmarkStore.save` (upsert on `(folder, url)`), so
re-importing the same file updates existing bookmarks instead of creating
duplicates.

## Usage

### Web UI

Click **⬆ Import CSV** in the collections sidebar and choose the file. The file
is read in the browser and sent to:

```
POST /api/import?format=raindrop
Content-Type: text/csv
<raw CSV body>
```

The response is a summary, e.g.
`{"format":"raindrop","total":783,"added":783,"failed":0,"encrypted":true}`,
and the UI shows how many were imported (and whether encrypted).

### CLI

```bash
yaml-bookmarks import export.csv                 # plaintext (unless required)
yaml-bookmarks import export.csv -e -p PASSWORD   # import encrypted
```

`--format raindrop` is the default. `-p/--password` unlocks first; `-e/--encrypt`
forces encryption (prompting for a password if none was given).

## Implementation notes

- `parse_raindrop_csv(text) -> list[Bookmark]` does the parsing/mapping and is
  pure (no I/O), which makes it easy to unit-test.
- `import_raindrop(store, text, *, encrypt) -> summary` persists via
  `BookmarkStore.save` and returns the summary dict above.
- To add another source format, add a `parse_<tool>` + `import_<tool>` pair here
  and wire a new `--format` value / endpoint parameter.
