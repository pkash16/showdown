import asyncio
import websockets
import requests
import json
import time
import html2text

from teams import load_team
import requests

import logging
logger = logging.getLogger(__name__)


class LoginError(Exception):
    pass


class SaveReplayError(Exception):
    pass


class PSWebsocketClient:

    websocket = None
    address = None
    login_uri = None
    username = None
    password = None
    last_message = None
    last_received_message = None
    last_user = None
    last_challenge_time = 0

    @classmethod
    async def create(cls, username, password, address):
        self = PSWebsocketClient()
        self.username = username
        self.password = password
        self.address = "ws://{}/showdown/websocket".format(address)
        self.websocket = await websockets.connect(self.address)
        self.login_uri = "https://play.pokemonshowdown.com/action.php"
        return self

    async def join_room(self, room_name):
        message = "/join {}".format(room_name)
        await self.send_message('', [message])
        logger.debug("Joined room '{}'".format(room_name))

    async def receive_message(self):
        message = await self.websocket.recv()
        logger.debug("Received message from websocket: {}".format(message))
        self.last_received_message = message
        return message

    async def send_message(self, room, message_list):
        message = room + "|" + "|".join(message_list)
        logger.debug("Sending message to websocket: {}".format(message))
        await self.websocket.send(message)
        self.last_message = message

    async def get_id_and_challstr(self):
        while True:
            message = await self.receive_message()
            split_message = message.split('|')
            if split_message[1] == 'challstr':
                return split_message[2], split_message[3]

    async def login(self):
        logger.debug("Logging in...")
        client_id, challstr = await self.get_id_and_challstr()
        if self.password:
            response = requests.post(
                self.login_uri,
                data={
                    'act': 'login',
                    'name': self.username,
                    'pass': self.password,
                    'challstr': "|".join([client_id, challstr])
                }
            )

        else:
            response = requests.post(
                self.login_uri,
                data={
                    'act': 'getassertion',
                    'userid': self.username,
                    'challstr': '|'.join([client_id, challstr]),
                }
            )

        if response.status_code == 200:
            if self.password:
                response_json = json.loads(response.text[1:])
                if not response_json['actionsuccess']:
                    logger.error("Login Unsuccessful")
                    raise LoginError("Could not log-in")

                assertion = response_json.get('assertion')
            else:
                assertion = response.text

            message = ["/trn " + self.username + ",0," + assertion]
            logger.debug("Successfully logged in")
            await self.send_message('', message)
        else:
            logger.error("Could not log-in\nDetails:\n{}".format(response.content))
            raise LoginError("Could not log-in")

    async def update_team(self, battle_format, team):
        if "random" in battle_format:
            logger.info("Setting team to None because the pokemon mode is {}".format(battle_format))
            message = ["/utm None"]
        else:
            message = ["/utm {}".format(team)]
        await self.send_message('', message)

    async def challenge_user(self, user_to_challenge, battle_format, team):
        logger.debug("Challenging {}...".format(user_to_challenge))
        if time.time() - self.last_challenge_time < 10:
            logger.info("Sleeping for 10 seconds because last challenge was less than 10 seconds ago")
            await asyncio.sleep(10)
        await self.update_team(battle_format, team)
        message = ["/challenge {},{}".format(user_to_challenge, battle_format)]
        await self.send_message('', message)
        self.last_challenge_time = time.time()

    async def accept_challenge(self, battle_format, team, room_name):
        if room_name is not None:
            await self.join_room(room_name)

        logger.debug("Waiting for a {} challenge".format(battle_format))
        await self.update_team(battle_format, team)
        username = None
        while username is None:
            msg = await self.receive_message()
            # while waiting for accepting a challenge, first see if we get any pokepaste links.
            split_msg = msg.split('|')
            if(len(split_msg) == 6 and split_msg[1] == "pm" and split_msg[4] == "pokepaste"):
                user = split_msg[2].strip()
                self.last_user = user
                # check for valid pokepaste link
                pokepaste_link = split_msg[5]
                # check the poke-paste link is valid
                url = pokepaste_link
                response = requests.get(url)
                if response.status_code == 200:
                    await self.send_message('', ["/msg " + user + ", updating pokepaste team..."])
                    set = html2text.html2text(response.text)
                    lines = set.split('\n')
                    lines = lines[1:-7]
                    lines = [line.strip() for line in lines]
                    for word in lines[:]:
                        if word == '':
                            lines.remove(word)
                        elif word.startswith("!"):
                            lines.remove(word)

                    # we removed all the lines, but now we need to add back the lines between pokemon.
                    newline_inserts = []
                    for idx in range(0, len(lines)-1):
                        line = lines[idx]
                        next_line = lines[idx+1]
                        if line.startswith("-") and (not next_line.startswith("-")):
                            # we have a new pokemon!
                            newline_inserts.append(idx+1)
                    for idx in range(0, len(newline_inserts)):
                        lines.insert(newline_inserts[idx]+idx, "")
                    
                    set = "\n".join(lines).strip()
                    f = open("teams/teams/gen9/qpl5/paste", "w")
                    f.write(set)
                    f.close()
                    team = load_team("gen9/qpl5/paste")
                    await self.update_team("gen9qpl5", team)
                    await self.send_message('', ["/msg " + user + ", updated!"])
                else:
                    await self.send_message('', ["/msg " + user + ", error updating team."])

            if (
                len(split_msg) == 9 and
                split_msg[1] == "pm" and
                split_msg[3].strip().replace("!", "").replace("‽", "") == self.username and
                split_msg[4].startswith("/challenge") and
                split_msg[5] == battle_format
            ):
                username = split_msg[2].strip()

        message = ["/accept " + username]
        await self.send_message('', message)

    async def search_for_match(self, battle_format, team):
        logger.debug("Searching for ranked {} match".format(battle_format))
        await self.update_team(battle_format, team)
        message = ["/search {}".format(battle_format)]
        await self.send_message("", message)

    async def leave_battle(self, battle_tag, save_replay=False):
        if save_replay:
            await self.save_replay(battle_tag)

        message = ["/leave {}".format(battle_tag)]
        await self.send_message('', message)

        while True:
            msg = await self.receive_message()
            if battle_tag in msg and 'deinit' in msg:
                return

    async def save_replay(self, battle_tag):
        message = ["/savereplay"]
        await self.send_message(battle_tag, message)

        while True:
            msg = await self.receive_message()
            if msg.startswith("|queryresponse|savereplay|"):
                obj = json.loads(msg.replace("|queryresponse|savereplay|", ""))
                log = obj['log']
                identifier = obj['id']
                post_response = requests.post(
                    "https://play.pokemonshowdown.com/~~showdown/action.php?act=uploadreplay",
                    data={
                        "log": log,
                        "id": identifier
                    }
                )
                if post_response.status_code != 200:
                    raise SaveReplayError("POST to save replay did not return a 200: {}".format(post_response.content))
                break
