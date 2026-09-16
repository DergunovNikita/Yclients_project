"""Which portal role a CRM position hands out.

`role_for_staff()` is the only implementation of this rule — the modal preselect, the row-menu
shortcut and bulk provisioning all go through it — and it inherits its answer from the alias
order in `STAFF_CATEGORY_ALIASES`. These cases exist so that order cannot be tidied away.
"""

import pytest

from portal_account_provision import role_for_staff


class _Staff:
    def __init__(self, position):
        self.position = position


@pytest.mark.parametrize(
    ('position', 'expected'),
    [
        ('Администратор', 'admin'),
        ('администратор', 'admin'),
        ('Administrator', 'admin'),
        ('admin', 'admin'),
        ('Барбер', 'barber'),
        ('Master', 'barber'),
        ('Top Master', 'barber'),
        # Mixed titles resolve to barber because the barber aliases are scanned first. The rule
        # is arbitrary but it has to be *one* rule: the label decides per-role money visibility.
        ('Барбер-администратор', 'barber'),
        ('Администратор-барбер', 'barber'),
        ('Мастер-администратор', 'barber'),
        ('Barber-admin', 'barber'),
        # Nothing to go on falls to the half that opens no manual-fact editor.
        ('', 'barber'),
        (None, 'barber'),
        ('Уборщица', 'barber'),
    ],
)
def test_role_for_staff(position, expected):
    assert role_for_staff(_Staff(position)) == expected
