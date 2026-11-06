# Nav Tool

[Русская версия](README.ru.md)

Desktop app for people who keep documentation in MkDocs (docs as code). Everything usually done by hand in `mkdocs.yml`, an editor and git is gathered in one window.

## Features

- **Site menu (`nav`).** Tree of sections and pages, adding pages with checkboxes, new sections (including empty nested ones), renaming, reordering, deleting together with files. `mkdocs.yml` is edited line by line, so comments and formatting stay.
- **`docs/` files.** Tree of all files, new files and folders, moving by drag and drop with automatic fixes of menu paths and relative links.
- **Markdown editor.** Formatting toolbar, preview with the same engine and extensions as mkdocs (you see broken markup before the commit), Mermaid diagrams, images from a file or the clipboard (saved next to the page), synced scrolling, zoom.
- **Templates.** Own page and block blanks with the task number, title and date filled in. Shared with colleagues as one file.
- **`mkdocs.yml` check.** Shows the error with a line number, fixes simple errors with a button, does not save an edit that would break the file.
- **Local site.** `mkdocs serve` with one button. Installs mkdocs and plugins from the project's `requirements.txt` into its own environment, can install Node.js and likec4 for C4 diagrams.
- **Git.** Changes, revert by file, branches (pick, create, switch with stashing), commit of selected files, push, Merge Request link, pulling updates, merging another unmerged branch into yours with menu items combined on conflict. Each page shows which branch it came from.
- **Undo.** Almost any action can be rolled back with "Undo".
- **Light and dark theme.**

## How it works

- `nav_tool.py` starts a local HTTP server on `127.0.0.1` and opens the UI (`ui.html`) in an Edge or Chrome app-mode window with its own profile.
- Data goes only to your git server and to the update source. Mermaid for the preview is loaded from jsDelivr.
- mkdocs and its plugins run in a separate virtual environment `.mkdocs-env` next to the program, so the system Python stays clean.
- Updates come from this repository: when a new version is out, an "Update" button shows the change list, pulls the new version and restarts the app. Settings, templates and backups stay in the program folder and are not tracked by git.

## Tech stack

- **Python 3.10+**, standard library `http.server` for the backend.
- [PyYAML](https://pyyaml.org) and [Python-Markdown](https://python-markdown.github.io) for `mkdocs.yml` and the preview.
- **HTML/JS** single-page UI, [Mermaid](https://mermaid.js.org) for diagrams.
- **Git** CLI for all repository actions.

## Requirements

- Windows 10 or 11.
- Python 3.10 or newer (check "Add python.exe to PATH" during installation).
- Git.
- Microsoft Edge or Google Chrome (Edge is always present on Windows).
- Node.js only for likec4 C4 diagrams on the local site; the app can install it itself.

Python packages are installed by the app.

## Getting started

```
git clone https://github.com/Trashfallen/Nav_tool.git NavTool
```

Run `NavTool\run.bat` once. The app creates a "Nav Tool" shortcut on the desktop; use it from then on.

## Templates

The repository has no templates: every team has its own. Templates are shared as `navtool-templates.json`: "Templates" -> "Load templates from file" in the editor, "Save templates to file" to export your own. Template text can use `{{LES}}`, `{{TITLE}}`, `{{NAME}}`, `{{DATE}}`.

## More

Step-by-step guide to every button (in Russian): [ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md).
