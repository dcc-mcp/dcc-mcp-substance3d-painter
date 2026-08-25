---
name: painter-project
description: >-
  Host skill - inspect and author the current Substance 3D Painter project.
  Use when creating or checking projects, building uniform or texture-driven
  PBR layers, baking mesh maps, searching resources, applying smart materials, or exporting.
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
    search-hint: "substance painter create project mesh pbr textured fill bake mesh maps smart material resource texture sets export preset"
    tags: "substance, painter, textures, materials, layers, baking, mesh-maps, smart-material, resources, export, project"
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

Add typed uniform or texture-driven PBR fill layers, inspect or orbit the
viewport camera, apply smart materials, and review texture sets before
exporting. Exports require an explicit Painter preset URL to keep the
operation typed and reviewable.

Mesh-map baking accepts one exact texture-set name and a bounded baker list per
call. It returns a core job ID immediately; poll `jobs_get_status` until the
native Painter event reports a terminal result. Painter owns this monolithic
operation. Core cancellation is forwarded to the native stop source; the job
does not report cancellation until Painter emits its terminal event. A
successful result includes the baked resource URLs and the texture-set
resolution read back from Painter.
