from __future__ import annotations

import pytest

from decision_memory import config


@pytest.fixture
def cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "decision-memory")
    monkeypatch.setenv("DM_DATABASE_URL", "postgresql://dm_app@/decision_memory")
    monkeypatch.delenv("DM_EXPECTED_AUDIENCE", raising=False)
    return monkeypatch


def test_cloud_run_sem_publico_nao_sobe(cloud_run):
    with pytest.raises(RuntimeError, match="DM_EXPECTED_AUDIENCE"):
        config.load()


def test_cloud_run_com_publico_sobe(cloud_run):
    cloud_run.setenv("DM_EXPECTED_AUDIENCE", "https://a.run.app, https://b.run.app")
    settings = config.load()
    assert settings.on_cloud_run
    assert settings.expected_audiences == ("https://a.run.app", "https://b.run.app")


def test_fora_do_cloud_run_publico_e_opcional(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("DM_EXPECTED_AUDIENCE", raising=False)
    monkeypatch.setenv("DM_DATABASE_URL", "postgresql://localhost/x")
    assert config.load().expected_audiences == ()
