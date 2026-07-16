# dcc-mcp-substance3d-painter

Substance 3D Painter adapter for the DCC Model Context Protocol (MCP).

It embeds a Streamable HTTP MCP server in Painter and routes host API calls
through Painter's Qt main thread. Painter uses an OS-assigned port by default
and advertises the endpoint through DCC-MCP discovery.

## Install and load

Install into the Python environment Painter uses:

```bash
python -m pip install dcc-mcp-substance3d-painter
```

Point `SUBSTANCE_PAINTER_PLUGINS_PATH` at the installed package's
`dcc_mcp_substance3d_painter/painter` folder and add the installation root to
`PYTHONPATH`. Painter discovers the packaged
`startup/dcc_mcp_substance3d_painter_plugin.py` entry point and starts the
adapter automatically.

Set `DCC_MCP_SUBSTANCE3D_PAINTER_PORT` before launching Painter only when a
fixed port is required; `0` keeps automatic allocation. Standard
`DCC_MCP_GATEWAY_PORT` and `DCC_MCP_REGISTRY_DIR` settings are also honoured.

## Bundled skills

`painter-project` provides typed tools for a complete material-authoring pass:

- inspect the project and texture sets;
- create PBR fill layers;
- search Painter resources and apply smart materials;
- list export presets, save the `.spp`, and export textures.

The tools use Painter resource and preset URLs supplied by Painter itself. They
do not expose raw JavaScript or arbitrary script execution.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check src tests tools
python -m build
```

Releases use release-please. The `release.yml` workflow publishes through the
`pypi` environment using PyPI Trusted Publishing.
