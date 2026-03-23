"""Unit tests for combobox toggle helper (additive open path)."""

from ghosthands.actions.combobox_toggle import combobox_toggle_clicked


def test_combobox_toggle_clicked_accepts_dict():
    assert combobox_toggle_clicked({"clicked": True, "via": "toggle"})
    assert not combobox_toggle_clicked({"clicked": False})
    assert not combobox_toggle_clicked({})


def test_combobox_toggle_clicked_accepts_json_string():
    assert combobox_toggle_clicked('{"clicked": true}')
    assert not combobox_toggle_clicked('{"clicked": false}')
    assert not combobox_toggle_clicked("not-json")
