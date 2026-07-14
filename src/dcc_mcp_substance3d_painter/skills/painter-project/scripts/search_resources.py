"""Search Painter resources with an optional usage filter."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(query: str = "", usage: str | None = None, limit: int = 25, **_kwargs):
    import substance_painter.resource as resource  # Lazy: Painter host only.

    resolved_limit = int(limit)
    if not 1 <= resolved_limit <= 100:
        return skill_error("limit must be between 1 and 100", "INVALID_LIMIT")

    usage_filter = None
    if usage:
        usage_name = str(usage).strip().upper()
        try:
            usage_filter = resource.Usage[usage_name]
        except KeyError:
            supported = sorted(member.name for member in resource.Usage)
            return skill_error("Unsupported Painter resource usage", f"{usage_name}; supported={supported}")

    matches = []
    skipped_count = 0
    for item in resource.search(str(query)):
        try:
            item_usages = list(item.usages())
        except (RuntimeError, ValueError):
            # Older Painter releases may expose resource usage values that are
            # missing from the public Usage enum (for example ``postfx``).
            skipped_count += 1
            continue
        if usage_filter is not None and usage_filter not in item_usages:
            continue
        identifier = item.identifier()
        matches.append(
            {
                "name": item.gui_name(),
                "url": identifier.url(),
                "category": item.category(),
                "type": item.type().name,
                "usages": sorted(member.name for member in item_usages),
            }
        )
        if len(matches) >= resolved_limit:
            break

    return skill_success(
        "Searched Painter resources",
        query=str(query),
        usage=usage_filter.name if usage_filter is not None else None,
        match_count=len(matches),
        skipped_count=skipped_count,
        resources=matches,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
