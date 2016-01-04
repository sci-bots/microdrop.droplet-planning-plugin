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
import sys, traceback

from flatland import Integer, Form, String
from microdrop.app_context import get_app
from microdrop.logger import logger
from microdrop.plugin_helpers import (AppDataController, StepOptionsController,
                                      get_plugin_info)
from microdrop.plugin_manager import (PluginGlobals, Plugin, IPlugin,
                                      ScheduleRequest, implements, emit_signal)
from path_helpers import path
from zmq_plugin.plugin import Plugin as ZmqPlugin
from zmq_plugin.schema import decode_content_data
import gobject
import gtk
import pandas as pd
import zmq

logger = logging.getLogger(__name__)

PluginGlobals.push_env('microdrop.managed')

def gtk_wait(wait_duration_s): gtk.main_iteration_do()

class RouteControllerZmqPlugin(ZmqPlugin):
    '''
    API for adding/clearing droplet routes.
    '''
    def __init__(self, parent, *args, **kwargs):
        self.parent = parent
        super(RouteControllerZmqPlugin, self).__init__(*args, **kwargs)

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


class DropletPlanningPlugin(Plugin, AppDataController, StepOptionsController):
    """
    This class is automatically registered with the PluginManager.
    """
    implements(IPlugin)
    version = get_plugin_info(path(__file__).parent).version
    plugin_name = get_plugin_info(path(__file__).parent).plugin_name

    '''
    AppFields
    ---------

    A flatland Form specifying application options for the current plugin.
    Note that nested Form objects are not supported.

    Since we subclassed AppDataController, an API is available to access and
    modify these attributes.  This API also provides some nice features
    automatically:
        -all fields listed here will be included in the app options dialog
            (unless properties=dict(show_in_gui=False) is used)
        -the values of these fields will be stored persistently in the microdrop
            config file, in a section named after this plugin's name attribute
    '''
    AppFields = Form.of(
        String.named('hub_uri').using(optional=True,
                                      default='tcp://localhost:31000'),
        Integer.named('transition_duration_ms').using(optional=True,
                                                      default=750),
    )

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
        Integer.named('min_duration').using(default=0, optional=True),
    )

    def __init__(self):
        self.name = self.plugin_name
        self.timeout_id = None
        self.start_time = None
        self.transition_counter = 0
        self.plugin = None
        self.plugin_timeout_id = None

    def default_drop_routes(self):
        return pd.DataFrame(None, columns=['route_i', 'electrode_i',
                                           'transition_i'])

    def get_routes(self, step_number=None):
        step_options = self.get_step_options(step_number=step_number)
        return step_options.get('drop_routes', self.default_drop_routes())

    def set_routes(self, df_drop_routes, step_number=None):
        step_options = self.get_step_options(step_number=step_number)
        step_options['drop_routes'] = df_drop_routes
        self.set_step_values(step_options, step_number=step_number)

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
        app = get_app()
        logger.info('[DropletPlanningPlugin] on_step_run(): step #%d',
                    app.protocol.current_step_number)
        app_values = self.get_app_values()
        try:
            if self.timeout_id is not None:
                # Timer was already set, so cancel previous timer.
                gobject.source_remove(self.timeout_id)

            drop_route_groups = self.get_routes().groupby('route_i')
            # Look up the drop routes for the current step.
            self.step_drop_routes = OrderedDict([(route_i, df_route_i)
                                                 for route_i, df_route_i in
                                                 drop_route_groups])
            # Get the number of transitions in each drop route.
            self.step_drop_route_lengths = drop_route_groups['route_i'].count()
            self.transition_counter = 0
            self.start_time = datetime.now()
            gobject.idle_add(self.on_timer_tick, False)
            self.timeout_id = gobject.timeout_add(app_values
                                                  ['transition_duration_ms'],
                                                  self.on_timer_tick)
        except:
            print "Exception in user code:"
            print '-'*60
            traceback.print_exc(file=sys.stdout)
            print '-'*60
            # An error occurred while initializing Analyst remote control.
            emit_signal('on_step_complete', [self.name, 'Fail'])

    def on_timer_tick(self, continue_=True):
        app = get_app()
        try:
            electrode_ids = self.get_routes().electrode_i.unique()

            if self.transition_counter < self.step_drop_route_lengths.max():
                active_step_lengths = (self.step_drop_route_lengths
                                       .loc[self.step_drop_route_lengths >
                                            self.transition_counter])

                electrode_states = pd.Series(-1, index=electrode_ids,
                                             dtype=int)
                for route_i, length_i in active_step_lengths.iteritems():
                    # Remove custom coloring for previously active electrode.
                    if self.transition_counter > 0:
                        transition_i = (self.step_drop_routes[route_i]
                                        .iloc[self.transition_counter - 1])
                        electrode_states[transition_i.electrode_i] = 0
                    # Add custom coloring to active electrode.
                    transition_i = (self.step_drop_routes[route_i]
                                    .iloc[self.transition_counter])
                    electrode_states[transition_i.electrode_i] = 1
                modified_electrode_states = (electrode_states
                                             [electrode_states >= 0])
                self.plugin.execute('wheelerlab'
                                    '.electrode_controller_plugin',
                                    'set_electrode_states',
                                    electrode_states=modified_electrode_states,
                                    wait_func=gtk_wait)
                self.transition_counter += 1
            else:
                command_status = {}
                if electrode_ids.shape[0] > 0:
                    # At least one drop route exists for current step.
                    # Deactivate all electrodes on any droplet route from
                    # current step.
                    electrode_states = pd.Series(0, index=electrode_ids,
                                                 dtype=int)
                    self.plugin.execute('wheelerlab'
                                        '.electrode_controller_plugin',
                                        'set_electrode_states',
                                        electrode_states=electrode_states,
                                        wait_func=gtk_wait)

                if self.timeout_id is not None:
                    gobject.source_remove(self.timeout_id)
                    self.timeout_id = None
                self.start_time = None
                self.transition_counter = 0

                logger.info('[DropletPlanningPlugin] on_timer_tick(): step %d',
                            app.protocol.current_step_number)
                # Transitions along all droplet routes have been processed.
                # Signal step has completed and reset plugin step state.
                emit_signal('on_step_complete', [self.name, None])
                return False
        except:
            print "Exception in user code:"
            print '-'*60
            traceback.print_exc(file=sys.stdout)
            print '-'*60
            emit_signal('on_step_complete', [self.name, 'Fail'])
            self.timeout_id = None
            self.remote = None
            return False
        return continue_

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

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest instances)
        for the function specified by function_name.
        """
        if function_name in ['on_step_run']:
            # Execute `on_step_run` before control board.
            return [ScheduleRequest(self.name,
                                    'wheelerlab.dmf_control_board_plugin')]
        elif function_name == 'on_plugin_enable':
            return [ScheduleRequest('wheelerlab.zmq_hub_plugin', self.name)]
        return []

    def on_plugin_enable(self):
        """
        Handler called once the plugin instance is enabled.

        Note: if you inherit your plugin from AppDataController and don't
        implement this handler, by default, it will automatically load all
        app options from the config file. If you decide to overide the
        default handler, you should call:

            AppDataController.on_plugin_enable(self)

        to retain this functionality.
        """
        super(DropletPlanningPlugin, self).on_plugin_enable()
        app_values = self.get_app_values()

        self.cleanup()
        self.plugin = RouteControllerZmqPlugin(self, self.name,
                                               app_values['hub_uri'])
        # Initialize sockets.
        self.plugin.reset()

        def check_command_socket():
            try:
                msg_frames = (self.plugin.command_socket
                              .recv_multipart(zmq.NOBLOCK))
            except zmq.Again:
                pass
            else:
                self.plugin.on_command_recv(msg_frames)
            return True

        self.plugin_timeout_id = gobject.timeout_add(10, check_command_socket)

    def cleanup(self):
        if self.plugin_timeout_id is not None:
            gobject.source_remove(self.plugin_timeout_id)
        if self.plugin is not None:
            self.plugin = None

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

    def on_step_inserted(self, step_number, *args):
        app = get_app()
        logger.info('[on_step_inserted] current step=%s, created step=%s',
                    app.protocol.current_step_number, step_number)
        self.clear_routes(step_number=step_number)

    def clear_routes(self, electrode_id=None, step_number=None):
        '''
        Clear all drop routes for protocol step that include the specified
        electrode (identified by string identifier).
        '''
        app = get_app()
        step_options = self.get_step_options(step_number)

        if electrode_id is None:
            # No electrode identifier specified.  Clear all step routes.
            df_drop_routes = self.default_drop_routes()
        else:
            df_drop_routes = step_options['drop_routes']
            # Find indexes of all routes that include electrode.
            routes_to_clear = drop_routes.loc[df_drop_routes.electrode_i ==
                                              electrode_id, 'route_i']
            # Remove all routes that include electrode.
            df_drop_routes = df_drop_routes.loc[~df_drop_routes.route_i
                                                .isin(routes_to_clear
                                                      .tolist())].copy()
        step_options['drop_routes'] = df_drop_routes
        self.set_step_values(step_options, step_number=step_number)

PluginGlobals.pop_env()
