from utils.args import args
from dotenv import load_dotenv
load_dotenv(dotenv_path=args.env)

from flask import Flask, request
from utils.twitch_monitor import TwitchContextMonitor

PORT = 5000
HOST_SERVER_URL = f"http://127.0.0.1:{PORT}"
app = Flask(__name__)
twitch = TwitchContextMonitor()

## AUTH #########################

@app.route('/auth/redirect/code')
def redirect_code():
    global jaison
    twitch.set_tokens_from_code(request.args.get('code'))
    return "Authorization complete!"

@app.route('/auth/redirect/tokens', methods=['GET'])
def redirect_tokens():
    return "Complete!"

print(f"Click this link to authenticate: {twitch.OAUTH_AUTHORIZE_URL}")
app.run(port=PORT)