from utils.args import args
from dotenv import load_dotenv
load_dotenv(dotenv_path=args.env)

from utils.twitch_monitor import TwitchContextMonitor

twitch = TwitchContextMonitor()
twitch.run()
