"""
Tools to create a WCS pipeline list of steps for NIRSPEC modes.

Call create_pipeline() which redirects based on EXP_TYPE

"""
from __future__ import (absolute_import, unicode_literals, division,
                        print_function)
import logging
from memory_profiler import profile
import numpy as np

from astropy.modeling import models, fitting
from astropy.modeling.models import Mapping, Identity, Const1D, Scale, Shift
from astropy import units as u
from astropy import coordinates as coord
from astropy.io import fits
from gwcs import coordinate_frames as cf

from ..transforms.models import (Rotation3DToGWA, DirCos2Unitless, Slit2Msa,
                                 AngleFromGratingEquation, WavelengthFromGratingEquation,
                                 Gwa2Slit, Unitless2DirCos, Logical, Slit, Snell,
                                 RefractionIndexFromPrism)
from .util import not_implemented_mode
from . import pointing
from ..datamodels import (CollimatorModel, CameraModel, DisperserModel, FOREModel,
                          IFUFOREModel, MSAModel, OTEModel, IFUPostModel, IFUSlicerModel,
                          WavelengthrangeModel, FPAModel)

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


@profile
def create_pipeline(input_model, reference_files):
    """
    Create a pipeline list based on EXP_TYPE.

    Parameters
    ----------
    input_model : jwst.datamodels.DataModel
        Either an ImageModel or a CubeModel
    reference_files : dict
        {reftype: file_name} mapping
        In the pipeline it's returned by CRDS.
    """
    exp_type = input_model.meta.exposure.type.lower()
    pipeline = exp_type2transform[exp_type](input_model, reference_files)
    if pipeline:
        log.info("Created a NIRSPEC {0} pipeline with references {1}".format(
                exp_type, reference_files))
    return pipeline


def imaging(input_model, reference_files):
    """
    Imaging pipeline

    frames : detector, gwa, msa, sky
    """
    # Get the corrected disperser model
    disperser = get_disperser(input_model, reference_files['disperser'])

    # DMS to SCA transform
    dms2detector = dms_to_sca(input_model)
    # DETECTOR to GWA transform
    det2gwa = detector_to_gwa(reference_files, input_model.meta.instrument.detector, disperser)

    gwa_through = Const1D(-1) * Identity(1) & Const1D(-1) * Identity(1) & Identity(1)

    angles = [disperser['theta_x'], disperser['theta_y'],
               disperser['theta_z'], disperser['tilt_y']]
    rotation = Rotation3DToGWA(angles, axes_order="xyzy", name='rotation').inverse
    dircos2unitless = DirCos2Unitless(name='directional_cosines2unitless')

    col_model = CollimatorModel(reference_files['collimator'])
    col = col_model.model
    col_model.close()

    # Get the default spectral order and wavelength range and record them in the model.
    sporder, wrange = get_spectral_order_wrange(input_model, reference_files['wavelengthrange'])
    input_model.meta.wcsinfo.waverange_start = wrange[0]
    input_model.meta.wcsinfo.waverange_end = wrange[1]
    input_model.meta.wcsinfo.spectral_order = sporder

    lam = wrange[0] + (wrange[1] - wrange[0]) * .5

    lam_model = Mapping((0, 1, 1)) | Identity(2) & Const1D(lam)

    gwa2msa = gwa_through | rotation | dircos2unitless | col | lam_model
    gwa2msa.inverse = col.inverse | dircos2unitless.inverse | rotation.inverse | gwa_through

    # Create coordinate frames in the NIRSPEC WCS pipeline
    # "detector", "gwa", "msa", "oteip", "v2v3", "world"
    det, sca, gwa, msa_frame, oteip, v2v3, world = create_imaging_frames()
    if input_model.meta.instrument.filter != 'OPAQUE':
        # MSA to OTEIP transform
        msa2ote = msa_to_oteip(reference_files)
        msa2oteip = msa2ote | Mapping((0, 1), n_inputs=3)
        msa2oteip.inverse = Mapping((0, 1, 0, 1)) | msa2ote.inverse | Mapping((0, 1), n_inputs=3)
        # OTEIP to V2,V3 transform
        with OTEModel(reference_files['ote']) as f:
            oteip2v23 = f.model

        # V2, V3 to world (RA, DEC) transform
        tel2sky = pointing.v23tosky(input_model)

        imaging_pipeline = [(det, dms2detector),
                            (sca, det2gwa),
                            (gwa, gwa2msa),
                            (msa_frame, msa2oteip),
                            (oteip, oteip2v23),
                            (v2v3, tel2sky),
                            (world, None)]
    else:
        # convert to microns if the pipeline ends earlier
        gwa2msa = (gwa2msa | Identity(2) & Scale(10**6)).rename('gwa2msa')
        imaging_pipeline = [(det, dms2detector),
                            (sca, det2gwa),
                            (gwa, gwa2msa),
                            (msa_frame, None)]

    return imaging_pipeline


@profile
def ifu(input_model, reference_files):
    """
    IFU pipeline
    """
    detector = input_model.meta.instrument.detector
    grating = input_model.meta.instrument.grating
    filter = input_model.meta.instrument.filter
    if detector == "NRS2" and grating.endswith('M'):
        log.critical("No IFU slices fall on detector {0}".format(detector))
        return None
    if detector == "NRS2" and grating == "G140H" and filter == "F070LP":
        log.critical("No IFU slices fall on detector {0}".format(detector))
        return None

    slits = np.arange(30)
    # Get the corrected disperser model
    disperser = get_disperser(input_model, reference_files['disperser'])

    # Get the default spectral order and wavelength range and record them in the model.
    sporder, wrange = get_spectral_order_wrange(input_model, reference_files['wavelengthrange'])
    input_model.meta.wcsinfo.waverange_start = wrange[0]
    input_model.meta.wcsinfo.waverange_end = wrange[1]
    input_model.meta.wcsinfo.spectral_order = sporder

    # DMS to SCA transform
    dms2detector = dms_to_sca(input_model)
    # DETECTOR to GWA transform
    det2gwa = Identity(2) & detector_to_gwa(reference_files, input_model.meta.instrument.detector, disperser)

    # GWA to SLIT
    gwa2slit = gwa_to_ifuslit(slits, input_model, disperser, reference_files)

    # SLIT to MSA transform
    slit2msa = ifuslit_to_msa(slits, reference_files)

    det, sca, gwa, slit_frame, msa_frame, oteip, v2v3, world = create_frames()
    if input_model.meta.instrument.filter != 'OPAQUE':
        # MSA to OTEIP transform
        msa2oteip = ifu_msa_to_oteip(reference_files)

        # OTEIP to V2,V3 transform
        # This includes a wavelength unit conversion from meters to microns.
        oteip2v23 = oteip_to_v23(reference_files)

        # V2, V3 to sky
        tel2sky = pointing.v23tosky(input_model) & Identity(1)

        # Create coordinate frames in the NIRSPEC WCS pipeline"
        #
        # The oteip2v2v3 transform converts the wavelength from meters (which is assumed
        # in the whole pipeline) to microns (which is the expected output)
        #
        # "detector", "gwa", "slit_frame", "msa_frame", "oteip", "v2v3", "world"

        pipeline = [(det, dms2detector),
                    (sca, det2gwa.rename('detector2gwa')),
                    (gwa, gwa2slit.rename('gwa2slit')),
                    (slit_frame, (Mapping((0, 1, 2, 3)) | slit2msa).rename('slit2msa')),
                    (msa_frame, msa2oteip.rename('msa2oteip')),
                    (oteip, oteip2v23.rename('oteip2v23')),
                    (v2v3, tel2sky),
                    (world, None)]
    else:
        # convert to microns if the pipeline ends earlier
        slit2msa = (Mapping((0, 1, 2, 3)) | slit2msa).rename('slit2msa')
        pipeline = [(det, dms2detector),
                    (sca, det2gwa.rename('detector2gwa')),
                    (gwa, gwa2slit.rename('gwa2slit')),
                    (slit_frame, slit2msa),
                    (msa_frame, None)]

    return pipeline


def slits_wcs(input_model, reference_files):
    """
    Create the WCS pipeline for observations using the MSA shutter array or fixed slits.

    Parameters
    ----------
    input_model : `~jwst.datamodels.ImageModel`
        The input data model.
    reference_files : dict
        Dictionary with reference files supplied by CRDS.

    """
    open_slits_id = get_open_slits(input_model, reference_files)
    if not open_slits_id:
        return None
    n_slits = len(open_slits_id)
    log.info("Computing WCS for {0} open slitlets".format(n_slits))

    msa_pipeline = slitlets_wcs(input_model, reference_files, open_slits_id)

    return msa_pipeline

def slitlets_wcs(input_model, reference_files, open_slits_id):
    # Get the corrected disperser model
    disperser = get_disperser(input_model, reference_files['disperser'])

    # Get the default spectral order and wavelength range and record them in the model.
    sporder, wrange = get_spectral_order_wrange(input_model, reference_files['wavelengthrange'])
    input_model.meta.wcsinfo.waverange_start = wrange[0]
    input_model.meta.wcsinfo.waverange_end = wrange[1]
    input_model.meta.wcsinfo.spectral_order = sporder

    # DMS to SCA transform
    dms2detector = dms_to_sca(input_model).rename('dms2sca')
    # DETECTOR to GWA transform
    det2gwa = Identity(2) & detector_to_gwa(reference_files, input_model.meta.instrument.detector, disperser)

    # GWA to SLIT
    gwa2slit = gwa_to_slit(open_slits_id, input_model, disperser, reference_files)

    # SLIT to MSA transform
    slit2msa = slit_to_msa(open_slits_id, reference_files['msa'])

    # Create coordinate frames in the NIRSPEC WCS pipeline"
    # "detector", "gwa", "slit_frame", "msa_frame", "oteip", "v2v3", "world"
    det, sca, gwa, slit_frame, msa_frame, oteip, v2v3, world = create_frames()
    if input_model.meta.instrument.filter != 'OPAQUE':
        # MSA to OTEIP transform
        msa2oteip = msa_to_oteip(reference_files)

        # OTEIP to V2,V3 transform
        # This includes a wavelength unit conversion from meters to microns.
        oteip2v23 = oteip_to_v23(reference_files)

        # V2, V3 to sky
        tel2sky = pointing.v23tosky(input_model) & Identity(1)

        msa_pipeline = [(det, dms2detector),
                        (sca, det2gwa.rename('det2gwa')),
                        (gwa, gwa2slit.rename('gwa2slit')),
                        (slit_frame, (Mapping((0, 1, 2, 3)) | slit2msa).rename('slit2msa')),
                        (msa_frame, msa2oteip.rename('msa2oteip')),
                        (oteip, oteip2v23.rename('oteip2v23')),
                        (v2v3, tel2sky),
                        (world, None)]
    else:
        # convert to microns if the pipeline ends earlier
        gwa2slit = (gwa2slit).rename('gwa2slit')
        msa_pipeline = [(det, dms2detector),
                        (sca, det2gwa),
                        (gwa, gwa2slit),
                        (slit_frame, Mapping((0, 1, 2, 3)) | slit2msa),
                        (msa_frame, None)]

    return msa_pipeline


def get_open_slits(input_model, reference_files=None):
    exp_type = input_model.meta.exposure.type.lower()
    if exp_type == "nrs_msaspec":
        msa_metadata_file, msa_metadata_id = get_msa_metadata(input_model)
        slits = get_open_msa_slits(msa_metadata_file, msa_metadata_id)
    elif exp_type == "nrs_fixedslit":
        slits = get_open_fixed_slits(input_model)
    elif exp_type == "nrs_brightobj":
        slits = [Slit('S1600A1', 3, 0, 0, -.5, .5, 5)]
    elif exp_type == "nrs_lamp":
        slits = get_open_fixed_slits(input_model)
    else:
        raise ValueError("EXP_TYPE {0} is not supported".format(exp_type.upper()))
    if reference_files is not None:
        slits = validate_open_slits(input_model, slits, reference_files)
        log.info("Slits projected on detector {0}: {1}".format(input_model.meta.instrument.detector,
                                                             [sl.name for sl in slits]))
    if not slits:
        log.critical("No open slits fall on detector {0}.".format(input_model.meta.instrument.detector))
    return slits


def get_open_fixed_slits(input_model):
    if input_model.meta.subarray.name is None:
        raise ValueError("Input file is missing SUBARRAY value/keyword.")
    slits = []
    s2a1 = Slit('S200A1', 0, 0, 0, -.5, .5, 5)
    s2a2 = Slit('S200A2', 1, 0, 0, -.5, .5, 5)
    s4a1 = Slit('S400A1', 2, 0, 0, -.5, .5, 5)
    s16a1 = Slit('S1600A1', 3, 0, 0, -.5, .5, 5)
    s2b1 = Slit('S200B1', 4, 0, 0, -.5, .5, 5)

    subarray = input_model.meta.subarray.name.upper()
    if subarray == "S200A1":
        slits.append(s2a1)
    elif subarray == "S200A2":
        slits.append(s2a2)
    elif subarray == "S400A1":
        slits.append(s4a1)
    elif subarray == "S1600A1":
        slits.append(s16a1)
    elif subarray == "S200B1":
        slits.append(s2b1)
    else:
        slits.extend([s2a1, s2a2, s4a1, s16a1, s2b1])
    return slits


def get_msa_metadata(input_model):
    """
    Get the MSA metadata file (MSAMTFL) and the msa metadata id (MSAMETID).

    """
    msa_config = input_model.meta.instrument.msa_configuration_file
    if msa_config is None:
        message = "MSA metadata file is not available (keyword MSAMETFL)."
        log.critical(message)
        raise KeyError(message)
    msa_metadata_id = input_model.meta.instrument.msa_metadata_id
    if msa_metadata_id is None:
        message = "MSA metadata ID is not available (keyword MSAMETID)."
        log.critical(message)
    return msa_config, msa_metadata_id


def get_open_msa_slits(msa_file, msa_metadata_id):
    """
    Computes (ymin, ymax) of open slitlets.

    The msa_file is expected to contain data (tuples) with the following fields:

        ('slitlet_id', '>i2'),
        ('msa_metadata_id', '>i2'),
        ('shutter_quadrant', '>i2'),
        ('shutter_row', '>i2'),
        ('shutter_column', '>i2'),
        ('source_id', '>i2'),
        ('background', 'S1'),
        ('shutter_state', 'S6'),
        ('estimated_source_in_shutter_x', '>f4'),
        ('estimated_source_in_shutter_y', '>f4')])

    For example, something like:
        (12, 2, 4, 251, 22, 1, 'Y', 'OPEN', nan, nan),

       column

    Parameters
    ----------
        msa_file : str
            MSA configuration file name, FITS keyword MSACONFL.
        msa_metadata_id : int
            The MSA meta id for the science file, FITS keyword MSAMETID.

    Returns
    -------
    slitlets : list
        A list of slitlets. Each slitlet is a tuple with
        ("name", "shutter_id", "xcen", "ycen", "ymin", "ymax", "quadrant", "source_id", "nshutters")

    """
    slitlets = []

    # If they passed in a string then we shall assume it is the filename
    # of the configuration file.
    with fits.open(msa_file) as msa_file:
        # Get the configuration header from teh _msa.fits file.  The EXTNAME should be 'SHUTTER_INFO'
        msa_conf = msa_file[('SHUTTER_INFO', 1)]
        msa_source = msa_file[("SOURCE_INFO", 1)].data

        # First we are going to filter the msa_file data on the msa_metadata_id
        # as that is all we are interested in for this function.
        msa_data = [x for x in msa_conf.data if x['msa_metadata_id'] == msa_metadata_id]

        log.debug('msa_data with msa_metadata_id = {}   {}'.format(msa_metadata_id, msa_data))
        log.info('Retrieving open slitlets for msa_metadata_id = {}'.format(msa_metadata_id))

        # First thing to do is to get the unique slitlet_ids
        slitlet_ids_unique = list(set([x['slitlet_id'] for x in msa_data]))

        # Now lets look at each unique slitlet id
        for slitlet_id in slitlet_ids_unique:

            # Get the rows for the current slitlet_id
            slitlets_sid = [x for x in msa_data if x['slitlet_id'] == slitlet_id]
            nshutters = len(slitlets_sid)
            # Count the number of backgrounds that have an 'N' (meaning main shutter)
            # This needs to be 0 or 1 and we will have to deal with those differently
            # See: https://github.com/STScI-JWST/jwst/commit/7588668b44b77486cdafb35f7e2eb2dcfa7d1b63#commitcomment-18987564

            n_main_shutter = len([s for s in slitlets_sid if s['background'] == 'N'])

            # In the next part we need to calculate, find, determine 5 things:
            #    quadrant,  xcen, ycen,  ymin, max

            margin = 0.05

            # There are no main shutters, all are background
            if n_main_shutter == 0:
                jmin = min([s['shutter_column'] for s in slitlets_sid])
                jmax = max([s['shutter_column'] for s in slitlets_sid])
                j = (jmax - jmin) // 2 + 1
                ymax = 0.5 + margin + (jmax - j) * 1.15
                ## TODO: check this formula - it is different (assuming it's incorrect in the report).
                ymin = -(0.5 + margin) + (jmin - j) * 1.15
                quadrant = slitlets_sid[0]['shutter_quadrant']
                ycen = j
                xcen = slitlets_sid[0]['shutter_row']  # grab the first as they are all the same
                source_xpos = 0.0
                source_ypos = 0.0
            # There is 1 main shutter, phew, that makes it easier.
            elif n_main_shutter == 1:
                xcen, ycen, quadrant, source_xpos, source_ypos = [
                    (s['shutter_row'], s['shutter_column'], s['shutter_quadrant'],
                     s['estimated_source_in_shutter_x'],
                     s['estimated_source_in_shutter_y'])
                    for s in slitlets_sid if s['background'] == 'N'][0]

                # y-size
                jmin = min([s['shutter_column'] for s in slitlets_sid])
                jmax = max([s['shutter_column'] for s in slitlets_sid])
                j = ycen
                ymax = 0.5 + margin + (jmax - j) * 1.15
                ymin = -(0.5 + margin) + (jmin - j) * 1.15

            # Not allowed....
            else:
                raise ValueError("MSA configuration file has more than 1 shutter with "
                                 "sources for metadata_id = {}".format(msa_metadata_id))

            shutter_id = xcen + (ycen - 1) * 365
            source_id = slitlets_sid[0]['source_id']
            source_name, source_alias, stellarity = [
                (s['source_name'], s['alias'], s['stellarity']) \
                for s in msa_source if s['source_id'] == source_id][0]
            # Create the output list of tuples that contain the required
            # data for further computations
            """
            Convert source positions from PPS to Model coordinate frame.
            The source x,y position in the shutter is given in the msa configuration file,
            columns "estimated_source_in_shutter_x" and "estimated_source_in_shutter_y".
            The source position is in a coordinate system associated with each shutter whose
            origin is the upper left corner of the shutter, positive x is to the right
            and positive y is downwards. To convert to the coordinate frame associated with the
            slit, where (0, 0) is in the center of the slit, we subtract 0.5 in both directions.
            """
            source_xpos = source_xpos - 0.5
            source_ypos = source_ypos - 0.5
            slitlets.append(Slit(slitlet_id, shutter_id, xcen, ycen, ymin, ymax,
                                 quadrant, source_id, nshutters, source_name, source_alias,
                                 stellarity, source_xpos, source_ypos))
    return slitlets


def get_spectral_order_wrange(input_model, wavelengthrange_file):
    """
    Read the spectral order and wavelength range from the reference file.

    Parameters
    ----------
    filter : str
        The filter used.
    grating : str
        The grating used in the observation.
    wavelength_range_file : str
        Reference file of type "wavelengthrange".
    """
    full_range = [.6e-6, 5.3e-6]

    filter = input_model.meta.instrument.filter
    lamp = input_model.meta.instrument.lamp_state
    grating = input_model.meta.instrument.grating

    #wave_range = AsdfFile.open(wavelengthrange_file)
    wave_range_model = WavelengthrangeModel(wavelengthrange_file)
    wrange_selector = wave_range_model.waverange_selector
    if filter == "OPAQUE":
        keyword = lamp + '_' + grating
    else:
        keyword = filter + '_' + grating
    try:
        index = wrange_selector.index(keyword)
        #order = wave_range.tree['filter_grating'][keyword]['order']
        #wrange = wave_range.tree['filter_grating'][keyword]['range']
    except (KeyError, ValueError):
        index = None
    if index is not None:
        order = wave_range_model.order[index]
        wrange = wave_range_model.wavelengthrange[index]
    else:
        order = -1
        wrange = full_range
        log.warning("Combination {0} missing in wavelengthrange file, setting order to -1 and range to {1}.".format(keyword, full_range))

    wave_range_model.close()
    return order, wrange


def ifuslit_to_msa(slits, reference_files):
    """
    The transform from slit_frame to msa_frame.

    Parameters
    ----------
    slits_id : list
        A list of slit IDs for all open shutters/slitlets.
    msafile : str
        The name of the msa reference file.

    Returns
    -------
    model : `~jwst.transforms.Slit2Msa` model.
        Transform from slit_frame to msa_frame.
    """

    #ifuslicer = AsdfFile.open(reference_files['ifuslicer'])
    ifuslicer = IFUSlicerModel(reference_files['ifuslicer'])
    models = []
    #ifuslicer_model = (ifuslicer.tree['model']).rename('ifuslicer_model')
    ifuslicer_model = ifuslicer.model
    for slit in slits:
        #slitdata = ifuslicer.tree['data'][slit]
        slitdata = ifuslicer.data[slit]
        slitdata_model = (get_slit_location_model(slitdata)).rename('slitdata_model')

        msa_transform = slitdata_model | ifuslicer_model
        models.append(msa_transform)
    ifuslicer.close()

    return Slit2Msa(slits, models)


def slit_to_msa(open_slits, msafile):
    """
    The transform from slit_frame to msa_frame.

    Parameters
    ----------
    open_slits : list
        A list of slit IDs for all open shutters/slitlets.
    msafile : str
        The name of the msa reference file.

    Returns
    -------
    model : `~jwst.transforms.Slit2Msa` model.
        Transform from slit_frame to msa_frame.
    """
    #msa = AsdfFile.open(msafile)
    msa = MSAModel(msafile)
    models = []
    for quadrant in range(1, 6):
        slits_in_quadrant = [s for s in open_slits if s.quadrant == quadrant]
        msa_quadrant = getattr(msa, 'Q{0}'.format(quadrant))
        if any(slits_in_quadrant):
            msa_data = msa_quadrant.data
            msa_model = msa_quadrant.model
            for slit in slits_in_quadrant:
                slit_id = slit.shutter_id
                slitdata = msa_data[slit_id]
                slitdata_model = get_slit_location_model(slitdata)
                msa_transform = slitdata_model | msa_model
                models.append(msa_transform)
    msa.close()
    return Slit2Msa(open_slits, models)


def gwa_to_ifuslit(slits, input_model, disperser, reference_files):
    """
    GWA to SLIT transform.

    Parameters
    ----------
    slits : list
        A list of slit IDs for all IFU slits 0-29.
    disperser : dict
        A corrected disperser ASDF object.
    filter : str
        The filter used.
    grating : str
        The grating used in the observation.
    reference_files: dict
        Dictionary with reference files returned by CRDS.

    Returns
    -------
    model : `~jwst.transforms.Gwa2Slit` model.
        Transform from GWA frame to SLIT frame.
   """
    ymin = -.55
    ymax = .55

    wrange = (input_model.meta.wcsinfo.waverange_start,
              input_model.meta.wcsinfo.waverange_end),
    order = input_model.meta.wcsinfo.spectral_order
    agreq = angle_from_disperser(disperser, input_model)
    lgreq = wavelength_from_disperser(disperser, input_model)

    # The wavelength units up to this point are
    # meters as required by the pipeline but the desired output wavelength units is microns.
    # So we are going to Scale the spectral units by 1e6 (meters -> microns)
    if input_model.meta.instrument.filter == 'OPAQUE':
        lgreq = lgreq | Scale(1e6)

    collimator2gwa = collimator_to_gwa(reference_files, disperser)
    mask = mask_slit(ymin, ymax)

    #ifuslicer = AsdfFile.open(reference_files['ifuslicer'])
    #ifupost = AsdfFile.open(reference_files['ifupost'])
    ifuslicer = IFUSlicerModel(reference_files['ifuslicer'])
    ifupost = IFUPostModel(reference_files['ifupost'])
    slit_models = []
    #ifuslicer_model = ifuslicer.tree['model']
    ifuslicer_model = ifuslicer.model
    for slit in slits:
        #slitdata = ifuslicer.tree['data'][slit]
        slitdata = ifuslicer.data[slit]
        slitdata_model = get_slit_location_model(slitdata)
        ifuslicer_transform = (slitdata_model | ifuslicer_model)
        #ifupost_transform = ifupost.tree[slit]['model']
        ifupost_transform = getattr(ifupost, "slice_{0}".format(slit))
        msa2gwa = ifuslicer_transform | ifupost_transform | collimator2gwa
        gwa2msa = gwa_to_ymsa(msa2gwa)# TODO: Use model sets here
        bgwa2msa = Mapping((0, 1, 0, 1), n_inputs=3) | \
                 Const1D(0) * Identity(1) & Const1D(-1) * Identity(1) & Identity(2) | \
                 Identity(1) & gwa2msa & Identity(2) | \
                 Mapping((0, 1, 0, 1, 2, 3)) | Identity(2) & msa2gwa & Identity(2) | \
                 Mapping((0, 1, 2, 3, 5), n_inputs=7) | Identity(2) & lgreq | mask

        # msa to before_gwa
        msa2bgwa = msa2gwa & Identity(1) | Mapping((3, 0, 1, 2)) | agreq
        bgwa2msa.inverse = msa2bgwa
        slit_models.append(bgwa2msa)

    ifuslicer.close()
    ifupost.close()
    return Gwa2Slit(slits, slit_models)


def gwa_to_slit(open_slits, input_model, disperser, reference_files):
    """
    GWA to SLIT transform.

    Parameters
    ----------
    open_slits : list
        A list of slit IDs for all open shutters/slitlets.
    disperser : dict
        A corrected disperser ASDF object.
    filter : str
        The filter used.
    grating : str
        The grating used in the observation.
    reference_files: dict
        Dictionary with reference files returned by CRDS.

    Returns
    -------
    model : `~jwst.transforms.Gwa2Slit` model.
        Transform from GWA frame to SLIT frame.
    """
    wrange = (input_model.meta.wcsinfo.waverange_start,
                     input_model.meta.wcsinfo.waverange_end),
    order = input_model.meta.wcsinfo.spectral_order

    agreq = angle_from_disperser(disperser, input_model)
    collimator2gwa = collimator_to_gwa(reference_files, disperser)
    lgreq = wavelength_from_disperser(disperser, input_model)

    # The wavelength units up to this point are
    # meters as required by the pipeline but the desired output wavelength units is microns.
    # So we are going to Scale the spectral units by 1e6 (meters -> microns)
    if input_model.meta.instrument.filter == 'OPAQUE':
        lgreq = lgreq | Scale(1e6)

    #msa = AsdfFile.open(reference_files['msa'])
    msa = MSAModel(reference_files['msa'])
    slit_models = []
    for quadrant in range(1, 6):
        slits_in_quadrant = [s for s in open_slits if s.quadrant == quadrant]
        log.info("There are {0} open slits in quadrant {1}".format(len(slits_in_quadrant), quadrant))
        msa_quadrant = getattr(msa, 'Q{0}'.format(quadrant))
        if any(slits_in_quadrant):
            #msa_model = msa.tree[quadrant]['model']
            msa_model = msa_quadrant.model
            log.info("Getting slits location for quadrant {0}".format(quadrant))
            #msa_data = msa.tree[quadrant]['data']
            msa_data = msa_quadrant.data
            for slit in slits_in_quadrant:
                mask = mask_slit(slit.ymin, slit.ymax)
                slit_id = slit.shutter_id
                slitdata = msa_data[slit_id]
                slitdata_model = get_slit_location_model(slitdata)
                msa_transform = slitdata_model | msa_model
                msa2gwa = (msa_transform | collimator2gwa)
                gwa2msa = gwa_to_ymsa(msa2gwa)# TODO: Use model sets here
                bgwa2msa = Mapping((0, 1, 0, 1), n_inputs=3) | \
                    Const1D(0) * Identity(1) & Const1D(-1) * Identity(1) & Identity(2) | \
                    Identity(1) & gwa2msa & Identity(2) | \
                    Mapping((0, 1, 0, 1, 2, 3)) | Identity(2) & msa2gwa & Identity(2) | \
                    Mapping((0, 1, 2, 3, 5), n_inputs=7) | Identity(2) & lgreq | mask
                    #Mapping((0, 1, 2, 5), n_inputs=7) | Identity(2) & lgreq | mask
                    # and modify lgreq to accept alpha_in, beta_in, alpha_out
                # msa to before_gwa
                msa2bgwa = msa2gwa & Identity(1) | Mapping((3, 0, 1, 2)) | agreq
                bgwa2msa.inverse = msa2bgwa
                slit_models.append(bgwa2msa)
    msa.close()
    return Gwa2Slit(open_slits, slit_models)


def angle_from_disperser(disperser, input_model):
    lmin = input_model.meta.wcsinfo.waverange_start
    lmax = input_model.meta.wcsinfo.waverange_end
    sporder = input_model.meta.wcsinfo.spectral_order
    if input_model.meta.instrument.grating.lower() != 'prism':
        agreq = AngleFromGratingEquation(disperser.groovedensity,
                                         sporder, name='alpha_from_greq')
        return agreq
    else:
        system_temperature = input_model.meta.instrument.gwa_tilt
        system_pressure = disperser['pref']

        snell = Snell(disperser['angle'], disperser['kcoef'], disperser['lcoef'],
                      disperser['tcoef'], disperser['tref'], disperser['pref'],
                      system_temperature, system_pressure, name="snell_law")
        return snell


def wavelength_from_disperser(disperser, input_model):
    sporder = input_model.meta.wcsinfo.spectral_order
    if input_model.meta.instrument.grating.lower() != 'prism':
        lgreq = WavelengthFromGratingEquation(disperser.groovedensity,
                                              sporder, name='lambda_from_gratingeq')
        return lgreq
    else:
        lmin = input_model.meta.wcsinfo.waverange_start
        lmax = input_model.meta.wcsinfo.waverange_end
        lam = np.linspace(lmin, lmax, 10000)
        system_temperature = input_model.meta.instrument.gwa_tilt
        if system_temperature is None:
            message = "Missing reference temperature (keyword GWA_TILT)."
            log.critical(message)
            raise KeyError(message)
        system_pressure = disperser['pref']
        tref = disperser['tref']
        pref = disperser['pref']
        kcoef = disperser['kcoef'][:]
        lcoef = disperser['lcoef'][:]
        tcoef = disperser['tcoef'][:]
        n = Snell.compute_refraction_index(lam, system_temperature, tref, pref,
                                           system_pressure, kcoef, lcoef, tcoef
                                           )
        poly = models.Polynomial1D(1)
        fitter = fitting.LinearLSQFitter()
        lam_of_n = fitter(poly, n, lam)
        lam_of_n = lam_of_n.rename('n_interpolate')
        n_from_prism = RefractionIndexFromPrism(disperser['angle'], name='n_prism')
        return n_from_prism | lam_of_n


def detector_to_gwa(reference_files, detector, disperser):
    """
    Transform from DETECTOR frame to GWA frame.

    Parameters
    ----------
    reference_files: dict
        Dictionary with reference files returned by CRDS.
    detector : str
        The detector keyword.
    disperser : dict
        A corrected disperser ASDF object.

    Returns
    -------
    model : `~astropy.modeling.core.Model` model.
        Transform from DETECTOR frame to GWA frame.

    """
    #with AsdfFile.open(reference_files['fpa']) as f:
    #    fpa = f.tree[detector].copy()
    with FPAModel(reference_files['fpa']) as f:
        fpa = getattr(f, detector.lower() + '_model')
    #with AsdfFile.open(reference_files['camera']) as f:
    with CameraModel(reference_files['camera']) as f:
        #camera = f.tree['model'].copy()
        camera = f.model

    angles = [disperser['theta_x'], disperser['theta_y'],
               disperser['theta_z'], disperser['tilt_y']]
    rotation = Rotation3DToGWA(angles, axes_order="xyzy", name='rotation')
    u2dircos = Unitless2DirCos(name='unitless2directional_cosines')
    model = (models.Shift(-1) & models.Shift(-1) | fpa | camera | u2dircos | rotation)
    return model


def dms_to_sca(input_model):
    """
    Transforms from DMS to SCA coordinates.
    """
    detector = input_model.meta.instrument.detector
    xstart = input_model.meta.subarray.xstart
    ystart = input_model.meta.subarray.ystart
    if xstart is None:
        xstart = 1
    if ystart is None:
        ystart = 1
    # The SCA coordinates are in full frame
    # The inputs are 1-based, remove -1 when'if they are 0-based
    # The outputs must be 1-based becaause this is what the model expects.
    # If xstart was 0-based and the inputs were 0-based ->
    # Shift(+1)
    subarray2full = models.Shift(xstart - 1) & models.Shift(ystart - 1)
    if detector == 'NRS2':
        model = models.Shift(-2048) & models.Shift(-2048) | models.Scale(-1) & models.Scale(-1)
    elif detector == 'NRS1':
        model = models.Identity(2)
    return subarray2full | model


def mask_slit(ymin=-.5, ymax=.5):
    """
    Returns a model which masks out pixels in a NIRSpec cutout outside the slit.

    Uses ymin, ymax for the slit and the wavelength range to define the location of the slit.

    Parameters
    ----------
    ymin, ymax : float
        ymin and ymax relative boundary of a slit.

    Returns
    -------
    model : `~astropy.modeling.core.Model`
        A model which takes x_slit, y_slit, lam inputs and substitutes the
        values outside the slit with NaN.

    """
    greater_than_ymax = Logical(condition='GT', compareto=ymax, value=np.nan)
    less_than_ymin = Logical(condition='LT', compareto=ymin, value=np.nan)

    model = Mapping((0, 1, 2, 1)) | Identity(3) & (greater_than_ymax | less_than_ymin | models.Scale(0)) | \
          Mapping((0, 1, 3, 2, 3)) | Identity(1) & Mapping((0,), n_inputs=2) + Mapping((1,)) & \
          Mapping((0,), n_inputs=2) + Mapping((1,))
    model.inverse = Identity(3)
    return model


def compute_bounding_box(slit2detector, wavelength_range, slit_ymin=-.5, slit_ymax=.5):
    """
    Compute the projection of a slit/slice on the detector.

    The edges of the slit are used to determine the location
    of the projection of the slit on the detector.
    Because the trace is curved and the wavelength_range may span the
    two detectors, y_min of the projection may be at an arbitrary wavelength.
    The transform is run with a regularly sampled wavelengths to determin y_min.

    Parameters
    ----------
    slit2detector : `astropy.modeling.core.Model`
        The transform from slit to detector.
    wavelength_range : tuple
        The wavelength range for the combination of grating and filter.

    """
    lam_min, lam_max = wavelength_range

    step = 1e-10
    nsteps = int((lam_max - lam_min) / step)
    lam_grid = np.linspace(lam_min, lam_max, nsteps)
    x_range_low, y_range_low = slit2detector([0] * nsteps, [slit_ymin] * nsteps, lam_grid)
    x_range_high, y_range_high = slit2detector([0] * nsteps, [slit_ymax] * nsteps, lam_grid)
    x_range = np.hstack((x_range_low, x_range_high))
    y_range = np.hstack((y_range_low, y_range_high))
    # add 10 px margin
    # The -1 is technically because the output of slit2detector is 1-based coordinates.
    x0 = max(0, x_range.min() - 1 - 10)
    x1 = min(2047, x_range.max() - 1 + 10)
    # add 2 px margin
    y0 = y_range.min() - 1 - 2
    y1 = y_range.max() - 1 + 2

    bounding_box = ((x0, x1), (y0, y1))
    return bounding_box


def collimator_to_gwa(reference_files, disperser):
    """
    Transform from COLLIMATOR to GWA frame.

    Parameters
    ----------
    reference_files: dict
        Dictionary with reference files returned by CRDS.
    disperser : dict
        A corrected disperser ASDF object.

    Returns
    -------
    model : `~astropy.modeling.core.Model` model.
        Transform from COLLIMATOR to GWA frame.

    """
    with CollimatorModel(reference_files['collimator']) as f:
        #collimator = f.tree['model'].copy()
        collimator = f.model
    angles = [disperser['theta_x'], disperser['theta_y'],
              disperser['theta_z'], disperser['tilt_y']]
    rotation = Rotation3DToGWA(angles, axes_order="xyzy", name='rotation')
    u2dircos = Unitless2DirCos(name='unitless2directional_cosines')

    return collimator.inverse | u2dircos | rotation


def get_disperser(input_model, disperserfile):
    """
    Return the disperser information corrected for the uncertainty in the GWA position.

    Parameters
    ----------
    input_model : `jwst.datamodels.DataModel`
        The input data model - either an ImageModel or a CubeModel.
    disperserfile : str
        The name of the disperser reference file.

    Returns
    -------
    disperser : dict
        The corrected disperser information.
    """
    #with DisperserModel(disperserfile) as f:
    #    disperser = f.tree
    disperser = DisperserModel(disperserfile)
    xtilt = input_model.meta.instrument.gwa_xtilt
    ytilt = input_model.meta.instrument.gwa_ytilt
    disperser = correct_tilt(disperser, xtilt, ytilt)
    return disperser


def correct_tilt(disperser, xtilt, ytilt):
    """
    Correct the tilt of the grating by a measured grating tilt angle.

    Parameters
    ----------
    xtilt : float
        Value of GWAXTILT keyword - angle in arcsec
    ytilt : float
        Value of GWAYTILT keyword - angle in arcsec
    disperser : dict
        Disperser information.

    """
    def _get_correction(gwa_tilt, tilt_angle):
        phi_exposure = gwa_tilt.tilt_model(tilt_angle)
        phi_calibrator = gwa_tilt.tilt_model(gwa_tilt.zeroreadings[0])
        del_theta = 0.5 * (phi_exposure - phi_calibrator) / 3600. #in deg
        return del_theta

    disp = disperser.copy()
    log.info("gwa_ytilt is {0}".format(ytilt))
    log.info("gwa_xtilt is {0}".format(xtilt))

    if xtilt is not None:
        theta_y_correction = _get_correction(disperser.gwa_tiltx, xtilt)
        log.info('theta_y correction: {0}'.format(theta_y_correction))
        disp['theta_y'] = disperser.theta_y + theta_y_correction
    else:
        log.info('gwa_xtilt not applied')
    if ytilt is not None:
        theta_x_correction = _get_correction(disperser.gwa_tilty, ytilt)
        log.info('theta_x correction: {0}'.format(theta_x_correction))
        disp.theta_x = disperser.theta_x + theta_x_correction
    else:
        log.info('gwa_ytilt not applied')
    return disp


def ifu_msa_to_oteip(reference_files):
    """
    Transform from the MSA frame to the OTEIP frame.

    Parameters
    ----------
    reference_files: dict
        Dictionary with reference files returned by CRDS.

    Returns
    -------
    model : `~astropy.modeling.core.Model` model.
        Transform from MSA to OTEIP.
    """
    with FOREModel(reference_files['fore']) as f:
        #fore = f.tree['model'].copy()
        fore = f.model
    with IFUFOREModel(reference_files['ifufore']) as f:
        ifufore = f.model

    msa2fore_mapping = Mapping((0, 1, 2, 2))
    msa2fore_mapping.inverse = Identity(3)
    ifu_fore_transform = ifufore & Identity(1)
    ifu_fore_transform.inverse = Mapping((0, 1, 2, 2)) | ifufore.inverse & Identity(1)
    fore_transform = msa2fore_mapping | fore & Identity(1)
    return msa2fore_mapping | ifu_fore_transform | fore_transform


def msa_to_oteip(reference_files):
    """
    Transform from the MSA frame to the OTEIP frame.

    Parameters
    ----------
    reference_files: dict
        Dictionary with reference files returned by CRDS.

    Returns
    -------
    model : `~astropy.modeling.core.Model` model.
        Transform from MSA to OTEIP.

    """
    with FOREModel(reference_files['fore']) as f:
        fore = f.model
    msa2fore_mapping = Mapping((0, 1, 2, 2), name='msa2fore_mapping')
    msa2fore_mapping.inverse = Identity(3)
    return msa2fore_mapping | (fore & Identity(1))


def oteip_to_v23(reference_files):
    """
    Transform from the OTEIP frame to the V2V3 frame.

    Parameters
    ----------
    reference_files: dict
        Dictionary with reference files returned by CRDS.

    Returns
    -------
    model : `~astropy.modeling.core.Model` model.
        Transform from OTEIP to V2V3.

    """
    with OTEModel(reference_files['ote']) as f:
        ote = f.model
    fore2ote_mapping = Identity(3, name='fore2ote_mapping')
    fore2ote_mapping.inverse = Mapping((0, 1, 2, 2))
    # Create the transform to v2/v3/lambda.  The wavelength units up to this point are
    # meters as required by the pipeline but the desired output wavelength units is microns.
    # So we are going to Scale the spectral units by 1e6 (meters -> microns)
    # The spatial units are currently in deg. Convertin to arcsec.
    oteip_to_xyan = fore2ote_mapping | (ote & Scale(1e6))
    # TODO: The scaling arcsec --> deg should be added with the next CDP delivery.
    # In CDP3 the units are still deg.
    oteip2v23 = oteip_to_xyan #| Scale(1 / 3600) & Scale(1 / 3600)  & Identity(1)

    return oteip2v23


def create_frames():
    """
    Create the coordinate frames in the NIRSPEC WCS pipeline.

    These are
    "detector", "gwa", "slit_frame", "msa_frame", "oteip", "v2v3", "world".
    """
    det = cf.Frame2D(name='detector', axes_order=(0, 1))
    sca = cf.Frame2D(name='sca', axes_order=(0, 1))
    gwa = cf.Frame2D(name="gwa", axes_order=(0, 1), unit=(u.rad, u.rad),
                      axes_names=('alpha_in', 'beta_in'))
    msa_spatial = cf.Frame2D(name='msa_spatial', axes_order=(0, 1), unit=(u.m, u.m),
                             axes_names=('x_msa', 'y_msa'))
    slit_spatial = cf.Frame2D(name='slit_spatial', axes_order=(0, 1), unit=("", ""),
                             axes_names=('x_slit', 'y_slit'))
    sky = cf.CelestialFrame(name='sky', axes_order=(0, 1), reference_frame=coord.ICRS())
    v2v3_spatial = cf.Frame2D(name='v2v3_spatial', axes_order=(0, 1), unit=(u.deg, u.deg),
                             axes_names=('V2', 'V3'))

    # The oteip_to_v23 incorporates a scale to convert the spectral units from
    # meters to microns.  So the v2v3 output frame will be in u.deg, u.deg, u.micron
    spec = cf.SpectralFrame(name='spectral', axes_order=(2,), unit=(u.micron,),
                            axes_names=('wavelength',))
    v2v3 = cf.CompositeFrame([v2v3_spatial, spec], name='v2v3')
    slit_frame = cf.CompositeFrame([slit_spatial, spec], name='slit_frame')
    msa_frame = cf.CompositeFrame([msa_spatial, spec], name='msa_frame')
    oteip_spatial = cf.Frame2D(name='oteip', axes_order=(0, 1), unit=(u.deg, u.deg),
                               axes_names=('X_OTEIP', 'Y_OTEIP'))
    oteip = cf.CompositeFrame([oteip_spatial, spec], name='oteip')
    world = cf.CompositeFrame([sky, spec], name='world')
    return det, sca, gwa, slit_frame, msa_frame, oteip, v2v3, world


def create_imaging_frames():
    """
    Create the coordinate frames in the NIRSPEC WCS pipeline.
    These are
    "detector", "gwa", "msa_frame", "oteip", "v2v3", "world".
    """
    det = cf.Frame2D(name='detector', axes_order=(0, 1))
    sca = cf.Frame2D(name='sca', axes_order=(0, 1))
    gwa = cf.Frame2D(name="gwa", axes_order=(0, 1), unit=(u.rad, u.rad),
                      axes_names=('alpha_in', 'beta_in'))
    msa = cf.Frame2D(name='msa', axes_order=(0, 1), unit=(u.m, u.m),
                             axes_names=('x_msa', 'y_msa'))
    v2v3 = cf.Frame2D(name='v2v3', axes_order=(0, 1), unit=(u.deg, u.deg),
                              axes_names=('v2', 'v3'))
    oteip = cf.Frame2D(name='oteip', axes_order=(0, 1), unit=(u.deg, u.deg),
                               axes_names=('x_oteip', 'y_oteip'))
    world = cf.CelestialFrame(name='world', axes_order=(0, 1), reference_frame=coord.ICRS())
    return det, sca, gwa, msa, oteip, v2v3, world


def get_slit_location_model(slitdata):
    """
    Compute the transform for the absolute position of a slit.

    Parameters
    ----------
    slitdata : ndarray
        An array of shape (5,) with elements:
        slit_id, xcenter, ycenter, xsize, ysize
        This is the slit info in the MSa description file.

    Returns
    -------
    model : `~astropy.modeling.core.Model` model.
        A model which transforms relative position on the slit to
        absolute positions in the quadrant..
        This is later combined with the quadrant model to return
        absolute positions in the MSA.
    """
    num, xcenter, ycenter, xsize, ysize = slitdata
    model = models.Scale(xsize) & models.Scale(ysize) | \
            models.Shift(xcenter) & models.Shift(ycenter)
    return model


def gwa_to_ymsa(msa2gwa_model):
    """
    Determine the linear relation d_y(beta_in) for the aperture on the detector.

    Parameters
    ----------
    msa2gwa_model : `astropy.modeling.core.Model`
        The transform from the MSA to the GWA.
    """
    dy = np.linspace(-.55, .55, 1000)
    dx = np.zeros(dy.shape)
    cosin_grating_k = msa2gwa_model(dx, dy)
    fitter = fitting.LinearLSQFitter()
    model = models.Polynomial1D(1)
    poly1d_model = fitter(model, cosin_grating_k[1], dy)
    poly_model = poly1d_model.rename('interpolation')
    return poly_model


def nrs_wcs_set_input(input_model, slit_name, wavelength_range=None):
    """
    Returns a WCS object for this slit.

    Parameters
    ----------
    input_model : `~jwst.datamodels.DataModel`
        A WCS object for the all open slitlets in an observation.
    slit_name : int or str
        Slit.name of an open slit.
    wavelength_range: list
        Wavelength range for the combination of fliter and grating.

    Returns
    -------
    wcsobj : `~gwcs.wcs.WCS`
        WCS object for this slit.
    """
    import copy # TODO: Add a copy method to gwcs.WCS
    wcsobj = input_model.meta.wcs
    if wavelength_range is None:
        _, wrange = spectral_order_wrange_from_model(input_model)
    else:
        wrange = wavelength_range
    slit_wcs = copy.deepcopy(wcsobj)
    slit_wcs.set_transform('sca', 'gwa', wcsobj.pipeline[1][1][1:])
    # get the open slits from the model
    # Need them to get the slit ymin,ymax
    g2s = wcsobj.pipeline[2][1]
    open_slits = g2s.slits

    slit_wcs.set_transform('gwa', 'slit_frame', g2s.get_model(slit_name))
    slit_wcs.set_transform('slit_frame', 'msa_frame', wcsobj.pipeline[3][1][1].get_model(slit_name) & Identity(1))
    slit2detector = slit_wcs.get_transform('slit_frame', 'detector')

    if input_model.meta.exposure.type.lower() != 'nrs_ifu':
        slit = [s for s in open_slits if s.name == slit_name][0]
        bb = compute_bounding_box(slit2detector, wrange, slit_ymin=slit.ymin, slit_ymax=slit.ymax)
    else:
        bb = compute_bounding_box(slit2detector, wrange)
    slit_wcs.bounding_box = bb
    return slit_wcs


def validate_open_slits(input_model, open_slits, reference_files):
    """
    For each slit computes the transform from the slit to the detector.

    Parameters
    ----------
    input_model : jwst.datamodels.DataModel
        Input data model

    Returns
    -------
    slit2det : dict
        A dictionary with the slit to detector transform for each slit,
        {slit_id: astropy.modeling.Model}
    """

    def _is_valid_slit(domain):
        xlow, xhigh = domain[0]
        ylow, yhigh = domain[1]
        if xlow >= 2048 or ylow >= 2048 or xhigh <= 0 or yhigh <= 0:
            return False
        else:
            return True

    det2dms = dms_to_sca(input_model).inverse
    # read models from reference files
    with FPAModel(reference_files['fpa']) as f:
        #fpa = f.tree[input_model.meta.instrument.detector].copy()
        fpa = getattr(f, input_model.meta.instrument.detector.lower() + '_model')
    with CameraModel(reference_files['camera']) as f:
        camera = f.model
    #with DisperserModel(reference_files['disperser']) as f:
        #disperser = f.tree
    disperser = DisperserModel(reference_files['disperser'])

    dircos2u = DirCos2Unitless(name='directional2unitless_cosines')
    disperser = correct_tilt(disperser, input_model.meta.instrument.gwa_xtilt,
                             input_model.meta.instrument.gwa_ytilt)
    angles = [disperser['theta_x'], disperser['theta_y'],
              disperser['theta_z'], disperser['tilt_y']]
    rotation = Rotation3DToGWA(angles, axes_order="xyzy", name='rotation')

    order, wrange = get_spectral_order_wrange(input_model, reference_files['wavelengthrange'])
    agreq = angle_from_disperser(disperser, input_model)
    # GWA to detector
    gwa2det = rotation.inverse | dircos2u | camera.inverse | fpa.inverse
    # collimator to GWA
    collimator2gwa = collimator_to_gwa(reference_files, disperser)

    msa = MSAModel(reference_files['msa'])
    col2det = collimator2gwa & Identity(1) | Mapping((3, 0, 1, 2)) | agreq | \
            gwa2det | det2dms
    for quadrant in range(1, 6):
        slits_in_quadrant = [s for s in open_slits if s.quadrant == quadrant]
        if any(slits_in_quadrant):
            msa_quadrant = getattr(msa, "Q{0}".format(quadrant))
            #msa_model = msa.tree[quadrant]['model']
            #msa_data = msa.tree[quadrant]['data']
            msa_model = msa_quadrant.model
            msa_data = msa_quadrant.data
            for slit in slits_in_quadrant:
                slit_id = slit.shutter_id
                slitdata = msa_data[slit_id]
                slitdata_model = get_slit_location_model(slitdata)
                msa_transform = slitdata_model | msa_model
                msa2det = msa_transform & Identity(1) | col2det
                bb = compute_bounding_box(msa2det, wrange, slit.ymin, slit.ymax)
                valid = _is_valid_slit(bb)
                if not valid:
                    log.info("Removing slit {0} from the list of open slits because the"
                             "WCS bounding_box is completely outside the detector.".format(slit.name))
                    log.debug("Slit bounding_box is {0}".format(bb))
                    idx = np.nonzero([s.name == slit.name for s in open_slits])[0][0]
                    open_slits.pop(idx)

    msa.close()
    return open_slits


def spectral_order_wrange_from_model(input_model):
    """
    Return the spectral order and wavelength range used in the WCS.

    Parameters
    ----------
    input_model : jwst.datamodels.DataModel
        The data model. Must have been through the assign_wcs step.

    """
    wrange = [input_model.meta.wcsinfo.waverange_start, input_model.meta.wcsinfo.waverange_end]
    spectral_order = input_model.meta.wcsinfo.spectral_order
    return spectral_order, wrange


def nrs_ifu_wcs(input_model):
    """
    Return a list of WCS for all NIRSPEC IFU slits.

    Parameters
    ----------
    input_model : jwst.datamodels.DataModel
        The data model. Must have been through the assign_wcs step.
    """
    _, wrange = spectral_order_wrange_from_model(input_model)
    wcs_list = []
    # loop over all IFU slits
    for i in range(30):
        wcs_list.append(nrs_wcs_set_input(input_model, i, wrange))
    return wcs_list


exp_type2transform = {'nrs_tacq': imaging,
                      'nrs_taslit': imaging,
                      'nrs_taconfirm': imaging,
                      'nrs_confirm': imaging,
                      'nrs_fixedslit': slits_wcs,
                      'nrs_ifu': ifu,
                      'nrs_msaspec': slits_wcs,
                      'nrs_image': imaging,
                      'nrs_focus': imaging,
                      'nrs_mimf': imaging,
                      'nrs_bota': imaging,
                      'nrs_autoflat': not_implemented_mode,
                      'nrs_autowave': not_implemented_mode,
                      'nrs_lamp': slits_wcs,
                      'nrs_brightobj': slits_wcs,
                      'nrs_dark': not_implemented_mode,
                      }
