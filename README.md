# B***ARM***bu Studio Helper

[![CI](https://github.com/amruthwo/barmbu/actions/workflows/ci.yml/badge.svg)](https://github.com/amruthwo/barmbu/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/amruthwo/barmbu)](https://github.com/amruthwo/barmbu/releases/latest)

> Unofficial community tool. Not affiliated with or endorsed by Bambu Lab.
> It downloads Bambu's own software from Bambu's servers at set-up time; no
> Bambu files are included in this repository.

**Get Bambu Lab's network plugin working on 64-bit ARM Linux**, for example an
Apple Silicon MacBook running Asahi / Fedora. Open the app, press **Set Up**,
then open Bambu Studio from your app menu.

---

## Why a second Bambu Studio?

Bambu Studio talks to your printer through a closed-source **network plugin**
(`libbambu_networking.so` and a few companion files). Bambu Lab only builds
it for regular **x86_64** PCs, and an ARM program can't load x86 code. Your
Flatpak Bambu Studio on ARM is an ARM program, so the plugin can never load
inside it, and x86 machine code can't be "patched" into ARM code.

So on ARM the app sets up a **separate x86_64 copy of Bambu Studio** that runs
under emulation, with the real plugin inside it:

| Piece | What it is | Size |
|---|---|---|
| FEX-Emu + muvm | Asahi's x86 emulator. On Apple Silicon (16K memory pages) FEX runs inside muvm, a tiny 4K-page virtual machine. | from your distro |
| Bambu Studio, x86 copy | Bambu Lab's official Linux AppImage, unpacked. A full second Studio. | ~180 MB download |
| Extra x86 libraries | WebKitGTK and friends from **Fedora's own x86_64 packages**, packed into an add-on image for the emulator. | ~90 MB |
| Network plugin | Downloaded from Bambu Lab's server, the same file Studio fetches. | ~25 MB |

Your existing Bambu Studio is not changed. The first time, your printers,
filaments and settings are copied over from the Flatpak copy.

The plugin is Bambu's own, unmodified, and signs in with its own built-in
credentials. Nothing is cracked, re-signed or faked.

On a normal x86_64 PC the app just drops the plugin into your Bambu Studio.

---

## Install

Download one package from the [Releases page](https://github.com/amruthwo/barmbu/releases/latest)
(or build them yourself with `make`, see below):

```bash
sudo dnf install ./barmbu-*.noarch.rpm        # Fedora / Asahi
sudo apt install ./barmbu_*_all.deb            # Debian / Ubuntu
flatpak install --user barmbu-*.flatpak        # any distro (aarch64)
```

Then open **bARMbu Studio Helper** from your app menu.

If the emulator isn't installed yet, the app shows an **Install** button
(it asks for your password). On Asahi that installs `fex-emu`, `muvm` and
`fex-emu-rootfs-fedora`.

---

## Using the app

1. Press **Set Up**. It downloads Bambu Studio and the plugin, adds the
   libraries, and creates the menu entry. Open **Details** to watch each step.
2. Press **Open Bambu Studio**, or pick **"Bambu Studio (x86, with network
   plugin)"** from your app menu. The first start under emulation takes a
   minute.
3. Later, **Check for Updates** fetches a newer Bambu Studio and matching
   plugin when Bambu releases one.
4. The ⋮ menu has **Remove Everything…** (your settings are kept).

---

## Command line

The window is a front-end for the `barmbu` command, which does
the same things:

```bash
barmbu detect                     # what's on this computer
barmbu install --download-studio  # set everything up
barmbu install --update           # newest Studio + plugin
barmbu launch                     # start the x86 Studio
barmbu status --json              # machine-readable status
barmbu uninstall [--all]          # remove (--all: also the x86 Studio)
barmbu check-update               # newer version of this tool on GitHub?
barmbu self-update                # download + install it (right package type)
```

Options for `install`: `--appimage FILE` (use an AppImage you already have),
`--local-dir DIR` (use plugin `.so` files you already have), `--offline`,
`--region US|CN`, `--rebuild-libs`.

Where things go:

* `~/.local/share/barmbu/`: the x86 Studio, the library add-on
  image and the launcher
* `~/.config/BambuStudio/`: the x86 Studio's settings, with the plugin in
  `plugins/`
* `~/.cache/barmbu/`: temporary downloads, cleared after a
  successful set-up

Disk space: about 620 MB after set-up (unpacked Studio ~520 MB, library
add-on ~90 MB, plugin ~60 MB). The downloaded AppImage is deleted once unpacked.

---

## Building the packages

```bash
make            # checks, then .deb, source tarball and .rpm
make test       # checks + unit tests
make flatpak    # Flatpak bundle (needs flatpak-builder; uses GNOME 51 runtime)
```

The `.rpm` uses `rpmbuild` if installed. If it isn't, it builds in a
throwaway Fedora container through rootless `podman`, so nothing extra is
installed on your machine. All packages land in `dist/`.

### Releasing

CI (`.github/workflows/ci.yml`) runs the tests on every push. To publish a
release, bump `__version__` in `src/barmbu.py` (and the
`<releases>` entry in the metainfo), then:

```bash
git tag v2.1.0 && git push origin v2.1.0
```

CI builds the .deb, .rpm, tarball and aarch64 Flatpak and attaches them to a
GitHub Release. The app checks that latest release on start-up. It detects
whether it was installed as an rpm, deb or Flatpak, downloads the matching
file, verifies its SHA-256 against GitHub's published digest, and installs it
(rpm/deb ask for your password). Source installs get a link instead.
Set `BARMBU_NO_UPDATE_CHECK=1` to turn the check off.

Source layout: `src/barmbu.py` (the tool), `src/x86_overlay.py`
(the library add-on builder), `src/barmbu_gui.py` (the window), `data/`
(menu entry, icon, app-store description), `packaging/` (deb/rpm/flatpak),
`tests/`.

---

## What's been checked, and what hasn't

Checked on an M1 MacBook Air running Fedora Asahi 45:

* the x86 Studio (02.09.02.62) starts under muvm + FEX with the library
  add-on, and all 165 of its libraries resolve;
* Bambu's server hands out the plugin without a login, and with the plugin
  in place Studio starts the plugin's own network log and certificate store.

Not yet checked: how smooth the 3D view is under emulation, and actually
connecting to and printing on a printer. If the 3D view misbehaves, that's
the emulator's graphics path, not the plugin.

The automatic library add-on needs a Fedora-based emulator root filesystem
and `dnf` (true on Asahi). On other distros, install the x86_64 WebKitGTK 4.1
libraries into your FEX root filesystem yourself.

---

## Uninstall

In the app: ⋮ → **Remove Everything…**. Or `barmbu uninstall --all`.
Your Bambu Studio settings in `~/.config/BambuStudio` are left alone.
