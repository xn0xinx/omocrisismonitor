from omocrisismonitor import config


def test_defaults_load_without_file(tmp_path):
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.server.port == 8792
    assert cfg.ui.units_currency == "USD"
    assert cfg.history.prune_after_days == 0
    assert isinstance(cfg.map.center, list) and len(cfg.map.center) == 2


def test_user_toml_deep_merges(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[server]\nport = 9999\n[map]\nmaptiler_key = "abc"\n', encoding="utf-8"
    )
    cfg = config.load(p)
    assert cfg.server.port == 9999          # overridden
    assert cfg.server.host == "127.0.0.1"   # untouched default survives
    assert cfg.map.maptiler_key == "abc"
    assert cfg.map.style == "dataviz-dark"  # sibling default survives


def test_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert config.config_path().name == "config.toml"
    assert config.state_path().name == "state.json"
    assert config.db_path().name == "history.db"
    assert config.db_path().parent.is_dir()
