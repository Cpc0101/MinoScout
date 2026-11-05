Mino Scout installer (zip)

Frozen payload (GitHub Release): layered. No Python on the machine.
Source payload (dev pack without --binary): needs Python 3.12+ and a venv.

Layers. This zip carries whatever layer directories are present next to
this file; layers it does not carry are left untouched on disk.

  runtime/   -> <prefix>/bin/mino-scout + _internal/   (deps + Python)
  app/       -> <prefix>/bin/app/mino_scout/           (Scout's own code)
  browser/   -> <prefix>/bin/ms-playwright/            (Chromium)

layers.txt at the root of this zip says which layers it carries and their
fingerprints. The installed state lives in <prefix>/bin/layers.txt.

A full install ships all three (about 439 MB). A code-only update ships
just the app layer (about 90 KB) and never rewrites the 780 MB browser
layer. An app-only zip is refused if its requires_runtime does not match
the runtime fingerprint already installed - use the combined zip then.

Daemon:

  macOS   root → LaunchDaemon /Library/LaunchDaemons/com.mino.scout.plist
          user → LaunchAgent  ~/Library/LaunchAgents/com.mino.scout.plist
  Windows admin → Scheduled Task "Mino Scout" AtStartup / SYSTEM
          user  → AtLogOn (Startup folder fallback)
  Linux   root → systemd mino-scout.service
          user → systemd --user mino-scout.service
                 (headless machines: loginctl enable-linger $USER, or install as root)

Mino Workbench writes config.json next to the install (nexus_url + token)
before opening this package. Nexus never hosts these binaries.

Control:

  mino-scout status
  mino-scout stop
  mino-scout probe

Sleep: Scout keeps the machine online while it is trying to stay connected to
Nexus, including reconnect. macOS uses caffeinate -dims (display, idle,
disk, and an AC-only system sleep assertion). Windows uses
SYSTEM_REQUIRED+DISPLAY_REQUIRED. Linux inhibits idle/sleep/lid.
On macOS, idle sleep assertions also work on battery. They do not guarantee
USB power or prevent lid-close or user-requested sleep, even on AC. Keep the
lid open during execution unless using a supported macOS closed-display setup.

Playwright Chromium ships inside the browser layer; nothing to install
by hand. PLAYWRIGHT_BROWSERS_PATH points at <prefix>/bin/ms-playwright.

MINO_SCOUT_SKIP_SERVICE=1 installs the payload without registering a
daemon (CI and self-tests).

Uninstall (user install):
  ~/.mino/scout/bin/mino-scout stop
  launchctl unload ~/Library/LaunchAgents/com.mino.scout.plist
  rm ~/Library/LaunchAgents/com.mino.scout.plist
  rm -rf ~/.mino/scout

macOS closed-lid execution (including battery power): startup automatically
requests first-use authorization unless explicitly disabled. Cancellation is
not retried during the same process. Manual controls:
  mino-scout configure closed-lid on|off|status
The on command requests native administrator authorization for a fixed-purpose
launchd helper. While Scout holds a live lease, the helper uses system-wide
SleepDisabled and restores its original value after all leases expire.
Keep the machine ventilated; this consumes battery and affects manual sleep.
See docs/DEVICE_SETUP.md for lease recovery and hardware acceptance checks.
