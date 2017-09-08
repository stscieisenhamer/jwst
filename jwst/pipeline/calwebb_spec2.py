#!/usr/bin/env python
from collections import defaultdict
from memory_profiler import profile
import weakref

from .. import datamodels
from ..associations.load_as_asn import LoadAsLevel2Asn
from ..stpipe import Pipeline

# step imports
from ..assign_wcs import assign_wcs_step
from ..background import background_step
from ..imprint import imprint_step
from ..msaflagopen import msaflagopen_step
from ..extract_2d import extract_2d_step
from ..flatfield import flat_field_step
from ..srctype import srctype_step
from ..straylight import straylight_step
from ..fringe import fringe_step
from ..pathloss import pathloss_step
from ..photom import photom_step
from ..cube_build import cube_build_step
from ..extract_1d import extract_1d_step
from ..resample import resample_spec_step

__version__ = "3.0"


class Spec2Pipeline(Pipeline):
    """
    Spec2Pipeline: Processes JWST spectroscopic exposures from Level 2a to 2b.
    Accepts a single exposure or an association as input.

    Included steps are:
    assign_wcs, background subtraction, NIRSpec MSA imprint subtraction,
    NIRSpec MSA bad shutter flagging, 2-D subwindow extraction, flat field,
    source type decision, straylight, fringe, pathloss, photom, resample_spec,
    cube_build, and extract_1d.
    """

    spec = """
        save_bsub = boolean(default=False) # Save background-subracted science
    """

    # Define aliases to steps
    step_defs = {
        'bkg_subtract': background_step.BackgroundStep,
        'assign_wcs': assign_wcs_step.AssignWcsStep,
        'imprint_subtract': imprint_step.ImprintStep,
        'msa_flagging': msaflagopen_step.MSAFlagOpenStep,
        'extract_2d': extract_2d_step.Extract2dStep,
        'flat_field': flat_field_step.FlatFieldStep,
        'srctype': srctype_step.SourceTypeStep,
        'straylight': straylight_step.StraylightStep,
        'fringe': fringe_step.FringeStep,
        'pathloss': pathloss_step.PathLossStep,
        'photom': photom_step.PhotomStep,
        'resample_spec': resample_spec_step.ResampleSpecStep,
        'cube_build': cube_build_step.CubeBuildStep,
        'extract_1d': extract_1d_step.Extract1dStep
    }

    # Main processing
    @profile
    def process(self, input):
        """Entrypoint for this pipeline

        Parameters
        ----------
        input: str, Level2 Association, or DataModel
            The exposure or association of exposures to process
        """
        self.log.info('Starting calwebb_spec2 ...')

        # Retrieve the input(s)
        asn = LoadAsLevel2Asn.load(input, basename=self.output_file)

        # Each exposure is a product in the association.
        # Process each exposure.
        weakrefs = []
        for product in asn['products']:
            self.log.info('Processing product {}'.format(product['name']))
            self.output_basename = product['name']
            result = self.process_exposure_product(
                product,
                asn['asn_pool'],
                asn.filename
            )

            # Save result
            suffix = 'cal'
            if isinstance(result, datamodels.CubeModel):
                suffix = 'calints'
            self.save_model(result, suffix)

            self.closeout(to_close=[result])

            weakrefs.append(weakref.ref(result))

        # We're done
        self.log.info('Ending calwebb_spec2')
        self.log.warning(
            'Result references still alive. Total={}):'.format(len(weakrefs))
        )
        for idx, wr in enumerate(weakrefs):
            if wr() is not None:
                self.log.warning('\nresult #{} is alive')

    # Process each exposure
    @profile
    def process_exposure_product(
            self,
            exp_product,
            pool_name=' ',
            asn_file=' '
    ):
        """Process an exposure found in the association product

        Parameters
        ---------
        exp_product: dict
            A Level2b association product.
        """

        # Find all the member types in the product
        members_by_type = defaultdict(list)
        for member in exp_product['members']:
            members_by_type[member['exptype'].lower()].append(member['expname'])

        # Get the science member. Technically there should only be
        # one. We'll just get the first one found.
        science = members_by_type['science']
        if len(science) != 1:
            self.log.warn(
                'Wrong number of science exposures found in {}'.format(
                    exp_product['name']
                )
            )
            self.log.warn('    Using only first one.')
        science = science[0]

        self.log.info('Working on input %s ...', science)
        if isinstance(science, datamodels.DataModel):
            input = science
        else:
            input = datamodels.open(science)
        exp_type = input.meta.exposure.type

        # Apply WCS info
        input = self.assign_wcs(input)

        # Do background processing, if necessary
        if len(members_by_type['background']) > 0:

            # Setup for saving
            self.bkg_subtract.suffix = 'bsub'
            if isinstance(input, datamodels.CubeModel):
                self.bkg_subtract.suffix = 'bsubints'

            # Backwards compatibility
            if self.save_bsub:
                self.bkg_subtract.save_results = True

            # Call the background subtraction step
            input = self.bkg_subtract(input, members_by_type['background'])

        # If assign_wcs was skipped, abort the rest of processing,
        # because so many downstream steps depend on the WCS
        if input.meta.cal_step.assign_wcs == 'SKIPPED':
            self.log.error('Assign_wcs processing was skipped')
            self.log.error('Aborting remaining processing for this exposure')
            self.log.error('No output product will be created')
            return input

        # Apply NIRSpec MSA imprint subtraction
        # Technically there should be just one.
        # We'll just get the first one found
        imprint = members_by_type['imprint']
        if exp_type in ['NRS_MSASPEC', 'NRS_IFU'] and \
           len(imprint) > 0:
            if len(imprint) > 1:
                self.log.warn('Wrong number of imprint members')
            imprint = imprint[0]
            input = self.imprint_subtract(input, imprint)

        # Apply NIRSpec MSA bad shutter flagging
        if exp_type in ['NRS_MSASPEC', 'NRS_IFU']:
            input = self.msa_flagging(input)

        # Extract 2D sub-windows for NIRSpec slit and MSA
        if exp_type in ['NRS_FIXEDSLIT', 'NRS_BRIGHTOBJ', 'NRS_MSASPEC']:
            input = self.extract_2d(input)

        # Apply flat-field correction
        input = self.flat_field(input)

        # Apply the source type decision step
        input = self.srctype(input)

        # Apply the straylight correction for MIRI MRS
        if exp_type == 'MIR_MRS':
            input = self.straylight(input)

        # Apply the fringe correction for MIRI MRS
        if exp_type == 'MIR_MRS':
            input = self.fringe(input)

        # Apply pathloss correction to NIRSpec exposures
        if exp_type in ['NRS_FIXEDSLIT', 'NRS_BRIGHTOBJ', 'NRS_MSASPEC',
                        'NRS_IFU']:
            input = self.pathloss(input)

        # Apply flux calibration
        input = self.photom(input)

        # Record ASN pool and table names in output
        input.meta.asn.pool_name = pool_name
        input.meta.asn.table_name = asn_file

        # Setup to save the calibrated exposure at end of step.
        self.suffix = 'cal'
        if isinstance(input, datamodels.CubeModel):
            self.suffix = 'calints'

        # Produce a resampled product, either via resample_spec for
        # "regular" spectra or cube_build for IFU data. No resampled
        # product is produced for time-series modes.
        if input.meta.exposure.type in [
                'NRS_FIXEDSLIT', 'NRS_BRIGHTOBJ',
                'NRS_MSASPEC', 'NIS_WFSS', 'NRC_GRISM'
        ]:

            # Call the resample_spec step
            self.resample_spec.suffix = 's2d'
            resamp = self.resample_spec(input)

            # Pass the resampled data to 1D extraction
            x1d_input = resamp.copy()
            resamp.close()

        elif exp_type in ['MIR_MRS', 'NRS_IFU']:

            # Call the cube_build step for IFU data
            self.cube_build.suffix = 's3d'
            cube = self.cube_build(input)

            # Pass the cube along for input to 1D extraction
            x1d_input = cube.copy()
            cube.close()

        else:
            # Pass the unresampled cal product to 1D extraction
            x1d_input = input

        # Extract a 1D spectrum from the 2D/3D data
        self.extract_1d.suffix = 'x1d'
        if isinstance(input, datamodels.CubeModel):
            self.extract_1d.suffix = 'x1dints'
        x1d_output = self.extract_1d(x1d_input)

        x1d_input.close()
        input.close()
        x1d_output.close()

        # That's all folks
        self.log.info(
            'Finished processing product {}'.format(exp_product['name'])
        )
        return input
