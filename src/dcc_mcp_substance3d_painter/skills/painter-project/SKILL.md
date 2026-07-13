---
name: painter-project
description: >-
  Host skill - inspect and export the current Substance 3D Painter project.
  Use when checking texture sets or exporting textures with an explicit preset.
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
    search-hint: "substance painter project texture sets inspect export textures preset"
    tags: "substance, painter, textures, export, project"
    tools: tools.yaml
---

# Painter Project

Inspect a project and texture sets before exporting. Exports require an explicit
Painter preset URL to keep the operation typed and reviewable.
