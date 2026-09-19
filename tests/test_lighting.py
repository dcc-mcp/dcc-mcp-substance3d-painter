from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SKILLS = Path(__file__).parent.parent / "src" / "dcc_mcp_substance3d_painter" / "skills"


def _load(skill: str, script: str) -> ModuleType:
    path = SKILLS / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(f"{skill}_{script}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Environment:
    """Minimal stand-in for Painter's environment module."""

    def __init__(self, *, exposure=0.0, rotation=0.0, env_url="env://studio", bg_url=None, writable=True):
        self._exposure = exposure
        self._rotation = rotation
        # An unset map is modelled as a null resource id, matching Painter.
        self._env = None if env_url is None else SimpleNamespace(url=lambda: env_url)
        self._bg = None if bg_url is None else SimpleNamespace(url=lambda: bg_url)
        self.writable = writable

    def get_environment_map(self):
        return self._env

    def get_background_texture(self):
        return self._bg

    def get_exposure(self):
        return self._exposure

    def get_rotation(self):
        return self._rotation

    def set_environment_map(self, identifier):
        self._env = identifier

    def set_exposure(self, value):
        if not self.writable:
            raise RuntimeError("read-only host")
        self._exposure = float(value)

    def set_rotation(self, value):
        self._rotation = float(value)


@pytest.fixture
def environment(monkeypatch):
    """Install a fake ``substance_painter.environnement`` module."""

    def _install(env):
        module = ModuleType("substance_painter.environnement")
        for name in (
            "get_environment_map",
            "get_background_texture",
            "get_exposure",
            "get_rotation",
            "set_environment_map",
            "set_exposure",
            "set_rotation",
        ):
            setattr(module, name, getattr(env, name))
        monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
        monkeypatch.setitem(sys.modules, "substance_painter.environnement", module)
        return module

    return _install


def _install_resource(monkeypatch, url="resource://hdri/studio"):
    resource = ModuleType("substance_painter.resource")
    identifier = SimpleNamespace(url=lambda: url)
    resource.ResourceID = SimpleNamespace(from_url=lambda value: identifier)
    monkeypatch.setitem(sys.modules, "substance_painter.resource", resource)
    return identifier


def test_inspect_environment_reports_state_and_capabilities(environment):
    environment(_Environment(exposure=1.5, rotation=0.25, bg_url="bg://plate"))

    result = _load("painter-lighting", "inspect_environment").main()

    assert result["success"] is True
    context = result["context"]
    assert context["environment_map"] == "env://studio"
    assert context["background_texture"] == "bg://plate"
    assert context["exposure"] == 1.5
    assert context["rotation"] == 0.25
    assert context["capabilities"]["exposure"] == {"readable": True, "writable": True}


def test_readable_reflects_getter_presence_not_current_value(environment):
    """A readable capability whose current value is empty must still report readable.

    Regresses: `readable` was derived from the read value, so an unset
    background texture looked like an unsupported read.
    """

    environment(_Environment(bg_url=None, env_url=None))

    result = _load("painter-lighting", "inspect_environment").main()

    assert result["success"] is True
    capabilities = result["context"]["capabilities"]
    assert capabilities["background_texture"]["readable"] is True
    assert capabilities["environment_map"]["readable"] is True
    assert result["context"]["background_texture"] is None
    assert result["context"]["environment_map"] is None


def test_readable_is_false_when_getter_absent(environment):
    """Removing the getter must flip readable to False."""

    env = _Environment(bg_url="bg://plate")
    module = environment(env)
    delattr(module, "get_background_texture")

    result = _load("painter-lighting", "inspect_environment").main()

    assert result["success"] is True
    capabilities = result["context"]["capabilities"]
    assert capabilities["background_texture"]["readable"] is False
    # The other capabilities are unaffected.
    assert capabilities["exposure"]["readable"] is True


def test_inspect_environment_reports_unavailable_module(monkeypatch):
    # No environment module is installed at all, so probing must fail cleanly.
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    for name in ("substance_painter.environnement", "substance_painter.environment"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    result = _load("painter-lighting", "inspect_environment").main()

    assert result["success"] is False
    assert result["error"] == "environment_module_unavailable"


def test_set_environment_exposure_is_confirmed_by_readback(environment):
    env = _Environment(exposure=0.0)
    environment(env)

    result = _load("painter-lighting", "set_environment_exposure").main(exposure=-2.5)

    assert result["success"] is True
    assert env.get_exposure() == -2.5
    assert result["postcondition"]["verified"] is True
    assert result["context"]["exposure"] == -2.5


def test_set_environment_exposure_rejects_out_of_range(environment):
    environment(_Environment())

    module = _load("painter-lighting", "set_environment_exposure")
    assert module.main(exposure=100)["success"] is False
    assert module.main(exposure=-100)["success"] is False


def test_set_environment_exposure_detects_readback_mismatch(environment):
    class _Drifting(_Environment):
        def set_exposure(self, value):
            self._exposure = float(value) + 1.0

    environment(_Drifting())

    result = _load("painter-lighting", "set_environment_exposure").main(exposure=2.0)

    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
    assert result["context"]["actual"] == 3.0


def test_set_environment_rotation_is_confirmed_by_readback(environment):
    env = _Environment(rotation=0.0)
    environment(env)

    result = _load("painter-lighting", "set_environment_rotation").main(rotation=0.75)

    assert result["success"] is True
    assert env.get_rotation() == 0.75


def test_set_environment_rotation_rejects_out_of_range(environment):
    environment(_Environment())

    module = _load("painter-lighting", "set_environment_rotation")
    assert module.main(rotation=1.5)["success"] is False
    assert module.main(rotation=-0.5)["success"] is False


def test_set_environment_map_is_confirmed_by_readback(environment, monkeypatch):
    env = _Environment(env_url="env://old")
    environment(env)
    _install_resource(monkeypatch, url="resource://hdri/studio")

    result = _load("painter-lighting", "set_environment_map").main(resource_url="resource://hdri/studio")

    assert result["success"] is True
    assert env.get_environment_map().url() == "resource://hdri/studio"
    assert result["postcondition"]["verified"] is True


def test_set_environment_map_detects_readback_mismatch(environment, monkeypatch):
    class _Stubborn(_Environment):
        def set_environment_map(self, identifier):
            pass  # Silently ignore the write.

    environment(_Stubborn(env_url="env://old"))
    _install_resource(monkeypatch, url="resource://hdri/studio")

    result = _load("painter-lighting", "set_environment_map").main(resource_url="resource://hdri/studio")

    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
    assert result["context"]["actual"] == "env://old"


def test_set_environment_map_distinguishes_bad_url_from_missing_capability(environment, monkeypatch):
    """A malformed URL is a caller input error, not a host capability gap.

    Regresses: from_url() shared the `except ValueError` with the environment
    probing, so a bad URL reported environment_capability_unsupported.
    """

    environment(_Environment())
    resource = ModuleType("substance_painter.resource")

    def _from_url(value):
        raise ValueError(f"invalid resource url: {value}")

    resource.ResourceID = SimpleNamespace(from_url=_from_url)
    monkeypatch.setitem(sys.modules, "substance_painter.resource", resource)

    result = _load("painter-lighting", "set_environment_map").main(resource_url="not-a-resource-url")

    assert result["success"] is False
    assert result["error"] == "invalid_resource_url"
    assert result["context"]["resource_url"] == "not-a-resource-url"
    assert "invalid resource url" in result["context"]["detail"]


def test_set_environment_map_rejects_empty_url(environment):
    environment(_Environment())

    result = _load("painter-lighting", "set_environment_map").main(resource_url="   ")

    assert result["success"] is False


def test_lighting_reports_missing_setter_as_unsupported(environment):
    """A host that exposes getters but no setters must fail honestly."""

    env = _Environment()
    module = environment(env)
    for name in ("set_exposure", "set_rotation", "set_environment_map"):
        delattr(module, name)

    result = _load("painter-lighting", "set_environment_exposure").main(exposure=1.0)

    assert result["success"] is False
    assert result["error"] == "environment_capability_unsupported"
    assert "set_exposure" in result["context"]["detail"]


def test_inspect_environment_marks_readonly_capabilities(environment):
    env = _Environment()
    module = environment(env)
    delattr(module, "set_rotation")

    result = _load("painter-lighting", "inspect_environment").main()

    assert result["success"] is True
    assert result["context"]["capabilities"]["rotation"]["writable"] is False
    assert result["context"]["capabilities"]["exposure"]["writable"] is True
