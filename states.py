from datetime import datetime
import logging

from logging_helpers import _L


def electrode_states(df_routes, trail_length=1, repeats=1,
                     repeat_duration_s=0):
    '''
    Yield consecutive electrode actuation states for the specified routes.

    Parameters
    ----------
    df_routes : pandas.DataFrame
        Table of route transitions.
    trail_length : int, optional
        Number of electrodes to turn on along route at once.
    repeats : int, optional
        Number of times to repeat **cyclic** routes.
    repeat_duration_s : float, optional
        Number of seconds to repeat **cyclic** routes.

    Yields
    ------
    pandas.Series
        Actuation states (i.e., ``True`` for **on**, ``False`` for **off**)
        of electrodes listed in :data:`df_routes`, indexed by electrode id
        (i.e., ``electrode_i``).
    '''
    if df_routes.shape[0] < 1:
        raise StopIteration

    # Find cycle routes, i.e., where first electrode matches last
    # electrode.
    route_starts = df_routes.groupby('route_i').nth(0)['electrode_i']
    route_ends = df_routes.groupby('route_i').nth(-1)['electrode_i']
    cycles = route_starts[route_starts == route_ends]
    cyclic_mask = df_routes.route_i.isin(cycles.index.tolist())

    j = 0
    start_time = datetime.now()
    while j < repeats or ((datetime.now() - start_time).total_seconds() <
                          repeat_duration_s):

        if j > 0:  # Only repeat *cyclic* routes.
            df_routes_j = df_routes.loc[cyclic_mask].copy()
            if df_routes_j.shape[0] < 1:
                raise StopIteration
        else:
            df_routes_j = df_routes

        route_groups = df_routes_j.groupby('route_i')
        # Get the number of transitions in each drop route.
        route_lengths = route_groups['route_i'].count()
        df_routes_j['route_length'] = (route_lengths[df_routes_j.route_i]
                                       .values)
        df_routes_j['cyclic'] = (df_routes_j.route_i.isin(cycles.index
                                                          .tolist()))

        start_time = datetime.now()

        for start_i in xrange(0 if j == 0 else 1, int(route_lengths.max())):
            # Trail follows transition corresponding to *transition counter* by
            # the specified *trail length*.
            end_i = (start_i + trail_length - 1)

            if _L().getEffectiveLevel() <= logging.DEBUG:
                if start_i == end_i:
                    _L().debug('%s', start_i)
                else:
                    _L().debug('%s-%s', start_i, end_i)

            start_i_mod = start_i % df_routes_j.route_length
            end_i_mod = end_i % df_routes_j.route_length

            #  1. Within the specified trail length of the current transition
            #     counter of a single pass.
            single_pass_mask = ((df_routes_j.transition_i >= start_i) &
                                (df_routes_j.transition_i <= end_i))
            #  2. Within the specified trail length of the current transition
            #     counter in the second route pass.
            second_pass_mask = (max(end_i, start_i) < 2 *
                                df_routes_j.route_length)
            #  3. Start marker is higher than end marker, i.e., end has wrapped
            #     around to the start of the route.
            wrap_around_mask = ((end_i_mod < start_i_mod) &
                                ((df_routes_j.transition_i >= start_i_mod) |
                                 (df_routes_j.transition_i <= end_i_mod + 1)))

            # Find active transitions based on the transition counter.
            active_transition_mask = (single_pass_mask |
                                      # Only consider wrap-around transitions for
                                      # the second pass of cyclic routes.
                                      (df_routes_j.cyclic & second_pass_mask &
                                       wrap_around_mask))
            # (subsequent_pass_mask | wrap_around_mask)))

            df_routes_j['active'] = active_transition_mask.astype(int)
            active_electrode_mask = (df_routes_j
                                     .groupby('electrode_i')['active'].sum())

            # An electrode may appear twice in the list of modified electrode
            # states in cases where the same channel is mapped to multiple
            # electrodes.
            #
            # Sort electrode states with "on" electrodes listed first so the "on"
            # state will take precedence when the electrode controller plugin drops
            # duplicate states for the same electrode.
            modified_electrode_states = (active_electrode_mask.astype(bool)
                                         .sort_values(ascending=False))
            yield modified_electrode_states
        j += 1
