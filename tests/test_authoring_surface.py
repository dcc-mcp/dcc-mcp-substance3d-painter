from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
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


def _install_smart_mask_host(
    monkeypatch, inserted_effects: list[object]
) -> tuple[ModuleType, ModuleType, object, MagicMock]:
    stack = object()
    state = {"masked": False, "effects": []}
    black = SimpleNamespace(name="Black")
    target = MagicMock()
    target.uid.return_value = 88
    target.get_stack.return_value = stack
    target.has_mask.side_effect = lambda: state["masked"]
    target.add_mask.side_effect = lambda _background: state.update(masked=True)
    target.remove_mask.side_effect = lambda: state.update(masked=False, effects=[])
    target.get_mask_background.return_value = black
    target.mask_effects.side_effect = lambda: list(state["effects"])
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
        state["effects"].extend(inserted_effects)
        return inserted_effects

    layerstack.insert_smart_mask = MagicMock(side_effect=insert_smart_mask)
    resource = ModuleType("substance_painter.resource")
    resource.ResourceID = SimpleNamespace(from_url=MagicMock(return_value=identifier))
    resource.Resource = SimpleNamespace(retrieve=MagicMock(return_value=[smart_mask]))
    _install(monkeypatch, project=project, textureset=textureset, layerstack=layerstack, resource=resource)
    return layerstack, resource, identifier, target


def _install_export_failure_host(monkeypatch, *, result=None, error: Exception | None = None) -> dict[str, object]:
    assert (result is None) != (error is None)
    stack = _Stack("Body")
    texture_set = SimpleNamespace(
        name=lambda: "Body",
        all_stacks=lambda: [stack],
        get_resolution=lambda: SimpleNamespace(width=2, height=2),
        has_uv_tiles=lambda: False,
    )
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = MagicMock(return_value=[texture_set])
    export = ModuleType("substance_painter.export")
    export.export_project_textures = (
        MagicMock(side_effect=error) if error is not None else MagicMock(return_value=result)
    )
    _install(monkeypatch, project=project, textureset=textureset, export=export)
    return _load("create_export_preset").main(
        name="benchmark-pbr",
        maps=[
            {
                "file_name": "$textureSet_BaseColor",
                "channels": [{"destination": "RGB", "source_channel": "RGB", "source_map": "baseColor"}],
            }
        ],
    )["context"]["preset"]


def test_smart_mask_validates_resource_and_reads_back_inserted_effects(monkeypatch) -> None:
    effect = MagicMock()
    effect.uid.return_value = 101
    effect.get_name.return_value = "Edge Wear"
    effect.get_type.return_value = SimpleNamespace(name="GeneratorEffect")
    effect.get_source.side_effect = RuntimeError("smart-mask effect sources are inspected separately")
    _layerstack, resource, identifier, _target = _install_smart_mask_host(monkeypatch, [effect])

    result = _load("add_mask").main(
        layer_uid=88,
        kind="smart",
        resource_url="resource://starter_assets/edge-wear",
    )

    assert result["success"] is True
    assert result["context"]["kind"] == "smart"
    assert result["context"]["effects"][0]["uid"] == 101
    resource.Resource.retrieve.assert_called_once_with(identifier)


def test_smart_mask_rejects_empty_host_insertion_and_confirms_cleanup(monkeypatch) -> None:
    _layerstack, _resource, _identifier, target = _install_smart_mask_host(monkeypatch, [])

    result = _load("add_mask").main(
        layer_uid=88,
        kind="smart",
        resource_url="resource://starter_assets/edge-wear",
    )

    assert result["success"] is False
    assert result["error"] == "HOST_SMART_MASK_INSERT_EMPTY"
    assert result["context"]["cleanup"] == {"attempted": True, "status": "confirmed"}
    target.remove_mask.assert_called_once_with()


def test_smart_mask_reports_partial_readback_and_unconfirmed_cleanup(monkeypatch) -> None:
    first = MagicMock()
    first.uid.return_value = 201
    second = MagicMock()
    second.uid.return_value = 202
    _layerstack, _resource, _identifier, target = _install_smart_mask_host(monkeypatch, [first, second])
    target.mask_effects.side_effect = lambda: [first]
    target.remove_mask.side_effect = RuntimeError("host cleanup status is unknown")

    result = _load("add_mask").main(
        layer_uid=88,
        kind="smart",
        resource_url="resource://starter_assets/edge-wear",
    )

    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_SMART_MASK_MISMATCH"
    assert result["context"]["cleanup"] == {"attempted": True, "status": "unconfirmed"}
    assert "rollback" not in str(result).lower()


def test_smart_mask_cleanup_oserror_preserves_the_original_failure(monkeypatch) -> None:
    _layerstack, _resource, _identifier, target = _install_smart_mask_host(monkeypatch, [])
    target.remove_mask.side_effect = OSError("P:/private/project.spp SECRET_CLEANUP_TOKEN")

    result = _load("add_mask").main(
        layer_uid=88,
        kind="smart",
        resource_url="resource://starter_assets/edge-wear",
    )

    assert result["success"] is False
    assert result["error"] == "HOST_SMART_MASK_INSERT_EMPTY"
    assert result["context"]["cleanup"] == {"attempted": True, "status": "unconfirmed"}
    assert "private" not in str(result).lower()
    assert "secret_cleanup_token" not in str(result).lower()


def test_smart_mask_cleanup_leaves_base_exceptions_to_the_skill_boundary(monkeypatch) -> None:
    _layerstack, _resource, _identifier, target = _install_smart_mask_host(monkeypatch, [])
    target.remove_mask.side_effect = KeyboardInterrupt()

    result = _load("add_mask").main(
        layer_uid=88,
        kind="smart",
        resource_url="resource://starter_assets/edge-wear",
    )

    assert result["success"] is False
    assert result["error"] == "interrupted"
    assert result["_meta"]["dcc.error"]["type"] == "KeyboardInterrupt"


def test_mask_host_errors_cannot_inject_public_error_codes(monkeypatch) -> None:
    _layerstack, _resource, _identifier, target = _install_smart_mask_host(monkeypatch, [])
    target.add_mask.side_effect = RuntimeError("HOST_SECRET_TOKEN")

    result = _load("add_mask").main(layer_uid=88, kind="black")

    assert result["success"] is False
    assert result["error"] == "PAINTER_MASK_OPERATION_FAILED"
    assert "secret" not in str(result).lower()


def test_mask_hostile_exception_args_cannot_escape_error_rendering(monkeypatch) -> None:
    class HostileRuntimeError(RuntimeError):
        def __getattribute__(self, name: str):
            if name == "args":
                raise OSError("P:/private/project.spp SECRET_ARGS_TOKEN")
            return super().__getattribute__(name)

    _layerstack, _resource, _identifier, target = _install_smart_mask_host(monkeypatch, [])
    target.add_mask.side_effect = HostileRuntimeError("HOST_READBACK_MASK_MISSING")

    result = _load("add_mask").main(layer_uid=88, kind="black")

    assert result["success"] is False
    assert result["error"] == "PAINTER_MASK_OPERATION_FAILED"
    assert result["context"]["cleanup"] == {"attempted": False, "status": "not_needed"}
    assert "private" not in str(result).lower()
    assert "secret_args_token" not in str(result).lower()


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


def test_import_resource_rejects_usage_extension_mismatch_before_host_io(monkeypatch, tmp_path) -> None:
    source = tmp_path / "texture.yaml"
    source.write_text("not: a texture", encoding="utf-8")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    resource = ModuleType("substance_painter.resource")
    resource.import_project_resource = MagicMock()
    _install(monkeypatch, project=project, resource=resource)

    result = _load("import_resource").main(file_path=str(source), usage="texture")

    assert result["success"] is False
    assert result["error"] == "RESOURCE_EXTENSION_MISMATCH"
    project.is_open.assert_not_called()
    resource.import_project_resource.assert_not_called()


@pytest.mark.parametrize(
    ("usage_name", "suffix"),
    [
        ("alpha", ".png"),
        ("texture", ".tiff"),
        ("environment", ".hdr"),
        ("environment", ".exr"),
        ("generator", ".sbsar"),
        ("smart_material", ".spsm"),
        ("smart_mask", ".spmsk"),
        ("export", ".spexp"),
    ],
)
def test_import_resource_accepts_only_the_declared_painter_resource_family(
    monkeypatch, tmp_path, usage_name: str, suffix: str
) -> None:
    source = tmp_path / f"resource{suffix}"
    source.write_bytes(b"bounded resource")
    enum_name = usage_name.upper()
    usage = SimpleNamespace(name=enum_name)
    identifier = SimpleNamespace(url=lambda: f"resource://project/{usage_name}")
    imported = SimpleNamespace(identifier=lambda: identifier, usages=lambda: [usage])
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    resource = ModuleType("substance_painter.resource")
    resource.Usage = SimpleNamespace(**{enum_name: usage})
    resource.import_project_resource = MagicMock(return_value=imported)
    resource.Resource = SimpleNamespace(retrieve=MagicMock(return_value=[imported]))
    _install(monkeypatch, project=project, resource=resource)

    result = _load("import_resource").main(file_path=str(source), usage=usage_name)

    assert result["success"] is True
    resource.import_project_resource.assert_called_once_with(
        str(source.resolve()),
        usage,
        name="resource",
        group=None,
    )


def test_import_resource_rejects_symlinks_before_host_io(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"texture")
    link = tmp_path / "linked.png"
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    resource = ModuleType("substance_painter.resource")
    resource.import_project_resource = MagicMock()
    _install(monkeypatch, project=project, resource=resource)

    result = _load("import_resource").main(file_path=str(link), usage="texture")

    assert result["success"] is False
    assert result["error"] == "RESOURCE_NOT_REGULAR_FILE"
    project.is_open.assert_not_called()
    resource.import_project_resource.assert_not_called()


def test_import_resource_enforces_the_exact_byte_limit_before_host_io(monkeypatch, tmp_path) -> None:
    source = tmp_path / "bounded.png"
    module = _load("import_resource")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    usage = SimpleNamespace(name="TEXTURE")
    identifier = SimpleNamespace(url=lambda: "resource://project/bounded")
    imported = SimpleNamespace(identifier=lambda: identifier, usages=lambda: [usage])
    resource = ModuleType("substance_painter.resource")
    resource.Usage = SimpleNamespace(TEXTURE=usage)
    resource.import_project_resource = MagicMock(return_value=imported)
    resource.Resource = SimpleNamespace(retrieve=MagicMock(return_value=[imported]))
    _install(monkeypatch, project=project, resource=resource)

    with source.open("wb") as stream:
        stream.truncate(module._MAX_RESOURCE_BYTES + 1)
    too_large = module.main(file_path=str(source), usage="texture")

    assert too_large["success"] is False
    assert too_large["error"] == "RESOURCE_SIZE_OUT_OF_RANGE"
    project.is_open.assert_not_called()
    resource.import_project_resource.assert_not_called()

    with source.open("r+b") as stream:
        stream.truncate(module._MAX_RESOURCE_BYTES)
    accepted = module.main(file_path=str(source), usage="texture")

    assert accepted["success"] is True
    resource.import_project_resource.assert_called_once()


def test_import_resource_does_not_expose_host_exception_details(monkeypatch, tmp_path) -> None:
    source = tmp_path / "safe.png"
    source.write_bytes(b"texture")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    resource = ModuleType("substance_painter.resource")
    resource.Usage = SimpleNamespace(TEXTURE=SimpleNamespace(name="TEXTURE"))
    resource.import_project_resource = MagicMock(side_effect=RuntimeError(r"C:\private\asset.png token=secret"))
    _install(monkeypatch, project=project, resource=resource)

    result = _load("import_resource").main(file_path=str(source), usage="texture")

    assert result["success"] is False
    assert result["error"] == "PAINTER_RESOURCE_IMPORT_READBACK_FAILED"
    assert "private" not in str(result).lower()
    assert "secret" not in str(result).lower()


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


@pytest.mark.parametrize(
    "destinations",
    [
        ["RGB", "R"],
        ["RGB", "G"],
        ["RGB", "B"],
        ["RGB", "A"],
        ["RGB+A", "R"],
        ["R", "RGB+A"],
    ],
)
def test_export_preset_rejects_overlapping_or_mixed_composite_destinations(destinations: list[str]) -> None:
    channels = [
        {"destination": destination, "source_channel": "R", "source_map": f"source{index}"}
        for index, destination in enumerate(destinations)
    ]

    result = _load("create_export_preset").main(
        name="packed-map",
        maps=[{"file_name": "$textureSet_Packed", "channels": channels}],
    )

    assert result["success"] is False
    assert result["error"] == "INVALID_EXPORT_PRESET"
    assert result["context"]["validation_error"] == "DESTINATION_CHANNELS_OVERLAP"


def test_export_preset_accepts_disjoint_scalar_destinations_and_one_composite() -> None:
    scalar = _load("create_export_preset").main(
        name="packed-scalars",
        maps=[
            {
                "file_name": "$textureSet_Packed",
                "channels": [
                    {"destination": channel, "source_channel": "R", "source_map": f"source{index}"}
                    for index, channel in enumerate(["R", "G", "B", "A"])
                ],
            }
        ],
    )
    composite = _load("create_export_preset").main(
        name="rgba-map",
        maps=[
            {
                "file_name": "$textureSet_RGBA",
                "channels": [{"destination": "RGB+A", "source_channel": "RGB+A", "source_map": "baseColor"}],
            }
        ],
    )

    assert scalar["success"] is True
    assert composite["success"] is True


def test_authoring_schema_publishes_import_and_destination_limits() -> None:
    tools = {item["name"]: item for item in yaml.safe_load(TOOLS.read_text(encoding="utf-8"))["tools"]}
    import_schema = tools["import_resource"]["input_schema"]["properties"]
    channel_schema = tools["create_export_preset"]["input_schema"]["properties"]["maps"]["items"]["properties"][
        "channels"
    ]

    assert "512 MiB" in import_schema["file_path"]["description"]
    assert ".sbsar" in import_schema["usage"]["description"]
    assert "non-overlapping" in channel_schema["description"]
    assert channel_schema["items"]["properties"]["destination"]["enum"] == ["R", "G", "B", "A", "RGB", "RGB+A"]


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


def test_export_host_oserror_cannot_leak_paths_or_tokens(monkeypatch, tmp_path) -> None:
    preset = _install_export_failure_host(
        monkeypatch,
        error=OSError("P:/private/export SECRET_EXPORT_TOKEN\nTraceback: host detail"),
    )

    result = _load("export_textures").main(export_path=str(tmp_path / "textures"), preset=preset)

    assert result["success"] is False
    assert result["error"] == "PAINTER_TEXTURE_EXPORT_FAILED"
    assert "private" not in str(result).lower()
    assert "secret_export_token" not in str(result).lower()
    assert "traceback" not in str(result).lower()


def test_export_host_status_and_message_cannot_become_public_details(monkeypatch, tmp_path) -> None:
    preset = _install_export_failure_host(
        monkeypatch,
        result=SimpleNamespace(
            status=SimpleNamespace(name="Failed SECRET_STATUS_TOKEN"),
            message="P:/private/export SECRET_MESSAGE_TOKEN\nTraceback: host detail",
            textures={},
        ),
    )

    result = _load("export_textures").main(export_path=str(tmp_path / "textures"), preset=preset)

    assert result["success"] is False
    assert result["error"] == "HOST_EXPORT_FAILED"
    assert "private" not in str(result).lower()
    assert "secret_status_token" not in str(result).lower()
    assert "secret_message_token" not in str(result).lower()
    assert "traceback" not in str(result).lower()


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
