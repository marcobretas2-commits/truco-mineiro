from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import random
import string
import asyncio

from auth import (
    criar_usuario,
    autenticar_usuario,
    gerar_codigo_recuperacao,
    redefinir_senha
)

app = FastAPI(title="Truco Mineiro Online")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")

# -------------------------
# REGRAS DO TRUCO MINEIRO
# -------------------------

SUITS = ["paus", "copas", "espadas", "ouros"]
RANKS = ["4", "5", "6", "7", "Q", "J", "K", "A", "2", "3"]

# Ordem crescente normal. As manilhas ficam acima do 3.
NORMAL_STRENGTH = {
    "4": 1, "5": 2, "6": 3, "7": 4,
    "Q": 5, "J": 6, "K": 7, "A": 8, "2": 9, "3": 10
}

# Truco Mineiro tradicional: manilhas fixas.
MANILHAS = {
    ("4", "paus"): 100,
    ("7", "copas"): 101,
    ("A", "espadas"): 102,
    ("7", "ouros"): 103,
}

VALUES = [3, 6, 9, 12]


@dataclass
class Card:
    rank: str
    suit: str

    @property
    def strength(self):
        return MANILHAS.get((self.rank, self.suit), NORMAL_STRENGTH[self.rank])

    def json(self):
        return {"rank": self.rank, "suit": self.suit, "strength": self.strength}


@dataclass
class Player:
    sid: str
    name: str
    seat: int
    bot: bool = False
    hand: List[Card] = field(default_factory=list)
    ws: Optional[WebSocket] = None

    @property
    def team(self):
        return self.seat % 2


@dataclass
class Room:
    code: str
    players: Dict[int, Player] = field(default_factory=dict)
    sockets: Dict[str, WebSocket] = field(default_factory=dict)
    scores: List[int] = field(default_factory=lambda: [0, 0])

    # MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o atual
    deck: List[Card] = field(default_factory=list)
    table: List[dict] = field(default_factory=list)
    last_trick: List[dict] = field(default_factory=list)
    current_player: int = 0

    # Valor atual da mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o
    hand_value: int = 1

    # Estados: waiting, playing, raise, finished
    hand_stage: str = "waiting"

    # Vazadas
    tricks: List[int] = field(default_factory=lambda: [0, 0])
    trick_winner: Optional[int] = None
    round_number: int = 0
    first_trick_winner: Optional[int] = None
    trick_history: List[Optional[int]] = field(default_factory=list)

    # Dealer
    dealer: int = 0

    # Controle do Truco
    pending_raise: Optional[int] = None
    pending_team: Optional[int] = None
    pending_raiser: Optional[int] = None
    truco_usado_na_vaza: bool = False

    # MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O DE 11
    mao11_ativa: bool = False
    mao11_team: Optional[int] = None
    mao11_decisoes: Dict[int, Optional[bool]] = field(default_factory=dict)
    mao11_especial: bool = False

    # Resultado
    hand_winner_team: Optional[int] = None
    winner_message: str = ""

    def human_count(self):
        return sum(1 for p in self.players.values() if not p.bot)

    def ordered_players(self):
        return [self.players[i] for i in sorted(self.players)]

    def make_deck(self):
        deck = [Card(r, s) for s in SUITS for r in RANKS]
        random.shuffle(deck)
        return deck

    def fill_bots(self):
        for seat in range(4):
            if seat not in self.players:
                sid = f"BOT-{self.code}-{seat}"
                self.players[seat] = Player(
                    sid=sid,
                    name=f"IA {seat + 1}",
                    seat=seat,
                    bot=True
                )

    def deal(self):
        self.deck = self.make_deck()

        for p in self.players.values():
            p.hand = [self.deck.pop() for _ in range(3)]

    def start_hand(self, special=False):
        if len(self.players) < 4:
            return False

        self.dealer = (self.dealer + 1) % 4

        # Toda nova mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o recebe cartas novas e embaralhadas.
        self.deal()

        self.truco_usado_na_vaza = False
        self.table = []
        self.tricks = [0, 0]
        self.round_number = 0
        self.first_trick_winner = None
        self.trick_history = []

        # MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o normal vale 2.
        # MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11 vale 3.
        self.mao11_especial = bool(special)
        self.hand_value = 3 if self.mao11_especial else 1

        # ?ltima dupla que aumentou o Truco.
        # A pr?xima s? pode ser a dupla advers?ria.
        self.last_raise_team = None
        self.hand_stage = "playing"

        self.mao11_ativa = False
        self.mao11_team = None
        self.mao11_decisoes = {}

        self.pending_raise = None
        self.pending_team = None
        self.pending_raiser = None

        self.hand_winner_team = None
        self.winner_message = ""

        # Jogador ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â  esquerda do dealer comeÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§a.
        self.current_player = (self.dealer + 1) % 4

        print(
            f"[NOVA MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O] Dealer: "
            f"{self.players.get(self.dealer).name if self.players.get(self.dealer) else self.dealer} | "
            f"Valor: {self.hand_value} | "
            f"MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11: {'SIM' if self.mao11_especial else 'NÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O'}"
        )

        return True

    def preparar_mao11(self, team):
        """Coloca a prÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³xima mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o em decisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11."""
        if self.scores[team] >= 12:
            self.hand_stage = "finished"
            return

        if self.scores[team] != 11:
            return

        self.mao11_ativa = True
        self.mao11_team = team

        self.mao11_decisoes = {
            seat: None
            for seat, jogador in self.players.items()
            if jogador.team == team
        }

        self.mao11_especial = False
        self.hand_stage = "mao11"

        self.winner_message = (
            f"MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O DE 11! Dupla {team + 1} chegou a 11 pontos. "
            f"Os dois parceiros precisam decidir: JOGAR ou CORRER."
        )

        print(
            f"[MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O 11] Dupla {team + 1} chegou a 11. "
            f"DecisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âµes pendentes: {self.mao11_decisoes}"
        )

    def finalizar_mao(self, team, pontos, mensagem):
        """Finaliza uma mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o e verifica se alguÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m chegou a 11 ou 12."""
        self.hand_winner_team = team
        self.scores[team] += pontos
        self.winner_message = mensagem

        self.pending_raise = None
        self.pending_team = None
        self.pending_raiser = None

        if self.scores[team] >= 12:
            self.hand_stage = "finished"
            print(
                f"[FIM] Dupla {team + 1} venceu. "
                f"PontuaÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o: {self.scores[0]} x {self.scores[1]}"
            )
            return

        if self.scores[team] == 11:
            self.preparar_mao11(team)
            return

        self.hand_stage = "finished"
    def card_wins(self, cards):
        if not cards:
            return None

        return max(
            cards,
            key=lambda x: x["card"].strength
        )

    def play_card(self, seat, card_index):
        if self.hand_stage != "playing":
            return {
                "ok": False,
                "error": "A mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o estÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ em andamento."
            }

        # Somente o jogador da vez pode jogar.
        if seat != self.current_player:
            return {
                "ok": False,
                "error": "Ainda nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© sua vez."
            }

        p = self.players.get(seat)

        if not p:
            return {
                "ok": False,
                "error": "Jogador nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o encontrado."
            }

        # Um jogador nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o pode jogar duas vezes na mesma vaza.
        if any(item["seat"] == seat for item in self.table):
            return {
                "ok": False,
                "error": "VocÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âª jÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ jogou nesta vaza."
            }

        if card_index < 0 or card_index >= len(p.hand):
            return {
                "ok": False,
                "error": "Carta invÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡lida."
            }

        # Cada jogador joga exatamente uma carta por vaza.
        card = p.hand.pop(card_index)

        self.table.append({
            "seat": seat,
            "card": card
        })

        print(
            f"[CARTA] {p.name} jogou "
            f"{card.rank} de {card.suit} | "
            f"Vaza {self.round_number + 1}/3 | "
            f"Carta {len(self.table)}/4"
        )

        # Ainda faltam jogadores para completar a vaza.
        if len(self.table) < 4:
            self.current_player = (seat + 1) % 4
            return {"ok": True}

        primeiro_da_vaza = self.table[0]["seat"]

        # =========================================================
        # AS 4 CARTAS DA VAZA FORAM JOGADAS
        # =========================================================

        self.round_number += 1

        maior_forca = max(
            item["card"].strength
            for item in self.table
        )

        melhores = [
            item for item in self.table
            if item["card"].strength == maior_forca
        ]

        # Empate na carta mais forte.
        if len(melhores) > 1:
            winner = None
            team = None

            print(
                f"[VAZA] Vaza {self.round_number} terminou EMPATADA."
            )
        else:
            winner = melhores[0]["seat"]
            team = self.players[winner].team

            self.tricks[team] += 1
            self.trick_winner = winner

            # Guarda o vencedor da primeira vaza.
            if self.round_number == 1:
                self.first_trick_winner = winner

            print(
                f"[VAZA] Terminou: vaza {self.round_number} | "
                f"Vencedor: {self.players[winner].name} | "
                f"Dupla: {team + 1} | "
                f"Vazas: {self.tricks[0]} x {self.tricks[1]}"
            )

        # =========================================================
        self.trick_history.append(team)
        # UMA DUPLA GANHOU DUAS VAZAS
        # =========================================================

        if team is not None and self.tricks[team] >= 2:
            pontos = self.hand_value

            mensagem = (
                f"Dupla {team + 1} ganhou a mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o "
                f"com 2 vazas, por {pontos} ponto(s)."
            )

            self.finalizar_mao(
                team,
                pontos,
                mensagem
            )

            return {"ok": True}

        # =========================================================
        # FIM DA TERCEIRA VAZA
        # =========================================================

        if self.round_number >= 3:

            # Se houve empate nas trÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âªs vazas, ninguÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m vence a mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o.
            if self.tricks[0] == 0 and self.tricks[1] == 0:
                pontos = self.hand_value

                self.hand_winner_team = None
                self.winner_message = (
                    f"As trÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âªs vazas terminaram empatadas. "
                    f"NinguÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m marcou os {pontos} ponto(s)."
                )

                self.pending_raise = None
                self.pending_team = None
                self.pending_raiser = None
                self.hand_stage = "finished"

                print(
                    "[FIM] As trÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âªs vazas terminaram empatadas. "
                    "Nenhuma dupla marcou pontos."
                )

                return {"ok": True}

            # Se uma dupla ganhou uma vaza e a outra tambÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m,
            # o vencedor da primeira vaza decide o desempate.
            if self.tricks[0] == 1 and self.tricks[1] == 1:
                if self.first_trick_winner is not None:
                    team_final = self.players[
                        self.first_trick_winner
                    ].team

                    pontos = self.hand_value

                    mensagem = (
                        f"Dupla {team_final + 1} ganhou a mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o "
                        f"pelo desempate da primeira vaza, "
                        f"por {pontos} ponto(s)."
                    )

                    self.finalizar_mao(
                        team_final,
                        pontos,
                        mensagem
                    )

                    return {"ok": True}

        # =========================================================
        # PRÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã¢â‚¬Å“XIMA VAZA
        # =========================================================

        # A mesa ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© limpa somente depois que os 4 jogadores
        # concluÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­ram a vaza.
        self.last_trick = list(self.table)
        self.table = []

        # Se houve vencedor, ele comeÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§a a prÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³xima vaza.
        if winner is not None:
            self.current_player = winner
            self.trick_winner = winner
        else:
            # Em caso de empate, mantÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m a ordem a partir
            # do jogador que iniciou a vaza.
            self.current_player = primeiro_da_vaza
            self.trick_winner = None

        # Novo pedido de Truco fica liberado na prÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³xima vaza.
        self.truco_usado_na_vaza = False

        print(
            f"[VAZA] PrÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³xima vaza comeÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§a com "
            f"{self.players[self.current_player].name}. "
            f"Truco liberado."
        )

        return {"ok": True}
    def raise_truco(self, seat, value):
        proximos = {
            1: 3,
            3: 6,
            6: 9,
            9: 12
        }

        p = self.players.get(seat)

        if not p:
            return {
                "ok": False,
                "error": "Jogador n?o encontrado."
            }

        # ==========================================
        # NOVO PEDIDO DE TRUCO DURANTE A M?O
        # ==========================================
        if self.hand_stage == "playing":

            if seat != self.current_player:
                return {
                    "ok": False,
                    "error": "S? pode pedir Truco na sua vez."
                }

            proximo_valor = proximos.get(self.hand_value)

            if proximo_valor is None:
                return {
                    "ok": False,
                    "error": "O Truco j? chegou ao valor m?ximo."
                }

            if value != proximo_valor:
                return {
                    "ok": False,
                    "error": f"O pr?ximo aumento deve ser para {proximo_valor}."
                }

            if (
                self.last_raise_team is not None
                and p.team == self.last_raise_team
            ):
                return {
                    "ok": False,
                    "error": (
                        "A outra dupla precisa aumentar o Truco "
                        "antes da sua dupla pedir novamente."
                    )
                }

            self.pending_raise = value
            self.pending_team = p.team
            self.pending_raiser = seat
            self.last_raise_team = p.team
            self.truco_usado_na_vaza = True
            self.hand_stage = "raise"

            print(
                f"[TRUCO] Dupla {p.team + 1} pediu "
                f"{value} pontos."
            )

            return {"ok": True}

        # ==========================================
        # AUMENTO DURANTE UM PEDIDO DE TRUCO
        # ==========================================
        if self.hand_stage == "raise":

            if self.pending_team is None:
                return {
                    "ok": False,
                    "error": "N?o existe pedido de Truco pendente."
                }

            # Somente a dupla advers?ria pode responder/aumentar.
            if p.team == self.pending_team:
                return {
                    "ok": False,
                    "error": "Sua dupla foi quem pediu. Aguarde a advers?ria."
                }

            valor_atual_pedido = self.pending_raise

            proximo_valor = proximos.get(valor_atual_pedido)

            # Se j? chegou em 12, n?o existe novo aumento.
            if proximo_valor is None:
                return {
                    "ok": False,
                    "error": "O Truco j? est? no valor m?ximo."
                }

            if value != proximo_valor:
                return {
                    "ok": False,
                    "error": (
                        f"O pr?ximo aumento deve ser para "
                        f"{proximo_valor}."
                    )
                }

            self.pending_raise = value
            self.pending_team = p.team
            self.pending_raiser = seat
            self.last_raise_team = p.team

            print(
                f"[TRUCO] Dupla {p.team + 1} aumentou "
                f"para {value} pontos."
            )

            return {"ok": True}

        return {
            "ok": False,
            "error": "N?o ? poss?vel pedir Truco agora."
        }


    def answer_raise(self, seat, accept):
        if self.hand_stage != "raise":
            return {
                "ok": False,
                "error": "N?o h? pedido de Truco."
            }

        p = self.players.get(seat)

        if not p:
            return {
                "ok": False,
                "error": "Jogador n?o encontrado."
            }

        if p.team == self.pending_team:
            return {
                "ok": False,
                "error": "A resposta ? da dupla advers?ria."
            }

        raiser = self.pending_raiser
        requested_value = self.pending_raise
        requesting_team = self.pending_team

        if raiser is None or requested_value is None:
            return {
                "ok": False,
                "error": "Pedido de Truco inv?lido."
            }

        # ==========================================
        # CORRER
        # ==========================================
        if not accept:
            winner = requesting_team

            self.scores[winner] += self.hand_value
            self.hand_winner_team = winner

            self.hand_stage = "finished"

            self.winner_message = (
                f"Dupla {winner + 1} pediu "
                f"{requested_value} e a advers?ria correu. "
                f"Valeu {self.hand_value} ponto(s)."
            )

            print(
                f"[TRUCO] Dupla {winner + 1} ganhou "
                f"{self.hand_value} ponto(s) porque a advers?ria correu."
            )

            self.pending_raise = None
            self.pending_team = None
            self.pending_raiser = None

            return {"ok": True}

        # ==========================================
        # ACEITAR
        # ==========================================
        self.hand_value = requested_value

        self.pending_raise = None
        self.pending_team = None
        self.pending_raiser = None

        self.hand_stage = "playing"

        # A m?o continua a partir do jogador seguinte
        # ao ?ltimo jogador que fez o pedido/aumento.
        self.current_player = raiser

        print(
            f"[TRUCO] Pedido de {requested_value} aceito. "
            f"M?o agora vale {self.hand_value}."
        )

        return {"ok": True}


    def answer_raise(self, seat, accept):
        if self.hand_stage != "raise":
            return {
                "ok": False,
                "error": "NÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o hÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ pedido de truco."
            }

        p = self.players.get(seat)

        if not p:
            return {
                "ok": False,
                "error": "Jogador nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o encontrado."
            }

        # Somente a dupla adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ria pode responder
        if p.team == self.pending_team:
            return {
                "ok": False,
                "error": "A resposta ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© da dupla adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ria."
            }

        raiser = self.pending_raiser
        requested_value = self.pending_raise
        requesting_team = self.pending_team

        if raiser is None or requested_value is None:
            return {
                "ok": False,
                "error": "Pedido de truco invÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡lido."
            }

        # ---------------------------------
        # CORREU
        # ---------------------------------

        if not accept:
            winner = requesting_team

            # Se recusou, quem pediu recebe
            # o valor anterior da mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o.
            self.scores[winner] += self.hand_value

            self.hand_winner_team = winner
            self.hand_stage = "finished"

            self.winner_message = (
                f"Dupla {winner + 1} pediu "
                f"{requested_value} e a adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ria correu."
            )

            self.pending_raise = None
            self.pending_team = None
            self.pending_raiser = None

            return {"ok": True}

        # ---------------------------------
        # ACEITOU
        # ---------------------------------

        self.hand_value = requested_value

        self.pending_raise = None
        self.pending_team = None
        self.pending_raiser = None

        self.hand_stage = "playing"

        # IMPORTANTE:
        # Depois que o truco ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© aceito, nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o deixamos
        # o mesmo jogador pedir outro truco imediatamente.
        #
        # A vez passa para o jogador seguinte.
        self.current_player = raiser

        return {"ok": True}

    def responder_mao11(self, seat, jogar):
        if not self.mao11_ativa:
            return {
                "ok": False,
                "error": "NÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o hÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ decisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11."
            }

        p = self.players.get(seat)

        if not p:
            return {
                "ok": False,
                "error": "Jogador nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o encontrado."
            }

        if p.team != self.mao11_team:
            return {
                "ok": False,
                "error": "Essa decisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o pertence ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â  outra dupla."
            }

        if seat not in self.mao11_decisoes:
            return {
                "ok": False,
                "error": "Jogador nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o participa desta decisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o."
            }

        if self.mao11_decisoes[seat] is not None:
            return {
                "ok": False,
                "error": "VocÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âª jÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ decidiu."
            }

        self.mao11_decisoes[seat] = bool(jogar)

        print(
            f"[MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O 11] {p.name}: "
            f"{'JOGA' if jogar else 'CORRE'}"
        )

        # =========================================================
        # UM DOS PARCEIROS CORRE
        # =========================================================
        if not jogar:

            equipe_mao11 = self.mao11_team
            adversaria = 1 - equipe_mao11

            # Correndo na MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11, adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡rio ganha 1 ponto.
            self.scores[adversaria] += 1

            self.hand_winner_team = adversaria

            self.winner_message = (
                f"{p.name} decidiu CORRER na MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11. "
                f"Dupla {adversaria + 1} ganhou 1 ponto."
            )

            print(
                f"[MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O 11] Dupla {equipe_mao11 + 1} CORREU. "
                f"Dupla {adversaria + 1} ganhou 1 ponto."
            )

            self.mao11_ativa = False
            self.mao11_team = None
            self.mao11_decisoes = {}
            self.mao11_especial = False

            # Se o adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡rio chegou a 11, ele tambÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m precisa
            # entrar na decisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11.
            if self.scores[adversaria] >= 12:
                self.hand_stage = "finished"
            elif self.scores[adversaria] == 11:
                self.preparar_mao11(adversaria)
            else:
                self.start_hand()

            return {"ok": True}

        # =========================================================
        # OS DOIS PARCEIROS PRECISAM ESCOLHER JOGAR
        # =========================================================
        decisoes = list(self.mao11_decisoes.values())

        if len(decisoes) == 2 and all(x is True for x in decisoes):

            equipe = self.mao11_team

            print(
                f"[MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O 11] Dupla {equipe + 1} "
                f"decidiu JOGAR. MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o valendo 3."
            )

            # A decisÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o terminou.
            self.mao11_ativa = False
            self.mao11_team = None
            self.mao11_decisoes = {}

            # ComeÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§a uma mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o completamente nova,
            # embaralhando e distribuindo novamente.
            self.start_hand(special=True)

            self.winner_message = (
                f"?? MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o de 11 aceita! "
                f"Dupla {equipe + 1} decidiu JOGAR. "
                f"A mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o vale 3 pontos."
            )

            return {"ok": True}

        # Apenas um parceiro decidiu atÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© agora.
        self.hand_stage = "mao11"

        return {"ok": True}
    def should_raise_bot(self, p):
        if self.hand_stage != "playing":
            return None

        if self.current_player != p.seat:
            return None

        # Se a mesma dupla foi a ?ltima a aumentar,
        # ela precisa esperar a advers?ria aumentar.
        if (
            self.last_raise_team is not None
            and p.team == self.last_raise_team
        ):
            return None

        proximos = {
            1: 3,
            3: 6,
            6: 9,
            9: 12
        }

        proximo = proximos.get(self.hand_value)

        if proximo is None:
            return None

        max_strength = max(
            [c.strength for c in p.hand],
            default=0
        )

        # Cartas muito fortes podem pedir aumento.
        if max_strength >= 100:
            return proximo

        # Cartas fortes podem pedir o primeiro Truco.
        if (
            self.hand_value == 1
            and max_strength >= 10
            and random.random() < 0.35
        ):
            return 3

        return None

    def bot_move(self):
        bots = [
            p for p in self.players.values()
            if p.bot
        ]

        if not bots:
            return

        # =================================
        # RESPOSTA AO TRUCO
        # =================================

        if self.hand_stage == "raise":

            adversarios = [
                p for p in self.players.values()
                if p.bot
                and p.team != self.pending_team
            ]

            if not adversarios:
                print(
                    "[IA] Nenhum bot adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡rio "
                    "encontrado para responder ao TRUCO."
                )
                return

            # Escolhe o bot adversÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡rio com
            # a melhor carta.
            target = max(
                adversarios,
                key=lambda p: max(
                    (c.strength for c in p.hand),
                    default=0
                )
            )

            strength = max(
                (c.strength for c in target.hand),
                default=0
            )

            # IA aceita com carta razoavelmente forte
            accept = strength >= 8

            print(
                f"[IA] {target.name} recebeu TRUCO "
                f"{self.pending_raise} - "
                f"{'ACEITA' if accept else 'CORRE'} "
                f"(forÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§a {strength})"
            )

            resultado = self.answer_raise(
                target.seat,
                accept
            )

            if resultado.get("ok"):
                print(
                    f"[IA] {target.name} respondeu ao TRUCO."
                )
            else:
                print(
                    "[IA] ERRO ao responder TRUCO: "
                    f"{resultado.get('error')}"
                )

            return

        # =================================
        # JOGADA NORMAL DA IA
        # =================================

        p = self.players.get(self.current_player)

        if not p or not p.bot:
            return

        # Primeiro verifica se quer pedir truco
        raise_value = self.should_raise_bot(p)

        if raise_value:
            resultado = self.raise_truco(
                p.seat,
                raise_value
            )

            if resultado.get("ok"):
                print(
                    f"[IA] {p.name} pediu TRUCO "
                    f"{raise_value}!"
                )

            return

        # Se nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o pediu truco, joga carta
        # Nunca joga carta enquanto houver uma resposta ao TRUCO pendente.
        if self.hand_stage != "playing":
            return
        if p.hand:

            idx = max(
                range(len(p.hand)),
                key=lambda i: p.hand[i].strength
            )

            carta = p.hand[idx]

            resultado = self.play_card(
                p.seat,
                idx
            )

            if resultado.get("ok"):
                print(
                    f"[IA] {p.name} jogou "
                    f"{carta.rank} de {carta.suit}"
                )

            return

    def public_state(self, viewer_seat=None):
        players = []

        for seat in range(4):

            p = self.players.get(seat)

            if not p:
                players.append(None)
                continue

            if viewer_seat == seat:
                hand = [
                    c.json()
                    for c in p.hand
                ]
            else:
                hand = [
                    {"hidden": True}
                    for _ in p.hand
                ]

            players.append({
                "seat": p.seat,
                "name": p.name,
                "team": p.team,
                "bot": p.bot,
                "hand": hand
            })

        return {
            "code": self.code,
            "players": players,
            "scores": self.scores,

            "table": [
                {
                    "seat": x["seat"],
                    "card": x["card"].json()
                }
                for x in self.table
            ],

            "last_trick": [
                {
                    "seat": x["seat"],
                    "card": x["card"].json()
                }
                for x in self.last_trick
            ],

            "current_player": self.current_player,

            "hand_value": self.hand_value,

            "stage": self.hand_stage,

            "pending_raise": self.pending_raise,

            "pending_team": self.pending_team,

            "pending_raiser": self.pending_raiser,

            "tricks": self.tricks,

            "winner_message": self.winner_message,

            "round": self.round_number,
        }


rooms: Dict[str, Room] = {}


def make_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in rooms:
            return code


async def broadcast(room: Room):
    dead = []
    for sid, ws in list(room.sockets.items()):
        p = next((x for x in room.players.values() if x.sid == sid), None)
        if not p:
            continue
        try:
            await ws.send_json({"type": "state", "state": room.public_state(p.seat)})
        except Exception:
            dead.append(sid)
    for sid in dead:
        room.sockets.pop(sid, None)


async def bot_loop(room: Room):
    while room.code in rooms:
        await asyncio.sleep(2.5)

        before = (
            room.hand_stage,
            room.current_player,
            len(room.table),
            room.hand_value,
            room.pending_raise,
            tuple(sorted(room.mao11_decisoes.items()))
        )

        # =========================================================
        # MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O DE 11
        # =========================================================
        if room.hand_stage == "mao11" and room.mao11_ativa:

            for seat, decisao in list(room.mao11_decisoes.items()):

                if decisao is None and seat in room.players:

                    jogador = room.players[seat]

                    if jogador.bot:

                        # A IA joga com cartas razoÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡veis/fortes.
                        max_strength = max(
                            [c.strength for c in jogador.hand],
                            default=0
                        )

                        jogar = max_strength >= 7

                        resultado = room.responder_mao11(
                            seat,
                            jogar
                        )

                        if resultado["ok"]:
                            print(
                                f"[IA MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O 11] "
                                f"{jogador.name}: "
                                f"{'JOGA' if jogar else 'CORRE'}"
                            )

                        break

        # =========================================================
        # JOGO NORMAL
        # =========================================================
        elif (
            room.hand_stage == "playing"
            and room.current_player in room.players
        ):

            jogador = room.players[room.current_player]

            if jogador.bot:
                try:
                    room.bot_move()
                except Exception as e:
                    import traceback
                    print(f'[IA] ERRO NO BOT: {e}')
                    traceback.print_exc()

        # =========================================================
        # RESPOSTA AO TRUCO
        # =========================================================
        elif room.hand_stage == "raise":

            # A dupla que recebeu o TRUCO ? a advers?ria
            # da dupla que pediu.
            equipe_responde = 1 - room.pending_team

            # Se houver jogador humano nessa dupla, N?O deixar
            # a IA responder automaticamente.
            humanos = [
                jogador
                for jogador in room.players.values()
                if jogador.team == equipe_responde
                and not jogador.bot
            ]

            if humanos:
                print(
                    f"[TRUCO] Aguardando resposta humana da "
                    f"Dupla {equipe_responde + 1}."
                )
                await asyncio.sleep(0.5)
            else:
                # Somente IA contra IA responde automaticamente.
                await asyncio.sleep(3)

                if room.hand_stage == "raise":
                    room.bot_move()

        after = (
            room.hand_stage,
            room.current_player,
            len(room.table),
            room.hand_value,
            room.pending_raise,
            tuple(sorted(room.mao11_decisoes.items()))
        )

        if before != after:
            await broadcast(room)

        # =========================================================
        # NOVA MÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O
        # =========================================================
        #
        # NÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢O inicia uma mÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o enquanto estiver em "mao11".
        #
        if room.hand_stage == "finished" and max(room.scores) < 12:

            await asyncio.sleep(4)

            # SeguranÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§a para o caso de alguÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©m ter chegado a 11.
            if max(room.scores) == 11:

                if room.scores[0] == 11:
                    team11 = 0
                else:
                    team11 = 1

                room.preparar_mao11(team11)

            else:
                room.start_hand()

            await broadcast(room)
# =========================
# AUTENTICAÇÃO DE USUÁRIOS
# =========================

from pydantic import BaseModel


class CadastroRequest(BaseModel):
    nome: str
    email: str
    senha: str


class LoginRequest(BaseModel):
    email: str
    senha: str


class RecuperacaoRequest(BaseModel):
    email: str


class RedefinirSenhaRequest(BaseModel):
    email: str
    codigo: str
    nova_senha: str


@app.post("/api/cadastro")
async def api_cadastro(dados: CadastroRequest):

    nome = dados.nome.strip()
    email = dados.email.strip().lower()
    senha = dados.senha

    if len(nome) < 2:
        return {
            "ok": False,
            "erro": "Informe um nome válido."
        }

    if "@" not in email or "." not in email:
        return {
            "ok": False,
            "erro": "Informe um e-mail válido."
        }

    if len(senha) < 6:
        return {
            "ok": False,
            "erro": "A senha deve ter pelo menos 6 caracteres."
        }

    resultado = criar_usuario(nome, email, senha)

    return resultado


@app.post("/api/login")
async def api_login(dados: LoginRequest):

    email = dados.email.strip().lower()
    senha = dados.senha

    usuario = autenticar_usuario(email, senha)

    if not usuario:
        return {
            "ok": False,
            "erro": "E-mail ou senha incorretos."
        }

    return {
        "ok": True,
        "usuario": usuario
    }


@app.post("/api/recuperar-senha")
async def api_recuperar_senha(dados: RecuperacaoRequest):

    email = dados.email.strip().lower()

    codigo = gerar_codigo_recuperacao(email)

    if not codigo:
        return {
            "ok": False,
            "erro": "Não encontramos uma conta com esse e-mail."
        }

    # TEMPORÁRIO:
    # futuramente o código será enviado por e-mail.
    return {
        "ok": True,
        "mensagem": "Código de recuperação gerado.",
        "codigo": codigo
    }


@app.post("/api/redefinir-senha")
async def api_redefinir_senha(dados: RedefinirSenhaRequest):

    email = dados.email.strip().lower()
    codigo = dados.codigo.strip()
    nova_senha = dados.nova_senha

    if len(nova_senha) < 6:
        return {
            "ok": False,
            "erro": "A nova senha deve ter pelo menos 6 caracteres."
        }

    resultado = redefinir_senha(
        email,
        codigo,
        nova_senha
    )

    return resultado



@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/sala/{code}", response_class=HTMLResponse)
async def sala(request: Request, code: str):
    code = code.upper()

    # Cria a sala quando o jogador usa "CRIAR PARTIDA".
    if code not in rooms and request.query_params.get("create") == "1":
        rooms[code] = Room(code=code)

    return templates.TemplateResponse(
        request=request,
        name="game.html",
        context={"code": code}
    )


@app.websocket("/ws/{code}")
async def websocket_endpoint(websocket: WebSocket, code: str):
    await websocket.accept()
    code = code.upper()
    room = rooms.get(code)
    if not room:
        await websocket.send_json({"type": "error", "message": "Sala nÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o encontrada."})
        await websocket.close()
        return

    sid = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    try:
        msg = await websocket.receive_json()
        name = str(msg.get("name", "Jogador")).strip()[:20] or "Jogador"
    except Exception:
        await websocket.close()
        return

    free_seat = next((s for s in range(4) if s not in room.players), None)
    if free_seat is None:
        await websocket.send_json({"type": "error", "message": "Sala cheia."})
        await websocket.close()
        return

    p = Player(sid=sid, name=name, seat=free_seat, ws=websocket)
    room.players[free_seat] = p
    room.sockets[sid] = websocket

    await websocket.send_json({
        "type": "joined",
        "seat": free_seat,
        "code": code
    })
    await broadcast(room)

    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")

            if action == "start":
                room.fill_bots()

                if room.start_hand():
                    # Inicia o loop da IA automaticamente.
                    if not hasattr(room, "_bot_task") or room._bot_task.done():
                        room._bot_task = asyncio.create_task(bot_loop(room))

                    await broadcast(room)
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "NÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o foi possÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­vel iniciar a partida."
                    })
            elif action == "play":
                result = room.play_card(p.seat, int(msg.get("index", -1)))
                if not result["ok"]:
                    await websocket.send_json({"type": "error", "message": result["error"]})
                await broadcast(room)

            elif action == "raise":
                result = room.raise_truco(p.seat, int(msg.get("value", 0)))
                if not result["ok"]:
                    await websocket.send_json({"type": "error", "message": result["error"]})
                await broadcast(room)

            elif action == "answer":
                result = room.answer_raise(p.seat, bool(msg.get("accept")))
                if not result["ok"]:
                    await websocket.send_json({"type": "error", "message": result["error"]})
                await broadcast(room)

            elif action == "mao11":
                jogar = bool(msg.get("jogar"))

                result = room.responder_mao11(
                    p.seat,
                    jogar
                )

                if not result["ok"]:
                    await websocket.send_json({
                        "type": "error",
                        "message": result["error"]
                    })

                await broadcast(room)

            elif action == "signal":
                sinal = str(msg.get("signal", "")).strip()[:30]

                sinais_validos = {
                    "manilha",
                    "boa",
                    "ruim",
                    "carta_alta",
                    "carta_media",
                    "carta_baixa",
                    "truco",
                    "segura"
                }

                if sinal not in sinais_validos:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Sinal inválido."
                    })
                    continue

                # O parceiro está sempre 2 posições de distância:
                # 0 <-> 2
                # 1 <-> 3
                parceiro_seat = (p.seat + 2) % 4
                parceiro = room.players.get(parceiro_seat)

                if parceiro and parceiro.ws:
                    await parceiro.ws.send_json({
                        "type": "partner_signal",
                        "from_seat": p.seat,
                        "from_name": p.name,
                        "signal": sinal
                    })

                # Confirma somente para quem enviou.
                await websocket.send_json({
                    "type": "signal_sent",
                    "signal": sinal
                })

            elif action == "chat":
                texto = str(msg.get("message", "")).strip()[:300]

                if not texto:
                    continue

                # Envia a mensagem para todos os jogadores da sala.
                for jogador in room.players.values():
                    if jogador.ws:
                        try:
                            await jogador.ws.send_json({
                                "type": "chat",
                                "from_seat": p.seat,
                                "from_name": p.name,
                                "message": texto
                            })
                        except Exception:
                            pass

            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        room.sockets.pop(sid, None)
        # NÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o removemos imediatamente a cadeira: isso permite reconexÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o futura
        # nesta primeira versÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£o.
        await broadcast(room)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)






































