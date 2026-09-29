"""A menu screen for the screener, in the style of the drift-racer menus.

Arrow keys move, ENTER confirms, ESC goes back. Screens run as a small state machine:
TITLE -> CATEGORY -> COUNT -> PRICE -> RESULTS.

Nothing here fetches data; it re-slices what is already in data/screener.db, so every
screen responds immediately.
"""
import time
import webbrowser

import pygame

from . import db, explore

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
        pygame.display.set_caption("Stock screener — Yahoo Finance")
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
        """S&P 500 closes from Yahoo, for relative-strength comparisons."""
        if self._benchmark is None:
            try:
                from . import yahoo
                df = yahoo.history("SPY")
                self._benchmark = df["Close"] if not df.empty else False
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

    def draw(self, app):
        pass


class TitleScene(Scene):
    def key(self, event, app):
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            return CategoryScene()
        if event.key == pygame.K_r:
            scene = RefreshScene(self)
            scene.begin(app)
            return scene
        if event.key == pygame.K_ESCAPE:
            app.running = False
        return None

    def draw(self, app):
        app.screen.fill(BG)
        app.text("STOCK SCREENER", app.font.xl, ACCENT, SCREEN_W // 2, 200, centre=True)
        loaded = (f"{len(app.bulk):,} stocks  ·  {len(app.prices):,} priced  ·  "
                  f"{len(app.detail):,} full profiles")
        app.text(loaded, app.font.md, DIM, SCREEN_W // 2, 290, centre=True)
        app.text("ENTER  -  choose what to screen", app.font.md, TEXT, SCREEN_W // 2, 400, centre=True)
        app.text("R  -  refresh: re-run the whole pipeline from Yahoo", app.font.md, GREEN,
                 SCREEN_W // 2, 440, centre=True)
        app.text("Arrows move     ENTER confirms     ESC back", app.font.sm, DIM,
                 SCREEN_W // 2, 500, centre=True)
        stamp = app.data_age()
        app.text(f"Showing data collected {stamp}." if stamp else
                 "No data yet — press R to collect it.", app.font.sm, DIM,
                 SCREEN_W // 2, 560, centre=True)


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
            webbrowser.open(stored or f"https://finance.yahoo.com/quote/{row['ticker']}")
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

        cols = [(50, "#"), (95, "TICKER"), (200, "COMPANY"), (520, "PRICE"),
                (625, "1-MO"), (700, "RSI"), (790, "$VOL"), (890, "B/H/S"),
                (985, "1MO ODDS"), (1130, "SCORE")]
        for x, label in cols:
            app.text(label, app.font.xs, DIM, x, 100)
        pygame.draw.line(app.screen, (46, 46, 56), (40, 122), (SCREEN_W - 40, 122))

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
            app.text(r["company"][:36], app.font.mono, TEXT, 200, y)
            price = f"${r['price']:,.2f}" if r["price"] is not None else "-"
            app.text(price, app.font.mono, TEXT, 600, y, right=True)

            ret = r["ret_1m"]
            ret_s = f"{ret * 100:+.1f}%" if ret is not None else "-"
            app.text(ret_s, app.font.mono, GREEN if (ret or 0) > 0 else RED, 685, y, right=True)

            rsi = r["rsi"]
            rsi_col = GREEN if rsi is not None and 50 <= rsi <= 70 else ACCENT if rsi and rsi > 70 else DIM
            app.text(f"{rsi:.0f}" if rsi is not None else "-", app.font.mono, rsi_col, 745, y, right=True)

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
        app.footer("UP / DOWN select     ENTER analyse + chart     O open on Yahoo     R start over     ESC back")


def run():
    app = App()
    if not app.bulk:
        print("No data yet — run `python run.py all` first.")
        return
    scene: Scene = TitleScene()
    while app.running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                app.running = False
            elif event.type == pygame.KEYDOWN:
                nxt = scene.key(event, app)
                if nxt is not None:
                    scene = nxt
        scene.draw(app)
        pygame.display.flip()
        app.clock.tick(60)
    pygame.quit()


class DetailScene(Scene):
    """One stock: a real price chart with moving averages and MACD, every indicator's
    verdict, and a link to the Yahoo Finance page the data came from."""

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
        return stored or f"https://finance.yahoo.com/quote/{self.row['ticker']}"

    def load(self, app):
        self.started = True
        try:
            from . import ta, yahoo
            row = app.bulk.get(self.row["ticker"], {}) or {}
            df = yahoo.history(self.row["ticker"])
            if df.empty:
                self.error = "Yahoo has no price history for this listing"
                return
            self.currency = row.get("currency") or "USD"
            self.analysis = ta.analyse(self.row["ticker"], df["Close"],
                                       app.benchmark(), row)
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
        price = f"${r['price']:,.2f}" if r["price"] is not None else "-"
        app.text(price, app.font.lg, TEXT, SCREEN_W - 40, 26, right=True)
        ret = r["ret_1m"]
        app.text(f"{ret * 100:+.1f}% in a month" if ret is not None else "",
                 app.font.md, GREEN if (ret or 0) > 0 else RED, SCREEN_W - 40, 80, right=True)

        if not self.started:
            app.text("analysing price history...", app.font.md, DIM, SCREEN_W // 2, 300, centre=True)
            return
        if self.error:
            app.text(self.error, app.font.md, RED, SCREEN_W // 2, 300, centre=True)
            app.footer("O open the Yahoo Finance page     ESC back")
            return

        a = self.analysis
        self.chart(app, 40, 130, 760, 300)
        self.macd_panel(app, 40, 442, 760, 110)

        app.text("MY OWN ANALYSIS", app.font.sm, ACCENT, 830, 130)
        app.text(f"{a.score:.0f}", app.font.xl, GREEN if a.score >= 70 else ACCENT if a.score >= 50 else RED,
                 830, 152)
        app.text(a.summary, app.font.sm, DIM, 960, 190)
        app.text("computed here from Yahoo's", app.font.xs, DIM, 830, 232)
        app.text("daily price history", app.font.xs, DIM, 830, 250)

        y = 284
        for s in a.signals:
            colour = GREEN if s.score >= 0.6 else ACCENT if s.score >= 0.4 else RED
            app.text(s.name, app.font.sm, TEXT, 830, y)
            value = f"{s.value:,.2f}" if s.value is not None else "-"
            app.text(value, app.font.sm, DIM, SCREEN_W - 40, y, right=True)
            app.text(s.verdict, app.font.xs, colour, 830, y + 20)
            y += 46

        self.projections(app, 40, 566)
        app.text(self.url(app), app.font.xs, BLUE, 40, 706)
        app.footer("O or ENTER open the Yahoo Finance page     ESC back")


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
            try:
                from . import db, notes, pipeline, calibrate, explore
                db.wipe()
                say("cleared previous data — starting from nothing")
                notes.start("Screener — refreshed from the menu")

                say("reading the market from Yahoo...")
                total = pipeline.sweep()
                say(f"swept {total:,} stocks")

                say("letting Yahoo's screens pick candidates...")
                picked = pipeline.discover()
                say(f"{len(picked):,} candidates surfaced")

                say("downloading price history...")
                priced = pipeline.prices(picked)
                say(f"{len(priced):,} priced")

                say("fetching analyst data...")
                finalists = [t for t in picked if t in priced] or picked
                with db.connect() as con:
                    db.set_meta(con, "finalists", finalists)
                pipeline.details(finalists)
                say("analyst data in")

                say("measuring historical odds...")
                calibrate.build(sample=200)
                say("odds table rebuilt")

                notes.close()
                with db.connect() as con:
                    app.bulk, app.prices, app.detail = explore.load(con)
                app.compute()
                say(f"done — {len(app.rows)} stocks ready")
                self.done = True
            except Exception as exc:
                self.failed = f"{type(exc).__name__}: {exc}"
                self.done = True

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
        app.text("live from Yahoo Finance — nothing is reused from the last run",
                 app.font.sm, DIM, SCREEN_W // 2, 110, centre=True)

        y = 180
        for line in self.lines[-14:]:
            app.text(line, app.font.mono, TEXT, 180, y)
            y += 28

        if not self.done:
            dots = "." * (1 + int(time.time() * 2) % 3)
            app.text(f"working{dots}", app.font.md, ACCENT, 180, y + 10)
            app.footer("this takes a couple of minutes")
        elif self.failed:
            app.text(self.failed[:90], app.font.md, RED, 180, y + 10)
            app.footer("ENTER to go back")
        else:
            app.text("finished", app.font.md, GREEN, 180, y + 10)
            app.footer("ENTER to see the results")
