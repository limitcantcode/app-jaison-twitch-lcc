import websockets
import requests
import json
import urllib
import os
import time
from utils.logging import logger
import yaml
import asyncio
from threading import Thread

'''
Class for interfacing with Twitch to track chat history and stream events using websockets
For reference: https://dev.twitch.tv/docs/eventsub/

We get user app tokens using OAuth code grant flow: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#authorization-code-grant-flow

Usage:
- FOR CHAT HISTORY: call self.get_chat_history()
- FOR TWITCH EVENTS: subscribe to ObserverServer self.broadcast_server and listen for event "twitch_event"
'''
class TwitchContextMonitor():
    CLIENT_ID = os.getenv("TWITCH_APP_ID")
    CLIENT_SECRET = os.getenv("TWITCH_APP_TOKEN")
    MAX_CHAT_LENGTH = 40
    OAUTH_REDIRECT_CODE = "http://localhost:5000/auth/redirect/code" # Needs to be added on Twitch Dev Console as well
    OAUTH_REDIRECT_TOKENS = "http://localhost:5000/auth/redirect/tokens" # Needs to be added on Twitch Dev Console as well
    OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    OAUTH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize?{}".format(urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_CODE,
        "response_type": "code",
        "scope": "user:read:chat moderator:read:followers bits:read channel:read:subscriptions channel:read:charity channel:read:hype_train" # https://dev.twitch.tv/docs/authentication/scopes/
    }))
    logger = logger

    # List of events:
    #   twitch_event: Triggered when a new twitch event occurs
    # broadcast_server = ObserverServer()

    def __init__(self):
        with open('config.yaml', 'r') as f:
            self.config = yaml.safe_load(f)
        self.TOKEN_FILE = os.path.join(os.getcwd(),'tokens','twitch_api_tokens.json')
        self.broadcaster_id = str(self.config["twitch-target-id"])
        self.user_id = str(self.config["twitch-bot-id"])
        self.jaison_api_endpoint = str(self.config["jaison-api-endpoint"])
        self.access_token = None
        self.refresh_token = None
        self._load_tokens()


    def run(self):
        self.event_ws = None
        self.chat_updater_thread = Thread(target=self._interval_chat_context_updater,daemon=True)
        self.chat_updater_thread.start()
        self.context_id = "twitch-chat-monitor-lcc"
        self.context_name = "Twitch Chat"
        self.context_description = '''Last {} messages in Twitch chat. Each message is on a new line. Name of chatter is in front and message is everything after ":".'''.format(self.MAX_CHAT_LENGTH)
        self.chat_history = [] # {"name","message"}

        self.async_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.async_loop)
        self.async_loop.run_until_complete(self._event_loop())

    # Loads tokens from file if it exists
    # If token file does not exist or is not formatted correctly, then logs an error and does nothing else
    # Never fails, only returns status of successful load
    def _load_tokens(self) -> bool:
        try:
            with open(self.TOKEN_FILE, 'r') as f:
                token_o = json.load(f)
                self.access_token = token_o['access_token']
                self.refresh_token = token_o['refresh_token']
            return True
        except:
            self.logger.error("{} is missing or malformed. Needs to be reauthenticated at {}".format(self.TOKEN_FILE,self.OAUTH_AUTHORIZE_URL))
            return False

    # Use loaded refresh token to save a new access/refresh token pair
    def _refresh_tokens(self):
        response = requests.post(
            self.OAUTH_TOKEN_URL,
            params={
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
                "refresh_token": self.refresh_token,
                "grant_type": 'refresh_token',
            }
        ).json()
        self.set_tokens(response['access_token'], response['refresh_token'])

    # Uses code (from webui) to save a new access/refresh token pair
    def set_tokens_from_code(self, code):
        response = requests.post(
            self.OAUTH_TOKEN_URL,
            params={
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
                "code": code,
                "grant_type": 'authorization_code',
                "redirect_uri": self.OAUTH_REDIRECT_TOKENS
            }
        ).json()
        self.set_tokens(response['access_token'], response['refresh_token'])

    # Saves new access/refresh token pair to file and reloads from that file
    def set_tokens(self, access_token, refresh_token):
        with open(self.TOKEN_FILE, 'w') as f:
            json.dump({
                "access_token": access_token,
                "refresh_token": refresh_token
            }, f, indent=4)

        self._load_tokens()
        
    # Attempts subscription using Twitch Events Sub API
    # For reference: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/
    def _subscribe(self):
        if self.access_token is None:
            self.logger.warning("Can't subscribe to events until authenticated. Please authenticate at {}".format(self.OAUTH_AUTHORIZE_URL))
            raise Exception("Can't complete subscription")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.CLIENT_ID,
            "Content-Type": "application/json"
        }
        for data in self.event_sub_data:
            response = requests.post(
                'https://api.twitch.tv/helix/eventsub/subscriptions',
                headers=headers,
                json=data
            )
            if response.status_code == 401: # In case forbidden, refresh tokens and retry once
                self.logger.debug("Forbidden subscription request. Refreshing tokens")
                self._refresh_tokens()
                headers = {
                    "Authorization": f"Bearer {self.access_token}",
                    "Client-Id": self.CLIENT_ID,
                    "Content-Type": "application/json"
                }
                response = requests.post(
                    'https://api.twitch.tv/helix/eventsub/subscriptions',
                    headers=headers,
                    json=data
                )

            if response.status_code != 202: # If not successful, signal failure
                self.logger.warning(f"Failing to subscribe to event: {response.json()}")
                raise Exception("Can't complete subscription")
            

    # Connect a new socket and resubscribe to events on its new session
    async def _setup_socket(self, reconnect_url: str = None):
        try:
            new_ws = await websockets.connect(reconnect_url or "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=10")
            welcome_msg = json.loads(await new_ws.recv())
            if self.event_ws:
                await self.event_ws.close()
            self.event_ws = new_ws
            self.logger.debug(f'Connected new subscription events websocket: {welcome_msg}')
            self.session_id = welcome_msg['payload']['session']['id']
            
            # List of subscriptables: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#subscription-types
            self.event_sub_data = [
                {
                    "type": "channel.chat.message", # scope: user:read:chat
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id,
                        "user_id": self.user_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.follow", # scope: moderator:read:followers
                    "version": "2",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id,
                        "moderator_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.cheer", # scope: bits:read
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.subscribe", # scope: channel:read:subscriptions
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.subscription.gift", # scope: channel:read:subscriptions
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.subscription.message", # scope: None
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.raid", # scope: None
                    "version": "1",
                    "condition": {
                        "to_broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.charity_campaign.donate", # scope: channel:read:charity
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                # hype train
                {
                    "type": "channel.hype_train.begin", # scope: channel:read:hype_train
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                },
                {
                    "type": "channel.hype_train.end", # scope: channel:read:hype_train
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id": self.broadcaster_id
                    },
                    "transport": {
                        "method": "websocket",
                        "session_id": self.session_id
                    }
                }
            ]

            self._subscribe()
            return True
        except Exception as err:
            self.logger.error("Failed to setup Twitch subscribed events websocket: {}".format(err))
            return False

    # Wrapper for self._setup_socket to reattempt until success, retrying after delay on failure
    async def setup_socket(self, reconnect_url: str = None):
        while True:
            self.logger.debug("Attempting to setup Twitch subscribed events websocket...")
            if await self._setup_socket(reconnect_url=reconnect_url):
                break
            time.sleep(5)

    def _interval_chat_context_updater(self):
        while True:
            time.sleep(1)
            logger.critical("Sending context") # debug
            content = ""
            for msg_o in self.chat_history:
                content += "{}: {}\n".format(msg_o['name'], msg_o['message'])

            response = requests.put(
                self.jaison_api_endpoint+'/context',
                headers={"Content-type":"application/json"},
                json={
                    "id": self.context_id,
                    "content": content
                }
            ).json()

            if response['status'] != 200:
                logger.error(f"Failed to request update on chat context: {response['message']}")
                requests.delete(
                    self.jaison_api_endpoint+'/context',
                    headers={"Content-type":"application/json"},
                    json={"id": self.context_id}
                )
                requests.post(
                    self.jaison_api_endpoint+'/context',
                    headers={"Content-type":"application/json"},
                    json={
                        "id": self.context_id,
                        "name": self.context_name,
                        "description": self.context_description
                    }
                )

    def request_jaison(self, request_msg):
        response = requests.post(
            self.jaison_api_endpoint+'/run',
            headers={"Content-type":"application/json"},
            json={
                "process_request": True,
                "input_text": request_msg,
                "output_text": True,
                "output_audio": True
            }
        ).json()

        if response['status'] == 500:
            logger.error(f"Failed to send a request: {response['message']}")
            raise Exception(response['message'])

    # Main event loop for handling incoming events from Twitch
    async def _event_loop(self):
        self.logger.debug("Started event loop!")
        await self.setup_socket()
        self.logger.info("Twitch Monitor Ready")
        while True:
            try:
                event = json.loads(await self.event_ws.recv())
                self.logger.debug("Event loop received event: {}".format(event))
                if 'metadata' not in event or 'payload' not in event: # Expect message to have a specific structure
                    self.logger.warning("Unexpected event: {}".format(event))
                if event['metadata']['message_type'] == "notification": # Handling subscribed events
                    event = event['payload']
                    if 'subscription' in event:
                        try:
                            if event['subscription']['type'] == 'channel.chat.message':
                                self.chat_history.append({
                                    "name": event['event']['chatter_user_name'],
                                    "message": event['event']['message']['text']
                                })
                                self.chat_history = self.chat_history[:self.MAX_CHAT_LENGTH]
                            elif event['subscription']['type'] == 'channel.follow':
                                self.request_jaison("Say thank you to {} for the follow.".format(event['event']['user_name']))
                            elif event['subscription']['type'] == 'channel.cheer':
                                message = "Say thank you" if event['event']['is_anonymous'] else  "Say thank you to {}".format(event['event']['user_name'])
                                message += " for the {} bits.".format(event['event']['bits'])
                                self.request_jaison(message)
                            elif event['subscription']['type'] == 'channel.subscribe':
                                if not event['event']['is_gift']:
                                    self.request_jaison("Say thank you to {} for the tier {} sub.".format(event['event']['user_name'], event['event']['tier']))
                            elif event['subscription']['type'] == 'channel.subscription.gift':
                                message = "Say thank you" if event['event']['is_anonymous'] else "Say thank you to {}".format(event['event']['user_name'])
                                message += " for the {} tier {} gifted subs.".format(event['event']['cumulative_total'], event['event']['tier'])
                                self.request_jaison(message)
                            elif event['subscription']['type'] == 'channel.subscription.message':
                                self.request_jaison("{} says {}. Thank them for their tier {} sub.".format(event['event']['user_name'], event['event']['message']['text'], event['event']['tier']))
                            elif event['subscription']['type'] == 'channel.raid':
                                self.request_jaison("Thank {} for raiding you with {} viewers.".format(event['event']['from_broadcaster_user_name'], event['event']['viewers']))
                            elif event['subscription']['type'] == 'channel.charity_campaign.donate':
                                self.request_jaison("Thank {} for donating {} {} to {}.".format(event['event']['user_name'], event['event']['amount']['value'], event['event']['amount']['currency'], event['event']['charity_name']))
                            elif event['subscription']['type'] == 'channel.hype_train.begin':
                                self.request_jaison("A Twitch hype train started. Hype up the hype train.")
                            elif event['subscription']['type'] == 'channel.hype_train.end':
                                self.request_jaison("The Twitch hype train has finished  at level {}. Thank the viewers for all their effort.".format(event['event']['level']))
                            else:
                                logger.warning("Unhandled event subscription: {}".format(event))
                        except Exception as err:
                            logger.error("Request failed for event: {}".format(event), exc_info=True)
                    else:
                        logger.warning("Unknown event response: {}".format(event))
                elif event['metadata']['message_type'] == "session_reconnect": # Handling reconnect request
                    self.setup_socket(event['payload']['session']['reconnect_url'])
                elif event['metadata']['message_type'] == "revocation": # Notified of a subscription being removed by Twitch
                    self.logger.warning("A Twitch event subscrption has been revoked: {}".format(event['payload']['subscription']['type']))
            except Exception as err:
                # Event must continue to run even in event of error
                self.logger.error(f"Event loop ran into an error: {err}")