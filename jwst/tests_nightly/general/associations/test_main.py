"""test_associations: Test of general Association functionality."""
import re

import pytest

from jwst.associations.tests.helpers import full_pool_rules
from jwst.associations.main import Main


def test_script(full_pool_rules):
    """Test full run of the script code"""
    pool, rules, pool_fname = full_pool_rules

    ref_rule_set = {
        'discover_Asn_SpectralTarget', 'discover_Asn_Coron',
        'candidate_Asn_Lv2Image', 'candidate_Asn_Lv2WFSS_NIS',
        'candidate_Asn_Lv2Spec', 'candidate_Asn_SpectralSource',
        'discover_Asn_SpectralSource', 'candidate_Asn_SpectralTarget',
        'discover_Asn_AMI', 'discover_Asn_Image',
        'candidate_Asn_Image', 'candidate_Asn_Lv2ImageTSO',
        'candidate_Asn_Lv2SpecTSO', 'candidate_Asn_Lv2FGS',
        'candidate_Asn_AMI', 'candidate_Asn_IFU',
        'candidate_Asn_Lv2WFSC', 'candidate_Asn_TSO',
        'candidate_Asn_Lv2ImageSpecial', 'discover_Asn_IFU',
        'candidate_Asn_Lv2SpecSpecial', 'candidate_Asn_Coron',
        'candidate_Asn_Lv2ImageNonScience'
    }

    generated = Main([pool_fname, '--dry-run'])
    asns = generated.associations
    assert len(asns) == 284
    assert len(generated.orphaned) == 204
    found_rules = set(
        asn['asn_rule']
        for asn in asns
    )
    assert ref_rule_set == found_rules


def test_asn_candidates(full_pool_rules):
    """Test basic candidate selection"""
    pool, rules, pool_fname = full_pool_rules

    generated = Main([pool_fname, '--dry-run', '-i', 'o001'])
    assert len(generated.associations) == 1
    generated = Main([pool_fname, '--dry-run', '-i', 'o001', 'o002'])
    assert len(generated.associations) == 2


def test_version_id(full_pool_rules):
    """Test that version id is properly included"""
    pool, rules, pool_fname = full_pool_rules

    generated = Main([pool_fname, '--dry-run', '-i', 'o001', '--version-id'])
    regex = re.compile(r'\d{3}t\d{6}')
    for asn in generated.associations:
        assert regex.search(asn.asn_name)

    version_id = 'mytestid'
    generated = Main([pool_fname, '--dry-run', '-i', 'o001', '--version-id', version_id])
    for asn in generated.associations:
        assert version_id in asn.asn_name


def test_pool_as_parameter(full_pool_rules):
    """Test passing the pool as an object"""
    pool, rules, pool_fname = full_pool_rules

    full = Main([pool_fname, '--dry-run'])
    full_as_param = Main(['--dry-run'], pool=pool)
    assert len(full.associations) == len(full_as_param.associations)
