# Image Tagger

A PySide6 desktop application for editing comma-separated image tag sidecars.

## Features

- Opens one folder at a time, scans its top-level images, and can close it to
  return the application to its empty state.
- Accepts a folder dragged onto the empty main window.
- Resolves `{image_stem}.txt` first, then `{image_full}.txt`, and creates a
  missing stem sidecar automatically.
- Displays an image list, scalable preview, and an inline tag editor.
- Searches tags from the toolbar, including `*` wildcard matching, and cycles
  through matching images with Enter.
- Provides staged folder traversal workflows for adding, deleting, toggling,
  and normalizing tags. Add traversals skip images that already have every
  requested tag; Delete traversals skip images missing any requested tag.
- Traversal windows can apply every available option to every candidate image
  at once after confirmation, without reviewing images individually.
- Add/Delete traversal windows accept temporary extra tags for the current
  image and clear that input whenever the displayed image changes.
- Uses UTF-8 sidecars and atomic per-file writes.

Images with duplicate stems or sidecar-path collisions are excluded and
reported. Unreadable tag files remain visible as read-only entries.

## Run

Python 3.13 or newer and [uv](https://docs.astral.sh/uv/) are required.

```powershell
uv sync
uv run python main.py
```

## Tests

```powershell
uv run pytest
```

Qt tests run with the offscreen platform plugin and do not require a visible
desktop session.
