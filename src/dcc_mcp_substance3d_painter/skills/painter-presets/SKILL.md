---
name: painter-presets
description: >-
  Control parametric Substance 3D Painter presets - generator (.sbsar) and
  smart-material (.spsm) instances. Use when listing available presets,
  inspecting their exposed parameters, or setting parameter values such as
  droplet density, weave scale, or roughness and confirming them by readback.
  Presets are provided by the host shelf or the caller, never bundled with the
  adapter. Not for importing files, applying a preset, or exporting textures -
  use painter-project for those steps.
license: MIT
compatibility: "Substance 3D Painter Python API; dcc-mcp-core 0.20.15+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: substance3d_painter
    version: "1.0.0"
    layer: domain
    stage: pipeline
    search-hint: "substance painter preset generator smart material sbsar spsm parameter droplets water weave texture randomness control readback"
    tags: "substance, painter, preset, generator, smart-material, sbsar, spsm, parameters, procedural, water, weave, texture"
    tools: tools.yaml
---

# Painter presets

A "preset" here is a **parametric resource instance** - a Substance 3D Designer
`.sbsar` generator or a Painter `.spsm` smart material that has been applied to
a texture-set stack. Effects such as water droplets, woven fabric, or grungy
textures are what those presets can produce once their exposed parameters are
tuned; the effect words are examples, not assets shipped with this adapter.
This skill never bundles any Adobe or third-party preset file. Presets come
from the running Painter shelf or from a caller-supplied file imported through
`painter_project__import_resource`.

Painter releases differ in which Python parameter controls they expose. This
skill probes a bounded set of host accessors and reports the resulting
capabilities rather than assuming an API that may be missing. Where a host
exposes no parameter write or no readback, the tool says so explicitly instead
of implying a change it could not verify.

## Workflow (a full authoring pass)

The tools here cover discovery and parameter control; the surrounding steps are
typed tools in `painter-project`, referenced from each tool's `next-tools`:

1. `painter_presets__list_preset_resources` - discover generator / smart-material
   presets on the shelf, in the project, or in the session (read-only).
2. `painter_project__import_resource` - import a caller-supplied `.sbsar` /
   `.spsm` file when it is not already on the shelf.
3. `painter_project__apply_smart_material` or
   `painter_project__add_generator_effect` - apply the preset, returning the
   node UID of the created instance.
4. `painter_presets__inspect_preset_parameters` - read the instance's exposed
   parameter names, current values, and host capabilities (read-only).
5. `painter_presets__set_preset_parameters` - set tuned parameter values and
   confirm each by host readback.
6. `painter_project__export_textures` - export the resulting texture set.

## Tools

- `list_preset_resources` - read-only discovery of preset resources with stable
  URLs. Only the `GENERATOR` and `SMART_MATERIAL` usage families are returned.
- `inspect_preset_parameters` - read-only inspection of one applied preset
  instance. Resolves a smart-material group to the generator effect that
  actually carries the parameters. Returns a `capabilities` map (`list`,
  `read`, `write`, `readback`) and a JSON-safe `name -> value` parameter map.
- `set_preset_parameters` - idempotent, bounded parameter write. Each value is
  validated before any host mutation (boolean, integer, finite number, short
  string, or a flat vector of up to four such scalars; at most 64 parameters).

## Verification contract

`set_preset_parameters` re-resolves the node after writing and reads each value
back through a fresh host handle. When the host exposes parameter readback and
every value matches, the result reports `verified=True` with a
`preset_parameter_readback` postcondition. A host with no readback reports
`verified=False` with a `write_accepted_only` postcondition; a readback that
disagrees fails with `HOST_READBACK_MISMATCH`. Treat `verified=False` as "write
accepted, value unconfirmed" and confirm by exporting the texture set. Parameter
values themselves are never proof of visual quality.
