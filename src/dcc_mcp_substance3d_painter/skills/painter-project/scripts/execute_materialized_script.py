"""Run one Core-materialized Python FileRef through the Painter main thread."""

from __future__ import annotations

from dcc_mcp_core.cancellation import DccMcpCancelledError
from dcc_mcp_core.skill import skill_entry, skill_error

from dcc_mcp_substance3d_painter.materialized_script_executor import (
    MaterializedScriptRejected,
    execute_materialized_file_ref,
)


@skill_entry
def main(file_ref):
    try:
        return execute_materialized_file_ref(file_ref)
    except MaterializedScriptRejected as exc:
        if exc.source_entered:
            result = skill_error(
                "Materialized script did not produce a verified result",
                exc.code,
            )
            result["prompt"] = None
            return result
        return skill_error(
            "Materialized script rejected before Painter execution",
            exc.code,
            prompt="Call materialize_script again and pass its unchanged file_ref.",
        )
    except DccMcpCancelledError:
        raise
    except BaseException:
        return skill_error(
            "Materialized script executor failed before a verified result",
            "script_executor_failed",
            prompt="Call materialize_script again and retry with its unchanged file_ref.",
        )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
