---
name: painter-project
description: >-
  Host skill - inspect and author the current Substance 3D Painter project.
  Use when creating or checking projects, building uniform or texture-driven
  PBR layers, baking mesh maps, searching resources, applying smart materials, or exporting.
  Not for arbitrary JavaScript execution.
license: MIT
compatibility: "Substance 3D Painter Python API; dcc-mcp-core 0.19+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: substance3d_painter
    version: "0.0.0"
    layer: domain
    stage: pipeline
    search-hint: "substance painter create project mesh pbr textured fill bake mesh maps smart material resource texture sets export preset"
    tags: "substance, painter, textures, materials, layers, baking, mesh-maps, smart-material, resources, export, project"
    tools: tools.yaml
---

# Painter Project

Create or inspect a project, add typed uniform or texture-driven PBR fill
layers, inspect or orbit the viewport camera, apply smart materials, save to an explicit `.spp`
path, and review texture sets before exporting. Exports require an explicit
Painter preset URL to keep the operation typed and reviewable.

Mesh-map baking accepts one exact texture-set name and a bounded baker list per
call. It returns a core job ID immediately; poll `jobs_get_status` until the
native Painter event reports a terminal result. Painter owns this monolithic
operation, so the tool does not advertise mid-bake cancellation.
