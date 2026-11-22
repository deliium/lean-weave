from auth.session import login


def test_login():
    assert login("a")
