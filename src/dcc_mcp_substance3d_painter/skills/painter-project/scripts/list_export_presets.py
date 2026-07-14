"""List Painter export presets through the dedicated export API."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(query: str = "", limit: int = 50, **_kwargs):
    import substance_painter.export as export  # Lazy: Painter host only.

    resolved_limit = int(limit)
    if not 1 <= resolved_limit <= 200:
        return skill_error("limit must be between 1 and 200", "INVALID_LIMIT")
    needle = str(query).strip().casefold()

    presets = []
    for preset in export.list_predefined_export_presets():
        if needle and needle not in preset.name.casefold():
            continue
        presets.append({"name": preset.name, "url": preset.url, "kind": "predefined"})

    for preset in export.list_resource_export_presets():
        identifier = preset.resource_id
        if needle and needle not in identifier.name.casefold():
            continue
        presets.append({"name": identifier.name, "url": identifier.url(), "kind": "resource"})

    presets.sort(key=lambda item: (item["name"].casefold(), item["kind"]))
    presets = presets[:resolved_limit]
    return skill_success(
        "Listed Painter export presets",
        query=str(query),
        preset_count=len(presets),
        presets=presets,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
