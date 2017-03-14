# -*- coding: utf-8 -*-
"""
Automation task as a AppDaemon App for Home Assistant

This little app is a not too simple alarm clock, which simulates a fast dawn with Hue lights,
while waking up the home cinema system, waiting for the start of the broadcast of La Cafetera radio program to
start playing it (or, if the alarm is at a different time of the typical emmision time,
it just play the last published episode).

For doing that, it talks directly with Kodi (or Mopidy, without any add-on) through its JSONRPC API,
which has to run a specific Kodi Add-On: plugin.audio.lacafetera.

"""
import appdaemon.appapi as appapi
import appdaemon.conf as conf
import datetime as dt
from dateutil.parser import parse
from functools import reduce
import json
import pytz
import requests


# Defaults para La Cafetera Alarm Clock:
MAX_VOLUME_MOPIDY = 40
DURATION_VOLUME_RAMP_SEC = 120
DEFAULT_DURATION = 1.2  # h
DEFAULT_EMISION_TIME = "08:30:00"
MAX_WAIT_TIME = dt.timedelta(minutes=10)
STEP_RETRYING_SEC = 20
WARM_UP_TIME_DELTA = dt.timedelta(seconds=25)
MIN_INTERVAL_BETWEEN_EPS = dt.timedelta(hours=8)
MASK_URL_STREAM_MOPIDY = "http://api.spreaker.com/listen/episode/{}/http"

LOG_LEVEL = 'INFO'
WEEKDAYS_DICT = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}


def get_info_last_ep(tz, limit=1):
    """Extrae la información del último (o 'n-último') episodio disponible de La Cafetera de Radiocable.com"""
    base_url_v2 = 'https://api.spreaker.com/v2/'
    cafetera_showid = 1060718
    mask_url = base_url_v2 + 'shows/' + str(cafetera_showid) + '/episodes?limit=' + str(limit)
    r = requests.get(mask_url)
    if r.ok:
        data = r.json()
        if ('response' in data) and ('items' in data['response']):
            episode = data['response']['items'][-1]
            published = parse(episode['published_at']).replace(tzinfo=pytz.UTC).astimezone(tz).replace(tzinfo=None)
            is_live = episode['type'] == 'LIVE'
            if is_live:
                duration = dt.timedelta(hours=DEFAULT_DURATION)
            else:
                duration = dt.timedelta(seconds=episode['duration'] / 1000)
            return True, {'published': published, 'is_live': is_live, 'duration': duration, 'episode': episode}
        return False, data
    return False, None


def is_last_episode_ready_for_play(now, tz):
    """Comprueba si hay un nuevo episodio disponible de La Cafetera.

    :param now: appdaemon datetime.now()
    :param tz: timezone, para corregir las fechas en UTC a local
    :return: (play_now, info_last_episode)
    :rtype: tuple(bool, dict)
    """
    est_today = dt.datetime.combine(now.date(), parse(DEFAULT_EMISION_TIME).time())  # .replace(tzinfo=tz)
    ok, info = get_info_last_ep(tz)
    if ok:
        if (info['is_live'] or (now - info['published'] < MIN_INTERVAL_BETWEEN_EPS) or
                (now + MAX_WAIT_TIME < est_today) or (now - MAX_WAIT_TIME > est_today)):
            # Reproducir YA
            return True, info
        else:
            # Esperar un poco más a que empiece
            return False, info
    # Network error?
    return False, None


def make_notification_episode(ep_info):
    """Crea los datos para la notificación de alarma, con información del episodio de La Cafetera a reproducir."""
    message = ("La Cafetera [{}]: {}\n(Publicado: {})"
               .format(ep_info['episode']['title'], 'LIVE' if ep_info['is_live'] else 'RECORDED', ep_info['published']))
    img_url = ep_info['episode']['image_url']
    data_msg = {"title": "Comienza el día en positivo!",
                "message": message,
                "data": {"push": {"badge": 0, "sound": "US-EN-Morgan-Freeman-Good-Morning.wav",
                                  "category": "ALARMCLOCK"},
                         "attachment": {"url": img_url}}}
    return data_msg


def _weekday(str_wday):
    str_wday = str_wday.lower().rstrip().lstrip()
    if str_wday in WEEKDAYS_DICT:
        return WEEKDAYS_DICT[str_wday]
    print('Error parsing weekday: "{}" -> mon,tue,wed,thu,fri,sat,sun'.format(str_wday))
    return -1


# noinspection PyClassHasNoInit
class AlarmClock(appapi.AppDaemon):
    """App for run a some-complex morning alarm,
    with sunrise light simulation and launch of a playing task to KODI within a Kodi add-on,
    after waking up the home-cinema system, or to a Modipy instance running in another RPI"""

    _alarm_time_sensor = None
    _delta_time_postponer_sec = None
    _weekdays_alarm = None
    _notifier = None
    _transit_time = None
    _phases_sunrise = []
    _tz = None
    _lights_alarm = None

    _room_select = None
    _manual_trigger = None
    _selected_player = None

    _media_player_kodi = None
    _kodi_ip = None
    _kodi_port = None
    _kodi_user = None
    _kodi_pass = None

    _media_player_mopidy = None
    _mopidy_ip = None
    _mopidy_port = None

    _next_alarm = None
    _handle_alarm = None
    _last_trigger = None
    _in_alarm_mode = False

    def initialize(self):
        """AppDaemon required method for app init."""
        conf_data = dict(self.config['AppDaemon'])
        self._tz = conf.tz
        self._alarm_time_sensor = self.args.get('alarm_time')
        self._delta_time_postponer_sec = int(self.args.get('postponer_minutos', 9)) * 60
        self.listen_state(self.alarm_time_change, self._alarm_time_sensor)
        self._weekdays_alarm = [_weekday(d) for d in self.args.get('alarmdays', 'mon,tue,wed,thu,fri').split(',')
                                if _weekday(d) >= 0]
        # Room selection:
        self._selected_player = 'KODI'
        self._room_select = self.args.get('room_select', None)
        if self._room_select is not None:
            # self._room_select = self.args.get('room_select', None)
            self._selected_player = self.get_state(entity_id=self._room_select)
            self.log('selected_player: {}'.format(self._selected_player))
            self.listen_state(self.change_player, self._room_select)

        self._media_player_kodi = conf_data.get('media_player')
        self._kodi_ip = conf_data.get('kodi_ip', '127.0.0.1')
        self._kodi_port = conf_data.get('kodi_port', 8080)
        self._kodi_user = conf_data.get('kodi_user', None)
        self._kodi_pass = conf_data.get('kodi_pass', None)

        self._media_player_mopidy = conf_data.get('media_player_mopidy')
        self._mopidy_ip = '192.168.1.51'
        self._mopidy_port = 6680

        # Trigger for last episode and boolean for play status
        self._manual_trigger = self.args.get('manual_trigger', None)
        if self._manual_trigger is not None:
            self.listen_state(self.manual_triggering, self._manual_trigger)

        # Listen to ios notification actions:
        self.listen_event(self.postpone_secuencia_despertador, 'postponer_despertador')

        self._lights_alarm = self.args.get('lights_alarm', None)
        self._notifier = conf_data.get('notifier', None)
        total_duration = int(self.args.get('sunrise_duration', 60))
        if not self._phases_sunrise:
            self._phases_sunrise.append({'brightness': 4, 'xy_color': [0.6051, 0.282], 'rgb_color': (62, 16, 17)})
            self._phases_sunrise.append({'brightness': 30, 'xy_color': [0.672, 0.327], 'rgb_color': (183, 66, 0)})
            self._phases_sunrise.append({'brightness': 60, 'xy_color': [0.629, 0.353], 'rgb_color': (224, 105, 19)})
            self._phases_sunrise.append({'brightness': 147, 'xy_color': [0.533, 0.421], 'rgb_color': (255, 175, 53)})
            self._phases_sunrise.append({'brightness': 196, 'xy_color': [0.4872, 0.4201], 'rgb_color': (255, 191, 92)})
            self._phases_sunrise.append({'brightness': 222, 'xy_color': [0.4587, 0.4103], 'rgb_color': (255, 199, 117)})
            self._phases_sunrise.append({'brightness': 254, 'xy_color': [0.449, 0.4078], 'rgb_color': (255, 203, 124)})
        self._transit_time = total_duration // len(self._phases_sunrise) + 1

        self._set_new_alarm_time()
        self.log('INIT WITH NEXT ALARM IN: {:%d-%m-%Y %H:%M:%S}'.format(self._next_alarm), LOG_LEVEL)

    @property
    def play_in_kodi(self):
        """Boolean for select each player (Kodi / Mopidy)."""
        return 'KODI' in self._selected_player.upper()

    # noinspection PyUnusedLocal
    def change_player(self, entity, attribute, old, new, kwargs):
        """Change player."""
        self.log('CHANGE PLAYER from {} to {}'.format(self._selected_player, new))
        self._selected_player = new

    # noinspection PyUnusedLocal
    def turn_off_alarm_clock(self, *args):
        """Stop current play when turning off the input_boolean."""
        if self._in_alarm_mode:
            if self.play_in_kodi and (self.get_state(entity_id=self._media_player_kodi) == 'playing'):
                self.call_service('media_player/media_stop', entity_id=self._media_player_kodi)
                self.call_service('switch/turn_off', entity_id='switch.kodi_tv_salon')
                if self._manual_trigger is not None:
                    self._last_trigger = None
                    self.set_state(entity_id=self._manual_trigger, state='off')
                self.log('TURN_OFF KODI')
            elif not self.play_in_kodi:
                if self.get_state(entity_id=self._media_player_mopidy) == 'playing':
                    # self.call_service('media_player/media_stop', entity_id=self._media_player_mopidy)  NotImplemented!
                    self.call_service('media_player/turn_off', entity_id=self._media_player_mopidy)
                self.call_service('switch/turn_off', entity_id="switch.altavoz")
                if self._manual_trigger is not None:
                    self._last_trigger = None
                    self.set_state(entity_id=self._manual_trigger, state='off')
                self.log('TURN_OFF MOPIDY')
        else:
            self.log('TURN_OFF ALARM CLOCK, BUT ALREADY OFF?')
        self._in_alarm_mode = False
        # else:
        #     self.log('WTF? TURN_OFF: kodi={}, id_k={}, id_m={}, st_m={}'
        #              .format(self.play_in_kodi, self._media_player_kodi, self._media_player_mopidy,
        #                      self.get_state(entity_id=self._media_player_mopidy)))

    # noinspection PyUnusedLocal
    def manual_triggering(self, entity, attribute, old, new, kwargs):
        """Start reproduction manually."""
        self.log('MANUAL_TRIGGERING BOOLEAN CHANGED from {} to {}'.format(old, new))
        # Manual triggering
        if (new == 'on') and ((self._last_trigger is None)
                              or ((dt.datetime.now() - self._last_trigger).total_seconds() > 30)):
            _ready, ep_info = is_last_episode_ready_for_play(self.datetime(), self._tz)
            self.log('TRIGGER_START with ep_ready, ep_info --> {}, {}'.format(_ready, ep_info))
            if self.play_in_kodi:
                ok = self.run_kodi_addon_lacafetera()
            else:
                ok = self.run_mopidy_stream_lacafetera(ep_info)
            # Notification:
            self.call_service(self._notifier.replace('.', '/'), **make_notification_episode(ep_info))
        # Manual stop after at least 15 sec
        elif ((new == 'off') and (old == 'on') and (self._last_trigger is not None) and
                ((dt.datetime.now() - self._last_trigger).total_seconds() > 10)):
            # Stop if it's playing
            self.log('TRIGGER_STOP (last trigger at {})'.format(self._last_trigger))
            self.turn_off_alarm_clock()

    # noinspection PyUnusedLocal
    def alarm_time_change(self, entity, attribute, old, new, kwargs):
        """Re-schedule next alarm when alarm time sliders change."""
        self._set_new_alarm_time()
        self.log('CHANGING ALARM TIME TO: {:%H:%M:%S}'.format(self._next_alarm), LOG_LEVEL)

    # noinspection PyUnusedLocal
    def _set_new_alarm_time(self, *args):
        if self._handle_alarm is not None:
            self.log('Cancelling timer "{}" -> {}'.format(self._handle_alarm, self._next_alarm), LOG_LEVEL)
            self.cancel_timer(self._handle_alarm)
        time_alarm = reduce(lambda x, y: x.replace(**{y[1]: int(y[0])}),
                            zip(self.get_state(entity_id=self._alarm_time_sensor).split(':'),
                                ['hour', 'minute', 'second']),
                            self.datetime().replace(second=0, microsecond=0))
        self._next_alarm = time_alarm - WARM_UP_TIME_DELTA
        self._handle_alarm = self.run_daily(self.run_alarm, self._next_alarm.time())
        self.log('Creating timer for {} --> {}'.format(self._next_alarm, self._handle_alarm), LOG_LEVEL)

    def _set_sunrise_phase(self, *args_runin):
        self.log('SET_SUNRISE_PHASE: XY={xy_color}, BRIGHT={brightness}, TRANSITION={transition}'
                 .format(**args_runin[0]))
        if self._in_alarm_mode:
            self.call_service('light/turn_on', **args_runin[0])
        else:
            self.log('ABORTED SET SUNRISE PHASE')

    # noinspection PyUnusedLocal
    def turn_on_lights_as_sunrise(self, *args):
        """Turn on the lights with a sunrise simulation done with multiple transitions."""
        self.log('RUN_SUNRISE')
        self.call_service('light/turn_off', entity_id=self._lights_alarm, transition=0)
        self.call_service('light/turn_on', entity_id=self._lights_alarm, transition=1,
                          xy_color=self._phases_sunrise[0]['xy_color'], brightness=1)
        run_in = 2
        for phase in self._phases_sunrise:
            # noinspection PyTypeChecker
            xy_color, brightness = phase['xy_color'], phase['brightness']
            self.run_in(self._set_sunrise_phase, run_in, entity_id=self._lights_alarm,
                        xy_color=xy_color, transition=self._transit_time, brightness=brightness)
            run_in += self._transit_time + 1

    def run_kodi_addon_lacafetera(self, mode="playlast"):
        """Run Kodi add-on with parameters vith JSONRPC API."""
        self.log('RUN_KODI_ADDON_LACAFETERA with mode={}'.format(mode), LOG_LEVEL)
        url_base = 'http://{}:{}/'.format(self._kodi_ip, self._kodi_port)
        auth = (self._kodi_user, self._kodi_pass) if self._kodi_user is not None else None
        data = {"request": json.dumps({"id": 1, "jsonrpc": "2.0", "method": "Addons.ExecuteAddon",
                                       "params": {"params": {"mode": mode},
                                                  "addonid": "plugin.audio.lacafetera"}})}
        r = requests.get(url_base + 'jsonrpc', params=data, auth=auth,
                         headers={'Content-Type': 'application/json'}, timeout=5)
        if r.ok:
            self._in_alarm_mode = True
            self._last_trigger = dt.datetime.now()
            return True
        self.log('KODI NOT PRESENT? -> {}'.format(r.content), 'ERROR')
        return False

    def run_command_mopidy(self, command='core.tracklist.get_tl_tracks', params=None):
        """Play stream in mopidy."""
        url_base = 'http://{}:{}/mopidy/rpc'.format(self._mopidy_ip, self._mopidy_port)
        headers = {'Content-Type': 'application/json'}
        payload = {"method": command, "jsonrpc": "2.0", "id": 1}
        if params is not None:
            payload.update(params=params)
        r = requests.post(url_base, headers=headers, data=json.dumps(payload))
        self.log('DEBUG MOPIDY {} COMMAND RESPONSE? -> {}'.format(command.upper(), r.content.decode()), 'DEBUG')
        if r.ok:
            res = json.loads(r.content.decode())
            if not res['result']:
                self.error('RUN MOPIDY {} COMMAND BAD RESPONSE? -> {}'.format(command.upper(), r.content.decode()))
            return res
        return None

    # noinspection PyUnusedLocal
    def increase_volume(self, *args):
        # Todo jugar con volumen y pause/resume en 'postponer'
        repeat = True
        if self._in_alarm_mode and self._last_trigger is not None:
            delta_sec = (dt.datetime.now() - self._last_trigger).total_seconds()
            if delta_sec > DURATION_VOLUME_RAMP_SEC:
                volume_set = MAX_VOLUME_MOPIDY
                repeat = False
            else:
                volume_set = int(max(5, (delta_sec / DURATION_VOLUME_RAMP_SEC) * MAX_VOLUME_MOPIDY))
            self.log('INCREASING MOPIDY VOLUME TO LEVEL {}'.format(volume_set))
            self.run_command_mopidy('core.mixer.set_volume', params=dict(volume=volume_set))
        else:
            repeat = False
        if repeat:
            self.run_in(self.increase_volume, 5)

    def run_mopidy_stream_lacafetera(self, ep_info):
        """Play stream in mopidy."""
        self.log('RUN_MOPIDY_STREAM_LACAFETERA', LOG_LEVEL)
        self.call_service('switch/turn_on', entity_id="switch.altavoz")
        self.run_command_mopidy('core.tracklist.clear')
        params = {"tracks": [{"__model__": "Track",
                              "uri": MASK_URL_STREAM_MOPIDY.format(ep_info['episode']['episode_id']),
                              "name": ep_info['episode']['title'],
                              "date": "{:%Y-%m-%d}".format(ep_info['published'])}]}
        json_res = self.run_command_mopidy('core.tracklist.add', params=params)
        if json_res is not None:
            self.log('Added track OK --> {}'.format(json_res))
            if ("result" in json_res) and (len(json_res["result"]) > 0):
                track_info = json_res["result"][0]
                self.run_command_mopidy('core.mixer.set_volume', params=dict(volume=5))
                res_play = self.run_command_mopidy('core.playback.play', params={"tl_track": track_info})
                if res_play is not None:
                    self._in_alarm_mode = True
                    self._last_trigger = dt.datetime.now()
                    self.run_in(self.increase_volume, 5)
                    return True
        self.log('MOPIDY NOT PRESENT??', 'ERROR')
        return False

    def prepare_context_alarm(self):
        """Initialize the alarm context (turn on devices, get ready the context, etc.)"""
        self.log('PREPARE_CONTEXT_ALARM', LOG_LEVEL)
        if self.play_in_kodi:
            return self.run_kodi_addon_lacafetera(mode='wakeup')
        else:
            return self.call_service('switch/turn_on', entity_id="switch.altavoz")
            # self.run_mopidy_stream_lacafetera()

    # noinspection PyUnusedLocal
    def trigger_service_in_alarm(self, *args):
        """Launch alarm secuence if ready, or set itself to retry in the short future."""
        # Check if alarm is ready to launch
        alarm_ready, alarm_info = is_last_episode_ready_for_play(self.datetime(), self._tz)
        # self.log('is_alarm_ready_to_trigger? {}, info={}'.format(alarm_ready, alarm_info), LOG_LEVEL)
        if alarm_ready:
            if self.play_in_kodi:
                ok = self.run_kodi_addon_lacafetera()
            else:
                ok = self.run_mopidy_stream_lacafetera(alarm_info)
            self.turn_on_lights_as_sunrise()
            # Notification:
            self.call_service(self._notifier.replace('.', '/'), **make_notification_episode(alarm_info))
            self.set_state(self._manual_trigger, state='on')
            duration = alarm_info['duration'].total_seconds() if ('duration' in alarm_info) else DEFAULT_DURATION * 3600
            self.run_in(self.turn_off_alarm_clock, int(duration))
            self.log('ALARM RUNNING NOW. AUTO STANDBY PROGRAMMED IN {:.0f} SECONDS'.format(duration), LOG_LEVEL)
        else:
            self.log('POSTPONE ALARM', LOG_LEVEL)
            self.run_in(self.trigger_service_in_alarm, STEP_RETRYING_SEC)

    # noinspection PyUnusedLocal
    def run_alarm(self, *args):
        """Run the alarm main secuence: prepare, trigger & schedule next"""
        self.set_state(self._manual_trigger, state='off')
        if self.datetime().weekday() in self._weekdays_alarm:
            ok = self.prepare_context_alarm()
            self.run_in(self.trigger_service_in_alarm, WARM_UP_TIME_DELTA.total_seconds())
        else:
            self.log('ALARM CLOCK NOT TRIGGERED TODAY (weekday={}, alarm weekdays={})'
                     .format(self.datetime().weekday(), self._weekdays_alarm))

    # noinspection PyUnusedLocal
    def postpone_secuencia_despertador(self, *args):
        """Botón de postponer alarma X min"""
        self.turn_off_alarm_clock()
        self.call_service('light/turn_off', entity_id=self._lights_alarm, transition=1)
        self.run_in(self.trigger_service_in_alarm, self._delta_time_postponer_sec)
        self.log('Postponiendo alarma {:.1f} minutos...'.format(self._delta_time_postponer_sec / 60.))


# if __name__ == '__main__':
#     def _run_mopidy_stream_lacafetera(ep_info):
#         """Play stream in mopidy."""
#         print('RUN_MOPIDY_STREAM_LACAFETERA with ep_info={}'.format(ep_info), LOG_LEVEL)
#
#         url_base = 'http://192.168.1.52:6680/mopidy/rpc'
#
#         url = MASK_URL_STREAM_MOPIDY.format(ep_info['episode']['episode_id'])
#         print(url, ep_info['episode']['site_url'])
#         headers = {'Content-Type': 'application/json'}
#         payload = {"method": "core.tracklist.add", "jsonrpc": "2.0", "id": 1,
#                    "params": {"tracks": None,
#                               "at_position": None,
#                               "uri": url}}
#         r = requests.post(url_base, headers=headers, data=json.dumps(payload))
#         print(r.content)
#         if r.ok:
#             result = r.text
#             json_res = json.loads(result)
#             if ("result" in json_res) and (len(json_res["result"]) > 0):
#                 track_info = json_res["result"][0]
#                 # track_info['track']['name'] = ep_info['episode']['title']
#                 # track_info['track']['name'] = 'lalala'
#                 # track_info['track']['name'] = ep_info['episode']['image_url']
#                 print('trackInfo: {}'.format(track_info))
#                 payload["method"] = "core.playback.play"
#                 payload["params"] = {"tl_track": track_info}  #, "on_error_step": -1}
#                 print('payload={}'.format(payload))
#                 r = requests.post(url_base, headers=headers, data=json.dumps(payload))
#                 print(r)
#                 print(r.content.decode())
#                 if r.ok:
#                     return True
#         print('MOPIDY NOT PRESENT? -> {}'.format(r.content), 'ERROR')
#         return False
#
#
#     is_ready, ep_info = is_last_episode_ready_for_play(dt.datetime.now(), tz=pytz.UTC)
#     print(is_ready)
#     print(ep_info)
#     print(ep_info['episode']['site_url'])
#     print('"https://www.spreaker.com/episode/10365929"')
#     print(_run_mopidy_stream_lacafetera(ep_info))
