from pkg.invoice import Invoice


def test_total():
    assert Invoice().total() == 0
