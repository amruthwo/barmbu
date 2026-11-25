#!/usr/bin/env python3
"""Offline regression tests for barmbu (stdlib unittest only)."""

import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import barmbu as m  # noqa: E402
import x86_overlay as xo  # noqa: E402


def fake_elf(path: Path, e_machine: int = 0x3E) -> Path:
    data = bytearray(64)
    data[0:4] = b"\x7fELF"
    data[4], data[5] = 2, 1
    data[18:20] = struct.pack("<H", e_machine)
    path.write_bytes(bytes(data))
    return path


class VersionTests(unittest.TestCase):
    def test_flatpak_display_version(self):
        self.assertEqual(m.plugin_query_version("2.9.2 Public Release"), "02.09.02.00")

    def test_padded_version(self):
        self.assertEqual(m.plugin_query_version("02.09.02.62"), "02.09.02.00")

    def test_none_falls_back(self):
        self.assertRegex(m.plugin_query_version(None), r"^\d{2}\.\d{2}\.\d{2}\.00$")

    def test_version_from_appimage_name(self):
        self.assertEqual(
            m._version_from_name("BambuStudio_ubu24-v02.09.02.62-20261124164959.AppImage"),
            "02.09.02.62")


class ElfTests(unittest.TestCase):
    def test_arches(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(m.elf_arch(fake_elf(Path(d) / "a", 0x3E)), "x86_64")
            self.assertEqual(m.elf_arch(fake_elf(Path(d) / "b", 0xB7)), "aarch64")
            (Path(d) / "c").write_bytes(b"nope")
            self.assertIsNone(m.elf_arch(Path(d) / "c"))

    def test_elf_needed_on_python(self):
        # The running Python is a dynamically linked ELF on Linux.
        needed = xo.elf_needed(Path(sys.executable).resolve())
        self.assertIsInstance(needed, list)
        if needed:
            self.assertTrue(any(n.startswith("libc.so") or n.startswith("libpython")
                                for n in needed))

    def test_elf_needed_non_elf(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x"
            p.write_bytes(b"hello world" * 10)
            self.assertEqual(xo.elf_needed(p), [])


class AppImageTests(unittest.TestCase):
    def test_squashfs_offset(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "t.AppImage"
            head = bytearray(64)
            head[0:4] = b"\x7fELF"
            head[4], head[5] = 2, 1
            struct.pack_into("<Q", head, 0x28, 100)      # e_shoff
            struct.pack_into("<HH", head, 0x3A, 64, 2)   # e_shentsize, e_shnum
            body = bytes(head) + b"\0" * (228 - 64) + b"hsqs" + b"\0" * 16
            img.write_bytes(body)
            self.assertEqual(m.appimage_squashfs_offset(img), 228)

    def test_not_appimage(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "t.AppImage"
            img.write_bytes(b"text" * 20)
            with self.assertRaises(m.ToolError):
                m.appimage_squashfs_offset(img)


class PackageTests(unittest.TestCase):
    def test_extract_sos_from_zip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pkg = d / "linux_x.zip"
            elf = bytearray(20)
            elf[0:4] = b"\x7fELF"
            elf[18] = 0x3E
            with zipfile.ZipFile(pkg, "w") as zf:
                zf.writestr("libbambu_networking.so", bytes(elf))
                zf.writestr("readme.txt", "hello")
            out = m.extract_sos_from_package(pkg, d / "unpacked")
            self.assertEqual([p.name for p in out], ["libbambu_networking.so"])

    def test_collect_local_sos_filters_arch(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            fake_elf(d / "good.so", 0x3E)
            fake_elf(d / "bad.so", 0xB7)
            (d / "x.so").write_bytes(b"nope")
            self.assertEqual([p.name for p in m.collect_local_sos(d)], ["good.so"])


    def test_managed_studio_survives_appimage_deletion(self):
        with tempfile.TemporaryDirectory() as d:
            old, m.STUDIO_DIR = m.STUDIO_DIR, Path(d)
            try:
                name = "BambuStudio_ubu24-v02.09.02.62.AppImage"
                run = Path(d) / "BambuStudio_ubu24-v02.09.02.62.extracted/squashfs-root"
                run.mkdir(parents=True)
                (run / "AppRun").write_text("")
                (Path(d) / "half.extracted").mkdir()      # no AppRun: ignored
                self.assertEqual([p.name for p in m.managed_studios()], [name])
            finally:
                m.STUDIO_DIR = old

class ToolUpdateTests(unittest.TestCase):
    def _check(self, tag):
        old_repo, old_get = m.TOOL_REPO, m.http_get
        m.TOOL_REPO = "someone/barmbu"
        m.http_get = lambda *a, **k: ('{"tag_name": "%s", "html_url": "u", "assets": []}' % tag).encode()
        try:
            return m.tool_update()
        finally:
            m.TOOL_REPO, m.http_get = old_repo, old_get

    def test_newer(self):
        upd = self._check("v99.0.0")
        self.assertEqual((upd["version"], upd["url"], upd["asset"]), ("99.0.0", "u", None))

    def test_same_or_older(self):
        self.assertIsNone(self._check("v" + m.__version__))
        self.assertIsNone(self._check("v0.1.0"))

    def test_pick_asset_per_install_kind(self):
        names = ["barmbu-2.1.0.tar.gz",
                 "barmbu-2.1.0-1.fc45.noarch.rpm",
                 "barmbu_2.1.0_all.deb",
                 "barmbu-v2.1.0-aarch64.flatpak"]
        assets = [{"name": n, "browser_download_url": "x/" + n} for n in names]
        pick = lambda k: m.pick_release_asset(assets, k, "aarch64")["name"]
        self.assertEqual(pick("source"), names[0])
        self.assertEqual(pick("rpm"), names[1])
        self.assertEqual(pick("deb"), names[2])
        self.assertEqual(pick("flatpak"), names[3])
        self.assertIsNone(m.pick_release_asset(assets, "flatpak", "x86_64"))

    def test_digest_mismatch_deletes_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "pkg"
            f.write_bytes(b"hello")
            m._verify_digest(f, "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e"
                                "1b161e5c1fa7425e73043362938b9824")
            with self.assertRaises(m.ToolError):
                m._verify_digest(f, "sha256:" + "0" * 64)
            self.assertFalse(f.exists())

    def test_opt_out_disables_check(self):
        import os
        os.environ["BARMBU_NO_UPDATE_CHECK"] = "1"
        try:
            self.assertIsNone(m.tool_update())
        finally:
            del os.environ["BARMBU_NO_UPDATE_CHECK"]

class MigrationTests(unittest.TestCase):
    def test_old_name_moved_and_paths_fixed(self):
        with tempfile.TemporaryDirectory() as d:
            saved = {k: getattr(m, k) for k in
                     ("home", "APP_DATA", "STATE_FILE", "LAUNCHER", "DESKTOP_FILE")}
            try:
                h = Path(d)
                m.home = lambda: h
                m.APP_DATA = h / ".local/share/barmbu"
                m.STATE_FILE = m.APP_DATA / "state.json"
                m.LAUNCHER = m.APP_DATA / "launch-bambu-studio.sh"
                m.DESKTOP_FILE = h / "studio.desktop"
                old = h / ".local/share/bambu-arm64-plugin"
                old.mkdir(parents=True)
                (old / "launch-bambu-studio.sh").write_text(f"exec {old}/studio/AppRun")
                m.DESKTOP_FILE.write_text(f"Exec={old}/launch-bambu-studio.sh")
                m.migrate_old_name()
                self.assertFalse(old.exists())
                self.assertIn(str(m.APP_DATA), m.LAUNCHER.read_text())
                self.assertNotIn("bambu-arm64-plugin", m.DESKTOP_FILE.read_text())
            finally:
                for k, v in saved.items():
                    setattr(m, k, v)

class LauncherTests(unittest.TestCase):
    def _emu(self, page):
        return m.Emulator(fex="/usr/bin/FEXBash", muvm="/usr/bin/muvm", page_size=page,
                          base_image="/usr/share/fex-emu/RootFS/default.erofs",
                          system_overlays=[], user_rootfs=None)

    def test_muvm_on_16k_pages(self):
        cmd = m.launch_command(self._emu(16384), Path("/x/AppRun"), Path("/o.erofs"))
        self.assertEqual(cmd[0], "muvm")
        # base image must come before the overlay
        self.assertLess(cmd.index("/usr/share/fex-emu/RootFS/default.erofs"),
                        cmd.index("/o.erofs"))
        self.assertEqual(cmd[-2:], ["--", "/x/AppRun"])
        self.assertIn("GDK_BACKEND=x11", cmd)

    def test_plain_fex_on_4k_pages(self):
        cmd = m.launch_command(self._emu(4096), Path("/x/AppRun"), None)
        self.assertIn("FEXBash", cmd)
        self.assertNotIn("muvm", cmd)

    def test_emulator_readiness(self):
        e = self._emu(16384)
        self.assertTrue(e.ready)
        e.muvm = None
        self.assertFalse(e.ready)
        self.assertIn("muvm", e.problem())


class CliTests(unittest.TestCase):
    def test_parser(self):
        p = m.build_parser()
        self.assertEqual(p.parse_args(["detect"]).command, "detect")
        a = p.parse_args(["install", "--download-studio", "--update"])
        self.assertTrue(a.download_studio and a.update)
        self.assertTrue(p.parse_args(["uninstall", "--all"]).all)


if __name__ == "__main__":
    unittest.main(verbosity=2)
