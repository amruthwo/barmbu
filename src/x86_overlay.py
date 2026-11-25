# -*- coding: utf-8 -*-
"""
x86_overlay -- add missing x86_64 libraries to FEX's root filesystem.

Asahi/Fedora ships FEX with a read-only x86_64 root filesystem image
(``/usr/share/fex-emu/RootFS/default.erofs``).  It has GTK3 and OpenGL but not
WebKitGTK, which the x86_64 Bambu Studio needs for its home/login pages.

``muvm`` can stack extra erofs images on top of that base image
(``muvm -f base.erofs -f overlay.erofs``).  This module builds such an overlay:

1. work out which shared libraries (sonames) are missing from the base image,
2. ask ``dnf`` to download the x86_64 Fedora packages that provide them (from
   Fedora's own ``fedora`` + ``updates`` repos only),
3. follow each package's own library needs until nothing is missing,
4. unpack just those packages and pack them into one erofs image.

Only standard Fedora tools are used: dnf, rpm, rpm2cpio, cpio, dump.erofs,
mkfs.erofs.  ELF dependencies are read with a small built-in parser.
"""

from __future__ import annotations

import mmap
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

LIB_DIRS = ("/usr/lib64", "/lib64", "/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu")

Log = Callable[[str], None]


# --------------------------------------------------------------------------- #
#  ELF parsing (no readelf needed)
# --------------------------------------------------------------------------- #

def elf_needed(path: Path) -> list[str]:
    """Return the DT_NEEDED sonames of a 64-bit little-endian ELF file."""
    try:
        with open(path, "rb") as fh:
            if os.fstat(fh.fileno()).st_size < 64:
                return []
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return []
    try:
        if mm[:4] != b"\x7fELF" or mm[4] != 2 or mm[5] != 1:
            return []
        e_phoff = struct.unpack_from("<Q", mm, 0x20)[0]
        e_phentsize, e_phnum = struct.unpack_from("<HH", mm, 0x36)
        loads: list[tuple[int, int, int]] = []
        dyn: Optional[tuple[int, int]] = None
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            if off + 56 > len(mm):
                break
            p_type = struct.unpack_from("<I", mm, off)[0]
            p_offset, p_vaddr = struct.unpack_from("<QQ", mm, off + 8)
            p_filesz = struct.unpack_from("<Q", mm, off + 32)[0]
            if p_type == 1:      # PT_LOAD
                loads.append((p_vaddr, p_offset, p_filesz))
            elif p_type == 2:    # PT_DYNAMIC
                dyn = (p_offset, p_filesz)
        if dyn is None:
            return []

        def v2o(vaddr: int) -> Optional[int]:
            for va, fo, sz in loads:
                if va <= vaddr < va + sz:
                    return fo + (vaddr - va)
            return None

        needed_offs: list[int] = []
        strtab = None
        doff, dsz = dyn
        for j in range(0, dsz, 16):
            if doff + j + 16 > len(mm):
                break
            tag, val = struct.unpack_from("<qQ", mm, doff + j)
            if tag == 0:
                break
            if tag == 1:
                needed_offs.append(val)
            elif tag == 5:
                strtab = val
        if strtab is None:
            return []
        base = v2o(strtab)
        if base is None:
            return []
        out = []
        for n in needed_offs:
            start = base + n
            end = mm.find(b"\0", start)
            if end > start:
                out.append(mm[start:end].decode("utf-8", "replace"))
        return out
    finally:
        mm.close()


def is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  The base image
# --------------------------------------------------------------------------- #

class ErofsImage:
    """Answers 'does this path exist?' for an erofs image, with a cache."""

    def __init__(self, image: Path, runner: Callable[..., subprocess.CompletedProcess]):
        self.image = image
        self._run = runner
        self._cache: dict[str, bool] = {}

    def has(self, path: str) -> bool:
        if path not in self._cache:
            res = self._run(["dump.erofs", f"--path={path}", str(self.image)],
                            check=False, capture=True)
            self._cache[path] = "Path :" in (res.stdout or "")
        return self._cache[path]

    def has_soname(self, soname: str) -> bool:
        return any(self.has(f"{d}/{soname}") for d in LIB_DIRS)

    def cat(self, path: str) -> str:
        res = self._run(["dump.erofs", "--cat", f"--path={path}", str(self.image)],
                        check=False, capture=True)
        return res.stdout or ""

    def fedora_release(self) -> Optional[str]:
        text = self.cat("/usr/lib/os-release") or self.cat("/etc/os-release")
        if "fedora" not in text.lower():
            return None
        m = re.search(r'^VERSION_ID="?(\d+)', text, re.MULTILINE)
        return m.group(1) if m else None


def missing_sonames(elf_files: Iterable[Path], bundled: set[str],
                    image: ErofsImage) -> list[str]:
    """Sonames needed by elf_files that are neither bundled nor in the image."""
    want: set[str] = set()
    for f in elf_files:
        for so in elf_needed(f):
            if so not in bundled:
                want.add(so)
    return sorted(so for so in want if not image.has_soname(so))


# --------------------------------------------------------------------------- #
#  Building the overlay
# --------------------------------------------------------------------------- #

_SONAME_PROV = re.compile(r"^(\S+\.so(?:\.[\w.]+)?)\(\)\(64bit\)")


def _rpm_arch_ok(name: str) -> bool:
    return name.endswith(".x86_64.rpm") or name.endswith(".noarch.rpm")


def build_overlay(missing: list[str], image: ErofsImage, out_image: Path,
                  cache_dir: Path, runner, log: Log) -> dict:
    """Build out_image providing every soname in `missing`.

    Returns a report dict: {"packages": [...], "unresolved": [...]}.
    """
    release = image.fedora_release()
    if release is None:
        raise RuntimeError("the x86 root filesystem is not Fedora-based; "
                           "automatic library overlay needs a Fedora rootfs")
    for tool in ("dnf", "rpm", "rpm2cpio", "cpio", "mkfs.erofs"):
        if runner(["sh", "-c", f"command -v {tool}"], check=False,
                  capture=True).returncode != 0:
            raise RuntimeError(f"'{tool}' is required to build the library overlay")

    rpm_dir = cache_dir / "rpms"
    root_dir = cache_dir / "dnfroot"
    staging = cache_dir / "staging"
    rpm_dir.mkdir(parents=True, exist_ok=True)
    root_dir.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    specs = [f"{so}()(64bit)" for so in missing]
    log(f"Downloading x86_64 Fedora {release} packages for: {', '.join(missing)}")
    cmd = ["dnf", "-y", "--quiet", "--forcearch=x86_64", f"--releasever={release}",
           f"--installroot={root_dir}", "--use-host-config",
           "--disablerepo=*", "--enablerepo=fedora", "--enablerepo=updates",
           "--setopt=install_weak_deps=False",
           "download", "--resolve", "--alldeps", f"--destdir={rpm_dir}", *specs]
    res = runner(cmd, check=False, capture=True)
    if res.returncode != 0:
        raise RuntimeError("dnf could not download the packages:\n"
                           + (res.stderr or res.stdout or "").strip()[-800:])

    rpms = sorted(p for p in rpm_dir.glob("*.rpm") if _rpm_arch_ok(p.name))
    if not rpms:
        raise RuntimeError("dnf downloaded no x86_64 packages")

    # soname -> rpm, from one batched rpm query.
    provider: dict[str, Path] = {}
    by_nvra = {p.name[:-4]: p for p in rpms}
    q = runner(["rpm", "-qp", "--nosignature", "--qf", "[%{=NVRA} %{PROVIDES}\\n]",
                *map(str, rpms)], check=False, capture=True)
    for line in (q.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        nvra, prov = parts
        m = _SONAME_PROV.match(prov.strip())
        if m and nvra in by_nvra:
            provider.setdefault(m.group(1), by_nvra[nvra])

    chosen: list[Path] = []
    staged_sonames: set[str] = set()
    unresolved: list[str] = []
    queue = list(missing)
    seen: set[str] = set()
    while queue:
        so = queue.pop(0)
        if so in seen:
            continue
        seen.add(so)
        if so in staged_sonames or image.has_soname(so):
            continue
        rpm = provider.get(so)
        if rpm is None:
            unresolved.append(so)
            continue
        if rpm in chosen:
            continue
        chosen.append(rpm)
        log(f"  + {rpm.name}")
        _extract_rpm(rpm, staging, runner)
        files = runner(["rpm", "-qlp", "--nosignature", str(rpm)],
                       check=False, capture=True).stdout or ""
        for fpath in files.splitlines():
            local = staging / fpath.lstrip("/")
            if local.is_file() and not local.is_symlink() and is_elf(local):
                for dep in elf_needed(local):
                    if dep not in seen:
                        queue.append(dep)
            if any(fpath.startswith(d + "/") for d in LIB_DIRS) and ".so" in fpath:
                staged_sonames.add(os.path.basename(fpath))

    if not chosen:
        raise RuntimeError("none of the downloaded packages provide the missing "
                           f"libraries ({', '.join(missing)})")

    out_image.parent.mkdir(parents=True, exist_ok=True)
    tmp_img = out_image.with_suffix(".erofs.tmp")
    log(f"Packing {len(chosen)} package(s) into {out_image.name} ...")
    # -b 4096: the microVM runs a 4K-page kernel, but mkfs.erofs on a
    # 16K-page host (Asahi) would otherwise pick 16K blocks it cannot mount.
    res = runner(["mkfs.erofs", "-b", "4096", "-zlz4hc", "--all-root",
                  str(tmp_img), str(staging)],
                 check=False, capture=True)
    if res.returncode != 0:
        raise RuntimeError("mkfs.erofs failed:\n" + (res.stderr or "").strip()[-800:])
    os.replace(tmp_img, out_image)
    # The image is all that's needed now; the RPMs + dnf metadata are ~300 MB.
    for d in (staging, rpm_dir, root_dir):
        shutil.rmtree(d, ignore_errors=True)
    return {"packages": [p.name for p in chosen], "unresolved": unresolved}


def _extract_rpm(rpm: Path, dest: Path, runner) -> None:
    res = runner(["sh", "-c",
                  'rpm2cpio "$1" | cpio -idm --quiet --no-absolute-filenames',
                  "sh", str(rpm)], check=False, capture=True, cwd=str(dest))
    if res.returncode != 0:
        raise RuntimeError(f"could not unpack {rpm.name}: {(res.stderr or '').strip()}")
