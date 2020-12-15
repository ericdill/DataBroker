import xarray
from importlib import import_module

from .core import BlueskyRun


__all__ = ['Projector', 'project_xarray']


class ProjectionError(Exception):
    pass


def get_run_projection(run: BlueskyRun, projection_name: str = None):
    """Finds a projection in the run.
    If projection_name is provided, searches through the projections in the run
    to find a match.

    Otherwise, looks in the run to see if there is only one projection. If so, returns it.

    Parameters
    ----------
    run : BlueskyRun
        Run to investigate for a projection
    projection_name : str, optional
        name of the projection to look for, by default None

    Returns
    -------
    dict
        returns a projection dictionary, or None of not found

    Raises
    ------
    KeyError
        If the a projection_name is specified and there is more than one
        projection in the run with that name
    """

    if projection_name is not None:
        projections = [projection for projection in run.metadata['start']['projections']
                       if projection.get('name') == projection_name]
        if len(projections) > 1:
            raise KeyError("Multiple projections of name {projection_name} found")
        if len(projections) == 1:
            return projections[0]
        if len(projections) == 0:
            return None

    if 'projections' in run.metadata['start'] and len(run.metadata['start']['projections']) == 1:
        return run.metadata['start']['projections'][0]

    return None


def get_calculated_value(run: BlueskyRun, key: str, mapping: dict):
    """Calls and returns the callable from the calculated projection mapping.

    It is ancticipated that the return will be
    and xarray.DataArray.

    This should be expressed in the familiar 'module:func' syntax borrowed from python entry-points.

    An example implementation of a calculated field projection entry:

        '/projection/key': {
            "type": "calculated",
            "callable": "foo.bar:really_fun",
            "args": ['arg1'], "kwargs": {"foo": "bar"}}

    And a corresponding function implementation might be:

        def really_fun(run, *args, **kwargs)"
            # args will be ['arg1']
            # kwargs will be {"foo": "bar"}
            # for this calculated field
            return xarray.DataArray[[1, 2, 3]]


    Parameters
    ----------
    run : BlueskyRun
        run which can be used for the calcuation
    key : str
        key name for this projection
    mapping : dict
        full contents of this projection

    Returns
    -------
    any
        result of calling the method specified in the calcated field in the projection

    Raises
    ------
    ProjectionError
        [description]
    """
    callable_name = mapping['callable']
    try:
        module_name, function_name = callable_name.split(":")
        module = import_module(module_name)
        callable_func = getattr(module, function_name)
    except ProjectionError as e:
        raise ProjectionError('Error importing callable {function_name}', e)

    calc_args = mapping['args']
    calc_kwargs = mapping['kwargs']
    return callable_func(run, *calc_args, **calc_kwargs)


class Projector():
    """Helper for scanning projection and building custom porjectors.

    EXPERIMENTAL: projection code is experimental and could change in the near future.

    Handles much of the inner workings of projecting a BlueskyRun by scanning the
    projection definition and providing callbacks for the different types of items
    that can be projected.
    """

    def __init__(self, metadata_cb=None, event_configuration_cb=None, event_field_cb=None):
        """Pass optional callbacks for the various types of projected field defined in a projection

        Parameters
        ----------
        metadata_cb : callable, optional
            Receives a field name and a value for every field in the projection with
            type=linked and location=start, by default None
        event_configuration_cb : callable, optional
            Receives a stream name, field name and a value for every field in the projection with
            type=linked and location=configuration, by default None, by default None
        event_field_cb : callable, optional
            Receives a stream name, field name and a value for every field in the projection with
            type=linked and location=event, by default None, by default None
        """
        self._metadata_cb = metadata_cb
        self._event_configuration_cb = event_configuration_cb
        self._event_field_cb = event_field_cb

    def project(self, run: BlueskyRun, projection=None, projection_name=None):
        """Iterates a projection and communicates fields through callbacks.

        Selects projection based on logic of get_run_projection().

        Projections come with multiple types: linked, and caclulated. Calculated fields are only supported
        in the data (not at the top-level attrs).

        Calculated fields in projections schema contain a callable field. This should be expressed in
        the familiar 'module:func' syntax.

        All projection fields with "location"=="start" will look in the run_start
        for metadata. Each field will be added to the return Dataset's attrs dictionary keyed
        on projection key.

        All projection fields with "location"=="configuration" will look in the event_descriptor.configuration
        field for settings that appear once per stream. Each field will be added to the return Dataset's
        attrs dictionary keyed on projection key.

        All projection fields with "location"=="event" will look for a field in a stream.

        Parameters
        ----------
        run : BlueskyRun
            run to project
        projection_name : str, optional
            name of a projection to select in the run, by default None
        projection : dict, optional
            projection not from the run to use, by default None

        Returns
        -------
        xarray.Dataset
            The return Dataset will contain:
            - single value meta data (from the run start) in the return Dataset's attrs dict, keyed
            on the projection key. These are projections marked  "location": "start"

            - single value meata data (from a streams configuration field) in the return Dataset's xarray's
            dict, keyed on the projection key. These are projections marked  "location": "start"

            - multi-value data (from a stream). Keys for the dict-like xarray.Dataset match keys
            in the passed-in projection. These are projections with "location": "linked"

        Raises
        ------
        ProjectionError
        """
        if projection is None:
            projection = get_run_projection(run, projection_name)
        if projection is None:
            raise ProjectionError("Projection could not be found")

        for field_key, mapping in projection['projection'].items():
            # go through each projection
            projection_type = mapping['type']
            projection_location = mapping.get('location')
            projection_linked_field = mapping.get('field')

            if projection_location == 'start':
                if self._metadata_cb:
                    self._metadata_cb(field_key,
                                      run.metadata['start'][projection_linked_field])
                continue

            elif projection_location == 'event':
                projection_stream = mapping.get('stream')

                if projection_stream is None:
                    raise ProjectionError(f'stream missing for event projection: {field_key}')

                if projection_type == "calculated":
                    if self._event_field_cb:
                        self._event_field_cb(field_key,
                                             projection_stream,
                                             projection_linked_field,
                                             get_calculated_value(run, field_key, mapping))
                        continue

                # TODO check if field exists in stream first
                if self._event_field_cb:
                    value = run[projection_stream].to_dask()[projection_linked_field]
                    self._event_field_cb(field_key,
                                         projection_stream,
                                         projection_linked_field,
                                         value)
                    continue

            elif projection_location == 'configuration':
                if self._event_configuration_cb:
                    projection_stream = mapping['stream']
                    config_index = mapping['config_index']
                    config_device = mapping['config_device']
                    value = run.primary.metadata['descriptors'][config_index]['configuration'][config_device]
                    value = value['data'][projection_linked_field]
                    self._event_configuration_cb(field_key,
                                                 projection_stream,
                                                 config_index,
                                                 config_device,
                                                 projection_linked_field,
                                                 value)
            else:
                raise KeyError(f'Unknown location: {projection_location} in projection.')


def project_xarray(run: BlueskyRun, *args, projection=None, projection_name=None):
    """Produces an xarray Dataset by projecting the provided run.

    EXPERIMENTAL: projection code is experimental and could change in the near future.

    Projections come with multiple types: linked, and caclulated. Calculated fields are only supported
    in the data (not at the top-level attrs).

    Projected fields will be inserted into the resulting xarray.Dataset

    Parameters
    ----------
    run : BlueskyRun
        run to project
    projection_name : str, optional
        name of a projection to select in the run, by default None
    projection : dict, optional
        projection not from the run to use, by default None

    Returns
    -------
    xarray.Dataset
         The return Dataset will contain:
        - single value meta data (from the run start) in the return Dataset's attrs dict, keyed
        on the projection key. These are projections marked  "location": "start"

        - single value meta data (from a streams configuration field) in the return Dataset's xarray's dict, keyed
        on the projection key. These are projections marked  "location": "configuration"

        - multi-value data (from a stream). Keys for the dict-like xarray.Dataset match keys
        in the passed-in projection. These are projections with "location": "linked"...note that
        every xarray for a field froma given stream will contain a reference to the same set of configuration attrs
        for as all fields from the same stream

        Dataset
            |_attrs
                |_'projection_start_field': value
            |_data
                |_ 'projection_event_field': xarray
                                                |_ attrs
                                                    |_'projection_configuration_field': value

    Raises
    ------
    ProjectionError
    """
    attrs = {}  # will populate the return Dataset attrs field
    data_vars = {}  # will populate the return Dataset DataArrays
    stream_configurations = {}  # will populate a collection of dicts of stream configurations

    def metadata_cb(field, value):
        attrs[field] = value

    def event_configuration_cb(
            projection_field,
            stream,
            config_index,
            config_device,
            config_field,
            value):

        if stream not in stream_configurations:
            stream_configurations[stream] = []
        if len(stream_configurations[stream]) == 0:
            stream_configurations[stream].append({})
        if config_device not in stream_configurations[stream][config_index]:
            stream_configurations[stream][config_index][config_device] = {}

        stream_configurations[stream][config_index][config_device][config_field] = value

    def event_field_cb(projection_field,
                       stream,
                       field,
                       xarray: xarray.DataArray):

        if projection_field not in stream_configurations:
            stream_configurations[stream] = []
        # associate the stream configuration to the xarrays's atrtrs
        xarray.attrs['configuration'] = stream_configurations[stream]
        data_vars[projection_field] = xarray

    # Use the callbacks defined above to project the run and build up a return xarray.Dataset
    try:
        projector = Projector(
            metadata_cb=metadata_cb,
            event_configuration_cb=event_configuration_cb,
            event_field_cb=event_field_cb)

        projector.project(run, projection=projection, projection_name=projection_name)
        dataset = xarray.Dataset(data_vars, attrs=attrs)
        return dataset
    except Exception as e:
        raise ProjectionError from e


def get_xarray_config_field(dataset: xarray.Dataset,
                            projection_field,
                            config_index,
                            device,
                            field):
    """Reach into the dataset and get the value for the provided
       configuration fidl

    Parameters
    ----------
    dataset : xarray.Dataset
        [description]
    projection_field : [type]
        [description]
    config_index : [type]
        [description]
    device : [type]
        [description]
    field : [type]
        [description]

    Returns
    -------
    [type]
        [description]
    """
    return dataset[projection_field].attrs['configuration'][config_index][device][field]