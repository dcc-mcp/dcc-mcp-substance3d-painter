"""Bounded inline Painter export-preset contract."""

from __future__ import annotations

import re
from typing import Any

_FILE_NAME = re.compile(r"^[A-Za-z0-9_$().-]{1,128}$")
_MAP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_CHANNELS = {"R", "G", "B", "A", "RGB", "RGB+A"}
_DESTINATION_OCCUPANCY = {
    "R": frozenset({"R"}),
    "G": frozenset({"G"}),
    "B": frozenset({"B"}),
    "A": frozenset({"A"}),
    "RGB": frozenset({"R", "G", "B"}),
    "RGB+A": frozenset({"R", "G", "B", "A"}),
}
_COMPOSITE_DESTINATIONS = {"RGB", "RGB+A"}


def build_export_preset(
    name: str,
    maps: list[dict[str, Any]],
    bit_depth: int = 8,
    dithering: bool = False,
) -> dict[str, Any]:
    resolved_name = str(name).strip()
    if not _MAP_NAME.fullmatch(resolved_name):
        raise ValueError("name must start with a letter and contain only letters, digits, hyphens, or underscores")
    if not 1 <= len(maps) <= 32:
        raise ValueError("maps must contain between 1 and 32 output maps")
    resolved_depth = int(bit_depth)
    if resolved_depth not in {8, 16}:
        raise ValueError("bit_depth must be 8 or 16")

    output_maps = []
    for index, output_map in enumerate(maps):
        if not isinstance(output_map, dict):
            raise ValueError(f"maps[{index}] must be an object")
        file_name = str(output_map.get("file_name", ""))
        if not _FILE_NAME.fullmatch(file_name) or ".." in file_name:
            raise ValueError(f"maps[{index}].file_name is not a bounded Painter filename template")
        channels = output_map.get("channels")
        if not isinstance(channels, list) or not 1 <= len(channels) <= 4:
            raise ValueError(f"maps[{index}].channels must contain between 1 and 4 channels")
        destinations = set()
        occupied_destinations: set[str] = set()
        resolved_channels = []
        for channel_index, channel in enumerate(channels):
            if not isinstance(channel, dict):
                raise ValueError(f"maps[{index}].channels[{channel_index}] must be an object")
            destination = str(channel.get("destination", "")).upper()
            source_channel = str(channel.get("source_channel", "")).upper()
            source_map = str(channel.get("source_map", ""))
            if destination not in _CHANNELS or source_channel not in _CHANNELS:
                raise ValueError("destination and source_channel must be bounded Painter channels")
            occupied = _DESTINATION_OCCUPANCY[destination]
            if occupied_destinations.intersection(occupied):
                raise ValueError("DESTINATION_CHANNELS_OVERLAP")
            if destinations and (destination in _COMPOSITE_DESTINATIONS or destinations & _COMPOSITE_DESTINATIONS):
                raise ValueError("DESTINATION_CHANNELS_OVERLAP")
            if not _MAP_NAME.fullmatch(source_map):
                raise ValueError(f"maps[{index}].channels[{channel_index}].source_map is invalid")
            destinations.add(destination)
            occupied_destinations.update(occupied)
            resolved_channels.append(
                {
                    "destChannel": destination,
                    "srcChannel": source_channel,
                    "srcMapType": "documentMap",
                    "srcMapName": source_map,
                }
            )
        output_maps.append(
            {
                "fileName": file_name,
                "channels": resolved_channels,
                "parameters": {
                    "fileFormat": "png",
                    "bitDepth": str(resolved_depth),
                    "dithering": bool(dithering),
                },
            }
        )
    return {"name": resolved_name, "maps": output_maps}


def validate_export_preset(preset: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(preset, dict):
        raise ValueError("preset must be an object returned by create_export_preset")
    maps = []
    for item in preset.get("maps", []):
        maps.append(
            {
                "file_name": item.get("fileName"),
                "channels": [
                    {
                        "destination": channel.get("destChannel"),
                        "source_channel": channel.get("srcChannel"),
                        "source_map": channel.get("srcMapName"),
                    }
                    for channel in item.get("channels", [])
                    if channel.get("srcMapType") == "documentMap"
                ],
            }
        )
    parameters = preset.get("maps", [{}])[0].get("parameters", {}) if preset.get("maps") else {}
    depth = int(parameters.get("bitDepth", 8))
    dithering = bool(parameters.get("dithering", False))
    rebuilt = build_export_preset(str(preset.get("name", "")), maps, depth, dithering)
    if rebuilt != preset:
        raise ValueError("preset contains unsupported or inconsistent fields")
    return rebuilt
