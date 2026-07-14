# dcc-mcp-substance3d-painter

Substance 3D Painter adapter for the DCC Model Context Protocol (MCP).

It embeds a Streamable HTTP MCP server in Painter and routes host API calls
through Painter's Qt main thread. The default endpoint is
`http://127.0.0.1:8765/mcp`.

## Install and load

Install into the Python environment Painter uses:

```bash
python -m pip install dcc-mcp-substance3d-painter
```

Point `SUBSTANCE_PAINTER_PLUGINS_PATH` at the installed package's
`dcc_mcp_substance3d_painter/painter` folder. Painter discovers the packaged
`plugins/dcc_mcp_substance3d_painter_plugin.py` entry point. For unattended
launches, set
`SUBSTANCE_PAINTER_STARTUP_PLUGINS=dcc_mcp_substance3d_painter_plugin` so
Painter starts the adapter without an interactive Python-menu action.

Set `DCC_MCP_SUBSTANCE3D_PAINTER_PORT` before launching Painter to choose a
different port. Standard `DCC_MCP_GATEWAY_PORT` and `DCC_MCP_REGISTRY_DIR`
settings are also honoured.

## Bundled skills

`painter-project` inspects the current project and texture sets. Its typed
export tool exports texture sets using a caller-provided Painter export preset
URL, avoiding raw JavaScript or arbitrary script execution.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check src tests tools
python -m build
```

Releases use release-please. The `release.yml` workflow publishes through the
`pypi` environment using PyPI Trusted Publishing.
