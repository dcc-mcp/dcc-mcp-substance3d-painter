from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import yaml

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project" / "scripts"
TOOLS = ROOT / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project" / "tools.yaml"


class _Stack:
    def __init__(self, root_path: str, name: str = "") -> None:
        self.root_path = root_path
        self._name = name

    def __str__(self) -> str:
        return self.root_path

    def name(self) -> str:
        return self._name


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(monkeypatch, **modules: ModuleType) -> None:
    painter = ModuleType("substance_painter")
    for name, module in modules.items():
        setattr(painter, name, module)
        monkeypatch.setitem(sys.modules, f"substance_painter.{name}", module)
    monkeypatch.setitem(sys.modules, "substance_painter", painter)


def _png(path: Path, width: int, height: int) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\0" + (b"\0\0\0" * width) for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_authoring_tools_are_typed_main_thread_declarations() -> None:
    tools = {item["name"]: item for item in yaml.safe_load(TOOLS.read_text(encoding="utf-8"))["tools"]}

    for name in (
        "insert_paint_layer",
        "add_mask",
        "add_generator_effect",
        "list_layer_stack",
        "import_resource",
        "create_export_preset",
    ):
        assert tools[name]["execution"] == "async"
        assert tools[name]["affinity"] == "main"
        assert tools[name]["input_schema"]["additionalProperties"] is False


def test_insert_paint_layer_uses_typed_position_and_requires_host_readback(monkeypatch) -> None:
    stack = SimpleNamespace(name=lambda: "Body")
    node = MagicMock()
    node.uid.return_value = 41
    node.get_name.return_value = "Detail Paint"
    node.get_type.return_value = SimpleNamespace(name="PaintLayer")
    node.get_stack.return_value = stack

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.InsertPosition = SimpleNamespace(from_textureset_stack=MagicMock(return_value="top"))
    layerstack.insert_paint = MagicMock(return_value=node)
    layerstack.get_node_by_uid = MagicMock(return_value=[node])
    _install(monkeypatch, project=project, textureset=textureset, layerstack=layerstack)

    result = _load("insert_paint_layer").main(name="Detail Paint", placement="top")

    assert result["success"] is True
    assert result["context"]["node"] == {"uid": 41, "name": "Detail Paint", "type": "PaintLayer"}
    layerstack.InsertPosition.from_textureset_stack.assert_called_once_with(stack)
    layerstack.insert_paint.assert_called_once_with("top")
    layerstack.get_node_by_uid.assert_called_once_with(41)

    layerstack.get_node_by_uid.return_value = []
    failed = _load("insert_paint_layer").main(name="Unconfirmed", placement="top")
    assert failed["success"] is False
    assert "readback" in failed["error"].lower()


def test_fill_layer_requires_same_stack_host_readback(monkeypatch) -> None:
    stack = object()
    layer = MagicMock()
    layer.uid.return_value = 73
    layer.get_name.return_value = "Verified Fill"
    layer.get_type.return_value = SimpleNamespace(name="FillLayer")
    layer.get_stack.return_value = stack
    channel_type = SimpleNamespace(BaseColor="base", Metallic="metal", Roughness="rough")

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    textureset.ChannelType = channel_type
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.InsertPosition = SimpleNamespace(from_textureset_stack=MagicMock(return_value="top"))
    layerstack.insert_fill = MagicMock(return_value=layer)
    layerstack.get_node_by_uid = MagicMock(return_value=[])
    colormanagement = ModuleType("substance_painter.colormanagement")
    colormanagement.Color = MagicMock(side_effect=lambda red, green, blue: (red, green, blue))
    _install(
        monkeypatch,
        project=project,
        textureset=textureset,
        layerstack=layerstack,
        colormanagement=colormanagement,
    )

    result = _load("create_pbr_fill_layer").main(name="Verified Fill", base_color=[0.1, 0.2, 0.3])

    assert result["success"] is False
    assert "readback" in result["error"].lower()


def test_mask_generator_and_layer_tree_share_the_same_host_readback(monkeypatch) -> None:
    stack = SimpleNamespace(name=lambda: "Body")
    mask_state = {"enabled": False, "background": None, "effects": []}
    black = SimpleNamespace(name="Black")
    white = SimpleNamespace(name="White")

    layer = MagicMock()
    layer.uid.return_value = 17
    layer.get_name.return_value = "Steel"
    layer.get_type.return_value = SimpleNamespace(name="FillLayer")
    layer.get_stack.return_value = stack
    layer.is_visible.return_value = True
    layer.has_mask.side_effect = lambda: mask_state["enabled"]
    layer.add_mask.side_effect = lambda background: mask_state.update(enabled=True, background=background)
    layer.get_mask_background.side_effect = lambda: mask_state["background"]
    layer.is_mask_enabled.return_value = True
    layer.mask_effects.side_effect = lambda: list(mask_state["effects"])
    layer.content_effects.return_value = []

    resource_id = SimpleNamespace(url=lambda: "resource://project/grunge")
    imported_resource = SimpleNamespace(
        identifier=lambda: resource_id, usages=lambda: [SimpleNamespace(name="GENERATOR")]
    )
    source = SimpleNamespace(resource_id=resource_id)
    effect = MagicMock()
    effect.uid.return_value = 99
    effect.get_name.return_value = "Grunge"
    effect.get_type.return_value = SimpleNamespace(name="GeneratorEffect")
    effect.get_source.return_value = source

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.MaskBackground = SimpleNamespace(Black=black, White=white)
    layerstack.NodeStack = SimpleNamespace(Mask="mask", Content="content", Substack="substack")
    layerstack.InsertPosition = SimpleNamespace(inside_node=MagicMock(return_value="inside-mask"))
    layerstack.get_node_by_uid = MagicMock(side_effect=lambda uid: [layer] if uid == 17 else [effect])
    layerstack.get_root_layer_nodes = MagicMock(return_value=[layer])

    def insert_generator(position, identifier):
        assert position == "inside-mask"
        assert identifier is resource_id
        mask_state["effects"].append(effect)
        return effect

    layerstack.insert_generator_effect = MagicMock(side_effect=insert_generator)
    resource = ModuleType("substance_painter.resource")
    resource.ResourceID = SimpleNamespace(from_url=MagicMock(return_value=resource_id))
    resource.Resource = SimpleNamespace(retrieve=MagicMock(return_value=[imported_resource]))
    _install(monkeypatch, project=project, textureset=textureset, layerstack=layerstack, resource=resource)

    masked = _load("add_mask").main(layer_uid=17, kind="black")
    generated = _load("add_generator_effect").main(
        target_uid=17,
        target_stack="mask",
        resource_url="resource://project/grunge",
    )
    listed = _load("list_layer_stack").main()

    assert masked["success"] is True
    assert generated["success"] is True
    assert listed["success"] is True
    assert listed["context"]["layers"] == [
        {
            "uid": 17,
            "name": "Steel",
            "type": "FillLayer",
            "visible": True,
            "mask": {
                "enabled": True,
                "background": "Black",
                "effects": [
                    {
                        "uid": 99,
                        "name": "Grunge",
                        "type": "GeneratorEffect",
                        "resource_url": "resource://project/grunge",
                    }
                ],
            },
            "content_effects": [],
            "children": [],
        }
    ]


def test_smart_mask_validates_resource_and_reads_back_inserted_effects(monkeypatch) -> None:
    stack = object()
    state = {"masked": False, "effects": []}
    black = SimpleNamespace(name="Black")
    target = MagicMock()
    target.uid.return_value = 88
    target.get_stack.return_value = stack
    target.has_mask.side_effect = lambda: state["masked"]
    target.add_mask.side_effect = lambda _background: state.update(masked=True)
    target.get_mask_background.return_value = black
    target.mask_effects.side_effect = lambda: list(state["effects"])
    effect = MagicMock()
    effect.uid.return_value = 101
    effect.get_name.return_value = "Edge Wear"
    effect.get_type.return_value = SimpleNamespace(name="GeneratorEffect")
    effect.get_source.side_effect = RuntimeError("smart-mask effect sources are inspected separately")

    identifier = SimpleNamespace(url=lambda: "resource://starter_assets/edge-wear")
    smart_mask = SimpleNamespace(
        identifier=lambda: identifier,
        usages=lambda: [SimpleNamespace(name="SMART_MASK")],
    )
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.MaskBackground = SimpleNamespace(Black=black)
    layerstack.NodeStack = SimpleNamespace(Mask="mask")
    layerstack.InsertPosition = SimpleNamespace(inside_node=MagicMock(return_value="inside-mask"))
    layerstack.get_node_by_uid = MagicMock(return_value=[target])

    def insert_smart_mask(position, resource_id):
        assert position == "inside-mask"
        assert resource_id is identifier
        state["effects"].append(effect)
        return [effect]

    layerstack.insert_smart_mask = MagicMock(side_effect=insert_smart_mask)
    resource = ModuleType("substance_painter.resource")
    resource.ResourceID = SimpleNamespace(from_url=MagicMock(return_value=identifier))
    resource.Resource = SimpleNamespace(retrieve=MagicMock(return_value=[smart_mask]))
    _install(monkeypatch, project=project, textureset=textureset, layerstack=layerstack, resource=resource)

    result = _load("add_mask").main(
        layer_uid=88,
        kind="smart",
        resource_url="resource://starter_assets/edge-wear",
    )

    assert result["success"] is True
    assert result["context"]["kind"] == "smart"
    assert result["context"]["effects"][0]["uid"] == 101
    resource.Resource.retrieve.assert_called_once_with(identifier)


def test_import_resource_is_bounded_and_requires_resource_catalog_readback(monkeypatch, tmp_path) -> None:
    source = tmp_path / "grunge.png"
    source.write_bytes(b"texture")
    usage = SimpleNamespace(name="TEXTURE")
    identifier = SimpleNamespace(url=lambda: "resource://project/grunge")
    imported = SimpleNamespace(identifier=lambda: identifier, usages=lambda: [usage])

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    resource = ModuleType("substance_painter.resource")
    resource.Usage = SimpleNamespace(TEXTURE=usage)
    resource.import_project_resource = MagicMock(return_value=imported)
    resource.Resource = SimpleNamespace(retrieve=MagicMock(return_value=[imported]))
    _install(monkeypatch, project=project, resource=resource)

    result = _load("import_resource").main(file_path=str(source), usage="texture", name="grunge", group="dcc-mcp")

    assert result["success"] is True
    assert result["context"]["resource"] == {
        "name": "grunge",
        "url": "resource://project/grunge",
        "usage": "TEXTURE",
        "group": "dcc-mcp",
    }
    resource.Resource.retrieve.assert_called_once_with(identifier)

    resource.Resource.retrieve.return_value = []
    failed = _load("import_resource").main(file_path=str(source), usage="texture", name="missing")
    assert failed["success"] is False
    assert "readback" in failed["error"].lower()


def test_export_preset_builds_bounded_painter_config() -> None:
    result = _load("create_export_preset").main(
        name="benchmark-pbr",
        maps=[
            {
                "file_name": "$textureSet_BaseColor",
                "channels": [
                    {
                        "destination": "RGB",
                        "source_channel": "RGB",
                        "source_map": "baseColor",
                    }
                ],
            }
        ],
        bit_depth=8,
        dithering=True,
    )

    assert result["success"] is True
    assert result["context"]["preset"] == {
        "name": "benchmark-pbr",
        "maps": [
            {
                "fileName": "$textureSet_BaseColor",
                "channels": [
                    {
                        "destChannel": "RGB",
                        "srcChannel": "RGB",
                        "srcMapType": "documentMap",
                        "srcMapName": "baseColor",
                    }
                ],
                "parameters": {"fileFormat": "png", "bitDepth": "8", "dithering": True},
            }
        ],
    }


def test_selective_export_verifies_files_and_png_resolution(monkeypatch, tmp_path) -> None:
    output = tmp_path / "textures"
    body_file = output / "Body_BaseColor.png"
    body_stack = _Stack("Body")
    glass_stack = _Stack("Glass")
    body = SimpleNamespace(
        name=lambda: "Body",
        all_stacks=lambda: [body_stack],
        get_resolution=lambda: SimpleNamespace(width=2, height=2),
        has_uv_tiles=lambda: False,
    )
    glass = SimpleNamespace(
        name=lambda: "Glass",
        all_stacks=lambda: [glass_stack],
        get_resolution=lambda: SimpleNamespace(width=4, height=4),
        has_uv_tiles=lambda: False,
    )
    success_status = SimpleNamespace(name="Success")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = MagicMock(return_value=[body, glass])
    export = ModuleType("substance_painter.export")

    def export_selected(config):
        output.mkdir(parents=True, exist_ok=True)
        _png(body_file, 2, 2)
        return SimpleNamespace(status=success_status, message="", textures={("Body", "1001"): [str(body_file)]})

    export.export_project_textures = MagicMock(side_effect=export_selected)
    _install(monkeypatch, project=project, textureset=textureset, export=export)
    preset = _load("create_export_preset").main(
        name="benchmark-pbr",
        maps=[
            {
                "file_name": "$textureSet_BaseColor",
                "channels": [{"destination": "RGB", "source_channel": "RGB", "source_map": "baseColor"}],
            }
        ],
    )["context"]["preset"]

    result = _load("export_textures").main(
        export_path=str(output), preset=preset, texture_sets=["Body"], padding_algorithm="infinite"
    )

    assert result["success"] is True
    assert result["context"]["verified_files"] == [{"path": str(body_file.resolve()), "width": 2, "height": 2}]
    config = export.export_project_textures.call_args.args[0]
    assert config["exportList"] == [{"rootPath": "Body"}]
    assert config["exportPresets"] == [preset]
    assert config["defaultExportPreset"] == "benchmark-pbr"

    export.export_project_textures.side_effect = lambda _config: (
        _png(body_file, 1, 1)
        or SimpleNamespace(status=success_status, message="", textures={("Body", "1001"): [str(body_file)]})
    )
    failed = _load("export_textures").main(
        export_path=str(output), preset=preset, texture_sets=["Body"], padding_algorithm="infinite"
    )
    assert failed["success"] is False
    assert "resolution" in failed["error"].lower()


def test_inspect_project_returns_diffable_texture_and_layer_state(monkeypatch) -> None:
    stack = _Stack("Body")
    channel_type = type("EnumValue", (), {"name": "BaseColor"})()
    channel = SimpleNamespace(
        label=lambda: "Base Color",
        format=lambda: SimpleNamespace(name="sRGB8"),
        bit_depth=lambda: 8,
        is_color=lambda: True,
    )
    mesh_usage = SimpleNamespace(name="AmbientOcclusion")
    mesh_map = SimpleNamespace(url=lambda: "resource://project/ao")
    texture_set = SimpleNamespace(
        name=lambda: "Body",
        get_resolution=lambda: SimpleNamespace(width=2048, height=2048),
        has_uv_tiles=lambda: False,
        all_uv_tiles=lambda: [],
        all_stacks=lambda: [stack],
        all_mesh_names=lambda: ["Rotor"],
        get_mesh_map_resource=lambda usage: mesh_map if usage is mesh_usage else None,
    )
    layer = MagicMock()
    layer.uid.return_value = 17
    layer.get_name.return_value = "Steel"
    layer.get_type.return_value = SimpleNamespace(name="FillLayer")
    layer.is_visible.return_value = True
    layer.has_mask.return_value = False
    layer.content_effects.return_value = []

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    project.file_path = MagicMock(return_value="P:/benchmark/rotor.spp")
    project.name = MagicMock(return_value="rotor")
    project.needs_saving = MagicMock(return_value=True)
    project.is_busy = MagicMock(return_value=False)
    project.is_in_edition_state = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = MagicMock(return_value=[texture_set])
    textureset.MeshMapUsage = SimpleNamespace(__members__={"AmbientOcclusion": mesh_usage})
    stack.all_channels = MagicMock(return_value={channel_type: channel})
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.get_root_layer_nodes = MagicMock(return_value=[layer])
    display = ModuleType("substance_painter.display")
    display.get_tone_mapping = MagicMock(return_value="ACES")
    colormanagement = ModuleType("substance_painter.colormanagement")
    _install(
        monkeypatch,
        project=project,
        textureset=textureset,
        layerstack=layerstack,
        display=display,
        colormanagement=colormanagement,
    )

    result = _load("inspect_project").main()

    assert result["success"] is True
    assert result["context"]["dirty"] is True
    assert result["context"]["texture_sets"] == [
        {
            "name": "Body",
            "resolution": {"width": 2048, "height": 2048},
            "mesh_names": ["Rotor"],
            "uv_tiles": [],
            "mesh_maps": {"AmbientOcclusion": "resource://project/ao"},
            "stacks": [
                {
                    "name": "Body",
                    "channels": [
                        {
                            "type": "BaseColor",
                            "label": "Base Color",
                            "format": "sRGB8",
                            "bit_depth": 8,
                            "is_color": True,
                        }
                    ],
                    "layers": [
                        {
                            "uid": 17,
                            "name": "Steel",
                            "type": "FillLayer",
                            "visible": True,
                            "mask": None,
                            "content_effects": [],
                            "children": [],
                        }
                    ],
                }
            ],
        }
    ]
