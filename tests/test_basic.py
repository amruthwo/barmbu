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
import deb_addon as da  # noqa: E402
import emulator_setup as es  # noqa: E402


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
        e = self._emu(4096)
        e.muvm = None                  # no muvm: FEX runs directly
        cmd = m.launch_command(e, Path("/x/AppRun"), None)
        self.assertIn("FEXBash", cmd)
        self.assertNotIn("muvm", cmd)

    def test_emulator_readiness(self):
        e = self._emu(16384)
        self.assertTrue(e.ready)
        e.muvm = None
        self.assertFalse(e.ready)
        self.assertIn("muvm", e.problem())


    def test_pi_16k_problem_mentions_kernel(self):
        e = self._emu(16384)
        e.muvm, e.pi_model = None, "Raspberry Pi 5 Model B Rev 1.0"
        self.assertIn("16K-page kernel", e.problem())
        self.assertEqual(m.emulator_fix(e), "barmbu pi-4k-kernel   (then restart)")
        e.pi_4k_pending = True
        self.assertEqual(m.emulator_fix(e), "restart your Pi")

    def test_ca_env_follows_rootfs(self):
        class Fake:
            def __init__(self, paths): self.paths = paths
            def has(self, p): return p in self.paths
        fedora = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
        self.assertEqual(m.ca_env(Fake({fedora}))["SSL_CERT_FILE"] in
                         (fedora, "/etc/ssl/certs/ca-certificates.crt"), True)
        env = m.ca_env(Fake({"/etc/ssl/certs/ca-certificates.crt"}))
        self.assertEqual(env["SSL_CERT_FILE"], "/etc/ssl/certs/ca-certificates.crt")


class EmulatorSetupTests(unittest.TestCase):
    def test_distro_family(self):
        f = es.distro_family
        self.assertEqual(f({"ID": "fedora"}), "fedora")
        self.assertEqual(f({"ID": "fedora-asahi-remix", "ID_LIKE": "fedora"}), "fedora")
        self.assertEqual(f({"ID": "ubuntu", "ID_LIKE": "debian"}), "ubuntu")
        self.assertEqual(f({"ID": "debian"}), "debian")
        self.assertEqual(f({"ID": "raspbian", "ID_LIKE": "debian"}), "debian")
        self.assertEqual(f({"ID": "arch"}), "arch")
        self.assertEqual(f({"ID": "opensuse-tumbleweed", "ID_LIKE": "opensuse suse"}), "suse")

    def test_cpu_level(self):
        pi5 = "Features : fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp"
        self.assertEqual(es.fex_ppa_package(es.cpu_features(pi5)), "fex-emu-armv8.2")
        self.assertEqual(es.fex_ppa_package({"fp", "asimd"}), "fex-emu-armv8.0")
        m1 = set(pi5.split(":")[1].split()) | {"uscat", "ilrcpc", "flagm"}
        self.assertEqual(es.fex_ppa_package(m1), "fex-emu-armv8.4")

    def test_install_plans(self):
        fed = es.install_plan("fedora", True, True, False)
        self.assertTrue(fed["auto"])
        self.assertIn("muvm", fed["script"])
        ub = es.install_plan("ubuntu", False, False, True, features={"fp"})
        self.assertIn("ppa:fex-emu/fex", ub["script"])
        self.assertIn("fex-emu-armv8.0", ub["script"])
        deb = es.install_plan("debian", False, False, True, ppa_key="/k.asc", features={"fp"})
        self.assertTrue(deb["experimental"])
        self.assertIn("noble main", deb["script"])
        self.assertFalse(es.install_plan("arch", False, False, False)["auto"])

    def test_pi_kernel_script(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.txt"
            cfg.write_text("[pi4]\narm_boost=1\n")
            (Path(d) / "kernel8.img").write_bytes(b"")
            old = es.PI_CONFIG_CANDIDATES
            es.PI_CONFIG_CANDIDATES = (cfg,)
            try:
                import subprocess
                subprocess.run(["sh", "-c", es.pi_kernel_script()], check=True)
                text = cfg.read_text()
                self.assertTrue(text.endswith("[all]\nkernel=kernel8.img\n"))
                subprocess.run(["sh", "-c", es.pi_kernel_script()], check=True)
                self.assertEqual(cfg.read_text().count("kernel=kernel8.img"), 1)
                subprocess.run(["sh", "-c", es.pi_kernel_script(undo=True)], check=True)
                self.assertNotIn("kernel8", cfg.read_text())
            finally:
                es.PI_CONFIG_CANDIDATES = old


class DebAddonTests(unittest.TestCase):
    def test_soname_guesses(self):
        g = da.soname_package_guesses
        self.assertEqual(g("libwebkit2gtk-4.1.so.0")[0], "libwebkit2gtk-4.1-0")
        self.assertEqual(g("libwayland-server.so.0")[0], "libwayland-server0")
        self.assertEqual(g("not-a-lib"), [])

    def test_parse_packages_and_deps(self):
        idx = da.parse_packages("Package: a\nVersion: 1\nDepends: b (>= 2), c | d\n\n"
                                "Package: b\nDescription: x\n more\n")
        self.assertEqual(idx["a"]["Version"], "1")
        self.assertEqual(da.dep_names(idx["a"]["Depends"]), ["b", "c", "d"])
        self.assertIn("more", idx["b"]["Description"])

    def test_extract_deb(self):
        import io as _io, tarfile as _tf, gzip as _gz
        buf = _io.BytesIO()
        with _tf.open(fileobj=buf, mode="w") as tf:
            data = b"\x7fELF"
            ti = _tf.TarInfo("./usr/lib/x86_64-linux-gnu/libz.so.1")
            ti.size = len(data)
            tf.addfile(ti, _io.BytesIO(data))
        member = _gz.compress(buf.getvalue())
        def ar(name, blob):
            hdr = f"{name:<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(blob):<10}`\n".encode()
            return hdr + blob + (b"\n" if len(blob) % 2 else b"")
        deb = b"!<arch>\n" + ar("debian-binary", b"2.0\n") + ar("data.tar.gz", member)
        with tempfile.TemporaryDirectory() as d:
            files = da.extract_deb(deb, Path(d))
            self.assertEqual(files, ["/usr/lib/x86_64-linux-gnu/libz.so.1"])
            self.assertTrue((Path(d) / "usr/lib/x86_64-linux-gnu/libz.so.1").is_file())

    def test_merge_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            st, root = Path(d) / "st", Path(d) / "root"
            (st / "usr/lib").mkdir(parents=True)
            (root / "usr/lib").mkdir(parents=True)
            (st / "usr/lib/new.so").write_text("new")
            (st / "usr/lib/old.so").write_text("new")
            (root / "usr/lib/old.so").write_text("old")
            added = da.merge_into_dir(st, root)
            self.assertEqual([Path(a).name for a in added], ["new.so"])
            self.assertEqual((root / "usr/lib/old.so").read_text(), "old")


class CliTests(unittest.TestCase):
    def test_parser(self):
        p = m.build_parser()
        self.assertEqual(p.parse_args(["detect"]).command, "detect")
        a = p.parse_args(["install", "--download-studio", "--update"])
        self.assertTrue(a.download_studio and a.update)
        self.assertTrue(p.parse_args(["uninstall", "--all"]).all)


if __name__ == "__main__":
    unittest.main(verbosity=2)
