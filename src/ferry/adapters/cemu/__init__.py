"""Cemu (Wii U) adapter — install discovery + title-ID extraction.

Structurally parallel to `adapters/dolphin/`: `cemu_paths` discovers
where Cemu keeps its Wii U saves + keys, and `cemu_tool` shells out to
the `cemu` binary to read a ROM's title ID (Cemu's `--extract` mode is
the Wii U equivalent of `dolphin-tool header`).
"""
