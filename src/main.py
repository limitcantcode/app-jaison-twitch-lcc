
from dotenv import load_dotenv
import asyncio
from utils.args import args
load_dotenv(dotenv_path=args.env)

from utils.twitch_monitor import TwitchContextMonitor

twitch = TwitchContextMonitor()
event_loop = asyncio.new_event_loop()
asyncio.set_event_loop(event_loop)
event_loop.create_task(twitch.run())
event_loop.run_forever()
