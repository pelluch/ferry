# ferry

Sync a self-hosted [RomM](https://romm.app/) library to a local Linux ES-DE
install (RetroDECK, EmuDeck, or bare ES-DE), and keep save data in sync
across devices.

Status: pre-alpha. See [DESIGN.md](../claude/romm/DESIGN.md) for the full
architectural plan and phased roadmap.

## Install

ferry isn't on PyPI yet (the name is taken by an unrelated abandoned
package — see DESIGN.md §8). Install from a local checkout:

```sh
uv tool install /path/to/ferry
```

This puts the `ferry` binary at `~/.local/bin/ferry`.

## Quickstart

```sh
ferry config edit          # creates ~/.config/ferry/config.toml from template
ferry detect               # probe for known ES-DE/RetroDECK/EmuDeck installs
ferry ping                 # smoke-test the RomM connection
ferry sync --dry-run       # preview what would change
ferry sync                 # do it
```

## Scheduled sync

ferry ships a systemd user timer for unattended periodic sync:

```sh
ferry install-units                    # daily, by default
ferry install-units --schedule hourly  # or any `OnCalendar` spec
```

This installs `ferry-sync.{service,timer}` to `~/.config/systemd/user/`,
runs `systemctl --user daemon-reload`, and enables the timer. Re-running
with a different `--schedule` updates the timer in place.

Schedules faster than every 10 minutes are rejected — RomM library updates
aren't real-time, and a faster cadence just hammers the server.

Inspect: `systemctl --user list-timers ferry-sync.timer`
Logs: `journalctl --user -u ferry-sync.service -f`

On non-systemd distros the install command errors out with a copy-paste
cron suggestion. Native cron support is on the roadmap (DESIGN.md §9).

## Uninstalling

Run `ferry uninstall-units` **before** removing ferry — otherwise the timer
keeps firing against a missing binary and pollutes your systemd journal.

```sh
ferry uninstall-units      # disable timer, remove unit files
uv tool uninstall ferry    # then remove the binary
```

## License

GPL-3.0-only. Substantial code is lifted from
[decky-romm-sync](https://github.com/) (GPL-3.0); see `LICENSE`.
