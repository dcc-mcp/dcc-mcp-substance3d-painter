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
material call in the same host session.

Use `create_export_preset` to build a bounded inline PNG preset with explicit
document-map channel routing. Pass that object to `export_textures` with one
or more exact texture-set names. The export succeeds only when Painter returns
`Success`, every returned file exists under the requested directory, and each
PNG resolution matches the selected texture sets. Existing Painter preset
URLs remain supported; their output files are checked for existence, but only
the typed inline PNG path provides resolution verification.

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
