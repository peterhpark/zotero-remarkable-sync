#!/usr/bin/env python3
"""
zotero_rm_app.py — macOS menu bar app for Zotero ↔ reMarkable sync.

USAGE:
  python3 zotero_rm_app.py

REQUIREMENTS:
  pip3 install rumps --break-system-packages
"""

import os
import sys
import json
import plistlib
import threading
import subprocess
from datetime import datetime
from pathlib import Path

# On macOS, framework Python registers as Python.app which overrides our
# .app bundle's LSUIElement and window server context.  Force accessory
# mode (no Dock icon, menu-bar-only) before rumps touches NSApplication.
try:
    import AppKit
    AppKit.NSApplication.sharedApplication()
    AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
except Exception:
    pass

try:
    import rumps
except ImportError:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "rumps", "--break-system-packages"],
        check=True,
    )
    import rumps

SCRIPT_DIR  = Path(__file__).parent
SYNC_SCRIPT = SCRIPT_DIR / "zotero_rm_sync.py"
STATE_FILE  = Path.home() / ".zotero_rm_sync_state.json"
LOG_FILE    = SCRIPT_DIR / "sync.log"
LAUNCHD_LOG = SCRIPT_DIR / "launchd.log"
NOTES_DIR   = Path.home() / "RemarkableNotes"
PYTHON      = sys.executable

LAUNCHD_LABEL = "com.user.zotero-remarkable-sync"
LAUNCHD_PLIST = Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


# ── launchd helpers ───────────────────────────────────────────────────────────

def _launchctl(args: list[str]) -> tuple[bool, str]:
    r = subprocess.run(["launchctl"] + args, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _launchd_state() -> dict:
    """Return {'loaded': bool, 'pid': int|None, 'last_exit': int|None}."""
    _, out = _launchctl(["list", LAUNCHD_LABEL])
    if "Could not find service" in out or not out:
        return {"loaded": False, "pid": None, "last_exit": None}
    info = {"loaded": True, "pid": None, "last_exit": None}
    for line in out.splitlines():
        line = line.strip().strip('"').rstrip(",")
        if '"PID"' in line:
            try:
                info["pid"] = int(line.split("=")[-1].strip().rstrip(";"))
            except ValueError:
                pass
        if '"LastExitStatus"' in line:
            try:
                info["last_exit"] = int(line.split("=")[-1].strip().rstrip(";"))
            except ValueError:
                pass
    return info


class ZoteroRMApp(rumps.App):
    def __init__(self):
        super().__init__("Z↔R", quit_button=None)
        self._busy = False

        # ── Status line (read-only) ──────────────────────────────────────────
        self._status = rumps.MenuItem("—")
        self._status.set_callback(None)

        # ── launchd submenu ──────────────────────────────────────────────────
        launchd_menu = rumps.MenuItem("Auto-Sync  (launchd)")
        self._ld_status = rumps.MenuItem("Status: checking…")
        self._ld_status.set_callback(None)
        self._ld_enable  = rumps.MenuItem("Enable",   callback=self.launchd_enable)
        self._ld_disable = rumps.MenuItem("Disable",  callback=self.launchd_disable)
        self._ld_runnow  = rumps.MenuItem("Run Now",  callback=self.launchd_run_now)
        self._ld_log     = rumps.MenuItem("View launchd Log", callback=self.open_launchd_log)
        self._ld_info    = rumps.MenuItem("How it works…",    callback=self.launchd_info)
        for item in [
            self._ld_status, None,
            self._ld_runnow,
            self._ld_enable, self._ld_disable, None,
            self._ld_log, self._ld_info,
        ]:
            launchd_menu.add(item)

        # ── Main menu ────────────────────────────────────────────────────────
        self.menu = [
            self._status,
            None,
            rumps.MenuItem("Sync Now",                      callback=self.sync_now),
            rumps.MenuItem("Dry Run  (preview, no upload)", callback=self.dry_run),
            rumps.MenuItem("Sync + Pull Annotated Notes",   callback=self.sync_pull),
            None,
            launchd_menu,
            None,
            rumps.MenuItem("Reset & Full Re-sync",          callback=self.reset_sync),
            None,
            rumps.MenuItem("Open Sync Log",                 callback=self.open_log),
            rumps.MenuItem("Open Notes Folder",             callback=self.open_notes),
            rumps.MenuItem("View Sync Stats",               callback=self.view_stats),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        self._refresh_status()
        self._refresh_launchd_status()

    # ── Status ────────────────────────────────────────────────────────────────

    def _refresh_status(self):
        try:
            if STATE_FILE.exists():
                state = json.loads(STATE_FILE.read_text())
                items = state.get("synced_items", {})
                times = [v["synced_at"] for v in items.values() if v.get("synced_at")]
                if times:
                    dt = datetime.fromisoformat(max(times))
                    self._status.title = (
                        f"Last sync: {dt.strftime('%b %d %H:%M')}  ·  {len(items)} papers"
                    )
                    return
        except Exception:
            pass
        self._status.title = "Last sync: never"

    def _refresh_launchd_status(self):
        state = _launchd_state()
        if not state["loaded"]:
            self._ld_status.title = "Status: ○ Not loaded"
            self._ld_enable.title  = "Enable"
            self._ld_disable.title = "Disable"
        elif state["pid"]:
            self._ld_status.title = f"Status: ● Running  (pid {state['pid']})"
        else:
            exit_ok = state["last_exit"] == 0
            self._ld_status.title = (
                f"Status: ◉ Loaded — every 5 min"
                + ("" if exit_ok else f"  (last exit {state['last_exit']})")
            )

    # ── Sync runner ───────────────────────────────────────────────────────────

    def _run(self, args=()):
        if self._busy:
            rumps.notification("Zotero ↔ reMarkable", "Already running", "Try again shortly.")
            return
        self._busy = True
        self.title = "⟳"

        def worker():
            try:
                env = os.environ.copy()
                if LAUNCHD_PLIST.exists():
                    with open(LAUNCHD_PLIST, "rb") as f:
                        plist_data = plistlib.load(f)
                    env.update(plist_data.get("EnvironmentVariables", {}))
                r = subprocess.run(
                    [PYTHON, str(SYNC_SCRIPT)] + list(args),
                    capture_output=True, text=True, timeout=600,
                    env=env,
                )
                out = r.stdout + r.stderr
                new = fail = skipped = 0
                for line in out.splitlines():
                    if "New uploads:"  in line: new     = int(line.rsplit(":", 1)[-1].strip())
                    if "Failed:"       in line: fail    = int(line.rsplit(":", 1)[-1].strip())
                    if "Skipped"       in line: skipped = int(line.rsplit(":", 1)[-1].strip())

                if "--dry-run" in args:
                    rumps.notification(
                        "Dry Run Complete", "Preview only — nothing uploaded",
                        f"{new} paper{'s' if new != 1 else ''} would be synced",
                    )
                elif fail:
                    rumps.notification(
                        "Sync Complete", f"⚠ {fail} upload{'s' if fail != 1 else ''} failed",
                        f"Uploaded {new}  ·  Skipped {skipped}",
                    )
                else:
                    rumps.notification(
                        "Sync Complete", "✓ Done",
                        f"Uploaded {new}  ·  Skipped {skipped}",
                    )
                self._refresh_status()

            except subprocess.TimeoutExpired:
                rumps.notification("Sync Timeout", "Took too long", "Check the sync log.")
            except Exception as e:
                rumps.notification("Sync Error", type(e).__name__, str(e))
            finally:
                self._busy  = False
                self.title  = "Z↔R"

        threading.Thread(target=worker, daemon=True).start()

    # ── Manual sync actions ───────────────────────────────────────────────────

    def sync_now(self, _):
        self._run()

    def dry_run(self, _):
        self._run(["--dry-run"])

    def sync_pull(self, _):
        self._run(["--pull-notes"])

    def reset_sync(self, _):
        resp = rumps.alert(
            title="Reset & Full Re-sync",
            message=(
                "This clears the sync history so every tagged paper is "
                "re-uploaded to your reMarkable.\n\nContinue?"
            ),
            ok="Reset & Re-sync",
            cancel="Cancel",
        )
        if resp == 1:
            self._run(["--reset"])

    def open_log(self, _):
        os.system(f'open "{LOG_FILE}"')

    def open_notes(self, _):
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        os.system(f'open "{NOTES_DIR}"')

    def view_stats(self, _):
        try:
            if not STATE_FILE.exists():
                rumps.alert("Sync Stats", "No sync history found.")
                return
            state = json.loads(STATE_FILE.read_text())
            items = state.get("synced_items", {})
            if not items:
                rumps.alert("Sync Stats", "No papers synced yet.")
                return
            folders: dict[str, int] = {}
            for v in items.values():
                folder = v.get("folder", "/Zotero")
                folders[folder] = folders.get(folder, 0) + 1
            lines = [f"Total papers synced: {len(items)}\n"]
            for folder, count in sorted(folders.items()):
                lines.append(f"  {folder}  —  {count}")
            rumps.alert("Sync Stats", "\n".join(lines))
        except Exception as e:
            rumps.alert("Sync Stats Error", str(e))

    # ── launchd actions ───────────────────────────────────────────────────────

    def launchd_enable(self, _):
        if not LAUNCHD_PLIST.exists():
            rumps.alert("Enable Failed", f"Plist not found:\n{LAUNCHD_PLIST}")
            return
        ok, out = _launchctl(["load", str(LAUNCHD_PLIST)])
        if ok or "already loaded" in out.lower():
            rumps.notification("Auto-Sync", "Enabled", "Syncing every 5 minutes via launchd.")
        else:
            rumps.alert("Enable Failed", out or "Unknown error")
        self._refresh_launchd_status()

    def launchd_disable(self, _):
        resp = rumps.alert(
            title="Disable Auto-Sync",
            message="This stops the background sync agent until you re-enable it.\n\nContinue?",
            ok="Disable",
            cancel="Cancel",
        )
        if resp != 1:
            return
        ok, out = _launchctl(["unload", str(LAUNCHD_PLIST)])
        if ok or "not loaded" in out.lower():
            rumps.notification("Auto-Sync", "Disabled", "Background sync agent stopped.")
        else:
            rumps.alert("Disable Failed", out or "Unknown error")
        self._refresh_launchd_status()

    def launchd_run_now(self, _):
        ok, out = _launchctl(["start", LAUNCHD_LABEL])
        if ok:
            rumps.notification("Auto-Sync", "Triggered", "launchd job started. Check log for results.")
        else:
            rumps.alert("Run Failed", out or "Agent may not be loaded. Try Enable first.")
        self._refresh_launchd_status()

    def open_launchd_log(self, _):
        if LAUNCHD_LOG.exists():
            os.system(f'open "{LAUNCHD_LOG}"')
        else:
            rumps.alert("No Log Yet", f"Log will appear here once the agent runs:\n{LAUNCHD_LOG}")

    def launchd_info(self, _):
        plist_path = str(LAUNCHD_PLIST).replace(str(Path.home()), "~")
        log_path   = str(LAUNCHD_LOG).replace(str(Path.home()), "~")
        rumps.alert(
            "How Auto-Sync Works",
            f"""Auto-sync is handled by macOS launchd — it runs in the background every 5 minutes, even when this app is closed.

AGENT
  Label:    {LAUNCHD_LABEL}
  Plist:    {plist_path}
  Schedule: Every 5 minutes (StartInterval = 300 s)
  Log:      {log_path}

MANAGE FROM THIS APP
  Run Now   — trigger an immediate sync
  Enable    — load the agent (launchctl load …)
  Disable   — unload the agent (launchctl unload …)

MANAGE FROM TERMINAL
  Check status:
    launchctl list {LAUNCHD_LABEL}

  Run immediately:
    launchctl start {LAUNCHD_LABEL}

  Disable (survives reboot):
    launchctl unload {plist_path}

  Re-enable:
    launchctl load {plist_path}

  Watch live log:
    tail -f {log_path}

NOTE
  "Sync Now" in this app runs the sync directly (outside launchd) and shows a notification when done. Use it when you want immediate feedback.""",
        )


if __name__ == "__main__":
    ZoteroRMApp().run()
