import pytest
from purchase_runner import Jev, Halt


def test_missing_api_key_fails_without_reading_private_config(monkeypatch):
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    with pytest.raises(Halt, match='TYPESAFE_API_KEY'):
        Jev()


def test_choice_adapter_uses_environment_and_has_no_browser_dependency(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('TYPESAFE_MODEL', 'unit-test-model')
    model = Jev()
    assert callable(model.choose)
    assert model.choose.__module__ == 'jev_choice.model'
