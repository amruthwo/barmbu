#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
barmbu
==================

Make Bambu Studio's official network plugin work on 64-bit ARM Linux
(e.g. an Apple-Silicon MacBook running Asahi/Fedora).

Why it works this way
---------------------
Bambu Lab's network plugin (``libbambu_networking.so`` and friends) is the
closed-source part of Bambu Studio that talks to printers.  Bambu only builds
it for **x86_64** Linux; their servers have no ARM64 Linux build.  x86_64
machine code cannot be patched into ARM64 code, and an ARM64 program (such as
the Flatpak Bambu Studio on ARM) cannot load an x86_64 library.

So on ARM64 this tool sets up a **second, x86_64 copy of Bambu Studio** that
runs under emulation, and puts the real plugin next to it:

* **FEX-Emu** translates x86_64 code to ARM64.
* On Apple-Silicon Asahi kernels (16K memory pages) FEX has to run inside
  **muvm**, a tiny virtual machine with 4K pages.  Asahi ships both.
* FEX's x86_64 root filesystem lacks WebKitGTK, which Bambu Studio needs, so
  the tool builds a small add-on image of x86_64 Fedora packages
  (see ``x86_overlay.py``).

The real plugin then loads normally and signs in with its own built-in
credentials.  Nothing is cracked, re-signed or faked.

On an x86_64 PC the tool simply installs the plugin into your Bambu Studio.

Commands: detect, status, download, install, launch, uninstall.
A graphical front-end lives in ``barmbu_gui.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib import request as _urlrequest
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import x86_overlay  # noqa: E402

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
#  Constants -- Bambu's real endpoints, from the public BambuStudio source
#  (src/slic3r/GUI/GUI_App.cpp: get_http_url / get_plugin_url / download_plugin)
# --------------------------------------------------------------------------- #

BAMBU_API_HOSTS = {
    "US": "https://api.bambulab.com/",
    "CN": "https://api.bambulab.cn/",
}
DEFAULT_REGION = "US"
RESOURCE_PATH = "v1/iot-service/api/slicer/resource"
GITHUB_RELEASES_LATEST = (
    "https://api.github.com/repos/bambulab/BambuStudio/releases/latest"
)
# Preferred x86_64 AppImage flavours, best first.
APPIMAGE_PREFERENCE = ("fedora", "ubu24", "ubuntu-24", "ubu22", "ubuntu-22")

# This tool's own GitHub repository ("owner/name"), used to check for new
# releases of the tool itself. Leave the placeholder to disable the check.
TOOL_REPO = "amruthwo/barmbu"

USER_AGENT = f"barmbu/{__version__} (+interoperability tool)"
FALLBACK_STUDIO_VERSION = "02.09.02.00"
FLATPAK_APP_ID = "com.bambulab.BambuStudio"

# Asahi / Fedora package names for the emulator.
EMULATOR_PACKAGES = ("fex-emu", "muvm", "fex-emu-rootfs-fedora")

SYSTEM_FEX_ROOTFS_DIR = Path("/usr/share/fex-emu/RootFS")
SYSTEM_FEX_OVERLAY_DIR = Path("/usr/share/fex-emu/overlays")

# Environment for the emulated Studio.
#  * certificates: the AppImage is built on Ubuntu; point it at Fedora's bundle
#  * GDK_BACKEND=x11: muvm forwards X11 windows
#  * WEBKIT_DISABLE_DMABUF_RENDERER: avoids blank web pages on virtual GPUs
STUDIO_ENV = {
    "SSL_CERT_FILE": "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "SSL_CERT_DIR": "/etc/pki/tls/certs",
    "CURL_CA_BUNDLE": "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "GDK_BACKEND": "x11",
    "WEBKIT_DISABLE_DMABUF_RENDERER": "1",
}


# --------------------------------------------------------------------------- #
#  Output helpers (plain text for people, '@@' lines for the GUI)
# --------------------------------------------------------------------------- #

GUI_MODE = os.environ.get("BARMBU_GUI") == "1"


def _color(text: str, code: str) -> str:
    if GUI_MODE or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(f"{_color('::', '34')} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{_color('ok', '32')} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{_color('!!', '33')} {msg}", flush=True)


def err(msg: str) -> None:
    print(f"{_color('xx', '31')} {msg}", file=sys.stderr, flush=True)


class ToolError(Exception):
    """An error with a message meant for the person, not a traceback."""


def step(n: int, total: int, text: str) -> None:
    """Announce a numbered step (the GUI turns these into a progress bar)."""
    if GUI_MODE:
        print(f"@@step {n} {total} {text}", flush=True)
    info(f"[{n}/{total}] {text}")


def progress(fraction: float, text: str = "") -> None:
    if GUI_MODE:
        print(f"@@progress {fraction:.3f} {text}", flush=True)
    elif sys.stdout.isatty():
        sys.stdout.write(f"\r   {int(fraction * 100):3d}% {text[:60]:<60}")
        sys.stdout.flush()
        if fraction >= 1:
            sys.stdout.write("\n")


# --------------------------------------------------------------------------- #
#  Running host commands (also works from inside a Flatpak sandbox)
# --------------------------------------------------------------------------- #

IN_FLATPAK = Path("/.flatpak-info").exists()


def _host(cmd: list[str], cwd: Optional[str] = None) -> list[str]:
    if not IN_FLATPAK:
        return cmd
    extra = [f"--directory={cwd}"] if cwd else []
    return ["flatpak-spawn", "--host", *extra, *cmd]


def run(cmd: list[str], check: bool = True, capture: bool = False,
        **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _host(cmd, kw.get("cwd")), check=check, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, **kw)
    except FileNotFoundError as exc:
        if check:
            raise ToolError(f"'{cmd[0]}' is not installed") from exc
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def which(name: str) -> Optional[str]:
    if not IN_FLATPAK:
        return shutil.which(name)
    res = run(["sh", "-c", f"command -v {name}"], check=False, capture=True)
    out = (res.stdout or "").strip()
    return out or None


def host_exists(path: Path) -> bool:
    if not IN_FLATPAK:
        return path.exists()
    return run(["test", "-e", str(path)], check=False).returncode == 0


def host_glob(directory: Path, pattern: str) -> list[Path]:
    if not IN_FLATPAK:
        return sorted(directory.glob(pattern)) if directory.is_dir() else []
    res = run(["sh", "-c", f'ls -1 "{directory}"/{pattern} 2>/dev/null'],
              check=False, capture=True)
    return [Path(p) for p in (res.stdout or "").split()]


def home() -> Path:
    return Path(os.path.expanduser("~"))


# --------------------------------------------------------------------------- #
#  Architecture
# --------------------------------------------------------------------------- #

def host_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m


def is_arm64() -> bool:
    return host_arch() == "aarch64"


def page_size() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return 4096


def elf_arch(path: Path) -> Optional[str]:
    """'x86_64', 'aarch64' or None for a file's ELF header."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return None
    e_machine = head[18] | (head[19] << 8)
    return {0x3E: "x86_64", 0xB7: "aarch64"}.get(e_machine)


# --------------------------------------------------------------------------- #
#  Where things live
# --------------------------------------------------------------------------- #

APP_DATA = home() / ".local/share/barmbu"
CACHE_DIR = home() / ".cache/barmbu"
STUDIO_DIR = APP_DATA / "studio"
OVERLAY_IMAGE = APP_DATA / "x86-libs.erofs"
LAUNCHER = APP_DATA / "launch-bambu-studio.sh"
DESKTOP_FILE = home() / ".local/share/applications/bambu-studio-arm64.desktop"
STATE_FILE = APP_DATA / "state.json"
X86_CONFIG_DIR = home() / ".config/BambuStudio"
X86_PLUGIN_DIR = X86_CONFIG_DIR / "plugins"
FLATPAK_CONFIG_DIR = home() / ".var/app" / FLATPAK_APP_ID / "config/BambuStudio"


def migrate_old_name() -> None:
    """Move files from this tool's old name (bambu-arm64-plugin, pre-release builds).

    The x86 Studio copy is ~700 MB, so move it rather than download it again,
    then fix the absolute paths stored in the launcher, menu entry and state.
    """
    old_data = home() / ".local/share/bambu-arm64-plugin"
    if not old_data.is_dir() or APP_DATA.exists():
        return
    old_data.rename(APP_DATA)
    old_cache = home() / ".cache/bambu-arm64-plugin"
    shutil.rmtree(old_cache, ignore_errors=True)       # only temporary downloads
    for f in (STATE_FILE, LAUNCHER, DESKTOP_FILE):
        try:
            f.write_text(f.read_text().replace(str(old_data), str(APP_DATA)))
        except OSError:
            pass
    info(f"Moved your existing set-up to {APP_DATA}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
#  Detecting Bambu Studio installs
# --------------------------------------------------------------------------- #

@dataclass
class StudioInstall:
    kind: str                 # 'flatpak' | 'appimage' | 'native'
    arch: str                 # 'x86_64' | 'aarch64' | 'unknown'
    location: str
    version: Optional[str]
    plugin_dir: str
    managed: bool = False     # True for the x86 copy this tool manages

    @property
    def can_load_x86_plugin(self) -> bool:
        return self.arch == "x86_64"


def _version_from_name(name: str) -> Optional[str]:
    m = re.search(r"(\d{2}\.\d{2}\.\d{2}\.\d{2})", name)
    if m:
        return m.group(1)
    m = re.search(r"v?(\d+\.\d+\.\d+(?:\.\d+)?)", name)
    return m.group(1) if m else None


def _flatpak_installs() -> list[StudioInstall]:
    if not which("flatpak"):
        return []
    res = run(["flatpak", "info", FLATPAK_APP_ID], check=False, capture=True)
    if res.returncode != 0:
        return []
    arch, version = "unknown", None
    for line in (res.stdout or "").splitlines():
        key, _, val = line.strip().partition(":")
        key = key.strip().lower()
        if key == "arch":
            arch = {"arm64": "aarch64"}.get(val.strip(), val.strip())
        elif key == "version":
            version = val.strip()
    return [StudioInstall("flatpak", arch, f"flatpak:{FLATPAK_APP_ID}", version,
                          str(FLATPAK_CONFIG_DIR / "plugins"))]


def _appimage_candidates() -> list[Path]:
    dirs = [STUDIO_DIR, home() / "Applications", home() / "Downloads",
            home() / ".local/bin", home() / "bin", home()]
    pat = re.compile(r"bambu.*studio.*\.appimage$", re.IGNORECASE)
    found: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for entry in sorted(d.iterdir()):
                if entry.is_file() and pat.search(entry.name):
                    p = entry.resolve()
                    if p not in found:
                        found.append(p)
        except OSError:
            continue
    return found


def _native_installs() -> list[StudioInstall]:
    out = []
    for exe_name in ("bambu-studio", "bambustudio", "BambuStudio"):
        exe = which(exe_name)
        if exe:
            p = Path(exe).resolve()
            out.append(StudioInstall("native", elf_arch(p) or "unknown", str(p),
                                     None, str(X86_PLUGIN_DIR)))
    return out


def managed_studios() -> list[Path]:
    """AppImages this tool manages, oldest first.

    The .AppImage file itself is deleted once unpacked (it's ~180 MB), so an
    unpacked ``<name>.extracted`` folder also counts as that AppImage.
    """
    if not STUDIO_DIR.is_dir():
        return []
    names = {p.name for p in STUDIO_DIR.glob("*.AppImage")}
    names |= {d.name[:-len(".extracted")] + ".AppImage"
              for d in STUDIO_DIR.glob("*.extracted")
              if (d / "squashfs-root" / "AppRun").is_file()}
    return [STUDIO_DIR / n for n in sorted(names)]


def detect_studio() -> list[StudioInstall]:
    installs = _flatpak_installs()
    imgs = [p for p in _appimage_candidates() if p.parent != STUDIO_DIR]
    for img in managed_studios() + imgs:
        installs.append(StudioInstall(
            "appimage", "x86_64", str(img), _version_from_name(img.name),
            str(X86_PLUGIN_DIR), managed=img.parent == STUDIO_DIR))
    installs += _native_installs()
    return installs


# --------------------------------------------------------------------------- #
#  The emulator (FEX, plus muvm on 16K-page kernels)
# --------------------------------------------------------------------------- #

@dataclass
class Emulator:
    fex: Optional[str]
    muvm: Optional[str]
    page_size: int
    base_image: Optional[str]
    system_overlays: list
    user_rootfs: Optional[str]

    @property
    def needs_muvm(self) -> bool:
        return self.page_size != 4096

    @property
    def has_rootfs(self) -> bool:
        return bool(self.base_image or self.user_rootfs)

    @property
    def ready(self) -> bool:
        if not self.fex or not self.has_rootfs:
            return False
        return bool(self.muvm) if self.needs_muvm else True

    def problem(self) -> Optional[str]:
        missing = []
        if not self.fex:
            missing.append("FEX-Emu")
        if self.needs_muvm and not self.muvm:
            missing.append("muvm")
        if not self.has_rootfs:
            missing.append("the x86_64 root filesystem")
        if not missing:
            return None
        return "Missing: " + ", ".join(missing)


def emulator_status() -> Emulator:
    base = None
    images = host_glob(SYSTEM_FEX_ROOTFS_DIR, "*.erofs")
    for img in images:
        if img.name == "default.erofs":
            base = img
    if base is None and images:
        base = images[0]
    user_rootfs = None
    urd = home() / ".fex-emu/RootFS"
    if urd.is_dir():
        entries = sorted(urd.iterdir())
        if entries:
            user_rootfs = str(entries[0])
    return Emulator(
        fex=which("FEXBash") or which("FEXInterpreter"),
        muvm=which("muvm"),
        page_size=page_size(),
        base_image=str(base) if base else None,
        system_overlays=[str(p) for p in host_glob(SYSTEM_FEX_OVERLAY_DIR, "*.erofs")],
        user_rootfs=user_rootfs,
    )


def emulator_install_command() -> list[str]:
    if which("dnf"):
        return ["dnf", "install", "-y", *EMULATOR_PACKAGES]
    if which("apt-get"):
        return ["apt-get", "install", "-y", "fex-emu"]
    if which("pacman"):
        return ["pacman", "-S", "--noconfirm", "fex-emu"]
    return []


def emulator_install_hint() -> str:
    cmd = emulator_install_command()
    return ("sudo " + " ".join(cmd)) if cmd else "Install FEX-Emu: https://fex-emu.com/"


# --------------------------------------------------------------------------- #
#  Downloads (stdlib only)
# --------------------------------------------------------------------------- #

def http_get(url: str, headers: Optional[dict] = None, timeout: int = 60) -> bytes:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    with _urlrequest.urlopen(_urlrequest.Request(url, headers=h),
                             timeout=timeout) as resp:  # nosec - fixed hosts
        return resp.read()


def http_download(url: str, dest: Path, label: str) -> Path:
    """Download to dest with progress; writes atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = _urlrequest.Request(url, headers={"User-Agent": USER_AGENT})
    with _urlrequest.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if total and now - last > 0.25:
                last = now
                progress(done / total, f"{label} ({done >> 20} / {total >> 20} MB)")
    if total:
        progress(1.0, label)
    os.replace(tmp, dest)
    return dest


def plugin_query_version(studio_version: Optional[str]) -> str:
    """Studio's plugin key: first three groups, zero-padded, + '.00'.

    Mirrors GUI_App::get_plugin_url (SLIC3R_VERSION.substr(0,9) + "00").
    """
    nums = re.findall(r"\d+", studio_version or "")
    if len(nums) < 3:
        nums = re.findall(r"\d+", FALLBACK_STUDIO_VERSION)
    a, b, c = (int(n) for n in nums[:3])
    return f"{a:02d}.{b:02d}.{c:02d}.00"


def resolve_plugin_resources(studio_version: Optional[str],
                             region: str = DEFAULT_REGION) -> list[dict]:
    host = BAMBU_API_HOSTS.get(region, BAMBU_API_HOSTS[DEFAULT_REGION])
    qver = plugin_query_version(studio_version)
    url = f"{host}{RESOURCE_PATH}?slicer/plugins/cloud={qver}"
    info(f"Asking Bambu for the Linux plugin that matches Studio {qver}")
    raw = http_get(url, headers={"X-BBL-OS-Type": "linux",
                                 "X-BBL-Agent-OS-Type": "linux",
                                 "Accept": "application/json"})
    data = json.loads(raw.decode("utf-8", "replace"))
    if data.get("message") != "success":
        raise ToolError(f"Bambu's server answered: {data!r}"[:300])
    resources = data.get("resources") or []
    if not isinstance(resources, list) or not resources:
        raise ToolError("Bambu's server has no plugin for this Studio version")
    return resources


def download_plugin_package(resources: list[dict], dest_dir: Path) -> Path:
    chosen = next((r for r in resources
                   if str(r.get("type", "")).lower() in ("plugins", "plugin")),
                  resources[0])
    url = chosen.get("url")
    if not url:
        raise ToolError("Bambu's plugin entry has no download address")
    name = os.path.basename(url.split("?")[0]) or "plugin.zip"
    return http_download(url, dest_dir / name, "Network plugin")


def extract_sos_from_package(pkg: Path, dest_dir: Path) -> list[Path]:
    """Unpack a plugin package (a zip of .so files)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    if zipfile.is_zipfile(pkg):
        with zipfile.ZipFile(pkg) as zf:
            for name in zf.namelist():
                base = os.path.basename(name)
                if base.endswith(".so") or ".so." in base:
                    target = dest_dir / base
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    out.append(target)
    elif pkg.suffix == ".so" or ".so." in pkg.name:
        target = dest_dir / pkg.name
        shutil.copy2(pkg, target)
        out.append(target)
    else:
        raise ToolError(f"Unrecognised plugin package: {pkg.name}")
    return out


def collect_local_sos(src_dir: Path) -> list[Path]:
    """x86_64 plugin .so files in src_dir (checked by ELF header)."""
    if not src_dir.is_dir():
        return []
    return [e.resolve() for e in sorted(src_dir.iterdir())
            if e.is_file() and (e.suffix == ".so" or ".so." in e.name)
            and elf_arch(e) == "x86_64"]


def latest_appimage() -> Optional[tuple[str, str]]:
    """(name, url) of the best x86_64 AppImage in Bambu's latest release."""
    try:
        rel = json.loads(http_get(GITHUB_RELEASES_LATEST,
                                  headers={"Accept": "application/vnd.github+json"}))
    except (HTTPError, URLError, ValueError, OSError) as exc:
        warn(f"Could not check Bambu Studio releases: {exc}")
        return None
    assets = [(a.get("name", ""), a.get("browser_download_url", ""))
              for a in rel.get("assets", [])
              if a.get("name", "").lower().endswith(".appimage")]
    for flavour in APPIMAGE_PREFERENCE:
        for name, url in assets:
            if flavour in name.lower():
                return name, url
    return assets[0] if assets else None


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v))


def tool_install_kind() -> str:
    """How this tool was installed: 'flatpak', 'rpm', 'deb' or 'source'."""
    if IN_FLATPAK:
        return "flatpak"
    me = str(Path(__file__).resolve())
    for kind, cmd in (("rpm", ["rpm", "-qf", me]), ("deb", ["dpkg", "-S", me])):
        if shutil.which(cmd[0]) and subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return kind
    return "source"


def pick_release_asset(assets: list[dict], kind: str, arch: str) -> Optional[dict]:
    """The release file that matches how this tool was installed."""
    suffix = {"rpm": ".noarch.rpm", "deb": "_all.deb",
              "flatpak": f"-{arch}.flatpak", "source": ".tar.gz"}[kind]
    for a in assets:
        if str(a.get("name", "")).endswith(suffix) and a.get("browser_download_url"):
            return {"name": a["name"], "url": a["browser_download_url"],
                    "digest": a.get("digest")}       # "sha256:<hex>" when GitHub has it
    return None


def tool_update() -> Optional[dict]:
    """Newest release of this tool on GitHub if it's newer than us, else None.

    Never raises: an update check must not get in the way of anything.
    Set BARMBU_NO_UPDATE_CHECK=1 to turn it off.
    """
    if "OWNER/" in TOOL_REPO or os.environ.get("BARMBU_NO_UPDATE_CHECK"):
        return None
    try:
        rel = json.loads(http_get(
            f"https://api.github.com/repos/{TOOL_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"}, timeout=10))
        latest = str(rel.get("tag_name", "")).lstrip("vV")
        if not latest or _version_tuple(latest) <= _version_tuple(__version__):
            return None
        kind = tool_install_kind()
        return {"version": latest, "kind": kind,
                "url": rel.get("html_url") or
                f"https://github.com/{TOOL_REPO}/releases/latest",
                "asset": pick_release_asset(rel.get("assets") or [], kind, host_arch())}
    except (HTTPError, URLError, ValueError, OSError, KeyError):
        return None


def _flatpak_user_install() -> bool:
    try:
        return "/.local/share/flatpak/" in Path("/.flatpak-info").read_text()
    except OSError:
        return True


def tool_install_command(kind: str, pkg: Path) -> list[str]:
    if kind == "flatpak":
        scope = "--user" if _flatpak_user_install() else "--system"
        return ["flatpak", "install", scope, "-y", "--bundle", str(pkg)]
    if kind == "rpm":
        return ["pkexec", "dnf", "install", "-y", str(pkg)] if which("dnf") \
            else ["pkexec", "rpm", "-U", str(pkg)]
    if kind == "deb":
        return ["pkexec", "apt-get", "install", "-y", str(pkg)]
    return []


def _verify_digest(path: Path, digest: Optional[str]) -> None:
    if not digest or not digest.startswith("sha256:"):
        warn("GitHub gave no checksum for this file; skipping verification")
        return
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != digest.split(":", 1)[1].lower():
        path.unlink(missing_ok=True)
        raise ToolError(f"Checksum mismatch for {path.name}; download deleted")
    ok("Checksum verified")


# --------------------------------------------------------------------------- #
#  The x86_64 Bambu Studio copy
# --------------------------------------------------------------------------- #

def appimage_squashfs_offset(img: Path) -> int:
    """Byte offset of the squashfs inside a type-2 AppImage (= ELF size)."""
    with open(img, "rb") as fh:
        head = fh.read(64)
        if head[:4] != b"\x7fELF" or head[4] != 2:
            raise ToolError(f"{img.name} is not a 64-bit AppImage")
        e_shoff = struct.unpack_from("<Q", head, 0x28)[0]
        e_shentsize, e_shnum = struct.unpack_from("<HH", head, 0x3A)
        offset = e_shoff + e_shentsize * e_shnum
        fh.seek(offset)
        if fh.read(4) != b"hsqs":
            raise ToolError(f"{img.name}: no squashfs found (not a type-2 AppImage?)")
    return offset


def extracted_dir(img: Path) -> Path:
    return STUDIO_DIR / (img.stem + ".extracted")


def extract_appimage(img: Path) -> Path:
    """Unpack an x86_64 AppImage without running it; returns AppRun."""
    root = extracted_dir(img) / "squashfs-root"
    apprun = root / "AppRun"
    if apprun.is_file():
        return apprun
    root.parent.mkdir(parents=True, exist_ok=True)
    if which("unsquashfs"):
        off = appimage_squashfs_offset(img)
        info("Unpacking Bambu Studio (no emulation needed for this step)")
        res = run(["unsquashfs", "-q", "-f", "-n", "-d", str(root), "-o", str(off),
                   str(img)], check=False, capture=True)
        if res.returncode != 0:
            raise ToolError("unsquashfs failed: " + (res.stderr or "").strip()[-400:])
    elif host_arch() == "x86_64":
        os.chmod(img, 0o755)
        run([str(img), "--appimage-extract"], cwd=str(root.parent), capture=True)
    else:
        raise ToolError("'unsquashfs' is needed to unpack Bambu Studio "
                        "(install the squashfs-tools package)")
    if not apprun.is_file():
        raise ToolError("Bambu Studio unpacked, but AppRun is missing")
    return apprun


def ensure_appimage(explicit: Optional[str], allow_download: bool,
                    update: bool) -> Optional[Path]:
    """Find (or fetch) the x86_64 Bambu Studio AppImage."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise ToolError(f"AppImage not found: {p}")
        return p
    managed = managed_studios()
    others = [p for p in _appimage_candidates() if p.parent != STUDIO_DIR]
    current = managed[-1] if managed else (others[0] if others else None)
    if current and not update:
        ok(f"Using Bambu Studio {current.name}")
        return current
    if not allow_download:
        return current
    latest = latest_appimage()
    if latest is None:
        return current
    name, url = latest
    if current and current.name == name:
        ok(f"Bambu Studio is up to date ({name})")
        return current
    dest = http_download(url, STUDIO_DIR / name, "Bambu Studio (x86_64 copy)")
    os.chmod(dest, 0o755)
    ok(f"Downloaded {name}")
    for old in managed:                      # tidy up the previous version
        if old != dest:
            shutil.rmtree(extracted_dir(old), ignore_errors=True)
            old.unlink(missing_ok=True)
    return dest


def studio_elf_files(apprun: Path) -> tuple[list[Path], set[str]]:
    bin_dir = apprun.parent / "bin"
    files = [p for p in bin_dir.iterdir() if p.is_file() and elf_arch(p) == "x86_64"]
    return files, {p.name for p in bin_dir.iterdir()}


# --------------------------------------------------------------------------- #
#  Installing pieces
# --------------------------------------------------------------------------- #

def install_sos(sos: Iterable[Path], plugin_dir: Path) -> list[Path]:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for so in sos:
        target = plugin_dir / so.name
        shutil.copy2(so, target)
        os.chmod(target, 0o644)
        done.append(target)
        ok(f"Installed {target.name}")
    return done


def acquire_plugin(args, studio_version: Optional[str], workdir: Path) -> list[Path]:
    """Plugin .so files: --local-dir, then Bambu's server, then local copies."""
    if getattr(args, "local_dir", None):
        sos = collect_local_sos(Path(args.local_dir).expanduser())
        if sos:
            ok(f"Using {len(sos)} plugin file(s) from {args.local_dir}")
            return sos
        warn(f"No x86_64 plugin files in {args.local_dir}")
    if args.source in ("auto", "download") and not args.offline:
        try:
            pkg = download_plugin_package(
                resolve_plugin_resources(studio_version, args.region), workdir)
            sos = extract_sos_from_package(pkg, workdir / "unpacked")
            if sos:
                ok(f"Got {len(sos)} plugin file(s) from Bambu")
                return sos
        except (HTTPError, URLError, ToolError, ValueError, OSError) as exc:
            warn(f"Could not download the plugin from Bambu: {exc}")
            if args.source == "download":
                raise ToolError("Plugin download failed") from exc
    return []


def copy_flatpak_settings() -> bool:
    """First run only: start the x86 copy with the Flatpak's settings."""
    marker = X86_CONFIG_DIR / ".copied-from-flatpak"
    if marker.exists() or not (FLATPAK_CONFIG_DIR / "BambuStudio.conf").is_file():
        return False
    shutil.copytree(FLATPAK_CONFIG_DIR, X86_CONFIG_DIR, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("plugins", "log", "cache*", "*.lock"))
    marker.write_text("Settings were copied once from the Flatpak Bambu Studio.\n")
    ok("Copied your printers, filaments and settings from the Flatpak Studio")
    return True


def launch_command(emu: Emulator, apprun: Path, overlay: Optional[Path]) -> list[str]:
    """The command that starts the x86_64 Studio."""
    if emu.needs_muvm:
        cmd = ["muvm"]
        if overlay and emu.base_image:
            # Passing -f replaces muvm's defaults, so list base + overlays.
            cmd += ["-f", emu.base_image]
            for o in emu.system_overlays:
                cmd += ["-f", o]
            cmd += ["-f", str(overlay)]
        for k, v in STUDIO_ENV.items():
            cmd += ["-e", f"{k}={v}"]
        return cmd + ["--", str(apprun)]
    env = [f"{k}={v}" for k, v in STUDIO_ENV.items()]
    return ["env", *env, "FEXBash", "-c", 'exec "$0" "$@"', str(apprun)]


def write_launcher(cmd: list[str]) -> Path:
    import shlex
    APP_DATA.mkdir(parents=True, exist_ok=True)
    LAUNCHER.write_text(
        "#!/usr/bin/env bash\n"
        f"# Starts the x86_64 Bambu Studio under emulation (barmbu {__version__}).\n"
        f"exec {' '.join(shlex.quote(c) for c in cmd)} \"$@\"\n")
    os.chmod(LAUNCHER, 0o755)
    return LAUNCHER


def write_desktop_entry(apprun: Path) -> Path:
    icon = apprun.parent / "BambuStudio.png"
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Bambu Studio (x86, with network plugin)\n"
        "Comment=Bambu Studio with Bambu's network plugin, running under emulation\n"
        f"Exec={LAUNCHER} %F\n"
        f"Icon={icon if icon.is_file() else 'applications-engineering'}\n"
        "Terminal=false\n"
        "Categories=Graphics;3DGraphics;Engineering;\n"
        "MimeType=model/stl;model/3mf;application/vnd.ms-3mfdocument;\n"
        "StartupWMClass=BambuStudio\n")
    return DESKTOP_FILE


# --------------------------------------------------------------------------- #
#  Status (shared by `status --json` and the GUI)
# --------------------------------------------------------------------------- #

def status_dict() -> dict:
    state = load_state()
    arch = host_arch()
    installs = detect_studio()
    emu = emulator_status() if arch == "aarch64" else None
    plugin_files = sorted(p.name for p in X86_PLUGIN_DIR.glob("*.so")) \
        if X86_PLUGIN_DIR.is_dir() else []
    managed = [i for i in installs if i.kind == "appimage"]
    apprun = Path(state["apprun"]) if state.get("apprun") else None
    ready = bool(
        "libbambu_networking.so" in plugin_files and
        (arch != "aarch64" or (emu and emu.ready and apprun and apprun.is_file()
                               and LAUNCHER.is_file())))
    return {
        "arch": arch,
        "page_size": page_size(),
        "studios": [asdict(i) | {"can_load_x86_plugin": i.can_load_x86_plugin}
                    for i in installs],
        "emulator": (asdict(emu) | {"ready": emu.ready, "needs_muvm": emu.needs_muvm,
                                    "problem": emu.problem(),
                                    "install_command": emulator_install_command()})
                    if emu else None,
        "x86_studio": {"appimage": state.get("appimage") or
                       (managed[0].location if managed else None),
                       "version": state.get("studio_version") or
                       (managed[0].version if managed else None),
                       "extracted": bool(apprun and apprun.is_file())},
        "libs_overlay": {"path": str(OVERLAY_IMAGE) if OVERLAY_IMAGE.is_file() else None,
                         "packages": state.get("overlay_packages", []),
                         "needed": state.get("overlay_needed")},
        "plugin": {"dir": str(X86_PLUGIN_DIR), "files": plugin_files,
                   "version": state.get("plugin_version")},
        "launcher": str(LAUNCHER) if LAUNCHER.is_file() else None,
        "ready": ready,
    }


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #

def cmd_detect(args) -> int:
    s = status_dict()
    print(f"Computer: {s['arch']} (memory pages {s['page_size'] // 1024}K)\n")
    if not s["studios"]:
        warn("No Bambu Studio found (Flatpak, AppImage or package).")
    for i, st in enumerate(s["studios"], 1):
        tag = " [managed by this tool]" if st["managed"] else ""
        print(f"[{i}] {st['kind']}  {st['arch']}  {st['version'] or ''}{tag}")
        print(f"    {st['location']}")
        print("    loads Bambu's plugin directly: "
              + ("yes" if st["can_load_x86_plugin"] else "no (ARM build)"))
    e = s["emulator"]
    if e:
        print("\nEmulator:")
        print(f"    FEX-Emu   : {e['fex'] or 'not installed'}")
        if e["needs_muvm"]:
            print(f"    muvm      : {e['muvm'] or 'not installed'} (needed: 16K pages)")
        print(f"    x86 rootfs: {e['base_image'] or e['user_rootfs'] or 'missing'}")
        if e["problem"]:
            print(f"    {e['problem']} -> {emulator_install_hint()}")
    p = s["plugin"]
    print(f"\nNetwork plugin: {', '.join(p['files']) or 'not installed'}")
    print(f"Ready: {'yes' if s['ready'] else 'no'}")
    return 0


def cmd_status(args) -> int:
    if args.json:
        print(json.dumps(status_dict(), indent=2))
        return 0
    return cmd_detect(args)


def cmd_download(args) -> int:
    workdir = Path(args.out).expanduser() if args.out else CACHE_DIR / "plugin"
    ver = next((i.version for i in detect_studio() if i.version), None)
    sos = acquire_plugin(args, ver, workdir)
    if not sos:
        raise ToolError("No plugin files obtained")
    ok(f"{len(sos)} plugin file(s) ready:")
    for s in sos:
        print(f"    {s}")
    return 0


def _install_arm64(args, state: dict) -> None:
    total = 6
    step(1, total, "Checking the x86 emulator")
    emu = emulator_status()
    if not emu.ready:
        raise ToolError(f"{emu.problem()}. Install it with:\n    {emulator_install_hint()}")
    ok("FEX" + (" + muvm" if emu.needs_muvm else "") + " ready")

    step(2, total, "Getting the x86_64 Bambu Studio")
    img = ensure_appimage(args.appimage, args.download_studio or args.update,
                          args.update)
    if img is None:
        raise ToolError("No x86_64 Bambu Studio found. Run again with "
                        "--download-studio, or pass --appimage FILE")
    apprun = extract_appimage(img)
    if img.parent == STUDIO_DIR and img.is_file():
        img.unlink()                     # unpacked copy is all we need; saves ~180 MB
    version = _version_from_name(img.name)
    state.update(appimage=str(img), apprun=str(apprun), studio_version=version)

    step(3, total, "Adding the libraries Bambu Studio needs")
    overlay: Optional[Path] = None
    if emu.base_image and which("dump.erofs"):
        image = x86_overlay.ErofsImage(Path(emu.base_image), run)
        files, bundled = studio_elf_files(apprun)
        missing = x86_overlay.missing_sonames(files, bundled, image)
        state["overlay_needed"] = missing
        if not missing:
            ok("Nothing missing")
            OVERLAY_IMAGE.unlink(missing_ok=True)
        elif (OVERLAY_IMAGE.is_file() and not args.rebuild_libs
              and state.get("overlay_for") == missing):
            ok("Library add-on already built")
            overlay = OVERLAY_IMAGE
        else:
            report = x86_overlay.build_overlay(
                missing, image, OVERLAY_IMAGE, CACHE_DIR / "overlay", run, info)
            if report["unresolved"]:
                warn("Still missing: " + ", ".join(report["unresolved"]))
            state.update(overlay_packages=report["packages"], overlay_for=missing)
            ok(f"Built library add-on ({len(report['packages'])} packages)")
            overlay = OVERLAY_IMAGE
    else:
        warn("Can't inspect the x86 root filesystem (needs dump.erofs); skipping")

    step(4, total, "Bringing over your settings")
    if not copy_flatpak_settings():
        ok("Keeping existing settings")

    step(5, total, "Installing Bambu's network plugin")
    sos = acquire_plugin(args, version, CACHE_DIR / "plugin")
    if not sos:
        raise ToolError("Couldn't get the network plugin from Bambu. If you "
                        "have the plugin files, pass --local-dir DIR")
    state["installed"] = [str(p) for p in install_sos(sos, X86_PLUGIN_DIR)]
    shutil.rmtree(CACHE_DIR / "plugin", ignore_errors=True)
    state["plugin_version"] = plugin_query_version(version)

    step(6, total, "Creating the app-menu shortcut")
    write_launcher(launch_command(emu, apprun, overlay))
    write_desktop_entry(apprun)
    state.update(launcher=str(LAUNCHER), desktop=str(DESKTOP_FILE))
    ok("Added 'Bambu Studio (x86, with network plugin)' to your app menu")


def _install_x86(args, state: dict) -> None:
    step(1, 2, "Finding Bambu Studio")
    installs = [i for i in detect_studio() if i.can_load_x86_plugin] or detect_studio()
    target = installs[0] if installs else None
    plugin_dir = Path(target.plugin_dir) if target else X86_PLUGIN_DIR
    ok(f"Plugin folder: {plugin_dir}")
    step(2, 2, "Installing Bambu's network plugin")
    sos = acquire_plugin(args, target.version if target else None, CACHE_DIR / "plugin")
    if not sos:
        raise ToolError("No plugin files available")
    state["installed"] = [str(p) for p in install_sos(sos, plugin_dir)]
    shutil.rmtree(CACHE_DIR / "plugin", ignore_errors=True)


def cmd_install(args) -> int:
    state = load_state()
    state["arch"] = host_arch()
    try:
        if is_arm64():
            _install_arm64(args, state)
        else:
            _install_x86(args, state)
    finally:
        save_state(state)
    ok("All set. Open 'Bambu Studio (x86, with network plugin)' from your app menu."
       if is_arm64() else "All set. Start Bambu Studio as usual.")
    return 0


def cmd_check_update(args) -> int:
    upd = tool_update()
    if args.json:
        print(json.dumps({"current": __version__, "update": upd}))
    elif upd:
        info(f"bARMbu Studio Helper {upd['version']} is available "
             f"(you have {__version__}). Run: barmbu self-update")
    else:
        ok(f"bARMbu Studio Helper {__version__} is up to date")
    return 0


def cmd_self_update(args) -> int:
    upd = tool_update()
    if not upd:
        ok(f"bARMbu Studio Helper {__version__} is up to date")
        return 0
    kind, asset = upd["kind"], upd["asset"]
    step(1, 2, f"Downloading version {upd['version']} ({kind})")
    if kind == "source" or not asset:
        info(f"No package to install automatically for a {kind} install. "
             f"Get it here: {upd['url']}")
        return 0
    pkg = http_download(asset["url"], CACHE_DIR / "tool-update" / asset["name"],
                        asset["name"])
    _verify_digest(pkg, asset.get("digest"))
    if args.download_only:
        ok(f"Downloaded {pkg}")
        return 0
    step(2, 2, f"Installing {asset['name']}")
    res = run(tool_install_command(kind, pkg), check=False)
    if res.returncode != 0:
        raise ToolError(f"Installing the update failed (code {res.returncode}). "
                        f"The file is in {pkg.parent}")
    shutil.rmtree(pkg.parent, ignore_errors=True)
    ok(f"Updated to {upd['version']}. Restart bARMbu Studio Helper to use it.")
    return 0


def cmd_launch(args) -> int:
    if not LAUNCHER.is_file():
        raise ToolError("Not set up yet - run install first")
    subprocess.Popen(_host([str(LAUNCHER)]), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok("Starting Bambu Studio (the first start under emulation can take a minute)")
    return 0


def cmd_uninstall(args) -> int:
    state = load_state()
    for p in state.get("installed", []):
        Path(p).unlink(missing_ok=True)
    for p in (LAUNCHER, DESKTOP_FILE, STATE_FILE):
        p.unlink(missing_ok=True)
    ok("Removed the network plugin and the app-menu shortcut")
    if args.all:
        shutil.rmtree(STUDIO_DIR, ignore_errors=True)
        OVERLAY_IMAGE.unlink(missing_ok=True)
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        ok("Removed the x86 Bambu Studio copy, library add-on and downloads")
    info(f"Your Bambu Studio settings in {X86_CONFIG_DIR} were left alone.")
    return 0


# --------------------------------------------------------------------------- #
#  Arguments
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="barmbu",
        description="Run Bambu Studio's official network plugin on ARM64 Linux.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def plugin_opts(sp):
        sp.add_argument("--region", default=DEFAULT_REGION, choices=sorted(BAMBU_API_HOSTS))
        sp.add_argument("--source", default="auto", choices=("auto", "download", "local"))
        sp.add_argument("--local-dir", help="folder with plugin .so files to use")
        sp.add_argument("--offline", action="store_true", help="never use the network")

    sp = sub.add_parser("detect", help="show what's on this computer")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("status", help="like detect; --json for scripts")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("download", help="only fetch the plugin files")
    plugin_opts(sp)
    sp.add_argument("--out", help="folder to put them in")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("install", help="set everything up")
    plugin_opts(sp)
    sp.add_argument("--appimage", help="x86_64 Bambu Studio AppImage to use")
    sp.add_argument("--download-studio", action="store_true",
                    help="download the x86_64 Bambu Studio if none is found")
    sp.add_argument("--update", action="store_true",
                    help="get the newest Bambu Studio + plugin")
    sp.add_argument("--rebuild-libs", action="store_true",
                    help="rebuild the x86 library add-on")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("check-update",
                        help="check GitHub for a newer version of this tool")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_check_update)

    sp = sub.add_parser("self-update",
                        help="download and install the newest version of this tool")
    sp.add_argument("--download-only", action="store_true")
    sp.set_defaults(func=cmd_self_update)

    sp = sub.add_parser("launch", help="start the emulated Bambu Studio")
    sp.set_defaults(func=cmd_launch)

    sp = sub.add_parser("uninstall", help="remove what this tool added")
    sp.add_argument("--all", action="store_true",
                    help="also delete the x86 Studio copy and downloads")
    sp.set_defaults(func=cmd_uninstall)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        migrate_old_name()
        return args.func(args)
    except ToolError as exc:
        err(str(exc))
        if GUI_MODE:
            print(f"@@error {str(exc).splitlines()[0]}", flush=True)
        return 1
    except KeyboardInterrupt:
        err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
