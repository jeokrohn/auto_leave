#!/usr/bin/env python
import asyncio
import json
import logging
import os
import re
import socket
import ssl
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import backoff
import certifi
import websockets
from dotenv import load_dotenv
from pydantic import Extra, parse_obj_as
from wxc_sdk.as_api import AsWebexSimpleApi
from wxc_sdk.as_rest import AsRestError as RestError
from wxc_sdk.base import ApiModel
from wxc_sdk.common import RoomType
from wxc_sdk.integration import Integration
from wxc_sdk.people import Person
from wxc_sdk.rooms import Room
from wxc_sdk.scopes import parse_scopes
from wxc_sdk.tokens import Tokens
from yaml import safe_load

log = logging.getLogger(__name__)

DEFAULT_DEVICE_URL = "https://wdm-a.wbx2.com/wdm/api/v1"

DEVICE_NAME = 'auto_leave'

CREATE_DEVICE = {
    "deviceName": DEVICE_NAME,
    "deviceType": "DESKTOP",
    "localizedModel": "python",
    "model": "python",
    "name": DEVICE_NAME,
    "systemName": "python-client",
    "systemVersion": "0.1"
}


class Device(ApiModel):
    class Config:
        extra = Extra.ignore

    device_type: str
    name: str
    model: str
    url: str
    web_socket_url: str
    localized_model: str
    system_name: str
    system_version: str
    creation_time: datetime
    modification_time: datetime
    country_code: Optional[str]
    region_code: Optional[str]
    user_id: str
    org_id: str
    org_name: str


class WebexObject(ApiModel):
    class Config:
        extra = Extra.ignore
    id: Optional[str]
    object_type: str
    global_id: Optional[str]
    email_address: Optional[str]

    @property
    def space_id(self) -> Optional[str]:
        return self.object_type == 'conversation' and self.global_id or None


class Activity(MyModel):
    """
    Model to deserialize activities received on the websocket
    """
    object_type: str
    url: str
    published: datetime
    verb: str
    actor: Optional[WebexObject]
    object: WebexObject
    target: Optional[WebexObject]

    @property
    def space_id(self) -> Optional[str]:
        return self.object.space_id or self.target and self.target.space_id

    @property
    def actor_email(self) -> Optional[str]:
        return self.actor and self.actor.email_address


def build_integration() -> Integration:
    """
    read integration parameters from environment variables and create an integration

    :return: :class:`wxc_sdk.integration.Integration` instance
    """
    client_id = os.getenv('INTEGRATION_CLIENT_ID')
    client_secret = os.getenv('INTEGRATION_CLIENT_SECRET')
    scopes = parse_scopes(os.getenv('INTEGRATION_SCOPES'))
    redirect_url = 'http://localhost:6001/redirect'
    if not all((client_id, client_secret, scopes)):
        raise ValueError('failed to get integration parameters from environment')
    return Integration(client_id=client_id, client_secret=client_secret, scopes=scopes,
                       redirect_url=redirect_url)


def token_yml_path() -> str:
    """
    determine path of YML file to persist tokens

    :return: path to YML file
    :rtype: str
    """
    return os.path.join(os.getcwd(), f'{os.path.splitext(os.path.basename(__file__))[0]}_tokens.yml')


def config_yml_path() -> str:
    """
    determine path of YML config file

    :return: path to YML file
    :rtype: str
    """
    return os.path.join(os.getcwd(), f'{os.path.splitext(os.path.basename(__file__))[0]}.yml')


def get_tokens() -> Optional[Tokens]:
    """
    Tokens are read from a YML file. If needed an OAuth flow is initiated.

    :return: tokens
    :rtype: :class:`wxc_sdk.tokens.Tokens`
    """
    integration = build_integration()
    tokens = integration.get_cached_tokens_from_yml(yml_path=token_yml_path())
    return tokens


@dataclass(init=False)
class SpaceMonitor:
    """
    * get/register a device from/with WDM
    * set up websocket
    * wait for space join messages
        * check new spaces against block list
        * leave immediately if needed
    * start an initial task to check whether any spaces on the block list exist and leave them
    """
    api: AsWebexSimpleApi
    #: list of regular expressions to check space names against
    block_list: list[re.Pattern]
    me: Person
    # set to keep references to scheduled tasks.
    # see: https://docs.python.org/3/library/asyncio-task.html#creating-tasks
    tasks: set

    async def _get_devices(self) -> list[Device]:
        """
        Get list of current devices from WDM
        """
        r = await self.api.session.rest_get(f'{DEFAULT_DEVICE_URL}/devices')
        devices = parse_obj_as(list[Device], r['devices'])
        return devices

    async def _get_or_create_device(self) -> Device:
        """
        Get a device from WDM or create a new one if none exists
        """
        devices = await self._get_devices()
        device = next((d for d in devices if d.name == DEVICE_NAME), None)
        if device is None:
            # create new device
            r = await self.api.session.rest_post(f'{DEFAULT_DEVICE_URL}/devices', json=CREATE_DEVICE)
            device = Device.parse_obj(r)
        return device

    def __init__(self, *, api: AsWebexSimpleApi, block_list: list[str]):
        """
        Set up the space monitor instance
        :param api:
        :param block_list:
        """
        # cache API instance
        self.api = api
        # try to compile all entries in the block list. Force block list regexes to match full space titles
        try:
            self.block_list = list(map(lambda b: re.compile(f'^{b}$'), block_list))
        except re.error as e:
            print(f'Failed to compile block list regular expression: {e}')
            exit(1)
        self.tasks = set()

    async def _activity_in_blocked_space(self, space: Room):
        """
        We detected activity in an unwanted space
        :param space: the space the activity happened in
        """
        # TODO: is the other one a bot? Look for @webex.bot in person_email
        memberships = await self.api.membership.list(room_id=space.id)
        membership = next((m for m in memberships
                           if m.person_id == self.me.person_id),
                          None)
        try:
            if space.type == RoomType.direct:
                # try to hide the space
                membership.is_room_hidden = True
                await self.api.membership.update(update=membership)
        except RestError as e:
            log.error(f'activity_in_blocked_space: Error, {e}')

    async def _conversation_activity(self, data: dict):
        """
        Handle conversation activity.
        """
        activity: Activity = Activity.parse_obj(data['activity'])
        space_gid = activity.space_id
        log.debug(f'conversation_activity: {activity.verb} {activity.object.object_type}')
        if space_gid is None:
            return

        # check if the space name is on the block list
        space = await self.api.rooms.details(room_id=space_gid)
        log.debug(f'conversation activity: {activity.verb} {activity.object.object_type} by {activity.actor_email} '
                  f'in space "{space.title}"')

        if activity.actor_email == self.me.emails[0]:
            # ignore activities by me
            return
        if any(b.match(space.title) for b in self.block_list):
            # this space is on the block list
            log.debug(f'Space "{space.title}" is on the block list')
            await self._activity_in_blocked_space(space)
        return

    async def _handle_message(self, message_str: str):
        """
        Handle one message received from Websocket
        """
        msg = json.loads(message_str)

        data = msg['data']
        if data['eventType'] != 'conversation.activity':
            log.debug('not a conversation activity')
            return
        # schedule an async task to handle conversation activity
        # keep a reference of the task in a set to avoid that the task gets garbage collected
        task = asyncio.create_task(self._conversation_activity(data))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return

    async def run(self):
        """
        Run the space monitor
        """
        # get/register a device from/with WDM
        # get person details
        device, self.me = await asyncio.gather(self._get_or_create_device(),
                                               self.api.people.me())
        device: Device

        @backoff.on_exception(backoff.expo, websockets.ConnectionClosedError)
        @backoff.on_exception(backoff.expo, websockets.ConnectionClosedOK)
        @backoff.on_exception(backoff.expo, websockets.ConnectionClosed)
        @backoff.on_exception(backoff.expo, socket.gaierror)
        async def _connect_and_listen():
            ws_url = device.web_socket_url
            log.info(f"Opening websocket {ws_url}")
            ssl_context = ssl.create_default_context()
            ssl_context.load_verify_locations(certifi.where())

            async with websockets.connect(ws_url, ssl=ssl_context) as websocket:
                log.info("WebSocket Opened.")
                # send authentication/authorization message
                msg = {'id': str(uuid.uuid4()),
                       'type': 'authorization',
                       'data': {'token': 'Bearer ' + self.api.access_token}}
                await websocket.send(json.dumps(msg))

                while True:
                    # continuously receive and handle ws messages
                    message = await websocket.recv()
                    log.debug("WebSocket Received Message(raw): %s\n" % message)
                    try:
                        await self._handle_message(message)
                    except Exception as e:
                        log.error(f'Failed to handle message: {e}')

        try:
            await _connect_and_listen()
        except Exception as e:
            log.error(f"Error working the websocket: {e}")
        return

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return


async def main():
    load_dotenv()
    try:
        with open(config_yml_path(), mode='r') as file:
            config = safe_load(file)
        block_list = config['blocked']
    except Exception as e:
        print(f'Failed to read config: {e}')
        exit(1)
    tokens = get_tokens()
    if tokens is None:
        print('Failed to get tokens', file=sys.stderr)
        exit(1)
    async with AsWebexSimpleApi(tokens=tokens) as api:
        try:
            me = await api.people.me()
        except RestError as e:
            print(f'Failed to determine authenticated user: {e}')
            exit(1)
        print(f'Authenticated as {me.display_name}({me.emails[0]})')
        # noinspection PyUnboundLocalVariable
        async with SpaceMonitor(api=api, block_list=block_list) as monitor:
            await monitor.run()


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger('wxc_sdk.as_rest').setLevel(logging.INFO)
    asyncio.run(main())
