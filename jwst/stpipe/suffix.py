"""Suffix manipulation"""
from copy import copy
from importlib import import_module
from inspect import (getmembers, isclass)
import logging
from os import (listdir, path, walk)
import re
import sys

from . import Step

__all__ = ['KNOW_SUFFIXES']

# Suffixes that are hard-coded or otherwise
# have to exist. Used by `find_suffixes` to
# add to the result it produces.
SUFFIXES_TO_ADD = set((
    'cal', 'calints', 'crf', 'crfints',
    'dark',
    'i2d',
    'jump',
    'psfalign', 'psfstack', 'psfsub',
    'ramp', 'rate', 'rateints',
    's2d', 's3d',
    'uncal',
    'wfscmb',
    'x1d', 'x1dints',
))

# Suffixes that are discovered but should not be considered.
# Used by `find_suffixes` to remove undesired values it has found.
SUFFIXES_TO_DISCARD = set(('functionwrapper', 'systemcall'))

# --------------------------------------------------
# The set of suffixes used by the pipeline.
# This set is generated by `find_suffixes`.
# Only update this list by `find_suffixes`.
# Modify `SUFFIXES_TO_ADD` and `SUFFIXES_TO_DISCARD`
# to change the results.
# --------------------------------------------------
KNOW_SUFFIXES = set((
    'alignrefs',
    'alignrefsstep',
    'ami3pipeline',
    'ami_analyze',
    'ami_average',
    'ami_normalize',
    'amianalyzestep',
    'amiaveragestep',
    'aminormalizestep',
    'assign_wcs',
    'assignwcsstep',
    'background',
    'backgroundstep',
    'barshadowstep',
    'cal',
    'calints',
    'combine1dstep',
    'combine_1d',
    'coron3pipeline',
    'crf',
    'crfints',
    'cube_build',
    'cubebuildstep',
    'cubeskymatchstep',
    'dark',
    'dark_current',
    'darkcurrentstep',
    'darkpipeline',
    'detector1pipeline',
    'dq_init',
    'dqinitstep',
    'emission',
    'emissionstep',
    'engdblog',
    'engdblogstep',
    'extract1dstep',
    'extract2dstep',
    'extract_1d',
    'extract_2d',
    'firstframe',
    'firstframestep',
    'flat_field',
    'flatfieldstep',
    'fringe',
    'fringestep',
    'gain_scale',
    'gainscalestep',
    'group_scale',
    'groupscalestep',
    'guidercds',
    'guidercdsstep',
    'guiderpipeline',
    'hlsp',
    'hlspstep',
    'i2d',
    'image2pipeline',
    'image3pipeline',
    'imprint',
    'imprintstep',
    'ipc',
    'ipcstep',
    'jump',
    'jumpstep',
    'klip',
    'klipstep',
    'lastframe',
    'lastframestep',
    'linearity',
    'linearitystep',
    'linearpipeline',
    'mrs_imatch',
    'mrsimatchstep',
    'msaflagopenstep',
    'outlier_detection',
    'outlier_detection_scaled',
    'outlierdetectionscaledstep',
    'outlierdetectionstackstep',
    'outlierdetectionstep',
    'pathloss',
    'pathlossstep',
    'persistence',
    'persistencestep',
    'photom',
    'photomstep',
    'pipeline',
    'psfalign',
    'psfstack',
    'psfsub',
    'ramp',
    'rampfit',
    'rampfitstep',
    'rate',
    'rateints',
    'refpix',
    'refpixstep',
    'resample',
    'resample_spec',
    'resamplespecstep',
    'resamplestep',
    'reset',
    'resetstep',
    'rscd',
    'rscd_step',
    's2d',
    's3d',
    'saturation',
    'saturationstep',
    'skymatch',
    'skymatchstep',
    'source_catalog',
    'sourcecatalogstep',
    'sourcetypestep',
    'spec2pipeline',
    'spec3pipeline',
    'srctype',
    'stackrefs',
    'stackrefsstep',
    'step',
    'straylight',
    'straylightstep',
    'subtract_images',
    'subtractimagesstep',
    'superbias',
    'superbiasstep',
    'testlinearpipeline',
    'tso3pipeline',
    'tsophotometrystep',
    'tweakreg',
    'tweakregstep',
    'uncal',
    'wfscmb',
    'wfscombine',
    'wfscombinestep',
    'white_light',
    'whitelightstep',
    'x1d',
    'x1dints'
))


# #####################################
# Functions to generate `KNOW_SUFFIXES`
# #####################################
def find_suffixes(
        suffixes_to_add=SUFFIXES_TO_ADD,
        suffixes_to_discard=SUFFIXES_TO_DISCARD
):
    """Find all possible suffixes from the jwst package

    Parameters
    ----------
    suffixes_to_add: set or list
        Suffixes to add to the result

    suffixes_to_discard: set or list
        Suffixes to ensure are not in the result

    Returns
    -------
    suffixes: set
        The set of all programmatically findable suffixes.

    Notes
    -----
    This will load all of the `jwst` package. Consider if this
    is worth doing dynamically or only as a utility to update
    a static list.
    """
    suffixes = set()

    jwst = import_module('jwst')
    jwst_fpath = path.split(jwst.__file__)[0]

    # First traverse the code base and find all
    # `Step` classes. The default suffix is the
    # class name.
    for module in load_local_pkg(jwst_fpath):
        for klass_name, klass in getmembers(
            module,
            lambda o: isclass(o) and issubclass(o, Step)
        ):
            suffixes.add(klass_name.lower())

    # Instantiate Steps/Pipelines from their configuration files.
    # Different names and suffixes can be defined in this way.
    # Note: Based on the `collect_pipeline_cfgs` script
    config_path = path.join(jwst_fpath, 'pipeline')
    for config_file in listdir(config_path):
        if config_file.endswith('.cfg'):
            try:
                step = Step.from_config_file(
                    path.join(config_path, config_file)
                )
            except Exception as exception:
                pass
            else:
                suffixes.add(step.name.lower())
                if step.suffix is not None:
                    suffixes.add(step.suffix.lower())

    # Discard known bad finds.
    suffixes.difference_update(suffixes_to_discard)

    # Add defined suffixes
    suffixes.update(suffixes_to_add)

    # That's all folks
    return suffixes


def load_local_pkg(fpath):
    """Generator producing all modules under fpath

    Parameters
    ----------
    fpath: string
        File path to the package to load.

    Returns
    -------
    generator
        `module` for each module found in the package.
    """
    package_fpath, package = path.split(fpath)
    package_fpath_len = len(package_fpath) + 1
    sys_path = copy(sys.path)
    sys.path.insert(0, package_fpath)
    try:
        for module_fpath in folder_traverse(
            fpath, basename_regex='[^_].+\.py$', path_exclude_regex='tests'
        ):
            folder_path, fname = path.split(module_fpath[package_fpath_len:])
            module_path = folder_path.split('/')
            module_path.append(path.splitext(fname)[0])
            module_path = '.'.join(module_path)
            try:
                module = import_module(module_path)
            except Exception:
                logging.warning('Cannot load module "{}"'.format(module_path))
            else:
                yield module
    except Exception as exception:
        logging.warning('Exception occurred: "{}'.format(exception))
    finally:
        sys.path = sys_path


def folder_traverse(folder_path, basename_regex='.+', path_exclude_regex='^$'):
    """Generator of full file paths for all files
    in a folder.

    Parameters
    ----------
    folder_path: str
        The folder to traverse

    basename_regex: str
        Regular expression that must match
        the `basename` part of the file path.

    path_exclude_regex: str
        Regular expression to exclude a path.

    Returns
    -------
    generator
        A generator, return the next file.
    """
    basename_regex = re.compile(basename_regex)
    path_exclude_regex = re.compile(path_exclude_regex)
    for root, dirs, files in walk(folder_path):
        if path_exclude_regex.search(root):
            continue
        for file in files:
            if basename_regex.match(file):
                yield path.join(root, file)


# ############################################
# Main
# Find and report differences from known list.
# ############################################
if __name__ == '__main__':
    print('Searching code base for calibration suffixes...')
    found_suffixes = find_suffixes()
    print(
        'Known list has {known_len} suffixes.'
        ' Found {new_len} suffixes.'.format(
            known_len=len(KNOW_SUFFIXES),
            new_len=len(found_suffixes)
        )
    )
    print(
        'Suffixes that have changed are {}'.format(
            found_suffixes.symmetric_difference(KNOW_SUFFIXES)
        )
    )
