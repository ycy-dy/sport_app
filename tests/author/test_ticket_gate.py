from dataclasses import replace
from decimal import Decimal
import pytest
from sports_app.core import all_branches, validate_branches

FROZEN = ((("o1", "3.30"), ("o2", "3.85")), (("p1", "1.57"),))

def ticket():
    return all_branches(FROZEN, equal_stake="1", total_investment="2")

def audit(rows, frozen=FROZEN):
    return validate_branches(rows, frozen_leg_options=frozen)

def test_complete_ticket():
    assert audit(ticket()) == []

def test_missing_universe():
    assert validate_branches(ticket())

@pytest.mark.parametrize('indices', [(), (0,), (1,)])
def test_missing_combinations(indices):
    rows=ticket()
    assert audit([rows[i] for i in indices])

def test_duplicate_pair():
    rows=ticket()
    assert audit([rows[0], replace(rows[0], branch_id='other')])

def test_extra_pair():
    rows=ticket()
    assert audit(rows+[replace(rows[0], branch_id='extra')])

def test_duplicate_id():
    rows=ticket(); rows[1]=replace(rows[1],branch_id=rows[0].branch_id)
    assert audit(rows)

def test_wrong_total():
    assert audit(all_branches(FROZEN,equal_stake='1',total_investment='1'))

@pytest.mark.parametrize('field,value', [('odds_product_decimal',Decimal('999')),('effective_multiple_decimal',Decimal('999')),('equal_stake_decimal',Decimal('0')),('equal_stake_decimal',Decimal('-1')),('total_investment_decimal',Decimal('0')),('total_investment_decimal',Decimal('NaN')),('effective_multiple_decimal',Decimal('Infinity'))])
def test_forgery(field,value):
    rows=ticket();rows[0]=replace(rows[0],**{field:value})
    assert audit(rows)

def test_changed_original():
    rows=ticket();rows[0]=replace(rows[0],original_odds_decimal=(Decimal('9'),Decimal('1.57')))
    assert audit(rows)

def test_strict_boundary():
    frozen=((("left","2.5"),),(("right","1"),))
    assert audit(all_branches(frozen,equal_stake='1',total_investment='1'),frozen)

def test_weak_branch():
    frozen=((("left1","3.30"),("left2","3.00")),(("right","1.57"),))
    assert audit(all_branches(frozen,equal_stake='1',total_investment='2'),frozen)
