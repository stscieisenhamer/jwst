"""Test DMSBaseMixin features"""
from os import path
import pytest

from .helpers import (
    t_path
)

from .. import AssociationRegistry


@pytest.fixture(scope='module')
def dms_registry():
    """Create the registry"""
    dms_test_rules_path = t_path(path.join('data', 'dms_rules.py'))
    dms_registry = AssociationRegistry(
        [dms_test_rules_path], include_default=False
    )
    return dms_registry


@pytest.fixture(scope='module')
def dms_asns(dms_registry):
    """Create basic associations"""
    result = dms_registry.match('item')
    return result


def test_dms(dms_registry):
    """Test basic registry creation and usage"""
    assert 'Asn_DMS_Base' in dms_registry


def test_asn(dms_asns):
    """Test basic associaiton creation"""
    asns, orphaned = dms_asns
    assert len(asns) == 1
    assert len(orphaned) == 0


def test_finalize(dms_registry, dms_asns):
    """Test finalization"""
    asns, orphaned = dms_asns

    finalized = dms_registry.finalize(asns)

    assert len(finalized) == 1
