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

To upgrade after pulling new commits, use `--reinstall` — without it,
`uv tool install` is a no-op for already-installed tools and your
snapshot stays at the previous version:

```sh
uv tool install /path/to/ferry --reinstall
```

## Quickstart

```sh
ferry config edit          # creates ~/.config/ferry/config.toml from template
ferry detect               # probe for known ES-DE/RetroDECK/EmuDeck installs
ferry ping                 # smoke-test the RomM connection
ferry sync --dry-run       # preview what would change
ferry sync                 # do it
```

## Uninstalling

If you installed ES-DE launch hooks, remove them first so they don't keep
pointing at a missing binary:

```sh
ferry uninstall-launch-hooks   # remove the managed block + wrapper script
uv tool uninstall ferry        # then remove the binary
```

## Credits

ferry is built on top of foundational work from
[decky-romm-sync](https://github.com/danielcopper/decky-romm-sync) by
Daniel Copper ([@danielcopper](https://github.com/danielcopper)),
licensed under GPL-3.0. ferry inherits and adapts substantial parts of
that codebase — including the save-conflict resolution model, the RomM
HTTP/API adapter, the RetroArch config and core-info parsing, and the
RomM-slug → ES-DE platform-directory map. Per-file headers in the
relevant modules call out exactly what was lifted and what changed; see
DESIGN.md §6 for the full reuse plan.

## License

GPL-3.0-only; see [LICENSE](LICENSE) for the full text. Code derived
from decky-romm-sync (GPL-3.0) is redistributed under the same license.
