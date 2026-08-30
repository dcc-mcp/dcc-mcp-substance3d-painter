---
name: painter-project
description: >-
  Host skill - inspect and author the current Substance 3D Painter project.
  Use when creating or checking projects, building uniform or texture-driven
  PBR layers, masks and generators, baking mesh maps, importing resources, or exporting.
  Not for arbitrary JavaScript execution.
license: MIT
compatibility: "Substance 3D Painter Python API; dcc-mcp-core 0.20.15+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: substance3d_painter
    version: "0.0.0"
    layer: domain
    stage: pipeline
    search-hint: "substance painter create project mesh pbr paint fill layer stack mask generator bake maps import resource selective export preset"
    tags: "substance, painter, textures, materials, layers, masks, generators, baking, mesh-maps, resources, export, project"
    tools: tools.yaml
---

# Painter Project

Every tool in this skill runs through Painter's main-thread bridge and returns
a core job envelope. Use `--wait` when calling through `dcc-mcp-cli`, or poll
`jobs_get_status` with the returned `job_id` until the job is terminal.

`execute_materialized_script` is the bounded companion to Core's
`materialize_script`. It accepts only the unchanged Python `file_ref` returned
by Core: no inline source, direct path, arguments, execution mode, or UI route.
Before Painter runs the fixed `main()` entry point, the adapter verifies the
materialization sidecar, scoped path, regular-file identity, single-link
ownership, size, UTF-8 encoding, digest, expiry, and a stable file snapshot.
The full security and expiry check is repeated immediately before main-thread
dispatch. Execution remains bound to the validated function definition, its
prefix helpers/imports, and its prefix globals. The adapter invokes that
host-owned entrypoint, snapshots its strict JSON result when valid, and executes
suffix source exactly once after every source-entered `main()` attempt in a
quarantined side-effect phase. Rebinding, object/module attributes, and mutable
aliases in suffix source cannot change the captured behavior or result, and
suffix failures do not clobber the main outcome (including a stable rejection
for invalid results). The suffix is side-effect-only; if `main()` needs a helper,
import, or mutable initialization introduced only by that suffix, execution
fails closed with `script_suffix_dependency`. Define all `main()` dependencies
before the entrypoint.
Cancellation is propagated only from the exact host token and job captured
before source entry; source-installed ambient tokens are rejected as execution
failures. The captured host ContextVar state, cancellation module bindings,
validator alias, and JSON serializer are restored after both source phases, so
source changes cannot poison a later request. Results
must be strict portable JSON with plain string-keyed objects and plain lists;
tuples, custom mappings, non-string keys, nested NaN, and Infinity are rejected.
Host-owned validation enforces maximum container depth 64, 10,000 value nodes,
and 256 KiB of compact UTF-8 JSON. The byte limit covers the complete public
response, including host-owned context and postcondition fields. Rejected
contracts return stable error codes without exposing host paths.

| Contract state | Result | Painter source entered |
| --- | --- | --- |
| Exact, live Core FileRef and unchanged snapshot | Pre-suffix validated `main()` result plus digest/size postcondition; suffix still runs once | Yes |
| Missing/extra fields, inline/path/mode input, wrong scope, or expired | `file_ref_invalid`, `file_ref_scope_denied`, or `file_ref_expired` | No |
| Link, hardlink, replacement, identity, size, or digest drift | Stable `file_ref_*` integrity error | No |
| Non-UTF-8 source, invalid syntax, or invalid fixed entry contract | Stable `script_*` error with rematerialization prompt | No |
| `main()` depends on helper/import/state introduced only by the suffix | `script_suffix_dependency` without retry/rematerialization prompt | Yes |
| Exception, forged adapter/cancellation error, or invalid/over-budget result after source entry | Stable `script_*` error without retry/rematerialization prompt; suffix still runs once | Yes |

Create or open a project, close only a clean project, save the active project
or save to an explicit `.spp` path, and reload its mesh through Painter's
native callback. Lifecycle mutations fail closed unless Painter readback
confirms the resulting open, closed, clean, or imported-mesh state. Project
creation accepts a bounded UV workflow, optional template, resolution, and
normal-map format.

Add typed uniform, texture-driven fill, or paint layers. Address a node by the
UID returned from `list_layer_stack`; masks are limited to black, white, or a
validated smart-mask resource, and generator effects require a validated
generator resource. Every mutation is read back from the selected Painter
stack before success. Import project resources with an explicit bounded
usage, then pass their stable resource URL to a mask, generator, or smart
material call in the same host session. Imports fail before Painter I/O unless
the source is a regular non-link file of at most 512 MiB and its extension
matches the declared resource family: raster alpha/texture, HDR/EXR environment,
SBSAR generator, SPSM smart material, SPMSK smart mask, or SPEXP export preset.

Use `create_export_preset` to build a bounded inline PNG preset with explicit
document-map channel routing. Pass that object to `export_textures` with one
or more exact texture-set names. The export succeeds only when Painter returns
`Success`, every returned file exists under the requested directory, and each
PNG resolution matches the selected texture sets. Existing Painter preset
URLs remain supported; their output files are checked for existence, but only
the typed inline PNG path provides resolution verification. Destination channels
must be disjoint; `RGB` and `RGB+A` are exclusive and cannot be mixed with scalar
R, G, B, or A destinations in the same output map.

`inspect_project` includes dirty/busy/editable state, texture-set and UV-tile
resolutions, channels, mesh-map resource URLs, and full layer trees so callers
can make deterministic before/after assertions. This is host state, not a
visual-quality judgement.

Mesh-map baking accepts one exact texture-set name and a bounded baker list per
call. It returns a core job ID immediately; poll `jobs_get_status` until the
native Painter event reports a terminal result. Painter owns this monolithic
operation. Core cancellation is forwarded to the native stop source; the job
does not report cancellation until Painter emits its terminal event. A
successful result includes the baked resource URLs and the texture-set
resolution read back from Painter.
