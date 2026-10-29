"""List preset resources (generators and smart materials) available to Painter."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import enum_name

# Only these two resource families are runtime presets: an .sbsar generator and
# an .spsm smart material. Other usages (alpha, environment, export, texture)
# are ordinary resources, not parametric presets.
_PRESET_USAGES = frozenset({"GENERATOR", "SMART_MATERIAL"})


def _usage_names(resource, item) -> list[str]:
    usages = []
    try:
        for value in item.usages():
            usages.append(enum_name(value).upper())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return []
    return usages


@skill_entry
def main(query: str = "", limit: int = 25, **_kwargs):
    import substance_painter.resource as resource  # Lazy: Painter host only.

    resolved_limit = int(limit)
    if not 1 <= resolved_limit <= 100:
        return skill_error("limit must be between 1 and 100", "INVALID_LIMIT")

    matches = []
    for item in resource.search(str(query)):
        usages = _usage_names(resource, item)
        if not _PRESET_USAGES.intersection(usages):
            continue
        try:
            identifier = item.identifier()
            entry = {
                "name": item.gui_name(),
                "url": str(identifier.url()),
                "category": item.category(),
                "type": enum_name(item.type()),
                "usages": sorted(usages),
            }
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        matches.append(entry)
        if len(matches) >= resolved_limit:
            break

    return skill_success(
        "Listed Painter preset resources",
        query=str(query),
        match_count=len(matches),
        resources=matches,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
