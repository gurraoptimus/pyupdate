"""
Professional auto-update system for a desktop application.

Displays the currently installed (build-time) version, checks GitHub
Releases for a newer version on startup, shows changelog/release notes,
and performs a verified one-click download + install + restart.

Author note: fill in the CONFIG section below for your own app/repo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

# --------------------------------------------------------------------------
# CONFIG - edit these for your application / GitHub repository
# --------------------------------------------------------------------------

APP_NAME = "PyUpdate"

# This is the version of THIS build. It must be bumped by your build/release
# pipeline (e.g. via CI injecting a value, or bumping this literal) and is
# ALWAYS what is shown to the user as "the installed version" -- it is never
# overwritten by the version fetched from the update server.
APP_VERSION = "1.4.2"

GITHUB_OWNER = "gurraoptimus"
GITHUB_REPO = "pyupdate"

# Only consider assets whose filename contains one of these hints for the
# current platform. Used to pick the right release asset automatically.
ASSET_HINTS = {
    "win32": ("win", ".exe", ".zip"),
    "darwin": ("mac", "osx", ".dmg", ".zip"),
    "linux": ("linux", ".tar.gz", ".appimage"),
}

ALLOW_PRERELEASE = True
REQUIRE_CHECKSUM = True  # refuse to install if we cannot verify integrity
REQUEST_TIMEOUT = 15  # seconds
MAX_RETRIES = 3

APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
LOG_FILE = APP_DIR / "update.log"

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logger = logging.getLogger("autoupdate")
logger.setLevel(logging.DEBUG)

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)
logger.addHandler(_console_handler)


# --------------------------------------------------------------------------
# Version parsing / comparison
# --------------------------------------------------------------------------


class Version:
    """Lightweight semantic-version parser/comparator (no external deps)."""

    _PART_RE = re.compile(r"\d+|[a-zA-Z]+")

    def __init__(self, raw: str):
        self.raw = raw
        cleaned = raw.strip().lstrip("vV")
        core, _, pre = cleaned.partition("-")
        self.core = tuple(int(p) for p in re.findall(r"\d+", core.split("+")[0]))
        self.pre = pre  # non-empty pre-release means "lower" than same core release

    def _key(self):
        # Pad core to length 3 for consistent comparisons (major, minor, patch).
        core = self.core + (0,) * max(0, 3 - len(self.core))
        # A release with no pre-release tag outranks one with a pre-release tag.
        has_pre = 1 if self.pre else 0
        return (core, -has_pre, self.pre)

    def __eq__(self, other: "Version") -> bool:
        return self._key() == other._key()

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __str__(self) -> str:
        return self.raw


def is_newer(latest: str, current: str) -> bool:
    try:
        return Version(latest) > Version(current)
    except Exception:
        logger.warning("Falling back to string comparison for versions %r vs %r", latest, current)
        return latest.strip().lstrip("vV") != current.strip().lstrip("vV")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    digest_sha256: Optional[str] = None


@dataclass
class ReleaseInfo:
    tag: str
    version: str
    notes: str
    prerelease: bool
    assets: list = field(default_factory=list)


class UpdateError(Exception):
    """Raised for any recoverable auto-update failure (network, parsing, etc.)."""


# --------------------------------------------------------------------------
# GitHub Releases client
# --------------------------------------------------------------------------


class GitHubReleaseChecker:
    def __init__(self, owner: str, repo: str, allow_prerelease: bool = False):
        self.owner = owner
        self.repo = repo
        self.allow_prerelease = allow_prerelease

    def _api_url(self) -> str:
        if self.allow_prerelease:
            return f"https://api.github.com/repos/{self.owner}/{self.repo}/releases"
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/releases/latest"

    def fetch_latest(self) -> ReleaseInfo:
        url = self._api_url()
        data = _http_get_json(url)

        if self.allow_prerelease:
            if not data:
                raise UpdateError("No releases found for this repository.")
            release = data[0]
        else:
            release = data

        tag = release.get("tag_name", "")
        if not tag:
            raise UpdateError("Release metadata is missing a tag/version.")

        assets = []
        for a in release.get("assets", []):
            digest = a.get("digest")  # e.g. "sha256:abcdef..."
            sha256 = None
            if digest and digest.startswith("sha256:"):
                sha256 = digest.split(":", 1)[1]
            assets.append(
                ReleaseAsset(
                    name=a.get("name", ""),
                    download_url=a.get("browser_download_url", ""),
                    size=a.get("size", 0),
                    digest_sha256=sha256,
                )
            )

        return ReleaseInfo(
            tag=tag,
            version=tag.lstrip("vV"),
            notes=release.get("body") or "No release notes provided.",
            prerelease=bool(release.get("prerelease", False)),
            assets=assets,
        )

    @staticmethod
    def pick_asset(assets: list, hints: tuple) -> Optional[ReleaseAsset]:
        for asset in assets:
            lname = asset.name.lower()
            if any(hint in lname for hint in hints):
                return asset
        # Fall back to the first non-checksum asset if nothing matched.
        for asset in assets:
            if asset.name.lower() not in ("checksums.txt", "sha256sums", "sha256sums.txt"):
                return asset
        return None

    @staticmethod
    def find_checksums_asset(assets: list) -> Optional[ReleaseAsset]:
        for asset in assets:
            if asset.name.lower() in ("checksums.txt", "sha256sums", "sha256sums.txt"):
                return asset
        return None


def _http_get_json(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_NAME}-auto-updater",
        },
    )
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdateError("No releases found for this repository.") from e
            if e.code == 403:
                raise UpdateError("GitHub API rate limit exceeded. Try again later.") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
        logger.warning("Update check attempt %d/%d failed: %s", attempt, MAX_RETRIES, last_err)
        time.sleep(min(2 ** attempt, 8))
    raise UpdateError(f"Could not reach update server: {last_err}")


# --------------------------------------------------------------------------
# Secure download
# --------------------------------------------------------------------------


def _validate_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UpdateError(f"Refusing to download from non-HTTPS URL: {url}")
    if not parsed.netloc:
        raise UpdateError(f"Invalid download URL: {url}")


def download_file(
    url: str,
    dest: Path,
    expected_size: int = 0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Download `url` to `dest`, returning the sha256 hex digest of the file."""
    _validate_https_url(url)

    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-auto-updater"})
    hasher = hashlib.sha256()
    downloaded = 0

    tmp_path = dest.with_suffix(dest.suffix + ".part")
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            downloaded = 0
            hasher = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp, open(tmp_path, "wb") as out:
                total = expected_size or int(resp.headers.get("Content-Length", 0) or 0)
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
            if expected_size and downloaded != expected_size:
                raise UpdateError(
                    f"Downloaded size ({downloaded}) does not match expected size ({expected_size})."
                )
            tmp_path.replace(dest)
            return hasher.hexdigest()
        except (urllib.error.URLError, OSError, UpdateError) as e:
            last_err = e
            logger.warning("Download attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            time.sleep(min(2 ** attempt, 8))
    tmp_path.unlink(missing_ok=True)
    raise UpdateError(f"Failed to download update after {MAX_RETRIES} attempts: {last_err}")


def verify_checksum(computed_sha256: str, expected_sha256: str) -> bool:
    return hmac_safe_compare(computed_sha256.lower(), expected_sha256.lower())


def hmac_safe_compare(a: str, b: str) -> bool:
    # Constant-time comparison to avoid timing side channels.
    import hmac

    return hmac.compare_digest(a, b)


def parse_checksums_file(text: str) -> dict:
    """Parse a standard `sha256sum` style checksums file into {filename: hash}."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            digest, filename = parts[0], parts[-1]
            filename = filename.lstrip("*")
            result[filename] = digest.lower()
    return result


# --------------------------------------------------------------------------
# Installer
# --------------------------------------------------------------------------


class Installer:
    """Applies a downloaded update and restarts the application."""

    def __init__(self, app_dir: Path):
        self.app_dir = app_dir

    def install_and_restart(self, downloaded_file: Path) -> None:
        if getattr(sys, "frozen", False):
            self._install_frozen_exe(downloaded_file)
        else:
            self._install_source_zip(downloaded_file)

    def _install_frozen_exe(self, new_exe: Path) -> None:
        current_exe = Path(sys.executable)
        pid = os.getpid()
        script = self.app_dir / "_apply_update.bat"
        script.write_text(
            "@echo off\r\n"
            f":wait\r\n"
            f"tasklist /FI \"PID eq {pid}\" | find \"{pid}\" >nul\r\n"
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            f'copy /Y "{new_exe}" "{current_exe}"\r\n'
            f'start "" "{current_exe}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8",
        )
        logger.info("Launching update installer script and exiting current process.")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
        sys.exit(0)

    def _install_source_zip(self, zip_path: Path) -> None:
        extract_dir = Path(tempfile.mkdtemp(prefix="app_update_"))
        with zipfile.ZipFile(zip_path) as zf:
            # Guard against zip-slip path traversal before extracting anything.
            for member in zf.namelist():
                member_path = (extract_dir / member).resolve()
                if not str(member_path).startswith(str(extract_dir.resolve())):
                    raise UpdateError(f"Unsafe path in update archive: {member}")
            zf.extractall(extract_dir)

        pid = os.getpid()
        python = sys.executable
        main_script = Path(sys.argv[0]).resolve()
        script = self.app_dir / "_apply_update.bat"
        script.write_text(
            "@echo off\r\n"
            ":wait\r\n"
            f"tasklist /FI \"PID eq {pid}\" | find \"{pid}\" >nul\r\n"
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            f'xcopy /E /Y /I "{extract_dir}" "{self.app_dir}" >nul\r\n'
            f'start "" "{python}" "{main_script}"\r\n'
            f'rmdir /S /Q "{extract_dir}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8",
        )
        logger.info("Launching update installer script and exiting current process.")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
        sys.exit(0)


# --------------------------------------------------------------------------
# Update orchestration
# --------------------------------------------------------------------------


class UpdateManager:
    def __init__(self):
        self.checker = GitHubReleaseChecker(GITHUB_OWNER, GITHUB_REPO, ALLOW_PRERELEASE)
        self.installer = Installer(APP_DIR)

    def check(self) -> Optional[ReleaseInfo]:
        """Returns ReleaseInfo if a newer version is available, else None."""
        release = self.checker.fetch_latest()
        logger.info("Current version: %s | Latest available: %s", APP_VERSION, release.version)
        if is_newer(release.version, APP_VERSION):
            return release
        return None

    def download_and_install(
        self, release: ReleaseInfo, progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> None:
        hints = ASSET_HINTS.get(sys.platform, ())
        asset = self.checker.pick_asset(release.assets, hints)
        if asset is None or not asset.download_url:
            raise UpdateError("No suitable release asset found for this platform.")

        expected_sha256 = asset.digest_sha256
        if not expected_sha256:
            checksums_asset = self.checker.find_checksums_asset(release.assets)
            if checksums_asset:
                with tempfile.TemporaryDirectory() as tmp:
                    cs_path = Path(tmp) / "checksums.txt"
                    download_file(checksums_asset.download_url, cs_path)
                    table = parse_checksums_file(cs_path.read_text(encoding="utf-8", errors="ignore"))
                    expected_sha256 = table.get(asset.name)

        if not expected_sha256 and REQUIRE_CHECKSUM:
            raise UpdateError(
                f"No checksum available to verify '{asset.name}'. Refusing to install for safety."
            )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / asset.name
            logger.info("Downloading update asset: %s (%s bytes)", asset.name, asset.size)
            computed = download_file(asset.download_url, dest, asset.size, progress_cb)

            if expected_sha256:
                if not verify_checksum(computed, expected_sha256):
                    raise UpdateError(
                        "Checksum verification failed. The downloaded file may be corrupted "
                        "or tampered with; aborting installation."
                    )
                logger.info("Checksum verified for %s", asset.name)
            else:
                logger.warning("Proceeding without checksum verification (not recommended).")

            final_path = APP_DIR / asset.name
            shutil.copy2(dest, final_path)
            logger.info("Applying update and restarting application.")
            self.installer.install_and_restart(final_path)


# --------------------------------------------------------------------------
# GUI - theme
# --------------------------------------------------------------------------


class Theme:
    BG = "#0f172a"          # slate-900
    BG_CARD = "#1e293b"     # slate-800
    FG = "#e2e8f0"          # slate-200
    FG_MUTED = "#94a3b8"    # slate-400
    ACCENT = "#3b82f6"      # blue-500
    ACCENT_HOVER = "#2563eb"
    SUCCESS = "#22c55e"
    DANGER = "#ef4444"
    FONT_TITLE = ("Segoe UI", 20, "bold")
    FONT_SUBTITLE = ("Segoe UI", 11)
    FONT_BODY = ("Segoe UI", 10)
    FONT_MONO = ("Consolas", 9)


def _style_button(btn: tk.Button, bg=Theme.ACCENT, hover=Theme.ACCENT_HOVER, fg="white"):
    btn.configure(
        bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
        relief="flat", bd=0, padx=16, pady=8, font=Theme.FONT_BODY, cursor="hand2",
    )
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover))
    btn.bind("<Leave>", lambda e: btn.configure(bg=bg))


class Spinner:
    """Small text-based spinner animation driven by `after()`."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, widget: tk.Label, base_text_fn: Callable[[], str], interval: int = 90):
        self.widget = widget
        self.base_text_fn = base_text_fn
        self.interval = interval
        self._i = 0
        self._job = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._tick()

    def _tick(self):
        if not self._running:
            return
        frame = self.FRAMES[self._i % len(self.FRAMES)]
        self._i += 1
        self.widget.config(text=f"{frame}  {self.base_text_fn()}")
        self._job = self.widget.after(self.interval, self._tick)

    def stop(self):
        self._running = False
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None


# --------------------------------------------------------------------------
# GUI - application shell / page navigation
# --------------------------------------------------------------------------


class App(tk.Tk):
    """Root window that hosts and fades between Welcome, Dashboard and Error pages."""

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("520x480")
        self.minsize(480, 440)
        self.configure(bg=Theme.BG)
        self.attributes("-alpha", 0.0)

        self.manager = UpdateManager()

        container = tk.Frame(self, bg=Theme.BG)
        container.pack(fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.frames: dict[str, tk.Frame] = {}
        for Page in (WelcomePage, DashboardPage, ErrorPage):
            frame = Page(container, self)
            self.frames[Page.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.show_page("WelcomePage")
        self.after(50, self._fade_in)

    def show_page(self, name: str):
        self.frames[name].tkraise()
        on_show = getattr(self.frames[name], "on_show", None)
        if callable(on_show):
            on_show()

    def show_error(self, message: str, retry_target: str = "DashboardPage"):
        self.frames["ErrorPage"].set_error(message, retry_target)
        self.show_page("ErrorPage")

    def _fade_in(self, alpha: float = 0.0):
        alpha = min(alpha + 0.08, 1.0)
        self.attributes("-alpha", alpha)
        if alpha < 1.0:
            self.after(15, lambda: self._fade_in(alpha))


# --------------------------------------------------------------------------
# GUI - Welcome page
# --------------------------------------------------------------------------


class WelcomePage(tk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, bg=Theme.BG)
        self.app = app

        center = tk.Frame(self, bg=Theme.BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        self.logo_label = tk.Label(center, text="◆", font=("Segoe UI", 42), bg=Theme.BG, fg=Theme.BG)
        self.logo_label.pack(pady=(0, 8))

        self.title_label = tk.Label(center, text=APP_NAME, font=Theme.FONT_TITLE, bg=Theme.BG, fg=Theme.BG)
        self.title_label.pack()

        self.subtitle_label = tk.Label(
            center, text=f"Version {APP_VERSION}", font=Theme.FONT_SUBTITLE, bg=Theme.BG, fg=Theme.BG
        )
        self.subtitle_label.pack(pady=(4, 24))

        self.enter_btn = tk.Button(center, text="Get Started", command=self._enter)
        _style_button(self.enter_btn)
        self.enter_btn.pack()
        self.enter_btn.pack_forget()  # revealed after the intro animation finishes

        # (widget, target foreground color) pairs faded in one at a time.
        self._widgets = (
            (self.logo_label, Theme.ACCENT),
            (self.title_label, Theme.FG),
            (self.subtitle_label, Theme.FG_MUTED),
        )
        self._animated = False

    def on_show(self):
        if not self._animated:
            self._animated = True
            self._fade_widget(0)

    @staticmethod
    def _blend(bg_hex: str, fg_hex: str, t: float) -> str:
        bg = tuple(int(bg_hex[i : i + 2], 16) for i in (1, 3, 5))
        fg = tuple(int(fg_hex[i : i + 2], 16) for i in (1, 3, 5))
        mixed = tuple(int(bg[i] + (fg[i] - bg[i]) * t) for i in range(3))
        return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"

    def _fade_widget(self, index: int, t: float = 0.0):
        if index >= len(self._widgets):
            self.enter_btn.pack(pady=(0, 0))
            return
        widget, target_color = self._widgets[index]
        widget.config(fg=self._blend(Theme.BG, target_color, t))
        if t < 1.0:
            self.after(20, lambda: self._fade_widget(index, min(t + 0.1, 1.0)))
        else:
            self.after(60, lambda: self._fade_widget(index + 1))

    def _enter(self):
        self.app.show_page("DashboardPage")


# --------------------------------------------------------------------------
# GUI - Dashboard (updater) page
# --------------------------------------------------------------------------


class DashboardPage(tk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, bg=Theme.BG)
        self.app = app
        self.pending_release: Optional[ReleaseInfo] = None
        self._checked_once = False
        self._build_ui()

    def _build_ui(self):
        header = tk.Frame(self, bg=Theme.BG, pady=18)
        header.pack(fill="x", padx=20)
        tk.Label(header, text=APP_NAME, font=Theme.FONT_TITLE, bg=Theme.BG, fg=Theme.FG).pack(anchor="w")
        # Always shows the real installed build version, never the server's version.
        tk.Label(
            header, text=f"Version: {APP_VERSION}", font=Theme.FONT_SUBTITLE, bg=Theme.BG, fg=Theme.FG_MUTED
        ).pack(anchor="w")

        card = tk.Frame(self, bg=Theme.BG_CARD, padx=18, pady=18)
        card.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        self.status_label = tk.Label(
            card, text="Checking for updates...", font=("Segoe UI", 12, "bold"),
            justify="left", bg=Theme.BG_CARD, fg=Theme.FG, wraplength=440,
        )
        self.status_label.pack(anchor="w")

        self.notes_box = scrolledtext.ScrolledText(
            card, height=9, wrap="word", state="disabled", bg="#0b1220", fg=Theme.FG,
            insertbackground=Theme.FG, relief="flat", font=Theme.FONT_MONO, borderwidth=0,
        )
        self.notes_box.pack(fill="both", expand=True, pady=(12, 12))

        self.progress = ttk.Progressbar(card, mode="determinate", maximum=100)

        button_row = tk.Frame(card, bg=Theme.BG_CARD)
        button_row.pack(fill="x")
        self.update_btn = tk.Button(button_row, text="Update Now", command=self._on_update_clicked, state="disabled")
        _style_button(self.update_btn, bg=Theme.SUCCESS, hover="#16a34a")
        self.update_btn.pack(side="left")
        self.recheck_btn = tk.Button(button_row, text="Check Again", command=self._start_update_check)
        _style_button(self.recheck_btn, bg=Theme.BG_CARD, hover="#334155")
        self.recheck_btn.pack(side="left", padx=(8, 0))

        self.spinner = Spinner(self.status_label, lambda: "Checking for updates...")

    def on_show(self):
        if not self._checked_once:
            self._checked_once = True
            self._start_update_check()

    def _set_notes(self, text: str):
        self.notes_box.configure(state="normal")
        self.notes_box.delete("1.0", "end")
        self.notes_box.insert("1.0", text)
        self.notes_box.configure(state="disabled")

    def _start_update_check(self):
        self._set_notes("")
        self.update_btn.config(state="disabled")
        self.recheck_btn.config(state="disabled")
        self.spinner.start()
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            release = self.manager_check()
        except UpdateError as e:
            logger.error("Update check failed: %s", e)
            self.after(0, self._show_check_error, str(e))
            return
        except Exception as e:  # unexpected/defensive
            logger.exception("Unexpected error during update check")
            self.after(0, self._show_check_error, str(e))
            return
        self.after(0, self._show_check_result, release)

    def manager_check(self):
        return self.app.manager.check()

    def _show_check_error(self, message: str):
        self.spinner.stop()
        self.recheck_btn.config(state="normal")
        # Transient network hiccups stay inline; route to the Error page too
        # so the user has a clear retry path for persistent failures.
        self.app.show_error(message, retry_target="DashboardPage")

    def _show_check_result(self, release: Optional[ReleaseInfo]):
        self.spinner.stop()
        self.recheck_btn.config(state="normal")
        if release is None:
            self.pending_release = None
            self.status_label.config(text="✓ You are running the latest version.", fg=Theme.SUCCESS)
            self._set_notes("")
            self.update_btn.config(state="disabled")
            return

        self.pending_release = release
        self.status_label.config(
            text=f"Update Available!  Latest Version: {release.version}", fg=Theme.ACCENT
        )
        self._set_notes(release.notes)
        self.update_btn.config(state="normal")

    def _on_update_clicked(self):
        if not self.pending_release:
            return
        if not messagebox.askyesno(
            "Confirm Update",
            f"Download and install version {self.pending_release.version} now?\n"
            "The application will restart automatically.",
        ):
            return

        self.update_btn.config(state="disabled")
        self.recheck_btn.config(state="disabled")
        self.progress.pack(fill="x", pady=(0, 10))
        self.progress["value"] = 0
        self.status_label.config(text="Downloading update...", fg=Theme.FG)
        threading.Thread(target=self._update_worker, args=(self.pending_release,), daemon=True).start()

    def _update_worker(self, release: ReleaseInfo):
        def progress_cb(done, total):
            pct = int(done * 100 / total) if total else 0
            self.after(0, lambda: self.progress.config(value=pct))

        try:
            self.app.manager.download_and_install(release, progress_cb)
            # install_and_restart exits the process on success; if we reach
            # here, something prevented the restart from happening.
        except UpdateError as e:
            logger.error("Update failed: %s", e)
            self.after(0, self._show_update_error, str(e))
        except Exception as e:
            logger.exception("Unexpected error during update installation")
            self.after(0, self._show_update_error, str(e))

    def _show_update_error(self, message: str):
        self.progress.pack_forget()
        self.update_btn.config(state="normal")
        self.recheck_btn.config(state="normal")
        self.app.show_error(message, retry_target="DashboardPage")


# --------------------------------------------------------------------------
# GUI - Error page
# --------------------------------------------------------------------------


class ErrorPage(tk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, bg=Theme.BG)
        self.app = app
        self.retry_target = "DashboardPage"

        center = tk.Frame(self, bg=Theme.BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        self.icon_label = tk.Label(center, text="⚠", font=("Segoe UI", 40), bg=Theme.BG, fg=Theme.DANGER)
        self.icon_label.pack(pady=(0, 10))

        tk.Label(center, text="Something went wrong", font=("Segoe UI", 15, "bold"), bg=Theme.BG, fg=Theme.FG).pack()

        self.message_label = tk.Label(
            center, text="", font=Theme.FONT_BODY, bg=Theme.BG, fg=Theme.FG_MUTED,
            wraplength=420, justify="center",
        )
        self.message_label.pack(pady=(8, 24))

        button_row = tk.Frame(center, bg=Theme.BG)
        button_row.pack()
        self.retry_btn = tk.Button(button_row, text="Retry", command=self._retry)
        _style_button(self.retry_btn)
        self.retry_btn.pack(side="left")
        self.quit_btn = tk.Button(button_row, text="Quit", command=self.app.destroy)
        _style_button(self.quit_btn, bg=Theme.BG_CARD, hover="#334155")
        self.quit_btn.pack(side="left", padx=(8, 0))

    def set_error(self, message: str, retry_target: str):
        self.retry_target = retry_target
        self.message_label.config(text=message)
        self._pulse(step=0)

    def _pulse(self, step: int):
        # Brief pulse animation on the warning icon to draw attention.
        sizes = [40, 46, 40]
        if step >= len(sizes):
            return
        self.icon_label.config(font=("Segoe UI", sizes[step]))
        self.after(120, lambda: self._pulse(step + 1))

    def _retry(self):
        self.app.show_page(self.retry_target)
        target = self.app.frames[self.retry_target]
        if isinstance(target, DashboardPage):
            target._start_update_check()


def main():
    logger.info("Starting %s v%s", APP_NAME, APP_VERSION)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
