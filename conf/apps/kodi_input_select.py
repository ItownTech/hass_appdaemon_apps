# -*- coding: utf-8 -*-
"""
Automation task as a AppDaemon App for Home Assistant

Populate dinamically an `input_select` with Kodi play options
and react when selected.

It reacts to `kodi_run_method_result` events, when the used API method is:
    - VideoLibrary.GetRecentlyAddedMovies
    - VideoLibrary.GetRecentlyAddedEpisodes
    - PVR.GetChannels
"""

import appdaemon.appapi as appapi
# from homeassistant.components.media_player.kodi import (
#     EVENT_KODI_RUN_METHOD_RESULT)

EVENT_KODI_RUN_METHOD = 'kodi_run_method_result'

ENTITY = 'input_select.kodi_results'
MEDIA_PLAYER = 'media_player.kodi'
DEFAULT_ACTION = "Nada que hacer"
MAX_RESULTS = 20


# noinspection PyClassHasNoInit
class DynamicKodiInputSelect(appapi.AppDaemon):
    """App to populate an input select with Kodi API calls results."""

    _ids_options = None

    def initialize(self):
        """Set up appdaemon app."""
        self.listen_event(self._receive_kodi_result, EVENT_KODI_RUN_METHOD)
        self.listen_state(self._change_selected_result, ENTITY)

        # Input select:
        self._ids_options = {DEFAULT_ACTION: None}

    # noinspection PyUnusedLocal
    def _receive_kodi_result(self, event_id, payload_event, *args):
        result = payload_event['result']
        method = payload_event['input']['method']

        if event_id == EVENT_KODI_RUN_METHOD:
            if method == 'VideoLibrary.GetRecentlyAddedMovies':
                # values = list(filter(lambda r: not r['lastplayed'],
                #                      result['movies']))[:MAX_RESULTS]
                values = result['movies'][:MAX_RESULTS]
                data = [('{} ({})'.format(r['label'], r['year']),
                         ('MOVIE', r['file'])) for r in values]
                self._ids_options.update(dict(zip(*zip(*data))))
                labels = list(list(zip(*data))[0])
                self.log('NEW OPTIONS:\n{}'.format(labels))
                self.call_service('input_select/set_options', entity_id=ENTITY,
                                  options=[DEFAULT_ACTION] + labels)
                self.set_state(ENTITY,
                               attributes={"friendly_name": 'Recent Movies',
                                           "icon": 'mdi:movie'})
            elif method == 'VideoLibrary.GetRecentlyAddedEpisodes':
                values = list(filter(lambda r: not r['lastplayed'],
                                     result['episodes']))[:MAX_RESULTS]
                data = [('{} - {}'.format(r['showtitle'], r['label']),
                         ('TVSHOW', r['file'])) for r in values]
                self._ids_options.update(dict(zip(*zip(*data))))
                labels = list(list(zip(*data))[0])
                self.log('NEW OPTIONS:\n{}'.format(labels))
                self.call_service('input_select/set_options', entity_id=ENTITY,
                                  options=[DEFAULT_ACTION] + labels)
                self.set_state(ENTITY,
                               attributes={"friendly_name": 'Recent TvShows',
                                           "icon": 'mdi:play-circle'})
            elif method == 'PVR.GetChannels':
                values = result['channels']
                data = [(r['label'], ('CHANNEL', r['channelid']))
                        for r in values]
                self._ids_options.update(dict(zip(*zip(*data))))
                labels = list(list(zip(*data))[0])
                self.log('NEW OPTIONS:\n{}'.format(labels))
                self.call_service('input_select/set_options', entity_id=ENTITY,
                                  options=[DEFAULT_ACTION] + labels)
                self.set_state(ENTITY,
                               attributes={"friendly_name": 'TV channels',
                                           "icon": 'mdi:play-box-outline'})

    # noinspection PyUnusedLocal
    def _change_selected_result(self, entity, attribute, old, new, kwargs):
        self.log('SELECTED OPTION: {} (from {})'.format(new, old))
        selected = self._ids_options[new]
        if selected:
            mediatype, file = selected
            self.log('PLAY MEDIA: {} {} [file={}]'.format(mediatype, new, file))
            self.call_service('media_player/play_media',
                              entity_id=MEDIA_PLAYER,
                              media_content_type=mediatype,
                              media_content_id=file)
