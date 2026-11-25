# barmbu -- build & packaging
#
#   make            # checks, then build every package this machine can make
#   make check      # compile + offline self-test
#   make test       # check + unit tests
#   make install    # install into $(DESTDIR)$(PREFIX) (default /usr/local)
#   make deb        # dist/barmbu_<ver>_all.deb
#   make rpm        # dist/barmbu-<ver>-1.noarch.rpm
#                   #   (uses rpmbuild, or a Fedora container via podman)
#   make flatpak    # dist/barmbu-<ver>.flatpak (needs flatpak-builder)
#   make dist       # source tarball
#   make clean

PREFIX  ?= /usr/local
DESTDIR ?=
PKGNAME := barmbu
APP_ID  := io.github.amruthwo.Barmbu
CORE    := src/barmbu.py
GUI     := src/barmbu_gui.py
OVERLAY := src/x86_overlay.py
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' $(CORE))
PY      ?= python3
FEDORA_IMAGE ?= registry.fedoraproject.org/fedora:45

BINDIR   = $(DESTDIR)$(PREFIX)/bin
SHAREDIR = $(DESTDIR)$(PREFIX)/share
LIBDIR   = $(SHAREDIR)/$(PKGNAME)
DOCDIR   = $(SHAREDIR)/doc/$(PKGNAME)

.DEFAULT_GOAL := all

.PHONY: all
all: check
	@if command -v dpkg-deb >/dev/null 2>&1; then $(MAKE) --no-print-directory deb; \
	else echo "dpkg-deb not found; skipping .deb"; fi
	@$(MAKE) --no-print-directory rpm || echo "!! .rpm build failed (see above); other packages are fine"
	@ls -1 dist/ 2>/dev/null | sed 's/^/  dist\//'

.PHONY: check
check:
	@echo ">> syntax check"
	$(PY) -I -m py_compile $(CORE) $(GUI) $(OVERLAY)
	@echo ">> --help"
	$(PY) -I $(CORE) --help >/dev/null
	@echo ">> offline self-test (detect)"
	$(PY) -I $(CORE) detect >/dev/null
	@if command -v desktop-file-validate >/dev/null 2>&1; then \
		echo ">> desktop file"; desktop-file-validate data/$(APP_ID).desktop; fi
	@if command -v appstreamcli >/dev/null 2>&1; then \
		echo ">> metainfo"; appstreamcli validate --no-net --pedantic data/$(APP_ID).metainfo.xml >/dev/null \
		|| appstreamcli validate --no-net data/$(APP_ID).metainfo.xml; fi
	@rm -rf src/__pycache__
	@echo "OK (version $(VERSION))"

.PHONY: test
test: check
	@echo ">> unit tests"
	$(PY) -I -m unittest discover -s tests -p 'test_*.py' -v
	@rm -rf src/__pycache__ tests/__pycache__

# Copy files into $(DESTDIR)$(PREFIX) -- used by install and every package.
.PHONY: install-files
install-files:
	install -Dm0755 $(CORE)    $(LIBDIR)/barmbu.py
	install -Dm0755 $(GUI)     $(LIBDIR)/barmbu_gui.py
	install -Dm0644 $(OVERLAY) $(LIBDIR)/x86_overlay.py
	install -d $(BINDIR)
	ln -sf ../share/$(PKGNAME)/barmbu.py $(BINDIR)/$(PKGNAME)
	ln -sf ../share/$(PKGNAME)/barmbu_gui.py    $(BINDIR)/$(PKGNAME)-gui
	install -Dm0644 data/$(APP_ID).desktop       $(SHAREDIR)/applications/$(APP_ID).desktop
	install -Dm0644 data/$(APP_ID).metainfo.xml  $(SHAREDIR)/metainfo/$(APP_ID).metainfo.xml
	install -Dm0644 data/icons/hicolor/scalable/apps/$(APP_ID).svg \
		$(SHAREDIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -Dm0644 README.md $(DOCDIR)/README.md
	install -Dm0644 LICENSE   $(DOCDIR)/LICENSE

.PHONY: install
install: check install-files
	@echo "Installed $(PKGNAME) $(VERSION) into $(DESTDIR)$(PREFIX)"

.PHONY: deb
deb: check
	@command -v dpkg-deb >/dev/null 2>&1 || { echo "dpkg-deb required"; exit 1; }
	rm -rf build/deb
	$(MAKE) --no-print-directory install-files DESTDIR=$(CURDIR)/build/deb PREFIX=/usr
	mkdir -p build/deb/DEBIAN
	sed 's/@VERSION@/$(VERSION)/g' packaging/deb/control.in > build/deb/DEBIAN/control
	install -m0755 packaging/deb/postinst build/deb/DEBIAN/postinst
	mkdir -p dist
	dpkg-deb --build --root-owner-group build/deb dist/$(PKGNAME)_$(VERSION)_all.deb
	@echo "Built dist/$(PKGNAME)_$(VERSION)_all.deb"

.PHONY: dist
dist: check
	rm -rf build/tar && mkdir -p dist build/tar/$(PKGNAME)-$(VERSION)
	cp -r src data tests packaging README.md LICENSE Makefile build/tar/$(PKGNAME)-$(VERSION)/
	rm -rf build/tar/$(PKGNAME)-$(VERSION)/src/__pycache__ build/tar/$(PKGNAME)-$(VERSION)/tests/__pycache__
	tar -C build/tar -czf dist/$(PKGNAME)-$(VERSION).tar.gz $(PKGNAME)-$(VERSION)
	@echo "Built dist/$(PKGNAME)-$(VERSION).tar.gz"

# RPM: use rpmbuild if installed, otherwise build inside a throwaway Fedora
# container (rootless podman), so no extra packages are needed on the host.
.PHONY: rpm
rpm: dist
	@set -e; \
	rm -rf build/rpm; \
	mkdir -p build/rpm/SPECS build/rpm/SOURCES build/rpm/BUILD build/rpm/RPMS build/rpm/SRPMS; \
	cp dist/$(PKGNAME)-$(VERSION).tar.gz build/rpm/SOURCES/; \
	sed 's/@VERSION@/$(VERSION)/g' packaging/rpm/$(PKGNAME).spec.in > build/rpm/SPECS/$(PKGNAME).spec; \
	if command -v rpmbuild >/dev/null 2>&1; then \
		rpmbuild --define "_topdir $(CURDIR)/build/rpm" -bb build/rpm/SPECS/$(PKGNAME).spec; \
	elif command -v podman >/dev/null 2>&1; then \
		echo ">> rpmbuild not installed; building the RPM in a Fedora container"; \
		podman run --rm --network=host -v "$(CURDIR)/build/rpm:/rpmbuild:Z" $(FEDORA_IMAGE) \
			bash -c 'dnf -y -q install rpm-build make >/dev/null && \
			         rpmbuild --define "_topdir /rpmbuild" -bb /rpmbuild/SPECS/$(PKGNAME).spec'; \
	else \
		echo "Neither rpmbuild nor podman found; skipping .rpm (sudo dnf install rpm-build)."; \
		exit 0; \
	fi; \
	mkdir -p dist; \
	find build/rpm/RPMS -name '*.rpm' -exec cp {} dist/ \; ; \
	echo "Built: $$(cd dist && ls *.rpm)"

.PHONY: flatpak
flatpak: check
	@command -v flatpak-builder >/dev/null 2>&1 || { echo "flatpak-builder required (skipping)"; exit 0; }
	flatpak-builder --force-clean --user --install-deps-from=flathub \
		--state-dir=build/.flatpak-builder --repo=build/flatpak-repo \
		build/flatpak packaging/flatpak/$(APP_ID).yml
	mkdir -p dist
	flatpak build-bundle build/flatpak-repo dist/$(PKGNAME)-$(VERSION).flatpak $(APP_ID)
	@echo "Built dist/$(PKGNAME)-$(VERSION).flatpak"

.PHONY: clean
clean:
	rm -rf build dist src/__pycache__ tests/__pycache__
	@echo "cleaned"
