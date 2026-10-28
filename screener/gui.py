"""A menu screen for the screener, in the style of the drift-racer menus.

Arrow keys move, ENTER confirms, ESC goes back. Screens run as a small state machine:

  TITLE -> RATIO LIST                                       (ENTER)
  TITLE -> CATEGORY -> COUNT -> PRICE -> RESULTS            (Y, the stock screens)
  TITLE -> STOCKS -> PERIOD -> CASH -> SETTINGS -> RESULT   (B, backtest)
  TITLE -> CASH -> WHO PICKS -> STOCKS -> HOLDINGS -> PLAN  (P, portfolio planner)
  TITLE -> REFRESH                                          (R, read MarketBeat again)

Every number comes from MarketBeat, through data/screener.db. The browsing screens re-slice
what is stored and respond immediately; the refresh, the planner and a stock's detail view
read MarketBeat when what is stored is out of date.
"""
import time
import webbrowser

import pygame

from . import db, explore, mb_ratio

SCREEN_W, SCREEN_H = 1280, 760
BG = (16, 16, 22)
CARD = (24, 24, 30)
CARD_SEL = (34, 34, 46)
TEXT = (238, 238, 242)
DIM = (150, 150, 160)
ACCENT = (255, 186, 60)
GREEN = (90, 210, 130)
RED = (235, 95, 95)
BLUE = (90, 165, 245)

CATEGORY_CARDS = [
    ("stocks", "STOCKS", "Individual companies", "Ranked on analyst consensus,\nmomentum and risk", BLUE),
    ("etfs", "ETFs", "Funds", "No analyst coverage exists,\nso momentum and liquidity", GREEN),
    ("penny", "PENNY", "Under $5", "Speculative, thin,\nmostly uncovered", ACCENT),
    ("all", "EVERYTHING", "All of it", "One combined ranking\nacross every type", (200, 140, 240)),
]

COUNT_CHOICES = [5, 10, 15, 20, 30, 50, 100]

PRICE_CARDS = [
    ((0.0, None), "ANY", "no limit"),
    ((0.0, 5.0), "UNDER $5", "penny"),
    ((5.0, 75.0), "$5 - $75", "mid"),
    ((75.0, 500.0), "$75 - $500", "large"),
    ((500.0, None), "OVER $500", "premium"),
    (None, "CUSTOM", "type your own range"),
]


class Fonts:
    def __init__(self):
        name = "Segoe UI,Arial,DejaVu Sans"
        self.xl = pygame.font.SysFont(name, 64, bold=True)
        self.lg = pygame.font.SysFont(name, 38, bold=True)
        self.md = pygame.font.SysFont(name, 22)
        self.sm = pygame.font.SysFont(name, 17)
        self.xs = pygame.font.SysFont(name, 14)
        self.mono = pygame.font.SysFont("Consolas,DejaVu Sans Mono", 16)
        self.mono_b = pygame.font.SysFont("Consolas,DejaVu Sans Mono", 16, bold=True)


class App:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Stock screener — MarketBeat")
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        self.clock = pygame.time.Clock()
        self.font = Fonts()
        self.running = True

        with db.connect() as con:
            self.bulk, self.prices, self.detail = explore.load(con)

        self.category = "stocks"
        self.count = 20
        self.band = (0.0, None)
        self.rows: list[dict] = []
        self.scroll = 0
        self.selected = 0
        self._benchmark = None
        self._http = None
        self.bt: dict = {}
        self.plan: dict = {}

    def data_age(self) -> str:
        """How old the stored data is, so the title screen never implies it is live."""
        try:
            from .config import DB_PATH
            age = time.time() - DB_PATH.stat().st_mtime
        except Exception:
            return ""
        if age < 120:
            return "moments ago"
        if age < 3600:
            return f"{age / 60:.0f} minutes ago"
        if age < 86400:
            return f"{age / 3600:.0f} hours ago"
        return f"{age / 86400:.0f} days ago"

    def benchmark(self):
        """S&P 500 (SPY) closes from MarketBeat, for relative-strength comparisons."""
        if self._benchmark is None:
            try:
                from . import prices
                closes = prices.history("SPY", "NYSEARCA")
                self._benchmark = closes if len(closes) else False
            except Exception:
                self._benchmark = False
        return self._benchmark if self._benchmark is not False else None

    # ---------------------------------------------------------------- drawing helpers

    def text(self, s, font, colour, x, y, centre=False, right=False):
        surf = font.render(s, True, colour)
        rect = surf.get_rect()
        if centre:
            rect.midtop = (x, y)
        elif right:
            rect.topright = (x, y)
        else:
            rect.topleft = (x, y)
        self.screen.blit(surf, rect)
        return rect

    def card(self, x, y, w, h, selected):
        pygame.draw.rect(self.screen, CARD_SEL if selected else CARD, (x, y, w, h), border_radius=12)
        if selected:
            pygame.draw.rect(self.screen, ACCENT, (x, y, w, h), width=3, border_radius=12)

    def footer(self, hint):
        self.text(hint, self.font.sm, DIM, SCREEN_W // 2, SCREEN_H - 38, centre=True)

    # ---------------------------------------------------------------- the ranking

    def compute(self):
        lo, hi = self.band
        tickers = explore.category_rows(self.category, self.bulk, self.prices, lo, hi)
        self.rows = explore.rank(self.category, tickers, self.bulk, self.prices,
                                 self.detail, self.count)
        self.scroll = 0
        self.selected = 0


class Scene:
    def key(self, event, app) -> "Scene | None":
        return None

    def tick(self, app) -> "Scene | None":
        """Called every frame; a scene waiting on background work returns its successor."""
        return None

    def draw(self, app):
        pass


class TitleScene(Scene):
    def key(self, event, app):
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            return RatioScene(self)
        if event.key == pygame.K_y:
            if not app.bulk:          # nothing stored yet: collect it first
                scene = RefreshScene(self)
                scene.begin(app)
                return scene
            return CategoryScene()
        if event.key == pygame.K_b:
            return backtest_start(app)
        if event.key == pygame.K_p:
            return planner_start(app)
        if event.key == pygame.K_m:
            return portfolio_start(app)
        if event.key == pygame.K_r:
            scene = RefreshScene(self)
            scene.begin(app)
            return scene
        if event.key == pygame.K_ESCAPE:
            app.running = False
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("STOCK SCREENER", app.font.xl, ACCENT, SCREEN_W // 2, 150, centre=True)
        loaded = (f"{len(app.bulk):,} stocks  ·  {len(app.prices):,} priced  ·  "
                  f"{len(app.detail):,} full profiles  ·  all from MarketBeat")
        app.text(loaded, app.font.md, DIM, SCREEN_W // 2, 240, centre=True)
        options = [
            ("ENTER", "most buys, fewest holds, no sells on MarketBeat", ACCENT),
            ("Y", "stock screens: stocks, ETFs, penny, everything", TEXT),
            ("B", "backtest the strategy on stored history", TEXT),
            ("P", "portfolio planner: what to buy with your cash", TEXT),
            ("M", "my portfolio: what you paid, what it's worth, sell limits", TEXT),
            ("R", "refresh: read MarketBeat again", DIM),
        ]
        for i, (key, label, colour) in enumerate(options):
            y = 296 + i * 38
            app.text(key, app.font.md, colour, SCREEN_W // 2 - 250, y, right=True)
            app.text(label, app.font.md, colour, SCREEN_W // 2 - 220, y)
        app.text("Arrows move     ENTER confirms     ESC back", app.font.sm, DIM,
                 SCREEN_W // 2, 540, centre=True)
        stamp = app.data_age()
        app.text(f"Showing data collected {stamp}." if stamp else
                 "No data yet — press R to collect it.", app.font.sm, DIM,
                 SCREEN_W // 2, 590, centre=True)


class CategoryScene(Scene):
    def __init__(self, index=0):
        self.index = index

    def key(self, event, app):
        if event.key in (pygame.K_LEFT, pygame.K_a):
            self.index = (self.index - 1) % len(CATEGORY_CARDS)
        elif event.key in (pygame.K_RIGHT, pygame.K_d):
            self.index = (self.index + 1) % len(CATEGORY_CARDS)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            app.category = CATEGORY_CARDS[self.index][0]
            return CountScene()
        elif event.key == pygame.K_ESCAPE:
            return TitleScene()
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("WHAT KIND OF STOCKS?", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("step 1 of 3", app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)

        gap, margin = 20, 60
        n = len(CATEGORY_CARDS)
        w = (SCREEN_W - 2 * margin - (n - 1) * gap) // n
        h, y0 = 340, 180
        for i, (_, title, sub, body, colour) in enumerate(CATEGORY_CARDS):
            x = margin + i * (w + gap)
            app.card(x, y0, w, h, i == self.index)
            pygame.draw.rect(app.screen, colour, (x, y0, w, 6),
                             border_top_left_radius=12, border_top_right_radius=12)
            app.text(title, app.font.lg, colour if i == self.index else TEXT,
                     x + w // 2, y0 + 40, centre=True)
            app.text(sub, app.font.sm, DIM, x + w // 2, y0 + 92, centre=True)
            for k, line in enumerate(body.split("\n")):
                app.text(line, app.font.sm, TEXT, x + w // 2, y0 + 150 + k * 26, centre=True)
        app.footer("LEFT / RIGHT choose     ENTER next     ESC back")


class CountScene(Scene):
    def __init__(self):
        self.index = COUNT_CHOICES.index(20)

    def key(self, event, app):
        if event.key in (pygame.K_LEFT, pygame.K_a):
            self.index = max(0, self.index - 1)
        elif event.key in (pygame.K_RIGHT, pygame.K_d):
            self.index = min(len(COUNT_CHOICES) - 1, self.index + 1)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            app.count = COUNT_CHOICES[self.index]
            return PriceScene()
        elif event.key == pygame.K_ESCAPE:
            return CategoryScene()
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("HOW MANY?", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("step 2 of 3", app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)

        app.text(str(COUNT_CHOICES[self.index]), app.font.xl, TEXT, SCREEN_W // 2, 210, centre=True)
        app.text("stocks in the list", app.font.md, DIM, SCREEN_W // 2, 300, centre=True)

        # a row of pips, the selected one filled
        total_w = len(COUNT_CHOICES) * 90
        x0 = (SCREEN_W - total_w) // 2
        for i, value in enumerate(COUNT_CHOICES):
            x = x0 + i * 90
            selected = i == self.index
            app.card(x, 390, 76, 64, selected)
            app.text(str(value), app.font.md, ACCENT if selected else DIM,
                     x + 38, 408, centre=True)
        app.footer("LEFT / RIGHT choose     ENTER next     ESC back")


class PriceScene(Scene):
    def __init__(self):
        self.index = 0

    def key(self, event, app):
        cols = 3
        if event.key in (pygame.K_LEFT, pygame.K_a):
            self.index = (self.index - 1) % len(PRICE_CARDS)
        elif event.key in (pygame.K_RIGHT, pygame.K_d):
            self.index = (self.index + 1) % len(PRICE_CARDS)
        elif event.key in (pygame.K_UP, pygame.K_w):
            self.index = (self.index - cols) % len(PRICE_CARDS)
        elif event.key in (pygame.K_DOWN, pygame.K_s):
            self.index = (self.index + cols) % len(PRICE_CARDS)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            band = PRICE_CARDS[self.index][0]
            if band is None:
                return CustomPriceScene()
            app.band = band
            app.compute()
            return ResultsScene()
        elif event.key == pygame.K_ESCAPE:
            return CountScene()
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("WHAT SHOULD THEY COST?", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("step 3 of 3  ·  share price, in each listing's own currency",
                 app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)

        cols, gap, margin = 3, 22, 160
        w = (SCREEN_W - 2 * margin - (cols - 1) * gap) // cols
        h = 130
        for i, (_, title, note) in enumerate(PRICE_CARDS):
            x = margin + (i % cols) * (w + gap)
            y = 190 + (i // cols) * (h + gap)
            app.card(x, y, w, h, i == self.index)
            app.text(title, app.font.lg, ACCENT if i == self.index else TEXT,
                     x + w // 2, y + 28, centre=True)
            app.text(note, app.font.sm, DIM, x + w // 2, y + 86, centre=True)
        app.footer("ARROWS choose     ENTER run     ESC back")


class CustomPriceScene(Scene):
    """Type a minimum and maximum. The match count updates as you type, so an empty
    range is obvious before running it."""

    def __init__(self):
        self.fields = ["", ""]
        self.active = 0
        self.matches: int | None = None
        self.error = ""

    def band(self) -> tuple[float, float | None] | None:
        try:
            lo = float(self.fields[0]) if self.fields[0] else 0.0
            hi = float(self.fields[1]) if self.fields[1] else None
        except ValueError:
            return None
        if hi is not None and hi < lo:
            return None
        return lo, hi

    def recount(self, app):
        band = self.band()
        if band is None:
            self.matches, self.error = None, "maximum must be above minimum"
            return
        self.error = ""
        self.matches = len(explore.category_rows(app.category, app.bulk, app.prices,
                                                 band[0], band[1]))

    def key(self, event, app):
        if event.key == pygame.K_ESCAPE:
            return PriceScene()
        if event.key in (pygame.K_TAB, pygame.K_DOWN, pygame.K_UP):
            self.active = 1 - self.active
            return None
        if event.key == pygame.K_BACKSPACE:
            self.fields[self.active] = self.fields[self.active][:-1]
            self.recount(app)
            return None
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            band = self.band()
            if band is None:
                return None
            app.band = band
            app.compute()
            return ResultsScene()
        char = event.unicode
        if char and (char.isdigit() or (char == "." and "." not in self.fields[self.active])):
            if len(self.fields[self.active]) < 9:
                self.fields[self.active] += char
                self.recount(app)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("CUSTOM PRICE RANGE", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("type the numbers — leave the maximum blank for no cap",
                 app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)

        box_w, box_h, gap = 360, 110, 60
        x0 = (SCREEN_W - (box_w * 2 + gap)) // 2
        y = 220
        for i, label in enumerate(("MINIMUM $", "MAXIMUM $")):
            x = x0 + i * (box_w + gap)
            active = i == self.active
            app.card(x, y, box_w, box_h, active)
            app.text(label, app.font.sm, DIM, x + 20, y + 14)
            shown = self.fields[i] or ("0" if i == 0 else "none")
            colour = TEXT if self.fields[i] else DIM
            app.text(shown, app.font.lg, colour, x + 20, y + 44)
            if active:
                caret_x = x + 24 + app.font.lg.size(self.fields[i])[0]
                if (pygame.time.get_ticks() // 450) % 2 == 0:
                    pygame.draw.rect(app.screen, ACCENT, (caret_x, y + 50, 3, 34))

        if self.error:
            app.text(self.error, app.font.md, RED, SCREEN_W // 2, 380, centre=True)
        elif self.matches is not None:
            app.text(f"{self.matches:,} {app.category} in that range",
                     app.font.md, GREEN if self.matches else RED, SCREEN_W // 2, 380, centre=True)
            if not self.matches:
                app.text("nothing matches — widen the range", app.font.sm, DIM,
                         SCREEN_W // 2, 416, centre=True)

        app.footer("TYPE digits     TAB switch field     ENTER run     ESC back")


class ResultsScene(Scene):
    ROWS_VISIBLE = 16

    def key(self, event, app):
        if event.key in (pygame.K_DOWN, pygame.K_s):
            app.selected = min(len(app.rows) - 1, app.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w):
            app.selected = max(0, app.selected - 1)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and app.rows:
            scene = DetailScene(app.rows[app.selected], self)
            scene.load(app)
            return scene
        elif event.key == pygame.K_o and app.rows:
            row = app.rows[app.selected]
            stored = (app.bulk.get(row["ticker"], {}) or {}).get("url")
            webbrowser.open(stored or f"https://www.marketbeat.com/stocks/"
                                      f"{row.get('exchange') or 'NASDAQ'}/{row['ticker']}/")
        elif event.key == pygame.K_m and app.rows:
            scene = MarketBeatScene(self, app.rows)
            scene.begin(app)
            return scene
        elif event.key == pygame.K_ESCAPE:
            return PriceScene()
        elif event.key == pygame.K_r:
            return CategoryScene()
        # keep the selection on screen
        app.scroll = min(app.scroll, app.selected)
        app.scroll = max(app.scroll, app.selected - self.ROWS_VISIBLE + 1)
        app.scroll = max(0, min(app.scroll, max(0, len(app.rows) - self.ROWS_VISIBLE)))
        return None

    def draw(self, app):
        app.screen.fill(BG)
        lo, hi = app.band
        band = ("any price" if hi is None and lo == 0 else f"over ${lo:,.0f}" if hi is None
                else f"under ${hi:,.0f}" if lo == 0 else f"${lo:,.0f} - ${hi:,.0f}")
        app.text(f"TOP {len(app.rows)}  ·  {app.category.upper()}", app.font.lg, ACCENT, 40, 30)
        app.text(band, app.font.md, DIM, SCREEN_W - 40, 44, right=True)

        if not app.rows:
            app.text("Nothing matches that combination.", app.font.md, TEXT,
                     SCREEN_W // 2, 300, centre=True)
            app.text("Try a wider price range, or collect more data with `python run.py all`.",
                     app.font.sm, DIM, SCREEN_W // 2, 344, centre=True)
            app.footer("ESC back     R start over")
            return

        cols = [(50, "#"), (95, "TICKER"), (200, "COMPANY"), (890, "B/H/S"),
                (985, "1MO ODDS"), (1130, "SCORE")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 100)
        for x, label in ((515, "PRICE"), (585, "1 WEEK"), (655, "1 MONTH"), (730, "1 YEAR"),
                         (780, "RSI"), (870, "$VOL")):
            app.text(label, app.font.xs, DIM, x, 100, right=True)
        pygame.draw.line(app.screen, (46, 46, 56), (40, 122), (SCREEN_W - 40, 122))

        key = tuple(r["ticker"] for r in app.rows)
        if getattr(app, "chart_key", None) != key:
            from . import prices
            app.chart_key = key
            app.chart, app.facts = prices.chart_changes(list(key))
        visible = app.rows[app.scroll:app.scroll + self.ROWS_VISIBLE]
        for i, r in enumerate(visible):
            y = 136 + i * 34
            rank = app.scroll + i + 1
            is_selected = (app.scroll + i) == app.selected
            if is_selected:
                pygame.draw.rect(app.screen, (40, 40, 54), (40, y - 6, SCREEN_W - 80, 32),
                                 border_radius=6)
                pygame.draw.rect(app.screen, ACCENT, (40, y - 6, 4, 32), border_radius=2)
            elif i % 2 == 0:
                pygame.draw.rect(app.screen, (21, 21, 27), (40, y - 6, SCREEN_W - 80, 32),
                                 border_radius=6)
            app.text(str(rank), app.font.mono, DIM, 50, y)
            app.text(r["ticker"], app.font.mono_b, BLUE, 95, y)
            app.text(r["company"][:24], app.font.mono, TEXT, 200, y)
            symbol = (app.bulk.get(r["ticker"]) or {}).get("currency") or "$"
            live = (app.facts.get(r["ticker"]) or {}).get("price") or r["price"]
            price = f"{symbol}{live:,.2f}" if live is not None else "-"
            app.text(price, app.font.mono, TEXT, 515, y, right=True)

            ch = app.chart.get(r["ticker"]) or {}
            for x, k in ((585, "1w"), (655, "1m"), (730, "1y")):
                _change(app, ch.get(k), x, y)

            rsi = r["rsi"]
            rsi_col = GREEN if rsi is not None and 50 <= rsi <= 70 else ACCENT if rsi and rsi > 70 else DIM
            app.text(f"{rsi:.0f}" if rsi is not None else "-", app.font.mono, rsi_col, 780, y, right=True)

            dv = r["dollar_vol"] or 0
            dv_s = (f"${dv / 1e9:.1f}B" if dv >= 1e9 else f"${dv / 1e6:.0f}M" if dv >= 1e6
                    else f"${dv / 1e3:.0f}K")
            app.text(dv_s, app.font.mono, DIM, 870, y, right=True)

            counts = (f"{r['counts'][0]}/{r['counts'][1]}/{r['counts'][2]}" if r["counts"]
                      else (r["rating"] or "none")[:14])
            c_col = (GREEN if r["counts"] and r["counts"][2] == 0 and r["counts"][0] >= 5
                     else RED if r["bearish"] else DIM)
            app.text(counts, app.font.mono, c_col, 890, y)

            month = next((h for h in ((r.get("outlook") or {}).get("horizons") or [])
                          if h["days"] == 21), None)
            win = (month or {}).get("historical_win_rate")
            if win is not None:
                colour = GREEN if win >= 55 else ACCENT if win >= 50 else RED
                app.text(f"{win:.0f}%", app.font.mono, colour, 1020, y, right=True)
                app.text(f"{month['low_68']:,.0f}-{month['high_68']:,.0f}",
                         app.font.xs, DIM, 1030, y + 2)

            score = r["score"]
            bar_w = int(70 * min(score, 100) / 100)
            s_col = GREEN if score >= 85 else ACCENT if score >= 60 else DIM
            pygame.draw.rect(app.screen, (40, 40, 50), (1090, y + 6, 70, 8), border_radius=4)
            pygame.draw.rect(app.screen, s_col, (1090, y + 6, bar_w, 8), border_radius=4)
            app.text(f"{score:.0f}", app.font.mono_b, s_col, SCREEN_W - 40, y, right=True)

            warn = []
            if r["structural"]:
                warn.append("/".join(r["structural"]))
            if r["spiked"]:
                warn.append("spiked")
            if r["bearish"]:
                warn.append("rated sell")
            if warn:
                app.text("!", app.font.mono_b, RED, 80, y)

        if len(app.rows) > self.ROWS_VISIBLE:
            app.text(f"showing {app.scroll + 1}-{app.scroll + len(visible)} of {len(app.rows)}",
                     app.font.xs, DIM, SCREEN_W - 40, SCREEN_H - 62, right=True)
        r = app.rows[app.selected]
        app.text(_wrap(app.font.sm, _facts_line(r["ticker"], app.facts.get(r["ticker"])),
                       SCREEN_W - 260)[0], app.font.sm, TEXT, 40, SCREEN_H - 90)
        app.footer("UP/DOWN select   ENTER analyse   M re-check live   O open on MarketBeat   "
                   "R start over   ESC back")


def run():
    app = App()
    scene: Scene = TitleScene()
    while app.running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                app.running = False
            elif event.type == pygame.KEYDOWN:
                nxt = scene.key(event, app)
                if nxt is not None:
                    scene = nxt
        nxt = scene.tick(app)
        if nxt is not None:
            scene = nxt
        scene.draw(app)
        pygame.display.flip()
        app.clock.tick(60)
    pygame.quit()


class DetailScene(Scene):
    """One stock: a real price chart with moving averages and MACD, every indicator's
    verdict, and a link to the MarketBeat page the data came from."""

    def __init__(self, row, back):
        self.row = row
        self.back = back
        self.analysis = None
        self.error = ""
        self.started = False
        self.events: list[dict] = []
        self.currency = "$"

    def key(self, event, app):
        if event.key == pygame.K_ESCAPE:
            return self.back
        if event.key in (pygame.K_o, pygame.K_RETURN, pygame.K_KP_ENTER):
            webbrowser.open(self.url(app))
        return None

    def url(self, app):
        stored = (app.bulk.get(self.row["ticker"], {}) or {}).get("url")
        return stored or (f"https://www.marketbeat.com/stocks/"
                          f"{self.row.get('exchange') or 'NASDAQ'}/{self.row['ticker']}/")

    def load(self, app):
        self.started = True
        try:
            from . import prices, ta
            row = app.bulk.get(self.row["ticker"], {}) or {}
            closes = prices.history(self.row["ticker"], row.get("exchange"))
            if closes.empty:
                self.error = "MarketBeat has no price history for this listing"
                return
            self.currency = row.get("currency") or "USD"
            self.analysis = ta.analyse(self.row["ticker"], closes, app.benchmark(), row)
        except Exception as exc:
            self.error = f"could not analyse: {type(exc).__name__}"

    def chart(self, app, x, y, w, h):
        a = self.analysis
        series = [v for v in a.closes if v is not None]
        if len(series) < 2:
            return
        lo, hi = min(series), max(series)
        span = (hi - lo) or 1.0
        pygame.draw.rect(app.screen, (21, 21, 27), (x, y, w, h), border_radius=8)

        def point(i, value, count):
            px = x + 6 + i * (w - 12) / max(count - 1, 1)
            py = y + h - 8 - (value - lo) / span * (h - 20)
            return px, py

        for values, colour, width in ((a.ma50, (120, 120, 150), 1),
                                      (a.ma20, (150, 190, 255), 1),
                                      (a.closes, ACCENT, 2)):
            pts = [point(i, v, len(values)) for i, v in enumerate(values) if v is not None]
            if len(pts) > 1:
                pygame.draw.lines(app.screen, colour, False, pts, width)

        band = [(i, u, l) for i, (u, l) in enumerate(zip(a.bb_upper, a.bb_lower))
                if u is not None and l is not None]
        if len(band) > 2:
            top = [point(i, u, len(a.bb_upper)) for i, u, _ in band]
            bottom = [point(i, l, len(a.bb_lower)) for i, _, l in band]
            pygame.draw.lines(app.screen, (60, 60, 78), False, top, 1)
            pygame.draw.lines(app.screen, (60, 60, 78), False, bottom, 1)

        app.text(f"{hi:,.2f}", app.font.xs, DIM, x + w - 8, y + 4, right=True)
        app.text(f"{lo:,.2f}", app.font.xs, DIM, x + w - 8, y + h - 22, right=True)
        app.text("120 days   price, 20-day, 50-day, Bollinger", app.font.xs, DIM, x + 8, y + 4)

    def macd_panel(self, app, x, y, w, h):
        a = self.analysis
        values = [v for v in a.macd_hist if v is not None]
        if not values:
            return
        pygame.draw.rect(app.screen, (21, 21, 27), (x, y, w, h), border_radius=8)
        peak = max(abs(min(values)), abs(max(values))) or 1.0
        mid = y + h / 2
        step = (w - 12) / max(len(a.macd_hist) - 1, 1)
        for i, v in enumerate(a.macd_hist):
            if v is None:
                continue
            px = x + 6 + i * step
            height = v / peak * (h / 2 - 6)
            colour = GREEN if v >= 0 else RED
            pygame.draw.rect(app.screen, colour,
                             (px, mid - max(height, 0), max(1.0, step * 0.7), abs(height)))
        pygame.draw.line(app.screen, (60, 60, 78), (x + 6, mid), (x + w - 6, mid))
        app.text("MACD histogram", app.font.xs, DIM, x + 8, y + 4)

    def projections(self, app, x, y):
        """Where the price could be, and what happened to stocks in this shape before."""
        outlook = self.row.get("outlook") or {}
        horizons = outlook.get("horizons") or []
        if not horizons:
            counts = self.row["counts"]
            app.text(f"Analysts: {counts[0]} buy / {counts[1]} hold / {counts[2]} sell"
                     if counts else "no analyst coverage", app.font.sm, DIM, x, y)
            return

        app.text("WHERE IT COULD GO", app.font.sm, ACCENT, x, y)
        odds = outlook.get("setup_odds") or {}
        if odds:
            app.text(f"setup: {odds['setup']}  ·  {odds['n']:,} past cases",
                     app.font.xs, DIM, x + 200, y + 3)

        head_y = y + 26
        app.text("HORIZON", app.font.xs, DIM, x, head_y)
        app.text("LIKELY RANGE (68%)", app.font.xs, DIM, x + 150, head_y)
        app.text("TYPICAL", app.font.xs, DIM, x + 400, head_y)
        app.text("HISTORICALLY UP", app.font.xs, DIM, x + 500, head_y)
        pygame.draw.line(app.screen, (46, 46, 56), (x, head_y + 18), (x + 740, head_y + 18))

        row_y = head_y + 28
        for h in horizons:
            app.text(h["label"], app.font.sm, TEXT, x, row_y)
            app.text(f"${h['low_68']:,.2f} – ${h['high_68']:,.2f}", app.font.sm, TEXT, x + 150, row_y)
            app.text(f"±{h['typical_move_pct']:.1f}%", app.font.sm, DIM, x + 400, row_y)
            win = h.get("historical_win_rate")
            if win is not None:
                colour = GREEN if win >= 55 else ACCENT if win >= 50 else RED
                app.text(f"{win:.0f}%", app.font.sm, colour, x + 500, row_y)
                mean = h.get("historical_mean_pct")
                if mean is not None:
                    app.text(f"avg {mean:+.1f}%", app.font.xs, DIM, x + 560, row_y + 2)
            row_y += 26

        target = outlook.get("analyst_target")
        if target:
            odds_txt = ""
            month = next((h for h in horizons if h["days"] == 21), None)
            if month and month.get("odds_reach_target_pct") is not None:
                odds_txt = f"  ·  {month['odds_reach_target_pct']:.0f}% chance of reaching it within a month"
            app.text(f"Analyst target ${target:,.2f} ({outlook.get('analyst_upside_pct', 0):+.0f}%) "
                     f"on a ~12-month view{odds_txt}", app.font.xs, DIM, x, row_y + 10)

    def draw(self, app):
        app.screen.fill(BG)
        r = self.row
        app.text(r["ticker"], app.font.lg, BLUE, 40, 26)
        app.text(r["company"], app.font.md, TEXT, 40, 78)
        facts = (getattr(app, "facts", None) or {}).get(r["ticker"]) or {}
        live = facts.get("price") or r["price"]
        price = f"${live:,.2f}" if live is not None else "-"
        app.text(price, app.font.lg, TEXT, SCREEN_W - 40, 26, right=True)
        ch = (getattr(app, "chart", None) or {}).get(r["ticker"]) or {}
        x = SCREEN_W - 40
        for label, k in (("1 year", "1y"), ("1 month", "1m"), ("1 week", "1w")):
            v = ch.get(k)
            text = f"{label} {_pct(v, 1)}"
            app.text(text, app.font.md, DIM if v is None else GREEN if v > 0 else RED, x, 80, right=True)
            x -= app.font.md.size(text)[0] + 22
        line = _facts_line(r["ticker"], facts).split("  ·  ", 1)[-1]
        app.text(_wrap(app.font.xs, line, SCREEN_W - 80)[0], app.font.xs, DIM, 40, 108)

        if not self.started:
            app.text("analysing price history...", app.font.md, DIM, SCREEN_W // 2, 300, centre=True)
            return
        if self.error:
            app.text(self.error, app.font.md, RED, SCREEN_W // 2, 300, centre=True)
            app.footer("O open the MarketBeat page     ESC back")
            return

        a = self.analysis
        self.chart(app, 40, 130, 760, 300)
        self.macd_panel(app, 40, 442, 760, 110)

        app.text("MY OWN ANALYSIS", app.font.sm, ACCENT, 830, 130)
        app.text(f"{a.score:.0f}", app.font.xl, GREEN if a.score >= 70 else ACCENT if a.score >= 50 else RED,
                 830, 152)
        app.text(a.summary, app.font.sm, DIM, 960, 190)
        app.text("computed here from MarketBeat's", app.font.xs, DIM, 830, 232)
        app.text("daily closing prices", app.font.xs, DIM, 830, 250)

        y = 284
        for s in a.signals:
            colour = GREEN if s.score >= 0.6 else ACCENT if s.score >= 0.4 else RED
            app.text(s.name, app.font.sm, TEXT, 830, y)
            value = f"{s.value:,.2f}" if s.value is not None else "-"
            app.text(value, app.font.sm, DIM, SCREEN_W - 40, y, right=True)
            app.text(s.verdict, app.font.xs, colour, 830, y + 20)
            y += 46

        self.projections(app, 40, 566)
        app.text(self.url(app), app.font.xs, BLUE, SCREEN_W - 40, 706, right=True)
        app.footer("O or ENTER open the MarketBeat page     ESC back")


class RefreshScene(Scene):
    """Run the whole pipeline from inside the menu, with progress on screen.

    The work happens on a background thread so the window keeps drawing; the thread only
    ever appends to a list of status lines, which the draw method reads.
    """

    def __init__(self, back):
        self.back = back
        self.lines: list[str] = []
        self.done = False
        self.failed = ""
        self.started = time.time()
        self.thread = None

    def begin(self, app):
        import threading

        def say(text):
            self.lines.append(f"{time.time() - self.started:5.0f}s  {text}")

        def work():
            from .runlock import Busy, RunLock
            lock = RunLock("the menu's R refresh")
            try:
                lock.acquire()
            except Busy as busy:
                self.failed = str(busy)
                self.done = True
                return
            try:
                from . import db, notes, pipeline, explore
                notes.start("Screener — refreshed from the menu")
                pipeline.refresh(say)
                notes.close()
                with db.connect() as con:
                    app.bulk, app.prices, app.detail = explore.load(con)
                app.compute()
                say(f"done — {len(app.rows)} stocks ready")
                self.done = True
            except Exception as exc:
                self.failed = f"{type(exc).__name__}: {exc}"
                self.done = True
            finally:
                lock.release()

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def key(self, event, app):
        if self.done and event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE,
                                       pygame.K_SPACE):
            return ResultsScene() if not self.failed else TitleScene()
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("REFRESHING DATA", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("live from MarketBeat — stored price history is reused, only new days are fetched",
                 app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)

        y = 180
        for line in self.lines[-14:]:
            app.text(line, app.font.mono, TEXT, 180, y)
            y += 28

        if not self.done:
            dots = "." * (1 + int(time.time() * 2) % 3)
            app.text(f"working{dots}", app.font.md, ACCENT, 180, y + 10)
            app.footer("about 15-20 minutes; every run reads every stock and price from MarketBeat again")
        elif self.failed:
            app.text(self.failed[:90], app.font.md, RED, 180, y + 10)
            app.footer("ENTER to go back")
        else:
            app.text("finished", app.font.md, GREEN, 180, y + 10)
            app.footer("ENTER to see the results")


class MarketBeatScene(Scene):
    """Check the stocks on screen against MarketBeat and Nasdaq, live.

    The list came from the stored run. This opens each name's MarketBeat page in a Chrome window
    and re-reads the analyst forecast, the buy/hold/sell split, the price target and the
    rest of its research tabs, while asking Nasdaq's own backend the same questions. Each
    figure then has two independent votes beside ours. A row nobody confirms is not a buy,
    whatever the score said.
    """

    LIMIT = 8     # eight MarketBeat page loads plus seven Nasdaq calls per stock

    def __init__(self, back, rows):
        self.back = back
        self.rows = rows[:self.LIMIT]
        self.results: list[dict] = []
        self.done = False
        self.failed = ""
        self.started = time.time()
        self.selected = 0
        self.unfiltered: set[str] = set()

    def begin(self, app):
        import threading
        from .score import consensus_shape_exclusions

        # The results list is ranked by category and price, not by the screener's
        # buy/hold/sell filter, so some rows never had to meet it. Saying so stops a
        # perfectly expected rejection from looking like a broken check.
        self.unfiltered = {r["ticker"] for r in self.rows
                           if consensus_shape_exclusions(app.detail.get(r["ticker"], {}))}

        def work():
            try:
                from . import verify
                picks = [{"ticker": r["ticker"], "score": r.get("score")} for r in self.rows]
                verify.check(picks, headless=False, split=False,
                             on_result=self.results.append)
            except Exception as exc:
                self.failed = f"{type(exc).__name__}: {exc}"
            self.done = True

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def key(self, event, app):
        if event.key in (pygame.K_DOWN, pygame.K_s):
            self.selected = min(max(len(self.results) - 1, 0), self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w):
            self.selected = max(0, self.selected - 1)
        elif event.key == pygame.K_o and self.results:
            webbrowser.open(self.results[self.selected]["url"])
        elif event.key == pygame.K_n and self.results:
            row = self.results[self.selected]
            if row.get("nasdaq_url"):
                webbrowser.open(row["nasdaq_url"])
        elif event.key == pygame.K_ESCAPE and self.done:
            return self.back
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("CHECKING AGAINST MARKETBEAT + NASDAQ", app.font.lg, ACCENT, 40, 30)
        app.text(f"{len(self.results)} of {len(self.rows)}", app.font.md, DIM,
                 SCREEN_W - 40, 44, right=True)
        app.text("Re-reading each pick live on MarketBeat, with Nasdaq asked the same questions "
                 "independently.", app.font.sm, DIM, 40, 76)

        y = 120
        for i, r in enumerate(self.results):
            chosen = i == self.selected
            app.card(40, y, SCREEN_W - 80, 64, chosen)
            colour = {"CONFIRMED": GREEN, "MIXED": ACCENT, "REJECTED": RED}[r["verdict"]]
            app.text(r["ticker"], app.font.mono_b, TEXT, 64, y + 10)
            app.text(r["verdict"], app.font.mono_b, colour, 170, y + 10)
            app.text(f"{r['confidence']}% of checks clean", app.font.xs, DIM, 310, y + 14)
            # MarketBeat's own ratio is the headline: that is what it is being asked.
            forecast = next((c for c in r["checks"] if c["name"] == "Analyst forecast"), None)
            counts = (forecast or {}).get("counts")
            if counts:
                buy, hold, sell = counts
                total = max(buy + hold + sell, 1)
                app.text(f"MarketBeat {buy}/{hold}/{sell}", app.font.mono, TEXT, 440, y + 8)
                app.text(f"{buy / total:.0%} buy", app.font.xs,
                         GREEN if buy / total >= 0.8 else ACCENT if buy / total >= 0.6 else RED,
                         440, y + 32)
            facts = r.get("factcheck", [])
            disputed = sum(1 for f in facts if f["status"] == "disputed")
            confirmed = sum(1 for f in facts if f["status"] == "confirmed")
            app.text(f"{confirmed} figures confirmed", app.font.xs, GREEN, 620, y + 8)
            if disputed:
                app.text(f"{disputed} nobody confirms", app.font.xs, RED, 620, y + 28)
            votes = sorted({n for f in facts for n in f.get("agreed_with", [])})
            app.text(" + ".join(votes) if votes else "no second source",
                     app.font.xs, DIM, 790, y + 8)
            if r["ticker"] in self.unfiltered:
                app.text("never passed the buy/hold/sell filter", app.font.xs, ACCENT,
                         790, y + 28)
            reason = (r["fails"] + r["warns"] + ["clean on every check"])[0]
            app.text(reason[:70], app.font.xs, DIM, 64, y + 38)
            y += 72

        if not self.done:
            dots = "." * (1 + int(time.time() * 2) % 3)
            app.text(f"reading MarketBeat{dots}", app.font.md, ACCENT, 64, y + 12)
            app.footer("about 15 seconds a stock — the Chrome window shows the pages being read")
        elif self.failed:
            app.text(self.failed[:90], app.font.md, RED, 64, y + 12)
            app.footer("ESC back")
        else:
            kept = sum(1 for r in self.results if r["verdict"] == "CONFIRMED")
            app.text(f"MarketBeat confirms {kept} of {len(self.results)}", app.font.md,
                     GREEN if kept else RED, 64, y + 12)
            app.footer("UP/DOWN select   O open the MarketBeat page   N open the Nasdaq page   "
                       "ESC back")


class RatioScene(Scene):
    """MarketBeat only: every candidate from MarketBeat's own lists, ranked by how cleanly
    its analysts say buy. Shows the last stored ranking; R reads MarketBeat again."""

    ROWS_VISIBLE = 14

    def __init__(self, back):
        from . import mb_ratio
        self.back = back
        self.result = mb_ratio.load()
        self.selected = 0
        self.scroll = 0
        self.lines: list[str] = []
        self.busy = False
        self.failed = ""
        self.load_chart()

    def load_chart(self):
        """The chart's 1-week, 1-month and 1-year changes, and the overview page's facts."""
        from . import prices
        self.changes, self.facts = {}, {}
        if self.result:
            self.changes, self.facts = prices.chart_changes(
                [r["ticker"] for r in self.result["rows"]], self.result["built"])

    def refresh(self):
        import threading
        started = time.time()
        self.busy, self.failed, self.lines = True, "", []

        def say(text):
            self.lines.append(f"{time.time() - started:5.0f}s  {text}")

        def work():
            from . import mb_ratio
            from .runlock import Busy, RunLock
            lock = RunLock("the menu's MarketBeat refresh")
            try:
                lock.acquire()
            except Busy as busy:
                self.failed, self.busy = str(busy), False
                return
            try:
                self.result = mb_ratio.run(say)
                self.load_chart()
                self.selected = self.scroll = 0
            except Exception as exc:
                self.failed = f"{type(exc).__name__}: {exc}"
            finally:
                lock.release()
                self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def key(self, event, app):
        rows = (self.result or {}).get("rows", [])
        if event.key == pygame.K_ESCAPE and not self.busy:
            return self.back
        if self.busy:
            return None
        if event.key == pygame.K_r:
            self.refresh()
        elif event.key in (pygame.K_DOWN, pygame.K_s) and rows:
            self.selected = min(len(rows) - 1, self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w) and rows:
            self.selected = max(0, self.selected - 1)
        elif event.key in (pygame.K_o, pygame.K_RETURN, pygame.K_KP_ENTER) and rows:
            webbrowser.open(rows[self.selected]["url"] + "forecast/")
        self.scroll = min(self.scroll, self.selected)
        self.scroll = max(self.scroll, self.selected - self.ROWS_VISIBLE + 1)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("MARKETBEAT  ·  MOST BUYS, FEWEST HOLDS, NO SELLS", app.font.lg, ACCENT, 40, 30)
        if self.busy:
            app.text("reading MarketBeat — about 5 to 10 minutes", app.font.sm, DIM, 40, 82)
            y = 140
            for line in self.lines[-16:]:
                app.text(line, app.font.mono, TEXT, 60, y)
                y += 28
            dots = "." * (1 + int(time.time() * 2) % 3)
            app.text(f"working{dots}", app.font.md, ACCENT, 60, y + 10)
            app.footer("reading every candidate's forecast page")
            return
        if self.failed:
            app.text(self.failed[:100], app.font.md, RED, 40, 120)
        if not self.result:
            app.text("No MarketBeat ranking stored yet.", app.font.md, TEXT,
                     SCREEN_W // 2, 300, centre=True)
            app.footer("R read MarketBeat now     ESC back")
            return

        res = self.result
        app.text(f"built {res['built']}  ·  {res['candidates']:,} candidates from MarketBeat's "
                 f"lists  ·  {len(res['rows']):,} with more than 35 buys, under 5 holds, no sells  ·  "
                 f"ranked by net buys = buys - 2 x holds", app.font.sm, DIM, 40, 82)
        cols = [(50, "#"), (100, "TICKER"), (200, "BUY"), (255, "HOLD"), (315, "SELL"),
                (375, "RATIO"), (540, "NET"), (600, "BUY %")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 118)
        for x, label in ((690, "1 WEEK"), (775, "1 MONTH"), (860, "1 YEAR"), (980, "PRICE"),
                         (1100, "TARGET"), (1235, "UPSIDE")):
            app.text(label, app.font.xs, DIM, x, 118, right=True)
        app.text("PRICE CHART", app.font.xs, ACCENT, 775 - 30, 102, centre=True)
        y = 142
        rows = res["rows"]
        for i in range(self.scroll, min(len(rows), self.scroll + self.ROWS_VISIBLE)):
            r = rows[i]
            if i == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (36, y - 4, SCREEN_W - 72, 34),
                                 border_radius=6)
            app.text(str(r["rank"]), app.font.mono, DIM, 50, y)
            app.text(r["ticker"], app.font.mono_b, TEXT, 100, y)
            app.text(str(r["buy"]), app.font.mono, GREEN, 200, y)
            app.text(str(r["hold"]), app.font.mono, ACCENT if r["hold"] else DIM, 255, y)
            app.text(str(r["sell"]), app.font.mono, RED if r["sell"] else DIM, 315, y)
            total = max(r["analysts"], 1)
            x, w = 375, 150
            for n, colour in ((r["buy"], GREEN), (r["hold"], ACCENT), (r["sell"], RED)):
                seg = round(w * n / total)
                if seg:
                    pygame.draw.rect(app.screen, colour, (x, y + 4, seg, 14))
                x += seg
            app.text(str(mb_ratio.net_buys(r["buy"], r["hold"], r["sell"])), app.font.mono_b,
                     GREEN, 540, y)
            app.text(f"{r['buy_share']:.0%}", app.font.mono, TEXT, 600, y)
            ch = self.changes.get(r["ticker"]) or {}
            for x, key in ((690, "1w"), (775, "1m"), (860, "1y")):
                _change(app, ch.get(key), x, y)
            price = (self.facts.get(r["ticker"]) or {}).get("price") or r["price"]
            app.text(f"${price:,.2f}" if price else "-", app.font.mono, TEXT, 980, y, right=True)
            app.text(f"${r['target']:,.2f}" if r["target"] else "-", app.font.mono, DIM, 1100, y, right=True)
            if r["target"] and price:
                _change(app, (r["target"] / price - 1) * 100, 1235, y)
            y += 36
        if rows:
            r = rows[self.selected]
            line = _facts_line(r["ticker"], self.facts.get(r["ticker"]))
            for k, part in enumerate(_wrap(app.font.sm, line, SCREEN_W - 80)[:2]):
                app.text(part, app.font.sm, TEXT if k == 0 else DIM, 40, 648 + k * 22)
        app.footer("UP/DOWN select   O or ENTER open on MarketBeat   R read MarketBeat again   "
                   "ESC back")


# ---------------------------------------------------------------- shared building blocks

def _back(target):
    """A scene to return to, given either the scene itself or a function that makes one."""
    return target() if callable(target) and not isinstance(target, Scene) else target


class ChoiceScene(Scene):
    """A row of cards to choose from, in the style of the stock screens' steps."""

    def __init__(self, title, step, cards, choose, back, subtitle=""):
        self.title, self.step, self.cards = title, step, cards
        self.choose, self.back, self.subtitle = choose, back, subtitle
        self.index = 0

    def key(self, event, app):
        if event.key in (pygame.K_LEFT, pygame.K_a, pygame.K_UP, pygame.K_w):
            self.index = (self.index - 1) % len(self.cards)
        elif event.key in (pygame.K_RIGHT, pygame.K_d, pygame.K_DOWN, pygame.K_s):
            self.index = (self.index + 1) % len(self.cards)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            return self.choose(self.cards[self.index][0], app)
        elif event.key == pygame.K_ESCAPE:
            return _back(self.back)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text(self.title, app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text(self.step, app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)
        if self.subtitle:
            app.text(self.subtitle, app.font.sm, DIM, SCREEN_W // 2, 136, centre=True)
        n = len(self.cards)
        cols = min(n, 3) if n != 4 else 4
        gap, margin = 22, 60 if cols == 4 else 160
        w = (SCREEN_W - 2 * margin - (cols - 1) * gap) // cols
        h = 150
        for i, (_, title, note) in enumerate(self.cards):
            x = margin + (i % cols) * (w + gap)
            y = 200 + (i // cols) * (h + gap)
            app.card(x, y, w, h, i == self.index)
            app.text(title, app.font.lg if len(title) < 14 else app.font.md,
                     ACCENT if i == self.index else TEXT, x + w // 2, y + 34, centre=True)
            for k, line in enumerate(note.split("\n")):
                app.text(line, app.font.sm, DIM, x + w // 2, y + 92 + k * 22, centre=True)
        app.footer("ARROWS choose     ENTER next     ESC back")


class InputScene(Scene):
    """Type one to three values. Each field is (label, kind, default); kind is money, int,
    date or tickers. `done(values, app)` returns the next scene, or a string to show as an
    error and stay."""

    def __init__(self, title, subtitle, fields, done, back, step=""):
        self.title, self.subtitle, self.fields = title, subtitle, fields
        self.done, self.back, self.step = done, back, step
        self.values = ["" if d is None else str(d) for _, _, d in fields]
        self.active = 0
        self.error = ""

    def parse(self):
        out = []
        for (label, kind, _), raw in zip(self.fields, self.values):
            raw = raw.strip()
            if kind in ("money", "float"):
                try:
                    value = float(raw.replace(",", ""))
                except ValueError:
                    return None, f"{label}: enter a number"
                if value <= 0:
                    return None, f"{label}: must be above zero"
                out.append(value)
            elif kind == "int":
                try:
                    out.append(int(raw))
                except ValueError:
                    return None, f"{label}: enter a whole number"
            elif kind == "date":
                try:
                    from datetime import date as _date
                    out.append(_date.fromisoformat(raw).isoformat())
                except ValueError:
                    return None, f"{label}: use YYYY-MM-DD"
            elif kind == "ticker":
                if not raw:
                    return None, f"{label}: type a ticker"
                out.append(raw.upper())
            else:
                tickers = [t.strip().upper() for t in raw.replace(" ", ",").split(",") if t.strip()]
                if not tickers:
                    return None, f"{label}: type at least one ticker"
                out.append(tickers)
        return out, ""

    def key(self, event, app):
        if event.key == pygame.K_ESCAPE:
            return _back(self.back)
        if event.key in (pygame.K_TAB, pygame.K_DOWN, pygame.K_UP) and len(self.fields) > 1:
            step = -1 if event.key == pygame.K_UP else 1
            self.active = (self.active + step) % len(self.fields)
            return None
        if event.key == pygame.K_BACKSPACE:
            self.values[self.active] = self.values[self.active][:-1]
            return None
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            values, err = self.parse()
            if err:
                self.error = err
                return None
            nxt = self.done(values, app)
            if isinstance(nxt, str):
                self.error = nxt
                return None
            return nxt
        char = event.unicode
        kind = self.fields[self.active][1]
        allowed = {"money": "0123456789.,", "float": "0123456789.", "int": "0123456789",
                   "date": "0123456789-"}.get(kind)
        if char and (char.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789., -"
                     if allowed is None else char in allowed):
            if len(self.values[self.active]) < (120 if kind == "tickers" else 14):
                self.values[self.active] += char.upper() if allowed is None else char
                self.error = ""
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text(self.title, app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        if self.step:
            app.text(self.step, app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)
        app.text(self.subtitle, app.font.sm, DIM, SCREEN_W // 2, 136, centre=True)
        box_w, box_h, gap = 720, 96, 26
        x = (SCREEN_W - box_w) // 2
        y = 190
        for i, (label, kind, _) in enumerate(self.fields):
            active = i == self.active
            app.card(x, y, box_w, box_h, active)
            app.text(label.upper(), app.font.sm, DIM, x + 20, y + 12)
            shown = self.values[i]
            prefix = "$ " if kind == "money" else ""
            font = app.font.lg if len(shown) < 34 else app.font.md
            app.text(prefix + (shown or ""), font, TEXT if shown else DIM, x + 20, y + 40)
            if active and (pygame.time.get_ticks() // 450) % 2 == 0:
                caret_x = x + 24 + font.size(prefix + shown)[0]
                pygame.draw.rect(app.screen, ACCENT, (caret_x, y + 46, 3, 32))
            y += box_h + gap
        if self.error:
            app.text(self.error, app.font.md, RED, SCREEN_W // 2, y + 10, centre=True)
        hint = "TYPE     TAB switch field     ENTER next     ESC back" if len(self.fields) > 1 \
            else "TYPE     ENTER next     ESC back"
        app.footer(hint)


class WorkScene(Scene):
    """Runs a job on a background thread and shows its progress lines; when the job ends,
    `finish(result, app)` gives the scene to show."""

    def __init__(self, title, subtitle, job, finish, back, lock=None):
        import threading
        self.title, self.subtitle, self.finish, self.back = title, subtitle, finish, back
        self.lines: list[str] = []
        self.result = None
        self.failed = ""
        self.done = False
        self.started = time.time()

        def say(text):
            self.lines.append(f"{time.time() - self.started:5.0f}s  {text}")

        def work():
            held = None
            try:
                if lock:
                    from .runlock import RunLock
                    held = RunLock(lock)
                    held.acquire()
                self.result = job(say)
            except Exception as exc:
                self.failed = f"{type(exc).__name__}: {exc}"
            finally:
                if held:
                    held.release()
                self.done = True

        threading.Thread(target=work, daemon=True).start()

    def key(self, event, app):
        if self.done and self.failed and event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                                                       pygame.K_ESCAPE):
            return _back(self.back)
        return None

    def tick(self, app):
        if self.done and not self.failed:
            return self.finish(self.result, app)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text(self.title, app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text(self.subtitle, app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)
        y = 170
        for line in self.lines[-14:]:
            app.text(line[:110], app.font.mono, TEXT, 140, y)
            y += 28
        if self.failed:
            app.text(self.failed[:100], app.font.md, RED, 140, y + 10)
            app.footer("ENTER to go back")
        else:
            dots = "." * (1 + int(time.time() * 2) % 3)
            app.text(f"working{dots}", app.font.md, ACCENT, 140, y + 10)
            app.footer("reading the database and MarketBeat")


def _money(v):
    return "-" if v is None else f"${v:,.2f}"


def _wrap(font, text, width):
    lines, line = [], ""
    for word in text.split():
        if line and font.size(line + " " + word)[0] > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    return lines + ([line] if line else [])


def _pct(v, digits=2):
    return "-" if v is None else f"{v:+.{digits}f}%"


def _change(app, v, x, y):
    """A price change, right-aligned at x, green up and red down."""
    app.text(_pct(v, 1), app.font.mono, DIM if v is None else GREEN if v > 0 else RED, x, y, right=True)


def _short_money(v):
    return ("-" if v is None else "$0" if not v else f"${v / 1e9:.1f}B" if v >= 1e9
            else f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K")


def _facts_line(ticker, f, ch=None):
    """One line of what MarketBeat's overview page says beyond the ratio."""
    f = f or {}
    parts = [ticker]
    if ch:
        parts.append(f"chart: 1 week {_pct(ch.get('1w'), 1)}, 1 month {_pct(ch.get('1m'), 1)}, "
                     f"1 year {_pct(ch.get('1y'), 1)}")
    if f.get("sector"):
        parts.append(f["sector"])
    if f.get("pe"):
        parts.append(f"P/E {f['pe']:.1f}" + (f", PEG {f['peg']:.2f}" if f.get("peg") else ""))
    if f.get("proj_earnings_growth_pct") is not None:
        parts.append(f"earnings growth {f['proj_earnings_growth_pct']:+.0f}% next year")
    if f.get("short_pct_float") is not None:
        parts.append(f"short {f['short_pct_float']:.1f}% of shares")
    bought, sold = f.get("insider_bought_3m"), f.get("insider_sold_3m")
    if bought or sold:
        parts.append(f"insiders bought {_short_money(bought or 0)} / sold {_short_money(sold or 0)} in 3 months")
    if f.get("news_sentiment") is not None:
        avg = f.get("news_sentiment_sector_avg")
        parts.append(f"news mood {f['news_sentiment']:.2f}" + (f" (sector {avg:.2f})" if avg is not None else ""))
    if f.get("inst_own_pct") is not None:
        parts.append(f"institutions own {f['inst_own_pct']:.0f}%")
    if f.get("dividend_yield"):
        parts.append(f"dividend {f['dividend_yield']:.2f}%")
    return "  ·  ".join(parts)


# ---------------------------------------------------------------- backtest

BT_PICKS = [3, 5, 10, 20]
BT_HOLDS = [5, 10, 21, 42, 63]
TREND_NAMES = {"none": "no price rule", "up1m": "skip 1-mo losers", "up3m": "skip 3-mo losers",
               "blend3m": "70/30 blend"}
# The backtest's "which stocks count" row: card name -> (mb_ratio.RULES key, fully invested).
BT_RULES = {"net buys": ("net", False), "my list": ("list", True), "no sells": ("net0", False),
            "old ratio": ("ratio", False)}
BT_RULE_NAMES = list(BT_RULES)
OVERLAY_NAMES = {"none": "none", "rating_up": "rating up", "upgrade_flow": "upgrades",
                 "target_raises": "target raises", "target_upside": "upside", "agreement": "agreement"}


def backtest_start(app):
    from . import replay
    app.bt = {}
    first, last = replay.history_range()
    stored = len(replay.stored_tickers())
    recs = replay.latest_recommendations(20)
    sub = (f"stored price history: {first} to {last}, {stored:,} stocks" if first
           else "no price history stored yet - refresh from MarketBeat first (R on the title screen)")

    def choose(value, app):
        if value == "own":
            return InputScene("WHICH STOCKS?", "type tickers separated by commas, e.g. V, MSFT, NVDA",
                              [("tickers", "tickers", "")], lambda v, a: _bt_set_tickers(v[0], a),
                              lambda: backtest_start(app), step="step 1 of 4")
        return _bt_set_tickers(recs if value == "recs" else None, app, from_recs=value == "recs")

    return ChoiceScene("WHICH STOCKS?", "step 1 of 4  ·  backtest", [
        ("recs", "LATEST TOP 20", "the last run's top 20\n(hindsight: flatters it)"),
        ("all", "EVERY STORED STOCK", f"all {stored:,} stocks with\nstored price history"),
        ("own", "MY OWN TICKERS", "type a list"),
    ], choose, TitleScene, subtitle=sub)


def _bt_set_tickers(tickers, app, from_recs=False):
    if tickers is not None and not tickers:
        return "no recommendations stored yet - refresh from MarketBeat first"
    app.bt["tickers"] = tickers
    app.bt["from_recs"] = from_recs
    return _bt_period(app)


def _bt_period(app):
    from datetime import date as _date, timedelta
    from . import replay
    first, last = replay.history_range()
    if not first:
        return "no price history stored yet - refresh from MarketBeat first"
    end = _date.fromisoformat(last)

    def ago(months):
        return max(first, (end - timedelta(days=round(months * 30.44))).isoformat())

    def choose(value, app):
        if value == "custom":
            return InputScene("WHAT PERIOD?", f"stored history runs {first} to {last}",
                              [("start date", "date", ago(6)), ("end date", "date", last)],
                              lambda v, a: _bt_set_period(v[0], v[1], a), lambda: _bt_period(app),
                              step="step 2 of 4")
        return _bt_set_period(ago(value), last, app)

    return ChoiceScene("WHAT PERIOD?", "step 2 of 4", [
        (3, "3 MONTHS", f"{ago(3)}\nto {last}"),
        (6, "6 MONTHS", f"{ago(6)}\nto {last}"),
        (12, "ALL STORED", f"{first}\nto {last}"),
        ("custom", "CUSTOM", "type the start\nand end dates"),
    ], choose, lambda: backtest_start(app),
        subtitle="MarketBeat's chart gives about a year; the database keeps every day it has stored")


def _bt_set_period(start, end, app):
    if start >= end:
        return "the start date must be before the end date"
    app.bt["start"], app.bt["end"] = start, end
    return InputScene("STARTING CASH", "how much the simulated portfolio starts with",
                      [("starting cash", "money", "10000")],
                      lambda v, a: _bt_set_cash(v[0], a), lambda: _bt_period(app), step="step 3 of 4")


def _bt_set_cash(cash, app):
    app.bt["cash"] = cash
    return BacktestParamsScene()


class BacktestParamsScene(Scene):
    def __init__(self):
        from . import replay
        self.picks = BT_PICKS.index(10)
        self.hold = BT_HOLDS.index(21)
        self.trends = list(replay.TRENDS)
        self.trend = self.trends.index(replay.DEFAULT_TREND)
        self.rules = list(BT_RULE_NAMES)
        self.rule = 0
        self.overlays = list(replay.OVERLAYS)
        self.overlay = self.overlays.index(replay.DEFAULT_OVERLAY)

    def key(self, event, app):
        if event.key in (pygame.K_t, pygame.K_TAB):
            self.trend = (self.trend + 1) % len(self.trends)
        elif event.key == pygame.K_r:
            self.rule = (self.rule + 1) % len(self.rules)
        elif event.key == pygame.K_x:
            self.overlay = (self.overlay + 1) % len(self.overlays)
        elif event.key in (pygame.K_LEFT, pygame.K_a):
            self.picks = max(0, self.picks - 1)
        elif event.key in (pygame.K_RIGHT, pygame.K_d):
            self.picks = min(len(BT_PICKS) - 1, self.picks + 1)
        elif event.key in (pygame.K_UP, pygame.K_w):
            self.hold = min(len(BT_HOLDS) - 1, self.hold + 1)
        elif event.key in (pygame.K_DOWN, pygame.K_s):
            self.hold = max(0, self.hold - 1)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            app.bt["picks"], app.bt["hold"] = BT_PICKS[self.picks], BT_HOLDS[self.hold]
            app.bt["trend"] = self.trends[self.trend]
            app.bt["rule"], app.bt["invest_all"] = BT_RULES[self.rules[self.rule]]
            app.bt["overlay"] = self.overlays[self.overlay]
            return run_backtest(app)
        elif event.key == pygame.K_ESCAPE:
            return InputScene("STARTING CASH", "how much the simulated portfolio starts with",
                              [("starting cash", "money", str(app.bt.get("cash", 10000)))],
                              lambda v, a: _bt_set_cash(v[0], a), lambda: _bt_period(app),
                              step="step 3 of 4")
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("HOW SHOULD IT TRADE?", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("step 4 of 4  ·  the strategy buys the most net buys (buys - 2 x holds - 5 x sells) and re-checks "
                 "them after each holding period", app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)
        from . import replay
        names = [TREND_NAMES[t] for t in self.trends]
        rule, invest_all = BT_RULES[self.rules[self.rule]]
        rule_note = "R to change  ·  " + replay.RULES[rule][1] + ("  ·  all money in" if invest_all else "")
        for row, (label, choices, index, note, w) in enumerate((
                ("STOCKS HELD AT ONCE", BT_PICKS, self.picks, "LEFT / RIGHT", 86),
                ("TRADING DAYS BETWEEN RE-CHECKS", BT_HOLDS, self.hold, "UP / DOWN", 86),
                ("WHICH STOCKS COUNT", self.rules, self.rule, rule_note, 230),
                ("PRICE-CHART RULE", names, self.trend, "T to change  ·  "
                 + replay.TRENDS[self.trends[self.trend]][0], 230),
                ("EXTRA MARKETBEAT RULE", [OVERLAY_NAMES[o] for o in self.overlays], self.overlay,
                 "X to change  ·  " + replay.OVERLAYS[self.overlays[self.overlay]][0], 150))):
            y = 128 + row * 118
            app.text(label, app.font.md, TEXT, SCREEN_W // 2, y, centre=True)
            app.text(note, app.font.xs, DIM, SCREEN_W // 2, y + 28, centre=True)
            total_w = len(choices) * (w + 14)
            x0 = (SCREEN_W - total_w) // 2
            for i, value in enumerate(choices):
                x = x0 + i * (w + 14)
                app.card(x, y + 50, w, 56, i == index)
                app.text(str(value), app.font.md, ACCENT if i == index else DIM, x + w // 2, y + 60,
                         centre=True)
        app.footer("ARROWS choose   R which stocks   T price-chart rule   X extra rule   ENTER run   ESC back")


def run_backtest(app):
    from . import replay
    bt = dict(app.bt)

    def job(say):
        return replay.run(bt["start"], bt["end"], bt["tickers"], bt["cash"], bt["picks"],
                          bt["hold"], say=say, from_recommendations=bt.get("from_recs", False),
                          trend=bt.get("trend", "none"), rule=bt.get("rule", "net"),
                          invest_all=bt.get("invest_all", False), overlay=bt.get("overlay", "none"))

    def finish(result, app):
        if result.get("error"):
            return MessageScene("BACKTEST", result["error"], lambda: backtest_start(app))
        return BacktestResultScene(result)

    return WorkScene("BACKTEST", f"{bt['start']} to {bt['end']}  ·  ${bt['cash']:,.0f}  ·  "
                                 f"top {bt['picks']}, re-checked every {bt['hold']} days",
                     job, finish, lambda: backtest_start(app))


class MessageScene(Scene):
    def __init__(self, title, message, back):
        self.title, self.message, self.back = title, message, back

    def key(self, event, app):
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE, pygame.K_SPACE):
            return _back(self.back)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text(self.title, app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        words, line, y = self.message.split(), "", 240
        for w in words:
            if app.font.md.size(line + " " + w)[0] > SCREEN_W - 240:
                app.text(line, app.font.md, TEXT, SCREEN_W // 2, y, centre=True)
                line, y = w, y + 34
            else:
                line = (line + " " + w).strip()
        app.text(line, app.font.md, TEXT, SCREEN_W // 2, y, centre=True)
        app.footer("ENTER back")


def _equity_chart(app, equity, x, y, w, h):
    pygame.draw.rect(app.screen, (21, 21, 27), (x, y, w, h), border_radius=8)
    series = [("strategy", ACCENT, 2), ("buy_hold", BLUE, 1), ("benchmark", DIM, 1)]
    values = [e[k] for e in equity for k, _, _ in series if e.get(k) is not None]
    if len(equity) < 2 or not values:
        return
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(equity)
    for key, colour, width in series:
        pts = [(x + 8 + i * (w - 16) / (n - 1), y + h - 10 - (e[key] - lo) / span * (h - 30))
               for i, e in enumerate(equity) if e.get(key) is not None]
        if len(pts) > 1:
            pygame.draw.lines(app.screen, colour, False, pts, width)
    app.text(f"${hi:,.0f}", app.font.xs, DIM, x + w - 8, y + 22, right=True)
    app.text(f"${lo:,.0f}", app.font.xs, DIM, x + w - 8, y + h - 40, right=True)
    app.text(equity[0]["date"], app.font.xs, DIM, x + 8, y + h - 20)
    app.text(equity[-1]["date"], app.font.xs, DIM, x + w - 8, y + h - 20, right=True)
    lx = x + 10
    for key, colour, _ in series:
        label = {"strategy": "strategy", "buy_hold": "buy-and-hold", "benchmark": "S&P 500 (SPY)"}[key]
        pygame.draw.rect(app.screen, colour, (lx, y + 10, 14, 4))
        app.text(label, app.font.xs, DIM, lx + 20, y + 3)
        lx += 40 + app.font.xs.size(label)[0]


class BacktestResultScene(Scene):
    def __init__(self, result, back=None):
        self.r = result
        self.back = back

    def key(self, event, app):
        if event.key == pygame.K_h:
            return BacktestHistoryScene()
        if event.key == pygame.K_r:
            return backtest_start(app)
        if event.key == pygame.K_ESCAPE:
            return _back(self.back) if self.back else TitleScene()
        return None

    def draw(self, app):
        app.screen.fill(BG)
        m, p = self.r["metrics"], self.r["params"]
        app.text(f"BACKTEST #{self.r.get('id', '-')}", app.font.lg, ACCENT, 40, 24)
        who = p["tickers"] if isinstance(p["tickers"], str) else ", ".join(p["tickers"][:8]) + \
            (f" +{len(p['tickers']) - 8}" if len(p["tickers"]) > 8 else "")
        app.text(f"{m['start']} to {m['end']}  ·  {m['stocks']} stocks ({who})  ·  top {p['picks']}, "
                 f"re-checked every {p['hold_days']} days", app.font.sm, DIM, 40, 74)
        from . import replay
        app.text(replay.describe(p), app.font.xs, DIM, 40, 98)

        rows = [
            ("", "STRATEGY", "BUY-AND-HOLD", "S&P 500"),
            ("total return", _pct(m["total_return_pct"]), _pct(m["buy_hold_return_pct"]),
             _pct(m["spy_return_pct"])),
            ("ending value", _money(m["end_equity"]),
             _money(m["start_cash"] * (1 + m["buy_hold_return_pct"] / 100)),
             _money(m["start_cash"] * (1 + m["spy_return_pct"] / 100)) if m["spy_return_pct"] is not None else "-"),
            ("max drawdown", _pct(m["max_drawdown_pct"]), _pct(m["buy_hold_max_drawdown_pct"]), ""),
            ("trades", str(m["trades"]), "", ""),
            ("win rate", "-" if m["win_rate_pct"] is None else f"{m['win_rate_pct']:.1f}%", "", ""),
            ("average trade", _pct(m["avg_trade_pct"]), "", ""),
        ]
        y = 116
        for i, (label, a, b, c) in enumerate(rows):
            colour = DIM if i == 0 else TEXT
            app.text(label, app.font.sm, DIM, 40, y)
            for x, v in ((250, a), (420, b), (590, c)):
                col = colour
                if i == 1 and v not in ("", "-"):
                    col = GREEN if v.startswith("+") else RED
                app.text(v, app.font.sm if i else app.font.xs, col, x, y)
            y += 30
        beat = m["vs_buy_hold_pts"]
        app.text(f"strategy vs buy-and-hold: {beat:+.2f} points", app.font.md,
                 GREEN if beat > 0 else RED, 40, y + 6)
        app.text(f"starting cash {_money(m['start_cash'])}  ·  {m['rebalances']} re-checks  ·  "
                 f"cash sitting idle {m['avg_idle_cash_pct']:.0f}% on average  ·  "
                 f"{m['picks_on_snapshots_pct']:.0f}% of picks on stored snapshots",
                 app.font.xs, DIM, 40, y + 40)

        equity = self.r.get("equity") or []
        _equity_chart(app, equity, 760, 116, 480, 300)

        trades = sorted(self.r.get("trades") or [], key=lambda t: -t["return_pct"])
        ty = 440
        app.text("BEST TRADES", app.font.xs, DIM, 760, ty)
        app.text("WORST TRADES", app.font.xs, DIM, 1010, ty)
        for k, t in enumerate(trades[:5]):
            app.text(f"{t['ticker']:<6} {t['return_pct']:+6.1f}%", app.font.mono, GREEN, 760, ty + 20 + k * 22)
        for k, t in enumerate(list(reversed(trades))[:5]):
            app.text(f"{t['ticker']:<6} {t['return_pct']:+6.1f}%", app.font.mono, RED, 1010, ty + 20 + k * 22)

        ny = max(y + 76, 440)
        for note in m.get("notes", [])[:5]:
            for chunk in _wrap(app.font.xs, note, 680)[:3]:
                app.text(chunk, app.font.xs, ACCENT, 40, ny)
                ny += 20
            ny += 6
        app.text("Past results from rebuilt MarketBeat data. Model-generated, not financial advice.",
                 app.font.xs, DIM, 40, SCREEN_H - 70)
        app.footer("H saved backtests     R new backtest     ESC title")


class BacktestHistoryScene(Scene):
    """Every saved backtest, newest first, so runs can be compared over time."""
    ROWS_VISIBLE = 15

    def __init__(self):
        from . import replay
        self.rows = replay.saved(200)
        self.selected = 0
        self.scroll = 0

    def key(self, event, app):
        if event.key in (pygame.K_DOWN, pygame.K_s) and self.rows:
            self.selected = min(len(self.rows) - 1, self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w) and self.rows:
            self.selected = max(0, self.selected - 1)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.rows:
            from . import replay
            r = self.rows[self.selected]
            with db.connect() as con:
                trades = [dict(t) for t in con.execute(
                    "SELECT * FROM backtest_trades WHERE backtest_id=?", (r["id"],))]
            return BacktestResultScene({"id": r["id"], "params": r["params"], "metrics": r["metrics"],
                                        "equity": replay.equity_of(r["id"]), "trades": trades},
                                       back=BacktestHistoryScene)
        elif event.key == pygame.K_ESCAPE:
            return TitleScene()
        elif event.key == pygame.K_r:
            return backtest_start(app)
        self.scroll = min(self.scroll, self.selected)
        self.scroll = max(self.scroll, self.selected - self.ROWS_VISIBLE + 1)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("SAVED BACKTESTS", app.font.lg, ACCENT, 40, 30)
        if not self.rows:
            app.text("No backtests saved yet.", app.font.md, TEXT, SCREEN_W // 2, 300, centre=True)
            app.footer("R new backtest     ESC title")
            return
        cols = [(50, "#"), (100, "RUN AT"), (290, "PERIOD"), (530, "STOCKS"), (620, "CASH"),
                (720, "TOP/DAYS"), (830, "RETURN"), (935, "BUY&HOLD"), (1045, "WIN"), (1120, "MAX DD")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 96)
        y = 122
        for i in range(self.scroll, min(len(self.rows), self.scroll + self.ROWS_VISIBLE)):
            r = self.rows[i]
            m, p = r["metrics"], r["params"]
            if i == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (36, y - 4, SCREEN_W - 72, 32), border_radius=6)
            app.text(str(r["id"]), app.font.mono, DIM, 50, y)
            app.text(r["created"].replace("T", " ")[:16], app.font.mono, TEXT, 100, y)
            app.text(f"{m['start']} - {m['end'][5:]}", app.font.mono, TEXT, 290, y)
            app.text(str(m["stocks"]), app.font.mono, TEXT, 530, y)
            app.text(f"${m['start_cash']:,.0f}", app.font.mono, TEXT, 620, y)
            app.text(f"{p['picks']}/{p['hold_days']}", app.font.mono, TEXT, 720, y)
            ret = m["total_return_pct"]
            app.text(_pct(ret, 1), app.font.mono_b, GREEN if ret > 0 else RED, 830, y)
            bh = m["buy_hold_return_pct"]
            app.text(_pct(bh, 1), app.font.mono, GREEN if bh > 0 else RED, 935, y)
            app.text("-" if m["win_rate_pct"] is None else f"{m['win_rate_pct']:.0f}%",
                     app.font.mono, TEXT, 1045, y)
            app.text(f"{m['max_drawdown_pct']:.1f}%", app.font.mono, RED, 1120, y)
            y += 34
        app.footer("UP/DOWN select     ENTER open     R new backtest     ESC title")


# ---------------------------------------------------------------- portfolio planner

PLAN_COUNTS = [3, 5, 8, 10]


def planner_start(app):
    app.plan = {}
    return InputScene("PORTFOLIO PLANNER", "how much cash do you have to invest?",
                      [("cash to invest", "money", "")], lambda v, a: _plan_set_cash(v[0], a),
                      TitleScene, step="step 1 of 3")


def _plan_set_cash(cash, app):
    app.plan["cash"] = cash

    def choose(value, app):
        if value == "own":
            return PlanPickScene()
        return ChoiceScene("HOW MANY STOCKS?", "step 2 of 3", [
            (n, str(n), "stocks") for n in PLAN_COUNTS], _plan_set_count,
            lambda: _plan_set_cash(cash, app),
            subtitle="the program takes the top of the last run's list and "
                     "checks each one live on MarketBeat")

    return ChoiceScene("WHO PICKS THE STOCKS?", f"step 2 of 3  ·  ${cash:,.2f} to invest", [
        ("own", "MY TICKERS", "choose from earlier\nrecommendations"),
        ("auto", "PICK FOR ME", "top Buy-rated stocks,\nchecked live"),
    ], choose, lambda: planner_start(app))


def _plan_set_count(n, app):
    app.plan["tickers"], app.plan["picks"] = None, n
    return HoldingsScene()


class PlanPickScene(Scene):
    """Tick stocks from the last run's recommendations, or type others."""
    ROWS_VISIBLE = 14

    def __init__(self):
        from . import planner, prices
        self.rows = planner.recommendation_choices(60)
        self.chart, _ = prices.chart_changes([r["ticker"] for r in self.rows],
                                             self.rows[0]["run_date"] if self.rows else None)
        self.chosen: list[str] = []
        self.selected = 0
        self.scroll = 0
        self.error = ""

    def key(self, event, app):
        if event.key in (pygame.K_DOWN, pygame.K_s) and self.rows:
            self.selected = min(len(self.rows) - 1, self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w) and self.rows:
            self.selected = max(0, self.selected - 1)
        elif event.key == pygame.K_SPACE and self.rows:
            t = self.rows[self.selected]["ticker"]
            self.chosen.remove(t) if t in self.chosen else self.chosen.append(t)
        elif event.key == pygame.K_t:
            scene = self

            def add(values, app):
                for t in values[0]:
                    if t not in scene.chosen:
                        scene.chosen.append(t)
                return scene

            return InputScene("ADD TICKERS", "type tickers separated by commas",
                              [("tickers", "tickers", "")], add, self, step="step 2 of 3")
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if not self.chosen:
                self.error = "tick at least one stock (SPACE), or type one (T)"
                return None
            app.plan["tickers"] = list(self.chosen)
            return HoldingsScene()
        elif event.key == pygame.K_ESCAPE:
            return _plan_set_cash(app.plan["cash"], app)
        self.scroll = min(self.scroll, self.selected)
        self.scroll = max(self.scroll, self.selected - self.ROWS_VISIBLE + 1)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("CHOOSE YOUR STOCKS", app.font.lg, ACCENT, 40, 24)
        built = self.rows[0]["run_date"] if self.rows else "-"
        app.text(f"step 2 of 3  ·  recommendations from the run of {built}  ·  "
                 f"chosen: {', '.join(self.chosen) or 'none yet'}", app.font.sm, DIM, 40, 74)
        cols = [(50, ""), (90, "#"), (140, "TICKER"), (250, "BUY"), (320, "HOLD"), (390, "SELL"),
                (470, "BUY %"), (570, "PRICE THEN"), (700, "TARGET"), (820, "UPSIDE")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 106)
        for x, label in ((990, "1 WEEK"), (1090, "1 MONTH"), (1190, "1 YEAR")):
            app.text(label, app.font.xs, DIM, x, 106, right=True)
        y = 130
        for i in range(self.scroll, min(len(self.rows), self.scroll + self.ROWS_VISIBLE)):
            r = self.rows[i]
            if i == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (36, y - 4, SCREEN_W - 72, 32), border_radius=6)
            ticked = r["ticker"] in self.chosen
            pygame.draw.rect(app.screen, GREEN if ticked else DIM, (52, y + 2, 16, 16),
                             0 if ticked else 2, border_radius=3)
            app.text(str(r["rank"]), app.font.mono, DIM, 90, y)
            app.text(r["ticker"], app.font.mono_b, TEXT, 140, y)
            app.text(str(r["buy"]), app.font.mono, GREEN, 250, y)
            app.text(str(r["hold"]), app.font.mono, ACCENT if r["hold"] else DIM, 320, y)
            app.text(str(r["sell"]), app.font.mono, RED if r["sell"] else DIM, 390, y)
            app.text(f"{r['buy_share']:.0%}", app.font.mono, TEXT, 470, y)
            app.text(_money(r["price"]), app.font.mono, TEXT, 570, y)
            app.text(_money(r["target"]), app.font.mono, DIM, 700, y)
            if r["upside_pct"] is not None:
                app.text(_pct(r["upside_pct"], 1), app.font.mono, GREEN if r["upside_pct"] > 0 else RED, 820, y)
            ch = self.chart.get(r["ticker"]) or {}
            for x, k in ((990, "1w"), (1090, "1m"), (1190, "1y")):
                _change(app, ch.get(k), x, y)
            y += 34
        if not self.rows:
            app.text("No recommendations stored yet - press T to type tickers.", app.font.md, TEXT,
                     SCREEN_W // 2, 300, centre=True)
        if self.error:
            app.text(self.error, app.font.sm, RED, 40, SCREEN_H - 70)
        app.footer("UP/DOWN move   SPACE tick   T type tickers   ENTER next   ESC back")


class HoldingsScene(Scene):
    """Stocks already owned. Saved in the database, so they are entered once."""

    def __init__(self):
        from . import planner
        self.rows = planner.holdings()
        self.selected = 0

    def key(self, event, app):
        from . import planner
        if event.key in (pygame.K_DOWN, pygame.K_s) and self.rows:
            self.selected = min(len(self.rows) - 1, self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w) and self.rows:
            self.selected = max(0, self.selected - 1)
        elif event.key == pygame.K_a:
            def add(values, app):
                ticker, shares, total = values
                planner.save_holding(ticker, shares, total / shares)
                return HoldingsScene()
            return InputScene("ADD A HOLDING", "a stock you already own - saved for next time",
                              [("ticker", "ticker", ""), ("number of shares", "float", ""),
                               ("total you paid for all of them", "money", "")], add, HoldingsScene,
                              step="step 3 of 3")
        elif event.key in (pygame.K_d, pygame.K_DELETE) and self.rows:
            planner.delete_holding(self.rows[self.selected]["ticker"])
            self.rows = planner.holdings()
            self.selected = min(self.selected, max(0, len(self.rows) - 1))
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return build_plan(app)
        elif event.key == pygame.K_ESCAPE:
            return PlanPickScene() if app.plan.get("tickers") else _plan_set_cash(app.plan["cash"], app)
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("STOCKS YOU ALREADY OWN", app.font.lg, ACCENT, SCREEN_W // 2, 60, centre=True)
        app.text("step 3 of 3  ·  add them once and the planner gives sell guidance every time",
                 app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)
        x0 = 300
        for x, label in ((x0, "TICKER"), (x0 + 160, "SHARES"), (x0 + 300, "PAID IN ALL"),
                         (x0 + 500, "PER SHARE")):
            app.text(label, app.font.xs, DIM, x, 170)
        y = 196
        for i, r in enumerate(self.rows[:12]):
            if i == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (x0 - 14, y - 4, 680, 32), border_radius=6)
            app.text(r["ticker"], app.font.mono_b, TEXT, x0, y)
            app.text(f"{r['shares']:g}", app.font.mono, TEXT, x0 + 160, y)
            app.text(_money(r["shares"] * r["purchase_price"]), app.font.mono, TEXT, x0 + 300, y)
            app.text(_money(r["purchase_price"]), app.font.mono, DIM, x0 + 500, y)
            y += 34
        if not self.rows:
            app.text("None saved. Press A to add one, or ENTER to skip.", app.font.md, DIM,
                     SCREEN_W // 2, 260, centre=True)
        app.footer("A add     D delete     ENTER build the plan     ESC back")


def build_plan(app):
    from . import planner
    plan = dict(app.plan)

    def job(say):
        return planner.build(plan["cash"], plan.get("tickers"), plan.get("picks") or 5, say=say)

    return WorkScene("BUILDING YOUR PLAN", f"${plan['cash']:,.2f}  ·  reading each stock live on MarketBeat",
                     job, lambda result, app: PlanScene(result), lambda: planner_start(app))


class PlanScene(Scene):
    def __init__(self, plan):
        self.plan = plan
        self.selected = 0
        self.items = [("buy", r) for r in plan["rows"]] + [("held", h) for h in plan["holdings"]]

    def key(self, event, app):
        if event.key in (pygame.K_DOWN, pygame.K_s) and self.items:
            self.selected = min(len(self.items) - 1, self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w) and self.items:
            self.selected = max(0, self.selected - 1)
        elif event.key == pygame.K_o and self.items:
            item = self.items[self.selected][1]
            webbrowser.open(item.get("url") or f"https://www.marketbeat.com/stocks/NASDAQ/{item['ticker']}/")
        elif event.key == pygame.K_r:
            return planner_start(app)
        elif event.key == pygame.K_ESCAPE:
            return TitleScene()
        return None

    def draw(self, app):
        from . import planner
        app.screen.fill(BG)
        p = self.plan
        app.text("YOUR PLAN", app.font.lg, ACCENT, 40, 20)
        app.text(f"${p['cash']:,.2f} to invest  ·  " +
                 ("your own picks" if p["mode"] == "own" else "picked by the program"),
                 app.font.sm, DIM, 40, 66)
        app.text(planner.DISCLAIMER, app.font.sm, ACCENT, SCREEN_W - 40, 30, right=True)

        cols = [(40, "TICKER"), (130, "RATING"), (215, "BUY / HOLD / SELL"), (420, "CONF."),
                (490, "PRICE"), (600, "ENTRY"), (800, "SHARES"), (880, "AMOUNT")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 100)
        y = 122
        for k, r in enumerate(p["rows"][:9]):
            idx = k
            if idx == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (30, y - 4, SCREEN_W - 60, 30), border_radius=6)
            s = r["signal"]
            colour = {"Buy": GREEN, "Hold": ACCENT, "Sell": RED}[s["label"]]
            app.text(r["ticker"], app.font.mono_b, TEXT, 40, y)
            app.text(s["label"], app.font.mono_b, colour, 130, y)
            counts = s.get("counts")
            app.text(f"{counts[0]}/{counts[1]}/{counts[2]}  {s['buy_share']:.0%} buy" if counts
                     else "no ratings", app.font.mono, TEXT, 215, y)
            app.text("-" if s["confidence"] is None else f"{s['confidence']:.0f}", app.font.mono, TEXT, 420, y)
            app.text(_money(r["price"]), app.font.mono, TEXT, 490, y)
            e = r["entry"]
            app.text("buy now" if not e["limit"] else f"limit {_money(e['limit'])}", app.font.mono,
                     GREEN if not e["limit"] else ACCENT, 600, y)
            app.text(str(r.get("shares", 0)), app.font.mono, TEXT, 800, y)
            app.text(_money(r.get("amount", 0)), app.font.mono_b, TEXT, 880, y)
            y += 30
        spent = sum(r.get("amount", 0) for r in p["rows"])
        app.text(f"invested {_money(spent)}  ·  left as cash {_money(p['left'])}", app.font.sm, TEXT, 40, y + 4)
        if p["skipped"]:
            app.text("skipped: " + "; ".join(p["skipped"])[:150], app.font.xs, DIM, 40, y + 28)

        hy = max(y + 60, 420)
        app.text("STOCKS YOU OWN", app.font.sm, ACCENT, 40, hy)
        hcols = [(40, "TICKER"), (130, "SHARES"), (210, "PAID"), (320, "PRICE"), (430, "GAIN / LOSS"),
                 (600, "ACTION"), (720, "SELL LIMIT"), (860, "STOP-LOSS"), (990, "RATING")]
        for x, label in hcols:
            app.text(label, app.font.xs, DIM, x, hy + 26)
        y = hy + 48
        if not p["holdings"]:
            app.text("none saved - add them on the holdings step to get sell guidance", app.font.sm,
                     DIM, 40, y)
        for k, h in enumerate(p["holdings"][:6]):
            idx = len(p["rows"]) + k
            if idx == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (30, y - 4, SCREEN_W - 60, 30), border_radius=6)
            app.text(h["ticker"], app.font.mono_b, TEXT, 40, y)
            app.text(f"{h['shares']:g}", app.font.mono, TEXT, 130, y)
            app.text(_money(h["purchase_price"]), app.font.mono, TEXT, 210, y)
            app.text(_money(h.get("price")), app.font.mono, TEXT, 320, y)
            if h.get("gain") is not None:
                app.text(f"{_money(h['gain'])} {_pct(h['gain_pct'], 1)}", app.font.mono,
                         GREEN if h["gain"] >= 0 else RED, 430, y)
            colour = RED if h["action"] == "Sell now" else TEXT
            app.text(h["action"], app.font.mono_b, colour, 600, y)
            limit, own = _sell_limit(h)
            app.text(_money(limit), app.font.mono_b if own else app.font.mono, ACCENT if own else TEXT, 720, y)
            app.text(_money(h.get("stop_loss")), app.font.mono, TEXT, 860, y)
            app.text(h["signal"]["label"], app.font.mono, DIM, 990, y)
            y += 30

        if self.items:
            kind, item = self.items[self.selected]
            reason = (item["entry"]["reason"] if kind == "buy" else item.get("reason", ""))
            app.text(f"{item['ticker']}: {reason}"[:150], app.font.sm, TEXT, 40, SCREEN_H - 72)
        app.footer("UP/DOWN select   O open on MarketBeat   R new plan   ESC title")


# ---------------------------------------------------------------- my portfolio

def _sell_limit(h):
    """The sell limit to show for a holding, and whether it is the user's own."""
    if h.get("sell_limit") and h.get("action") != "Sell now":
        return h["sell_limit"], True
    return h.get("target_sell"), False


def portfolio_start(app, selected=0):
    from . import planner

    def job(say):
        rows = planner.review(say)
        if not rows:
            say("no holdings saved yet")
        return rows

    return WorkScene("MY PORTFOLIO", "reading each stock you own live on MarketBeat", job,
                     lambda rows, app: PortfolioScene(rows, selected), TitleScene)


class PortfolioScene(Scene):
    """The stocks you own: what you paid, what they are worth now, and when to sell."""
    ROWS_VISIBLE = 11

    def __init__(self, rows, selected=0):
        from . import prices
        self.rows = rows
        self.selected = min(selected, max(0, len(rows) - 1))
        self.scroll = max(0, self.selected - self.ROWS_VISIBLE + 1)
        self.chart, self.facts = prices.chart_changes([r["ticker"] for r in rows])

    def _edit(self, app, row=None):
        from . import planner

        def save(values, app):
            ticker, shares, total = values
            if row and row["ticker"] != ticker:
                planner.delete_holding(row["ticker"])
            planner.save_holding(ticker, shares, total / shares)
            tickers = sorted({r["ticker"] for r in self.rows} - {row and row["ticker"]} | {ticker})
            return portfolio_start(app, tickers.index(ticker))

        fields = [("ticker", "ticker", row["ticker"] if row else ""),
                  ("number of shares", "float", f"{row['shares']:g}" if row else ""),
                  ("total you paid for all of them", "money",
                   f"{row['shares'] * row['purchase_price']:.2f}" if row else "")]
        return InputScene("EDIT A HOLDING" if row else "ADD A STOCK YOU OWN",
                          "saved in the database, so you only enter it once", fields, save, self)

    def key(self, event, app):
        from . import planner
        row = self.rows[self.selected] if self.rows else None
        if event.key in (pygame.K_DOWN, pygame.K_s) and self.rows:
            self.selected = min(len(self.rows) - 1, self.selected + 1)
        elif event.key in (pygame.K_UP, pygame.K_w) and self.rows:
            self.selected = max(0, self.selected - 1)
        elif event.key == pygame.K_a:
            return self._edit(app)
        elif event.key == pygame.K_e and row:
            return self._edit(app, row)
        elif event.key == pygame.K_l and row:
            def save(values, app):
                planner.set_sell_limit(row["ticker"], values[0])
                return portfolio_start(app, self.selected)

            suggested = row.get("target_sell")
            default = row.get("sell_limit") or suggested
            return InputScene(f"SELL LIMIT FOR {row['ticker']}",
                              f"sell when it reaches this price  ·  now {_money(row.get('price'))}  ·  "
                              f"suggested {_money(suggested)}",
                              [("sell limit per share", "money", f"{default:.2f}" if default else "")],
                              save, self)
        elif event.key == pygame.K_x and row and row.get("sell_limit"):
            planner.set_sell_limit(row["ticker"], None)
            return portfolio_start(app, self.selected)
        elif event.key in (pygame.K_d, pygame.K_DELETE) and row:
            planner.delete_holding(row["ticker"])
            self.rows.remove(row)
            self.selected = min(self.selected, max(0, len(self.rows) - 1))
        elif event.key == pygame.K_o and row:
            webbrowser.open(row.get("url") or f"https://www.marketbeat.com/stocks/NASDAQ/{row['ticker']}/")
        elif event.key == pygame.K_r:
            return portfolio_start(app, self.selected)
        elif event.key == pygame.K_ESCAPE:
            return TitleScene()
        self.scroll = min(self.scroll, self.selected)
        self.scroll = max(self.scroll, self.selected - self.ROWS_VISIBLE + 1)
        return None

    def draw(self, app):
        from . import planner
        app.screen.fill(BG)
        app.text("MY PORTFOLIO", app.font.lg, ACCENT, 40, 20)
        app.text(planner.DISCLAIMER, app.font.sm, ACCENT, SCREEN_W - 40, 30, right=True)
        app.text("prices read live on MarketBeat  ·  sell limit in orange = yours, white = suggested",
                 app.font.sm, DIM, 40, 66)
        cols = [(40, "TICKER"), (130, "SHARES"), (220, "PAID IN ALL"), (330, "PRICE NOW"), (450, "VALUE"),
                (580, "GAIN / LOSS"), (790, "ACTION"), (900, "SELL LIMIT"), (1040, "STOP-LOSS"),
                (1170, "RATING")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 100)
        y = 124
        for i in range(self.scroll, min(len(self.rows), self.scroll + self.ROWS_VISIBLE)):
            h = self.rows[i]
            if i == self.selected:
                pygame.draw.rect(app.screen, CARD_SEL, (30, y - 4, SCREEN_W - 60, 32), border_radius=6)
            app.text(h["ticker"], app.font.mono_b, TEXT, 40, y)
            app.text(f"{h['shares']:g}", app.font.mono, TEXT, 130, y)
            app.text(_money(h.get("cost")), app.font.mono, TEXT, 220, y)
            app.text(_money(h.get("price")), app.font.mono, TEXT, 330, y)
            app.text(_money(h.get("value")), app.font.mono, TEXT, 450, y)
            if h.get("gain") is not None:
                app.text(f"{_money(h['gain'])} {_pct(h['gain_pct'], 1)}", app.font.mono,
                         GREEN if h["gain"] >= 0 else RED, 580, y)
            app.text(h["action"], app.font.mono_b, RED if h["action"] == "Sell now" else TEXT, 790, y)
            limit, own = _sell_limit(h)
            app.text(_money(limit), app.font.mono_b if own else app.font.mono, ACCENT if own else TEXT, 900, y)
            app.text(_money(h.get("stop_loss")), app.font.mono, TEXT, 1040, y)
            label = h["signal"]["label"]
            app.text(label, app.font.mono, {"Buy": GREEN, "Sell": RED}.get(label, ACCENT), 1170, y)
            y += 34

        if not self.rows:
            app.text("No stocks saved yet. Press A to add one you own.", app.font.md, TEXT,
                     SCREEN_W // 2, 260, centre=True)
            app.text("ticker, how many shares, and what you paid per share", app.font.sm, DIM,
                     SCREEN_W // 2, 300, centre=True)
        else:
            cost = sum(h.get("cost") or 0 for h in self.rows)
            priced = [h for h in self.rows if h.get("value") is not None]
            value = sum(h["value"] for h in priced)
            gain = sum(h["gain"] for h in priced)
            paid = sum(h["cost"] for h in priced)
            app.text(f"paid {_money(cost)}  ·  worth now {_money(value)}  ·  ", app.font.md, TEXT, 40, y + 8)
            x = 40 + app.font.md.size(f"paid {_money(cost)}  ·  worth now {_money(value)}  ·  ")[0]
            app.text(f"{_money(gain)} {_pct(gain / paid * 100 if paid else None, 1)}", app.font.md,
                     GREEN if gain >= 0 else RED, x, y + 8)
            h = self.rows[self.selected]
            for k, part in enumerate(_wrap(app.font.sm, _facts_line(h["ticker"], self.facts.get(h["ticker"])),
                                           SCREEN_W - 80)[:2]):
                app.text(part, app.font.sm, DIM, 40, y + 52 + k * 22)
            ch = self.chart.get(h["ticker"]) or {}
            for k, line in enumerate(_wrap(app.font.sm, f"{h['ticker']}: {_money(h['purchase_price'])} a share "
                                                        f"on average  ·  chart: 1 week {_pct(ch.get('1w'), 1)}, "
                                                        f"1 month {_pct(ch.get('1m'), 1)}, 1 year "
                                                        f"{_pct(ch.get('1y'), 1)}  ·  {h.get('reason', '')}",
                                           SCREEN_W - 80)[:2]):
                app.text(line, app.font.sm, TEXT, 40, SCREEN_H - 100 + k * 24)
        app.footer("A add   E edit   L set sell limit   X clear my limit   D delete   "
                   "O MarketBeat   R reload   ESC title")
