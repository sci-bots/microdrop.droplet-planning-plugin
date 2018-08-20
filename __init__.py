# -*- coding: utf-8 -*-
from concurrent.futures import ThreadPoolExecutor
import logging

from flatland import Integer, Form
from flatland.validation import ValueAtLeast
from microdrop.app_context import get_hub_uri
from microdrop.interfaces import IElectrodeMutator, IPlugin
from microdrop.logging_helpers import _L  #: .. versionadded: 2.4
from microdrop.plugin_helpers import (StepOptionsController, get_plugin_info,
                                      hub_execute_async)
from microdrop.plugin_manager import (PluginGlobals, Plugin, ScheduleRequest,
                                      implements)
from path_helpers import path
from zmq_plugin.plugin import Plugin as ZmqPlugin, watch_plugin
from zmq_plugin.schema import decode_content_data
import pandas as pd
import zmq

from ._version import get_versions
from .states import electrode_states

__version__ = get_versions()['version']
del get_versions

logger = logging.getLogger(__name__)

PluginGlobals.push_env('microdrop.managed')


class RouteControllerZmqPlugin(ZmqPlugin):
    '''
    API for adding/clearing droplet routes.
    '''
    def __init__(self, parent, *args, **kwargs):
        self.parent = parent
        super(RouteControllerZmqPlugin, self).__init__(*args, **kwargs)

    def check_sockets(self):
        try:
            msg_frames = self.command_socket.recv_multipart(zmq.NOBLOCK)
        except zmq.Again:
            pass
        else:
            self.on_command_recv(msg_frames)
        return True

    def on_execute__add_route(self, request):
        data = decode_content_data(request)
        try:
            return self.parent.add_route(data['drop_route'])
        except Exception:
            _L().error(str(data), exc_info=True)

    def on_execute__get_routes(self, request):
        return self.parent.get_routes()

    def on_execute__clear_routes(self, request):
        data = decode_content_data(request)
        try:
            return self.parent.clear_routes(electrode_id=data
                                            .get('electrode_id'))
        except Exception:
            _L().error(str(data), exc_info=True)


class RouteController(object):
    '''
    Manage execution of a set of routes in lock-step.
    '''
    def __init__(self, plugin):
        self.plugin = plugin
        self.route_info = {}

    @staticmethod
    def default_routes():
        return pd.DataFrame(None, columns=['route_i', 'electrode_i',
                                           'transition_i'], dtype='int32')


class DropletPlanningPlugin(Plugin, StepOptionsController):
    """
    This class is automatically registered with the PluginManager.


    .. versionchanged:: 2.4
        Refactor to implement the `IElectrodeMutator` interface, which
        delegates route execution to the
        ``microdrop.electrode_controller_plugin``.
    """
    implements(IPlugin)
    implements(IElectrodeMutator)
    version = get_plugin_info(path(__file__).parent).version
    plugin_name = get_plugin_info(path(__file__).parent).plugin_name

    '''
    StepFields
    ---------

    A flatland Form specifying the per step options for the current plugin.
    Note that nested Form objects are not supported.

    Since we subclassed StepOptionsController, an API is available to access and
    modify these attributes.  This API also provides some nice features
    automatically:
        -all fields listed here will be included in the protocol grid view
            (unless properties=dict(show_in_gui=False) is used)
        -the values of these fields will be stored persistently for each step
    '''
    StepFields = Form.of(
        Integer.named('trail_length')
        .using(default=1, optional=True, validators=[ValueAtLeast(minimum=1)]),
        Integer.named('route_repeats')
        .using(default=1, optional=True, validators=[ValueAtLeast(minimum=1)]),
        Integer.named('repeat_duration_s').using(default=0, optional=True))

    def __init__(self):
        self.name = self.plugin_name
        self._electrode_states = iter([])
        self.plugin = None
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._plugin_monitor_task = None

    def get_schedule_requests(self, function_name):
        """
        .. versionchanged:: 2.5
            Enable _after_ command plugin and zmq hub to ensure command can be
            registered.
        """
        if function_name in ['on_step_run']:
            # Execute `on_step_run` before control board.
            return [ScheduleRequest(self.name, 'dmf_control_board_plugin')]
        elif function_name == 'on_plugin_enable':
            return [ScheduleRequest('microdrop.zmq_hub_plugin', self.name),
                    ScheduleRequest('microdrop.command_plugin', self.name)]
        return []

    def on_plugin_enable(self):
        '''
        .. versionchanged:: 2.5
            - Use `zmq_plugin.plugin.watch_plugin()` to monitor ZeroMQ
              interface in background thread.
            - Register `clear_routes` commands with ``microdrop.command_plugin``.
        '''
        self.cleanup()
        self.plugin = RouteControllerZmqPlugin(self, self.name, get_hub_uri())

        self._plugin_monitor_task = watch_plugin(self.executor, self.plugin)

        hub_execute_async('microdrop.command_plugin', 'register_command',
                          command_name='clear_routes', namespace='global',
                          plugin_name=self.name, title='Clear all r_outes')
        hub_execute_async('microdrop.command_plugin', 'register_command',
                          command_name='clear_routes', namespace='electrode',
                          plugin_name=self.name, title='Clear electrode '
                          '_routes')

    def on_plugin_disable(self):
        """
        Handler called once the plugin instance is disabled.
        """
        self.cleanup()

    def on_app_exit(self):
        """
        Handler called just before the Microdrop application exits.
        """
        self.cleanup()

    def cleanup(self):
        if self.plugin is not None:
            self.plugin = None
        if self._plugin_monitor_task is not None:
            self._plugin_monitor_task.cancel()

    ###########################################################################
    # Step event handler methods
    def get_electrode_states_request(self):
        try:
            return self._electrode_states.next()
        except StopIteration:
            return None

    def on_step_options_swapped(self, plugin, old_step_number, step_number):
        """
        Handler called when the step options are changed for a particular
        plugin.  This will, for example, allow for GUI elements to be
        updated based on step specified.

        Parameters
        ----------
        plugin : plugin instance for which the step options changed
        old_step_number : int
            Previous step number.
        step_number : int
            Current step number that the options changed for.
        """
        self.reset_electrode_states_generator()

    def on_step_swapped(self, old_step_number, step_number):
        """
        Handler called when the current step is swapped.
        """
        self.reset_electrode_states_generator()

        if self.plugin is not None:
            self.plugin.execute_async(self.name, 'get_routes')

    def on_step_inserted(self, step_number, *args):
        self.clear_routes(step_number=step_number)
        self._electrode_states = iter([])

    ###########################################################################
    # Step options dependent methods
    def add_route(self, electrode_ids):
        '''
        Add droplet route.

        Args:

            electrode_ids (list) : Ordered list of identifiers of electrodes on
                route.
        '''
        drop_routes = self.get_routes()
        route_i = (drop_routes.route_i.max() + 1
                   if drop_routes.shape[0] > 0 else 0)
        drop_route = (pd.DataFrame(electrode_ids, columns=['electrode_i'])
                      .reset_index().rename(columns={'index': 'transition_i'}))
        drop_route.insert(0, 'route_i', route_i)
        drop_routes = drop_routes.append(drop_route, ignore_index=True)
        self.set_routes(drop_routes)
        return {'route_i': route_i, 'drop_routes': drop_routes}

    def clear_routes(self, electrode_id=None, step_number=None):
        '''
        Clear all drop routes for protocol step that include the specified
        electrode (identified by string identifier).
        '''
        step_options = self.get_step_options(step_number)

        if electrode_id is None:
            # No electrode identifier specified.  Clear all step routes.
            df_routes = RouteController.default_routes()
        else:
            df_routes = step_options['drop_routes']
            # Find indexes of all routes that include electrode.
            routes_to_clear = df_routes.loc[df_routes.electrode_i ==
                                            electrode_id, 'route_i']
            # Remove all routes that include electrode.
            df_routes = df_routes.loc[~df_routes.route_i
                                      .isin(routes_to_clear.tolist())].copy()
        step_options['drop_routes'] = df_routes
        self.set_step_values(step_options, step_number=step_number)

    def get_routes(self, step_number=None):
        step_options = self.get_step_options(step_number=step_number)
        return step_options.get('drop_routes',
                                RouteController.default_routes())

    def set_routes(self, df_routes, step_number=None):
        step_options = self.get_step_options(step_number=step_number)
        step_options['drop_routes'] = df_routes
        self.set_step_values(step_options, step_number=step_number)

    def reset_electrode_states_generator(self):
        '''
        Reset iterator over actuation states of electrodes in routes table.
        '''
        df_routes = self.get_routes()
        step_options = self.get_step_options()
        _L().debug('df_routes=%s\nstep_options=%s', df_routes, step_options)
        self._electrode_states = \
            electrode_states(df_routes,
                             trail_length=step_options['trail_length'],
                             repeats=step_options['route_repeats'],
                             repeat_duration_s=step_options
                             ['repeat_duration_s'])


PluginGlobals.pop_env()
