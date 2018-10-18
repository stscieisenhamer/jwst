"""
Test suite for set_telescope_pointing
"""
import copy
import numpy as np
import os
import sys
import pytest
from tempfile import TemporaryDirectory

import requests_mock

from astropy.table import Table
from astropy.time import Time

from .. import engdb_tools
from .. import set_telescope_pointing as stp
from ... import datamodels
from ...tests.helpers import word_precision_check

# Setup mock engineering service
GOOD_MNEMONIC = 'INRSI_GWA_Y_TILT_AVGED'
STARTTIME = Time('2014-01-03')
ENDTIME = Time('2014-01-04')
ZEROTIME_START = Time('2014-01-01')
ZEROTIME_END = Time('2014-01-02')

# Header defaults
TARG_RA = 345.0
TARG_DEC = -87.0
V2_REF = 200.0
V3_REF = -350.0
V3I_YANG = 42.0
VPARITY = -1

# Get the mock DB
db_path = os.path.join(os.path.dirname(__file__), 'data', 'engdb_mock.csv')
mock_db = Table.read(db_path)
siaf_db = os.path.join(os.path.dirname(__file__), 'data', 'siaf.db')

# Some expected values.
# This pointing was provided by NGAS through W.Kinzel via
# spreadsheet `acs_tim_data_4_stsci_dms_jitter_file_mod_ra_dec_pa.xls`
Q_EXPECTED = np.asarray(
    [-0.36915286, 0.33763282, 0.05758533, 0.86395264]
)
J2FGS_MATRIX_EXPECTED = np.asarray(
    [-1.00444000e-03,   3.38145836e-03,   9.99993778e-01,
     9.99999496e-01,  -3.90000000e-14,   1.00444575e-03,
     3.39649146e-06,   9.99994283e-01,  -3.38145665e-03]
)
FSMCORR_EXPECTED = np.zeros((2,))
OBSTIME_EXPECTED = STARTTIME
VINFO_RA_EXPECTED = 348.927867
VINFO_DEC_EXPECTED = -38.749239
VINFO_PA_EXPECTED = 50.176708


def register_responses(mocker, response_db, starttime, endtime):
    request_url = ''.join([
        engdb_tools.ENGDB_BASE_URL,
        'Data/',
        '{mnemonic}',
        '?sTime={starttime}',
        '&eTime={endtime}'
    ])

    starttime_mil = int(starttime.unix * 1000)
    endtime_mil = int(endtime.unix * 1000)
    time_increment = (endtime_mil - starttime_mil) // len(response_db)

    response_generic = {
        'AllPoints': 1,
        'Count': 2,
        'ReqSTime': '/Date({:013d}+0000)/'.format(starttime_mil),
        'ReqETime': '/Date({:013d}+0000)/'.format(endtime_mil),
        'TlmMnemonic': None,
        'Data': [],
    }

    responses = {}
    for mnemonic in response_db.colnames:
        response = copy.deepcopy(response_generic)
        response['TlmMnemonic'] = mnemonic
        current_time = starttime_mil - time_increment
        for row in response_db:
            current_time += time_increment
            data = {}
            data['ObsTime'] = '/Date({:013d}+0000)/'.format(current_time)
            data['EUValue'] = row[mnemonic]
            response['Data'].append(data)
        mocker.get(
            request_url.format(
                mnemonic=mnemonic,
                starttime=starttime.iso,
                endtime=endtime.iso
            ),
            json=response
        )
        responses[mnemonic] = response

    return responses


@pytest.fixture
def eng_db():
    with requests_mock.Mocker() as rm:

        # Define response for aliveness
        url = ''.join([
            engdb_tools.ENGDB_BASE_URL,
            engdb_tools.ENGDB_METADATA
        ])
        rm.get(url, text='Success')

        # Define good responses
        good_responses = register_responses(
            rm,
            mock_db[1:2],
            STARTTIME,
            ENDTIME
        )

        # Define with zeros in the first row
        zero_responses = register_responses(
            rm,
            mock_db,
            ZEROTIME_START,
            ENDTIME
        )

        edb = engdb_tools.ENGDB_Service()

        # Test for good responses.
        for mnemonic in mock_db.colnames:
            r = edb.get_records(mnemonic, STARTTIME, ENDTIME)
            assert r == good_responses[mnemonic]

        # Test for zeros.
        for mnemonic in mock_db.colnames:
            r = edb.get_records(mnemonic, ZEROTIME_START, ENDTIME)
            assert r == zero_responses[mnemonic]

        yield edb


@pytest.fixture
def data_file():
    model = datamodels.Level1bModel()
    model.meta.exposure.start_time = STARTTIME.mjd
    model.meta.exposure.end_time = ENDTIME.mjd
    model.meta.target.ra = TARG_RA
    model.meta.target.dec = TARG_DEC
    model.meta.wcsinfo.v2_ref = V2_REF
    model.meta.wcsinfo.v3_ref = V3_REF
    model.meta.wcsinfo.v3yangle = V3I_YANG
    model.meta.wcsinfo.vparity = VPARITY
    model.meta.aperture.name = "MIRIM_FULL"
    model.meta.observation.date = '1/1/2017'

    with TemporaryDirectory() as path:
        file_path = os.path.join(path, 'fits.fits')
        model.save(file_path)
        yield file_path

def test_spot_check_v1():
    """Spot check on a specific quaternion
    """
    p = stp.Pointing()
    p.q = Q_EXPECTED
    p.j2fgs_matrix = J2FGS_MATRIX_EXPECTED
    p.fsmcorr = FSMCORR_EXPECTED

    # Not checking SIAF. Set to unity
    siaf = stp.SIAF()
    siaf.v2ref = 0.
    siaf.v3ref = 0.
    siaf.v3idlyang = 0.
    siaf.vparity = 1.

    wcsinfo, vinfo = stp.calc_wcs(p, siaf)

    assert np.isclose(vinfo.ra, VINFO_RA_EXPECTED)
    assert np.isclose(vinfo.dec, VINFO_DEC_EXPECTED)
    assert np.isclose(vinfo.pa, VINFO_PA_EXPECTED)


def test_get_pointing_fail():
    with pytest.raises(Exception):
        q, j2fgs_matrix, fmscorr, obstime = stp.get_pointing(47892.0, 48256.0)


def test_get_pointing(eng_db):
        (q,
         j2fgs_matrix,
         fsmcorr,
         obstime) = stp.get_pointing(STARTTIME, ENDTIME)
        assert np.isclose(q, Q_EXPECTED).all()
        assert np.isclose(j2fgs_matrix, J2FGS_MATRIX_EXPECTED).all()
        assert np.isclose(fsmcorr, FSMCORR_EXPECTED).all()
        assert obstime == STARTTIME


def test_get_pointing_list(eng_db):
        results = stp.get_pointing(STARTTIME, ENDTIME, result_type='all')
        assert isinstance(results, list)
        assert len(results) > 0
        assert np.isclose(results[0].q, Q_EXPECTED).all()
        assert np.isclose(results[0].j2fgs_matrix, J2FGS_MATRIX_EXPECTED).all()
        assert np.isclose(results[0].fsmcorr, FSMCORR_EXPECTED).all()
        assert results[0].obstime == STARTTIME


def test_get_pointing_with_zeros(eng_db):
    (q,
     j2fgs_matrix,
     fsmcorr,
     obstime) = stp.get_pointing(ZEROTIME_START, ENDTIME)
    assert j2fgs_matrix.any()
    (q_desired,
     j2fgs_matrix_desired,
     fsmcorr_desired,
     obstime) = stp.get_pointing(STARTTIME, ENDTIME)
    assert np.array_equal(q, q_desired)
    assert np.array_equal(j2fgs_matrix, j2fgs_matrix_desired)
    assert np.array_equal(fsmcorr, fsmcorr_desired)


@pytest.mark.skipif(sys.version_info.major<3,
                    reason="No URI support in sqlite3")
def test_add_wcs_default(data_file):
    try:
        stp.add_wcs(data_file, siaf_path=siaf_db)
    except Exception as e:
        pytest.skip(
            'Live ENGDB service is not accessible.'
            '\nException={}'.format(e)
        )

    model = datamodels.open(data_file)
    assert model.meta.pointing.ra_v1 == TARG_RA
    assert model.meta.pointing.dec_v1 == TARG_DEC
    assert model.meta.pointing.pa_v3 == 0.
    assert model.meta.wcsinfo.crval1 == TARG_RA
    assert model.meta.wcsinfo.crval2 == TARG_DEC
    assert np.isclose(model.meta.wcsinfo.pc1_1, -1.0)
    assert np.isclose(model.meta.wcsinfo.pc1_2, 0.0)
    assert np.isclose(model.meta.wcsinfo.pc2_1, 0.0)
    assert np.isclose(model.meta.wcsinfo.pc2_2, 1.0)
    assert model.meta.wcsinfo.ra_ref == TARG_RA
    assert model.meta.wcsinfo.dec_ref == TARG_DEC
    assert np.isclose(model.meta.wcsinfo.roll_ref, 358.9045979379)
    assert model.meta.wcsinfo.wcsaxes == 2
    assert word_precision_check(
        model.meta.wcsinfo.s_region,
        (
            'POLYGON ICRS'
            ' 345.0516057166881 -86.87312441299257'
            ' 344.61737392066823 -86.85221531104224'
            ' 344.99072891662956 -86.82863042295425'
            ' 345.42745662836063 -86.84915871318734'
        )
    )


@pytest.mark.skipif(sys.version_info.major<3,
                    reason="No URI support in sqlite3")
def test_add_wcs_fsmcorr_v1(data_file):
    """Test with default value using FSM original correction"""
    try:
        stp.add_wcs(data_file, fsmcorr_version='v1', siaf_path=siaf_db)
    except Exception as e:
        pytest.skip(
            'Live ENGDB service is not accessible.'
            '\nException={}'.format(e)
        )

    model = datamodels.open(data_file)
    assert model.meta.pointing.ra_v1 == TARG_RA
    assert model.meta.pointing.dec_v1 == TARG_DEC
    assert model.meta.pointing.pa_v3 == 0.
    assert model.meta.wcsinfo.crval1 == TARG_RA
    assert model.meta.wcsinfo.crval2 == TARG_DEC
    assert np.isclose(model.meta.wcsinfo.pc1_1, -1.0)
    assert np.isclose(model.meta.wcsinfo.pc1_2, 0.0)
    assert np.isclose(model.meta.wcsinfo.pc2_1, 0.0)
    assert np.isclose(model.meta.wcsinfo.pc2_2, 1.0)
    assert model.meta.wcsinfo.ra_ref == TARG_RA
    assert model.meta.wcsinfo.dec_ref == TARG_DEC
    assert np.isclose(model.meta.wcsinfo.roll_ref, 358.9045979379)
    assert model.meta.wcsinfo.wcsaxes == 2
    assert word_precision_check(
        model.meta.wcsinfo.s_region,
        (
            'POLYGON ICRS'
            ' 345.0516057166881 -86.87312441299257'
            ' 344.61737392066823 -86.85221531104224'
            ' 344.99072891662956 -86.82863042295425'
            ' 345.42745662836063 -86.84915871318734'
        )
    )


@pytest.mark.skipif(sys.version_info.major<3,
                    reason="No URI support in sqlite3")
def test_add_wcs_with_db(eng_db, data_file, siaf_file=siaf_db):
    """Test using the database"""
    stp.add_wcs(data_file, siaf_path=siaf_db)

    model = datamodels.open(data_file)
    assert np.isclose(model.meta.pointing.ra_v1, 348.9278669)
    assert np.isclose(model.meta.pointing.dec_v1, -38.749239)
    assert np.isclose(model.meta.pointing.pa_v3, 50.1767077)
    assert np.isclose(model.meta.wcsinfo.crval1, 348.8776709)
    assert np.isclose(model.meta.wcsinfo.crval2, -38.854159)
    assert np.isclose(model.meta.wcsinfo.pc1_1, 0.0385309)
    assert np.isclose(model.meta.wcsinfo.pc1_2, 0.9992574)
    assert np.isclose(model.meta.wcsinfo.pc2_1, 0.9992574)
    assert np.isclose(model.meta.wcsinfo.pc2_2, -0.0385309)
    assert np.isclose(model.meta.wcsinfo.ra_ref, 348.8776709)
    assert np.isclose(model.meta.wcsinfo.dec_ref, -38.854159)
    assert np.isclose(model.meta.wcsinfo.roll_ref, 50.20832726650)
    assert model.meta.wcsinfo.wcsaxes == 2
    assert word_precision_check(
        model.meta.wcsinfo.s_region,
        (
            'POLYGON ICRS'
            ' 349.00694612561705 -38.776964589744054'
            ' 349.0086451128466 -38.74533844552814'
            ' 349.04874980331374 -38.746495669763334'
            ' 349.0474396482846 -38.77812380255898'
        )
    )


@pytest.mark.skipif(sys.version_info.major<3,
                    reason="No URI support in sqlite3")
def test_add_wcs_with_db_fsmcorr_v1(eng_db, data_file):
    """Test using the database with original FSM correction"""
    stp.add_wcs(data_file, fsmcorr_version='v1', siaf_path=siaf_db)

    model = datamodels.open(data_file)
    assert np.isclose(model.meta.pointing.ra_v1, 348.9278669)
    assert np.isclose(model.meta.pointing.dec_v1, -38.749239)
    assert np.isclose(model.meta.pointing.pa_v3, 50.1767077)
    assert np.isclose(model.meta.wcsinfo.crval1, 348.8776709)
    assert np.isclose(model.meta.wcsinfo.crval2, -38.854159)
    assert np.isclose(model.meta.wcsinfo.pc1_1, 0.0385309)
    assert np.isclose(model.meta.wcsinfo.pc1_2, 0.9992574)
    assert np.isclose(model.meta.wcsinfo.pc2_1, 0.9992574)
    assert np.isclose(model.meta.wcsinfo.pc2_2, -0.0385309)
    assert np.isclose(model.meta.wcsinfo.ra_ref, 348.8776709)
    assert np.isclose(model.meta.wcsinfo.dec_ref, -38.854159)
    assert np.isclose(model.meta.wcsinfo.roll_ref, 50.20832726650)
    assert model.meta.wcsinfo.wcsaxes == 2
    assert word_precision_check(
        model.meta.wcsinfo.s_region,
        (
            'POLYGON ICRS'
            ' 349.00694612561705 -38.776964589744054'
            ' 349.0086451128466 -38.74533844552814'
            ' 349.04874980331374 -38.746495669763334'
            ' 349.0474396482846 -38.77812380255898'
        )
    )
