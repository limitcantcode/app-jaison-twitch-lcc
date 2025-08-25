import websockets
import requests
import json
import urllib
import os
import time
from datetime import datetime
import yaml
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from enum import IntEnum

from utils.logging import logger
from utils.helper import get_twitch_sub_tier

class ChatModeEnum(IntEnum):
    ALL=1
    KEYWORD=2
    HIGHLIGHT=3
    BITS=4
    DISABLE=5

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
        "scope": "user:read:chat moderator:read:followers bits:read channel:read:subscriptions channel:read:charity channel:read:hype_train channel:read:redemptions" # https://dev.twitch.tv/docs/authentication/scopes/
    }))
    SUMMARIZATION_PROMPT = """
You are generating a summary of a Twitch chat making use of the previously generated summary and all the latest Twitch chat messages since then.
The user will provide the summary under the header "### Previous summary ###" and the latest messages under "### New messages ###"

For example, the user may input:

### Previous summary ###
The chat is all saying hi.

### New messages ###
[limit]: What are we doing this stream?


You will then output something like the following:

Chat is now asking what is going on in stream.


Please keep these summaries to 6 sentences or less.
"""
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
        self.jaison_ws_endpoint = str(self.config["jaison-ws-endpoint"])
        self.access_token = None
        self.refresh_token = None
        self._load_tokens()

        self.chatting_method = getattr(ChatModeEnum, str(self.config["chat-mode"]))
        self.keywords = self.config.get("chat-keywords", "").split(",")
        if "" in self.keywords: self.keywords.remove("")
        self.bits_threshold = self.config.get("chat-bits-threshold", 0)

    async def run(self):
        # Twitch chat summary context setup
        self.context_id = "twitch-chat-monitor-lcc"
        self.context_name = "Twitch Chat Summary"
        self.context_description = '''This is a summary of changes in Twitch chat since the last Twitch Chat Summary.'''
        self.chat_history = [] # {"name","message"}

        self.chat_summary: str = "No previous summary"
        if self.config['summary-interval'] > 0:
            self.scheduler: AsyncIOScheduler = AsyncIOScheduler()
            self.scheduler.start()
            self.chat_update_timer = self.scheduler.add_job(
                self._interval_chat_context_updater,
                'interval',
                seconds=10,
                args=[],
                id='chat_update_timer',
                replace_existing=True
            )
        
        # Twitch event sub setup
        self.event_ws = None
        self.twitch_event_task = asyncio.create_task(self._event_loop())

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
            logger.error("{} is missing or malformed. Needs to be reauthenticated at {}".format(self.TOKEN_FILE,self.OAUTH_AUTHORIZE_URL))
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
            logger.warning("Can't subscribe to events until authenticated. Please authenticate at {}".format(self.OAUTH_AUTHORIZE_URL))
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
                logger.debug("Forbidden subscription request. Refreshing tokens")
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
                logger.warning(f"Failing to subscribe to event: {response.json()}")
                raise Exception("Can't complete subscription")
            

    # Connect a new socket and resubscribe to events on its new session
    async def _setup_socket(self, reconnect_url: str = None):
        try:
            new_ws = await websockets.connect(reconnect_url or "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=10")
            welcome_msg = json.loads(await new_ws.recv())
            if self.event_ws:
                await self.event_ws.close()
            self.event_ws = new_ws
            logger.debug(f'Connected new subscription events websocket: {welcome_msg}')
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
                },
                {
                    "type": "channel.bits.use", # scope: bits:read
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
                    "type": "channel.channel_points_automatic_reward_redemption.add", # scope: channel:read:redemptions
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
            logger.error("Failed to setup Twitch subscribed events websocket: {}".format(err))
            return False

    # Wrapper for self._setup_socket to reattempt until success, retrying after delay on failure
    async def setup_socket(self, reconnect_url: str = None):
        while True:
            logger.debug("Attempting to setup Twitch subscribed events websocket...")
            if await self._setup_socket(reconnect_url=reconnect_url):
                break
            time.sleep(5)

    def _register_chat_context(self):
        logger.critical("Registering context")
        response = requests.put(
            self.jaison_api_endpoint+'/api/context/custom',
            headers={"Content-type":"application/json"},
            json={
                "context_id": self.context_id,
                "context_name": self.context_name,
                "context_description": self.context_description
            }
        )
        
        logger.critical(response.json())
        
        if response.status_code != 200:
            raise Exception(f"Failed to register chat context: {response.status_code} {response.reason}")

    def _generate_summary_input(self):
        content = ""
        for msg_o in self.chat_history:
            content += "{}: {}\n".format(msg_o['name'], msg_o['message'])
    
        result = "### Previous summary ###\n\n{prev_summary}\n### New messages ###\n\n{new_messages}".format(
            prev_summary = self.chat_summary,
            new_messages = content
        )
        logger.debug(f"Generated summary input: {result}")
        return result
        
    async def _interval_chat_context_updater(self):
        try:
            if len(self.chat_history) == 0: return
            
            # generate new summary
            summary = ""
            
            async with websockets.connect(self.jaison_ws_endpoint) as ws:
                job_request_response = requests.post(
                    self.jaison_api_endpoint+"/api/operations/use",
                    headers={"Content-type":"application/json"},
                    json={
                        "role": "mcp",
                        "payload": {
                            "instruction_prompt": self.SUMMARIZATION_PROMPT, 
                            "messages": [{"type": "raw", "message": self._generate_summary_input()}]
                        }
                    }
                )
                if job_request_response.status_code != 200:
                    raise Exception(f"Failed to register chat context: {job_request_response.status_code} {job_request_response.reason}")
                
                parsed_job_request = job_request_response.json()
                job_id = parsed_job_request['response']['job_id']
                
                while True:
                    data = json.loads(await ws.recv())
                    event, status = data[0], data[1]
                    if event.get('response', {}).get('job_id') == job_id:
                        if not event.get('response', {}).get("finished", False):
                            summary += event['response'].get('result', {}).get('content', "")
                        elif event.get('response', {}).get("finished", False) and not event.get('response', {}).get("success", False):
                            raise Exception(f"Failed to summarize chat: {job_request_response.status_code} {job_request_response.reason}")
                        else:
                            break
                
            # save new summary
            logger.debug(f"Got new twitch chat summary: {summary}")
            self.chat_summary = summary
            
            # chat history
            self.chat_history.clear()
            
            # send new summary
            if summary:
                async with websockets.connect(self.jaison_ws_endpoint) as ws:
                    response = requests.post(
                        self.jaison_api_endpoint+'/api/context/custom',
                        headers={"Content-type":"application/json"},
                        json={
                            "context_id": self.context_id,
                            "context_contents": summary,
                            "timestamp": datetime.now().timestamp()
                        }
                    )

                    if response.status_code != 200:
                        raise Exception(f"{response.status_code} {response.reason}")
                    
                    parsed_response = response.json()
                    job_id = parsed_response['response']['job_id']
                    while True:
                        data = json.loads(await ws.recv())
                        event, status = data[0], data[1]
                        if event.get('response', {}).get('job_id') == job_id:
                            payload = event.get('response', {})
                            if "success" in payload:
                                if not payload["success"]:
                                    self._register_chat_context()
                                    break
                                else:
                                    break
        except Exception as err:
            logger.error(f"Failed to update Twitch chat context", exc_info=True)

    def request_jaison(self, request_msg):
        response = requests.post(
            self.jaison_api_endpoint+'/api/context/request',
            headers={"Content-type":"application/json"},
            json={
                "content": request_msg
            }
        ).json()

        if response['status'] == 500:
            logger.error(f"Failed to send a request: {response['message']}")
            raise Exception(response['message'])
        
    def converse_jaison(self, user, message):
        response = requests.post(
            self.jaison_api_endpoint+'/api/context/conversation/text',
            headers={"Content-type":"application/json"},
            json={
                "user": user,
                "timestamp": datetime.now().timestamp(),
                "content": message
            }
        )
        if response.status_code != 200:
            raise Exception(f"{response.status_code} {response.reason}")

    # Main event loop for handling incoming events from Twitch
    async def _event_loop(self):
        logger.debug("Started event loop!")
        await self.setup_socket()
        logger.info("Twitch Monitor Ready")
        while True:
            try:
                event = json.loads(await self.event_ws.recv())
                logger.debug("Event loop received event: {}".format(event))
                if 'metadata' not in event or 'payload' not in event: # Expect message to have a specific structure
                    logger.warning("Unexpected event: {}".format(event))
                if event['metadata']['message_type'] == "notification": # Handling subscribed events
                    event = event['payload']
                    if 'subscription' in event:
                        try:
                            if event['subscription']['type'] == 'channel.chat.message':
                                name = event['event']['chatter_user_name']
                                message = event['event']['message']['text']
                                self.chat_history.append({
                                    "name": name,
                                    "message": message
                                })
                                self.chat_history = self.chat_history[-(self.MAX_CHAT_LENGTH):]
                                
                                if self.chatting_method <= ChatModeEnum.ALL:
                                    self.converse_jaison(name, message)
                                elif self.chatting_method <= ChatModeEnum.KEYWORD:
                                    for keyword in self.keywords:
                                        if keyword in message:
                                            self.converse_jaison(name, message)
                                            break
                            elif event['subscription']['type'] == 'channel.follow':
                                self.request_jaison("Say thank you to {} for the follow.".format(event['event']['user_name']))
                            elif event['subscription']['type'] == 'channel.subscribe':
                                if not event['event']['is_gift']:
                                    self.request_jaison("Say thank you to {} for the tier {} sub.".format(event['event']['user_name'], get_twitch_sub_tier(event['event']['tier'])))
                            elif event['subscription']['type'] == 'channel.subscription.gift':
                                message = "Say thank you" if event['event']['is_anonymous'] else "Say thank you to {}".format(event['event']['user_name'])
                                message += " for the {} tier {} gifted subs.".format(event['event']['cumulative_total'], get_twitch_sub_tier(event['event']['tier']))
                                self.request_jaison(message)
                            elif event['subscription']['type'] == 'channel.subscription.message':
                                self.request_jaison("{} says {}. Thank them for their tier {} sub.".format(event['event']['user_name'], event['event']['message']['text'], get_twitch_sub_tier(event['event']['tier'])))
                            elif event['subscription']['type'] == 'channel.raid':
                                self.request_jaison("Thank {} for raiding you with {} viewers.".format(event['event']['from_broadcaster_user_name'], event['event']['viewers']))
                            elif event['subscription']['type'] == 'channel.charity_campaign.donate':
                                self.request_jaison("Thank {} for donating {} {} to {}.".format(event['event']['user_name'], event['event']['amount']['value'], event['event']['amount']['currency'], event['event']['charity_name']))
                            elif event['subscription']['type'] == 'channel.hype_train.begin':
                                self.request_jaison("A Twitch hype train started. Hype up the hype train.")
                            elif event['subscription']['type'] == 'channel.hype_train.end':
                                self.request_jaison("The Twitch hype train has finished  at level {}. Thank the viewers for all their effort.".format(event['event']['level']))
                            elif self.chatting_method <= ChatModeEnum.HIGHLIGHT and event['subscription']['type'] == 'channel.channel_points_automatic_reward_redemption.add':
                                if event['event']['reward']['type'] == "send_highlighted_message":
                                    user = event['event']['user_name']
                                    message = event['event']['message']['text']
                                    self.converse_jaison(user, message)
                            elif event['subscription']['type'] == 'channel.bits.use':
                                user = event['event']['user_name']
                                bits_spent = event['event']['bits']
                                byte_redemption_type = event['event']['type'] # cheer for message or powerup for whatever
                                if self.chatting_method <= ChatModeEnum.HIGHLIGHT and byte_redemption_type == 'cheer' and bits_spent >= self.bits_threshold:
                                    message = event['event']['message']['text']
                                    self.converse_jaison(user, f"(spent {bits_spent} bits) {message}")
                                else: 
                                    message = "Say thank you to {} for the {} bits.".format(event['event']['user_name'], event['event']['bits'])
                                    self.request_jaison(message)                                
                            else:
                                logger.warning("Unhandled event subscription: {}".format(event))
                        except Exception as err:
                            logger.error("Request failed for event: {}".format(event), exc_info=True)
                    else:
                        logger.warning("Unknown event response: {}".format(event))
                elif event['metadata']['message_type'] == "session_reconnect": # Handling reconnect request
                    self.setup_socket(event['payload']['session']['reconnect_url'])
                elif event['metadata']['message_type'] == "revocation": # Notified of a subscription being removed by Twitch
                    logger.warning("A Twitch event subscrption has been revoked: {}".format(event['payload']['subscription']['type']))
            except Exception as err:
                # Event must continue to run even in event of error
                logger.error(f"Event loop ran into an error: {err}")