import pytest
from calc import add, divide, mean


def test_add():
    assert add(2, 3) == 5


def test_divide():
    assert divide(9, 3) == 3


def test_divide_by_zero():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5
