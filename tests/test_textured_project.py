from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call

from dcc_mcp_core import DeferredToolResult

SCRIPTS = (
    Path(__file__).parent.parent / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project" / "scripts"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_project_uses_explicit_painter_settings(tmp_path, monkeypatch):
    mesh = tmp_path / "weapon.fbx"
    mesh.write_bytes(b"fbx")
    settings = MagicMock(return_value="settings")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(side_effect=[False, True])
    project.Settings = settings
    project.NormalMapFormat = SimpleNamespace(DirectX="dx", OpenGL="gl")
    project.ProjectWorkflow = SimpleNamespace(Default="default", TextureSetPerUVTile="legacy", UVTile="uv-tile")
    project.create = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    result = _load("create_project").main(
        mesh_path=str(mesh),
        project_path=str(tmp_path / "weapon.spp"),
        resolution=1024,
        normal_map_format="DirectX",
    )

    assert result["success"] is True
    settings.assert_called_once_with(
        default_save_path=str((tmp_path / "weapon.spp").resolve()),
        normal_map_format="dx",
        project_workflow="default",
        default_texture_resolution=1024,
    )
    project.create.assert_called_once_with(str(mesh.resolve()), settings="settings")


def test_create_project_fails_closed_when_host_readback_stays_closed(tmp_path, monkeypatch):
    mesh = tmp_path / "weapon.fbx"
    mesh.write_bytes(b"fbx")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=False)
    project.Settings = MagicMock(return_value="settings")
    project.NormalMapFormat = SimpleNamespace(DirectX="dx", OpenGL="gl")
    project.ProjectWorkflow = SimpleNamespace(Default="default", TextureSetPerUVTile="legacy", UVTile="uv-tile")
    project.create = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    result = _load("create_project").main(
        mesh_path=str(mesh),
        project_path=str(tmp_path / "weapon.spp"),
    )

    assert result["success"] is False
    assert "readback" in result["message"].lower()


def test_create_project_applies_template_and_uv_workflow(tmp_path, monkeypatch):
    mesh = tmp_path / "weapon.fbx"
    template = tmp_path / "pbr.spt"
    mesh.write_bytes(b"fbx")
    template.write_bytes(b"spt")
    settings = MagicMock(return_value="settings")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(side_effect=[False, True])
    project.Settings = settings
    project.NormalMapFormat = SimpleNamespace(DirectX="dx", OpenGL="gl")
    project.ProjectWorkflow = SimpleNamespace(Default="default", TextureSetPerUVTile="legacy", UVTile="uv-tile")
    project.create = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    result = _load("create_project").main(
        mesh_path=str(mesh),
        project_path=str(tmp_path / "weapon.spp"),
        template_path=str(template),
        uv_workflow="uv-tile",
    )

    assert result["success"] is True
    assert settings.call_args.kwargs["project_workflow"] == "uv-tile"
    project.create.assert_called_once_with(
        str(mesh.resolve()),
        template_file_path=str(template.resolve()),
        settings="settings",
    )


def test_open_and_close_project_preserve_unsaved_work(tmp_path, monkeypatch):
    path = tmp_path / "weapon.spp"
    path.write_bytes(b"spp")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(side_effect=[False, True, True, True, False])
    project.file_path = MagicMock(return_value=str(path))
    project.needs_saving = MagicMock(side_effect=[True, False])
    project.open = MagicMock()
    project.close = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    opened = _load("open_project").main(project_path=str(path))
    refused = _load("close_project").main()
    closed = _load("close_project").main()

    assert opened["success"] is True
    project.open.assert_called_once_with(str(path.resolve()))
    assert refused["success"] is False
    assert "Save the project" in refused["error"]
    assert closed["success"] is True
    project.close.assert_called_once_with()


def test_open_project_fails_closed_when_readback_path_does_not_match(tmp_path, monkeypatch):
    requested = tmp_path / "weapon.spp"
    requested.write_bytes(b"spp")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(side_effect=[False, True])
    project.file_path = MagicMock(return_value=str(tmp_path / "different.spp"))
    project.open = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    result = _load("open_project").main(project_path=str(requested))

    assert result["success"] is False
    assert "readback" in result["message"].lower()


def test_close_project_fails_closed_when_host_readback_stays_open(monkeypatch):
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    project.needs_saving = MagicMock(return_value=False)
    project.close = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    result = _load("close_project").main()

    assert result["success"] is False
    assert "readback" in result["message"].lower()


def test_save_project_uses_typed_mode_and_reads_back_clean_state(tmp_path, monkeypatch):
    path = tmp_path / "weapon.spp"
    path.write_bytes(b"spp")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    project.file_path = MagicMock(return_value=str(path))
    project.needs_saving = MagicMock(return_value=False)
    project.ProjectSaveMode = SimpleNamespace(Incremental="incremental", Full="full")
    project.save = MagicMock()
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    result = _load("save_project").main(mode="incremental")

    assert result["success"] is True
    assert result["context"]["project_path"] == str(path.resolve())
    assert result["context"]["needs_saving"] is False
    project.save.assert_called_once_with("incremental")


def test_reload_mesh_defers_until_native_callback_and_verifies_readback(tmp_path, monkeypatch):
    mesh = tmp_path / "weapon-v2.fbx"
    mesh.write_bytes(b"fbx")
    settings = MagicMock(return_value="settings")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    project.MeshReloadingSettings = settings
    project.last_imported_mesh_path = MagicMock(return_value=str(mesh))
    project.reload_mesh = MagicMock(
        side_effect=lambda _path, _settings, callback: callback(SimpleNamespace(name="SUCCESS"))
    )
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)

    deferred = _load("reload_mesh").main(
        mesh_path=str(mesh),
        import_cameras=False,
        preserve_strokes=True,
    )

    assert isinstance(deferred, DeferredToolResult)
    result = deferred.check_is_finished()
    assert result["success"] is True
    assert result["context"]["mesh_path"] == str(mesh.resolve())
    settings.assert_called_once_with(import_cameras=False, preserve_strokes=True, auto_unwrap_settings=None)
    project.reload_mesh.assert_called_once_with(str(mesh.resolve()), "settings", project.reload_mesh.call_args.args[2])


def test_textured_layer_maps_imported_resources_to_pbr_channels(tmp_path, monkeypatch):
    paths = {}
    for name in ("base", "normal", "roughness", "metallic", "ao"):
        path = tmp_path / f"{name}.png"
        path.write_bytes(b"png")
        paths[name] = str(path)

    layer = MagicMock()
    layer.uid.return_value = 557
    stack = MagicMock()
    stack.has_channel.side_effect = lambda channel: channel != "ao"
    channels = SimpleNamespace(
        BaseColor="base",
        Normal="normal",
        Roughness="rough",
        Metallic="metal",
        AO="ao",
    )
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    textureset.ChannelType = channels
    textureset.ChannelFormat = SimpleNamespace(sRGB8="srgb8", RGB8="rgb8", L8="l8")
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.InsertPosition = SimpleNamespace(from_textureset_stack=MagicMock(return_value="top"))
    layerstack.insert_fill = MagicMock(return_value=layer)
    resource = ModuleType("substance_painter.resource")
    resource.Usage = SimpleNamespace(TEXTURE="texture")
    resource.import_project_resource = MagicMock(
        side_effect=lambda _path, _usage, name, group: SimpleNamespace(identifier=lambda: name)
    )
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    for name, module in (
        ("substance_painter.project", project),
        ("substance_painter.textureset", textureset),
        ("substance_painter.layerstack", layerstack),
        ("substance_painter.resource", resource),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    result = _load("create_textured_pbr_layer").main(
        name="Drawcall 557",
        base_color_path=paths["base"],
        normal_path=paths["normal"],
        roughness_path=paths["roughness"],
        metallic_path=paths["metallic"],
        ambient_occlusion_path=paths["ao"],
    )

    assert result["success"] is True
    stack.add_channel.assert_called_once_with("ao", "l8")
    assert layer.active_channels == {"base", "normal", "rough", "metal", "ao"}
    assert layer.set_source.call_args_list == [
        call("base", "Drawcall 557_BaseColor"),
        call("normal", "Drawcall 557_Normal"),
        call("rough", "Drawcall 557_Roughness"),
        call("metal", "Drawcall 557_Metallic"),
        call("ao", "Drawcall 557_AO"),
    ]


def test_camera_orbit_pose_faces_the_center():
    module = _load("start_camera_orbit")

    position, rotation = module._camera_pose((0.0, 0.0, 0.0), 10.0, 0.0, 0.0)
    assert position == [0.0, 0.0, 10.0]
    assert rotation == [0.0, 0.0, 0.0]

    position, rotation = module._camera_pose((0.0, 0.0, 0.0), 10.0, 0.0, 90.0)
    assert position[0] == 10.0
    assert abs(position[2]) < 1e-12
    assert rotation == [0.0, 90.0, 0.0]
