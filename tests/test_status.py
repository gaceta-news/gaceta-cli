from gaceta.edition.build import edition_status

D = 86400


def test_status_rules():
    end = 100 * D
    assert edition_status("corrected", 1, 0, end) == "corrected"
    assert edition_status("developing", 3, end - 1 * D, end) == "developing"
    assert edition_status("developing", 3, end - 6 * D, end) == "settled"
    assert edition_status("developing", 1, end, end) == "settled"
