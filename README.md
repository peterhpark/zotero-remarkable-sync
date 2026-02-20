# Zotero <-> reMarkable Paper Pro Sync

Automatically sync PDFs from Zotero 7 to your reMarkable Paper Pro using a **tag-based folder structure**.

## How It Works

```
Zotero                              reMarkable
┌─────────────────────┐             ┌─────────────────────┐
│ Paper: "Attention    │   tag:     │ /Zotero/             │
│  Is All You Need"    │──rm/ML/──→ │   ML/                │
│                      │ Transformers│     Transformers/    │
│ Tags:                │            │       Attention Is   │
│  • rm/ML/Transformers│            │       All You Need   │
│  • deep-learning     │──────────→ │       Tags:          │
│  • 2017              │──────────→ │        deep-learning │
│                      │  (as rM    │        2017          │
│                      │   tags)    │                      │
└─────────────────────┘             └─────────────────────┘
```

You control what gets synced and where by adding tags in Zotero:

| Zotero Tag | reMarkable Folder |
|---|---|
| `rm/Neuroscience` | `/Zotero/Neuroscience/` |
| `rm/ML/Transformers` | `/Zotero/ML/Transformers/` |
| `rm/ML/Diffusion` | `/Zotero/ML/Diffusion/` |
| `rm/Methods/Statistics` | `/Zotero/Methods/Statistics/` |
| `rm/To Read` | `/Zotero/To Read/` |
| (no `rm/` tag) | (not synced) |

- Papers **without** an `rm/` tag are ignored -- only tagged papers sync
- If a paper has **multiple** `rm/` tags, the first one is used
- Slashes create **nested folders**: `rm/A/B/C` -> `/Zotero/A/B/C/`
- The PDF keeps its **original filename** (the meaningful title+author name from Zotero)

## Requirements

- **macOS** (tested on Sonoma/Sequoia)
- **Zotero 7** with local storage (WebDAV syncs files locally first, so this works)
- **reMarkable Paper Pro** with Connect (for cloud sync)
- **Homebrew** (`/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`)

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/zotero-remarkable-sync.git
cd zotero-remarkable-sync
bash setup.sh
```

The setup script will:
1. Create a Python virtual environment with dependencies (`pyzotero`, `pymupdf`)
2. Install `rmapi` via Homebrew if missing
3. Walk you through reMarkable Cloud authentication
4. Ask for your Zotero API credentials
5. Install a launchd agent that syncs every 5 minutes (and on Zotero storage changes)
6. Run a dry-run test so you can verify everything works

Re-running `setup.sh` is safe -- it recreates the venv and reloads the launchd agent.

## Getting Your Zotero Credentials

1. Go to [https://www.zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. Your **Library ID** is the number shown as "Your userID for use in API calls"
3. Click **"Create new private key"**:
   - Give it a name like "reMarkable Sync"
   - Check **"Allow library access"** -> **"Allow read access"**
   - Save and copy the key

## Manual Usage

```bash
# Sync now
~/.local/share/zotero-remarkable/venv/bin/python ~/Scripts/zotero-remarkable/zotero_rm_sync.py

# Preview what would be uploaded (no changes)
~/.local/share/zotero-remarkable/venv/bin/python ~/Scripts/zotero-remarkable/zotero_rm_sync.py --dry-run

# Sync AND download annotated PDFs back to Mac
~/.local/share/zotero-remarkable/venv/bin/python ~/Scripts/zotero-remarkable/zotero_rm_sync.py --pull-notes

# Re-sync everything (clear the "already sent" list)
~/.local/share/zotero-remarkable/venv/bin/python ~/Scripts/zotero-remarkable/zotero_rm_sync.py --reset

# Use a different tag prefix
~/.local/share/zotero-remarkable/venv/bin/python ~/Scripts/zotero-remarkable/zotero_rm_sync.py --tag-prefix "remarkable/"
```

## Annotated PDFs

When you annotate a PDF on your reMarkable, the `--pull-notes` flag downloads the annotated version to:

```
~/RemarkableNotes/
├── Vaswani_2017_Attention.pdf     (with your highlights & notes baked in)
├── Ho_2020_DDPM.pdf
└── ...
```

> **Note:** This is a copy with annotations rendered into the PDF, not a modification of the original Zotero file. Your Zotero library stays pristine.

## Configuration

All settings can be overridden with environment variables:

| Variable | Default | Description |
|---|---|---|
| `ZOTERO_LIBRARY_ID` | (required) | Your Zotero user ID |
| `ZOTERO_API_KEY` | (required) | Your Zotero API key |
| `ZOTERO_STORAGE` | `~/Zotero/storage` | Local path to Zotero's storage folder |
| `RM_TAG_PREFIX` | `rm/` | Tag prefix that triggers sync |
| `RM_BASE_FOLDER` | `/Zotero` | Root folder on reMarkable |
| `RM_NOTES_DIR` | `~/RemarkableNotes` | Where annotated PDFs are downloaded |
| `RMAPI` | `rmapi` | Path to rmapi binary |

## Stopping / Uninstalling

```bash
# Stop automatic sync
launchctl unload ~/Library/LaunchAgents/com.user.zotero-remarkable-sync.plist

# Remove completely
rm ~/Library/LaunchAgents/com.user.zotero-remarkable-sync.plist
rm -rf ~/Scripts/zotero-remarkable
rm -rf ~/.local/share/zotero-remarkable
rm ~/.zotero_rm_sync_state.json
```

## Troubleshooting

**"rmapi: command not found"**
-> Run `brew install rmapi` or add `/opt/homebrew/bin` to your PATH

**"No PDF found for: Paper Title"**
-> The PDF hasn't been downloaded to local storage yet. Open the paper in Zotero to trigger download, or check that your WebDAV sync is working.

**Papers not appearing on reMarkable**
-> Check `~/Scripts/zotero-remarkable/sync.log` for errors. Common issue: rmapi token expired -- run `rmapi` interactively to re-authenticate.

**Sync log location**
-> `tail -f ~/Scripts/zotero-remarkable/sync.log`

## Architecture

```
┌──────────────┐     pyzotero API      ┌──────────────┐
│  Zotero 7    │ ────────────────────→  │              │
│  (tags +     │  "which items have     │  zotero_rm_  │
│   metadata)  │   rm/ tags?"           │  sync.py     │
└──────┬───────┘                        │              │
       │                                │  - reads tags│
       │ local filesystem               │  - finds PDFs│
       ▼                                │  - calls     │
┌──────────────┐                        │    rmapi     │
│ ~/Zotero/    │ ────────────────────→  │              │
│ storage/     │  "read the actual      └──────┬───────┘
│ 8CHARKEY/    │   PDF file"                   │
│ Author.pdf   │                               │ rmapi put
└──────────────┘                               ▼
                                        ┌──────────────┐
                                        │  reMarkable  │
                                        │  Cloud API   │
                                        └──────┬───────┘
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │  reMarkable  │
                                        │  Paper Pro   │
                                        │              │
                                        │ /Zotero/     │
                                        │   ML/        │
                                        │     Trans/   │
                                        │       ✎ PDF  │
                                        └──────────────┘
```
