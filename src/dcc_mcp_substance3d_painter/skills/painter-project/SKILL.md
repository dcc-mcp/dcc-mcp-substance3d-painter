---
name: painter-project
description: >-
  Host skill - inspect and author the current Substance 3D Painter project.
  Use when checking texture sets, building PBR layers, searching resources,
  applying smart materials, or exporting textures with an explicit preset.
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
    search-hint: "substance painter project pbr fill smart material resource texture sets export preset"
    tags: "substance, painter, textures, materials, layers, smart-material, resources, export, project"
    tools: tools.yaml
---

# Painter Project

Inspect a project, create typed PBR fill layers, search shelf resources, apply
smart materials, list export presets, save the project to an explicit `.spp`
path, and review texture sets before exporting. Exports require an explicit
Painter preset URL to keep the operation typed and reviewable.
