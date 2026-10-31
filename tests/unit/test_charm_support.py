import pytest
from maas_bird.charm_support import config_string


@pytest.mark.parametrize("value", [None, True, 1, 1.5, [], {}])
def test_config_string_rejects_non_strings_without_coercion(value):
    with pytest.raises(ValueError, match="endpoint must be a string"):
        config_string({"endpoint": value}, "endpoint")


@pytest.mark.parametrize("value", ["", "https://maas.example/MAAS/"])
def test_config_string_preserves_strings(value):
    assert config_string({"endpoint": value}, "endpoint") == value
