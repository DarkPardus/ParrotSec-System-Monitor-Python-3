#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
neonmon - animasyonlu, renkli terminal sistem izleyici
Sadece Python 3 standart kütüphanesi gerekir (psutil vb. yok).

Tuşlar:  q çıkış | c tema | a otomatik tema | s işlem sıralama
Kullanım: neonmon [--theme ateş]
"""
import argparse
import curses
import glob
import locale
import os
import platform
import re
import socket
import subprocess
import threading
import time
from collections import deque

HIST = 300
THEMES = {
    "gökkuşağı": [196, 202, 208, 214, 220, 226, 190, 154, 118, 82, 46, 48, 50, 51,
                  45, 39, 33, 27, 57, 93, 129, 165, 201, 199],
    "ateş": [52, 88, 124, 160, 196, 202, 208, 214, 220, 226, 220, 214, 208, 202, 196, 160, 124, 88],
    "okyanus": [17, 18, 19, 20, 21, 27, 33, 39, 45, 51, 50, 49, 48, 43, 38, 33, 27, 21, 19],
    "matrix": [22, 28, 34, 40, 46, 82, 118, 154, 118, 82, 46, 40, 34, 28],
    "neon": [201, 200, 199, 198, 165, 129, 93, 57, 63, 69, 75, 81, 87, 81, 75, 69, 63, 57, 93, 129, 165, 199],
}
GUN = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
AY = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
      "Eylül", "Ekim", "Kasım", "Aralık"]
FS_OK = {"ext2", "ext3", "ext4", "btrfs", "xfs", "vfat", "ntfs", "ntfs3", "f2fs", "fuseblk", "exfat", "zfs"}
DISK_RE = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")

# 5 satırlık blok font (logo + büyük saat)
FONT = {
    "0": ["███", "█ █", "█ █", "█ █", "███"], "1": [" █ ", "██ ", " █ ", " █ ", "███"],
    "2": ["███", "  █", "███", "█  ", "███"], "3": ["███", "  █", "███", "  █", "███"],
    "4": ["█ █", "█ █", "███", "  █", "  █"], "5": ["███", "█  ", "███", "  █", "███"],
    "6": ["███", "█  ", "███", "█ █", "███"], "7": ["███", "  █", "  █", "  █", "  █"],
    "8": ["███", "█ █", "███", "█ █", "███"], "9": ["███", "█ █", "███", "  █", "███"],
    ":": ["░", "█", "░", "█", "░"], ";": [" ", " ", " ", " ", " "], " ": ["  "] * 5,
    "N": ["█  █", "██ █", "█ ██", "█  █", "█  █"], "E": ["███", "█  ", "███", "█  ", "███"],
    "O": ["███", "█ █", "█ █", "█ █", "███"],
}


# ----------------------------------------------------------------- yardımcılar
def rd(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def hb(n):
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def hdur(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return (f"{d}g " if d else "") + f"{h}s {s // 60}d"


def fit(s, w):
    if w <= 0:
        return ""
    return s if len(s) <= w else s[: max(0, w - 1)] + "…"


def render(text):
    rows = [""] * 5
    for k, ch in enumerate(text):
        g = FONT.get(ch, FONT[" "])
        for r in range(5):
            rows[r] += g[r] + (" " if k < len(text) - 1 else "")
    return rows


def put(scr, y, x, s, attr=0):
    H, W = scr.getmaxyx()
    if y < 0 or y >= H or x >= W or not s:
        return
    if x < 0:
        s, x = s[-x:], 0
    s = s[: W - x]
    try:
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# -------------------------------------------------------------------- istatistik
class Stats:
    def __init__(self):
        self.ncpu = os.cpu_count() or 1
        self.t = self.proc_t = 0
        self.prev_cpu = None
        self.cpu_total, self.cpu_cores = 0.0, []
        self.cpu_hist = deque([0] * HIST, maxlen=HIST)
        self.rx = self.tx = self.rx0 = self.tx0 = None
        self.down = self.up = 0.0
        self.down_h = deque([0] * HIST, maxlen=HIST)
        self.up_h = deque([0] * HIST, maxlen=HIST)
        self.dio = None
        self.dread = self.dwrite = 0.0
        self.mem, self.disks, self.temps, self.procs = {}, [], [], []
        self.proc_prev = {}
        self.freq = None
        self.uptime = 0
        self.battery = ""
        self.ip = local_ip()
        self.tick = 0

    def _read_cpu(self):
        out = {}
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                p = line.split()
                v = list(map(int, p[1:9]))
                out[p[0]] = (v[3] + v[4], sum(v))
        return out

    def update(self, now):
        dt = now - self.t if self.t else None
        self.tick += 1
        # CPU
        cur = self._read_cpu()
        if self.prev_cpu:
            def pct(k):
                i0, t0 = self.prev_cpu.get(k, cur[k])
                i1, t1 = cur[k]
                d = t1 - t0
                return 0.0 if d <= 0 else max(0.0, min(100.0, 100.0 * (1 - (i1 - i0) / d)))
            self.cpu_total = pct("cpu")
            keys = sorted((k for k in cur if k != "cpu"), key=lambda k: int(k[3:]))
            self.cpu_cores = [pct(k) for k in keys]
            self.cpu_hist.append(self.cpu_total)
        self.prev_cpu = cur
        # bellek
        m = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            m[k] = int(v.split()[0]) * 1024
        total = m["MemTotal"]
        avail = m.get("MemAvailable", m["MemFree"])
        self.mem = dict(total=total, used=total - avail,
                        cached=m.get("Cached", 0) + m.get("SReclaimable", 0),
                        buffers=m.get("Buffers", 0), swap_t=m.get("SwapTotal", 0),
                        swap_u=m.get("SwapTotal", 0) - m.get("SwapFree", 0))
        # ağ
        rx = tx = 0
        for line in list(open("/proc/net/dev"))[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo" or name.strip().startswith(("docker", "veth", "br-")):
                continue
            f = data.split()
            rx += int(f[0])
            tx += int(f[8])
        if self.rx is None:
            self.rx0, self.tx0 = rx, tx
        elif dt:
            self.down, self.up = max(0, (rx - self.rx) / dt), max(0, (tx - self.tx) / dt)
            self.down_h.append(self.down)
            self.up_h.append(self.up)
        self.rx, self.tx = rx, tx
        # disk I/O
        r = w = 0
        for line in open("/proc/diskstats"):
            f = line.split()
            if DISK_RE.match(f[2]):
                r += int(f[5]) * 512
                w += int(f[9]) * 512
        if self.dio and dt:
            self.dread, self.dwrite = max(0, (r - self.dio[0]) / dt), max(0, (w - self.dio[1]) / dt)
        self.dio = (r, w)
        # sık değişmeyenler
        self.uptime = float(rd("/proc/uptime", "0").split()[0])
        if self.tick % 3 == 1:
            self.disks = self._disks()
            self.temps = self._temps()
            self.freq = self._freq()
            self.battery = self._battery()
        if self.tick % 30 == 0:
            self.ip = local_ip()
        if now - self.proc_t >= 2:
            self._procs(now)
        self.t = now

    def _disks(self):
        seen, res = set(), []
        for line in open("/proc/mounts"):
            dev, mp, fs = line.split()[:3]
            mp = mp.replace("\\040", " ")
            if fs not in FS_OK or dev in seen or mp.startswith(("/snap", "/var/lib/docker")):
                continue
            try:
                st = os.statvfs(mp)
            except OSError:
                continue
            tot = st.f_blocks * st.f_frsize
            if tot:
                res.append((mp, tot, tot - st.f_bavail * st.f_frsize))
                seen.add(dev)
        return res

    def _temps(self):
        res = []
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name, n = rd(hw + "/name", "hw"), 0
            for tf in sorted(glob.glob(hw + "/temp*_input")):
                try:
                    t = int(rd(tf)) / 1000
                except ValueError:
                    continue
                if 0 < t < 150 and n < 4:
                    res.append((f"{name}:{rd(tf.replace('_input', '_label')) or 'sıc'}", t))
                    n += 1
        if not res:
            for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
                try:
                    res.append((rd(z + "/type", "zone"), int(rd(z + "/temp")) / 1000))
                except ValueError:
                    pass
        return res

    def _freq(self):
        v = []
        for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
            try:
                v.append(int(rd(p)))
            except ValueError:
                pass
        return sum(v) / len(v) / 1e6 if v else None

    def _battery(self):
        for p in glob.glob("/sys/class/power_supply/BAT*"):
            cap = rd(p + "/capacity")
            if cap:
                return f"pil %{cap}" + (" ⚡" if rd(p + "/status") == "Charging" else "")
        return "AC güç"

    def _procs(self, now):
        clk, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        dt = now - self.proc_t if self.proc_t else None
        cur, rows = {}, []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat") as f:
                    s = f.read()
                e = s.rfind(")")
                comm = s[s.find("(") + 1:e]
                fl = s[e + 2:].split()
                ticks = int(fl[11]) + int(fl[12])
                rss = int(fl[21]) * page
            except (OSError, ValueError, IndexError):
                continue
            cur[pid] = ticks
            cpu = 0.0
            if dt and pid in self.proc_prev:
                cpu = (ticks - self.proc_prev[pid]) / clk / dt * 100
            rows.append((pid, comm, cpu, rss))
        self.proc_prev, self.proc_t, self.procs = cur, now, rows


# ------------------------------------------------------------------------ renkler
class Colors:
    def __init__(self, theme):
        curses.start_color()
        curses.use_default_colors()
        self.n256 = curses.COLORS >= 256
        self.names = list(THEMES)
        self.ti = self.names.index(theme) if theme in THEMES else 0
        self.apply()

    def apply(self):
        pal = THEMES[self.names[self.ti]] if self.n256 else [1, 2, 3, 4, 5, 6, 5, 4, 3, 2]
        self.N = len(pal)
        for i, col in enumerate(pal):
            curses.init_pair(1 + i, col, -1)
        for i, col in enumerate([46, 118, 226, 208, 196] if self.n256 else [2, 2, 3, 1, 1]):
            curses.init_pair(40 + i, col, -1)
        curses.init_pair(50, 245 if self.n256 else 7, -1)
        curses.init_pair(51, 15 if self.n256 else 7, -1)
        self.gray = curses.color_pair(50)
        self.white = curses.color_pair(51) | curses.A_BOLD

    def nxt(self):
        self.ti = (self.ti + 1) % len(self.names)
        self.apply()

    @property
    def name(self):
        return self.names[self.ti]

    def g(self, i):
        return curses.color_pair(1 + int(i) % self.N)

    def lv(self, level):
        return curses.color_pair(40 + max(0, min(4, level)))


# --------------------------------------------------------------------- uygulama
class App:
    def __init__(self, args):
        self.args = args
        self.c = Colors(args.theme)
        self.st = Stats()
        self.phase, self.auto, self.theme_t = 0, True, time.time()
        self.sort = "cpu"
        self.user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
        self.host = socket.gethostname()
        self.kernel = platform.release()
        self.osname = self._osname()
        self.cpu_model = self._cpu_model()
        self.machine = self._machine()
        self.gpu = "…"
        threading.Thread(target=self._gpu, daemon=True).start()
        self.last = 0

    # -- statik donanım bilgisi
    @staticmethod
    def _osname():
        for line in rd("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
        return platform.system()

    @staticmethod
    def _cpu_model():
        try:
            for line in open("/proc/cpuinfo"):
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    m = re.sub(r"\((R|TM|r|tm)\)|CPU|@.*", "", line.split(":", 1)[1])
                    return re.sub(r"\s+", " ", m).strip()
        except OSError:
            pass
        return platform.processor() or platform.machine()

    @staticmethod
    def _machine():
        v, p = rd("/sys/class/dmi/id/sys_vendor"), rd("/sys/class/dmi/id/product_name")
        return (f"{v} {p}".strip() or rd("/sys/firmware/devicetree/base/model") or "-").strip("\x00")

    def _gpu(self):
        try:
            out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                if re.search(r"VGA|3D controller|Display controller", line):
                    g = re.sub(r"\s*\(rev [^)]*\)", "", line.split(": ", 1)[1])
                    m = re.search(r"\[([^\]]+)\]\s*$", g) or re.search(r"\[([^\]]+)\]", g)
                    self.gpu = (m.group(1) if m else g)[:30]
                    return
        except Exception:
            pass
        self.gpu = "-"

    # -- çizim parçaları
    def box(self, scr, y, x, h, w, title=""):
        if h < 3 or w < 8:
            return
        c, ph = self.c, self.phase
        for i in range(w):
            top = "╭" if i == 0 else "╮" if i == w - 1 else "─"
            bot = "╰" if i == 0 else "╯" if i == w - 1 else "─"
            a = c.g(i + ph)
            put(scr, y, x + i, top, a)
            put(scr, y + h - 1, x + i, bot, a)
        for j in range(1, h - 1):
            put(scr, y + j, x, "│", c.g(ph - j))
            put(scr, y + j, x + w - 1, "│", c.g(ph + w + j))
        for k, ch in enumerate(f" {fit(title, w - 6)} "):
            put(scr, y, x + 2 + k, ch, c.g(ph * 2 + k) | curses.A_BOLD)

    def bar(self, scr, y, x, w, frac):
        c = self.c
        frac = max(0.0, min(1.0, frac))
        full = int(frac * w)
        i = 0
        while i < full:
            lv = min(4, i * 5 // w)
            j = i
            while j < full and min(4, j * 5 // w) == lv:
                j += 1
            put(scr, y, x + i, "█" * (j - i), c.lv(lv))
            i = j
        if full < w:
            part = " ▏▎▍▌▋▊▉"[int((frac * w - full) * 8)]
            if part != " ":
                put(scr, y, x + full, part, c.lv(min(4, full * 5 // w)))
            rest = w - full - (1 if part != " " else 0)
            put(scr, y, x + w - rest, "░" * rest, c.gray)

    def meter(self, scr, y, x, w, label, frac, text, lw=7):
        bw = w - lw - len(text) - 2
        put(scr, y, x, f"{fit(label, lw - 1):<{lw}}", self.c.g(self.phase // 2 + y))
        if bw >= 4:
            self.bar(scr, y, x + lw, bw, frac)
            put(scr, y, x + lw + bw + 1, text, self.c.white)
        else:
            put(scr, y, x + lw, fit(text, w - lw), self.c.lv(int(max(0.0, min(1.0, frac)) * 5)) | curses.A_BOLD)

    def graph(self, scr, y, x, h, w, data, maxv, mode="lvl", off=0):
        if h < 1 or w < 2:
            return
        c = self.c
        vals = list(data)[-w:]
        pad = w - len(vals)
        blocks = " ▁▂▃▄▅▆▇█"
        for col, v in enumerate(vals):
            units = int(max(0.0, min(1.0, v / maxv)) * h * 8)
            for r in range(h):
                u = units - r * 8
                if u <= 0:
                    break
                attr = c.lv(r * 5 // h) if mode == "lvl" else c.g(off + r * 2 + self.phase // 2)
                put(scr, y + h - 1 - r, x + pad + col, blocks[min(8, u)], attr)

    # -- paneller
    def header(self, scr, W):
        c, st = self.c, self.st
        lt = time.localtime()
        title = f"NEONMON · {GUN[lt.tm_wday]} {lt.tm_mday} {AY[lt.tm_mon - 1]} {lt.tm_year}"
        big = W >= 100
        if not big:
            title += time.strftime(" · %H:%M:%S")
        self.box(scr, 0, 0, 7, W, title)
        logo = render("NEON")
        for r, row in enumerate(logo):
            for ci, ch in enumerate(row):
                if ch != " ":
                    put(scr, 1 + r, 2 + ci, ch, c.g(ci + r * 2 + self.phase) | curses.A_BOLD)
        ix = 2 + len(logo[0]) + 3
        clock_w = 27
        iw = W - ix - 3 - (clock_w + 2 if big else 0)
        m = st.mem
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0
        lines = [
            f"{self.user}@{self.host}",
            f"{self.osname}  ·  Linux {self.kernel}",
            f"{self.cpu_model}  ({st.ncpu} çekirdek)",
            f"RAM {hb(m.get('total', 0))}  ·  GPU {self.gpu}  ·  {self.machine}",
            f"açık {hdur(st.uptime)}  ·  IP {st.ip}  ·  {st.battery}  ·  yük {load:.2f}",
        ]
        for i, t in enumerate(lines):
            attr = (c.g(self.phase // 2 + i * 3) | curses.A_BOLD) if i == 0 else c.white
            put(scr, 1 + i, ix, fit(t, iw), attr)
        if big:
            txt = time.strftime("%H:%M:%S")
            if int(time.time() * 2) % 2:
                txt = txt.replace(":", ";")
            for r, row in enumerate(render(txt)):
                for ci, ch in enumerate(row):
                    if ch != " ":
                        put(scr, 1 + r, W - clock_w - 3 + ci, ch,
                            c.g(ci + self.phase + r * 2) | curses.A_BOLD)

    def cpu_box(self, scr, y, x, h, w):
        st = self.st
        self.box(scr, y, x, h, w, f"İŞLEMCİ  %{st.cpu_total:4.1f}")
        ix, iw, ih = x + 2, w - 4, h - 2
        self.meter(scr, y + 1, ix, iw, "Toplam", st.cpu_total / 100, f"{st.cpu_total:5.1f}%")
        freq = f"{st.freq:.2f} GHz" if st.freq else "- GHz"
        l1, l5, l15 = os.getloadavg()
        put(scr, y + 2, ix, fit(f"{freq}  ·  yük {l1:.2f} {l5:.2f} {l15:.2f}  ·  {len(st.procs)} işlem", iw),
            self.c.gray)
        n = len(st.cpu_cores)
        cols = max(1, iw // 17)
        crow = min(-(-n // cols), max(0, ih - 2 - 3))
        gh = ih - 2 - crow
        self.graph(scr, y + 3, ix, gh, iw, st.cpu_hist, 100)
        cw = iw // cols
        for idx in range(min(n, cols * crow)):
            rr, cc = divmod(idx, cols)
            px, py = ix + cc * cw, y + h - 1 - crow + rr
            put(scr, py, px, f"{idx:>2} ", self.c.gray)
            self.bar(scr, py, px + 3, cw - 9, st.cpu_cores[idx] / 100)
            put(scr, py, px + cw - 5, f"{st.cpu_cores[idx]:3.0f}%", self.c.white)

    def mem_box(self, scr, y, x, h, w):
        st, m = self.st, self.st.mem
        self.box(scr, y, x, h, w, "BELLEK & DİSK")
        ix, iw, ih = x + 2, w - 4, h - 2
        if not m:
            return
        rows = [
            ("RAM", m["used"] / m["total"], f"{hb(m['used'])}/{hb(m['total'])} {100 * m['used'] / m['total']:3.0f}%"),
        ]
        if m["swap_t"]:
            rows.append(("Takas", m["swap_u"] / m["swap_t"],
                         f"{hb(m['swap_u'])}/{hb(m['swap_t'])} {100 * m['swap_u'] / m['swap_t']:3.0f}%"))
        r = 0
        for lab, fr, txt in rows:
            self.meter(scr, y + 1 + r, ix, iw, lab, fr, txt)
            r += 1
        if r < ih:
            put(scr, y + 1 + r, ix, fit(f"önbellek {hb(m['cached'])} · tampon {hb(m['buffers'])}"
                                         f"  ·  disk G/Ç ↓{hb(st.dread)}/s ↑{hb(st.dwrite)}/s", iw), self.c.gray)
            r += 1
        for mp, tot, used in st.disks:
            if r >= ih:
                break
            lab = mp if len(mp) <= 6 else "…" + mp[-5:]
            self.meter(scr, y + 1 + r, ix, iw, lab, used / tot, f"{hb(used)}/{hb(tot)} {100 * used / tot:3.0f}%")
            r += 1

    def net_box(self, scr, y, x, h, w):
        st = self.st
        self.box(scr, y, x, h, w, f"AĞ  ↓{hb(st.down)}/s  ↑{hb(st.up)}/s")
        ix, iw, ih = x + 2, w - 4, h - 2
        free = max(2, ih - 2)
        gh = [(free + 1) // 2, free // 2]
        down_t = f"toplam {hb((st.rx or 0) - (st.rx0 or 0))}"
        up_t = f"toplam {hb((st.tx or 0) - (st.tx0 or 0))}"
        base = y + 1
        for n, (lab, hist, now, tot, off) in enumerate((
            ("↓ İNDİRME", st.down_h, st.down, down_t, 0), ("↑ YÜKLEME", st.up_h, st.up, up_t, 8),
        )):
            peak = max(max(hist) * 1.1, 50_000)
            put(scr, base, ix, lab, self.c.g(self.phase // 2 + off) | curses.A_BOLD)
            put(scr, base, ix + 11, fit(f"{hb(now)}/s · {tot} · zirve {hb(max(hist))}/s", iw - 11), self.c.gray)
            self.graph(scr, base + 1, ix, gh[n], iw, hist, peak, "theme", off)
            base += 1 + gh[n]

    def temp_box(self, scr, y, x, h, w):
        self.box(scr, y, x, h, w, "SICAKLIK")
        ix, iw, ih = x + 2, w - 4, h - 2
        if not self.st.temps:
            put(scr, y + 1, ix, "sensör bulunamadı", self.c.gray)
        for i, (lab, t) in enumerate(self.st.temps[:ih]):
            self.meter(scr, y + 1 + i, ix, iw, lab.split(":")[-1][:11], t / 100, f"{t:3.0f}°C", lw=12)

    def proc_box(self, scr, y, x, h, w):
        st = self.st
        self.box(scr, y, x, h, w, f"İŞLEMLER  (sırala: {'CPU' if self.sort == 'cpu' else 'BELLEK'})")
        ix, iw, ih = x + 2, w - 4, h - 2
        nw = max(4, iw - 24)
        put(scr, y + 1, ix, f"{'PID':>7} {'İSİM':<{nw}} {'CPU%':>6} {'BELLEK':>8}", self.c.gray)
        idx = 2 if self.sort == "cpu" else 3
        for i, (pid, name, cpu, rss) in enumerate(sorted(st.procs, key=lambda r: r[idx], reverse=True)[:ih - 1]):
            yy = y + 2 + i
            put(scr, yy, ix, f"{pid:>7} ", self.c.gray)
            put(scr, yy, ix + 8, f"{fit(name, nw):<{nw}}", self.c.g(self.phase // 2 + i * 2))
            put(scr, yy, ix + 9 + nw, f"{cpu:6.1f}", self.c.lv(int(min(cpu, 100) / 20)) | curses.A_BOLD)
            put(scr, yy, ix + 16 + nw, f"{hb(rss):>9}", self.c.white)

    def footer(self, scr, y, W):
        txt = (f" q çıkış  c tema  a otomatik-tema:{'açık' if self.auto else 'kapalı'}  "
               f"s sırala  │  tema: {self.c.name}")
        for i, ch in enumerate(fit(txt, W - 1)):
            put(scr, y, i, ch, self.c.g(i + self.phase) if ch != " " else 0)

    def draw(self, scr):
        scr.erase()
        H, W = scr.getmaxyx()
        if H < 26 or W < 90:
            put(scr, 0, 0, f"Terminal küçük ({W}x{H}). En az 90x26 gerekli.", self.c.white)
            return
        self.header(scr, W)
        top, avail = 7, H - 8
        lw = W // 2
        rw = W - lw
        ch = max(10, int(avail * 0.58))
        self.cpu_box(scr, top, 0, ch, lw)
        self.mem_box(scr, top + ch, 0, avail - ch, lw)
        nh = max(7, int(avail * 0.34))
        th = max(6, int(avail * 0.22))
        self.net_box(scr, top, lw, nh, rw)
        self.temp_box(scr, top + nh, lw, th, rw)
        self.proc_box(scr, top + nh + th, lw, avail - nh - th, rw)
        self.footer(scr, H - 1, W)

    def tick(self, now):
        self.phase = int(now * 6)
        if now - self.last >= 1.0:
            self.st.update(now)
            self.last = now
        if self.auto and now - self.theme_t > 45:
            self.c.nxt()
            self.theme_t = now


def main(scr, args):
    curses.curs_set(0)
    scr.timeout(100)
    app = App(args)
    while True:
        app.tick(time.time())
        app.draw(scr)
        scr.refresh()
        k = scr.getch()
        if k in (ord("q"), ord("Q")):
            break
        if k == ord("c"):
            app.c.nxt()
            app.theme_t = time.time()
        elif k == ord("a"):
            app.auto = not app.auto
        elif k == ord("s"):
            app.sort = "mem" if app.sort == "cpu" else "cpu"


if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, "")
    ap = argparse.ArgumentParser(description="neonmon - renkli terminal sistem izleyici")
    ap.add_argument("--theme", default="gökkuşağı", choices=list(THEMES))
    a = ap.parse_args()
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(main, a)
    except KeyboardInterrupt:
        pass
