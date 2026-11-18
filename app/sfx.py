"""Sound effect library.

Sounds live in sfx/<kind>/ folders (mp3/wav/ogg/m4a). Add them from the dashboard
(upload a file or paste a link) or drop files into the folders yourself.
For each moment the app picks a random sound from the folders that fit it, and
prefers your own files over the simple built-in ones.
Built-in sounds are generated with ffmpeg on first start so the app works offline.
"""
import ipaddress
import logging
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from . import tools
from .config import ROOT

log = logging.getLogger("sfx")
SFX_DIR = ROOT / "sfx"
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
MAX_BYTES = 8 * 1024 * 1024

# folder -> what to put in it (shown in the dashboard)
KINDS = {
    "boom": "Vine boom / bass boom",
    "fahh": "FAHHH meme",
    "bruh": "Bruh",
    "huh": "Huh? (huh cat)",
    "emotional_damage": "Emotional damage",
    "metal_pipe": "Metal pipe falling",
    "taco_bell": "Taco Bell bong",
    "oh_no": "Oh no no no (laugh)",
    "sad_violin": "Sad violin",
    "error": "Windows XP error",
    "sus": "Among Us / sus",
    "goofy": "Goofy ahh / cartoon boing",
    "laugh": "Laugh track / meme laugh",
    "airhorn": "MLG airhorn",
    "rizz": "Rizz sound",
    "wow": "Anime wow / ding",
    "fail": "Sad trombone",
    "suspense": "Suspense riser",
    "whoosh": "Whoosh (clip start)",
}

# moment type -> folders that fit it
SFX_FOR = {
    "laugh": ["laugh", "goofy", "oh_no", "boom"],
    "hype": ["airhorn", "rizz", "wow"],
    "shock": ["boom", "fahh", "taco_bell", "huh", "metal_pipe", "wow"],
    "fail": ["fahh", "emotional_damage", "sad_violin", "oh_no", "error", "fail", "bruh"],
    "suspense": ["suspense", "oh_no", "sus"],
    "bruh": ["bruh", "huh", "sus", "goofy", "fahh"],
    "loud": ["metal_pipe", "boom", "taco_bell", "fahh"],
}

# ffmpeg lavfi recipes for simple built-in sounds (no downloads needed)
BUILTIN = {
    "boom": ("aevalsrc='0.95*sin(2*PI*55*t*(1-0.35*t))*exp(-3.2*t)+0.4*sin(2*PI*110*t)*exp(-6*t)':d=1.4:s=44100",
             "lowpass=f=500,volume=2.2,alimiter=limit=0.95"),
    "airhorn": ("aevalsrc='0.22*(sgn(sin(2*PI*466*t))+sgn(sin(2*PI*587*t))+0.6*sgn(sin(2*PI*349*t)))"
                "*lt(mod(t,0.5),0.42)':d=1.5:s=44100",
                "highpass=f=300,lowpass=f=3500,afade=t=out:st=1.25:d=0.25"),
    "wow": ("aevalsrc='0.55*sin(2*PI*1568*t)*exp(-3.5*t)+0.3*sin(2*PI*2349*t)*exp(-4.5*t)':d=1.6:s=44100", "anull"),
    "fail": ("aevalsrc='0.35*sin(2*PI*t*if(lt(t,0.45),466,if(lt(t,0.9),440,if(lt(t,1.35),415,"
             "392+5*sin(2*PI*6*t)))))':d=2.4:s=44100",
             "lowpass=f=1800,afade=t=out:st=1.9:d=0.5,aecho=0.6:0.4:40:0.3"),
    "suspense": ("aevalsrc='0.4*sin(2*PI*(150*t+110*t*t))*min(t/1.6,1)*(0.7+0.3*sin(2*PI*9*t))':d=1.8:s=44100",
                 "afade=t=out:st=1.65:d=0.15"),
    "whoosh": ("anoisesrc=d=0.65:c=pink:a=0.8:r=44100",
               "bandpass=f=1400:width_type=h:w=1800,afade=t=in:d=0.3,afade=t=out:st=0.32:d=0.33,volume=1.5"),
    "goofy": ("aevalsrc='0.5*sin(2*PI*(300*t+900*t*t*exp(-2*t))*(1+0.3*sin(2*PI*14*t)))*exp(-2.2*t)':d=1.0:s=44100",
              "afade=t=out:st=0.8:d=0.2"),
    "metal_pipe": ("aevalsrc='0.5*(sin(2*PI*523*t)+0.7*sin(2*PI*1307*t)+0.5*sin(2*PI*2711*t)"
                   "+0.3*sin(2*PI*4129*t))*exp(-2.5*t)':d=2.0:s=44100",
                   "aecho=0.8:0.5:60|110:0.4|0.25,alimiter=limit=0.95"),
    "error": ("aevalsrc='0.4*sin(2*PI*if(lt(t,0.16),659,if(lt(t,0.32),523,784))*t)*exp(-1.5*t)':d=0.9:s=44100",
              "afade=t=out:st=0.7:d=0.2"),
}


def ensure_library():
    for kind in KINDS:
        d = SFX_DIR / kind
        d.mkdir(parents=True, exist_ok=True)
        if kind in BUILTIN and not (d / "_builtin.wav").exists():
            src, af = BUILTIN[kind]
            try:
                tools.run([tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
                           "-af", af, "-ac", "2", str(d / "_builtin.wav")], timeout=60)
            except Exception as e:  # noqa: BLE001
                log.warning("could not create built-in %s sound: %s", kind, e)
    readme = SFX_DIR / "README.txt"
    readme.write_text(__doc__.strip() + "\n\nFolders:\n" +
                      "\n".join(f"  {k:<17} {v}" for k, v in KINDS.items()) +
                      "\n\nFree sounds: https://pixabay.com/sound-effects/ . Meme sounds: https://www.myinstants.com/ "
                      "(paste the page link in the dashboard). Check the license before posting.\n",
                      encoding="utf-8")


def _files(kind):
    d = SFX_DIR / kind
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT)


def available() -> dict:
    """{kind: {"files": [paths], "own": bool}}. A folder's built-in sound is only used
    while you haven't added your own sound to that folder."""
    out = {}
    for kind in KINDS:
        files = _files(kind)
        own = [p for p in files if not p.name.startswith("_builtin")]
        chosen = own or files
        if chosen:
            out[kind] = {"files": [str(p.resolve()) for p in chosen], "own": bool(own)}
    return out


def library():
    """For the dashboard."""
    return [{"kind": k, "label": v, "files": [{"name": p.name, "builtin": p.name.startswith("_builtin"),
                                               "url": f"/sfx/{k}/{p.name}"} for p in _files(k)]}
            for k, v in KINDS.items()]


# ------------------------------------------------------------- adding sounds
def _safe_name(name: str, default_ext=".mp3") -> str:
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9 _-]+", "", stem).strip().replace(" ", "_")[:60] or "sound"
    if stem.startswith("_builtin"):
        stem = "my" + stem
    return stem + (ext if ext in AUDIO_EXT else default_ext)


def _folder(kind):
    if kind not in KINDS:
        raise ValueError("unknown sound folder")
    d = SFX_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(kind: str, filename: str, data: bytes) -> str:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("file is empty or bigger than 8 MB")
    name = _safe_name(filename)
    path = _folder(kind) / name
    path.write_bytes(data)
    _check_audio(path)
    return name


def _check_public(url: str):
    """Only fetch public http(s) addresses (no localhost / home network)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("link must start with http:// or https://")
    for info in socket.getaddrinfo(u.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("that link points to a private address")


def import_url(kind: str, url: str) -> str:
    """Downloads a sound. Accepts a direct .mp3 link or a page (like a myinstants.com
    sound page): the first .mp3 found on the page is used."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36"}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=False) as c:
        for _ in range(4):  # follow a few redirects, checking each hop
            _check_public(url)
            r = c.get(url)
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "text/html" in ctype:
                m = (re.search(r"""<meta[^>]+property=["']og:audio(?::url)?["'][^>]+content=["']([^"']+)""", r.text, re.I)
                     or re.search(r"""["'(]([^"'()\s]+\.(?:mp3|wav|ogg|m4a))["')]""", r.text, re.I))
                if not m:
                    raise ValueError("no sound file found on that page")
                url = urljoin(url, m.group(1))
                continue
            if len(r.content) > MAX_BYTES:
                raise ValueError("sound is bigger than 8 MB")
            name = Path(urlparse(url).path).name or "sound.mp3"
            return save_upload(kind, name, r.content)
    raise ValueError("too many redirects")


def _check_audio(path: Path):
    """Makes sure ffmpeg can read it, so a bad file never breaks a clip."""
    try:
        tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-t", "1",
                   "-f", "null", "-"], timeout=30)
    except Exception:
        path.unlink(missing_ok=True)
        raise ValueError("that file isn't a playable sound")


def delete(kind: str, name: str) -> bool:
    path = (_folder(kind) / name).resolve()
    if path.parent != (SFX_DIR / kind).resolve() or not path.is_file() or path.name.startswith("_builtin"):
        return False
    path.unlink()
    return True


# ------------------------------------------------------------------- mixing
def mix_args(picks, volume: float):
    """Extra ffmpeg inputs and an audio filter that mixes sounds over input 0's audio.
    Returns (input_args, filter_string, output_label)."""
    inputs, chains, labels = [], [], ["[s0]"]
    chains.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo[s0]")
    for i, p in enumerate(picks, start=1):
        inputs += ["-i", p["file"]]
        ms = int(p["time"] * 1000)
        # long files are cut to 4 s so a whole song never plays over the clip
        chains.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:4,"
                      f"afade=t=out:st=3.5:d=0.5,volume={volume:.2f},adelay={ms}|{ms}[s{i}]")
        labels.append(f"[s{i}]")
    chains.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
                  "alimiter=limit=0.95[aout]")
    return inputs, ";".join(chains), "[aout]"
