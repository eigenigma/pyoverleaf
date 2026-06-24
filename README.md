# PyOverleaf

> **Note:** This is a personal fork of [jkulhanek/pyoverleaf](https://github.com/jkulhanek/pyoverleaf) maintained for the author's own workflow. It adds collaboration-safe OT-based writes (`patch` / `replace`), snapshot, dry-run, figure upload, comments, and tracked-changes commands, and flips a few defaults (notably `--track` on by default). It is **not** intended for upstreaming. If you want the original, lean Overleaf API, use the upstream package.

Unofficial Python API to access Overleaf.

## Tasks
- [x] List projects
- [x] Download project as zip
- [x] List and download individual files/docs
- [x] Upload new files/docs
- [x] Delete files, create folders
- [x] Python CLI interface to access project files
- [ ] Move, rename files
- [ ] Create, delete, archive, and rename projects
- [ ] Access/update comments, perform live changes
- [ ] Access/update profile details
- [ ] Robust login

## Getting started
Install the project by running the following:
```bash
pip install 'pyoverleaf'
```

Before using the API, make sure you are logged into Overleaf in your default web browser.
Currently, only Google Chrome and Mozilla Firefox are supported: https://github.com/borisbabic/browser_cookie3
Test if everything is working by listing the projects:
```bash
pyoverleaf ls
```


## Python API
The low-level Python API provides a way to access Overleaf projects from Python.
The main entrypoint is the class `pyoverleaf.Api`

### Accessing projects
```python
import pyoverleaf

api = pyoverleaf.Api()
api.login_from_browser()

# Lists the projects
projects = api.get_projects()

# Download the project as a zip
project_id = projects[0].id
api.download_project(project_id, "project.zip")
```

### Managing project files
```python
import pyoverleaf

api = pyoverleaf.Api()
api.login_from_browser()
# Choose a project
project_id = projects[0].id

# Get project files
root_folder = api.project_get_files(project_id)

# Create new folder
new_folder = api.project_create_folder(project_id, root_folder.id, "new-folder")

# Upload new file to the newly created folder
file_bytes = open("test-image.jpg", "rb").read()
new_file = api.project_upload_file(project_id, new_folder.id, "file-name.jpg", file_bytes)

# Delete newly added folder containing the file
api.project_delete_entity(project_id, new_folder)
```

## Higher-level Python IO API
The higher-level Python IO API allows users to access the project files in a Pythonic way.
The main entrypoint is the class `pyoverleaf.ProjectIO`

Here are some examples on how to use the API:
```python
import pyoverleaf

api = pyoverleaf.Api()
api.login_from_browser()
# Choose a project
project_id = projects[0].id

# Get project IO API
io = pyoverleaf.ProjectIO(api, project_id)

# Check if a path exists
exists = io.exists("path/to/a/file/or/folder")

# Create a directory
io.mkdir("path/to/new/directory", parents=True, exist_ok=True)

# Listing a directory
for entity in io.listdir("path/to/a/directory"):
    print(entity.name)

# Reading a file
with io.open("path/to/a/file", "r") as f:
    print(f.read())

# Creating a new file
with io.open("path/to/a/new/file", "w+") as f:
    f.write("new content")
```


## Using the CLI
The CLI provides a way to access Overleaf from the shell.
To get started, run `pyoverleaf --help` to list available commands and their arguments.
If you want to access your own Overleaf instance, you may set an environment variable `PYOVERLEAF_HOST` 
or specify it in each call appending `--host HOST`.  

### Listing projects and files
```bash
# Listing projects
pyoverleaf ls

# Listing projects of your own instance
pyoverleaf ls --host overleaf.my-host.com

# Listing project files
pyoverleaf ls project-name

# Listing project files in a folder
pyoverleaf ls project-name/path/to/files
```

### Downloading existing projects
```bash
pyoverleaf download-project project-name output.zip
```

### Creating and deleting directories
```bash
# Creating a new directory (including parents)
pyoverleaf mkdir -p project-name/path/to/new/directory

# Deleting
pyoverleaf rm project-name/path/to/new/directory
```

### Reading and writing files
```bash
# Writing to a file (whole-file upload; replaces all content)
echo "new content" | pyoverleaf write project-name/path/to/file.txt

# Uploading an image
cat image.jpg | pyoverleaf write project-name/path/to/image.jpg

# Reading a file
pyoverleaf read project-name/path/to/file.txt
```

### Patching docs without clobbering collaborators (`patch`)

`pyoverleaf write` performs a whole-file upload. If a collaborator has
unsaved keystrokes in the same doc, those keystrokes will be **lost**.
For docs being edited by live collaborators, use `pyoverleaf patch`
instead, which submits the change through Overleaf's `applyOtUpdate`
socket channel so the server merges your edit with concurrent edits
via Operational Transformation (OT).

```bash
# Patch an existing doc (collab-safe; preserves concurrent edits)
cat new-main.tex | pyoverleaf patch project-name/main.tex

# Submit as a tracked change (visible in Overleaf's Review panel)
cat new-main.tex | pyoverleaf patch -t project-name/main.tex
```

The command reads stdin as UTF-8 text, diffs against the current
server-side document, sends the resulting ops, and waits for the
server's sender-shape `otUpdateApplied` echo before returning. The
post-edit version is reported to stderr as `v<old> -> v<new>`. If the
server applies our update but the resulting document is unchanged
(e.g. the op was nullified by a concurrent collaborator update), the
command exits non-zero with a `silent no-op` message.

`patch` only works on text docs; binary file uploads still go through
`pyoverleaf write`. Note also that the project's per-user
`track_changes_on_for_me` setting may force tracked-changes mode even
without `-t`.

### Surgical find-and-replace (`replace`)

For targeted single-string edits (typo fixes, renames), `patch` is
overkill: it forces the caller to assemble the whole new file body.
`pyoverleaf replace` runs a literal find-and-replace internally and
submits the result through the same OT path, so collab-safety is
identical.

```bash
# Replace a single occurrence (the safe default — rejects multi-match)
pyoverleaf replace project-name/main.tex -f "teh" -r "the"

# Replace the first 3 occurrences explicitly
pyoverleaf replace -n 3 project-name/main.tex -f "TODO" -r "DONE"

# Replace every occurrence (must opt in)
pyoverleaf replace --all project-name/main.tex -f "old phrase" -r "new phrase"
```

By default `replace` requires the find string to occur exactly once;
multiple matches are treated as ambiguous and rejected with exit code
3 (so a typo fix can't silently rewrite five unrelated occurrences).
Pass `-n N` to take the first N matches, or `--all` to allow every
match.

Other exit codes: 0 = success, 1 = no occurrences, 2 = silent no-op
(server applied the op but the doc is unchanged).

## Collab-safe Python API: `api.write_doc` and `api.apply_ot_update`

Mirroring the CLI, the `Api` class exposes two new methods for
collaboration-safe doc edits:

```python
import pyoverleaf

api = pyoverleaf.Api()
api.login_from_browser()

# High-level: diff a string against the current server text, submit,
# verify, and return the new version.
result = api.write_doc(project_id, "main.tex", "new file contents",
                       track_changes=False,
                       raise_on_silent_noop=True)
print(result.old_version, "->", result.new_version,
      "silent_no_op=", result.silent_no_op)

# Low-level: submit a caller-built op list against a known version.
new_version = api.apply_ot_update(
    project_id,
    doc_id,
    [{"p": 0, "i": "hello "}],
    version=12,
    track_changes=False,
)
```

`write_doc` raises `pyoverleaf.SilentNoOpError` by default when the
submitted ops produced no observable change to the server-side text;
pass `raise_on_silent_noop=False` to receive the flag on the returned
`WriteResult` instead. Both methods raise `pyoverleaf.OtUpdateError`
when the server emits `otUpdateError` (e.g. payload too large).

`ProjectIO.write_doc(path, content, ...)` is a thin convenience wrapper
that resolves the project id for you.

For literal find-and-replace inside a doc, use
`api.find_and_replace(project_id, "main.tex", "old", "new",
expect_unique=True, count=None, track_changes=False)` (also exposed as
`ProjectIO.find_and_replace`). Returns a
`FindReplaceResult(replacements, old_version, new_version)` where
`old_version`/`new_version` are `None` when nothing matched (no socket
round-trip in that case).

The default `expect_unique=True` raises
`pyoverleaf.MultipleMatchesError` (`e.occurrences`, `e.find`) when the
find string occurs more than once, so a single-edit caller can't
silently rewrite many unrelated occurrences. Disambiguate by passing
`count=N` for the first N matches or `expect_unique=False` for
replace-all.
