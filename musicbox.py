import appdaemon.plugins.hass.hassapi as hass
import json
import os
from typing import Optional, Dict, List
import contextlib

import sqlite3

class Card():
    id: str
    title: Optional[str]
    content_type: Optional[str]
    content_id: Optional[int]
    shuffle: bool

    def __init__(self, id):
        self.id = id
        self.title = None
        self.content_type = None
        self.content_id = None
        self.shuffle = False

    def as_dict(self) -> str:
        return {
            "title": self.title,
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
        if "playlist" in data:
            self.content_id = data["playlist"]
            self.content_type = "sonos.playlist"

        for k in ("title", "content_type", "content_id", "shuffle"):
            if k in data:
                setattr(self, k, data[k])


class Cards():
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
            cur.execute("SELECT id, metadata FROM card")
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

class Player:
    app: hass.Hass
    player_entity: str

    def __init__(self, app: hass.Hass, player_entity: str):
        self.app = app
        self.player_entity = player_entity

    def log(self, message: str):
        self.app.log(message)

    def get_state(self, *args, **kwargs):
        result = self.app.get_state(*args, **kwargs)
        self.log(f"Get state {args} {kwargs} -> {result}")
        return result
        
    def call(self, *args, **kwargs):
        self.log(f"Call: {args} {kwargs}")
        return self.app.call_service(*args, **kwargs)

    def prepare(self):
        pass
    
    def get_media_content_id(self, card: Card) -> str:
        return card.content_id

    def get_media_content_type(self, card: Card) -> str:
        return card.content_type

    def play(self, card: Card):
        self.call(
            'media_player/shuffle_set',
            entity_id=self.player_entity,
            shuffle=card.shuffle,
        )
        self.call(
            'media_player/play_media',
            entity_id=self.player_entity,
            media_content_id=self.get_media_content_id(card),
            media_content_type=self.get_media_content_type(card),
        )

    def pause(self):
        self.call(
            'media_player/media_pause',
            entity_id=self.player_entity,
        )

    def resume(self):
        self.call(
            'media_player/media_play',
            entity_id=self.player_entity,
        )

    def get_volume(self) -> float:
        return self.get_state(self.player_entity, 'volume_level')

    def set_volume(self, volume: float):
        self.call(
            'media_player/volume_set',
            entity_id=self.player_entity,
            volume_level=volume,
        )


class SpotifyPlayer(Player):
    player_source: str

    def __init__(self, app: hass.Hass, player_entity: str, player_source: str):
        super().__init__(app, player_entity)
        self.player_source = player_source
    
    def prepare(self):
        current_source = self.get_state(self.player_entity, 'source')
        self.app.log(f"Current source {current_source} desired source {self.player_source}")
        if current_source != self.player_source:
            self.log(f"Changing source to {self.player_source}")
            self.call(
                'media_player/select_source',
                entity_id=self.player_entity,
                source=self.player_source,
            )

    def get_media_content_type(self, card: Card) -> str:
        return "playlist"


class SonosPlayer(Player):
    player_group: List[str]

    def __init__(self, app: hass.Hass, player_entity: str, player_group: List[str]):
        super().__init__(app, player_entity)
        self.player_group = player_group

    def players(self) -> List[str]:
        result = [self.player_entity]
        if self.player_group:
            result += self.player_group
        return result

    def prepare(self):
        current_group = self.get_state(self.player_entity, 'group_members')
        if self.player_group:
            if not current_group or set(current_group) != set([self.player_entity] + self.player_group):
                self.log(f"Grouping SONOS speakers...")
                self.call(
                    'media_player/join',
                    entity_id=self.player_entity,
                    group_members=self.player_group,
                )
        else:
            # disabled because inconvenient
            pass
            #if not current_group or len(current_group) > 0:
            #    self.call(
            #        'sonos/unjoin',
            #        entity_id=self.player_entity,
            #    )


    def get_media_content_type(self, card: Card) -> str:
        return "music"

    def get_volume(self) -> float:
        volume = .0
        
        for player in self.players():
            player_volume = self.get_state(player, 'volume_level')
            if player_volume is not None:
                volume = max(volume, player_volume)

        return volume

    def set_volume(self, volume: float):
        for player in self.players():
            self.call(
                'media_player/volume_set',
                entity_id=player,
                volume_level=volume,
            )


class App(hass.Hass):
    cards: Cards
    player: Player

    def initialize(self):
        self.log(f"Initializing '{self.name}' with args {self.args}")

        self.tag_id_entity = self.args['tag_id_entity']
        self.player_type = self.args.get('player_type', 'default')
        self.player_entity = self.args['player_entity']

        if self.player_type == "sonos":
            self.player = SonosPlayer(self, self.player_entity, self.args.get('player_group'))
        elif self.player_type == "spotify":
            self.player = SpotifyPlayer(self, self.player_entity, self.args.get('player_source'))
        else:
            self.player = Player(self, self.player_entity)

        self.current = None
        self.playing = None

        self.cards = Cards()

        self.listen_state(self.on_event, self.tag_id_entity)
        self.log(f"Listening '{self.tag_id_entity}' for tag changes...")


    def on_event(self, entity, attribute, old, new, kwargs):
        self.log(f"Tag changed to: '{new}'")
        if not new or new == "":
            self.gone()
        elif new == "unavailable":
            pass
        else:
            if self.current != new:
                self.appeared(new)

    def appeared(self, tag_id):
        self.current = tag_id

        card = self.cards.get(tag_id)
        self.log(f"Card '{card.as_dict()}' found for '{tag_id}'!")

        if not self.playing or self.playing != card.content_id:
            # play something new
            self.playing = None
            if self.play(card):
                self.playing = card.content_id
        else:
            # unpause
            self.log("Resume Playing")
            self.player.prepare()
            self.ensure_volume()
            self.player.resume()

    def gone(self):
        self.log('Tag is gone!')
        self.current = None
        self.player.pause()

    def play(self, card) -> bool:
        content_id = card.content_id
        if not content_id or content_id == "":
            return False

        self.log('Play: {}'.format(content_id))
        
        self.player.prepare()
        self.ensure_volume()
        self.player.play(card)

        return True

    def ensure_volume(self):
        volume_default = self.args.get('volume_default')
        volume_max = self.args.get('volume_max')
        volume_min = self.args.get('volume_min')
        volume = self.player.get_volume()
        volume_new = None
        
        if volume_default and volume_min and volume_max:
            if volume > volume_max or volume < volume_min:
                volume_new = volume_default
        elif volume_default and not volume_min and not volume_max:
            volume_new = volume_default
        else:
            if volume_max and volume > volume_max:
                volume_new = volume_max
            
            if volume_min and volume < volume_min:
                volume_new = volume_min
        
        if volume_new and volume_new != volume:
            self.player.set_volume(volume_new)

