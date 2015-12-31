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
import sys, traceback
from datetime import datetime
from collections import OrderedDict

import numpy as np
import pandas as pd
from path_helpers import path
from flatland import Integer, Boolean, Form, String
from flatland.validation import ValueAtLeast, ValueAtMost
from microdrop.logger import logger
from microdrop.plugin_helpers import (AppDataController, StepOptionsController,
                                      get_plugin_info)
from microdrop.plugin_manager import (PluginGlobals, Plugin, IPlugin,
                                      IWaveformGenerator, ScheduleRequest,
                                      implements, emit_signal,
                                      get_service_instance_by_name)
from microdrop.app_context import get_app
import gobject
import gtk

PluginGlobals.push_env('microdrop.managed')


class ElectrodeController(object):
    '''
    API for turning electrode(s) on/off.

    Must handle:
     - Updating state of hardware channels (if connected).
     - Updating device user interface.
    '''
    def __init__(self):
        self.control_board = None

    def get_control_board(self):
        if self.control_board is None:
            try:
                plugin = get_service_instance_by_name('wheelerlab'
                                                      '.dmf_control_board')
            except:
                logging.warning('Could not get connection to control board.')
                return None
            else:
                self.control_board = plugin.control_board
        return self.control_board

    def set_electrode_state(self, electrode_id, state):
        '''
        Set the state of a single electrode.

        Args:

            electrode_id (str) : Electrode identifier (e.g., `"electrode001"`)
            state (int) : State of electrode
        '''
        self.set_electrode_states(pd.Series([state], index=[electrode_index]))

    def set_electrode_states(self, electrode_states):
        '''
        Set the state of multiple electrodes.

        Args:

            electrode_states (pandas.Series) : State of electrodes, indexed by
                electrode identifier (e.g., `"electrode001"`).
        '''
        colors = (electrode_states.repeat(3).reshape(-1, 3) *
                  np.array([255., 255., 255.]))
        colors[colors == 0] = None

        app = get_app()
        device_view = app.dmf_device_controller.view

        # Update color of electrodes according to state.
        for electrode_id, color_i in zip(electrode_states.index, colors):
            color_i = color_i if not np.isnan(color_i[0]) else None
            device_view.set_electrode_color(electrode_id, rgb_color=color_i)

        if self.get_control_board() is not None and (self.control_board
                                                     .connected()):
            # Set the state of DMF control board channels.
            step = app.protocol.get_step()
            dmf_options = step.get_data('microdrop.gui.dmf_device_controller')
            options = step.get_data('wheelerlab.dmf_control_board')

            channel_states = dmf_options.state_of_channels
            electrode_channels = (app.dmf_device
                                  .actuated_channels(electrode_states.index))
            channel_states[electrode_channels.values
                           .tolist()] = electrode_states.values

            try:
                emit_signal("set_voltage", options.voltage,
                            interface=IWaveformGenerator)
                emit_signal("set_frequency", options.frequency,
                            interface=IWaveformGenerator)
                self.control_board.set_state_of_all_channels(channel_states)
            except:
                self.control_board = None
                logger.error('[ElectrodeController]', exc_info=True)


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
        self.electrode_controller = ElectrodeController()

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
        device_step_options = app.dmf_device_controller.get_step_options()
        try:
            if self.timeout_id is not None:
                # Timer was already set, so cancel previous timer.
                gobject.source_remove(self.timeout_id)

            drop_route_groups = (device_step_options.drop_routes
                                 .groupby('route_i'))
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
            device_step_options = (app.dmf_device_controller
                                    .get_step_options())
            electrode_indexes = (device_step_options.drop_routes.electrode_i
                                 .unique())
            electrode_ids = app.dmf_device.indexed_shapes.ix[electrode_indexes]

            if self.transition_counter < self.step_drop_route_lengths.max():
                active_step_lengths = (self.step_drop_route_lengths
                                       .loc[self.step_drop_route_lengths >
                                            self.transition_counter])

                electrode_states = pd.Series(-1, index=electrode_indexes,
                                             dtype=int)
                electrode_states_by_id = pd.Series(electrode_states.values,
                                                   index=electrode_ids)
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
                modified_electrode_states = (electrode_states_by_id
                                             [electrode_states_by_id >= 0])
                (self.electrode_controller
                 .set_electrode_states(modified_electrode_states))
                gtk.idle_add(app.dmf_device_controller.view.update_draw_queue)
                self.transition_counter += 1
            else:
                if electrode_ids.shape[0] > 0:
                    # At least one drop route exists for current step.
                    # Deactivate all electrodes on any droplet route from
                    # current step.
                    electrode_states_by_id = pd.Series(0, index=electrode_ids,
                                                       dtype=int)
                    (self.electrode_controller
                     .set_electrode_states(electrode_states_by_id))
                    gtk.idle_add(app.dmf_device_controller.view
                                 .update_draw_queue)

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

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest instances)
        for the function specified by function_name.
        """
        if function_name in ['on_step_run']:
            # Execute `on_step_run` before control board.
            return [ScheduleRequest(self.name, 'wheelerlab.dmf_control_board')]
        return []


PluginGlobals.pop_env()
