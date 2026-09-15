from school_bot.services import calculate_arrival, credibility_percent, status_counts


def test_calculate_arrival():
    assert calculate_arrival("08:30", 20) == "08:50"
    assert calculate_arrival("23:45", 30) == "00:15"


def test_credibility_percent():
    assert credibility_percent(3, 1) == 75
    assert credibility_percent(0, 0) is None


def test_status_counts_ignores_unknown_values():
    assert status_counts(["present", "late", "late", "unknown"]) == {
        "present": 1,
        "late": 2,
        "maybe": 0,
        "absent": 0,
    }

