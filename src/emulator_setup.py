# -*- coding: utf-8 -*-
"""
emulator_setup -- get FEX-Emu (and an x86_64 root filesystem) onto any
ARM64 Linux, as automatically as each distro allows.

  Fedora (incl. Asahi)     dnf: fex-emu, fex-emu-rootfs-fedora, muvm on 16K pages
  Ubuntu                   FEX's official PPA (ppa:fex-emu/fex), CPU-matched package
  Debian, Raspberry Pi OS  the same PPA's Ubuntu 24.04 build (experimental)
  others                   instructions only

The root filesystem comes from the distro package (Fedora) or from FEX's own
``FEXRootFSFetcher`` (Ubuntu 24.04, the same base Bambu's AppImage is built on).

16K memory pages: FEX needs 4K pages.  Asahi solves that with muvm (a 4K-page
micro-VM).  On a Raspberry Pi 5 the simpler fix is the 4K kernel that
Raspberry Pi OS already ships (``kernel=kernel8.img`` in config.txt).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

FEX_PPA = "ppa:fex-emu/fex"
PPA_LAUNCHPAD_API = "https://api.launchpad.net/1.0/~fex-emu/+archive/ubuntu/fex"
PPA_URL = "https://ppa.launchpadcontent.net/fex-emu/fex/ubuntu"
PPA_FALLBACK_SUITE = "noble"          # Ubuntu 24.04 build, used on Debian
ROOTFS_DISTRO = ("ubuntu", "24.04")   # matches Bambu's ubu24 AppImage

PI_CONFIG_CANDIDATES = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt"))
PI_KERNEL_LINE = "kernel=kernel8.img"
PI_MARK = "# Added by barmbu: 4K-page kernel so FEX can run x86 programs"


# --------------------------------------------------------------------------- #
#  What is this machine?
# --------------------------------------------------------------------------- #

def os_release(root: Path = Path("/")) -> dict:
    """Parsed os-release of the host (works inside a Flatpak too)."""
    for p in (root / "run/host/os-release", root / "etc/os-release",
              root / "usr/lib/os-release"):
        try:
            text = p.read_text()
        except OSError:
            continue
        out = {}
        for line in text.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                out[k.strip()] = v.strip().strip('"')
        return out
    return {}


def distro_family(rel: Optional[dict] = None) -> str:
    """'fedora', 'ubuntu', 'debian', 'arch', 'suse' or 'other'."""
    rel = os_release() if rel is None else rel
    ids = {rel.get("ID", "").lower(), *rel.get("ID_LIKE", "").lower().split()}
    for fam in ("fedora", "ubuntu", "debian", "arch", "suse"):
        if fam in ids or (fam == "suse" and any("suse" in i for i in ids)):
            return fam
    if ids & {"rhel", "centos"}:
        return "fedora"
    if "raspbian" in ids:
        return "debian"
    return "other"


def cpu_features(cpuinfo: Optional[str] = None) -> set[str]:
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError:
            return set()
    m = re.search(r"^Features\s*:\s*(.*)$", cpuinfo, re.MULTILINE)
    return set(m.group(1).split()) if m else set()


def fex_ppa_package(features: Optional[set[str]] = None) -> str:
    """fex-emu-armv8.{0,2,4}: the PPA ships builds per CPU level.

    Picking too low a level only costs speed; too high would crash, so the
    checks are conservative.
    """
    f = cpu_features() if features is None else features
    if {"atomics", "asimdrdm", "fphp", "dcpop", "uscat", "ilrcpc", "flagm"} <= f:
        return "fex-emu-armv8.4"
    if {"atomics", "asimdrdm", "fphp", "dcpop"} <= f:
        return "fex-emu-armv8.2"
    return "fex-emu-armv8.0"


def raspberry_pi_model() -> Optional[str]:
    for p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            model = Path(p).read_bytes().rstrip(b"\0").decode("utf-8", "replace")
        except OSError:
            continue
        if "Raspberry Pi" in model:
            return model
    return None


def pi_config_file() -> Optional[Path]:
    for p in PI_CONFIG_CANDIDATES:
        if p.is_file() and (p.parent / "kernel8.img").is_file():
            return p
    return None


def pi_4k_kernel_configured() -> bool:
    cfg = pi_config_file()
    try:
        return bool(cfg) and PI_KERNEL_LINE in cfg.read_text()
    except OSError:
        return False


def pi_kernel_script(undo: bool = False) -> Optional[str]:
    """Root shell script that adds (or removes) the 4K-kernel line."""
    cfg = pi_config_file()
    if cfg is None:
        return None
    if undo:
        return (f"set -e\ncp '{cfg}' '{cfg}.barmbu-backup'\n"
                f"sed -i '/^{re.escape(PI_MARK)}$/,/^{PI_KERNEL_LINE}$/d' '{cfg}'\n")
    # [all] makes sure the line isn't swallowed by an earlier [pi4]/[cm5] filter.
    return (f"set -e\ngrep -qx '{PI_KERNEL_LINE}' '{cfg}' && exit 0\n"
            f"cp '{cfg}' '{cfg}.barmbu-backup'\n"
            f"printf '\\n%s\\n[all]\\n%s\\n' '{PI_MARK}' '{PI_KERNEL_LINE}' >> '{cfg}'\n")


# --------------------------------------------------------------------------- #
#  FEX's own configuration (which root filesystem it uses)
# --------------------------------------------------------------------------- #

def fex_home() -> Path:
    return Path(os.environ.get("FEX_APP_DATA_LOCATION")
                or Path.home() / ".fex-emu")


def fex_configured_rootfs() -> Optional[Path]:
    """The RootFS FEX will use when run directly (not under muvm)."""
    base = fex_home()
    try:
        cfg = json.loads((base / "Config.json").read_text())
        name = (cfg.get("Config") or {}).get("RootFS")
    except (OSError, ValueError, AttributeError):
        name = None
    if name:
        p = Path(name)
        p = p if p.is_absolute() else base / "RootFS" / name
        if p.exists():
            return p
    rd = base / "RootFS"
    if rd.is_dir():
        entries = sorted(e for e in rd.iterdir() if not e.name.startswith("."))
        dirs = [e for e in entries if e.is_dir()]
        if dirs or entries:
            return (dirs or entries)[0]
    return None


def rootfs_fetch_command() -> list[str]:
    name, ver = ROOTFS_DISTRO
    return ["FEXRootFSFetcher", "-y", "-x",
            f"--distro-name={name}", f"--distro-version={ver}"]


# --------------------------------------------------------------------------- #
#  Install plans
# --------------------------------------------------------------------------- #

def install_plan(family: str, needs_muvm: bool, have_dnf: bool, have_apt: bool,
                 ppa_key: Optional[str] = None,
                 features: Optional[set[str]] = None) -> dict:
    """How to get FEX here: {'auto': bool, 'script': str, 'summary': str,
    'manual': str}. 'script' runs as root (through pkexec)."""
    if family == "fedora" and have_dnf:
        # muvm on every Fedora: needed for 16K pages, and on 4K hosts it's what
        # lets the library add-on be stacked onto the read-only system image.
        pkgs = ["fex-emu", "fex-emu-rootfs-fedora", "muvm", "erofs-utils",
                "squashfs-tools"]
        return {"auto": True, "summary": "dnf install " + " ".join(pkgs),
                "script": "set -e\ndnf install -y " + " ".join(pkgs) + "\n"}
    pkg = fex_ppa_package(features)
    if family == "ubuntu" and have_apt:
        return {"auto": True,
                "summary": f"add {FEX_PPA} and install {pkg}",
                "script": ("set -e\nexport DEBIAN_FRONTEND=noninteractive\n"
                           "apt-get update\n"
                           "apt-get install -y software-properties-common squashfs-tools\n"
                           f"add-apt-repository -y {FEX_PPA}\n"
                           "apt-get update\n"
                           f"apt-get install -y {pkg}\n")}
    if family == "debian" and have_apt and ppa_key:
        keyring = "/etc/apt/keyrings/fex-emu-ppa.asc"
        line = f"deb [signed-by={keyring}] {PPA_URL} {PPA_FALLBACK_SUITE} main"
        return {"auto": True, "experimental": True,
                "summary": f"add FEX's Ubuntu 24.04 packages and install {pkg} "
                           "(experimental on Debian)",
                "script": ("set -e\nexport DEBIAN_FRONTEND=noninteractive\n"
                           "install -d -m 0755 /etc/apt/keyrings\n"
                           f"install -m 0644 '{ppa_key}' {keyring}\n"
                           f"echo '{line}' > /etc/apt/sources.list.d/fex-emu-ppa.list\n"
                           "apt-get update\n"
                           f"apt-get install -y squashfs-tools {pkg}\n")}
    manual = {
        "arch": "Install 'fex-emu' from the AUR (for example: yay -S fex-emu).",
        "suse": "Install FEX-Emu with zypper if your release has it, or build it: "
                "https://wiki.fex-emu.com/",
    }.get(family, "Install FEX-Emu for your distro: https://fex-emu.com/")
    return {"auto": False, "summary": "", "script": "", "manual": manual}
