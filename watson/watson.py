# -*- coding: utf-8 -*-

import os
import json

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

import arrow
import click
import requests

from .config import ConfigParser
from .frames import Frames


class WatsonError(RuntimeError):
    pass


class ConfigurationError(WatsonError, configparser.Error):
    pass


class Watson(object):
    def __init__(self, **kwargs):
        """
        :param frames: If given, should be a list representating the
                        frames.
                        If not given, the value is extracted
                        from the frames file.
        :type frames: list

        :param current: If given, should be a dict representating the
                        current frame.
                        If not given, the value is extracted
                        from the state file.
        :type current: dict

        :param config_dir: If given, the directory where the configuration
                           files will be
        """
        self._current = None
        self._old_state = None
        self._frames = None
        self._last_sync = None
        self._config = None
        self._config_changed = False

        self._dir = kwargs.pop('config_dir', click.get_app_dir('watson'))

        self.config_file = os.path.join(self._dir, 'config')
        self.frames_file = os.path.join(self._dir, 'frames')
        self.state_file = os.path.join(self._dir, 'state')
        self.last_sync_file = os.path.join(self._dir, 'last_sync')

        if 'frames' in kwargs:
            self.frames = kwargs['frames']

        if 'current' in kwargs:
            self.current = kwargs['current']

        if 'last_sync' in kwargs:
            self.last_sync = kwargs['last_sync']

    def _load_json_file(self, filename, type=dict):
        """
        Return the content of the the given JSON file.
        If the file doesn't exist, return an empty instance of the
        given type.
        """
        try:
            with open(filename) as f:
                return json.load(f)
        except IOError:
            return type()
        except ValueError as e:
            # If we get an error because the file is empty, we ignore
            # it and return an empty dict. Otherwise, we raise
            # an exception in order to avoid corrupting the file.
            if os.path.getsize(filename) == 0:
                return type()
            else:
                raise WatsonError(
                    "Invalid JSON file {}: {}".format(filename, e)
                )
        else:
            raise WatsonError(
                "Impossible to open JSON file in {}".format(filename)
            )

    def _parse_date(self, date):
        return arrow.Arrow.utcfromtimestamp(date).to('local')

    def _format_date(self, date):
        if not isinstance(date, arrow.Arrow):
            date = arrow.get(date)

        return date.timestamp

    @property
    def config(self):
        """
        Return Watson's config as a ConfigParser object.
        """
        if not self._config:
            try:
                config = ConfigParser()
                config.read(self.config_file)
            except configparser.Error as e:
                raise ConfigurationError(
                    "Cannot parse config file: {}".format(e))

            self._config = config

        return self._config

    @config.setter
    def config(self, value):
        """
        Set a ConfigParser object as the current configuration.
        """
        self._config = value
        self._config_changed = True

    def save(self):
        """
        Save the state in the appropriate files. Create them if necessary.
        """
        try:
            if not os.path.isdir(self._dir):
                os.makedirs(self._dir)

            if self._current is not None and self._old_state != self._current:
                if self.is_started:
                    current = {
                        'project': self.current['project'],
                        'start': self._format_date(self.current['start']),
                        'tags': self.current['tags'],
                    }
                else:
                    current = {}

                with open(self.state_file, 'w+') as f:
                    json.dump(current, f, indent=1, ensure_ascii=False)

            if self._frames is not None and self._frames.changed:
                with open(self.frames_file, 'w+') as f:
                    json.dump(self.frames.dump(), f, indent=1,
                              ensure_ascii=False)

            if self._config_changed:
                with open(self.config_file, 'w+') as f:
                    self.config.write(f)

            if self._last_sync is not None:
                with open(self.last_sync_file, 'w+') as f:
                    json.dump(self._format_date(self.last_sync), f)
        except OSError as e:
            raise WatsonError(
                "Impossible to write {}: {}".format(e.filename, e)
            )

    @property
    def frames(self):
        if self._frames is None:
            self.frames = self._load_json_file(self.frames_file, type=list)

            include_current = self.config.getboolean('options',
                                                     'include_current_frame')
            if self.is_started and include_current:
                self._frames.add(self.current['project'],
                                 self.current['start'],
                                 arrow.now(),
                                 tags=self.current['tags']
                )

        return self._frames

    @frames.setter
    def frames(self, frames):
        self._frames = Frames(frames)

    @property
    def current(self):
        if self._current is None:
            self.current = self._load_json_file(self.state_file)

        if self._old_state is None:
            self._old_state = self._current

        return dict(self._current)

    @current.setter
    def current(self, value):
        if not value or 'project' not in value:
            self._current = {}

            if self._old_state is None:
                self._old_state = {}

            return

        start = value.get('start', arrow.now())

        if not isinstance(start, arrow.Arrow):
            start = self._parse_date(start)

        self._current = {
            'project': value['project'],
            'start': start,
            'tags': value.get('tags') or []
        }

        if self._old_state is None:
            self._old_state = self._current

    @property
    def last_sync(self):
        if self._last_sync is None:
            self.last_sync = self._load_json_file(
                self.last_sync_file, type=int
            )

        return self._last_sync

    @last_sync.setter
    def last_sync(self, value):
        if not value:
            self._last_sync = arrow.get(0)
            return

        if not isinstance(value, arrow.Arrow):
            value = self._parse_date(value)

        self._last_sync = value

    @property
    def is_started(self):
        return bool(self.current)

    def start(self, project, tags=None):
        if self.is_started:
            raise WatsonError(
                "Project {} is already started.".format(
                    self.current['project']
                )
            )

        if not project:
            raise WatsonError("No project given.")

        self.current = {'project': project, 'tags': tags}
        return self.current

    def stop(self):
        if not self.is_started:
            raise WatsonError("No project started.")

        old = self.current
        frame = self.frames.add(
            old['project'], old['start'], arrow.now(), tags=old['tags']
        )
        self.current = None

        return frame

    def cancel(self):
        if not self.is_started:
            raise WatsonError("No project started.")

        old_current = self.current
        self.current = None
        return old_current

    @property
    def projects(self):
        """
        Return the list of all the existing projects, sorted by name.
        """
        return sorted(set(self.frames['project']))

    @property
    def tags(self):
        """
        Return the list of the tags, sorted by name.
        """
        return sorted(set(tag for tags in self.frames['tags'] for tag in tags))

    def _get_request_info(self, route):
        config = self.config

        dest = config.get('backend', 'url')
        token = config.get('backend', 'token')

        if dest and token:
            dest = "{}/{}/".format(
                dest.rstrip('/'),
                route.strip('/')
            )
        else:
            raise ConfigurationError(
                "You must specify a remote URL (backend.url) and a token "
                "(backend.token) using the config command."
            )

        headers = {
            'content-type': 'application/json',
            'Authorization': "Token {}".format(token)
        }

        return dest, headers

    def _get_remote_projects(self):
        if not hasattr(self, '_remote_projects'):
            dest, headers = self._get_request_info('projects')

            try:
                response = requests.get(dest, headers=headers)
                assert response.status_code == 200

                self._remote_projects = response.json()
            except requests.ConnectionError:
                raise WatsonError("Unable to reach the server.")
            except AssertionError:
                raise WatsonError(
                    "An error occured with the remote "
                    "server: {}".format(response.json())
                )

        return self._remote_projects

    def pull(self):
        dest, headers = self._get_request_info('frames')

        try:
            response = requests.get(
                dest, params={'last_sync': self.last_sync}, headers=headers
            )
            assert response.status_code == 200
        except requests.ConnectionError:
            raise WatsonError("Unable to reach the server.")
        except AssertionError:
            raise WatsonError(
                "An error occured with the remote "
                "server: {}".format(response.json())
            )

        frames = response.json() or ()

        for frame in frames:
            try:
                # Try to find the project name, as the API returns an URL
                project = next(
                    p['name'] for p in self._get_remote_projects()
                    if p['url'] == frame['project']
                )
            except StopIteration:
                raise WatsonError(
                    "Received frame with invalid project from the server "
                    "(id: {})".format(frame['project']['id'])
                )

            self.frames[frame['id']] = (project, frame['start'], frame['stop'],
                                        frame['tags'])

        return frames

    def push(self, last_pull):
        dest, headers = self._get_request_info('frames/bulk')

        frames = []

        for frame in self.frames:
            if last_pull > frame.updated_at > self.last_sync:
                try:
                    # Find the url of the project
                    project = next(
                        p['url'] for p in self._get_remote_projects()
                        if p['name'] == frame.project
                    )
                except StopIteration:
                    raise WatsonError(
                        "The project {} does not exists on the remote server, "
                        "please create it or edit the frame (id: {})".format(
                            frame.project, frame.id
                        )
                    )

                frames.append({
                    'id': frame.id,
                    'start': str(frame.start),
                    'stop': str(frame.stop),
                    'project': project,
                    'tags': frame.tags
                })

        try:
            response = requests.post(dest, json.dumps(frames), headers=headers)
            assert response.status_code == 201
        except requests.ConnectionError:
            raise WatsonError("Unable to reach the server.")
        except AssertionError:
            raise WatsonError(
                "An error occured with the remote "
                "server: {}".format(response.json())
            )

        return frames
