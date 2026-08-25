# Install DCC-MCP for Substance 3D Painter

This adapter installs a small startup loader into Painter's user resource
profile. The loader adds one exact wheel environment to Python's module search
path and starts the embedded DCC-MCP server on Painter's main thread.

## Requirements

- Substance 3D Painter 7.2 or newer.
- Python 3.9 or newer for the wheel environment.
- `dcc-mcp-core>=0.20.15,<1.0.0` in that same environment.
- Write access to the current user's Painter resource profile.

Install or update the wheel in the exact interpreter that the lifecycle
command will record. When Painter's `resources/pythonsdk/lib/python*.zip`
marker is present, preflight also requires that interpreter's major/minor
version to match Painter's embedded Python:

```bash
python -m pip install --upgrade dcc-mcp-substance3d-painter
```

## Supported versions

Windows, macOS, and Linux are supported. The lifecycle command checks the
Painter 7.2 floor before changing the profile. It reads Windows executable
metadata, a macOS app bundle's `Info.plist`, or a version embedded in the
selected path. If installed metadata is unavailable, set
the exact Painter executable with `--dcc-path`; unverified version overrides are
not accepted.

Default user resource profile on all three platforms:

- Windows: `%USERPROFILE%\Documents\Adobe\Adobe Substance 3D Painter`
- macOS: `~/Documents/Adobe/Adobe Substance 3D Painter`
- Linux: `~/Documents/Adobe/Adobe Substance 3D Painter`

Override only when Painter uses a different profile with
`DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE`.

## Agent quick path

Use the same absolute Python path for wheel installation and `--python`.
Omit `--dcc-path` only when exactly one standard Painter installation exists.

```bash
dcc-mcp-substance3d-painter install --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json --dry-run
dcc-mcp-substance3d-painter install --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json --yes
dcc-mcp-substance3d-painter status --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json
```

Without `--yes`, `install`, `upgrade`, and `uninstall` only return a plan.
JSON uses schema version 1 and stable exits: `0` success, `10` preflight,
`20` acquisition, `30` install, `40` verification, and `50` restart required.
Every recovery action is returned in `next_steps` as an executable argument
array.

An install can return exit 40 after safely writing its receipt when Painter is
not running. Execute the returned launch command, wait for startup, then run
`verify`; only a successful main-thread ping reports `directly_usable: true`.

## Manual path

The standard lifecycle writes these receipt-owned files without editing a
shared startup file:

```text
<profile>/python/startup/dcc_mcp_substance3d_painter_plugin.py
<profile>/python/modules/dcc_mcp_substance3d_painter_bootstrap/__init__.py
<profile>/.dcc-mcp/receipts/substance3d_painter.json
```

For a manually managed environment, point `SUBSTANCE_PAINTER_PLUGINS_PATH` at
the installed package's `dcc_mcp_substance3d_painter/painter` directory (the
directory containing `startup`) and add that environment's site-packages to
`PYTHONPATH`. The lifecycle command is preferred because it records ownership,
stages replacements, checks versions, and supports rollback.

## Verify

Launch Painter normally, then run:

```bash
dcc-mcp-substance3d-painter verify --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json
```

Verification checks the receipt and SHA-256 digests, imports the adapter and
Core from their recorded installed distributions, checks bootstrap error logs,
finds one live Painter runtime, independently captures its PID, executable and
start identity before and after the probe, and calls the read-only
`painter_diagnostics__ping` tool through the existing main-thread bridge. The
probe must report the exact receipted plugin/module origins before readiness is
reported.

## Upgrade

First update the wheel in the recorded interpreter. Close Painter on Windows
so loaded files cannot block replacement, then plan and execute the upgrade:

```bash
python -m pip install --upgrade dcc-mcp-substance3d-painter
dcc-mcp-substance3d-painter upgrade --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json --dry-run
dcc-mcp-substance3d-painter upgrade --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json --yes
```

The new payload is staged on the profile filesystem. A failed upgrade restores
the previous loader, bootstrap package, and receipt.

## Uninstall

Plan first, then remove only the files named and hashed by the receipt:

```bash
dcc-mcp-substance3d-painter uninstall --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json --dry-run
dcc-mcp-substance3d-painter uninstall --dcc-path "/absolute/path/to/Painter" --python "/absolute/path/to/python" --json --yes
python -m pip uninstall dcc-mcp-substance3d-painter
```

Close Painter before uninstalling. Unreceipted or locally modified files are
preserved and reported instead of being deleted.

## Troubleshooting

- **Host preflight (exit 10):** pass the exact executable or `.app` with
  `--dcc-path`. The installer fails closed when trusted executable or bundle
  version metadata is unavailable.
- **Python/Core import (exit 10):** reinstall the wheel and
  `dcc-mcp-core>=0.20.15` using the exact `--python` interpreter. Do not use
  credentials or a different shell's Python implicitly.
- **Partial or repair state (exit 10):** keep the reported files in place and
  rerun `install --yes`; unreceipted files are never overwritten.
- **Bootstrap failure (exit 40):** inspect `.dcc-mcp/logs` below the Painter
  profile. Import, startup, and shutdown failures are captured and re-raised so
  Painter does not silently appear ready.
- **Readiness failure (exit 40):** launch Painter, wait for its startup plugins,
  ensure only one Painter runtime is registered, and execute the exact verify
  command returned in `next_steps`.
- **Locked files (exit 50):** close Painter and execute the returned retry
  command. The installer will not delete a loaded tree before replacement.
