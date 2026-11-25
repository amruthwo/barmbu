#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BARMbu Studio Helper -- a one-button front-end for barmbu.

The window shows a short checklist of what's needed and one big button.
All the real work is done by barmbu.py, which this window runs
in the background and follows line by line (so the command line and the app
always behave the same).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import barmbu as core  # noqa: E402

APP_ID = "io.github.amruthwo.Barmbu"
CORE_SCRIPT = HERE / "barmbu.py"

ICON_OK = "emblem-ok-symbolic"
ICON_TODO = "content-loading-symbolic"
ICON_WARN = "dialog-warning-symbolic"
ICON_INFO = "dialog-information-symbolic"


class StatusRow(Adw.ActionRow):
    """A checklist row: status icon, title, explanation."""

    def __init__(self, title: str):
        super().__init__(title=title)
        self.set_subtitle_lines(3)
        self.icon = Gtk.Image(pixel_size=20)
        self.add_prefix(self.icon)

    def show(self, state: str, subtitle: str) -> None:
        self.set_subtitle(GLib.markup_escape_text(subtitle))
        name = {"ok": ICON_OK, "todo": ICON_TODO, "warn": ICON_WARN}.get(state, ICON_INFO)
        self.icon.set_from_icon_name(name)
        for cls in ("success", "warning", "dim-label"):
            self.icon.remove_css_class(cls)
        self.icon.add_css_class({"ok": "success", "warn": "warning"}.get(state, "dim-label"))


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="BARMbu Studio Helper")
        self.set_default_size(560, 820)
        self.status: dict = {}
        self.busy = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(self._build_menu())
        view.add_top_bar(header)
        # Shown only when GitHub has a newer release of this tool.
        self.update_banner = Adw.Banner(button_label="Download")
        self.update_banner.connect("button-clicked", self.on_tool_update)
        self.tool_update: dict = {}
        view.add_top_bar(self.update_banner)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        scroller = Gtk.ScrolledWindow(vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        view.set_content(scroller)
        clamp = Adw.Clamp(maximum_size=520)
        scroller.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=24, margin_start=18, margin_end=18)
        clamp.set_child(box)

        # --- headline ----------------------------------------------------- #
        self.hero_icon = Gtk.Image(icon_name=APP_ID, pixel_size=96)
        self.title = Gtk.Label(label="Checking your computer…", wrap=True,
                               justify=Gtk.Justification.CENTER)
        self.title.add_css_class("title-1")
        self.subtitle = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.subtitle.add_css_class("dim-label")
        for w in (self.hero_icon, self.title, self.subtitle):
            box.append(w)

        # --- checklist ---------------------------------------------------- #
        group = Adw.PreferencesGroup(title="What's needed")
        self.row_studio = StatusRow("Your Bambu Studio")
        self.row_emu = StatusRow("x86 emulator")
        self.row_copy = StatusRow("Bambu Studio, x86 copy")
        self.row_libs = StatusRow("Extra x86 libraries")
        self.row_plugin = StatusRow("Bambu network plugin")
        self.emu_button = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
        self.emu_button.add_css_class("suggested-action")
        self.emu_button.connect("clicked", self.on_install_emulator)
        self.emu_action: list[str] = ["setup-emulator"]
        self.emu_button.set_visible(False)
        self.row_emu.add_suffix(self.emu_button)
        for r in (self.row_studio, self.row_emu, self.row_copy, self.row_libs,
                  self.row_plugin):
            group.add(r)
        box.append(group)

        # --- progress ----------------------------------------------------- #
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.progress.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.progress)

        # --- buttons ------------------------------------------------------ #
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                          halign=Gtk.Align.CENTER)
        self.main_button = Gtk.Button(label="Set Up")
        self.main_button.add_css_class("pill")
        self.main_button.add_css_class("suggested-action")
        self.main_button.set_size_request(260, -1)
        self.main_button.connect("clicked", self.on_main)
        self.update_button = Gtk.Button(label="Check for Updates", visible=False)
        self.update_button.add_css_class("pill")
        self.update_button.connect("clicked", self.on_update)
        buttons.append(self.main_button)
        buttons.append(self.update_button)
        box.append(buttons)

        # --- details log -------------------------------------------------- #
        self.log_buffer = Gtk.TextBuffer()
        log_view = Gtk.TextView(buffer=self.log_buffer, editable=False,
                                cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                top_margin=8, bottom_margin=8,
                                left_margin=8, right_margin=8)
        self.log_scroll = Gtk.ScrolledWindow(min_content_height=200, child=log_view)
        self.log_scroll.add_css_class("card")
        expander = Gtk.Expander(label="Details", child=self.log_scroll)
        box.append(expander)

        self.refresh()
        self.check_tool_update()

    # ------------------------------------------------------------------ #
    #  Menu
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        menu.append("Open Settings Folder", "win.open-config")
        menu.append("Remove Everything…", "win.remove")
        menu.append("About", "win.about")
        for name, cb in (("open-config", self.on_open_config),
                         ("remove", self.on_remove), ("about", self.on_about)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                              tooltip_text="Menu")

    # ------------------------------------------------------------------ #
    #  Status
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        def work():
            try:
                st = core.status_dict()
            except Exception as exc:  # show anything unexpected, don't crash
                st = {"error": str(exc)}
            GLib.idle_add(self.apply_status, st)
        threading.Thread(target=work, daemon=True).start()

    def check_tool_update(self) -> None:
        def work():
            upd = core.tool_update()
            if upd:
                GLib.idle_add(self.show_tool_update, upd)
        threading.Thread(target=work, daemon=True).start()

    def show_tool_update(self, upd: dict) -> bool:
        self.tool_update = upd
        self.update_banner.set_title(
            f"BARMbu Studio Helper {upd['version']} is available "
            f"(you have {core.__version__})")
        self.update_banner.set_button_label(
            "Update" if upd.get("asset") and upd["kind"] != "source" else "Download")
        self.update_banner.set_revealed(True)
        return False

    def on_tool_update(self, _banner) -> None:
        upd = self.tool_update
        if not upd:
            return
        if upd["kind"] == "source" or not upd.get("asset"):
            Gtk.UriLauncher.new(upd["url"]).launch(self, None, None, None)
            return
        self.update_banner.set_revealed(False)
        self.run_job(["self-update"], f"Updating to {upd['version']}…",
                     "Updated. Close and reopen this app to use the new version.")

    def apply_status(self, st: dict) -> bool:
        self.status = st
        if "error" in st:
            self.title.set_label("Something went wrong")
            self.subtitle.set_label(st["error"])
            return False
        arm = st["arch"] == "aarch64"

        # Your Studio
        studios = [s for s in st["studios"] if not s["managed"]]
        if studios:
            s = studios[0]
            kind = {"flatpak": "Flatpak", "appimage": "AppImage",
                    "native": "Installed package"}[s["kind"]]
            what = f"{kind} {s['version'] or ''}".strip()
            if arm and not s["can_load_x86_plugin"]:
                self.row_studio.show("info", f"{what} (ARM). It can't load Bambu's "
                                     "plugin, so a separate x86 copy is set up below. "
                                     "Your current Studio is left untouched.")
            else:
                self.row_studio.show("ok", what)
        else:
            self.row_studio.show("info", "None found. That's fine, the x86 copy "
                                 "below is a complete Bambu Studio."
                                 if arm else "None found. Install Bambu Studio first.")

        # Emulator / copy / libraries only matter on ARM
        for r in (self.row_emu, self.row_copy, self.row_libs):
            r.set_visible(arm)
        if arm:
            e = st["emulator"]
            if e["ready"]:
                self.row_emu.show("ok", "FEX" + (" + muvm" if e["use_muvm"] else "")
                                  + " ready. Runs x86 programs on this computer.")
                self.emu_button.set_visible(False)
            elif e["pi_model"] and e["needs_muvm"] and not e["muvm"]:
                if e["pi_4k_pending"]:
                    self.row_emu.show("warn", f"{e['problem']}.")
                    self.emu_button.set_visible(False)
                else:
                    self.row_emu.show("warn", f"{e['problem']}. Raspberry Pi OS "
                                      "already includes a 4K kernel: click Switch, "
                                      "then restart.")
                    self.emu_button.set_label("Switch")
                    self.emu_action = ["pi-4k-kernel"]
                    self.emu_button.set_visible(True)
            elif e["fix"] == "barmbu setup-emulator":
                self.row_emu.show("warn", f"{e['problem']}. Click Install "
                                  "(it will ask for your password).")
                self.emu_button.set_label("Install")
                self.emu_action = ["setup-emulator"]
                self.emu_button.set_visible(True)
            else:
                self.row_emu.show("warn", f"{e['problem']}. {e['fix']}")
                self.emu_button.set_visible(False)

            x = st["x86_studio"]
            if x["extracted"]:
                self.row_copy.show("ok", f"Version {x['version']} is installed. "
                                   "It's a full second Bambu Studio that runs "
                                   "under emulation.")
            else:
                self.row_copy.show("todo", "Will be downloaded from Bambu Lab "
                                   "(about 180 MB). It's a full second Bambu "
                                   "Studio, because the plugin only works inside "
                                   "an x86 program.")

            lib = st["libs_overlay"]
            if lib["path"]:
                self.row_libs.show("ok", "Web view add-on ready "
                                   f"({len(lib['packages'])} packages).")
            elif lib["needed"] == []:
                self.row_libs.show("ok", "Nothing extra needed.")
            else:
                self.row_libs.show("todo", "Bambu Studio needs WebKit, which the "
                                   "emulator doesn't include. It will be added from "
                                   "Fedora's own packages (about 90 MB).")

        p = st["plugin"]
        if "libbambu_networking.so" in p["files"]:
            ver = f" (for Studio {p['version']})" if p.get("version") else ""
            self.row_plugin.show("ok", f"Installed{ver}.")
        else:
            self.row_plugin.show("todo", "Will be downloaded from Bambu Lab's "
                                 "server, the same file Bambu Studio fetches.")

        # Headline + buttons
        if st["ready"]:
            self.title.set_label("You're all set")
            self.subtitle.set_label(
                "Open Bambu Studio here or from your app menu: look for "
                "“Bambu Studio (x86, with network plugin)”." if arm else
                "Start Bambu Studio as usual; the network plugin is in place.")
            self.main_button.set_label("Open Bambu Studio" if arm else "Reinstall Plugin")
            self.update_button.set_visible(True)
        else:
            self.title.set_label("Let's get your printer talking")
            self.subtitle.set_label(
                "Bambu's network plugin only exists for regular (x86) PCs. "
                "This sets up an x86 copy of Bambu Studio that runs under "
                "emulation, with the real plugin inside." if arm else
                "This downloads Bambu's network plugin and puts it where "
                "Bambu Studio looks for it.")
            self.main_button.set_label("Set Up")
            self.update_button.set_visible(False)
        can_go = not arm or st["emulator"]["ready"]
        self.main_button.set_sensitive(can_go and not self.busy)
        self.update_button.set_sensitive(not self.busy)
        return False

    # ------------------------------------------------------------------ #
    #  Running jobs
    # ------------------------------------------------------------------ #

    def log(self, text: str) -> None:
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text + "\n")
        adj = self.log_scroll.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)

    def set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        self.progress.set_visible(busy)
        self.main_button.set_sensitive(not busy)
        self.update_button.set_sensitive(not busy)
        self.emu_button.set_sensitive(not busy)
        if busy:
            self.progress.set_fraction(0)
            self.progress.set_text(label)
            self.title.set_label("Working…")
            self.subtitle.set_label("You can keep using your computer. "
                                    "Open Details to see each step.")

    def run_job(self, argv: list[str], label: str, done_msg: str,
                use_core: bool = True) -> None:
        if self.busy:
            return
        self.set_busy(True, label)
        cmd = [sys.executable, str(CORE_SCRIPT), *argv] if use_core else argv
        env = dict(os.environ, BARMBU_GUI="1", PYTHONUNBUFFERED="1")
        self.log("$ " + " ".join(argv))
        state = {"step": 0, "total": 1, "text": label, "error": None}

        def update_bar(sub: float = 0.0):
            frac = (max(state["step"] - 1, 0) + sub) / max(state["total"], 1)
            self.progress.set_fraction(min(frac, 1.0))
            self.progress.set_text(state["text"])
            return False

        def handle(line: str):
            if line.startswith("@@step "):
                _, n, total, text = line.split(" ", 3)
                state.update(step=int(n), total=int(total), text=text)
                update_bar()
            elif line.startswith("@@progress "):
                _, frac, *rest = line.split(" ", 2)
                if rest:
                    state["text"] = rest[0]
                update_bar(float(frac))
            elif line.startswith("@@error "):
                state["error"] = line[8:]
            else:
                self.log(line)
            return False

        def finish(code: int):
            self.set_busy(False)
            if code == 0:
                self.progress.set_fraction(1)
                self.toasts.add_toast(Adw.Toast(title=done_msg))
            else:
                msg = state["error"] or "It didn't finish. Open Details to see why."
                self.toasts.add_toast(Adw.Toast(title=msg, timeout=8))
                self.log(f"(stopped with code {code})")
            self.refresh()
            return False

        def work():
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        env=env, bufsize=1)
            except OSError as exc:
                GLib.idle_add(handle, f"Could not start: {exc}")
                GLib.idle_add(finish, 127)
                return
            for raw in proc.stdout:
                GLib.idle_add(handle, raw.rstrip("\n"))
            GLib.idle_add(finish, proc.wait())

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Button handlers
    # ------------------------------------------------------------------ #

    def on_main(self, _btn) -> None:
        if self.status.get("ready") and self.status.get("arch") == "aarch64":
            subprocess.Popen([sys.executable, str(CORE_SCRIPT), "launch"],
                             start_new_session=True)
            self.toasts.add_toast(Adw.Toast(
                title="Starting Bambu Studio. The first start can take a minute."))
            return
        self.run_job(["install", "--download-studio"], "Setting up…",
                     "All set! Bambu Studio is ready.")

    def on_update(self, _btn) -> None:
        self.run_job(["install", "--update"], "Checking for updates…",
                     "Everything is up to date.")

    def on_install_emulator(self, _btn) -> None:
        if self.emu_action == ["pi-4k-kernel"]:
            self.run_job(self.emu_action, "Selecting the 4K-page kernel…",
                         "Done. Restart your Pi, then open this app again.")
        else:
            self.run_job(["setup-emulator"], "Setting up the x86 emulator…",
                         "Emulator ready.")

    def on_remove(self, *_a) -> None:
        dialog = Adw.AlertDialog(
            heading="Remove everything?",
            body="This removes the x86 copy of Bambu Studio, the library add-on, "
                 "the network plugin and the app-menu shortcut. Your printers and "
                 "settings are kept, and your other Bambu Studio is not touched.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response == "remove":
                self.run_job(["uninstall", "--all"], "Removing…", "Removed.")
        dialog.connect("response", on_response)
        dialog.present(self)

    def on_open_config(self, *_a) -> None:
        folder = core.X86_CONFIG_DIR if core.X86_CONFIG_DIR.exists() else core.home()
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder))).launch(self, None, None)

    def on_about(self, *_a) -> None:
        about = Adw.AboutDialog(
            application_name="BARMbu Studio Helper",
            application_icon=APP_ID,
            version=core.__version__,
            developer_name="barmbu contributors",
            license_type=Gtk.License.GPL_3_0,
            comments="Runs Bambu Lab's official network plugin on ARM64 Linux by "
                     "setting up an x86_64 Bambu Studio under FEX emulation.\n\n"
                     "Not affiliated with Bambu Lab. Bambu Studio and its network "
                     "plugin are downloaded from Bambu Lab and keep their own licences.")
        about.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # When run from the source folder, use the bundled icon.
        icons = HERE.parent / "data" / "icons"
        display = Gdk.Display.get_default()
        if icons.is_dir() and display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main() -> int:
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
