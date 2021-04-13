import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import yaml
import json
import os
from typing import Optional, Dict, List
import contextlib
import requests
import re

import sqlite3
from aiohttp import web

# https://docs.aiohttp.org/en/stable/web_reference.html#request-and-base-request

class Spotify:
    client_id: str
    client_secret: str
    _token: str
    _expires: int

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None

    def token(self):
        if not self._token or datetime.now() >= self._expires:
            r = requests.post(
                url='https://accounts.spotify.com/api/token',
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
            )
            d=r.json()
            self._token = d.get('access_token')
            self._expires = datetime.now() + timedelta(seconds=d.get('expires_in'))
        return self._token

    def parse_url(self, url):
        a = re.compile(
            f"""
                ^
                https://open\.spotify\.com\/
                ([^\/]+)\/
                ([^?]+)
                ([?].*)
                $
            """,
            re.X
        )

        m = a.match(url)

        if m:
            return {"type": m.group(1), "id": m.group(2)}

    def data(self, url):
        item = self.parse_url(url)
        if item:
            r = requests.get(
                url=f'https://api.spotify.com/v1/{item["type"]}s/{item["id"]}',
                headers={"Authorization": f"Bearer {self.token()}"},
            )
            return r.json()




class App(hass.Hass):
    current: Dict[str,str]
    cards: "Cards"
    spotify: "Spotify"

    def initialize(self):
        self.log(f"Initializing '{self.name}' with args {self.args}")
        self.cards = Cards()
        self.spotify = Spotify(
            self.args['spotify']['client_id'],
            self.args['spotify']['client_secret'],
        )
        self.current = {}
        for entity in self.args.get('tag_id_entities', []):
            self.current[entity] = None
            self.log(f"Listening '{entity}' for tag changes...")
            self.on_event(entity, None, None, self.get_state(entity), {})
            self.listen_state(self.on_event, entity)

        self.register_route(self.index)

    def fix_cards(self):
        for card in self.cards.list():
            self.set_card_from_spotify_url(card, card.content_id)

    def set_card_from_spotify_url(self, card, url):
        item = self.spotify.parse_url(url)
        if item:
            data = self.spotify.data(url)
            self.log(data)
            card.title = ""
            if "artists" in data:
                card.title += ", ".join(artist['name'] for artist in data['artists'])
                card.title += " - "
            card.title += data['name']
            if "description" in data:
                card.description = data['description']
            card.art = data['images'][0]['url']
            card.content_id = data['external_urls']['spotify']
            card.content_type = item['type']
            self.cards.store(card)

    def on_event(self, entity, attribute, old, new, kwargs):
        self.log(f"Tag on {entity} changed to: '{new}'")
        if new not in ("unavailable", "unknown"):
            self.current[entity] = new

    async def index(self, req):
        self.log(f"Index({req.path})")

        if 'id' in req.query:
            return self.web_get_set_card(req)
        elif 'drop' in req.query:
            return self.web_drop_card(req)
        elif 'current' in req.query:
            return self.web_current_card(req)
        elif 'list' in req.query:
            return self.web_list_cards(req)
        elif 'token' in req.query:
            return self.web_token(req)
        elif 'parse' in req.query:
            return self.web_parse(req, req.query['parse'])
        elif 'icon' in req.query:
            return self.icon(req)
        else:
            return self.web_page(req)

    def web_get_set_card(self, req):
        card = self.cards.get(req.query['id'])
        if 'content_id' in req.query:
            self.set_card_from_spotify_url(card, req.query['content_id'])
        if 'shuffle' in req.query:
            card.shuffle = req.query['shuffle'] == "on"
        if 'content_id' in req.query or 'shuffle' in req.query:
            self.cards.store(card)
        return web.Response(text=card.json(), content_type="application/json")

    def web_drop_card(self, req):
        card = self.cards.get(req.query['drop'])
        if card:
            self.cards.drop(card)
        return web.Response(text=card.json(), content_type="application/json")

    def web_current_card(self, req):
        current_cards = [
            {current: self.cards.get(current).as_dict() if current else None}
            for current in self.current.values()
        ]

        return web.Response(text=json.dumps(current_cards), content_type="application/json")

    def web_list_cards(self, req):
        #self.fix_cards()
        all_cards = {
            card.id: card.as_dict()
            for card in self.cards.list()
        }

        return web.Response(text=json.dumps(all_cards), content_type="application/json")

    def web_token(self, req):
        return web.Response(text=json.dumps(self.spotify.token()), content_type="application/json")

    def web_parse(self, req, url):
        return web.Response(text=json.dumps(self.spotify.data(url)), content_type="application/json")

    def icon(self, req):
        svg = """<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="24" height="24" viewBox="0 0 24 24"><path d="M16,9V7H12V12.5C11.58,12.19 11.07,12 10.5,12A2.5,2.5 0 0,0 8,14.5A2.5,2.5 0 0,0 10.5,17A2.5,2.5 0 0,0 13,14.5V9H16M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22A10,10 0 0,1 2,12A10,10 0 0,1 12,2Z" /></svg>"""

        return web.Response(text=svg, content_type='image/svg+xml')

    def web_page(self, req):
        file_name = os.path.join(os.path.dirname(__file__), 'musicadmin.html')
        with open(file_name, 'r') as ifp:
            return web.Response(text=ifp.read(), content_type='text/html')


class Card:
    id: str
    title: Optional[str]
    description: Optional[str]
    art: Optional[str]
    content_type: Optional[str]
    content_id: Optional[int]
    shuffle: bool

    def __init__(self, id):
        self.id = id
        self.title = None
        self.description = None
        self.art = None
        self.content_type = None
        self.content_id = None
        self.shuffle = False

    def as_dict(self) -> str:
        return {
            "title": self.title,
            "description": self.description,
            "art": self.art,
            "content_type": self.content_type,
            "content_id": self.content_id,
            "shuffle": self.shuffle,
        }

    def json(self) -> str:
        return json.dumps(self.as_dict())

    @classmethod
    def load(cls, id: str, text: str) -> "Card":
        d = json.loads(text)
        card = Card(id)
        card.update_from_json(text)
        return card

    def update_from_json(self, text: str) -> None:
        self.update(**json.loads(text))

    def update(self, **data) -> None:
        for k in ("title", "description", "art", "content_type", "content_id", "shuffle"):
            if k in data:
                setattr(self, k, data[k])

    def embed_url(self) -> str:
        if not self.content_id:
            return ''

        parts = self.content_id.split('/')
        return '/'.join(parts[0:3] + ['embed'] + parts[3:])


class Cards:
    def __init__(self):
        self.file = os.path.join(os.path.dirname(__file__), 'music.db')

    @contextlib.contextmanager
    def cursor(self):
        with contextlib.closing(sqlite3.connect(self.file)) as conn:
            with conn:
                with contextlib.closing(conn.cursor()) as cursor:
                    cursor.execute("CREATE TABLE IF NOT EXISTS card (id VARCHAR(32) PRIMARY KEY, metadata TEXT)")
                with contextlib.closing(conn.cursor()) as cursor:
                    yield cursor

    def list(self):
        with self.cursor() as cur:
            cur.execute("SELECT id, metadata FROM card ORDER BY id")
            for row in cur.fetchall():
                yield Card.load(*row)

    def get(self, card_id):
        with self.cursor() as cur:
            cur.execute("SELECT id, metadata FROM card WHERE id=?", (card_id,))
            row = cur.fetchone()
            if row:
                return Card.load(*row)
            else:
                return Card(card_id)

    def store(self, card:Card) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO card (id, metadata) VALUES (?, ?)",
                (card.id, card.json())
            )

    def drop(self, card:Card) -> None:
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM card WHERE id = ?",
                (card.id,)
            )
