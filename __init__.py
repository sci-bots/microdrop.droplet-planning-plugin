"""
Copyright 2015 Christian Fobel

This file is part of droplet_planning_plugin.

droplet_planning_plugin is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

dmf_control_board is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with droplet_planning_plugin.  If not, see <http://www.gnu.org/licenses/>.
"""
from collections import OrderedDict
from datetime import datetime
import logging

from flatland import Integer, Form
from flatland.validation import ValueAtLeast
from microdrop.app_context import get_app, get_hub_uri
from microdrop.plugin_helpers import StepOptionsController, get_plugin_info
from microdrop.plugin_manager import (PluginGlobals, Plugin, IPlugin,
                                      ScheduleRequest, implements, emit_signal)
from pygtkhelpers.utils import refresh_gui
from path_helpers import path
from si_prefix import si_format
from zmq_plugin.plugin import Plugin as ZmqPlugin
from zmq_plugin.schema import decode_content_data
import gobject
import gtk
import pandas as pd
import zmq

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
        except:
            logger.error(str(data), exc_info=True)

    def on_execute__get_routes(self, request):
        return self.parent.get_routes()

    def on_execute__clear_routes(self, request):
        data = decode_content_data(request)
        try:
            return self.parent.clear_routes(electrode_id=
                                            data.get('electrode_id'))
        except:
            logger.error(str(data), exc_info=True)

    def on_execute__execute_routes(self, request):
        data = decode_content_data(request)
        try:
            df_routes = self.get_routes()
            if 'route_i' in data:
                # A route index was specified.  Only process transitions from
                # specified route.
                df_routes = df_routes.loc[df_routes.route_i == data['route_i']]
            route_controller = RouteController(self)
            return route_controller.execute_routes(df_routes,
                                                   data
                                                   ['transition_duration_ms'])
        except:
            logger.error(str(data), exc_info=True)


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
                                           'transition_i'])

    def execute_routes(self, df_routes, transition_duration_ms,
                       on_complete=None, on_error=None, trail_length=1):
        '''
        Begin execution of a set of routes.

        Args:

            df_routes (pandas.DataFrame) : Table of route transitions.
            transition_duration_ms (int) : Duration of each transition.
            on_complete (function) : Callback function called upon completed
                execution of all routes.
            on_error (function) : Callback function called upon error during
                execution of any route.
        '''
        # Stop execution (if running).
        self.reset()
        route_groups = df_routes.groupby('route_i')

        route_info = {}
        self.route_info = route_info

        route_info['df_routes'] = df_routes
        route_info['electrode_ids'] = df_routes.electrode_i.unique()
        route_info['transition_counter'] = 0
        route_info['transition_duration_ms'] = transition_duration_ms
        route_info['trail_length'] = trail_length

        # Find cycle routes, i.e., where first electrode matches last
        # electrode.
        route_starts = df_routes.groupby('route_i').nth(0)['electrode_i']
        route_ends = df_routes.groupby('route_i').nth(-1)['electrode_i']
        route_info['cycles'] = route_starts[route_starts == route_ends]

        # Look up the drop routes for the current.
        route_info['routes'] = OrderedDict([(route_j, df_route_j)
                                            for route_j, df_route_j in
                                            route_groups])
        # Get the number of transitions in each drop route.
        route_info['route_lengths'] = route_groups['route_i'].count()
        route_info['start_time'] = datetime.now()

        def _first_pass():
            # Execute first route transition immediately.
            self.check_routes_progress(on_complete, on_error, False)
        gobject.idle_add(_first_pass)

    def check_routes_progress(self, on_complete, on_error, continue_=True):
        '''
        Callback called by periodic timeout at intervals of
        `transition_duration_ms` until all routes are completed.
        '''
        route_info = self.route_info
        try:
            stop_i = route_info['route_lengths'].max()
            logger.debug('[check_routes_progress] stop_i: %s', stop_i)
            if (route_info['transition_counter'] < stop_i):
                # There is at least one route with remaining transitions to
                # execute.
                self.execute_transition()
                route_info['transition_counter'] += 1

                # Execute remaining route transitions periodically, at the
                # specified interval duration.
                route_info['timeout_id'] =\
                    gobject.timeout_add(route_info['transition_duration_ms'],
                                        self.check_routes_progress,
                                        on_complete, on_error)
            else:
                # All route transitions have executed.
                self.reset()
                if on_complete is not None:
                    on_complete(route_info['start_time'],
                                route_info['electrode_ids'])
        except:
            # An error occurred while executing routes.
            if on_error is not None: on_error()
        return False

    def execute_transition(self):
        '''
        Execute a single transition (corresponding to the current transition
        index) in each route with a sufficient number of transitions.
        '''
        route_info = self.route_info

        stop_i = route_info['route_lengths'] + route_info['trail_length'] - 1
        active_route_lengths = (route_info['route_lengths']
                                .loc[route_info['transition_counter'] <
                                     stop_i])

        electrode_states = pd.Series(-1, index=route_info['electrode_ids'],
                                     dtype=int)

        for route_i, length_i in active_route_lengths.iteritems():
            # Trail follows transition corresponding to *transition counter* by
            # the specified *trail length*.
            start_i = route_info['transition_counter']
            end_i = (route_info['transition_counter'] +
                     route_info['trail_length'] - 1)

            if start_i == end_i:
                logger.debug('[execute_transition] route %d: %s', route_i,
                             start_i)
            else:
                logger.debug('[execute_transition] route %d: %s-%s', route_i,
                             start_i, end_i)

            # Deactivate electrodes from previous route transition.
            if start_i > 0 and start_i < length_i:
                s_transition_i = (route_info['routes'][route_i]
                                  .iloc[start_i - 1])
                electrode_states[s_transition_i.electrode_i] = 0

            # Activate electrodes for current route transition.
            s_transition_i = (route_info['routes'][route_i]
                              .iloc[start_i:end_i + 1])
            electrode_states[s_transition_i.electrode_i] = 1

            if route_i in route_info['cycles'] and end_i + 1 > length_i:
                s_transition_i = (route_info['routes'][route_i]
                                  .iloc[:end_i - length_i + 2])
                electrode_states[s_transition_i.electrode_i] = 1

        modified_electrode_states = electrode_states[electrode_states >= 0]
        self.plugin.execute('wheelerlab.electrode_controller_plugin',
                            'set_electrode_states',
                            electrode_states=modified_electrode_states,
                            save=False, wait_func=lambda *args: refresh_gui())

    def reset(self):
        '''
        Reset execution state.
        '''
        if 'timeout_id' in self.route_info:
            gobject.source_remove(self.route_info['timeout_id'])
            del self.route_info['timeout_id']

        if ('electrode_ids' in self.route_info and
            (self.route_info['electrode_ids'].shape[0] > 0)):
            # At least one route exists.
            # Deactivate all electrodes belonging to all routes.
            electrode_states = pd.Series(0, index=self.route_info
                                         ['electrode_ids'], dtype=int)
            self.plugin.execute('wheelerlab'
                                '.electrode_controller_plugin',
                                'set_electrode_states',
                                electrode_states=electrode_states,
                                save=False, wait_func=lambda *args:
                                refresh_gui())
        self.route_info = {'transition_counter': 0}


class DropletPlanningPlugin(Plugin, StepOptionsController):
    """
    This class is automatically registered with the PluginManager.
    """
    implements(IPlugin)
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
        Integer.named('trail_length').using(default=1, optional=True,
                                            validators=
                                            [ValueAtLeast(minimum=1)]),
        Integer.named('route_repeats').using(default=1, optional=True,
                                            validators=
                                            [ValueAtLeast(minimum=1)]),
        Integer.named('repeat_duration_s').using(default=0, optional=True),
        Integer.named('transition_duration_ms')
        .using(optional=True, default=750,
               validators=[ValueAtLeast(minimum=0)]))

    def __init__(self):
        self.name = self.plugin_name
        self.plugin = None
        self.plugin_timeout_id = None
        self.step_start_time = None
        self.route_controller = None

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest instances)
        for the function specified by function_name.
        """
        if function_name in ['on_step_run']:
            # Execute `on_step_run` before control board.
            return [ScheduleRequest(self.name,
                                    'wheelerlab.dmf_control_board_plugin')]
        return []

    def on_plugin_enable(self):
        self.cleanup()
        self.plugin = RouteControllerZmqPlugin(self, self.name, get_hub_uri())
        self.route_controller = RouteController(self.plugin)
        # Initialize sockets.
        self.plugin.reset()

        self.plugin_timeout_id = gobject.timeout_add(10,
                                                     self.plugin.check_sockets)

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
        if self.plugin_timeout_id is not None:
            gobject.source_remove(self.plugin_timeout_id)
        if self.plugin is not None:
            self.plugin = None

    ###########################################################################
    # Step event handler methods
    def on_error(self, *args):
        logger.error('Error executing routes.', exc_info=True)
        # An error occurred while initializing Analyst remote control.
        emit_signal('on_step_complete', [self.name, 'Fail'])

    def on_step_run(self):
        """
        Handler called whenever a step is executed. Note that this signal
        is only emitted in realtime mode or if a protocol is running.

        Plugins that handle this signal must emit the on_step_complete
        signal once they have completed the step. The protocol controller
        will wait until all plugins have completed the current step before
        proceeding.

        return_value can be one of:
            None
            'Repeat' - repeat the step
            or 'Fail' - unrecoverable error (stop the protocol)
        """
        step_options = self.get_step_options()

        try:
            self.repeat_i = 0
            self.step_start_time = datetime.now()
            df_routes = self.get_routes()
            self.route_controller.execute_routes(
                df_routes, step_options['transition_duration_ms'],
                trail_length=step_options['trail_length'],
                on_complete=self.on_step_routes_complete,
                on_error=self.on_error)
        except:
            self.on_error()

    def on_step_routes_complete(self, start_time, electrode_ids):
        step_options = self.get_step_options()
        step_duration_s = (datetime.now() -
                           self.step_start_time).total_seconds()
        if ((step_options['repeat_duration_s'] > 0 and step_duration_s <
             step_options['repeat_duration_s']) or
            (self.repeat_i + 1 < step_options['route_repeats'])):
            # Either repeat duration has not been met, or the specified number
            # of repetitions has not been met.  Execute another iteration of
            # the routes.
            self.repeat_i += 1
            df_routes = self.get_routes()
            self.route_controller.execute_routes(
                df_routes, step_options['transition_duration_ms'],
                trail_length=step_options['trail_length'],
                on_complete=self.on_step_routes_complete,
                on_error=self.on_error)
        else:
            logger.info('Completed routes (%s repeats in %ss)', self.repeat_i +
                        1, si_format(step_duration_s))
            # Transitions along all droplet routes have been processed.
            # Signal step has completed and reset plugin step state.
            emit_signal('on_step_complete', [self.name, None])

    def on_step_options_swapped(self, plugin, old_step_number, step_number):
        """
        Handler called when the step options are changed for a particular
        plugin.  This will, for example, allow for GUI elements to be
        updated based on step specified.

        Parameters:
            plugin : plugin instance for which the step options changed
            step_number : step number that the options changed for
        """
        pass

    def on_step_swapped(self, old_step_number, step_number):
        """
        Handler called when the current step is swapped.
        """
        if self.plugin is not None:
            self.plugin.execute_async(self.name, 'get_routes')

    def on_step_inserted(self, step_number, *args):
        app = get_app()
        logger.info('[on_step_inserted] current step=%s, created step=%s',
                    app.protocol.current_step_number, step_number)
        self.clear_routes(step_number=step_number)

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


PluginGlobals.pop_env()
