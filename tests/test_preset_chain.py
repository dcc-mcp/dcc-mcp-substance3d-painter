"""Full-chain integration test for the painter-presets authoring pass.

Drives the reused ``painter-project`` tools and the new ``painter-presets``
parameter tools against one shared in-memory fake of Painter's Python API. It
proves the caller-provided ``.sbsar`` chain: discover -> import -> apply ->
inspect/set parameters -> export, with parameter changes readable back (host
readback) at every mutating step. No real Painter or third-party asset is used;
the imported ``.sbsar`` is a self-authored temporary fixture.

The fake models the real two-layer API: a layer-stack ``GeneratorEffectNode``
exposes ``get_source()`` returning a ``SourceSubstance``, and that source owns
``get_parameters()`` / ``set_parameters()``. Parameters are never on the node.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

PROJECT_SCRIPTS = ROOT / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project" / "scripts"
PRESET_SCRIPTS = ROOT / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-presets" / "scripts"

GEN_URL = "resource://shelf/generators/water_droplets.sbsar"
EXPORT_URL = "resource://preset/pbr_metallic_roughness"


def _load(directory: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"chain_{name}", directory / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Stack:
    def __init__(self, name: str = "body") -> None:
        self._name = name

    def __str__(self) -> str:
        return f"/texture_set/{self._name}"

    def name(self) -> str:
        return self._name


class _Identifier:
    def __init__(self, url: str, name: str = "water_droplets") -> None:
        self._url = url
        self.name = name

    def url(self) -> str:
        return self._url


class _ResourceItem:
    def __init__(self, url: str, usage: str, name: str = "Water Droplets") -> None:
        self._id = _Identifier(url, name)
        self._usage = usage
        self._name = name

    def identifier(self) -> _Identifier:
        return self._id

    def usages(self):
        return [SimpleNamespace(name=self._usage)]

    def gui_name(self) -> str:
        return self._name

    def category(self) -> str:
        return "procedural"

    def type(self) -> SimpleNamespace:
        return SimpleNamespace(name=self._usage)


class _SourceSubstance:
    """Parameter host behind ``get_source()``: owns the parameter interface."""

    def __init__(self, params: dict) -> None:
        self.params = dict(params)

    def get_parameters(self):
        return dict(self.params)

    def get_parameter(self, name):
        return self.params[name]

    def set_parameter(self, name, value):
        self.params[name] = value

    def set_parameters(self, mapping):
        self.params.update(mapping)


class _PresetNode:
    """The applied preset instance: a generator effect node with a source."""

    def __init__(self, uid: int, stack: _Stack, source: _SourceSubstance) -> None:
        self._uid = uid
        self._stack = stack
        self._source = source
        self._name = "Water Droplets"

    def uid(self) -> int:
        return self._uid

    def get_name(self) -> str:
        return self._name

    def set_name(self, name: str) -> None:
        self._name = name

    def get_type(self) -> SimpleNamespace:
        return SimpleNamespace(name="Generator")

    def get_stack(self) -> _Stack:
        return self._stack

    def get_source(self):
        return self._source

    def sub_layers(self):
        return []

    def content_effects(self):
        return []

    def mask_effects(self):
        return []


class _TextureSet:
    def __init__(self, stack: _Stack) -> None:
        self._stack = stack

    def name(self) -> str:
        return "body"

    def all_stacks(self):
        return [self._stack]

    def has_uv_tiles(self):
        return False

    def get_resolution(self):
        return SimpleNamespace(width=4, height=4)


class _FakeHost:
    """One coherent, stateful fake of the substance_painter modules."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.stack = _Stack()
        self.source = _SourceSubstance(
            {"droplet_density": 0.4, "weave_scale": 2.0, "random_seed": 7},
        )
        self.node = _PresetNode(42, self.stack, self.source)
        self.nodes = {self.node.uid(): self.node}
        self.resources = [
            _ResourceItem(GEN_URL, "GENERATOR"),
            _ResourceItem(EXPORT_URL, "EXPORT"),
        ]
        self.exported_file = tmp_path / "export" / "body_basecolor.png"
        self._build_modules()

    def _build_modules(self) -> None:
        host = self

        # --- project ---
        project = ModuleType("substance_painter.project")
        project.is_open = MagicMock(return_value=True)

        # --- textureset ---
        textureset = ModuleType("substance_painter.textureset")
        textureset.get_active_stack = MagicMock(return_value=host.stack)
        textureset.all_texture_sets = MagicMock(return_value=[_TextureSet(host.stack)])

        # --- resource ---
        resource = ModuleType("substance_painter.resource")
        resource.Usage = SimpleNamespace(
            GENERATOR="GENERATOR",
            SMART_MATERIAL="SMART_MATERIAL",
            EXPORT="EXPORT",
            TEXTURE="TEXTURE",
        )

        def _search(query):
            needle = str(query).casefold()
            return [r for r in host.resources if not needle or needle in r.gui_name().casefold()]

        def _from_url(url):
            return _Identifier(str(url))

        def _import_project_resource(path, usage, name=None, group=None):
            # Import a caller-provided file; register a resource under a stable
            # URL so apply/validate_resource readback can find it.
            item = _ResourceItem(GEN_URL, "GENERATOR", name or Path(path).stem)
            host.resources = [r for r in host.resources if r.identifier().url() != GEN_URL]
            host.resources.insert(0, item)
            return item

        def _retrieve(identifier):
            return [r for r in host.resources if r.identifier().url() == str(identifier.url())]

        resource.search = MagicMock(side_effect=_search)
        resource.ResourceID = SimpleNamespace(from_url=MagicMock(side_effect=_from_url))
        resource.import_project_resource = MagicMock(side_effect=_import_project_resource)
        resource.Resource = SimpleNamespace(retrieve=MagicMock(side_effect=_retrieve))

        # --- layerstack ---
        layerstack = ModuleType("substance_painter.layerstack")
        layerstack.InsertPosition = SimpleNamespace(
            from_textureset_stack=MagicMock(return_value="top-position"),
        )
        layerstack.NodeStack = SimpleNamespace(Content="content", Mask="mask")

        def _insert_smart_material(position, identifier):
            return host.node

        def _get_node_by_uid(uid):
            return host.nodes.get(int(uid))

        layerstack.insert_smart_material = MagicMock(side_effect=_insert_smart_material)
        layerstack.get_node_by_uid = MagicMock(side_effect=_get_node_by_uid)
        layerstack.get_root_layer_nodes = MagicMock(return_value=[host.node])

        # --- export ---
        def _export_project_textures(config):
            host.exported_file.parent.mkdir(parents=True, exist_ok=True)
            # Minimal valid-ish 1x1 PNG bytes; the preset_url path only needs
            # the file to exist under the requested output directory.
            host.exported_file.write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 8 + b"\x08\x02\x00\x00\x00"
            )
            return SimpleNamespace(
                status=SimpleNamespace(name="Success"),
                textures={("body",): [str(host.exported_file)]},
            )

        export = ModuleType("substance_painter.export")
        export.export_project_textures = MagicMock(side_effect=_export_project_textures)

        self.modules = {
            "project": project,
            "textureset": textureset,
            "resource": resource,
            "layerstack": layerstack,
            "export": export,
        }

    def install(self, monkeypatch) -> None:
        painter = ModuleType("substance_painter")
        for name, module in self.modules.items():
            setattr(painter, name, module)
            monkeypatch.setitem(sys.modules, f"substance_painter.{name}", module)
        monkeypatch.setitem(sys.modules, "substance_painter", painter)


def test_full_preset_authoring_chain_with_parameter_readback(monkeypatch, tmp_path):
    host = _FakeHost(tmp_path)
    host.install(monkeypatch)

    # 1. Discover preset resources (new skill).
    listed = _load(PRESET_SCRIPTS, "list_preset_resources").main(query="droplet")
    assert listed["success"] is True
    assert GEN_URL in [r["url"] for r in listed["context"]["resources"]]

    # 2. Import a caller-provided .sbsar file (reused painter-project tool).
    sbsar = tmp_path / "water_droplets.sbsar"
    sbsar.write_bytes(b"self-authored test fixture - not a real Adobe asset")
    imported = _load(PROJECT_SCRIPTS, "import_resource").main(
        file_path=str(sbsar), usage="generator", name="Water Droplets"
    )
    assert imported["success"] is True, imported
    assert imported["context"]["resource"]["url"] == GEN_URL

    # 3. Apply the smart material / generator (reused painter-project tool).
    applied = _load(PROJECT_SCRIPTS, "apply_smart_material").main(resource_url=GEN_URL)
    assert applied["success"] is True, applied
    node_uid = applied["context"]["layer_uid"]
    assert node_uid == host.node.uid()

    # 4. Inspect exposed parameters (new skill) — read from get_source().
    inspected = _load(PRESET_SCRIPTS, "inspect_preset_parameters").main(node_uid=node_uid)
    assert inspected["success"] is True, inspected
    assert inspected["context"]["parameters"]["droplet_density"] == 0.4
    assert inspected["context"]["capabilities"]["write"] is True

    # 5. Set tuned parameters with verified host readback (new skill).
    tuned = _load(PRESET_SCRIPTS, "set_preset_parameters").main(
        node_uid=node_uid,
        parameters={"droplet_density": 0.85, "weave_scale": 3.0, "random_seed": 21},
    )
    assert tuned["success"] is True, tuned
    assert tuned["postcondition"]["verified"] is True
    assert tuned["context"]["observed"]["droplet_density"] == 0.85

    # 6. Parameter change is readable back through a fresh inspection.
    readback = _load(PRESET_SCRIPTS, "inspect_preset_parameters").main(node_uid=node_uid)
    assert readback["success"] is True, readback
    assert readback["context"]["parameters"]["droplet_density"] == 0.85
    assert readback["context"]["parameters"]["weave_scale"] == 3.0
    assert readback["context"]["parameters"]["random_seed"] == 21

    # 7. Export the texture set with host readback (reused painter-project tool).
    exported = _load(PROJECT_SCRIPTS, "export_textures").main(
        export_path=str(tmp_path / "export"),
        preset_url=EXPORT_URL,
        texture_sets=["body"],
    )
    assert exported["success"] is True, exported
    assert host.exported_file.is_file()
    assert exported["context"]["texture_sets"] == ["body"]


def test_chain_bool_parameter_is_coerced_to_int(monkeypatch, tmp_path):
    # P2-BOOL over the full chain: a boolean parameter reaches the source as an
    # int (SourceSubstance rejects bool) and is verified by int readback.
    host = _FakeHost(tmp_path)
    host.install(monkeypatch)

    applied = _load(PROJECT_SCRIPTS, "apply_smart_material").main(resource_url=GEN_URL)
    node_uid = applied["context"]["layer_uid"]

    tuned = _load(PRESET_SCRIPTS, "set_preset_parameters").main(
        node_uid=node_uid, parameters={"droplet_density": 0.85, "enabled": True}
    )
    assert tuned["success"] is True, tuned
    assert tuned["postcondition"]["verified"] is True
    assert host.source.params["enabled"] == 1
    assert not isinstance(host.source.params["enabled"], bool)
