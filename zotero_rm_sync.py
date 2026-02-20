#!/usr/bin/env python3
"""
zotero_rm_sync.py — Sync Zotero PDFs to reMarkable with tag-based folder structure.

HOW IT WORKS:
  1. Reads your Zotero library via the API
  2. Looks for tags that start with a configurable prefix (default: "rm/")
  3. Uses the tag name (minus prefix) as a folder path on the reMarkable
  4. Slash-separated tags become nested folders: "rm/ML/Transformers" → /Zotero/ML/Transformers/
  5. Uploads the PDF via rmapi to the correct folder
  6. Tracks what's been sent to avoid duplicates
  7. Optionally downloads annotated PDFs back to a local notes folder

ZOTERO TAGGING CONVENTION:
  - Tag a paper with "rm/Neuroscience" → uploads to /Zotero/Neuroscience/
  - Tag a paper with "rm/ML/Diffusion" → uploads to /Zotero/ML/Diffusion/
  - Tag a paper with "rm/" or just "rm"  → uploads to /Zotero/ (root)
  - Multiple rm/ tags? First one wins.
  - No rm/ tag? Paper is skipped (not synced to reMarkable).

REQUIREMENTS:
  pip3 install pyzotero --break-system-packages
  brew install rmapi
  # Run `rmapi` once to authenticate with reMarkable Cloud

USAGE:
  python3 zotero_rm_sync.py                    # normal sync
  python3 zotero_rm_sync.py --dry-run          # preview without uploading
  python3 zotero_rm_sync.py --pull-notes       # also download annotated PDFs
  python3 zotero_rm_sync.py --reset            # clear sent-files log and re-sync everything
"""

import os
import sys
import json
import subprocess
import argparse
import logging
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these or set as environment variables
# ---------------------------------------------------------------------------

# Zotero credentials (get from https://www.zotero.org/settings/keys)
ZOTERO_LIBRARY_ID = os.environ.get("ZOTERO_LIBRARY_ID", "YOUR_LIBRARY_ID")
ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "YOUR_API_KEY")
ZOTERO_LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")  # "user" or "group"

# Local Zotero storage path (where PDFs live in nested 8-char folders)
ZOTERO_STORAGE = os.environ.get(
    "ZOTERO_STORAGE",
    os.path.expanduser("~/Zotero/storage")
)

# Tag prefix that marks "this tag is a reMarkable folder"
TAG_PREFIX = os.environ.get("RM_TAG_PREFIX", "rm/")

# Base folder on reMarkable
RM_BASE_FOLDER = os.environ.get("RM_BASE_FOLDER", "/Zotero")

# Local folder for downloading annotated PDFs from reMarkable
NOTES_DIR = os.environ.get(
    "RM_NOTES_DIR",
    os.path.expanduser("~/RemarkableNotes")
)

# State file to track already-uploaded items
STATE_FILE = os.environ.get(
    "RM_STATE_FILE",
    os.path.expanduser("~/.zotero_rm_sync_state.json")
)

# rmapi binary
RMAPI = os.environ.get("RMAPI", "rmapi")

# Crop PDF margins before uploading (removes whitespace borders)
# Toggle via env var or --no-crop-margins flag to disable
CROP_MARGINS = os.environ.get("RM_CROP_MARGINS", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

LOG_FILE = os.environ.get(
    "RM_SYNC_LOG",
    os.path.expanduser("~/Scripts/zotero-remarkable/sync.log")
)

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("zotero_rm_sync")

# ---------------------------------------------------------------------------
# STATE MANAGEMENT
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load the set of already-synced item keys."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            # Ensure library_version exists (migration from old state)
            data.setdefault("library_version", 0)
            return data
    return {"synced_items": {}, "created_folders": [], "library_version": 0}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# RMAPI HELPERS
# ---------------------------------------------------------------------------

def rmapi_run(cmd: str, timeout: int = 300, cwd: str | None = None) -> tuple[bool, str]:
    """Run an rmapi command, return (success, output)."""
    full_cmd = f"{RMAPI} {cmd}"
    for attempt in range(2):
        try:
            result = subprocess.run(
                full_cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                cwd=cwd,
            )
            output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
            if result.returncode == 0:
                return True, output
            # If auth expired, rmapi may print an error — retry once
            if attempt == 0 and ("auth" in output.lower() or "token" in output.lower()):
                log.warning(f"  Auth issue detected, retrying: {output}")
                continue
            return False, output
        except subprocess.TimeoutExpired:
            if attempt == 0:
                log.warning(f"  Command timed out ({timeout}s), retrying: {cmd}")
                continue
            return False, f"timeout after {timeout}s"
        except Exception as e:
            return False, str(e)
    return False, "failed after retries"


def ensure_rm_folder(folder_path: str, state: dict):
    """Create folder hierarchy on reMarkable if it doesn't exist."""
    if folder_path in state["created_folders"]:
        return

    # Build each level: /Zotero, /Zotero/ML, /Zotero/ML/Transformers
    parts = folder_path.strip("/").split("/")
    for i in range(len(parts)):
        partial = "/" + "/".join(parts[: i + 1])
        if partial not in state["created_folders"]:
            ok, out = rmapi_run(f'mkdir "{partial}"')
            # mkdir returns error if folder exists — that's fine
            state["created_folders"].append(partial)
            log.debug(f"mkdir {partial}: {'ok' if ok else out}")


def crop_pdf_margins(input_path: str, margin_top: float = 5.0,
                     margin_bottom: float = 2.0) -> str | None:
    """Crop PDF margins for reMarkable's 3:4 screen. Vertical-first strategy:

    1. Find tight content bounds via page.get_text("blocks") — fast, lightweight.
    2. Set vertical crop: content top − margin_top, content bottom + margin_bottom.
    3. Derive horizontal crop from 3:4 ratio (width = height × 0.75), centered
       on content. If content is wider, expand width to fit and grow height to match.
    4. Clamp to page bounds.

    Returns path to a temporary cropped PDF, or None on failure.
    """
    TARGET_RATIO = 3.0 / 4.0  # width / height

    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("  PyMuPDF not installed — skipping margin crop. "
                    "Install with: pip3 install pymupdf --break-system-packages")
        return None

    try:
        doc = fitz.open(input_path)

        # Scan ALL pages to find the union content bounds
        global_bx0, global_by0 = float("inf"), float("inf")
        global_bx1, global_by1 = float("-inf"), float("-inf")
        pw = ph = 0

        for page in doc:
            pw = page.rect.width
            ph = page.rect.height
            blocks = page.get_text("blocks")
            for b in blocks:
                w = b[2] - b[0]
                h = b[3] - b[1]
                if w < 5 or h < 5:
                    continue
                if h > w * 5:
                    continue  # skip rotated sidebar text
                global_bx0 = min(global_bx0, b[0])
                global_by0 = min(global_by0, b[1])
                global_bx1 = max(global_bx1, b[2])
                global_by1 = max(global_by1, b[3])

        cw = global_bx1 - global_bx0
        ch = global_by1 - global_by0
        if cw < 10 or ch < 10 or pw == 0:
            doc.close()
            return None

        cw = global_bx1 - global_bx0
        ch = global_by1 - global_by0
        if cw < 10 or ch < 10:
            doc.close()
            return None

        # Step 1: Fix vertical bounds
        cy0 = global_by0 - margin_top
        cy1 = global_by1 + margin_bottom
        crop_h = cy1 - cy0

        # Step 2: Derive width from 3:4 ratio
        crop_w = crop_h * TARGET_RATIO

        # Step 3: If content is wider, expand width and grow height to match
        min_h_margin = 5.0
        if cw + 2 * min_h_margin > crop_w:
            crop_w = cw + 2 * min_h_margin
            crop_h = crop_w / TARGET_RATIO
            content_cy = global_by0 + ch / 2.0
            cy0 = content_cy - crop_h / 2.0
            cy1 = content_cy + crop_h / 2.0

        # Step 4: Center horizontally on content
        content_cx = global_bx0 + cw / 2.0
        cx0 = content_cx - crop_w / 2.0
        cx1 = content_cx + crop_w / 2.0

        # Clamp to page, shifting to keep dimensions
        if cx0 < 0:
            cx1 -= cx0; cx0 = 0
        if cy0 < 0:
            cy1 -= cy0; cy0 = 0
        if cx1 > pw:
            cx0 -= (cx1 - pw); cx1 = pw
        if cy1 > ph:
            cy0 -= (cy1 - ph); cy1 = ph
        cx0 = max(0, cx0)
        cy0 = max(0, cy0)
        cx1 = min(pw, cx1)
        cy1 = min(ph, cy1)

        crop_rect = fitz.Rect(cx0, cy0, cx1, cy1)

        if not (cx0 > 5 or cy0 > 5 or
                (pw - cx1) > 5 or (ph - cy1) > 5):
            doc.close()
            log.debug("  No significant margins to crop")
            return None

        # Apply the same crop box to every page
        for p in doc:
            p.set_cropbox(crop_rect)

        tmp_dir = tempfile.mkdtemp(prefix="zrm_crop_")
        tmp_path = os.path.join(tmp_dir, os.path.basename(input_path))
        doc.save(tmp_path, garbage=3, deflate=True)
        doc.close()

        log.info(f"  \u2702 Cropped margins (3:4, top={margin_top}pt bot={margin_bottom}pt)")
        return tmp_path

    except Exception as e:
        log.warning(f"  Margin crop failed: {e}")
        return None




def upload_pdf(local_path: str, rm_folder: str) -> bool:
    """Upload a PDF to a specific reMarkable folder."""
    ok, out = rmapi_run(f'put "{local_path}" "{rm_folder}"')
    if ok:
        log.info(f"  ✓ Uploaded to {rm_folder}")
        return True
    if "entry already exists" in out.lower():
        log.info(f"  ✓ Already on reMarkable: {rm_folder}")
        return True
    log.error(f"  ✗ Upload failed: {out}")
    return False




# ---------------------------------------------------------------------------
# ZOTERO API
# ---------------------------------------------------------------------------

def get_zotero_items(since_version: int = 0):
    """Fetch items from Zotero library, optionally only those modified since a version.

    Returns (zot, items, new_library_version).
    """
    try:
        from pyzotero import zotero
    except ImportError:
        log.error("pyzotero not installed. Run: pip3 install pyzotero --break-system-packages")
        sys.exit(1)

    zot = zotero.Zotero(ZOTERO_LIBRARY_ID, ZOTERO_LIBRARY_TYPE, ZOTERO_API_KEY)

    # Get all top-level items (papers, not attachments or notes)
    items = []
    start = 0
    limit = 100

    if since_version > 0:
        log.info(f"Fetching items modified since library version {since_version}...")
        while True:
            batch = zot.top(start=start, limit=limit, since=since_version)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < limit:
                break
            start += limit
    else:
        log.info("Fetching all items from Zotero (initial sync)...")
        while True:
            batch = zot.top(start=start, limit=limit)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < limit:
                break
            start += limit

    # Get the library version from the last response
    new_version = int(zot.request.headers.get("Last-Modified-Version", since_version))

    log.info(f"Fetched {len(items)} items from Zotero (library version {new_version})")
    return zot, items, new_version


def get_rm_tags(item: dict) -> list[str]:
    """Extract reMarkable folder tags from an item. Returns folder paths."""
    tags = item.get("data", {}).get("tags", [])
    rm_tags = []
    prefix_lower = TAG_PREFIX.lower()
    for tag_obj in tags:
        tag = tag_obj.get("tag", "").strip()
        if tag.lower().startswith(prefix_lower):
            # Remove prefix (preserving original case of folder name), strip slashes
            folder = tag[len(TAG_PREFIX):].strip("/")
            if folder:
                rm_tags.append(folder)
            else:
                # Tag is exactly "rm/" — put in root
                rm_tags.append("")
    return rm_tags



def find_pdf_local(item_key: str) -> str | None:
    """Try to find a PDF in local Zotero storage without an API call.

    Zotero stores files as: storage/<8CHAR_KEY>/<filename>.pdf
    We scan the storage directory for the item key folder directly.
    """
    folder = os.path.join(ZOTERO_STORAGE, item_key)
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.lower().endswith(".pdf"):
                return os.path.join(folder, f)
    return None


def find_pdf_attachment(zot, item: dict) -> str | None:
    """Find the local PDF file for a Zotero item.

    First checks local storage to avoid an API call, then falls back to
    fetching children from the Zotero API.
    """
    item_key = item["data"]["key"]

    # Fast path: check if item key itself has a PDF folder in local storage
    local = find_pdf_local(item_key)
    if local:
        return local

    # Slow path: fetch children via API
    try:
        children = zot.children(item_key)
    except Exception as e:
        log.warning(f"  Could not fetch children for {item_key}: {e}")
        return None

    for child in children:
        child_data = child.get("data", {})
        if child_data.get("contentType") == "application/pdf":
            child_key = child_data["key"]
            local = find_pdf_local(child_key)
            if local:
                return local

    return None


# ---------------------------------------------------------------------------
# MAIN SYNC LOGIC
# ---------------------------------------------------------------------------

def sync(dry_run: bool = False, pull_notes: bool = False):
    state = load_state()
    since_version = state.get("library_version", 0)
    zot, items, new_version = get_zotero_items(since_version=since_version)

    # Early exit: nothing changed since last sync
    if not items and since_version > 0:
        log.info("No changes since last sync — nothing to do.")
        state["library_version"] = new_version
        save_state(state)
        if pull_notes and not dry_run:
            pull_annotated_notes(state)
        return

    new_count = 0
    skip_count = 0
    fail_count = 0

    for item in items:
        item_key = item["data"]["key"]
        title = item["data"].get("title", "Unknown")

        # Check for rm/ tags
        rm_tags = get_rm_tags(item)
        if not rm_tags:
            continue  # No rm/ tag — skip

        # First matching tag wins
        folder_suffix = rm_tags[0]

        # Build full reMarkable path
        if folder_suffix:
            rm_folder = f"{RM_BASE_FOLDER}/{folder_suffix}"
        else:
            rm_folder = RM_BASE_FOLDER

        # Already synced?
        synced_info = state["synced_items"].get(item_key)
        if synced_info:
            zotero_modified = item["data"].get("dateModified", "")
            prev_modified = synced_info.get("zotero_modified", "")
            folder_changed = synced_info.get("folder") != rm_folder

            # If we have no prev_modified (legacy state entry), backfill it and skip
            if not prev_modified:
                synced_info["zotero_modified"] = zotero_modified
                skip_count += 1
                continue

            content_changed = zotero_modified > prev_modified

            if not folder_changed and not content_changed:
                skip_count += 1
                continue
            elif folder_changed:
                log.info(f"Tag changed for '{title}': {synced_info.get('folder')} → {rm_folder}")
            elif content_changed:
                log.info(f"Updated in Zotero: '{title}' ({prev_modified} → {zotero_modified})")

        # Find the PDF
        pdf_path = find_pdf_attachment(zot, item)
        if not pdf_path:
            log.debug(f"  No PDF found for: {title}")
            continue

        log.info(f"Syncing: {title}")
        log.info(f"  PDF: {os.path.basename(pdf_path)}")
        log.info(f"  → {rm_folder}")
        if dry_run:
            log.info("  [DRY RUN] Would upload")
            new_count += 1
            continue

        # Create folder hierarchy
        ensure_rm_folder(rm_folder, state)

        # Optionally crop whitespace margins
        cropped_path = None
        upload_path = pdf_path
        if CROP_MARGINS:
            cropped_path = crop_pdf_margins(pdf_path)
            if cropped_path:
                upload_path = cropped_path

        # Upload
        if upload_pdf(upload_path, rm_folder):
            state["synced_items"][item_key] = {
                "title": title,
                "folder": rm_folder,
                "filename": os.path.basename(pdf_path),
                "synced_at": datetime.now().isoformat(),
                "zotero_modified": item["data"].get("dateModified", ""),
            }
            new_count += 1
        else:
            fail_count += 1

        # Clean up temp cropped file and its directory
        if cropped_path and os.path.exists(cropped_path):
            tmp_dir = os.path.dirname(cropped_path)
            os.remove(cropped_path)
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    state["library_version"] = new_version
    save_state(state)

    log.info(f"--- Sync complete ---")
    log.info(f"  New uploads: {new_count}")
    log.info(f"  Skipped (already synced): {skip_count}")
    log.info(f"  Failed: {fail_count}")

    # Optionally pull annotated PDFs back
    if pull_notes and not dry_run:
        pull_annotated_notes(state)


def pull_annotated_notes(state: dict):
    """Download annotated PDFs from reMarkable back to a local folder."""
    os.makedirs(NOTES_DIR, exist_ok=True)
    log.info(f"Pulling annotated PDFs to {NOTES_DIR}...")

    ok, listing = rmapi_run(f'find "{RM_BASE_FOLDER}"')
    if not ok:
        log.error(f"Could not list reMarkable folder: {listing}")
        return

    count = 0
    for line in listing.split("\n"):
        line = line.strip()
        if not line or "[d]" in line:
            continue
        if "[f]" not in line:
            continue

        # Extract file path (format: [f]    Zotero/ML/paper_name)
        rm_path = line.split("]", 1)[-1].strip()
        name = os.path.basename(rm_path)

        # Download annotated PDF via geta inside a temp directory
        # (rmapi writes files to cwd, so we isolate each download)
        tmp_dir = tempfile.mkdtemp(prefix="zrm_geta_")
        try:
            ok, out = rmapi_run(f'geta "{rm_path}"', cwd=tmp_dir)
            if not ok:
                log.warning(f"  Could not download: {name}: {out}")
                continue

            # geta produces <name>-annotations.pdf in the temp directory
            ann_file = os.path.join(tmp_dir, f"{name}-annotations.pdf")
            plain_file = os.path.join(tmp_dir, f"{name}.pdf")

            src = None
            if os.path.exists(ann_file):
                src = ann_file
            elif os.path.exists(plain_file):
                src = plain_file

            if src:
                dest = os.path.join(NOTES_DIR, os.path.basename(src))
                try:
                    shutil.move(src, dest)
                    count += 1
                    log.debug(f"  ✓ {os.path.basename(src)}")
                except Exception as e:
                    log.warning(f"  Could not move {src}: {e}")
            else:
                log.debug(f"  No PDF produced for: {name}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    log.info(f"  Downloaded {count} annotated PDFs")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global TAG_PREFIX, RM_BASE_FOLDER, CROP_MARGINS

    parser = argparse.ArgumentParser(
        description="Sync Zotero PDFs to reMarkable using tag-based folder structure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
TAGGING CONVENTION:
  In Zotero, add tags with the prefix "rm/" to papers you want on your reMarkable:

    rm/Neuroscience          → /Zotero/Neuroscience/
    rm/ML/Transformers       → /Zotero/ML/Transformers/
    rm/Methods/Statistics    → /Zotero/Methods/Statistics/

  Papers without any rm/ tag are ignored (not synced).
  If a paper has multiple rm/ tags, the first one is used.

  ALL OTHER TAGS are synced as reMarkable document tags:
    "deep-learning", "2024", "attention" → appear as tags on reMarkable

EXAMPLES:
  python3 zotero_rm_sync.py                # normal sync
  python3 zotero_rm_sync.py --dry-run      # preview what would be uploaded
  python3 zotero_rm_sync.py --pull-notes   # also download annotated PDFs back
  python3 zotero_rm_sync.py --reset        # clear state and re-sync everything
        """,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview sync without uploading"
    )
    parser.add_argument(
        "--pull-notes",
        action="store_true",
        help=f"Download annotated PDFs from reMarkable to {NOTES_DIR}",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear sync state and re-upload everything",
    )
    parser.add_argument(
        "--tag-prefix",
        default=None,
        help=f'Override tag prefix (default: "{TAG_PREFIX}")',
    )
    parser.add_argument(
        "--rm-folder",
        default=None,
        help=f'Override reMarkable base folder (default: "{RM_BASE_FOLDER}")',
    )
    parser.add_argument(
        "--crop-margins",
        action="store_true",
        default=None,
        help="Crop whitespace borders from PDFs before uploading (requires pymupdf)",
    )
    parser.add_argument(
        "--no-crop-margins",
        action="store_true",
        default=False,
        help="Disable margin cropping even if RM_CROP_MARGINS env var is set",
    )

    args = parser.parse_args()

    if args.tag_prefix:
        TAG_PREFIX = args.tag_prefix
    if args.rm_folder:
        RM_BASE_FOLDER = args.rm_folder
    if args.no_crop_margins:
        CROP_MARGINS = False
    elif args.crop_margins:
        CROP_MARGINS = True

    if args.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            log.info("State reset. Will re-sync all items.")

    # Validate config
    if ZOTERO_LIBRARY_ID == "YOUR_LIBRARY_ID":
        log.error("Please set ZOTERO_LIBRARY_ID (edit the script or set env var)")
        sys.exit(1)
    if ZOTERO_API_KEY == "YOUR_API_KEY":
        log.error("Please set ZOTERO_API_KEY (edit the script or set env var)")
        sys.exit(1)

    sync(dry_run=args.dry_run, pull_notes=args.pull_notes)


if __name__ == "__main__":
    main()