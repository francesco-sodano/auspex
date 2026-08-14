from auspex.api.auth import compatible_user_id


def test_compatible_user_id_matches_stable_algorithm():
    assert compatible_user_id("provider-user-1") == "354accb7-c30c-5c3a-b23e-b9af5223ddf3"


def test_compatible_user_id_is_stable_and_user_specific():
    assert compatible_user_id("provider-user-1") == compatible_user_id("provider-user-1")
    assert compatible_user_id("provider-user-1") != compatible_user_id("provider-user-2")
