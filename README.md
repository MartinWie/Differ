# differ

`differ` is a terminal UI tool to monitor multiple local Git repositories from one place.

It scans subdirectories in a base folder, shows branch and change state, and lets you inspect per-file diffs.

## Requirements

- Python 3.11+
- Git installed and available in `PATH`
- A terminal with `curses` support

## Run

```bash
python3 differ.py [base_dir]
```

- If `base_dir` is omitted, the current directory is used.

## Install

### pipx (recommended)

```bash
pipx install .
```

Then run:

```bash
differ [base_dir]
```

### Homebrew (tap)

After creating a git tag/release and updating the formula SHA:

```bash
brew tap MartinWie/differ https://github.com/MartinWie/Differ
brew install MartinWie/differ/differ
```

Formula template is in `Formula/differ.rb`.

## Build Binary

Build a standalone executable with PyInstaller:

```bash
./scripts/build-binary.sh
```

Outputs:

- `dist/differ`
- `dist/differ-<os>-<arch>`

## Key Controls

- `q`: quit
- `?`: toggle help overlay
- `a` / `d`: toggle all repos vs dirty-only
- Arrow keys: navigate repos/files/diff
- `Enter` / `Right`: open detail or focus diff
- `Left` / `Esc`: go back
- `r`: refresh statuses
- `u`: pull selected repo with `git pull --ff-only`
- `Shift+u`: update all clean repos
- `o`: open selected repo in IntelliJ (or configured editor)

## Notes

- Upstream/divergence information is shown when upstream tracking is configured.
- Editor can be customized with `REPO_CHANGES_TUI_EDITOR`.
- Version output is available via `differ --version`.
