# -*- coding: utf-8 -*-
"""
deb_addon -- add missing x86_64 libraries from Ubuntu's archive, on any distro.

The Fedora path (x86_overlay.build_overlay) needs ``dnf`` on the host.  This
one needs nothing but Python: it reads Ubuntu's package index, downloads the
amd64 ``.deb`` files that provide the missing libraries (checking each one's
SHA-256 against the index), unpacks them with a tiny ``ar`` reader and
follows each library's own needs until nothing is missing.

Used with an Ubuntu x86_64 root filesystem (what FEXRootFSFetcher gives on
Debian, Ubuntu, Raspberry Pi OS ...), which matches the Ubuntu 24.04 build of
Bambu Studio.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Callable, Optional

from x86_overlay import LIB_DIRS, elf_needed, is_elf

MIRROR = "https://archive.ubuntu.com/ubuntu"
COMPONENTS = ("main", "universe")
MAX_PROBES = 80            # bound the fallback search for oddly named packages

Fetch = Callable[[str], bytes]
Log = Callable[[str], None]


# --------------------------------------------------------------------------- #
#  The package index
# --------------------------------------------------------------------------- #

def parse_packages(text: str) -> dict[str, dict]:
    """Debian 'Packages' file -> {name: {field: value}}."""
    out: dict[str, dict] = {}
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        key = None
        for line in stanza.splitlines():
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line.strip()
            elif ":" in line:
                key, _, val = line.partition(":")
                fields[key] = val.strip()
        name = fields.get("Package")
        if name:
            out[name] = fields
    return out


def dep_names(field: str) -> list[str]:
    """'libc6 (>= 2.34), libfoo1 | libbar1' -> ['libc6', 'libfoo1', 'libbar1']."""
    names = []
    for alt in re.split(r"[,|]", field or ""):
        n = alt.strip().split(" ", 1)[0].split(":", 1)[0]
        if n:
            names.append(n)
    return names


def soname_package_guesses(soname: str) -> list[str]:
    """Debian policy names: libfoo.so.1 -> libfoo1, libfoo-4.1.so.0 -> libfoo-4.1-0
    (and the 't64' renames from Ubuntu 24.04's time_t transition)."""
    m = re.match(r"^(lib.+?)\.so\.(\d+)", soname)
    if not m:
        return []
    base, ver = m.group(1).lower().replace("_", "-"), m.group(2)
    name = f"{base}-{ver}" if base[-1].isdigit() else f"{base}{ver}"
    return [name, name + "t64"]


class UbuntuIndex:
    def __init__(self, suite: str, fetch: Fetch, cache_dir: Path, log: Log):
        self.suite, self.fetch, self.log = suite, fetch, log
        self.cache_dir = cache_dir
        self.packages: dict[str, dict] = {}

    def load(self) -> "UbuntuIndex":
        # Later suites win: release < updates < security.
        for suite in (self.suite, f"{self.suite}-updates", f"{self.suite}-security"):
            for comp in COMPONENTS:
                url = f"{MIRROR}/dists/{suite}/{comp}/binary-amd64/Packages.xz"
                cached = self.cache_dir / f"{suite}_{comp}_Packages.xz"
                if not cached.is_file():
                    self.log(f"Reading Ubuntu package list {suite}/{comp}")
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(self.fetch(url))
                text = lzma.decompress(cached.read_bytes()).decode("utf-8", "replace")
                self.packages.update(parse_packages(text))
        return self


# --------------------------------------------------------------------------- #
#  Unpacking a .deb without dpkg
# --------------------------------------------------------------------------- #

def _ar_members(data: bytes):
    if data[:8] != b"!<arch>\n":
        raise ValueError("not a .deb (ar) archive")
    pos = 8
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name = hdr[:16].decode().strip().rstrip("/")
        size = int(hdr[48:58].decode().strip())
        yield name, data[pos + 60:pos + 60 + size]
        pos += 60 + size + (size & 1)


def _open_data_tar(name: str, blob: bytes) -> tarfile.TarFile:
    if name.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
            blob = zstd.decompress(blob)
        except ImportError:
            res = subprocess.run(["zstd", "-dc"], input=blob, capture_output=True)
            if res.returncode != 0:
                raise RuntimeError("Unpacking Ubuntu packages needs Python 3.14+ "
                                   "or the 'zstd' command (install zstd)")
            blob = res.stdout
        return tarfile.open(fileobj=io.BytesIO(blob), mode="r:")
    if name.endswith(".xz"):
        return tarfile.open(fileobj=io.BytesIO(lzma.decompress(blob)), mode="r:")
    if name.endswith(".gz"):
        return tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:")
    return tarfile.open(fileobj=io.BytesIO(blob), mode="r:")


def extract_deb(deb: bytes, dest: Path) -> list[str]:
    """Unpack a .deb's files into dest; returns their paths ('/usr/lib/...')."""
    for name, blob in _ar_members(deb):
        if name.startswith("data.tar"):
            with _open_data_tar(name, blob) as tf:
                members = tf.getmembers()
                tf.extractall(dest, filter="tar")
            return ["/" + m.name.lstrip("./") for m in members if not m.isdir()]
    raise ValueError("no data.tar in .deb")


# --------------------------------------------------------------------------- #
#  Building the add-on
# --------------------------------------------------------------------------- #

def build_addon(missing: list[str], rootfs, suite: str, staging: Path,
                cache_dir: Path, fetch: Fetch, log: Log) -> dict:
    """Stage the files that provide `missing` (and what they need) in staging.

    rootfs answers has_soname(); returns {'packages': [...], 'unresolved': [...]}.
    """
    index = UbuntuIndex(suite, fetch, cache_dir / "index", log).load()
    pkgs = index.packages
    unpack_root = cache_dir / "unpacked"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    inspected: dict[str, list[str]] = {}      # package -> its file list

    def inspect(name: str) -> list[str]:
        if name in inspected:
            return inspected[name]
        info = pkgs[name]
        deb = fetch(f"{MIRROR}/{info['Filename']}")
        want = info.get("SHA256", "")
        if want and hashlib.sha256(deb).hexdigest() != want:
            raise RuntimeError(f"checksum mismatch for {name}")
        files = extract_deb(deb, unpack_root / name)
        inspected[name] = files
        return files

    def provides(files: list[str], soname: str) -> bool:
        return any(f == f"{d}/{soname}" for f in files for d in LIB_DIRS)

    chosen: list[str] = []
    staged: set[str] = set()
    unresolved: list[str] = []
    queue, seen = list(missing), set()
    probes = 0
    while queue:
        so = queue.pop(0)
        if so in seen:
            continue
        seen.add(so)
        if so in staged or rootfs.has_soname(so):
            continue
        provider = None
        for guess in soname_package_guesses(so):
            if guess in pkgs and provides(inspect(guess), so):
                provider = guess
                break
        if provider is None:
            # Fall back to the dependencies of what we already picked.
            for parent in list(chosen):
                for dep in dep_names(pkgs[parent].get("Depends", "")):
                    if dep in pkgs and dep.startswith("lib") and probes < MAX_PROBES:
                        if dep not in inspected:
                            probes += 1
                        if provides(inspect(dep), so):
                            provider = dep
                            break
                if provider:
                    break
        if provider is None:
            unresolved.append(so)
            continue
        if provider in chosen:
            continue
        chosen.append(provider)
        log(f"  + {provider} {pkgs[provider].get('Version', '')}")
        src = unpack_root / provider
        shutil.copytree(src, staging, symlinks=True, dirs_exist_ok=True)
        for f in inspected[provider]:
            local = src / f.lstrip("/")
            if local.is_file() and not local.is_symlink() and is_elf(local):
                queue.extend(d for d in elf_needed(local) if d not in seen)
            if any(f.startswith(d + "/") for d in LIB_DIRS) and ".so" in f:
                staged.add(os.path.basename(f))

    shutil.rmtree(unpack_root, ignore_errors=True)
    if not chosen:
        raise RuntimeError("no Ubuntu package provides the missing libraries "
                           f"({', '.join(missing)})")
    return {"packages": chosen, "unresolved": unresolved}


def merge_into_dir(staging: Path, rootfs_dir: Path) -> list[str]:
    """Copy staged files into an unpacked root filesystem, never overwriting.

    Returns the paths added (so they can be removed again)."""
    added = []
    for dirpath, dirnames, filenames in os.walk(staging):
        rel = Path(dirpath).relative_to(staging)
        (rootfs_dir / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames + [d for d in dirnames
                                 if (Path(dirpath) / d).is_symlink()]:
            src, dst = Path(dirpath) / name, rootfs_dir / rel / name
            if os.path.lexists(dst):
                continue
            if src.is_symlink():
                os.symlink(os.readlink(src), dst)
            else:
                shutil.copy2(src, dst)
            added.append(str(dst))
    return added
