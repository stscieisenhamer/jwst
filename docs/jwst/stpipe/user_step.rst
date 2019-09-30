=====
Steps
=====

.. _configuring-a-step:

Configuring a Step
==================

This section describes how to instantiate a Step and set configuration
parameters on it.

Steps can be configured by either:

    - Writing a configuration file
    - Instantiating the Step directly from Python

.. _running_a_step_from_a_configuration_file:

Running a Step from a configuration file
========================================

A Step configuration file contains one or more of a ``Step``'s parameters. Any
parameter not specified in the file will take its value from the CRDS-retrieved
configuration or the defaults coded directly into the ``Step``. Note that any
parameter specified on the command line overrides all other values.

The preferred format of configuration files is the :ref:`config_asdf_files`
format. Refer to the :ref:`minimal example<asdf_minimal_file>` for a complete
description of the contents. The rest of this documented will focus on the step
parameters themselves.

All step parameters appear under the ``parameters:`` section, and they must be
indented. The amount of indentation does not matter, as long as all parameters
are indented equally.

Every step configuration file must contain the parameter ``class``, followed by
the optional ``name`` followed by any parameters that are specific to the step
being run.

``class`` specifies the Python class to run.  It should be a
fully-qualified Python path to the class.  Step classes can ship with
``stpipe`` itself, they may be part of other Python packages, or they
exist in freestanding modules alongside the configuration file.  For
example, to use the ``SystemCall`` step included with ``stpipe``, set
``class`` to ``stpipe.subprocess.SystemCall``.  To use a class called
``Custom`` defined in a file ``mysteps.py`` in the same directory as
the configuration file, set ``class`` to ``mysteps.Custom``.

``name`` defines the name of the step.  This is distinct from the
class of the step, since the same class of Step may be configured in
different ways, and it is useful to be able to have a way of
distinguishing between them.  For example, when Steps are combined
into :ref:`stpipe-user-pipelines`, a Pipeline may use the same Step class
multiple times, each with different configuration parameters.

Below ``name`` and ``class`` in the configuration file are parameters
specific to the Step.  The set of accepted parameters is defined in
the Step’s spec member.  You can print out a Step’s configspec using
the ``stspec`` commandline utility.  For example, to print the
configspec for an imaginary step called `stpipe.cleanup`::

    $ stspec stpipe.cleanup
    # The threshold below which to apply cleanup
    threshold = float()

    # A scale factor
    scale = float()

    # The output file to save to
    output_file = output_file(default = None)

.. note::

    Configspec information can also be displayed from Python, just
    call ``print_configspec`` on any Step class.

.. doctest-skip::

  >>> from jwst.stpipe import cleanup
  >>> cleanup.print_configspec()
  >>> # The threshold below which to apply cleanup
  >>> threshold = float()
  >>> # A scale factor
  >>> scale = float()

Using this information, one can write a configuration file to use this
step.  For example, here is a configuration file (``do_cleanup.asdf``)
that runs the ``stpipe.cleanup`` step to clean up an image.

.. code-block::

    #ASDF 1.0.0
    #ASDF_STANDARD 1.3.0
    %YAML 1.1
    %TAG ! tag:stsci.edu:asdf/
    --- !core/asdf-1.1.0
    parameters:
      class = "stpipe.cleanup"
      name = "MyCleanup"
      threshold = 42.0
      scale = 0.01
    ...

.. _strun:

Running a Step from the commandline
-----------------------------------
The ``strun`` command can be used to run Steps from the commandline.

The first argument may be either:

    - The path to a configuration file

    - A Python class

Additional configuration parameters may be passed on the commandline.
These parameters override any that are present in the configuration
file.  Any extra positional parameters on the commandline are passed
to the step's process method.  This will often be input filenames.

For example, to use an existing configuration file from above, but
override it so the threshold parameter is different::

    $ strun do_cleanup.asdf input.fits --threshold=86

To display a list of the parameters that are accepted for a given Step
class, pass the ``-h`` parameter, and the name of a Step class or
configuration file::

    $ strun -h do_cleanup.asdf
    usage: strun [--logcfg LOGCFG] cfg_file_or_class [-h] [--pre_hooks]
                 [--post_hooks] [--skip] [--scale] [--extname]

    optional arguments:
      -h, --help       show this help message and exit
      --logcfg LOGCFG  The logging configuration file to load
      --verbose, -v    Turn on all logging messages
      --debug          When an exception occurs, invoke the Python debugger, pdb
      --pre_hooks
      --post_hooks
      --skip           Skip this step
      --scale          A scale factor
      --threshold      The threshold below which to apply cleanup
      --output_file    File to save the output to

Every step has an `--output_file` parameter.  If one is not provided,
the output filename is determined based on the input file by appending
the name of the step.  For example, in this case, `foo.fits` is output
to `foo_cleanup.fits`.

Finally, the parameters a ``Step`` actually ran with can be saved to a new
configuration file using the `--save-parameters` option. This file will have all
the parameters, specific to the step, and the final values used.

Parameter Precedence
````````````````````

There are a number of places where the value of a parameter can be specified.
The order of precedence, from most to least significant, for parameter value
assignment is as follows:

    1. Value specified on the command-line: ``strun step.asdf --par=value_that_will_be_used``
    2. Value found in the user-specified configuration file
    3. CRDS-retrieved configuration
    4. ``Step``-coded default, determined by the parameter definition ``Step.spec``

Debugging
`````````

To output all logging output from the step, add the `--verbose` option
to the commandline.  (If more fine-grained control over logging is
required, see :ref:`user-logging`).

To start the Python debugger if the step itself raises an exception,
pass the `--debug` option to the commandline.

Running a Step in Python
------------------------

Running a step can also be done inside the Python interpreter and is as simple
as calling its `run()` or `call()` classmethods.

run()
`````

The `run()` classmethod will run a previously instantiated step class. This is
very useful if one wants to setup the step's attributes first, then run it::

    from jwst.flatfield import FlatFieldStep

    mystep = FlatFieldStep()
    mystep.override_sflat = ‘sflat.fits’
    output = mystep.run(input)

Using the `.run()` method is the same as calling the instance or class directly.
They are equivalent::

    output = mystep(input)

call()
``````

If one has all the configuration in a configuration file or can pass the
arguments directly to the step, one can use call(), which creates a new
instance of the class every time you use the `call()` method.  So::

    output = mystep.call(input)

makes a new instance of `FlatFieldStep` and then runs. Because it is a new
instance, it ignores any attributes of `mystep` that one may have set earlier,
such overriding the sflat.

The nice thing about call() is that it can take a configuration file, so::

    output = mystep.call(input, config_file=’my_flatfield.asdf’)

and it will take all the configuration from the config file.

Configuration parameters may be passed to the step by setting the `config_file`
kwarg in `call` (which takes a path to a configuration file) or as keyword
arguments.  Any remaining positional arguments are passed along to the step's
`process()` method::

    from jwst.stpipe import cleanup

    cleanup.call('image.fits', config_file='do_cleanup.cfg', threshold=42.0)

So use call() if you’re passing a config file or passing along args or kwargs.
Otherwise use run().
